from apps.agents.registry import job_handler

from .processing import process_due


@job_handler("followups.process_due")
def followups_due(job):
    return process_due(job=job)
