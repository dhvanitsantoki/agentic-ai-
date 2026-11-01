"""Central sending policy. Every outbound e-mail is evaluated here before it can be sent.

evaluate(email)        -> content/recipient decision: blocked? needs a human? may auto-send?
can_dispatch_now(email)-> operational gate: automation state, mailbox caps, recipient business hours.
"""
import re
from dataclasses import dataclass, field

from django.utils import timezone

from .models import AutomationState, ExecutionMode, SystemSettings

# Topics that ALWAYS need a human, in every execution mode.
SENSITIVE_PATTERNS = {
    "price": r"(?:[$€£₹]\s?\d|\b(?:usd|eur|inr|gbp|aed|rs\.?)\s?\d|\bprice[sd]?\b|\bpricing\b|\bper (?:piece|pc|unit)\b|\bcost of\b|\brates?\b(?= (?:for|of|per)))",
    "quotation": r"\bquot(?:e|ation|ed)\b|\bproforma\b|\bpro-forma\b",
    "discount": r"\bdiscount|\brebate|\bspecial (?:price|offer|rate)|\bbest price|\blower price|\bprice reduction",
    "payment_terms": r"\bpayment terms?|\badvance payment|\bletter of credit|\bl/c\b|\bt/t\b|\btelegraphic transfer|\bd/p\b|\bd/a\b|\bnet \d+ days|\bcredit (?:terms|period)|\bdeposit\b",
    "delivery_commitment": r"\blead[- ]time|\bdeliver(?:y|ed)? (?:with)?in \d+|\bship(?:ped|ment)? (?:with)?in \d+|\b\d+\s*(?:working )?(?:days|weeks) (?:delivery|lead|dispatch)",
    "exclusivity": r"\bexclusiv|\bsole (?:distributor|agent|agency)|\bterritor(?:y|ial) rights",
    "legal": r"\bcontract\b|\bagreement\b|\bliabilit|\bwarrant(?:y|ies)\b|\bindemn|\bguarantee[sd]?\b|\bterms and conditions\b|\blegally\b|\bpenalt",
    "certification": r"\biso\s?\d{3,5}|\biso[- ]certified|\bce[- ]mark|\bcertif|\bnsf\b|\bwras\b|\bbis\b|\bastm\b|\bdin\s?\d|\ben\s?\d{3,5}|\bkitemark|\bapproved by\b",
    "technical_claim": r"\bpn\s?\d+|\b\d+(?:\.\d+)?\s?(?:bar|psi|°c|deg(?:rees)? c)\b|\bpressure rating|\btemperature rating|\buv[- ]?(?:stabili[sz]ed|resistant)|\blifetime\b|\bleak[- ]?proof|\bzero leak|\bmaintenance[- ]free",
    "purchase_order": r"\bpurchase order|\bp\.o\.(?:\s|$)|\border confirmation|\binvoice\b",
    "capacity_claim": r"\bproduction capacity|\b\d[\d,]*\s*(?:pcs|pieces|units|tons|tonnes|mt)\s*(?:per|/|a)\s*(?:day|month|year|annum)",
}
_COMPILED = {k: re.compile(v, re.I) for k, v in SENSITIVE_PATTERNS.items()}
SENSITIVE_LABELS = {
    "price": "Price mentioned", "quotation": "Quotation / pro-forma", "discount": "Discount", "payment_terms": "Payment terms",
    "delivery_commitment": "Delivery / lead-time commitment", "exclusivity": "Exclusivity / territory", "legal": "Legal / contractual language",
    "certification": "Certification claim", "technical_claim": "Technical claim", "purchase_order": "Purchase order / invoice",
    "capacity_claim": "Production capacity claim", "unsupported_number": "Number not found in approved facts",
    "ai_flagged": "Flagged by the drafting agent",
}


def detect_sensitive(text: str) -> list[str]:
    return [k for k, rx in _COMPILED.items() if rx.search(text or "")]


