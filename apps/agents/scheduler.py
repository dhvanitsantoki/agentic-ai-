"""Cron-style scheduler backed by ScheduledTask rows. Enqueues jobs; the worker runs them."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter
from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)


def compute_next_run(cron: str, base: datetime | None = None) -> datetime:
    tz = ZoneInfo(settings.TIME_ZONE)
    base = (base or timezone.now()).astimezone(tz)
    nxt = croniter(cron, base).get_next(datetime)
    if timezone.is_naive(nxt):
        nxt = nxt.replace(tzinfo=tz)
    return nxt


def is_valid_cron(cron: str) -> bool:
    return croniter.is_valid(cron)


def tick(now: datetime | None = None) -> list:
    """Enqueue every enabled task that is due. Never enqueues a task twice while one is pending."""
    from .models import Job, ScheduledTask
    from .registry import enqueue

    now = now or timezone.now()
    created = []
    for task in ScheduledTask.objects.filter(enabled=True):
        if task.next_run_at is None:
            task.next_run_at = compute_next_run(task.cron, now)
            task.save(update_fields=["next_run_at"])
            continue
        if task.next_run_at > now:
            continue
        pending = Job.objects.filter(scheduled_task=task, status__in=[Job.Status.QUEUED, Job.Status.RUNNING]).exists()
        if not pending:
            created.append(enqueue(task.job_kind, task.payload, scheduled_task=task))
        task.last_run_at = now
        task.next_run_at = compute_next_run(task.cron, now)
        task.save(update_fields=["last_run_at", "next_run_at"])
    if created:
        log.info("scheduler enqueued %s job(s)", len(created))
    return created
