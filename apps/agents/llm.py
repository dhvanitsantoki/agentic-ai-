"""AI wrapper used by every agent (Anthropic Claude, optionally OpenRouter for some tasks).

* Meters tokens, web searches and cost on the current AgentRun.
* Enforces the monthly AI budget before every call.
* Uses structured outputs (JSON schema) for extraction/classification/drafting.
* Runs a manual tool loop for research (server-side web_search / web_fetch + a strict
  client-side `submit_*` tool that carries the structured result), handling pause_turn.
* Opts into server-side refusal fallbacks where the model supports them.
* Drafting and classification can run on OpenRouter instead (System settings -> AI provider):
  JSON-schema output, validated locally, one repair attempt, cost from OpenRouter's usage report.
  Research always runs on Anthropic because it depends on Anthropic's server-side web tools.
"""
import datetime as dt
import json
import logging
import re
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

import anthropic
import requests
from django.conf import settings
from jsonschema import Draft202012Validator
from django.db.models import Sum
from django.utils import timezone

from .models import AgentRun
from .registry import ensure_not_stopped

log = logging.getLogger(__name__)

# USD per million tokens: (input, output, cache read, cache write)
PRICING = {
    "claude-opus-5-5": (Decimal("4"), Decimal("20"), Decimal("0.20"), Decimal("5")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10"), Decimal("0.20"), Decimal("2.5")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50"), Decimal("0.01"), Decimal("0.125")),
    "claude-opus-5": (Decimal("5"), Decimal("25"), Decimal("0.50"), Decimal("6.25")),
    "claude-opus-4-8": (Decimal("5"), Decimal("25"), Decimal("0.50"), Decimal("6.25")),
}
WEB_SEARCH_USD = Decimal("0.01")  # $10 per 1,000 searches
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
MILLION = Decimal(1_000_000)


class AIError(Exception):
    permanent = False


class AIUnavailable(AIError):
    permanent = True


class BudgetExceeded(AIError):
    permanent = True


class AIRefused(AIError):
    permanent = True


class AIOutputError(AIError):
    pass


class AICreditsExhausted(AIUnavailable):
    """The provider account has no prepaid credits left (never retried)."""


class AIQuotaReached(AIError):
    """The provider's request quota is used up (e.g. OpenRouter's free daily limit).

    The worker re-queues the job for `retry_at` without counting it as a failed attempt.
    """

    def __init__(self, message: str, retry_at: dt.datetime):
        super().__init__(message)
        self.retry_at = retry_at


def _translate_api_error(exc: Exception):
    """Map SDK exceptions to agent errors; permanent problems are not retried by the worker."""
    if isinstance(exc, anthropic.AuthenticationError):
        return AIUnavailable("The Anthropic API key was rejected.")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return AIUnavailable(f"The API key lacks permission for this request: {exc.message}")
    if isinstance(exc, anthropic.BadRequestError):
        message = str(getattr(exc, "message", exc))
        if "credit balance" in message.lower():
            return AICreditsExhausted("Anthropic account has no credits left - add credits at console.anthropic.com > Plans & Billing.")
        return AIOutputError(f"Request rejected by the API: {message}")
    if isinstance(exc, anthropic.RateLimitError):
        return AIError("Rate limited by the API - the job will retry later.")
    if isinstance(exc, anthropic.APIStatusError):
        return AIError(f"API error {exc.status_code}")
    if isinstance(exc, anthropic.APIConnectionError):
        return AIError("Could not reach the Anthropic API.")
    return exc


def _notify_unavailable(exc: AIError):
    from apps.core.audit import notify
    from apps.core.models import Notification

    title = "AI agents stopped: " + str(exc)[:150]
    if not Notification.objects.filter(title=title, is_read=False).exists():
        notify(title, body=str(exc), url="/agents/", level="danger")


ANTHROPIC, OPENROUTER = "anthropic", "openrouter"
TASKS = ("research", "drafting", "classification")


def task_providers(sys=None) -> dict:
    """Which provider serves each task. Research is always Anthropic (server-side web search/fetch)."""
    if sys is None:
        from apps.core.models import SystemSettings

        sys = SystemSettings.get_solo()
    return {
        "research": ANTHROPIC,
        "drafting": sys.ai_provider_drafting or ANTHROPIC,
        "classification": sys.ai_provider_classification or ANTHROPIC,
    }


def provider_key(provider: str) -> str:
    return settings.OPENROUTER_API_KEY if provider == OPENROUTER else settings.ANTHROPIC_API_KEY


def ai_status(sys=None) -> dict:
    """{task: True/False} - whether the provider chosen for each task has an API key."""
    return {task: bool(provider_key(p)) for task, p in task_providers(sys).items()}


