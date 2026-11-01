"""CRM service layer (Agent 7). All stage changes, activities, escalations and duplicate checks go through here."""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Q
from django.utils import timezone

from apps.core.audit import audit, notify

from .models import Activity, Company, Contact, Stage, StageHistory, Task, domain_from_url, normalize_name, stage_index

log = logging.getLogger(__name__)

# Free webmail domains must never be used to match a company by e-mail domain.
FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "hotmail.com", "outlook.com", "live.com",
    "msn.com", "aol.com", "icloud.com", "me.com", "mail.ru", "yandex.ru", "yandex.com", "qq.com", "163.com",
    "126.com", "rediffmail.com", "gmx.de", "gmx.net", "web.de", "proton.me", "protonmail.com", "zoho.com",
}


def log_activity(company, kind, subject="", body="", *, contact=None, user=None, agent="", outbound=None,
                 inbound=None, occurred_at=None) -> Activity:
    return Activity.objects.create(
        company=company, contact=contact, kind=kind, subject=subject[:300], body=body,
        user=user if getattr(user, "is_authenticated", False) else None, agent=agent,
        outbound_email=outbound, inbound_email=inbound, occurred_at=occurred_at or timezone.now(),
    )


def set_stage(company: Company, new_stage: str, *, user=None, agent: str = "", reason: str = "") -> bool:
    """Set any stage (human action). Records history and an activity."""
    if company.stage == new_stage:
        return False
    old = company.stage
    company.stage = new_stage
    company.save(update_fields=["stage", "updated_at"])
    StageHistory.objects.create(
        company=company, from_stage=old, to_stage=new_stage,
        changed_by=user if getattr(user, "is_authenticated", False) else None, agent=agent, reason=reason[:300],
    )
    log_activity(company, Activity.Kind.STAGE, f"{Stage(old).label} → {Stage(new_stage).label}", reason,
                 user=user, agent=agent)
    audit("crm.stage", company, user=user, agent=agent, summary=f"{company}: {old} → {new_stage}", reason=reason)
    if new_stage == Stage.LOST:
        from apps.followups.services import stop_company_enrollments

        stop_company_enrollments(company, "declined", note=reason)
    return True


def advance_stage(company: Company, new_stage: str, *, agent: str = "", user=None, reason: str = "") -> bool:
    """Automatic transitions only ever move a deal forward (never backwards, never out of 'lost')."""
    if company.stage == Stage.LOST and new_stage != Stage.LOST:
        if new_stage in (Stage.REPLIED, Stage.INTERESTED, Stage.REQUIREMENT):
            # A buyer who said no earlier and now writes back with interest re-opens the deal.
            return set_stage(company, new_stage, user=user, agent=agent, reason=reason or "Buyer re-engaged")
        return False
    if new_stage == Stage.LOST or stage_index(new_stage) > stage_index(company.stage):
        return set_stage(company, new_stage, user=user, agent=agent, reason=reason)
    return False


def find_duplicate(name: str, website: str = "", country_iso: str = "", emails=()) -> Company | None:
    """Duplicate detection: same domain, same normalised name in the same country, or same contact e-mail."""
    domain = domain_from_url(website)
    if domain:
        match = Company.objects.filter(domain=domain).first()
        if match:
            return match
    norm = normalize_name(name)
    if norm:
        qs = Company.objects.filter(normalized_name=norm)
        if country_iso:
            qs = qs.filter(Q(country_id=country_iso.upper()) | Q(country__isnull=True))
        match = qs.first()
        if match:
            return match
    clean = [e.strip().lower() for e in emails if e and "@" in e]
    if clean:
        contact = Contact.objects.filter(email__in=clean).select_related("company").first()
        if contact:
            return contact.company
        for e in clean:
            dom = e.split("@")[1]
            if dom not in FREEMAIL_DOMAINS:
                match = Company.objects.filter(domain=dom).first()
                if match:
                    return match
    return None


def match_sender(email: str):
    """Return (company, contact) for an e-mail address, if known."""
    email = (email or "").strip().lower()
    if not email:
        return None, None
    contact = Contact.objects.filter(email__iexact=email).select_related("company").first()
    if contact:
        return contact.company, contact
    domain = email.split("@")[-1]
    if domain and domain not in FREEMAIL_DOMAINS:
        company = Company.objects.filter(domain=domain).first()
        if company:
            return company, None
    return None, None


def create_task(title, *, company=None, details="", kind=Task.Kind.OTHER, priority=Task.Priority.NORMAL,
                assigned_to=None, due_date=None, agent="", user=None, link="") -> Task:
    return Task.objects.create(
        company=company, title=title[:255], details=details, kind=kind, priority=priority,
        assigned_to=assigned_to or (company.owner if company else None),
        due_date=due_date or timezone.localdate(), created_by_agent=agent,
        created_by=user if getattr(user, "is_authenticated", False) else None, link=link,
    )


def escalate(company: Company, title: str, details: str = "", *, link: str = "", urgent: bool = False, agent: str = "") -> Task:
    """Hand an important opportunity to the human sales team: task + notification + optional e-mail."""
    from apps.core.models import SystemSettings

    priority = Task.Priority.URGENT if urgent else Task.Priority.HIGH
    existing = Task.objects.filter(company=company, kind=Task.Kind.ESCALATION, done=False, title=title[:255]).first()
    if existing:
        return existing
    task = create_task(title, company=company, details=details, kind=Task.Kind.ESCALATION, priority=priority,
                       agent=agent, link=link or company.get_absolute_url())
    notify(title, body=details[:1000], url=link or company.get_absolute_url(), level="danger" if urgent else "warning",
           user=company.owner)
    recipients = list(SystemSettings.get_solo().escalation_email_list)
    if company.owner and company.owner.email:
        recipients.append(company.owner.email)
    if recipients:
        try:
            send_mail(
                f"[Flotec Sales AI] {title}",
                f"{details}\n\nOpen: {settings.SITE_URL}{link or company.get_absolute_url()}",
                settings.DEFAULT_FROM_EMAIL, sorted(set(recipients)), fail_silently=False,
            )
        except Exception:  # noqa: BLE001 - notification e-mail must never break processing
            log.exception("escalation e-mail failed")
    return task


def touch_contacted(company: Company, when=None):
    company.last_contacted_at = when or timezone.now()
    company.save(update_fields=["last_contacted_at", "updated_at"])


def touch_replied(company: Company, when=None):
    company.last_reply_at = when or timezone.now()
    company.save(update_fields=["last_reply_at", "updated_at"])
