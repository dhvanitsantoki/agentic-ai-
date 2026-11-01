from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.core.models import ExecutionMode, Role
from apps.core.testing import make_user, seed_reference_data
from apps.crm.models import Stage
from apps.followups.models import Enrollment
from apps.followups.seeds import seed_sequences
from apps.mailbox.models import MailAccount
from apps.mailbox.providers.base import AuthError
from apps.outreach.models import OutboundEmail, SuppressionEntry
from apps.outreach.seeds import seed_templates
from apps.outreach.sending import send_email, send_queue, unsubscribe_url
from apps.products.models import CompanyDocument

from .helpers import FakeProvider, agent_email, configure, make_account, make_company


class SendingTests(TestCase):
    def setUp(self):
        seed_reference_data()
        seed_templates()
        seed_sequences()
        configure(ExecutionMode.CONTROLLED_AUTOMATION)
        self.company, self.contact = make_company()
        self.account = make_account()
        self.provider = FakeProvider()
        patcher = mock.patch("apps.outreach.sending.get_provider", return_value=self.provider)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _approved(self, **kw):
        from apps.core import policy

        e = agent_email(self.company, self.contact, **kw)
        policy.apply(e)
        self.assertEqual(e.status, OutboundEmail.Status.APPROVED)
        return e

    def test_send_updates_crm_followups_and_compliance_headers(self):
        e = self._approved()
        self.assertEqual(send_email(e), "sent")
        e.refresh_from_db()
        self.assertEqual(e.status, OutboundEmail.Status.SENT)
        self.assertEqual(e.provider_message_id, "prov-1")
        self.assertTrue(e.rfc_message_id.startswith("<"))
        msg = self.provider.sent[0]
        self.assertIn("unsubscribe", msg.body_text.lower())
        self.assertIn("/u/", msg.body_text)
        self.assertIn("List-Unsubscribe", msg.headers)
        self.company.refresh_from_db()
        self.assertEqual(self.company.stage, Stage.CONTACTED)
        self.assertIsNotNone(self.company.last_contacted_at)
        en = Enrollment.objects.get(contact=self.contact)
        self.assertEqual(en.status, Enrollment.Status.ACTIVE)
        self.assertEqual((en.next_due_at - en.started_at).days, 3, "first follow-up on Day 3")
        self.assertTrue(self.company.activities.filter(kind="email_out").exists())

    def test_never_resends_and_blocks_second_intro(self):
        e = self._approved()
        send_email(e)
        self.assertEqual(send_email(e), "sent", "status is returned unchanged for an already-sent e-mail")
        self.assertEqual(len(self.provider.sent), 1)
        e2 = agent_email(self.company, self.contact)
        OutboundEmail.objects.filter(pk=e2.pk).update(status="approved")
        self.assertEqual(send_email(e2), "blocked")

    def test_suppressed_after_approval_is_blocked_at_send_time(self):
        e = self._approved()
        SuppressionEntry.add(self.contact.email, SuppressionEntry.Reason.UNSUBSCRIBE)
        self.assertEqual(send_email(e), "blocked")
        self.assertEqual(self.provider.sent, [])

    def test_attachments_only_approved_documents(self):
        from django.core.files.base import ContentFile

        doc = CompanyDocument(title="Flotec Catalogue 2026", kind="catalogue", approved_for_sending=True)
        doc.file.save("cat.pdf", ContentFile(b"%PDF-1.4 test"), save=True)
        internal = CompanyDocument(title="Internal price list", kind="price", approved_for_sending=False)
        internal.file.save("prices.pdf", ContentFile(b"%PDF-1.4 secret"), save=True)
        e = self._approved()
        e.attachments.add(doc, internal)
        send_email(e)
        self.assertEqual([a.filename for a in self.provider.sent[0].attachments], ["flotec-catalogue-2026.pdf"])
        self.assertEqual(self.provider.sent[0].attachments[0].content, b"%PDF-1.4 test")

    def test_auth_error_marks_mailbox_and_retries(self):
        self.provider.fail = AuthError("revoked")
        e = self._approved()
        self.assertEqual(send_email(e), "failed")
        e.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(e.status, OutboundEmail.Status.APPROVED)
        self.assertIsNotNone(e.scheduled_for)
        self.assertEqual(self.account.status, MailAccount.Status.ERROR)

    def test_no_mailbox_defers(self):
        MailAccount.objects.all().delete()
        e = self._approved()
        self.assertEqual(send_email(e), "deferred")

    def test_send_queue_respects_schedule(self):
        e = self._approved()
        OutboundEmail.objects.filter(pk=e.pk).update(scheduled_for=timezone.now() + timezone.timedelta(hours=2))
        self.assertEqual(send_queue(), {})
        OutboundEmail.objects.filter(pk=e.pk).update(scheduled_for=None)
        self.assertEqual(send_queue(), {"sent": 1})


