from apps.agents.registry import job_handler

from .services import generate_report


@job_handler("reports.generate")
def reports_generate(job):
    snap = generate_report(job.payload.get("period", "daily"), job=job)
    return {"report": snap.pk, "period": snap.period, "start": snap.start.isoformat()}
