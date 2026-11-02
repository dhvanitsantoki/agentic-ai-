"""Database-backed job worker.

Automation states:
* RUNNING – scheduler enqueues jobs; all jobs run.
* PAUSED  – scheduler enqueues nothing; only jobs a user triggered by hand run.
* STOPPED – emergency stop: queued jobs were cancelled; the worker runs nothing.
"""
import logging
import os
import socket
import time
import traceback

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from apps.core.models import AutomationState, SystemSettings

from . import scheduler
from .models import Job
from .registry import HANDLERS, AutomationHalted

log = logging.getLogger(__name__)


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _claimable(state: str):
    qs = Job.objects.filter(status=Job.Status.QUEUED, run_after__lte=timezone.now())
    if state == AutomationState.PAUSED:
        qs = qs.filter(created_by__isnull=False)
    return qs.order_by("priority", "run_after", "id")


def claim_next_job(wid: str, state: str):
    with transaction.atomic():
        qs = _claimable(state)
        if connection.features.has_select_for_update_skip_locked:
            qs = qs.select_for_update(skip_locked=True)
        job = qs.first()
        if job is None:
            return None
        job.status = Job.Status.RUNNING
        job.locked_by = wid
        job.locked_at = job.started_at = timezone.now()
        job.attempts += 1
        job.save(update_fields=["status", "locked_by", "locked_at", "started_at", "attempts"])
        return job


def run_job(job: Job) -> Job:
    handler = HANDLERS.get(job.kind)
    try:
        if handler is None:
            raise RuntimeError(f"No handler registered for job kind '{job.kind}'")
        result = handler(job)
        job.status = Job.Status.DONE
        job.result = result or {}
        job.last_error = ""
    except AutomationHalted as exc:
        job.status = Job.Status.CANCELLED
        job.last_error = str(exc)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job
        job.last_error = f"{exc.__class__.__name__}: {exc}\n\n{traceback.format_exc()[-4000:]}"
        retry_at = getattr(exc, "retry_at", None)  # provider quota used up: wait, without using an attempt
        if retry_at is not None:
            job.status = Job.Status.QUEUED
            job.run_after = retry_at
            job.attempts = max(0, job.attempts - 1)
        elif job.attempts < job.max_attempts and not getattr(exc, "permanent", False):
            job.status = Job.Status.QUEUED
            job.run_after = timezone.now() + timezone.timedelta(minutes=2 ** job.attempts)
        else:
            job.status = Job.Status.FAILED
            _notify_failure(job, exc)
        if retry_at is not None:
            log.warning("job %s (%s) waits until %s: %s", job.pk, job.kind, retry_at, exc)
        else:
            log.exception("job %s (%s) failed", job.pk, job.kind)
    job.finished_at = timezone.now()
    job.locked_by = ""
    job.save(update_fields=["status", "result", "last_error", "run_after", "finished_at", "locked_by", "attempts"])
    return job


def _notify_failure(job, exc):
    from apps.core.audit import notify

    notify(f"Agent job failed: {job.kind}", body=str(exc)[:500], url=f"/agents/jobs/{job.pk}/", level="danger")


def recover_stale_jobs():
    cutoff = timezone.now() - timezone.timedelta(minutes=settings.JOB_STALE_MINUTES)
    return Job.objects.filter(status=Job.Status.RUNNING, locked_at__lt=cutoff).update(
        status=Job.Status.QUEUED, locked_by="", last_error="Recovered after worker interruption."
    )


def work_once(wid: str | None = None, max_jobs: int = 20) -> int:
    wid = wid or worker_id()
    state = SystemSettings.get_solo().automation_state
    if state == AutomationState.STOPPED:
        return 0
    recover_stale_jobs()
    if state == AutomationState.RUNNING:
        scheduler.tick()
    done = 0
    while done < max_jobs:
        state = SystemSettings.get_solo().automation_state
        if state == AutomationState.STOPPED:
            break
        job = claim_next_job(wid, state)
        if job is None:
            break
        run_job(job)
        done += 1
    return done


def process_tick(budget_seconds: int, max_running: int, wid: str | None = None) -> dict:
    """One cron invocation on serverless hosting: schedule due tasks, then run jobs until the time
    budget is used, never more than `max_running` jobs at once across invocations.

    A job that starts before the budget runs out may run longer, up to the platform's function limit.
    """
    wid = wid or worker_id()
    deadline = time.monotonic() + budget_seconds
    state = SystemSettings.get_solo().automation_state
    result = {"state": state, "recovered": 0, "scheduled": 0, "processed": 0, "busy": False}
    if state == AutomationState.STOPPED:
        return result
    result["recovered"] = recover_stale_jobs()
    if state == AutomationState.RUNNING:
        result["scheduled"] = len(scheduler.tick())
    while time.monotonic() < deadline:
        if Job.objects.filter(status=Job.Status.RUNNING).count() >= max_running:
            result["busy"] = True
            break
        state = SystemSettings.get_solo().automation_state
        if state == AutomationState.STOPPED:
            break
        job = claim_next_job(wid, state)
        if job is None:
            break
        run_job(job)
        result["processed"] += 1
    return result


def run_forever(sleep_seconds: int = 15):
    wid = worker_id()
    log.info("worker %s started", wid)
    while True:
        close_old_connections()
        try:
            n = work_once(wid)
        except Exception:  # noqa: BLE001 - keep the worker alive
            log.exception("worker loop error")
            n = 0
        if n == 0:
            time.sleep(sleep_seconds)


def cancel_queued_jobs(reason: str = "Automation stopped") -> int:
    return Job.objects.filter(status=Job.Status.QUEUED).update(
        status=Job.Status.CANCELLED, last_error=reason, finished_at=timezone.now()
    )