@dataclass
class Decision:
    blocked: list = field(default_factory=list)
    approval: list = field(default_factory=list)
    sensitive: list = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked)

    @property
    def auto_send(self) -> bool:
        return not self.blocked and not self.approval


def evaluate(email, sys: SystemSettings | None = None) -> Decision:
    """Decide whether `email` (OutboundEmail) may be sent, and whether a human must approve it."""
    from apps.crm.models import Contact
    from apps.mailbox.models import InboundEmail
    from apps.outreach.models import SENSITIVE_TEMPLATE_KINDS, EmailKind, OutboundEmail, SuppressionEntry

    sys = sys or SystemSettings.get_solo()
    d = Decision()
    company, contact = email.company, email.contact
    to = (email.to_email or "").strip().lower()

    # ---- hard blocks -------------------------------------------------------------------
    if not to:
        d.blocked.append("No recipient address.")
    if SuppressionEntry.is_suppressed(to):
        d.blocked.append("Recipient is on the suppression list (unsubscribed, bounced or declined).")
    if company.do_not_contact:
        d.blocked.append("Company is marked do-not-contact.")
    if contact is not None and (contact.do_not_contact or contact.unsubscribed_at):
        d.blocked.append("Contact asked not to be contacted.")
    wrote_to_us = InboundEmail.objects.filter(from_email__iexact=to).exists()
    if email.kind in (EmailKind.INITIAL, EmailKind.FOLLOWUP):
        if contact is None or contact.email.lower() != to:
            d.blocked.append("Outreach must go to a CRM contact.")
        elif contact.email_status not in (Contact.EmailStatus.VERIFIED, Contact.EmailStatus.MANUAL):
            d.blocked.append("Recipient e-mail is not verified.")
    elif email.kind in (EmailKind.CATALOGUE, EmailKind.QUOTATION, EmailKind.MANUAL):
        verified = contact is not None and contact.email.lower() == to and contact.is_emailable
        if not (verified or wrote_to_us):
            d.blocked.append("Recipient is neither a verified contact nor someone who wrote to us.")
    elif email.kind == EmailKind.REPLY and not wrote_to_us and not (contact and contact.is_emailable):
        d.blocked.append("Reply recipient has not written to us and is not verified.")
    if email.created_by_agent and sys.execution_mode == ExecutionMode.RESEARCH_ONLY:
        d.blocked.append("Mode 1 (research only): AI-prepared e-mails cannot be sent.")
    if email.kind == EmailKind.INITIAL:
        since = timezone.now() - timezone.timedelta(days=sys.min_days_between_initial_emails)
        earlier = OutboundEmail.objects.filter(company=company, kind=EmailKind.INITIAL).exclude(pk=email.pk)
        if earlier.filter(status=OutboundEmail.Status.SENT, sent_at__gte=since).exists():
            d.blocked.append(f"An introduction was already sent to this company in the last {sys.min_days_between_initial_emails} days.")
        if earlier.filter(status__in=[OutboundEmail.Status.PENDING_APPROVAL, OutboundEmail.Status.APPROVED,
                                      OutboundEmail.Status.SENDING]).exists():
            d.blocked.append("Another introduction to this company is already waiting to be sent.")
    if email.kind == EmailKind.FOLLOWUP:
        en = email.enrollment
        if en is None or en.status != en.Status.ACTIVE:
            d.blocked.append("Follow-up sequence is no longer active (reply, decline, unsubscribe or manual stop).")
        elif InboundEmail.objects.filter(company=company, received_at__gte=en.started_at).exclude(
                classification__in=["bounce", "auto_reply", "unrelated"]).exists():
            d.blocked.append("The buyer has replied - follow-ups stop.")

    # ---- approval requirements --------------------------------------------------------------
    text = f"{email.subject}\n{email.body_text}"
    d.sensitive = sorted(set(detect_sensitive(text)) | set(email.sensitive_flags or []))
    if d.sensitive:
        d.approval.append("Sensitive topics: " + ", ".join(SENSITIVE_LABELS.get(s, s) for s in d.sensitive))
    if not email.created_by_agent:
        d.approval.append("Written or edited by a team member - send after review.")
    if sys.execution_mode != ExecutionMode.CONTROLLED_AUTOMATION:
        d.approval.append(f"{ExecutionMode(sys.execution_mode).label}: every e-mail needs approval.")
    else:
        if email.kind not in (sys.auto_send_kinds or []):
            d.approval.append(f"'{email.get_kind_display()}' e-mails are not approved for automatic sending.")
        if email.kind in (EmailKind.REPLY, EmailKind.QUOTATION, EmailKind.MANUAL):
            d.approval.append("Replies, quotations and manual e-mails always need a human.")
        if email.template_id and (not email.template.approved_for_auto_send or email.template.kind in SENSITIVE_TEMPLATE_KINDS):
            d.approval.append("Template is not approved for automatic sending.")
        if email.kind in (EmailKind.INITIAL, EmailKind.FOLLOWUP) and not email.template_id:
            d.approval.append("No approved template recorded.")
        if sys.automation_state != AutomationState.RUNNING:
            d.approval.append("Automation is not running.")
    return d


