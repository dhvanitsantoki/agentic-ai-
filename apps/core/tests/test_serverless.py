"""Serverless (Vercel) support: database file storage, protected media, cron endpoint, build helpers."""
import os
import tomllib
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.agents.models import Job, ScheduledTask
from apps.agents.registry import HANDLERS
from apps.core.models import AutomationState, Role, StoredFile, SystemSettings
from apps.core.storage import DatabaseStorage
from apps.core.testing import make_user

DB_STORAGE = {**settings.STORAGES, "default": {"BACKEND": "apps.core.storage.DatabaseStorage"}}


@override_settings(STORAGES=DB_STORAGE)
class DatabaseStorageTests(TestCase):
    def test_save_open_size_listdir_delete(self):
        storage = DatabaseStorage()
        name = storage.save("products/2026/valve.jpg", ContentFile(b"\xff\xd8jpeg-bytes"))
        self.assertEqual(name, "products/2026/valve.jpg")
        self.assertTrue(storage.exists(name))
        self.assertEqual(storage.size(name), 12)
        with storage.open(name) as fh:
            self.assertEqual(fh.read(), b"\xff\xd8jpeg-bytes")
        self.assertEqual(storage.url(name), "/media/products/2026/valve.jpg")
        self.assertEqual(storage.listdir("products"), (["2026"], []))
        second = storage.save("products/2026/valve.jpg", ContentFile(b"other"))
        self.assertNotEqual(second, name, "an existing name is never overwritten")
        storage.delete(name)
        self.assertFalse(storage.exists(name))
        with self.assertRaises(FileNotFoundError):
            storage.open(name)

    def test_unsafe_names_rejected(self):
        from django.core.exceptions import SuspiciousFileOperation

        with self.assertRaises((ValueError, SuspiciousFileOperation)):
            DatabaseStorage().save("../../etc/passwd", ContentFile(b"x"))

    def test_model_file_fields_use_it(self):
        from apps.products.models import CompanyDocument

        doc = CompanyDocument(title="Catalogue")
        doc.file.save("catalogue.pdf", ContentFile(b"%PDF-1.4 test"), save=True)
        self.assertTrue(StoredFile.objects.filter(name=doc.file.name).exists())
        doc.refresh_from_db()
        with doc.file.open("rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.4 test")


@override_settings(STORAGES=DB_STORAGE)
class MediaViewTests(TestCase):
    def setUp(self):
        default_storage.save("documents/price.pdf", ContentFile(b"%PDF-1.4 internal"))
        default_storage.save("documents/page.html", ContentFile(b"<script>alert(1)</script>"))

    def test_only_signed_in_users(self):
        resp = self.client.get("/media/documents/price.pdf")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])
        self.client.force_login(make_user("viewer", Role.VIEWER))
        resp = self.client.get("/media/documents/price.pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"".join(resp.streaming_content), b"%PDF-1.4 internal")
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertEqual(self.client.get("/media/documents/missing.pdf").status_code, 404)
        self.assertEqual(self.client.get("/media/../settings.py").status_code, 404)

    def test_active_content_is_downloaded_not_rendered(self):
        self.client.force_login(make_user("viewer", Role.VIEWER))
        resp = self.client.get("/media/documents/page.html")
        self.assertTrue(resp["Content-Disposition"].startswith("attachment"))

    @override_settings(SERVERLESS=True, SERVERLESS_MAX_BODY_BYTES=10)
    def test_large_files_explained_on_serverless(self):
        self.client.force_login(make_user("viewer", Role.VIEWER))
        resp = self.client.get("/media/documents/price.pdf")
        self.assertEqual(resp.status_code, 413)
        self.assertContains(resp, "too large", status_code=413)


@override_settings(CRON_SECRET="s3cret-value", CRON_TICK_BUDGET_SECONDS=30, CRON_MAX_RUNNING_JOBS=2)
class CronTickTests(TestCase):
    def setUp(self):
        sys = SystemSettings.get_solo()
        sys.automation_state = AutomationState.RUNNING
        sys.require_two_factor = True
        sys.save()
        self.calls = []

        def handler(job):
            self.calls.append(job.pk)
            return {"ok": True}

        patcher = mock.patch.dict(HANDLERS, {"test.noop": handler})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tick(self, auth="Bearer s3cret-value"):
        headers = {"HTTP_AUTHORIZATION": auth} if auth else {}
        return self.client.get("/cron/tick/", **headers)

    def test_requires_the_cron_secret(self):
        Job.objects.create(kind="test.noop")
        self.assertEqual(self.tick(auth=None).status_code, 401)
        self.assertEqual(self.tick(auth="Bearer wrong").status_code, 401)
        with override_settings(CRON_SECRET=""):
            self.assertEqual(self.tick().status_code, 503)
        self.assertEqual(self.calls, [], "nothing runs without the secret")

    def test_runs_queued_jobs_and_due_schedules(self):
        job = Job.objects.create(kind="test.noop")
        ScheduledTask.objects.create(name="t", job_kind="test.noop", cron="* * * * *",
                                     next_run_at=timezone.now() - timedelta(minutes=1))
        resp = self.tick()
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["scheduled"], 1)
        self.assertEqual(data["processed"], 2, "the queued job and the scheduled one")
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.DONE)

    def test_respects_running_job_limit_and_stop(self):
        Job.objects.create(kind="test.noop", status=Job.Status.RUNNING, locked_at=timezone.now())
        Job.objects.create(kind="test.noop", status=Job.Status.RUNNING, locked_at=timezone.now())
        Job.objects.create(kind="test.noop")
        data = self.tick().json()
        self.assertTrue(data["busy"])
        self.assertEqual(self.calls, [])
        sys = SystemSettings.get_solo()
        sys.automation_state = AutomationState.STOPPED
        sys.save()
        Job.objects.filter(status=Job.Status.RUNNING).delete()
        self.assertEqual(self.tick().json()["processed"], 0)
        self.assertEqual(self.calls, [])

    @override_settings(JOB_STALE_MINUTES=20)
    def test_interrupted_jobs_are_recovered(self):
        stale = Job.objects.create(kind="test.noop", status=Job.Status.RUNNING,
                                   locked_at=timezone.now() - timedelta(minutes=30), attempts=1)
        data = self.tick().json()
        self.assertEqual(data["recovered"], 1)
        stale.refresh_from_db()
        self.assertEqual(stale.status, Job.Status.DONE)


