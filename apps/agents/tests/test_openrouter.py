"""OpenRouter provider: routing, request shape, validation/repair, usage metering, error mapping.

OpenRouter's HTTP API is mocked; nothing here calls a real service.
"""
import datetime as dt
import json
from decimal import Decimal
from unittest import mock

import requests
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.agents import worker
from apps.agents.llm import (
    AICreditsExhausted, AIError, AIOutputError, AIQuotaReached, AIRefused, AIUnavailable, BudgetExceeded, RunTracker,
    ai_configured, ai_status, extract_json, structured_call,
)
from apps.agents.models import AgentRun, Job
from apps.agents.registry import HANDLERS
from apps.core.forms import SystemSettingsForm
from apps.core.models import Notification, SystemSettings

SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": ["interested", "not_interested"]},
                   "notes": {"type": "array", "items": {"type": "string"}}},
    "required": ["label", "notes"],
    "additionalProperties": False,
}
GOOD = {"label": "interested", "notes": ["asked for a catalogue"]}


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def completion(content, *, model="qwen/qwen3-free:free", finish="stop", cost=0, prompt_tokens=900, completion_tokens=120):
    return FakeResponse(200, {
        "id": "gen-1", "model": model,
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens,
                  "cost": cost, "prompt_tokens_details": {"cached_tokens": 0}},
    })


def error(status, message, headers=None):
    return FakeResponse(status, {"error": {"code": status, "message": message}}, headers=headers)


def use_openrouter(drafting=True, classification=True, models="openrouter/free"):
    sys = SystemSettings.get_solo()
    sys.ai_provider_drafting = "openrouter" if drafting else "anthropic"
    sys.ai_provider_classification = "openrouter" if classification else "anthropic"
    sys.openrouter_models = models
    sys.save()


