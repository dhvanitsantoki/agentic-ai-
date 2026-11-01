from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.agents.testing import FakeClient, json_response
from apps.core.models import ExecutionMode, Role
from apps.core.testing import make_user, seed_reference_data
from apps.followups.models import Enrollment, FollowUpSequence
from apps.followups.processing import process_due
from apps.followups.seeds import seed_sequences
from apps.followups.services import on_followup_discarded
from apps.mailbox.models import InboundEmail
from apps.outreach.models import OutboundEmail, SuppressionEntry
from apps.outreach.seeds import seed_templates
from apps.outreach.sending import send_email
from apps.outreach.tests.helpers import FakeProvider, configure, make_account, make_company

FOLLOWUP = {"subject": "Re: Plastic valves", "body": "Dear Amina,\n\nJust checking you received my note.\n\nBest regards,",
            "facts_used": [], "sensitive_topics": [], "needs_human_input": [], "attach_document_ids": []}


@override_settings(ANTHROPIC_API_KEY="test-key")
class FollowUpTests(TestCase):
    def setUp(self):
        seed_reference_data()
        seed_templates()
        seed_sequences()
        configure(ExecutionMode.CONTROLLED_AUTOMATION)
        self.company, self.contact = make_company()
        self.account = make_account()
        started = timezone.now() - timezone.timedelta(days=3, minutes=5)
        self.initial = OutboundEmail.objects.create(company=self.company, contact=self.contact, to_email=self.contact.email,
                                                    kind="initial", subject="Plastic valves", body_text="Hello", status="sent",
                                                    sent_at=started, account=self.account, rfc_message_id="<i1@flotec>",
                                                    template_id=1)
        from apps.outreach.models import EmailTemplate

        EmailTemplate.objects.update(approved_for_auto_send=True)
        self.initial.template = EmailTemplate.objects.get(kind="distributor")
        self.initial.save()
        self.en = Enrollment.objects.create(company=self.company, contact=self.contact, sequence=FollowUpSequence.default(),
                                            initial_email=self.initial, started_at=started, next_due_at=started + timezone.timedelta(days=3))

    def _process(self, responses):
        with mock.patch("apps.agents.llm.get_client", return_value=FakeClient(responses)):
            return process_due()

    def test_default_schedule_is_3_7_14_30(self):
        self.assertEqual(list(FollowUpSequence.default().steps.values_list("day_offset", flat=True)), [3, 7, 14, 30])

    def test_due_followup_drafted_then_sent_then_next_scheduled(self):
        stats = self._process([json_response(FOLLOWUP)])
        self.assertEqual(stats["drafted"], 1)
        email = OutboundEmail.objects.get(enrollment=self.en, followup_step=1)
        self.assertEqual(email.status, OutboundEmail.Status.APPROVED, "Mode 3 + approved template + clean text")
        self.en.refresh_from_db()
        self.assertEqual(self.en.last_step_done, 1)
        self.assertIsNone(self.en.next_due_at, "next step waits until this one is sent")
        provider = FakeProvider()
        with mock.patch("apps.outreach.sending.get_provider", return_value=provider):
            self.assertEqual(send_email(email), "sent")
        self.assertEqual(provider.sent[0].in_reply_to, "<i1@flotec>", "follow-up threads with the introduction")
        self.en.refresh_from_db()
        self.assertEqual((self.en.next_due_at - self.en.started_at).days, 7)
        self.assertEqual(self._process([]), {"stopped": 0, "drafted": 0, "completed": 0, "skipped": 0, "errors": []},
                         "nothing due before Day 7")

    def test_no_duplicate_email_for_same_step(self):
        self._process([json_response(FOLLOWUP)])
        Enrollment.objects.filter(pk=self.en.pk).update(next_due_at=timezone.now(), last_step_done=0)
        stats = self._process([])
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(OutboundEmail.objects.filter(enrollment=self.en, followup_step=1).count(), 1)

    def test_reply_stops_sequence_before_drafting(self):
        InboundEmail.objects.create(account=self.account, provider_message_id="r", from_email=self.contact.email, company=self.company,
                                    classification="interested", received_at=timezone.now())
        stats = self._process([])
        self.assertEqual(stats["stopped"], 1)
        self.en.refresh_from_db()
        self.assertEqual(self.en.stop_reason, "replied")

    def test_auto_reply_does_not_stop(self):
        InboundEmail.objects.create(account=self.account, provider_message_id="r", from_email=self.contact.email, company=self.company,
                                    classification="auto_reply", received_at=timezone.now())
        self.assertEqual(self._process([json_response(FOLLOWUP)])["drafted"], 1)

    def test_unsubscribed_or_declined_stops(self):
        SuppressionEntry.add(self.contact.email, SuppressionEntry.Reason.UNSUBSCRIBE)
        self._process([])
        self.en.refresh_from_db()
        self.assertEqual(self.en.stop_reason, "unsubscribed")

    def test_mode1_drafts_nothing(self):
        configure(ExecutionMode.RESEARCH_ONLY)
        self.assertEqual(self._process([])["skipped"], 1)

    def test_rejected_followup_moves_to_next_step_and_final_completes(self):
        self._process([json_response(FOLLOWUP)])
        email = OutboundEmail.objects.get(enrollment=self.en, followup_step=1)
        email.status = OutboundEmail.Status.REJECTED
        email.save()
        on_followup_discarded(email)
        self.en.refresh_from_db()
        self.assertIsNotNone(self.en.next_due_at)
        Enrollment.objects.filter(pk=self.en.pk).update(last_step_done=4, next_due_at=timezone.now())
        self.assertEqual(self._process([])["completed"], 1)

    def test_sequence_editor_validates_increasing_days(self):
        self.client.force_login(make_user("mgr", Role.MANAGER))
        seq = FollowUpSequence.default()
        steps = list(seq.steps.all())
        data = {"name": seq.name, "description": "", "is_default": "on", "is_active": "on",
                "steps-TOTAL_FORMS": str(len(steps) + 1), "steps-INITIAL_FORMS": str(len(steps)), "steps-MIN_NUM_FORMS": "0",
                "steps-MAX_NUM_FORMS": "1000"}
        for i, st in enumerate(steps):
            data.update({f"steps-{i}-id": st.pk, f"steps-{i}-step_number": st.step_number, f"steps-{i}-day_offset": st.day_offset,
                         f"steps-{i}-angle": st.angle})
        data.update({f"steps-{len(steps)}-step_number": "5", f"steps-{len(steps)}-day_offset": "20", f"steps-{len(steps)}-angle": "x"})
        self.client.post(f"/followups/sequences/{seq.pk}/", data)
        self.assertEqual(seq.steps.count(), 4, "day 20 after day 30 is rejected")
        data[f"steps-{len(steps)}-day_offset"] = "45"
        self.client.post(f"/followups/sequences/{seq.pk}/", data)
        self.assertEqual(list(seq.steps.values_list("day_offset", flat=True)), [3, 7, 14, 30, 45])
