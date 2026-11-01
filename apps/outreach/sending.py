"""Sending service: policy check -> mailbox selection -> MIME -> provider -> CRM updates."""
import logging
import os
from email.utils import make_msgid

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.core import policy
from apps.core.audit import audit
from apps.core.models import SystemSettings
from apps.crm import services as crm
from apps.crm.models import Activity, Stage
from apps.mailbox.models import MailAccount
from apps.mailbox.providers.base import Attachment, AuthError, OutgoingMessage, ProviderError, get_provider

from .models import EmailKind, OutboundEmail

log = logging.getLogger(__name__)
UNSUB_SALT = "flotec.unsubscribe"
OUTREACH_KINDS = (EmailKind.INITIAL, EmailKind.FOLLOWUP)


def unsubscribe_token(email_address: str, company_id: int) -> str:
    return signing.dumps({"e": email_address.lower(), "c": company_id}, salt=UNSUB_SALT)


def read_unsubscribe_token(token: str) -> dict:
    return signing.loads(token, salt=UNSUB_SALT, max_age=60 * 60 * 24 * 365 * 3)


def unsubscribe_url(email_address: str, company_id: int) -> str:
    return f"{settings.SITE_URL}/u/{unsubscribe_token(email_address, company_id)}/"


def choose_account(email: OutboundEmail) -> MailAccount | None:
    qs = MailAccount.objects.filter(is_active=True, use_for_sending=True, status=MailAccount.Status.CONNECTED)
    if email.account_id and qs.filter(pk=email.account_id).exists():
        return email.account
    # keep a conversation on the mailbox it started from
    previous = OutboundEmail.objects.filter(company=email.company, status=OutboundEmail.Status.SENT, account__in=qs).order_by("-sent_at").first()
    if previous:
        return previous.account
    if email.company.owner_id:
        owned = qs.filter(owner=email.company.owner).first()
        if owned:
            return owned
    return qs.filter(is_default_sender=True).first() or qs.first()


def compose(email: OutboundEmail, account: MailAccount) -> OutgoingMessage:
    body = email.body_text
    headers = {}
    if email.kind in OUTREACH_KINDS:
        url = unsubscribe_url(email.to_email, email.company_id)
        footer = SystemSettings.get_solo().unsubscribe_footer.format(unsubscribe_url=url)
        body = f"{body.rstrip()}\n\n--\n{footer}"
        headers["List-Unsubscribe"] = f"<{url}>"
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    attachments = []
    for d in email.attachments.filter(approved_for_sending=True):
        if not d.file:
            continue
        ext = os.path.splitext(d.file.name)[1].lower() or ".pdf"
        d.file.open("rb")
        attachments.append(Attachment(f"{slugify(d.title) or 'document'}{ext}", d.file.read()))
        d.file.close()
    if email.quotation_id and email.quotation.pdf:
        q = email.quotation
        q.pdf.open("rb")
        attachments.append(Attachment(f"{q.number.replace('/', '-')}.pdf", q.pdf.read(), "application/pdf"))
        q.pdf.close()
    msg = OutgoingMessage(
        from_email=account.email_address, from_name=account.display_name, to=[email.to_email],
        cc=[c.strip() for c in email.cc_emails.split(",") if c.strip()], subject=email.subject, body_text=body,
        attachments=attachments, headers=headers,
        message_id=email.rfc_message_id or make_msgid(domain=account.email_address.split("@")[-1]),
    )
    # thread replies / follow-ups with the conversation they belong to
    if email.in_reply_to_id:
        src = email.in_reply_to
        msg.in_reply_to, msg.references = src.rfc_message_id, src.references
        msg.reply_to_provider_id = src.provider_message_id if src.account_id == account.pk else ""
        msg.thread_id = src.provider_thread_id if src.account_id == account.pk else ""
    elif email.kind == EmailKind.FOLLOWUP and email.enrollment_id and email.enrollment.initial_email_id:
        first = email.enrollment.initial_email
        if first.account_id == account.pk:
            msg.in_reply_to = first.rfc_message_id
            msg.thread_id = first.provider_thread_id
    return msg