@override_settings(OPENROUTER_API_KEY="or-test-key", ANTHROPIC_API_KEY="", SITE_URL="https://sales.example.test")
class OpenRouterCallTests(TestCase):
    def setUp(self):
        use_openrouter()
        self.tracker = RunTracker(AgentRun.objects.create(agent="mailbox", action="test"))

    def call(self, responses, task="classification"):
        with mock.patch("apps.agents.llm.requests.post", side_effect=responses) as post, \
                mock.patch("apps.agents.llm.get_client", side_effect=AssertionError("Anthropic must not be called")):
            result = structured_call(self.tracker, task=task, model="claude-haiku-5-5", effort="low", system="Classify.",
                                     user_content="Please send your catalogue.", schema=SCHEMA, max_tokens=3000)
        return result, post

    def test_routes_to_openrouter_with_schema_and_meters_usage(self):
        result, post = self.call([completion(json.dumps(GOOD), cost=0.00042)])
        self.assertEqual(result, GOOD)
        url = post.call_args.args[0]
        kwargs = post.call_args.kwargs
        self.assertEqual(url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer or-test-key")
        self.assertEqual(kwargs["headers"]["HTTP-Referer"], "https://sales.example.test")
        payload = kwargs["json"]
        self.assertEqual(payload["model"], "openrouter/free")
        self.assertNotIn("models", payload)
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], SCHEMA)
        self.assertTrue(payload["provider"]["require_parameters"], "only endpoints that support structured output")
        self.assertGreaterEqual(payload["max_tokens"], 6000)
        self.assertIn('"enum"', payload["messages"][0]["content"], "schema also given in the prompt")
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "Please send your catalogue."})
        run = self.tracker.run
        run.refresh_from_db()
        self.assertEqual((run.llm_calls, run.input_tokens, run.output_tokens), (1, 900, 120))
        self.assertEqual(run.cost_usd, Decimal("0.0004"))
        self.assertEqual(run.model, "qwen/qwen3-free:free", "the model that actually answered is recorded")

    def test_fallback_models_are_sent_as_a_list(self):
        use_openrouter(models="vendor/model-a:free, vendor/model-b:free")
        _, post = self.call([completion(json.dumps(GOOD))])
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["models"], ["vendor/model-a:free", "vendor/model-b:free"])
        self.assertNotIn("model", payload)

    def test_fenced_json_with_thinking_and_extra_keys_is_accepted(self):
        text = "<think>the buyer wants a catalogue {maybe}</think>\n```json\n" + json.dumps({**GOOD, "extra": 1}) + "\n```"
        result, _ = self.call([completion(text)])
        self.assertEqual(result, GOOD, "unknown keys are dropped, fences and <think> removed")

    def test_invalid_answer_gets_one_repair_attempt(self):
        result, post = self.call([completion('{"label": "maybe", "notes": []}'), completion(json.dumps(GOOD))])
        self.assertEqual(result, GOOD)
        self.assertEqual(post.call_count, 2)
        repair = post.call_args_list[1].kwargs["json"]["messages"]
        self.assertEqual(repair[-2]["role"], "assistant")
        self.assertIn("label", repair[-1]["content"], "the validation problem is explained to the model")
        self.tracker.run.refresh_from_db()
        self.assertEqual(self.tracker.run.llm_calls, 2)

    def test_still_invalid_after_repair_raises(self):
        with self.assertRaises(AIOutputError):
            self.call([completion("I think they are interested."), completion('{"label": "interested"}')])

    def test_truncated_answer_raises(self):
        with self.assertRaises(AIOutputError):
            self.call([completion('{"label": "inter', finish="length")])

    def test_content_filter_is_a_refusal(self):
        with self.assertRaises(AIRefused):
            self.call([completion("", finish="content_filter")])

    def test_rejected_key_is_permanent_and_notifies_once(self):
        for _ in range(2):
            with self.assertRaises(AIUnavailable) as ctx:
                self.call([error(401, "No auth credentials found")])
        self.assertTrue(ctx.exception.permanent)
        self.assertEqual(Notification.objects.filter(title__startswith="AI agents stopped").count(), 1)

    def test_no_credits_is_permanent(self):
        with self.assertRaises(AICreditsExhausted) as ctx:
            self.call([error(402, "Insufficient credits")])
        self.assertTrue(ctx.exception.permanent)
        self.assertIn("openrouter/free", str(ctx.exception))

    def test_privacy_settings_blocking_free_models_is_explained(self):
        with self.assertRaises(AIUnavailable) as ctx:
            self.call([error(404, "No endpoints found matching your data policy (Free model publication)")])
        self.assertIn("openrouter.ai/settings/privacy", str(ctx.exception))

    def test_unknown_model_is_explained(self):
        with self.assertRaises(AIUnavailable) as ctx:
            self.call([error(404, "No endpoints found for vendor/gone:free.")])
        self.assertIn("System settings", str(ctx.exception))

    def test_daily_free_limit_waits_until_reset(self):
        reset = timezone.now() + dt.timedelta(hours=5)
        headers = {"X-RateLimit-Reset": str(int(reset.timestamp() * 1000))}
        with self.assertRaises(AIQuotaReached) as ctx:
            self.call([error(429, "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day", headers)])
        self.assertFalse(ctx.exception.permanent)
        self.assertAlmostEqual(ctx.exception.retry_at.timestamp(), reset.timestamp() + 60, delta=2)
        self.assertTrue(Notification.objects.filter(title="AI paused: OpenRouter daily limit reached").exists())

    def test_daily_limit_without_reset_header_waits_until_after_utc_midnight(self):
        with self.assertRaises(AIQuotaReached) as ctx:
            self.call([error(429, "Rate limit exceeded: free-models-per-day")])
        when = ctx.exception.retry_at.astimezone(dt.timezone.utc)
        self.assertEqual((when.hour, when.minute), (0, 5))
        self.assertGreater(when, timezone.now())

    def test_short_rate_limit_and_network_errors_are_retryable(self):
        with self.assertRaises(AIError) as ctx:
            self.call([error(429, "Rate limit exceeded: 20 requests per minute")])
        self.assertNotIsInstance(ctx.exception, AIQuotaReached)
        self.assertFalse(ctx.exception.permanent)
        with self.assertRaises(AIError) as ctx:
            self.call([requests.ConnectionError("down")])
        self.assertFalse(ctx.exception.permanent)
        with self.assertRaises(AIError):
            self.call([FakeResponse(502, None, text="<html>Bad gateway</html>")])

    def test_error_inside_a_200_response_is_raised(self):
        body = {"id": "gen-2", "choices": [{"error": {"code": 502, "message": "Provider returned error"}, "finish_reason": "error",
                                           "message": {"content": ""}}]}
        with self.assertRaises(AIError):
            self.call([FakeResponse(200, body)])

    def test_budget_cap_applies_before_calling(self):
        sys = SystemSettings.get_solo()
        sys.monthly_ai_budget_usd = Decimal("0")
        sys.save()
        with self.assertRaises(BudgetExceeded):
            self.call([])

    def test_missing_key_is_reported(self):
        with override_settings(OPENROUTER_API_KEY=""), self.assertRaises(AIUnavailable):
            self.call([])

    def test_anthropic_stays_in_use_when_not_selected(self):
        use_openrouter(drafting=False, classification=True)
        from apps.agents.testing import FakeClient, json_response

        client = FakeClient([json_response(GOOD)])
        with override_settings(ANTHROPIC_API_KEY="k"), mock.patch("apps.agents.llm.get_client", return_value=client), \
                mock.patch("apps.agents.llm.requests.post", side_effect=AssertionError("OpenRouter must not be called")):
            result = structured_call(self.tracker, task="drafting", model="claude-sonnet-5-5", effort="low", system="s",
                                     user_content="u", schema=SCHEMA)
        self.assertEqual(result, GOOD)
        self.assertEqual(client.messages.calls[0]["model"], "claude-sonnet-5-5")


