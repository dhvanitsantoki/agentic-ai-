from django.core.management import call_command

from .pipeline import run_nightly
from .registry import job_handler


@job_handler("pipeline.nightly")
def nightly(job):
    return run_nightly(job)


@job_handler("system.backup")
def backup(job):
    from io import StringIO

    out = StringIO()
    call_command("backup", stdout=out)
    return {"output": out.getvalue()[-2000:]}
