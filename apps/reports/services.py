"""Agent 8: periodic management reports."""
import json
import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone

from apps.agents.llm import agent_run, ai_configured, structured_call
from apps.agents.models import AgentName
from apps.core.models import SystemSettings

from . import metrics
from .models import ReportSnapshot

log = logging.getLogger(__name__)

NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "highlights": {"type": "array", "items": {"type": "string"}},
                   "attention": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "highlights", "attention"],
    "additionalProperties": False,
}


def write_narrative(snap: ReportSnapshot, job=None) -> str:
    sys = SystemSettings.get_solo()
    prompt = ("Write a short management summary of this export & OEM sales report for the owner of Ajay Plastic (Flotec). "
              "Use ONLY the numbers below; do not estimate, extrapolate or add facts. Mention pending approvals and agent errors "
              f"if non-zero.\n\nPERIOD: {snap.get_period_display()} {snap.start} to {snap.end}\nMETRICS:\n{json.dumps(snap.metrics, default=str)}")
    with agent_run(AgentName.REPORT, "report.narrative", snap, job) as tracker:
        data = structured_call(tracker, task="classification", model=sys.ai_model_classification, effort="low",
                               system="You are a precise sales-operations analyst. Plain business English.",
                               user_content=prompt, schema=NARRATIVE_SCHEMA, max_tokens=2000)
    text = data["summary"]
    if data["highlights"]:
        text += "\n\nHighlights:\n" + "\n".join(f"- {h}" for h in data["highlights"])
    if data["attention"]:
        text += "\n\nNeeds attention:\n" + "\n".join(f"- {h}" for h in data["attention"])
    return text


def generate_report(period: str = "daily", ref_date=None, *, narrative: bool = True, email: bool = True, job=None) -> ReportSnapshot:
    start, end = metrics.period_bounds(period, ref_date)
    data = metrics.compute(start, end)
    snap, _ = ReportSnapshot.objects.update_or_create(period=period, start=start, defaults={"end": end, "metrics": data})
    if narrative and ai_configured("classification"):
        try:
            snap.narrative = write_narrative(snap, job)
            snap.save(update_fields=["narrative"])
        except Exception as exc:  # noqa: BLE001 - the numeric report is still valid without the narrative
            log.warning("report narrative failed: %s", exc)
    if email:
        send_report(snap)
    return snap


def send_report(snap: ReportSnapshot) -> list[str]:
    recipients = SystemSettings.get_solo().report_recipient_list
    if not recipients:
        return []
    from .exports import report_to_pdf

    m = snap.metrics
    body = (f"{snap.get_period_display()} report {snap.start} – {snap.end}\n\n"
            f"New companies: {m['new_companies']} · Verified buyers: {m['verified_buyers']}\n"
            f"E-mails sent: {m['emails_sent']} · Replies: {m['replies_received']} · Interested: {m['interested']}\n"
            f"Quotations requested: {m['quotations_requested']} · sent: {m['quotations_sent']} · Orders: {m['orders_confirmed']}\n"
            f"Pending approvals: {m['pending_email_approvals']} e-mails, {m['pending_quotation_approvals']} quotations\n"
            f"Agent errors: {m['agent_errors']} · AI cost: US$ {m['ai_cost_usd']:.2f}\n\n{snap.narrative}\n\n"
            f"Dashboard: {settings.SITE_URL}/")
    msg = EmailMessage(f"[Flotec Sales AI] {snap.get_period_display()} report {snap.start}", body, settings.DEFAULT_FROM_EMAIL, recipients)
    msg.attach(f"report-{snap.period}-{snap.start}.pdf", report_to_pdf(snap), "application/pdf")
    try:
        msg.send(fail_silently=False)
        snap.emailed_to = ", ".join(recipients)
        snap.save(update_fields=["emailed_to"])
    except Exception:  # noqa: BLE001
        log.exception("report e-mail failed")
    return recipients
