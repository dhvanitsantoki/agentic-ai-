from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.agents import registry, scheduler, worker
from apps.agents.models import Job, ScheduledTask
from apps.agents.seeds import seed_schedule
from apps.core.models import AutomationState, Notification, SystemSettings
from apps.core.testing import make_user

CALLS = []


@registry.job_handler("test.ok")
def _ok(job):
    CALLS.append(job.pk)
    return {"ok": True}


@registry.job_handler("test.boom")
def _boom(job):
    raise RuntimeError("boom")


@registry.job_handler("test.halt")
def _halt(job):
    registry.ensure_not_stopped()
    return {}


def set_state(state):
    s = SystemSettings.get_solo()
    s.automation_state = state
    s.save()


class WorkerTests(TestCase):
    def setUp(self):
        CALLS.clear()
        self.user = make_user()

    def test_fresh_install_starts_paused(self):
        self.assertEqual(SystemSettings.get_solo().automation_state, AutomationState.PAUSED)

    def test_running_executes_all_jobs(self):
        set_state(AutomationState.RUNNING)
        a = registry.enqueue("test.ok")
        b = registry.enqueue("test.ok", user=self.user)
        self.assertEqual(worker.work_once("t"), 2)
        self.assertEqual(sorted(CALLS), sorted([a.pk, b.pk]))
        a.refresh_from_db()
        self.assertEqual(a.status, Job.Status.DONE)
        self.assertEqual(a.result, {"ok": True})

    def test_paused_runs_only_manual_jobs(self):
        set_state(AutomationState.PAUSED)
        auto = registry.enqueue("test.ok")
        manual = registry.enqueue("test.ok", user=self.user)
        worker.work_once("t")
        auto.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(auto.status, Job.Status.QUEUED)
        self.assertEqual(manual.status, Job.Status.DONE)

    def test_stopped_runs_nothing_and_cancels_queue(self):
        registry.enqueue("test.ok", user=self.user)
        set_state(AutomationState.STOPPED)
        self.assertEqual(worker.work_once("t"), 0)
        self.assertEqual(worker.cancel_queued_jobs(), 1)
        self.assertEqual(Job.objects.filter(status=Job.Status.CANCELLED).count(), 1)

    def test_handler_sees_stop_mid_run(self):
        set_state(AutomationState.RUNNING)
        job = registry.enqueue("test.halt")
        job = worker.claim_next_job("t", AutomationState.RUNNING)
        set_state(AutomationState.STOPPED)
        worker.run_job(job)
        self.assertEqual(job.status, Job.Status.CANCELLED)

    def test_retry_with_backoff_then_fail_and_notify(self):
        set_state(AutomationState.RUNNING)
        job = registry.enqueue("test.boom", max_attempts=2)
        worker.work_once("t")
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertGreater(job.run_after, timezone.now())
        Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
        worker.work_once("t")
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("boom", job.last_error)
        self.assertTrue(Notification.objects.filter(title__icontains="test.boom").exists())

    def test_stale_running_jobs_recovered(self):
        job = registry.enqueue("test.ok")
        Job.objects.filter(pk=job.pk).update(status=Job.Status.RUNNING, locked_at=timezone.now() - timezone.timedelta(hours=3))
        self.assertEqual(worker.recover_stale_jobs(), 1)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            registry.enqueue("does.not.exist")


class SchedulerTests(TestCase):
    def test_default_schedule_seeded_with_2am_pipeline(self):
        seed_schedule()
        nightly = ScheduledTask.objects.get(job_kind="pipeline.nightly")
        self.assertEqual(nightly.cron, "0 2 * * *")
        local = timezone.localtime(nightly.next_run_at)
        self.assertEqual((local.hour, local.minute), (2, 0))
        for task in ScheduledTask.objects.all():
            self.assertIn(task.job_kind, registry.HANDLERS, task.job_kind)

    def test_tick_enqueues_due_task_once(self):
        task = ScheduledTask.objects.create(name="t", job_kind="test.ok", cron="*/5 * * * *",
                                            next_run_at=timezone.now() - timezone.timedelta(minutes=1))
        jobs = scheduler.tick()
        self.assertEqual(len(jobs), 1)
        task.refresh_from_db()
        self.assertGreater(task.next_run_at, timezone.now())
        ScheduledTask.objects.filter(pk=task.pk).update(next_run_at=timezone.now() - timezone.timedelta(minutes=1))
        self.assertEqual(scheduler.tick(), [], "a pending job for the task blocks a second enqueue")

    def test_disabled_and_invalid(self):
        ScheduledTask.objects.create(name="off", job_kind="test.ok", cron="* * * * *", enabled=False,
                                     next_run_at=timezone.now() - timezone.timedelta(minutes=1))
        self.assertEqual(scheduler.tick(), [])
        self.assertFalse(scheduler.is_valid_cron("61 * * * *"))

    def test_scheduler_only_ticks_when_running(self):
        ScheduledTask.objects.create(name="t2", job_kind="test.ok", cron="* * * * *",
                                     next_run_at=timezone.now() - timezone.timedelta(minutes=1))
        set_state(AutomationState.PAUSED)
        worker.work_once("t")
        self.assertEqual(Job.objects.count(), 0)
        set_state(AutomationState.RUNNING)
        worker.work_once("t")
        self.assertEqual(Job.objects.filter(status=Job.Status.DONE).count(), 1)


class ControlViewTests(TestCase):
    def test_stop_button_cancels_queue_and_is_audited(self):
        admin = make_user("boss")
        self.client.force_login(admin)
        registry.enqueue("test.ok")
        resp = self.client.post("/agents/state/", {"state": "stopped"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(SystemSettings.get_solo().automation_state, AutomationState.STOPPED)
        self.assertEqual(Job.objects.filter(status=Job.Status.QUEUED).count(), 0)
        from apps.core.models import AuditLog

        self.assertTrue(AuditLog.objects.filter(action="automation.state").exists())

    def test_executive_cannot_change_state(self):
        from apps.core.models import Role

        ex = make_user("exec", Role.EXECUTIVE)
        self.client.force_login(ex)
        self.assertEqual(self.client.post("/agents/state/", {"state": "running"}).status_code, 403)
        self.assertEqual(self.client.post("/agents/mode/", {"mode": "3"}).status_code, 403)


class ApiErrorMappingTests(TestCase):
    def test_no_credits_is_permanent_and_notifies(self):
        import anthropic
        import httpx2
        from django.test import override_settings

        from apps.agents.llm import AICreditsExhausted, RunTracker, structured_call
        from apps.agents.models import AgentRun

        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        resp = httpx2.Response(400, request=req, json={"type": "error", "error": {"type": "invalid_request_error",
                               "message": "Your credit balance is too low to access the Anthropic API."}})
        err = anthropic.BadRequestError("Your credit balance is too low to access the Anthropic API.", response=resp,
                                        body=resp.json())

        class Boom:
            class beta:
                class messages:
                    @staticmethod
                    def create(**kw):
                        raise err

        tracker = RunTracker(AgentRun.objects.create(agent="outreach", action="t"))
        with override_settings(ANTHROPIC_API_KEY="k"), mock.patch("apps.agents.llm.get_client", return_value=Boom), \
                self.assertRaises(AICreditsExhausted) as ctx:
            structured_call(tracker, model="claude-opus-5-5", effort="low", system="s", user_content="u", schema={"type": "object"})
        self.assertTrue(ctx.exception.permanent, "worker must not retry")
        self.assertTrue(Notification.objects.filter(title__startswith="AI agents stopped").exists())