class ApprovalViewTests(TestCase):
    def setUp(self):
        seed_reference_data()
        seed_templates()
        configure(ExecutionMode.DRAFT_AND_APPROVE)
        self.company, self.contact = make_company()
        self.exec = make_user("exec", Role.EXECUTIVE)
        self.manager = make_user("mgr", Role.MANAGER)

    def _pending(self, body):
        from apps.core import policy

        e = agent_email(self.company, self.contact, body=body)
        policy.apply(e)
        return e

    def test_executive_approves_plain_email_and_job_is_queued(self):
        from apps.agents.models import Job

        e = self._pending("Dear Amina, we make valves. Best regards,")
        self.client.force_login(self.exec)
        self.client.post(f"/outreach/emails/{e.pk}/decide/", {"action": "approve"})
        e.refresh_from_db()
        self.assertEqual(e.status, OutboundEmail.Status.APPROVED)
        self.assertEqual(e.approved_by, self.exec)
        self.assertTrue(Job.objects.filter(kind="outreach.send_one", payload__email_id=e.pk).exists())

    def test_sensitive_email_needs_manager(self):
        e = self._pending("Our price is USD 2.00 per piece. Best regards,")
        self.client.force_login(self.exec)
        self.client.post(f"/outreach/emails/{e.pk}/decide/", {"action": "approve"})
        e.refresh_from_db()
        self.assertEqual(e.status, OutboundEmail.Status.PENDING_APPROVAL)
        self.client.force_login(self.manager)
        self.client.post(f"/outreach/emails/{e.pk}/decide/", {"action": "approve"})
        e.refresh_from_db()
        self.assertEqual(e.status, OutboundEmail.Status.APPROVED)

    def test_editing_resets_approval(self):
        e = self._pending("Hello. Best regards,")
        self.client.force_login(self.exec)
        self.client.post(f"/outreach/emails/{e.pk}/edit/", {"to_email": e.to_email, "subject": "New subject",
                                                           "body_text": "Changed. Our discount is 10%."})
        e.refresh_from_db()
        self.assertEqual(e.status, OutboundEmail.Status.PENDING_APPROVAL)
        self.assertIn("discount", e.sensitive_flags)


class UnsubscribeTests(TestCase):
    def setUp(self):
        seed_reference_data()
        seed_sequences()
        self.company, self.contact = make_company()
        from apps.followups.models import FollowUpSequence

        self.en = Enrollment.objects.create(company=self.company, contact=self.contact, sequence=FollowUpSequence.default())
        self.pending = OutboundEmail.objects.create(company=self.company, contact=self.contact, to_email=self.contact.email,
                                                    kind="followup", subject="s", body_text="b", status="pending_approval",
                                                    enrollment=self.en, followup_step=1)

    def test_link_flow(self):
        url = unsubscribe_url(self.contact.email, self.company.pk).replace("http://localhost:8000", "")
        resp = self.client.get(url)
        self.assertContains(resp, "Yes, unsubscribe me")
        self.assertFalse(SuppressionEntry.is_suppressed(self.contact.email), "GET alone never unsubscribes")
        resp = self.client.post(url)
        self.assertContains(resp, "have been unsubscribed")
        self.assertTrue(SuppressionEntry.is_suppressed(self.contact.email))
        self.contact.refresh_from_db()
        self.en.refresh_from_db()
        self.pending.refresh_from_db()
        self.assertIsNotNone(self.contact.unsubscribed_at)
        self.assertEqual(self.en.status, Enrollment.Status.STOPPED)
        self.assertEqual(self.pending.status, OutboundEmail.Status.CANCELLED)

    def test_one_click_post_without_csrf_and_bad_token(self):
        from django.test import Client

        c = Client(enforce_csrf_checks=True)
        url = unsubscribe_url(self.contact.email, self.company.pk).replace("http://localhost:8000", "")
        self.assertEqual(c.post(url, {"List-Unsubscribe": "One-Click"}).status_code, 200)
        self.assertTrue(SuppressionEntry.is_suppressed(self.contact.email))
        self.assertEqual(self.client.get("/u/not-a-valid-token/").status_code, 400)
