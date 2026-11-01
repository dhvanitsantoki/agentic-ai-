from datetime import datetime
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone

from apps.core import policy
from apps.core.models import AutomationState, ExecutionMode, SystemSettings
from apps.core.testing import seed_reference_data
from apps.crm.models import Contact
from apps.followups.models import Enrollment, FollowUpSequence
from apps.followups.seeds import seed_sequences
from apps.followups.services import next_send_window
from apps.mailbox.models import InboundEmail
from apps.outreach.models import EmailKind, OutboundEmail, SuppressionEntry
from apps.outreach.seeds import seed_templates

from .helpers import agent_email, configure, make_account, make_company


class SensitiveDetectionTests(TestCase):
    def test_detects_mandatory_approval_topics(self):
        cases = {
            "Our price is USD 1.20 per piece": {"price"},
            "We can offer a 5% discount": {"discount"},
            "Payment terms: 30% advance payment, balance against B/L": {"payment_terms"},
            "Lead time is 25 days after order": {"delivery_commitment"},
            "We can appoint you as exclusive distributor": {"exclusivity"},
            "Our valves are ISO 9001 certified": {"certification"},
            "Rated PN16 and 10 bar at 23°C": {"technical_claim"},
            "Please send your purchase order": {"purchase_order"},
            "Production capacity of 50,000 pcs per month": {"capacity_claim"},
            "Please find attached our quotation": {"quotation"},
        }
        for text, expected in cases.items():
            self.assertTrue(expected <= set(policy.detect_sensitive(text)), (text, policy.detect_sensitive(text)))

    def test_plain_introduction_is_not_sensitive(self):
        text = ("Dear Ms Amina,\n\nI noticed that Delta Agro distributes drip irrigation kits in Kenya. We manufacture PVC and PP "
                "ball valves in Ahmedabad and would be glad to share our catalogue. Which valve sizes do you sell most?\n\nBest regards,")
        self.assertEqual(policy.detect_sensitive(text), [])


class PolicyTests(TestCase):
    def setUp(self):
        seed_reference_data()
        seed_templates()
        seed_sequences()
        self.company, self.contact = make_company()

    def test_mode2_always_needs_approval(self):
        configure(ExecutionMode.DRAFT_AND_APPROVE)
        e = agent_email(self.company, self.contact)
        policy.apply(e)
        self.assertEqual(e.status, OutboundEmail.Status.PENDING_APPROVAL)

    def test_mode3_auto_approves_clean_pre_approved_email(self):
        configure(ExecutionMode.CONTROLLED_AUTOMATION)
        e = agent_email(self.company, self.contact)
        d = policy.apply(e)
        self.assertTrue(d.auto_send, d.approval)
        self.assertEqual(e.status, OutboundEmail.Status.APPROVED)

    def test_mode3_sensitive_content_needs_human(self):
        configure(ExecutionMode.CONTROLLED_AUTOMATION)
        e = agent_email(self.company, self.contact, body="Our price is USD 1.10 per piece for 20 mm.")
        policy.apply(e)
        self.assertEqual(e.status, OutboundEmail.Status.PENDING_APPROVAL)
        self.assertIn("price", e.sensitive_flags)

    def test_mode3_unapproved_template_or_kind_needs_human(self):
        configure(ExecutionMode.CONTROLLED_AUTOMATION, auto_kinds=("followup",))
        e = agent_email(self.company, self.contact)
        policy.apply(e)
        self.assertEqual(e.status, OutboundEmail.Status.PENDING_APPROVAL)
        configure(ExecutionMode.CONTROLLED_AUTOMATION)
        e2 = agent_email(self.company, self.contact, approve_template=False, template_kind="importer")
        self.assertFalse(policy.evaluate(e2).auto_send)

    def test_mode3_paused_automation_needs_human(self):
        configure(ExecutionMode.CONTROLLED_AUTOMATION, state=AutomationState.PAUSED)
        e = agent_email(self.company, self.contact)
        self.assertFalse(policy.evaluate(e).auto_send)

    def test_replies_and_quotations_never_auto(self):
        configure(ExecutionMode.CONTROLLED_AUTOMATION, auto_kinds=("initial", "followup", "reply", "quotation"))
        InboundEmail.objects.create(account=make_account(), provider_message_id="m1", from_email=self.contact.email,
                                    company=self.company, classification="interested")
        e = agent_email(self.company, self.contact, kind=EmailKind.REPLY)
        self.assertFalse(policy.evaluate(e).auto_send)

    def test_mode1_blocks_agent_emails(self):
        configure(ExecutionMode.RESEARCH_ONLY)
        e = agent_email(self.company, self.contact)
        policy.apply(e)
        self.assertEqual(e.status, OutboundEmail.Status.BLOCKED)

    def test_blocks_unverified_suppressed_dnc_and_duplicates(self):
        configure(ExecutionMode.CONTROLLED_AUTOMATION)
        _, unverified = make_company("Unv Co", "x@unv.example", status=Contact.EmailStatus.UNVERIFIED)
        self.assertTrue(policy.evaluate(agent_email(unverified.company, unverified)).is_blocked)
        SuppressionEntry.add(self.contact.email, SuppressionEntry.Reason.UNSUBSCRIBE)
        self.assertTrue(policy.evaluate(agent_email(self.company, self.contact)).is_blocked)
        SuppressionEntry.objects.all().delete()
        SuppressionEntry.objects.create(domain="delta-agro.example", reason="manual")
        self.assertTrue(policy.evaluate(agent_email(self.company, self.contact)).is_blocked, "domain suppression")
        SuppressionEntry.objects.all().delete()
        first = agent_email(self.company, self.contact)
        OutboundEmail.objects.filter(pk=first.pk).update(status="sent", sent_at=timezone.now())
        d = policy.evaluate(agent_email(self.company, self.contact))
        self.assertTrue(any("already sent" in r for r in d.blocked))
        self.company.do_not_contact = True
        self.company.save()
        self.assertTrue(policy.evaluate(agent_email(self.company, self.contact)).is_blocked)

    def test_followup_blocked_after_reply(self):
        configure(ExecutionMode.CONTROLLED_AUTOMATION)
        en = Enrollment.objects.create(company=self.company, contact=self.contact, sequence=FollowUpSequence.default(),
                                       started_at=timezone.now() - timezone.timedelta(days=4))
        e = agent_email(self.company, self.contact, kind=EmailKind.FOLLOWUP, enrollment=en, followup_step=1)
        self.assertFalse(policy.evaluate(e).is_blocked)
        InboundEmail.objects.create(account=make_account(), provider_message_id="r1", from_email=self.contact.email,
                                    company=self.company, classification="interested")
        self.assertTrue(policy.evaluate(e).is_blocked)

    def test_human_written_email_needs_review(self):
        configure(ExecutionMode.CONTROLLED_AUTOMATION)
        e = OutboundEmail.objects.create(company=self.company, contact=self.contact, to_email=self.contact.email,
                                         kind=EmailKind.MANUAL, subject="Hello", body_text="Hello")
        self.assertFalse(policy.evaluate(e).auto_send)


