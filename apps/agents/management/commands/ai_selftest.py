"""Live check of the AI provider: one small structured request with a clearly synthetic test e-mail.

    python manage.py ai_selftest                       # provider chosen in System settings for classification
    python manage.py ai_selftest --provider openrouter # force a provider for this test only (settings unchanged)
    python manage.py ai_selftest --provider anthropic --model claude-haiku-5-5

Uses one request (free with openrouter/free; a fraction of a cent on Claude Haiku). The call is
recorded as a "System" agent run, so it shows in the Agent log and counts towards the AI budget.
"""
import json
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.agents.llm import (
    ANTHROPIC, OPENROUTER, AIError, agent_run, openrouter_models, openrouter_structured_call, provider_key,
    structured_call, task_providers,
)
from apps.agents.models import AgentName
from apps.core.models import SystemSettings

TEST_EMAIL = (
    "SYSTEM SELF-TEST - synthetic message, not from a real buyer.\n\n"
    "Subject: Catalogue request\n\n"
    "Hello, we are interested in your PVC ball valves. Please send us your product catalogue.\n"
)
SYSTEM = (
    "You classify inbound sales e-mails for a valve manufacturer. Answer only from the e-mail text."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {"type": "string", "enum": ["interested", "need_catalogue", "need_quotation", "not_interested", "other"]},
        "wants_catalogue": {"type": "boolean"},
        "summary": {"type": "string"},
    },
    "required": ["classification", "wants_catalogue", "summary"],
    "additionalProperties": False,
}


def openrouter_key_info() -> dict:
    """Limits of the OpenRouter key (no secrets are returned or printed)."""
    headers = {"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"}
    for path in ("/key", "/auth/key"):
        try:
            resp = requests.get(f"{settings.OPENROUTER_BASE_URL}{path}", headers=headers, timeout=(10, 30))
        except requests.RequestException as exc:
            return {"error": f"could not reach OpenRouter: {exc.__class__.__name__}"}
        if resp.status_code == 404:
            continue
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}: {(body.get('error') or {}).get('message', '')}"[:300]}
        data = body.get("data") or {}
        keep = ("label", "is_free_tier", "limit", "limit_remaining", "usage", "usage_daily", "rate_limit")
        return {k: data[k] for k in keep if k in data}
    return {"error": "key information endpoint not found"}


class Command(BaseCommand):
    help = "Make one live AI request (synthetic test e-mail) to check the API key, model and limits."

    def add_arguments(self, parser):
        parser.add_argument("--provider", choices=[ANTHROPIC, OPENROUTER],
                            help="Provider to test (default: the one chosen for classification in System settings).")
        parser.add_argument("--model", help="Claude model for an Anthropic test (default: the classification model).")

    def handle(self, *args, **opts):
        sys = SystemSettings.get_solo()
        provider = opts["provider"] or task_providers(sys)["classification"]
        if not provider_key(provider):
            var = "OPENROUTER_API_KEY" if provider == OPENROUTER else "ANTHROPIC_API_KEY"
            raise CommandError(f"{var} is not set (environment variable or .env).")
        self.stdout.write(f"Provider: {provider}")
        if provider == OPENROUTER:
            self.stdout.write(f"Models:   {', '.join(openrouter_models(sys))}")
            self.stdout.write(f"Key:      {json.dumps(openrouter_key_info(), default=str)}")
        else:
            self.stdout.write(f"Model:    {opts['model'] or sys.ai_model_classification}")

        started = time.monotonic()
        try:
            with agent_run(AgentName.SYSTEM, "ai.selftest") as tracker:
                if provider == OPENROUTER:
                    result = openrouter_structured_call(tracker, system=SYSTEM, user_content=TEST_EMAIL, schema=SCHEMA,
                                                        max_tokens=1500)
                else:
                    result = structured_call(tracker, model=opts["model"] or sys.ai_model_classification, effort="low",
                                             system=SYSTEM, user_content=TEST_EMAIL, schema=SCHEMA, max_tokens=1500)
        except AIError as exc:
            raise CommandError(f"FAILED ({exc.__class__.__name__}): {exc}") from exc
        run = tracker.run
        run.refresh_from_db()
        self.stdout.write(f"Answered by: {run.model}  ·  {run.input_tokens} in / {run.output_tokens} out tokens  ·  "
                          f"US$ {run.cost_usd}  ·  {time.monotonic() - started:.1f} s  ·  {run.llm_calls} request(s)")
        self.stdout.write(f"Result:   {json.dumps(result, ensure_ascii=False)}")
        if result["wants_catalogue"] and result["classification"] in ("need_catalogue", "interested"):
            self.stdout.write(self.style.SUCCESS("PASS - the AI returned valid structured output and understood the test e-mail."))
        else:
            self.stdout.write(self.style.WARNING("WARNING - valid format, but the model misread the test e-mail "
                                                 "(it asks for a catalogue). Consider another model."))