def ai_configured(task: str | None = None) -> bool:
    """True if `task` can call its AI provider; without a task: if at least one task can."""
    status = ai_status()
    return status[task] if task else any(status.values())


def not_configured_message(task: str) -> str:
    provider = task_providers()[task]
    if task == "research":
        return "Research needs the Anthropic API (ANTHROPIC_API_KEY): it uses Anthropic's web search and fetch tools."
    var = "OPENROUTER_API_KEY" if provider == OPENROUTER else "ANTHROPIC_API_KEY"
    return f"AI is not configured for {task}: set {var} in .env (or choose another provider in System settings)."


_client = None


def get_client():
    global _client
    if not ai_configured():
        raise AIUnavailable("ANTHROPIC_API_KEY is not set - AI agents are disabled.")
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=3, timeout=600.0)
    return _client


def month_spend() -> Decimal:
    start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return AgentRun.objects.filter(started_at__gte=start).aggregate(s=Sum("cost_usd"))["s"] or Decimal("0")


def check_budget():
    from apps.core.models import SystemSettings

    budget = SystemSettings.get_solo().monthly_ai_budget_usd
    spent = month_spend()
    if spent >= budget:
        raise BudgetExceeded(f"Monthly AI budget reached (US$ {spent:.2f} of {budget:.2f}). Raise it in Settings to continue.")


def _usage_numbers(usage) -> dict:
    def g(obj, name):
        return int(getattr(obj, name, 0) or 0) if obj is not None else 0

    stu = getattr(usage, "server_tool_use", None)
    return {
        "input": g(usage, "input_tokens"),
        "output": g(usage, "output_tokens"),
        "cache_read": g(usage, "cache_read_input_tokens"),
        "cache_write": g(usage, "cache_creation_input_tokens"),
        "web_searches": g(stu, "web_search_requests"),
    }


def compute_cost(model: str, u: dict) -> Decimal:
    p_in, p_out, p_read, p_write = PRICING.get(model, PRICING["claude-opus-5-5"])
    tokens = (
        Decimal(u["input"]) * p_in + Decimal(u["output"]) * p_out
        + Decimal(u["cache_read"]) * p_read + Decimal(u["cache_write"]) * p_write
    ) / MILLION
    return (tokens + Decimal(u["web_searches"]) * WEB_SEARCH_USD).quantize(Decimal("0.0001"))


class RunTracker:
    def __init__(self, run: AgentRun):
        self.run = run
        self.skipped_reason = ""

    def record(self, response, requested_model: str):
        u = _usage_numbers(getattr(response, "usage", None))
        model = getattr(response, "model", None) or requested_model
        r = self.run
        r.llm_calls += 1
        r.input_tokens += u["input"]
        r.output_tokens += u["output"]
        r.cache_read_tokens += u["cache_read"]
        r.cache_write_tokens += u["cache_write"]
        r.web_searches += u["web_searches"]
        r.cost_usd += compute_cost(model, u)
        r.model = model
        r.save(update_fields=["llm_calls", "input_tokens", "output_tokens", "cache_read_tokens",
                              "cache_write_tokens", "web_searches", "cost_usd", "model"])

    def record_usage(self, *, model: str, input_tokens: int, output_tokens: int, cache_read: int = 0,
                     cost: Decimal = Decimal("0")):
        """Record a call whose cost the provider reported directly (OpenRouter)."""
        r = self.run
        r.llm_calls += 1
        r.input_tokens += input_tokens
        r.output_tokens += output_tokens
        r.cache_read_tokens += cache_read
        r.cost_usd += cost.quantize(Decimal("0.0001"))
        r.model = (model or "")[:60]
        r.save(update_fields=["llm_calls", "input_tokens", "output_tokens", "cache_read_tokens", "cost_usd", "model"])

    def skip(self, reason: str):
        self.skipped_reason = reason


@contextmanager
def agent_run(agent: str, action: str, obj=None, job=None):
    run = AgentRun.objects.create(
        agent=agent, action=action, job=job,
        object_type=obj._meta.label if obj is not None else "",
        object_id=str(obj.pk) if obj is not None else "",
    )
    tracker = RunTracker(run)
    try:
        yield tracker
        run.status = AgentRun.Status.SKIPPED if tracker.skipped_reason else AgentRun.Status.SUCCEEDED
        if tracker.skipped_reason:
            run.summary = (run.summary + "\n" if run.summary else "") + f"Skipped: {tracker.skipped_reason}"
    except Exception as exc:
        run.status = AgentRun.Status.FAILED
        run.error = f"{exc.__class__.__name__}: {exc}"[:4000]
        raise
    finally:
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "summary", "error", "finished_at"])


