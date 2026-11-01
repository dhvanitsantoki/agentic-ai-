"""Claude API wrapper used by every agent.

* Meters tokens, web searches and cost on the current AgentRun.
* Enforces the monthly AI budget before every call.
* Uses structured outputs (JSON schema) for extraction/classification/drafting.
* Runs a manual tool loop for research (server-side web_search / web_fetch + a strict
  client-side `submit_*` tool that carries the structured result), handling pause_turn.
* Opts into server-side refusal fallbacks where the model supports them.
"""
import json
import logging
from contextlib import contextmanager
from decimal import Decimal

import anthropic
from django.conf import settings
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


def ai_configured() -> bool:
    return bool(settings.ANTHROPIC_API_KEY)


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
                    max_tokens: int = 16000) -> dict:
    """Single request whose answer is JSON matching `schema` (structured outputs)."""
    ensure_not_stopped()
    check_budget()
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
    except anthropic.BadRequestError as exc:
        raise AIOutputError(f"Request rejected by the API: {exc}") from exc
    except anthropic.AuthenticationError as exc:
        raise AIUnavailable("The Anthropic API key was rejected.") from exc
    except anthropic.RateLimitError as exc:
        raise AIError("Rate limited by the API - the job will retry later.") from exc
    except anthropic.APIStatusError as exc:
        raise AIError(f"API error {exc.status_code}") from exc
    except anthropic.APIConnectionError as exc:
        raise AIError("Could not reach the Anthropic API.") from exc
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
        except anthropic.BadRequestError as exc:
            raise AIOutputError(f"Request rejected by the API: {exc}") from exc
        except anthropic.AuthenticationError as exc:
            raise AIUnavailable("The Anthropic API key was rejected.") from exc
        except (anthropic.RateLimitError, anthropic.APIConnectionError) as exc:
            raise AIError(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            raise AIError(f"API error {exc.status_code}") from exc
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