def send_email(email: OutboundEmail, *, now=None) -> str:
    """Try to send one e-mail. Returns 'sent', 'deferred', 'pending_approval', 'blocked' or 'failed'."""
    now = now or timezone.now()
    with transaction.atomic():
        email = OutboundEmail.objects.select_for_update().get(pk=email.pk)
        if email.status not in (OutboundEmail.Status.APPROVED,):
            return email.status
        decision = policy.evaluate(email)
        if decision.is_blocked:
            email.status = OutboundEmail.Status.BLOCKED
            email.approval_reasons = decision.blocked
            email.save(update_fields=["status", "approval_reasons", "updated_at"])
            audit("email.blocked", email, agent="outreach", summary="; ".join(decision.blocked)[:400])
            return "blocked"
        if decision.approval and email.approved_by_id is None:
            email.status = OutboundEmail.Status.PENDING_APPROVAL
            email.approval_reasons = decision.approval
            email.save(update_fields=["status", "approval_reasons", "updated_at"])
            return "pending_approval"
        account = choose_account(email)
        if account is None:
            email.error = "No connected mailbox available for sending."
            email.save(update_fields=["error", "updated_at"])
            return "deferred"
        ok, reason, retry_at = policy.can_dispatch_now(email, account, now=now)
        if not ok:
            email.scheduled_for = retry_at
            email.error = reason
            email.save(update_fields=["scheduled_for", "error", "updated_at"])
            return "deferred"
        email.status = OutboundEmail.Status.SENDING
        email.account = account
        email.send_attempts += 1
        email.save(update_fields=["status", "account", "send_attempts", "updated_at"])
    msg = compose(email, account)
    try:
        result = get_provider(account.provider).send(account, msg)
    except (ProviderError, OSError) as exc:
        email.error = str(exc)[:2000]
        if isinstance(exc, AuthError):
            account.status = MailAccount.Status.ERROR
            account.last_error = str(exc)[:1000]
            account.save(update_fields=["status", "last_error"])
        if email.send_attempts >= 3:
            email.status = OutboundEmail.Status.FAILED
            from apps.core.audit import notify

            notify(f"E-mail to {email.to_email} failed", body=email.error[:500], url=f"/outreach/emails/{email.pk}/", level="danger")
        else:
            email.status = OutboundEmail.Status.APPROVED
            email.scheduled_for = timezone.now() + timezone.timedelta(minutes=15 * email.send_attempts)
        email.save(update_fields=["status", "error", "scheduled_for", "updated_at"])
        log.warning("send failed for email %s: %s", email.pk, exc)
        return "failed"
    email.status = OutboundEmail.Status.SENT
    email.sent_at = timezone.now()
    email.provider_message_id = result.provider_message_id
    email.provider_thread_id = result.thread_id
    email.rfc_message_id = result.rfc_message_id or msg.message_id
    email.error = ""
    email.save(update_fields=["status", "sent_at", "provider_message_id", "provider_thread_id", "rfc_message_id", "error", "updated_at"])
    after_sent(email)
    return "sent"


def after_sent(email: OutboundEmail):
    """CRM and follow-up bookkeeping after a successful send."""
    from apps.followups.services import enroll_after_initial, on_followup_sent

    company = email.company
    crm.log_activity(company, Activity.Kind.EMAIL_OUT, email.subject, email.body_text[:2000], contact=email.contact,
                     user=email.approved_by, agent=email.created_by_agent, outbound=email)
    crm.touch_contacted(company, email.sent_at)
    if email.kind == EmailKind.INITIAL:
        crm.advance_stage(company, Stage.CONTACTED, agent="outreach", reason="Introduction e-mail sent")
        enroll_after_initial(email)
    elif email.kind == EmailKind.FOLLOWUP and email.enrollment_id:
        on_followup_sent(email)
    elif email.kind == EmailKind.QUOTATION and email.quotation_id:
        q = email.quotation
        q.status = q.Status.SENT
        q.sent_at = email.sent_at
        q.save(update_fields=["status", "sent_at", "updated_at"])
        crm.advance_stage(company, Stage.QUOTED, agent="quotation", reason=f"Quotation {q.number} sent")
    audit("email.sent", email, user=email.approved_by, agent=email.created_by_agent,
          summary=f"{email.get_kind_display()} to {email.to_email}: {email.subject}"[:400])


def send_queue(limit: int = 50) -> dict:
    now = timezone.now()
    from django.db.models import Q

    due = OutboundEmail.objects.filter(status=OutboundEmail.Status.APPROVED).filter(
        Q(scheduled_for__isnull=True) | Q(scheduled_for__lte=now)).order_by("approved_at", "created_at")[:limit]
    counts = {}
    for e in due:
        outcome = send_email(e, now=now)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts
