"""Follow-up agent (Agent 5): enrolment, stop rules, due processing and local-time scheduling."""
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from .models import Enrollment, FollowUpSequence

log = logging.getLogger(__name__)


def cancel_open_followups(enrollment) -> int:
    """Cancel follow-up drafts that were prepared but not yet sent."""
    from apps.outreach.models import OutboundEmail

    return OutboundEmail.objects.filter(
        enrollment=enrollment,
        status__in=[OutboundEmail.Status.DRAFT, OutboundEmail.Status.PENDING_APPROVAL, OutboundEmail.Status.APPROVED],
    ).update(status=OutboundEmail.Status.CANCELLED, rejected_reason="Follow-up sequence stopped")


def _stop(qs, reason, note):
    n = 0
    for en in qs:
        if en.stop(reason, note):
            cancel_open_followups(en)
            n += 1
    return n


def stop_company_enrollments(company, reason: str, note: str = "") -> int:
    return _stop(Enrollment.objects.filter(company=company, status=Enrollment.Status.ACTIVE), reason, note)


def stop_contact_enrollments(contact, reason: str, note: str = "") -> int:
    return _stop(Enrollment.objects.filter(contact=contact, status=Enrollment.Status.ACTIVE), reason, note)


def schedule_next(enrollment, *, sent_at=None):
    """After a step was sent (or skipped), schedule the next step relative to Day 0."""
    nxt = enrollment.next_step()
    now = timezone.now()
    if nxt is None:
        enrollment.status = Enrollment.Status.COMPLETED
        enrollment.next_due_at = None
        enrollment.log("completed")
    else:
        due = enrollment.started_at + timedelta(days=nxt.day_offset)
        # never two e-mails within 2 days of each other, even if approvals were slow
        enrollment.next_due_at = max(due, (sent_at or now) + timedelta(days=2))
    if sent_at:
        enrollment.last_sent_at = sent_at
    enrollment.save(update_fields=["status", "next_due_at", "last_sent_at", "history"])


def on_followup_sent(outbound):
    en = outbound.enrollment
    en.log("sent", step=outbound.followup_step, email=outbound.pk)
    if en.status == Enrollment.Status.ACTIVE:
        schedule_next(en, sent_at=outbound.sent_at)
    else:
        en.save(update_fields=["history"])


def on_followup_discarded(outbound):
    """A follow-up draft was rejected by a human: move on to the next step."""
    en = outbound.enrollment
    if en and en.status == Enrollment.Status.ACTIVE:
        en.log("skipped", step=outbound.followup_step, email=outbound.pk)
        schedule_next(en)


def next_send_window(tz_name: str, earliest: datetime, start_hour: int, end_hour: int, skip_weekends: bool) -> datetime:
    """First moment >= `earliest` inside the recipient's local business window."""
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:  # noqa: BLE001 - unknown zone names fall back to UTC
        tz = ZoneInfo("UTC")
    local = earliest.astimezone(tz)
    for _ in range(14):
        if skip_weekends and local.weekday() >= 5:
            local = datetime.combine(local.date() + timedelta(days=1), time(start_hour), tz)
            continue
        start = datetime.combine(local.date(), time(start_hour), tz)
        end = datetime.combine(local.date(), time(0), tz) + timedelta(hours=end_hour)
        if local < start:
            return start
        if local < end:
            return local
        local = datetime.combine(local.date() + timedelta(days=1), time(start_hour), tz)
    return local


def in_send_window(tz_name: str, when: datetime, start_hour: int, end_hour: int, skip_weekends: bool) -> bool:
    return next_send_window(tz_name, when, start_hour, end_hour, skip_weekends) <= when + timedelta(seconds=1)


def enroll_after_initial(outbound) -> Enrollment | None:
    """Start the default sequence when an initial introduction e-mail has been sent."""
    if not outbound.contact_id:
        return None
    if Enrollment.objects.filter(contact=outbound.contact, status=Enrollment.Status.ACTIVE).exists():
        return None
    sequence = FollowUpSequence.default()
    if sequence is None:
        return None
    started = outbound.sent_at or timezone.now()
    en = Enrollment(company=outbound.company, contact=outbound.contact, sequence=sequence, initial_email=outbound,
                    started_at=started)
    first = sequence.steps.order_by("step_number").first()
    en.next_due_at = started + timedelta(days=first.day_offset) if first else None
    if first is None:
        en.status = Enrollment.Status.COMPLETED
    en.log("enrolled", initial_email=outbound.pk)
    en.save()
    return en
