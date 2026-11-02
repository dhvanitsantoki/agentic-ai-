"""One live AI request with a clearly synthetic test e-mail - used by `manage.py ai_selftest` and the
"Test AI" button on the Automation page. Recorded as a System agent run (counts towards the budget)."""
import json
import time

import requests
from django.conf import settings

from .llm import (
    ANTHROPIC, OPENROUTER, AIError, agent_run, openrouter_models, openrouter_structured_call, provider_key,
    structured_call, task_providers,
)
from .models import AgentName

TEST_EMAIL = (
    "SYSTEM SELF-TEST - synthetic message, not from a real buyer.\n\n"
    "Subject: Catalogue request\n\n"
    "Hello, we are interested in your PVC ball valves. Please send us your product catalogue.\n"
)
SYSTEM = "You classify inbound sales e-mails for a valve manufacturer. Answer only from the e-mail text."
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
    """Limits of the OpenRouter key (no secrets are returned)."""
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


def run_selftest(provider: str | None = None, model: str | None = None) -> dict:
    """Returns {"ok", "verdict", "provider", "model", "tokens_in", "tokens_out", "cost", "seconds", "result", "key", "error"}."""
    from apps.core.models import SystemSettings

    sys = SystemSettings.get_solo()
    provider = provider or task_providers(sys)["classification"]
    out = {"ok": False, "provider": provider, "verdict": "FAILED", "error": "", "key": None}
    if not provider_key(provider):
        out["error"] = ("OPENROUTER_API_KEY" if provider == OPENROUTER else "ANTHROPIC_API_KEY") + " is not set."
        return out
    if provider == OPENROUTER:
        out["models"] = ", ".join(openrouter_models(sys))
        out["key"] = openrouter_key_info()
    claude_model = model or sys.ai_model_classification
    started = time.monotonic()
    tracker = None
    try:
        with agent_run(AgentName.SYSTEM, "ai.selftest") as tracker:
            if provider == OPENROUTER:
                result = openrouter_structured_call(tracker, system=SYSTEM, user_content=TEST_EMAIL, schema=SCHEMA,
                                                    max_tokens=1500)
            else:
                result = structured_call(tracker, model=claude_model, effort="low", system=SYSTEM,
                                         user_content=TEST_EMAIL, schema=SCHEMA, max_tokens=1500)
    except AIError as exc:
        out["error"] = f"{exc.__class__.__name__}: {exc}"
        return out
    finally:
        out["seconds"] = round(time.monotonic() - started, 1)
    run = tracker.run
    run.refresh_from_db()
    out.update(model=run.model, tokens_in=run.input_tokens, tokens_out=run.output_tokens, cost=str(run.cost_usd),
               requests=run.llm_calls, result=result, ok=True)
    understood = result["wants_catalogue"] and result["classification"] in ("need_catalogue", "interested")
    out["verdict"] = "PASS" if understood else "WARNING"
    return out


def describe(out: dict) -> list[str]:
    lines = [f"Provider: {out['provider']}" + (f" ({out['models']})" if out.get("models") else "")]
    if out.get("key"):
        lines.append(f"Key: {json.dumps(out['key'], default=str)}")
    if not out["ok"]:
        lines.append(f"FAILED - {out['error']}")
        return lines
    lines.append(f"Answered by: {out['model']} · {out['tokens_in']} in / {out['tokens_out']} out tokens · "
                 f"US$ {out['cost']} · {out['seconds']} s · {out['requests']} request(s)")
    lines.append(f"Result: {json.dumps(out['result'], ensure_ascii=False)}")
    lines.append("PASS - the AI returned valid structured output and understood the test e-mail." if out["verdict"] == "PASS"
                 else "WARNING - valid format, but the model misread the test e-mail (it asks for a catalogue). "
                      "Consider another model.")
    return lines
