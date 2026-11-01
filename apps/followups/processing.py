"""Agent 5: prepare due follow-ups, applying stop rules first."""
import logging

from django.utils import timezone

from apps.agents.registry import AutomationHalted, ensure_not_stopped
from apps.core.models import ExecutionMode, SystemSettings
from apps.crm.models import Stage, stage_index
from apps.mailbox.models import InboundEmail
from apps.outreach.models import OutboundEmail, SuppressionEntry

from .models import Enrollment

log = logging.getLogger(__name__)
IGNORED_INBOUND = ["bounce", "auto_reply", "unrelated"]


def stop_reason_for(en: Enrollment) -> str | None:
    company, contact = en.company, en.contact
    if company.do_not_contact or contact.do_not_contact:
        return Enrollment.StopReason.MANUAL
    if contact.unsubscribed_at or SuppressionEntry.is_suppressed(contact.email):
        return Enrollment.StopReason.UNSUBSCRIBED
    if contact.email_status == contact.EmailStatus.BOUNCED:
        return Enrollment.StopReason.BOUNCED
    if not contact.is_emailable:
        return Enrollment.StopReason.INVALID
    if InboundEmail.objects.filter(company=company, received_at__gte=en.started_at).exclude(classification__in=IGNORED_INBOUND).exists():
        return Enrollment.StopReason.REPLIED
    if company.stage == Stage.LOST:
        return Enrollment.StopReason.DECLINED
    if stage_index(company.stage) > stage_index(Stage.CONTACTED):
        return Enrollment.StopReason.STAGE
    return None


def process_due(now=None, job=None, limit: int = 50) -> dict:
    from apps.agents.llm import ai_configured
    from apps.outreach.drafting import draft_followup

    from .services import cancel_open_followups

    now = now or timezone.now()
    sys = SystemSettings.get_solo()
    stats = {"stopped": 0, "drafted": 0, "completed": 0, "skipped": 0, "errors": []}
    for en in Enrollment.objects.filter(status=Enrollment.Status.ACTIVE, next_due_at__lte=now).select_related(
            "company", "contact", "sequence")[:limit]:
        ensure_not_stopped()
        reason = stop_reason_for(en)
        if reason:
            if en.stop(reason, "Stop rule at follow-up time"):
                cancel_open_followups(en)
            stats["stopped"] += 1
            continue
        step = en.next_step()
        if step is None:
            en.status = Enrollment.Status.COMPLETED
            en.next_due_at = None
            en.log("completed")
            en.save(update_fields=["status", "next_due_at", "history"])
            stats["completed"] += 1
            continue
        if OutboundEmail.objects.filter(enrollment=en, followup_step=step.step_number).exclude(
                status__in=[OutboundEmail.Status.REJECTED, OutboundEmail.Status.CANCELLED, OutboundEmail.Status.FAILED]).exists():
            stats["skipped"] += 1  # never two e-mails for the same step
            continue
        if sys.execution_mode == ExecutionMode.RESEARCH_ONLY or not ai_configured():
            stats["skipped"] += 1
            continue
        try:
            email = draft_followup(en, step, job=job)
        except AutomationHalted:
            raise
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"enrollment {en.pk}: {exc}")
            log.warning("follow-up draft failed for enrollment %s: %s", en.pk, exc)
            if exc.__class__.__name__ in ("BudgetExceeded", "AIUnavailable"):
                break
            continue
        en.last_step_done = step.step_number
        en.next_due_at = None  # the next step is scheduled when this one is sent or skipped
        en.log("drafted", step=step.step_number, email=email.pk, status=email.status)
        en.save(update_fields=["last_step_done", "next_due_at", "history"])
        stats["drafted"] += 1
    return stats
