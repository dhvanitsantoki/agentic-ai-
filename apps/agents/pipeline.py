"""Nightly automation pipeline (default 02:00).

1. Discover companies in priority countries (rotation, oldest-researched first)
2-7. Research websites, find contacts, verify e-mails, remove duplicates, score, add to CRM (research pipeline)
8. Prepare personalised drafts for verified, uncontacted companies (Mode 2/3)
9. Mark drafts that the policy allows as auto-approved (Mode 3 only); everything else waits for approval
10. Schedule / prepare due follow-ups
11. Generate the daily report
Each step is isolated: a failure is logged and the next step still runs.
"""
import logging
import math

from django.utils import timezone

from apps.core.models import ExecutionMode, SystemSettings

from .llm import agent_run, ai_configured
from .models import AgentName
from .registry import AutomationHalted, enqueue, ensure_not_stopped

log = logging.getLogger(__name__)


def _step(results: dict, name: str, fn):
    try:
        ensure_not_stopped()
        results[name] = fn()
    except AutomationHalted:
        raise
    except Exception as exc:  # noqa: BLE001 - one failed step must not abort the pipeline
        log.exception("nightly step %s failed", name)
        results[name] = {"error": f"{exc.__class__.__name__}: {exc}"}


def discover(sys: SystemSettings, queue: bool = False) -> dict:
    """Research priority countries. queue=True (serverless): one background job per run instead of inline."""
    from apps.geo.models import Country
    from apps.research.models import ResearchRun
    from apps.research.pipeline import companies_added_today, execute_run

    if not ai_configured("research"):
        return {"skipped": "AI not configured for research (needs ANTHROPIC_API_KEY)"}
    remaining = sys.research_daily_company_limit - companies_added_today()
    if remaining <= 0:
        return {"skipped": "daily research limit reached"}
    countries = list(Country.objects.filter(is_priority=True).order_by("last_researched_at", "name"))
    if not countries:
        return {"skipped": "no priority countries selected (mark countries as priority in Country markets)"}
    per_run = max(1, sys.research_companies_per_run)
    n_runs = min(len(countries), math.ceil(remaining / per_run), 6)
    runs = []
    for i, country in enumerate(countries[:n_runs]):
        ensure_not_stopped()
        kind = ResearchRun.Kind.OEM if i % 2 else ResearchRun.Kind.BUYER
        run = ResearchRun.objects.create(kind=kind, target_count=per_run, triggered_by_schedule=True)
        run.countries.set([country])
        if queue:
            job = enqueue("research.run", {"run_id": run.pk}, max_attempts=1, priority=4)
            run.job = job
            run.save(update_fields=["job"])
            runs.append({"run": run.pk, "country": country.iso2, "kind": kind, "status": "queued", "job": job.pk})
            continue
        try:
            execute_run(run)
        except AutomationHalted:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("nightly research run %s failed: %s", run.pk, exc)
        run.refresh_from_db()
        runs.append({"run": run.pk, "country": country.iso2, "kind": kind, "status": run.status, "added": run.added_count})
    return {"runs": runs}


def prepare_drafts(sys: SystemSettings) -> dict:
    from apps.outreach.drafting import prepare_initial_drafts

    if sys.execution_mode == ExecutionMode.RESEARCH_ONLY:
        return {"skipped": "Mode 1 (research only)"}
    if not ai_configured("drafting"):
        return {"skipped": "AI not configured for drafting"}
    return prepare_initial_drafts(limit=sys.daily_send_limit)


def followups() -> dict:
    from apps.followups.processing import process_due

    return process_due()


def daily_report() -> dict:
    from apps.reports.services import generate_report

    snap = generate_report("daily")
    return {"report": snap.pk}


# Serverless: steps 8-11 run as a separate job after the queued research runs had time to finish.
FINISH_DELAY = timezone.timedelta(minutes=60)


def run_nightly(job=None) -> dict:
    from django.conf import settings

    sys = SystemSettings.get_solo()
    results: dict = {"started": timezone.now().isoformat(), "mode": sys.execution_mode}
    with agent_run(AgentName.SYSTEM, "pipeline.nightly", job=job) as tracker:
        if settings.SERVERLESS:
            _step(results, "1-7 discovery & verification (queued)", lambda: discover(sys, queue=True))
            finish = enqueue("pipeline.nightly_finish", {}, run_after=timezone.now() + FINISH_DELAY, priority=6)
            results["8-11"] = f"queued as job {finish.pk}"
        else:
            _step(results, "1-7 discovery & verification", lambda: discover(sys))
            _finish_steps(sys, results)
        tracker.run.summary = "\n".join(f"{k}: {v}" for k, v in results.items())[:4000]
    return results


def _finish_steps(sys, results: dict):
    _step(results, "8-9 drafts & policy", lambda: prepare_drafts(sys))
    _step(results, "10 follow-ups", followups)
    _step(results, "11 daily report", daily_report)


def run_nightly_finish(job=None) -> dict:
    sys = SystemSettings.get_solo()
    results: dict = {"started": timezone.now().isoformat(), "mode": sys.execution_mode}
    with agent_run(AgentName.SYSTEM, "pipeline.nightly_finish", job=job) as tracker:
        _finish_steps(sys, results)
        tracker.run.summary = "\n".join(f"{k}: {v}" for k, v in results.items())[:4000]
    return results