def _request_kwargs(model: str, effort: str) -> dict:
    kwargs = {"model": model, "output_config": {"effort": effort}}
    if model in FALLBACK_MODELS:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    return kwargs


def _check_stop(response):
    reason = getattr(response, "stop_reason", None)
    if reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise AIRefused(f"The model declined this request (category: {category or 'unspecified'}).")
    if reason == "max_tokens":
        raise AIOutputError("The model ran out of output tokens before finishing.")


def _system_blocks(system: str) -> list:
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def structured_call(tracker: RunTracker, *, model: str, effort: str, system: str, user_content, schema: dict,
                    max_tokens: int = 16000, task: str | None = None) -> dict:
    """Single request whose answer is JSON matching `schema` (structured outputs).

    `task` ("drafting"/"classification") selects the provider configured in System settings;
    `model` is the Claude model used when that provider is Anthropic.
    """
    ensure_not_stopped()
    check_budget()
    if task and task_providers()[task] == OPENROUTER:
        return openrouter_structured_call(tracker, system=system, user_content=user_content, schema=schema,
                                          max_tokens=max_tokens)
    client = get_client()
    kwargs = _request_kwargs(model, effort)
    kwargs["output_config"]["format"] = {"type": "json_schema", "schema": schema}
    try:
        response = client.beta.messages.create(
            max_tokens=max_tokens,
            system=_system_blocks(system),
            messages=[{"role": "user", "content": user_content}],
            **kwargs,
        )
    except anthropic.APIError as exc:
        err = _translate_api_error(exc)
        if isinstance(err, AIUnavailable):
            _notify_unavailable(err)
        raise err from exc
    tracker.record(response, model)
    _check_stop(response)
    text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIOutputError("The model did not return valid JSON.") from exc


def collect_seen_urls(content) -> set[str]:
    """URLs the server-side tools actually returned/fetched in this response (evidence of real sources)."""
    seen = set()
    for block in content or []:
        btype = getattr(block, "type", "")
        if btype == "web_search_tool_result":
            results = getattr(block, "content", None)
            if isinstance(results, list):
                for r in results:
                    url = getattr(r, "url", None)
                    if url:
                        seen.add(url)
        elif btype == "web_fetch_tool_result":
            inner = getattr(block, "content", None)
            url = getattr(inner, "url", None)
            if url:
                seen.add(url)
        elif btype == "text":
            for c in getattr(block, "citations", None) or []:
                url = getattr(c, "url", None)
                if url:
                    seen.add(url)
    return seen


def research_call(tracker: RunTracker, *, model: str, effort: str, system: str, prompt: str, submit_tool: dict,
                  max_searches: int = 10, max_fetches: int = 10, max_turns: int = 8) -> tuple[dict, set[str]]:
    """Agentic research with Anthropic's web search/fetch tools.

    Returns (submitted_input, urls_seen_by_tools). The model must deliver its result by calling
    the strict client tool `submit_tool['name']`.
    """
    client = get_client()
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max_fetches},
        {**submit_tool, "strict": True},
    ]
    messages = [{"role": "user", "content": prompt}]
    seen: set[str] = set()
    nudged = False
    for _ in range(max_turns):
        ensure_not_stopped()
        check_budget()
        kwargs = _request_kwargs(model, effort)
        try:
            response = client.beta.messages.create(
                max_tokens=16000, system=_system_blocks(system), tools=tools, messages=messages, **kwargs
            )
        except anthropic.APIError as exc:
            err = _translate_api_error(exc)
            if isinstance(err, AIUnavailable):
                _notify_unavailable(err)
            raise err from exc
        tracker.record(response, model)
        _check_stop(response)
        seen |= collect_seen_urls(response.content)
        submitted = [b for b in response.content if getattr(b, "type", "") == "tool_use" and b.name == submit_tool["name"]]
        if submitted:
            data = submitted[0].input
            if isinstance(data, str):
                data = json.loads(data)
            return data, seen
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue
        if nudged:
            raise AIOutputError("The research agent finished without submitting structured results.")
        nudged = True
        messages.append({
            "role": "user",
            "content": f"Now call the {submit_tool['name']} tool with your findings. "
                       "Include only items you verified on real pages; an empty list is acceptable.",
        })
    raise AIOutputError("Research did not finish within the allowed number of turns.")


# --- OpenRouter -------------------------------------------------------------------

