from django.core.management import call_command

from .pipeline import run_nightly, run_nightly_finish
from .registry import job_handler


@job_handler("pipeline.nightly")
def nightly(job):
    return run_nightly(job)


@job_handler("pipeline.nightly_finish")
def nightly_finish(job):
    return run_nightly_finish(job)


@job_handler("system.backup")
def backup(job):
    from io import StringIO

    from django.conf import settings

    if settings.SERVERLESS:
        return {"skipped": "No local disk on serverless hosting - use the hosted database's backups "
                           "(e.g. Neon point-in-time restore)."}

    out = StringIO()
    call_command("backup", stdout=out)
    return {"output": out.getvalue()[-2000:]}