class DispatchGateTests(TestCase):
    def setUp(self):
        seed_reference_data()
        seed_templates()
        self.company, self.contact = make_company()
        self.account = make_account()

    def test_recipient_business_hours(self):
        tz = "Africa/Nairobi"
        sat_night = datetime(2026, 11, 7, 23, 0, tzinfo=ZoneInfo(tz))  # Saturday 23:00 local
        nxt = next_send_window(tz, sat_night, 9, 17, True)
        self.assertEqual((nxt.weekday(), nxt.hour), (0, 9), "Monday 09:00 local")
        mid = datetime(2026, 11, 4, 11, 30, tzinfo=ZoneInfo(tz))
        self.assertEqual(next_send_window(tz, mid, 9, 17, True), mid)

    def test_gate_states_and_limits(self):
        s = configure(ExecutionMode.CONTROLLED_AUTOMATION)
        e = agent_email(self.company, self.contact)
        ok, _, _ = policy.can_dispatch_now(e, self.account)
        self.assertTrue(ok)
        s.automation_state = AutomationState.PAUSED
        s.save()
        self.assertFalse(policy.can_dispatch_now(e, self.account)[0], "auto-approved mail waits while paused")
        e.approved_by = __import__("apps.core.testing", fromlist=["make_user"]).make_user("mgr")
        self.assertTrue(policy.can_dispatch_now(e, self.account)[0], "human-approved mail may go while paused")
        s.automation_state = AutomationState.STOPPED
        s.save()
        self.assertFalse(policy.can_dispatch_now(e, self.account)[0])
        s.automation_state = AutomationState.RUNNING
        s.daily_send_limit = 1
        s.save()
        OutboundEmail.objects.create(company=self.company, to_email="z@z.example", kind="manual", subject="s", body_text="b",
                                     account=self.account, status="sent", sent_at=timezone.now())
        ok, reason, retry = policy.can_dispatch_now(e, self.account)
        self.assertFalse(ok)
        self.assertIn("Daily", reason)
        self.assertIsNotNone(retry)

    def test_outside_window_defers(self):
        s = configure(ExecutionMode.CONTROLLED_AUTOMATION)
        s.send_window_start_hour, s.send_window_end_hour = 9, 17
        s.save()
        e = agent_email(self.company, self.contact)
        night = datetime(2026, 11, 4, 2, 0, tzinfo=ZoneInfo("Africa/Nairobi"))
        ok, reason, retry = policy.can_dispatch_now(e, self.account, now=night)
        self.assertFalse(ok)
        self.assertEqual(retry.astimezone(ZoneInfo("Africa/Nairobi")).hour, 9)
