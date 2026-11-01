from datetime import datetime, timezone as dt_tz
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.agents.testing import FakeClient, json_response
from apps.core.models import ExecutionMode
from apps.core.testing import seed_reference_data
from apps.crm.models import Company, Contact, Requirement, Stage, Task
from apps.followups.models import Enrollment, FollowUpSequence
from apps.followups.seeds import seed_sequences
from apps.mailbox.models import Classification, InboundEmail
from apps.mailbox.processing import process_inbound, sync_account
from apps.mailbox.providers.base import IncomingMessage
from apps.outreach.models import OutboundEmail, SuppressionEntry
from apps.outreach.seeds import seed_templates
from apps.outreach.tests.helpers import configure, make_account, make_company
from apps.quotations.models import Quotation


def analysis(cls, **kw):
    base = {"classification": cls, "confidence": 0.9, "summary": f"Buyer e-mail: {cls}", "is_urgent": False,
            "sender_company_name": "", "sender_person_title": "", "requirements": [], "questions": [], "wants_catalogue": False,
            "wants_quotation": False, "wants_sample": False, "private_label_or_branding": "", "target_price_mentioned": "",
            "destination_or_incoterm": "", "currency_mentioned": "", "reply_needed": False, "suggested_next_action": "Call the buyer"}
    base.update(kw)
    return base


DRAFT = {"subject": "Re: Plastic valves", "body": "Dear Amina,\n\nThank you. [TEAM TO CONFIRM: sizes]\n\nBest regards,",
         "facts_used": [], "sensitive_topics": [], "needs_human_input": ["Confirm sizes"], "attach_document_ids": []}


