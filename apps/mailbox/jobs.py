from apps.agents.registry import job_handler

from .models import InboundEmail
from .processing import process_inbound, sync_all


@job_handler("mailbox.sync_all")
def mailbox_sync_all(job):
    return sync_all(job=job)


@job_handler("mailbox.process_one")
def mailbox_process_one(job):
    inbound = InboundEmail.objects.get(pk=job.payload["inbound_id"])
    if job.payload.get("force"):
        inbound.processed_at = None
    process_inbound(inbound, job=job)
    return {"classification": inbound.classification}


@job_handler("mailbox.draft_reply")
def mailbox_draft_reply(job):
    from django.contrib.auth import get_user_model

    from apps.outreach.drafting import draft_reply

    inbound = InboundEmail.objects.get(pk=job.payload["inbound_id"])
    user = get_user_model().objects.filter(pk=job.payload.get("user_id")).first()
    email = draft_reply(inbound, job=job, user=user)
    return {"email": email.pk if email else None}
