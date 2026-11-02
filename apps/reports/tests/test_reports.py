import os
import tempfile
from datetime import date, timedelta
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.agents.models import AgentRun
from apps.agents.pipeline import run_nightly
from apps.agents.testing import FakeClient, json_response
from apps.core.models import CompanyProfile, ExecutionMode, SystemSettings
from apps.core.testing import seed_reference_data
from apps.crm.models import Stage
from apps.mailbox.models import InboundEmail
from apps.outreach.drafting import grounding_flags, prepare_initial_drafts
from apps.outreach.models import OutboundEmail
from apps.outreach.seeds import seed_templates
from apps.outreach.tests.helpers import configure, make_account, make_company
from apps.reports import metrics
from apps.reports.services import generate_report

INTRO = {"subject": "Plastic valves for Delta Agro", "body": "Dear Amina,\n\nI saw Delta Agro distributes drip kits.\n\nBest regards,",
         "facts_used": [{"fact": "Distributes drip kits", "source": "buyer website"}], "sensitive_topics": [], "needs_human_input": [],
         "attach_document_ids": []}


class MetricsTests(TestCase):
    def setUp(self):
        seed_reference_data()
        self.company, self.contact = make_company()
        self.account = make_account()

    def test_period_bounds(self):
        ref = date(2026, 11, 4)  # Wednesday
        self.assertEqual(metrics.period_bounds("daily", ref), (date(2026, 11, 3), date(2026, 11, 3)))
        self.assertEqual(metrics.period_bounds("weekly", ref), (date(2026, 10, 26), date(2026, 11, 1)))
        self.assertEqual(metrics.period_bounds("monthly", ref), (date(2026, 10, 1), date(2026, 10, 31)))

    def test_counts_are_from_real_records(self):
        now = timezone.now()
        OutboundEmail.objects.create(company=self.company, contact=self.contact, to_email=self.contact.email, kind="initial",
                                     subject="s", body_text="b", status="sent", sent_at=now, account=self.account)
        OutboundEmail.objects.create(company=self.company, to_email="x@y.example", kind="initial", subject="s", body_text="b",
                                     status="sent", sent_at=now, bounced_at=now, account=self.account)
        InboundEmail.objects.create(account=self.account, provider_message_id="1", from_email=self.contact.email, company=self.company,
                                    classification="interested", received_at=now)
        InboundEmail.objects.create(account=self.account, provider_message_id="2", from_email="mailer-daemon@x.example",
                                    classification="bounce", received_at=now)
        self.company.expected_order_value = 12000
        self.company.stage = Stage.INTERESTED
        self.company.save()
        today = timezone.localdate()
        m = metrics.compute(today, today)
        self.assertEqual(m["emails_sent"], 2)
        self.assertEqual(m["emails_bounced"], 1)
        self.assertEqual(m["emails_not_bounced"], 1)
        self.assertEqual(m["replies_received"], 1, "bounces are not replies")
        self.assertEqual(m["interested"], 1)
        self.assertEqual(m["open_pipeline_value"], {"USD": 12000.0})
        self.assertEqual(m["by_country"][0]["country"], "Kenya")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_report_snapshot_exports_and_email(self):
        s = SystemSettings.get_solo()
        s.report_recipients = "owner@flotec.example"
        s.save()
        snap = generate_report("daily", narrative=False)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].attachments[0][0], f"report-daily-{snap.start}.pdf")
        again = generate_report("daily", narrative=False, email=False)
        self.assertEqual(snap.pk, again.pk, "one snapshot per period")
        from apps.reports.exports import report_to_pdf

        self.assertTrue(report_to_pdf(snap).startswith(b"%PDF"))


