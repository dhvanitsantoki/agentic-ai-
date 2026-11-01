from apps.agents.registry import job_handler

from .services import expire_quotations


@job_handler("quotations.expire")
def quotations_expire(job):
    return {"expired": expire_quotations()}