OPENROUTER_TIMEOUT = (15, 300)  # connect, read (seconds)
OPENROUTER_MIN_OUTPUT_TOKENS = 6000  # reasoning models spend output tokens on thinking
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def openrouter_models(sys=None) -> list[str]:
    if sys is None:
        from apps.core.models import SystemSettings

        sys = SystemSettings.get_solo()
    from apps.core.models import DEFAULT_OPENROUTER_MODELS

    ids = [m.strip() for m in (sys.openrouter_models or "").split(",") if m.strip()]
    return ids or [DEFAULT_OPENROUTER_MODELS]


def _next_utc_midnight(now=None) -> dt.datetime:
    now = now or timezone.now()
    return (now + dt.timedelta(days=1)).astimezone(dt.timezone.utc).replace(hour=0, minute=5, second=0, microsecond=0)


def _quota_reset_time(headers) -> dt.datetime:
    """When a 429 quota resets: X-RateLimit-Reset (epoch ms or s), else Retry-After, else next UTC midnight."""
    now = timezone.now()
    reset = (headers or {}).get("X-RateLimit-Reset")
    if reset:
        try:
            value = float(reset)
            when = dt.datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=dt.timezone.utc)
            if now < when < now + dt.timedelta(days=2):
                return when + dt.timedelta(minutes=1)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    retry_after = (headers or {}).get("Retry-After")
    if retry_after and str(retry_after).isdigit():
        return now + dt.timedelta(seconds=min(int(retry_after), 86400))
    return _next_utc_midnight(now)


def _openrouter_error(status, message: str, headers=None) -> AIError:
    text = (message or "").strip() or f"HTTP {status}"
    low = text.lower()
    if status == 401:
        return AIUnavailable("The OpenRouter API key was rejected - create a new key at openrouter.ai/settings/keys.")
    if status == 402:
        return AICreditsExhausted(
            "OpenRouter has no credits for the selected model(s) - use free models (openrouter/free) "
            "or add credits at openrouter.ai/settings/credits."
        )
    if status == 403:
        return AIRefused(f"OpenRouter moderation blocked this request: {text[:300]}")
    if status == 404:
        if "data policy" in low or "privacy" in low:
            return AIUnavailable(
                "No OpenRouter model is allowed by the account's privacy settings. Free models need the "
                "free-endpoint / training option enabled at openrouter.ai/settings/privacy - or choose other models "
                "in System settings."
            )
        return AIUnavailable(
            f"OpenRouter has no available endpoint for the selected model(s): {text[:300]}. "
            "Change 'OpenRouter models' in System settings (e.g. openrouter/free)."
        )
    if status == 429:
        if re.search(r"per[- ]day|daily|free-models-per", low):
            when = _quota_reset_time(headers)
            return AIQuotaReached(
                f"OpenRouter daily request limit reached - AI work continues automatically after "
                f"{timezone.localtime(when):%d %b %H:%M}. ({text[:200]})", when,
            )
        return AIError("Rate limited by OpenRouter - the job will retry later.")
    if status == 400:
        return AIOutputError(f"Request rejected by OpenRouter: {text[:500]}")
    return AIError(f"OpenRouter error {status}: {text[:300]}")


def _notify_quota(exc: AIQuotaReached):
    from apps.core.audit import notify
    from apps.core.models import Notification

    title = "AI paused: OpenRouter daily limit reached"
    if not Notification.objects.filter(title=title, is_read=False).exists():
        notify(title, body=str(exc), url="/agents/", level="warning")


def _raise_openrouter(status, message, headers=None):
    err = _openrouter_error(status, message, headers)
    if isinstance(err, AIUnavailable):
        _notify_unavailable(err)
    elif isinstance(err, AIQuotaReached):
        _notify_quota(err)
    raise err


def _error_message(err) -> str:
    if isinstance(err, dict):
        message = str(err.get("message") or "")
        raw = (err.get("metadata") or {}).get("raw") if isinstance(err.get("metadata"), dict) else None
        if raw and str(raw) not in message:
            message = f"{message} ({str(raw)[:300]})" if message else str(raw)[:300]
        return message
    return str(err or "")


def _openrouter_post(payload: dict) -> dict:
    if not settings.OPENROUTER_API_KEY:
        err = AIUnavailable("OPENROUTER_API_KEY is not set - choose Anthropic in System settings or add the key to .env.")
        _notify_unavailable(err)
        raise err
    headers = {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": settings.SITE_URL,
        "X-Title": "Flotec Sales AI",
    }
    try:
        resp = requests.post(f"{settings.OPENROUTER_BASE_URL}/chat/completions", json=payload, headers=headers,
                             timeout=OPENROUTER_TIMEOUT)
    except requests.RequestException as exc:
        raise AIError("Could not reach OpenRouter.") from exc
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code >= 400:
        message = _error_message(body.get("error")) if isinstance(body, dict) else (resp.text or "")[:300]
        _raise_openrouter(resp.status_code, message, resp.headers)
    if not isinstance(body, dict):
        raise AIError("OpenRouter returned an unreadable response.")
    if body.get("error"):
        err = body["error"]
        code = err.get("code") if isinstance(err, dict) else None
        _raise_openrouter(code if isinstance(code, int) else 502, _error_message(err), resp.headers)
    return body