@override_settings(ANTHROPIC_API_KEY="test-key")
class DraftingAndNightlyTests(TestCase):
    def setUp(self):
        seed_reference_data()
        seed_templates()
        configure(ExecutionMode.DRAFT_AND_APPROVE)
        self.company, self.contact = make_company()

    def test_numbers_not_in_facts_are_flagged(self):
        self.assertEqual(grounding_flags("We have 25 years of experience", '{"facts": "established 2001"}'), ["unsupported_number"])
        self.assertEqual(grounding_flags("Sizes 20 mm to 110 mm", '{"sizes": "20 mm - 110 mm"}'), [])

    def test_prepare_drafts_requires_complete_profile(self):
        p = CompanyProfile.get_solo()
        p.about = ""
        p.save()
        self.assertIn("skipped", prepare_initial_drafts())

    def test_prepare_drafts_creates_one_personalised_draft_per_company(self):
        client = FakeClient([json_response(INTRO)])
        with mock.patch("apps.agents.llm.get_client", return_value=client):
            res = prepare_initial_drafts(limit=5)
            again = prepare_initial_drafts(limit=5)
        self.assertEqual(res["drafted"], 1)
        self.assertEqual(again["drafted"], 0, "never a second introduction to the same company")
        email = OutboundEmail.objects.get()
        self.assertEqual(email.status, OutboundEmail.Status.PENDING_APPROVAL)
        self.assertEqual(email.template.kind, "distributor", "category C distributor -> distributor template")
        self.assertIn("Sales Team", email.body_text, "signature added by code")
        prompt = client.messages.calls[0]["messages"][0]["content"]
        self.assertIn("Delta Agro", prompt)
        self.assertIn("Manufacturer of PVC and PP ball valves", prompt, "approved company facts are the grounding")

    @override_settings(OPENROUTER_API_KEY="or-test-key")
    def test_prepare_drafts_via_openrouter(self):
        import json as _json

        sys = SystemSettings.get_solo()
        sys.ai_provider_drafting = "openrouter"
        sys.save()
        body = {"id": "gen-1", "model": "vendor/free-model:free",
                "choices": [{"finish_reason": "stop", "message": {"content": _json.dumps(INTRO)}}],
                "usage": {"prompt_tokens": 1500, "completion_tokens": 200, "cost": 0}}
        response = mock.Mock(status_code=200, headers={}, json=mock.Mock(return_value=body))
        with mock.patch("apps.agents.llm.requests.post", return_value=response) as post, \
                mock.patch("apps.agents.llm.get_client", side_effect=AssertionError("Anthropic must not be called")):
            res = prepare_initial_drafts(limit=5)
        self.assertEqual(res["drafted"], 1)
        email = OutboundEmail.objects.get()
        self.assertEqual(email.status, OutboundEmail.Status.PENDING_APPROVAL, "same approval rules for every provider")
        self.assertIn("Sales Team", email.body_text, "signature added by code")
        prompt = post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("Manufacturer of PVC and PP ball valves", prompt, "approved company facts are the grounding")
        run = AgentRun.objects.get(action="draft.initial")
        self.assertEqual(run.model, "vendor/free-model:free")

    def test_prepare_drafts_stops_when_credits_run_out(self):
        from apps.agents.llm import AICreditsExhausted

        with mock.patch("apps.outreach.drafting.draft_initial", side_effect=AICreditsExhausted("no credits")) as draft, \
                mock.patch("apps.outreach.drafting.eligible_for_intro", return_value=[self.company] * 4):
            res = prepare_initial_drafts(limit=5)
        self.assertEqual(draft.call_count, 1, "a provider-wide problem stops the batch")
        self.assertEqual(res["drafted"], 0)

    def test_nightly_pipeline_runs_all_steps_and_logs(self):
        client = FakeClient([json_response(INTRO)])
        with mock.patch("apps.agents.llm.get_client", return_value=client):
            results = run_nightly()
        self.assertIn("no priority countries", str(results["1-7 discovery & verification"]))
        self.assertEqual(results["8-9 drafts & policy"]["drafted"], 1)
        self.assertIn("report", results["11 daily report"])
        self.assertTrue(AgentRun.objects.filter(action="pipeline.nightly", status="succeeded").exists())

    def test_nightly_in_mode1_prepares_no_drafts(self):
        configure(ExecutionMode.RESEARCH_ONLY)
        with mock.patch("apps.agents.llm.get_client", return_value=FakeClient([])):
            results = run_nightly()
        self.assertIn("Mode 1", str(results["8-9 drafts & policy"]))
        self.assertFalse(OutboundEmail.objects.exists())


class BackupAndHygieneTests(TestCase):
    def test_backup_command_writes_and_rotates(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(BACKUP_DIR=tmp, BACKUP_KEEP=2):
            for _ in range(3):
                call_command("backup", stdout=open(os.devnull, "w"))
            files = sorted(os.listdir(tmp))
            self.assertTrue(any(f.startswith("db-") for f in files))
            self.assertTrue(any(f.startswith("media-") for f in files))
            self.assertLessEqual(sum(f.startswith("db-") for f in files), 2)

    def test_crm_hygiene_creates_tasks_once(self):
        from apps.crm.jobs import crm_hygiene
        from apps.crm.models import Task

        seed_reference_data()
        company, _ = make_company()
        company.next_action, company.next_action_date = "Call buyer", timezone.localdate() - timedelta(days=2)
        company.save()
        self.assertEqual(crm_hygiene(None)["tasks_created"], 1)
        self.assertEqual(crm_hygiene(None)["tasks_created"], 0)
        self.assertEqual(Task.objects.count(), 1)
