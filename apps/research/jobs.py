from apps.agents.registry import job_handler
from apps.crm.models import Company, Contact

from . import pipeline, services
from .models import ResearchRun
from .verification import PageCache


@job_handler("research.run")
def run_research(job):
    run = ResearchRun.objects.get(pk=job.payload["run_id"])
    pipeline.execute_run(run, job=job)
    return {"run": run.pk, "added": run.added_count, "duplicates": run.duplicate_count, "review": run.review_count}


@job_handler("research.verify_contact")
def verify_contact(job):
    contact = Contact.objects.filter(pk=job.payload["contact_id"]).first()
    if not contact:
        return {"skipped": "contact deleted"}
    services.verify_contact(contact)
    return {"contact": contact.pk, "status": contact.email_status}


@job_handler("research.verify_companies")
def verify_companies(job):
    cache = PageCache()
    results = []
    for company in Company.objects.filter(pk__in=job.payload.get("company_ids", [])):
        results.append(services.verify_company(company, cache))
    return {"verified": len(results)}