def _record_openrouter_usage(tracker: RunTracker, body: dict, fallback_model: str):
    usage = body.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    try:
        cost = Decimal(str(usage.get("cost") or 0))
    except (InvalidOperation, ValueError):
        cost = Decimal("0")
    tracker.record_usage(
        model=body.get("model") or fallback_model,
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        cache_read=int(details.get("cached_tokens") or 0),
        cost=max(cost, Decimal("0")),
    )


def _message_text(body: dict) -> str:
    choices = body.get("choices") or []
    if not choices:
        raise AIOutputError("OpenRouter returned no answer.")
    choice = choices[0]
    if choice.get("error"):
        err = choice["error"]
        code = err.get("code") if isinstance(err, dict) else None
        _raise_openrouter(code if isinstance(code, int) else 502, _error_message(err))
    message = choice.get("message") or {}
    if message.get("refusal"):
        raise AIRefused(f"The model declined this request: {str(message['refusal'])[:300]}")
    finish = choice.get("finish_reason")
    if finish == "content_filter":
        raise AIRefused("The provider's content filter blocked the answer.")
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if finish == "length":
        raise AIOutputError("The model ran out of output tokens before finishing.")
    return content


def extract_json(text: str):
    """Parse a JSON object from a model answer (tolerates code fences and <think> blocks)."""
    t = _THINK.sub("", text or "").strip()
    fenced = _FENCE.search(t)
    if fenced:
        t = fenced.group(1).strip()
    if not t.startswith("{"):
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in the answer")
        t = t[start:end + 1]
    return json.loads(t)


def _prune(data, schema):
    """Drop keys the schema does not allow (weaker models sometimes add extras)."""
    if isinstance(data, dict) and schema.get("type") == "object":
        props = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            data = {k: v for k, v in data.items() if k in props}
        return {k: _prune(v, props[k]) if k in props else v for k, v in data.items()}
    if isinstance(data, list) and isinstance(schema.get("items"), dict):
        return [_prune(v, schema["items"]) for v in data]
    return data


def schema_problems(data, schema) -> list[str]:
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    return [f"{'/'.join(str(p) for p in e.path) or '(root)'}: {e.message}"[:300] for e in errors[:8]]


def openrouter_structured_call(tracker: RunTracker, *, system: str, user_content, schema: dict,
                               max_tokens: int = 16000) -> dict:
    """JSON-schema answer from OpenRouter, validated locally, with one repair attempt."""
    models = openrouter_models()
    instructions = (
        system
        + "\n\nOUTPUT FORMAT: reply with exactly one JSON object and nothing else (no prose, no code fences). "
        + "It must match this JSON Schema:\n" + json.dumps(schema, ensure_ascii=False)
    )
    messages = [{"role": "system", "content": instructions}, {"role": "user", "content": user_content}]
    payload = {
        "messages": messages,
        "max_tokens": max(max_tokens, OPENROUTER_MIN_OUTPUT_TOKENS),
        "response_format": {"type": "json_schema", "json_schema": {"name": "result", "strict": True, "schema": schema}},
        "provider": {"require_parameters": True},
    }
    if len(models) == 1:
        payload["model"] = models[0]
    else:
        payload["models"] = models
    problem = ""
    for attempt in range(2):
        if attempt:
            ensure_not_stopped()
            check_budget()
        body = _openrouter_post(payload)
        _record_openrouter_usage(tracker, body, models[0])
        text = _message_text(body)
        try:
            data = _prune(extract_json(text), schema)
        except ValueError as exc:  # json.JSONDecodeError is a ValueError
            problem = f"not valid JSON ({exc})"
        else:
            problems = schema_problems(data, schema)
            if not problems:
                return data
            problem = "; ".join(problems)
        payload["messages"] = messages + [
            {"role": "assistant", "content": text[:20000]},
            {"role": "user", "content": f"That answer was rejected: {problem}. Reply again with only the corrected "
                                        "JSON object matching the schema."},
        ]
    raise AIOutputError(f"The model did not return JSON matching the schema: {problem[:500]}")
