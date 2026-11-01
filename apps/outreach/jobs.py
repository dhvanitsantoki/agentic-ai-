from apps.agents.registry import job_handler

from .models import OutboundEmail
from .sending import send_email, send_queue


@job_handler("outreach.send_queue")
def outreach_send_queue(job):
    return send_queue()


@job_handler("outreach.send_one")
def outreach_send_one(job):
    email = OutboundEmail.objects.get(pk=job.payload["email_id"])
    return {"outcome": send_email(email)}


@job_handler("outreach.prepare_drafts")
def outreach_prepare_drafts(job):
    from apps.core.models import SystemSettings

    from .drafting import prepare_initial_drafts

    return prepare_initial_drafts(limit=job.payload.get("limit") or SystemSettings.get_solo().daily_send_limit, job=job)


@job_handler("outreach.draft_for_company")
def outreach_draft_for_company(job):
    from django.contrib.auth import get_user_model

    from apps.crm.models import Company, Contact

    from .drafting import draft_initial
    from .models import EmailTemplate

    company = Company.objects.get(pk=job.payload["company_id"])
    contact = Contact.objects.get(pk=job.payload["contact_id"], company=company)
    template = EmailTemplate.objects.filter(pk=job.payload.get("template_id")).first()
    user = get_user_model().objects.filter(pk=job.payload.get("user_id")).first()
    email = draft_initial(company, contact, template=template, user=user, instructions=job.payload.get("instructions", ""), job=job)
    return {"email": email.pk, "status": email.status}