def apply(email, sys: SystemSettings | None = None) -> Decision:
    """Evaluate and set the e-mail's status: BLOCKED, PENDING_APPROVAL or APPROVED (auto)."""
    from apps.outreach.models import OutboundEmail

    d = evaluate(email, sys)
    email.approval_reasons = d.blocked + d.approval
    email.sensitive_flags = d.sensitive
    if d.is_blocked:
        email.status = OutboundEmail.Status.BLOCKED
        email.requires_approval = True
    elif d.approval:
        email.status = OutboundEmail.Status.PENDING_APPROVAL
        email.requires_approval = True
    else:
        email.status = OutboundEmail.Status.APPROVED
        email.requires_approval = False
    email.save(update_fields=["status", "requires_approval", "approval_reasons", "sensitive_flags", "updated_at"])
    return d


def mailbox_sent_counts(account) -> tuple[int, int]:
    from apps.outreach.models import OutboundEmail

    now = timezone.now()
    day_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    qs = OutboundEmail.objects.filter(account=account, status=OutboundEmail.Status.SENT)
    return qs.filter(sent_at__gte=day_start).count(), qs.filter(sent_at__gte=now - timezone.timedelta(hours=1)).count()


def can_dispatch_now(email, account, sys: SystemSettings | None = None, now=None):
    """Operational gate. Returns (ok, reason, retry_at)."""
    from apps.followups.services import next_send_window
    from apps.outreach.models import EmailKind

    sys = sys or SystemSettings.get_solo()
    now = now or timezone.now()
    if sys.automation_state == AutomationState.STOPPED:
        return False, "Automation is stopped.", None
    human_approved = email.approved_by_id is not None
    if not human_approved and sys.automation_state != AutomationState.RUNNING:
        return False, "Automation paused - auto-approved e-mails wait.", None
    daily, hourly = mailbox_sent_counts(account)
    if daily >= (account.daily_limit or sys.daily_send_limit):
        tomorrow = timezone.localtime(now).replace(hour=0, minute=5, second=0, microsecond=0) + timezone.timedelta(days=1)
        return False, "Daily sending limit for this mailbox reached.", tomorrow
    if hourly >= sys.hourly_send_limit:
        return False, "Hourly sending limit reached.", now + timezone.timedelta(minutes=20)
    # Conversations the buyer started (replies, quotations) are not held to the outreach window,
    # but nothing goes out in the middle of the recipient's night.
    if email.kind in (EmailKind.INITIAL, EmailKind.FOLLOWUP, EmailKind.CATALOGUE):
        start, end, weekends = sys.send_window_start_hour, sys.send_window_end_hour, sys.skip_weekends
    else:
        start, end, weekends = 7, 21, False
    nxt = next_send_window(email.company.local_timezone, now, start, end, weekends)
    if nxt > now + timezone.timedelta(seconds=30):
        return False, "Outside the recipient's local business hours.", nxt
    return True, "", None
