from types import SimpleNamespace

from django.utils import timezone

from apps.core.models import AutomationState, CompanyProfile, ExecutionMode, SystemSettings
from apps.crm.models import Company, Contact, Stage
from apps.geo.models import Country
from apps.mailbox.models import MailAccount
from apps.mailbox.providers.base import SentResult
from apps.outreach.models import EmailKind, EmailTemplate, OutboundEmail


def configure(mode=ExecutionMode.DRAFT_AND_APPROVE, state=AutomationState.RUNNING, auto_kinds=("initial", "followup")):
    s = SystemSettings.get_solo()
    s.execution_mode = mode
    s.automation_state = state
    s.auto_send_kinds = list(auto_kinds)
    s.send_window_start_hour, s.send_window_end_hour, s.skip_weekends = 0, 24, False
    s.save()
    p = CompanyProfile.get_solo()
    p.about = "Manufacturer of plastic valves in Ahmedabad."
    p.website = "https://flotec.example"
    p.sender_name = "Sales Team"
    p.approved_facts = "Manufacturer of PVC and PP ball valves"
    p.save()
    return s


def make_company(name="Delta Agro", email="buyer@delta-agro.example", status=Contact.EmailStatus.VERIFIED, country="KE"):
    company = Company.objects.create(name=name, website=f"https://{email.split('@')[1]}", country=Country.objects.get(iso2=country),
                                     stage=Stage.VERIFIED, oem_category="C", company_type="distributor")
    contact = Contact.objects.create(company=company, email=email, full_name="Amina Buyer", email_status=status, is_primary=True)
    return company, contact


def make_account(email="export@flotec.example"):
    acc = MailAccount(provider="gmail", email_address=email, display_name="Flotec Export", is_default_sender=True)
    acc.access_token = "tok"
    acc.refresh_token = "ref"
    acc.token_expires_at = timezone.now() + timezone.timedelta(hours=1)
    acc.save()
    return acc


def agent_email(company, contact, *, kind=EmailKind.INITIAL, body="Dear Amina,\n\nWe make plastic valves.\n\nBest regards,", subject="Plastic valves",
                template_kind="distributor", approve_template=True, **kw):
    template = EmailTemplate.objects.filter(kind=template_kind).first()
    if template and approve_template:
        template.approved_for_auto_send = True
        template.save()
    return OutboundEmail.objects.create(company=company, contact=contact, to_email=contact.email, kind=kind, template=template,
                                        subject=subject, body_text=body, created_by_agent="outreach", **kw)


class FakeProvider:
    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail

    def send(self, account, msg):
        if self.fail:
            raise self.fail
        self.sent.append(msg)
        return SentResult(provider_message_id=f"prov-{len(self.sent)}", thread_id="thread-1", rfc_message_id=msg.message_id)
