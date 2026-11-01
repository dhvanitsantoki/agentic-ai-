"""Agent 7: daily CRM hygiene."""
from django.utils import timezone

from apps.agents.registry import job_handler

from .models import Company, Stage, Task
from .services import create_task

STALE_DAYS = 21
ACTIVE = [Stage.REPLIED, Stage.INTERESTED, Stage.REQUIREMENT, Stage.QUOTED, Stage.NEGOTIATION, Stage.SAMPLE]


@job_handler("crm.hygiene")
def crm_hygiene(job):
    today = timezone.localdate()
    created = 0
    for c in Company.objects.filter(next_action_date__lt=today, do_not_contact=False).exclude(stage=Stage.LOST):
        title = f"Overdue next action: {c.name}"
        if not Task.objects.filter(company=c, title=title, done=False).exists():
            create_task(title, company=c, details=c.next_action, kind=Task.Kind.FOLLOWUP, agent="crm")
            created += 1
    cutoff = timezone.now() - timezone.timedelta(days=STALE_DAYS)
    for c in Company.objects.filter(stage__in=ACTIVE, updated_at__lt=cutoff):
        title = f"Stale opportunity ({STALE_DAYS}+ days no activity): {c.name}"
        if not Task.objects.filter(company=c, title=title, done=False).exists():
            create_task(title, company=c, details=f"Stage: {c.get_stage_display()}. Decide the next step or close.",
                        kind=Task.Kind.REVIEW, priority=Task.Priority.HIGH, agent="crm")
            created += 1
    return {"tasks_created": created}