@override_settings(ANTHROPIC_API_KEY="test-key")
class InboundProcessingTests(TestCase):
    def setUp(self):
        seed_reference_data()
        seed_templates()
        seed_sequences()
        configure(ExecutionMode.DRAFT_AND_APPROVE)
        self.company, self.contact = make_company()
        self.account = make_account()
        self.sent = OutboundEmail.objects.create(company=self.company, contact=self.contact, to_email=self.contact.email, kind="initial",
                                                 subject="Plastic valves", body_text="Hello", status="sent", account=self.account,
                                                 sent_at=timezone.now() - timezone.timedelta(days=2), rfc_message_id="<out1@flotec.example>",
                                                 provider_thread_id="t-1")
        self.company.stage = Stage.CONTACTED
        self.company.save()
        self.en = Enrollment.objects.create(company=self.company, contact=self.contact, sequence=FollowUpSequence.default(),
                                            initial_email=self.sent, started_at=self.sent.sent_at,
                                            next_due_at=timezone.now() + timezone.timedelta(days=1))

    def inbound(self, **kw):
        defaults = {"account": self.account, "provider_message_id": f"m{InboundEmail.objects.count() + 1}",
                    "from_email": self.contact.email, "subject": "Re: Plastic valves", "body_text": "Thanks"}
        defaults.update(kw)
        return InboundEmail.objects.create(**defaults)

    def run_ai(self, inbound, responses):
        client = FakeClient(responses)
        with mock.patch("apps.agents.llm.get_client", return_value=client):
            process_inbound(inbound)
        inbound.refresh_from_db()
        return client

    def test_bounce_marks_contact_and_suppresses(self):
        b = self.inbound(from_email="mailer-daemon@googlemail.com", subject="Delivery Status Notification (Failure)",
                         body_text=f"Address not found: {self.contact.email} could not be delivered")
        self.run_ai(b, [])
        self.assertEqual(b.classification, Classification.BOUNCE)
        self.contact.refresh_from_db()
        self.en.refresh_from_db()
        self.sent.refresh_from_db()
        self.assertEqual(self.contact.email_status, Contact.EmailStatus.BOUNCED)
        self.assertTrue(SuppressionEntry.is_suppressed(self.contact.email))
        self.assertEqual(self.en.stop_reason, "bounced")
        self.assertIsNotNone(self.sent.bounced_at)

    def test_auto_reply_does_not_stop_followups(self):
        a = self.inbound(subject="Automatic reply: Plastic valves", headers={"auto-submitted": "auto-replied"})
        self.run_ai(a, [])
        self.assertEqual(a.classification, Classification.AUTO_REPLY)
        self.en.refresh_from_db()
        self.assertEqual(self.en.status, Enrollment.Status.ACTIVE)
        self.assertFalse(a.needs_attention)

    def test_unsubscribe_request(self):
        u = self.inbound(body_text="Please remove me from your mailing list.")
        self.run_ai(u, [])
        self.assertEqual(u.classification, Classification.UNSUBSCRIBE)
        self.assertTrue(SuppressionEntry.is_suppressed(self.contact.email))
        self.company.refresh_from_db()
        self.assertEqual(self.company.stage, Stage.LOST)

    def test_interested_reply_threads_stops_followups_escalates_and_drafts(self):
        pending = OutboundEmail.objects.create(company=self.company, contact=self.contact, to_email=self.contact.email, kind="followup",
                                               subject="Re: x", body_text="b", status="pending_approval", enrollment=self.en, followup_step=1)
        r = self.inbound(in_reply_to="<out1@flotec.example>", body_text="Yes we are interested, which sizes do you make?")
        client = self.run_ai(r, [json_response(analysis("interested", reply_needed=True, questions=["Which sizes?"])), json_response(DRAFT)])
        self.assertEqual(r.matched_outbound, self.sent)
        self.assertEqual(r.classification, Classification.INTERESTED)
        self.company.refresh_from_db()
        self.en.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(self.company.stage, Stage.INTERESTED)
        self.assertEqual(self.en.stop_reason, "replied")
        self.assertEqual(pending.status, OutboundEmail.Status.CANCELLED, "queued follow-up cancelled on reply")
        self.assertTrue(Task.objects.filter(company=self.company, kind="escalation").exists())
        reply = OutboundEmail.objects.get(in_reply_to=r)
        self.assertEqual(reply.status, OutboundEmail.Status.PENDING_APPROVAL)
        self.assertEqual(reply.to_email, self.contact.email)
        self.assertIn("ai_flagged", reply.sensitive_flags, "needs-human-input forces review")
        self.assertEqual(len(client.messages.calls), 2)
        self.assertIn("output_config", client.messages.calls[0])
        self.assertEqual(client.messages.calls[0]["output_config"]["format"]["type"], "json_schema")

    def test_quotation_request_creates_requirements_and_draft_quotation(self):
        r = self.inbound(in_reply_to="<out1@flotec.example>", body_text="Please quote 5000 pcs PVC ball valve 1/2 inch, CIF Mombasa")
        reqs = [{"description": "PVC ball valve", "material": "PVC", "size": '1/2"', "quantity": "5000 pcs", "pressure_rating": "",
                 "connection_type": ""}]
        self.run_ai(r, [json_response(analysis("need_quotation", wants_quotation=True, requirements=reqs,
                                               destination_or_incoterm="CIF Mombasa", currency_mentioned="USD"))])
        self.company.refresh_from_db()
        self.assertEqual(self.company.stage, Stage.REQUIREMENT)
        req = Requirement.objects.get(company=self.company)
        self.assertEqual((req.material, req.quantity, req.source_email), ("PVC", "5000 pcs", r))
        q = Quotation.objects.get(company=self.company)
        self.assertEqual(q.status, Quotation.Status.NEEDS_INPUT, "no verified product / approved price -> human input")
        self.assertTrue(q.created_by_agent)
        self.assertIn("CIF Mombasa", q.internal_notes)
        self.assertTrue(any("approved price" in m or "verified product" in m for m in q.missing_fields))

    def test_not_interested_closes_and_suppresses_future_intros(self):
        r = self.inbound(body_text="No thanks, we already have a supplier.")
        self.run_ai(r, [json_response(analysis("not_interested"))])
        self.company.refresh_from_db()
        self.assertEqual(self.company.stage, Stage.LOST)
        self.assertTrue(SuppressionEntry.is_suppressed(self.contact.email))
        self.assertEqual(Task.objects.filter(kind="escalation").count(), 0)

    def test_unknown_sender_enquiry_creates_lead(self):
        r = self.inbound(from_email="ops@newbuyer.example", from_name="Omar", body_text="We need a catalogue of your CPVC valves")
        self.run_ai(r, [json_response(analysis("need_catalogue", sender_company_name="New Buyer Trading LLC"))])
        company = Company.objects.get(name="New Buyer Trading LLC")
        self.assertEqual(company.lead_source, "inbound")
        self.assertEqual(company.contacts.get().email_status, Contact.EmailStatus.VERIFIED)
        self.assertEqual(r.company, company)

    def test_complaint_is_urgent_escalation(self):
        r = self.inbound(body_text="The last shipment had leaking valves!")
        self.run_ai(r, [json_response(analysis("complaint", is_urgent=True))])
        t = Task.objects.get(company=self.company, kind="escalation")
        self.assertEqual(t.priority, "urgent")

    def test_without_ai_messages_wait_for_human(self):
        r = self.inbound(body_text="Hello")
        with override_settings(ANTHROPIC_API_KEY=""):
            process_inbound(r)
        r.refresh_from_db()
        self.assertEqual(r.classification, Classification.PENDING)
        self.assertTrue(r.needs_attention)


class SyncTests(TestCase):
    def test_sync_dedupes_and_skips_own_messages(self):
        seed_reference_data()
        account = make_account()

        class P:
            def fetch_new(self, acc, cursor):
                when = datetime(2026, 11, 1, tzinfo=dt_tz.utc)
                return [IncomingMessage(provider_message_id="a", from_email="x@buyer.example", subject="s", body_text="b", received_at=when),
                        IncomingMessage(provider_message_id="b", from_email="export@flotec.example", subject="own", body_text="b", received_at=when)], "c2"

        with mock.patch("apps.mailbox.processing.get_provider", return_value=P()):
            first = sync_account(account)
            second = sync_account(account)
        self.assertEqual(first["new"], 1)
        self.assertEqual(second["new"], 0)
        account.refresh_from_db()
        self.assertEqual(account.sync_cursor, "c2")
