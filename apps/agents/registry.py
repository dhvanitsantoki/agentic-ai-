"""Job handler registry. Each app registers handlers in its `jobs.py` with @job_handler."""
from django.utils import timezone

HANDLERS: dict = {}


class AutomationHalted(Exception):
    """Raised inside a handler when automation was stopped; the job is cancelled."""


def job_handler(kind: str):
    def decorator(fn):
        HANDLERS[kind] = fn
        return fn

    return decorator


def enqueue(kind: str, payload: dict | None = None, *, user=None, run_after=None, priority: int = 5,
            max_attempts: int = 3, scheduled_task=None):
    from .models import Job

    if kind not in HANDLERS:
        raise ValueError(f"Unknown job kind: {kind}")
    return Job.objects.create(
        kind=kind, payload=payload or {}, created_by=user if getattr(user, "is_authenticated", False) else None,
        run_after=run_after or timezone.now(), priority=priority, max_attempts=max_attempts,
        scheduled_task=scheduled_task,
    )


def ensure_not_stopped():
    """Call between steps of long jobs: aborts work immediately after an emergency stop."""
    from apps.core.models import AutomationState, SystemSettings

    if SystemSettings.objects.filter(pk=1, automation_state=AutomationState.STOPPED).exists():
        raise AutomationHalted("Automation was stopped by an administrator.")