class ProviderStatusTests(TestCase):
    @override_settings(OPENROUTER_API_KEY="or", ANTHROPIC_API_KEY="")
    def test_status_per_task(self):
        use_openrouter(drafting=True, classification=False)
        self.assertEqual(ai_status(), {"research": False, "drafting": True, "classification": False})
        self.assertTrue(ai_configured())
        self.assertTrue(ai_configured("drafting"))
        self.assertFalse(ai_configured("research"), "research always needs Anthropic")

    @override_settings(OPENROUTER_API_KEY="", ANTHROPIC_API_KEY="k")
    def test_default_is_anthropic_everywhere(self):
        self.assertEqual(ai_status(), {"research": True, "drafting": True, "classification": True})

    def test_extract_json_errors(self):
        with self.assertRaises(ValueError):
            extract_json("no json here")
        self.assertEqual(extract_json('Sure! {"a": 1} hope this helps'), {"a": 1})


class SettingsFormTests(TestCase):
    def form(self, **changes):
        sys = SystemSettings.get_solo()
        data = {name: getattr(sys, name) for name in SystemSettingsForm.Meta.fields}
        data["auto_send_kinds"] = sys.auto_send_kinds
        data.update(changes)
        data = {k: v for k, v in data.items() if v is not False}
        return SystemSettingsForm(data, instance=sys)

    def test_models_are_normalised(self):
        form = self.form(ai_provider_drafting="openrouter", openrouter_models=" vendor/a:free ,vendor/b ")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().openrouter_models, "vendor/a:free, vendor/b")

    def test_bad_model_ids_rejected(self):
        self.assertFalse(self.form(ai_provider_drafting="openrouter", openrouter_models="").is_valid())
        self.assertFalse(self.form(openrouter_models="gpt free").is_valid())


class QuotaWorkerTests(TestCase):
    def test_quota_wait_requeues_without_using_an_attempt(self):
        retry_at = timezone.now() + dt.timedelta(hours=3)

        def handler(job):
            raise AIQuotaReached("daily limit", retry_at)

        with mock.patch.dict(HANDLERS, {"test.quota": handler}):
            job = Job.objects.create(kind="test.quota", attempts=3, max_attempts=3, status=Job.Status.RUNNING)
            worker.run_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUEUED, "a quota wait is not a failure")
        self.assertEqual(job.run_after, retry_at)
        self.assertEqual(job.attempts, 2)


@override_settings(OPENROUTER_API_KEY="or-test-key", ANTHROPIC_API_KEY="")
class SelfTestCommandTests(TestCase):
    def run_command(self, *args, post=None, get=None):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with mock.patch("apps.agents.llm.requests.post", side_effect=post or []), \
                mock.patch("apps.agents.selftest.requests.get", side_effect=get or []):
            call_command("ai_selftest", *args, stdout=out)
        return out.getvalue()

    def test_openrouter_selftest_passes_and_shows_key_limits(self):
        answer = {"classification": "need_catalogue", "wants_catalogue": True, "summary": "Asks for the catalogue."}
        key = FakeResponse(200, {"data": {"label": "sk-or-v1-abc...", "is_free_tier": True, "limit": None, "usage": 0}})
        out = self.run_command("--provider", "openrouter", post=[completion(json.dumps(answer))], get=[key])
        self.assertIn("PASS", out)
        self.assertIn('"is_free_tier": true', out)
        self.assertNotIn("or-test-key", out, "the key itself is never printed")
        self.assertTrue(AgentRun.objects.filter(agent="system", action="ai.selftest", status="succeeded").exists())

    def test_failure_is_reported_as_command_error(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError) as ctx:
            self.run_command("--provider", "openrouter", post=[error(401, "No auth credentials found")],
                             get=[FakeResponse(401, {"error": {"message": "No auth credentials found"}})])
        self.assertIn("AIUnavailable", str(ctx.exception))

    def test_missing_key(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self.run_command("--provider", "anthropic")