@override_settings(SERVERLESS=True)
class ServerlessPipelineTests(TestCase):
    def test_nightly_queues_research_runs_and_a_finish_job(self):
        from apps.agents.pipeline import run_nightly
        from apps.core.testing import seed_reference_data
        from apps.geo.models import Country

        seed_reference_data()
        Country.objects.filter(iso2__in=["KE", "AE"]).update(is_priority=True)
        with override_settings(ANTHROPIC_API_KEY="k"), \
                mock.patch("apps.research.pipeline.execute_run", side_effect=AssertionError("must not run inline")):
            run_nightly()
        research = Job.objects.filter(kind="research.run")
        self.assertEqual(research.count(), 2)
        finish = Job.objects.get(kind="pipeline.nightly_finish")
        self.assertGreater(finish.run_after, timezone.now() + timedelta(minutes=30))

    def test_backup_skipped_and_not_scheduled(self):
        from apps.agents.jobs import backup
        from apps.agents.seeds import seed_schedule

        self.assertIn("skipped", backup(None))
        seed_schedule()
        self.assertFalse(ScheduledTask.objects.get(job_kind="system.backup").enabled)


class BuildHelperTests(TestCase):
    def test_ensure_owner(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        with mock.patch.dict(os.environ, {"OWNER_INITIAL_PASSWORD": ""}):
            call_command("ensure_owner", stdout=open(os.devnull, "w"))
        self.assertFalse(User.objects.exists(), "no password, no account")
        with mock.patch.dict(os.environ, {"OWNER_INITIAL_PASSWORD": "short"}), self.assertRaises(CommandError):
            call_command("ensure_owner", stdout=open(os.devnull, "w"))
        with mock.patch.dict(os.environ, {"OWNER_INITIAL_PASSWORD": "Vq8-long-unique-pass", "OWNER_USERNAME": "ajay"}):
            call_command("ensure_owner", stdout=open(os.devnull, "w"))
            call_command("ensure_owner", stdout=open(os.devnull, "w"))
        user = User.objects.get()
        self.assertEqual(user.username, "ajay")
        self.assertTrue(user.is_superuser)
        self.assertEqual(user.profile.role, Role.ADMIN)
        self.assertTrue(user.check_password("Vq8-long-unique-pass"))

    def test_vercel_build_without_database_only_reports(self):
        from io import StringIO

        out = StringIO()
        with mock.patch.dict(os.environ, {"DATABASE_URL": ""}), \
                mock.patch("apps.core.management.commands.vercel_build.call_command") as cmd:
            call_command("vercel_build", stdout=out)
        cmd.assert_not_called()
        self.assertIn("DATABASE_URL is not set", out.getvalue())

    def test_pyproject_dependencies_match_requirements(self):
        root = Path(settings.BASE_DIR)
        reqs = [line.strip() for line in (root / "requirements.txt").read_text().splitlines()
                if line.strip() and not line.startswith("#")]
        deps = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
        self.assertEqual(deps, reqs)


class AiTestButtonTests(TestCase):
    def test_admin_only_and_reports_result(self):
        self.client.force_login(make_user("mgr", Role.MANAGER))
        self.assertEqual(self.client.post("/agents/ai-test/").status_code, 403)
        self.client.force_login(make_user("boss", Role.ADMIN))
        out = {"ok": True, "verdict": "PASS", "provider": "openrouter", "models": "openrouter/free", "key": {"is_free_tier": True},
               "model": "vendor/m:free", "tokens_in": 10, "tokens_out": 5, "cost": "0", "seconds": 1.0, "requests": 1,
               "result": {"classification": "need_catalogue", "wants_catalogue": True, "summary": "x"}}
        with mock.patch("apps.agents.selftest.run_selftest", return_value=out) as run:
            resp = self.client.post("/agents/ai-test/", {"provider": "openrouter"}, follow=True)
        run.assert_called_once_with("openrouter")
        self.assertContains(resp, "PASS")
        self.assertContains(resp, "vendor/m:free")
