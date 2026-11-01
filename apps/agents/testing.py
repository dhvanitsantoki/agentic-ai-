"""Test doubles for the Claude API (used only by the test suite)."""
import json
from types import SimpleNamespace


def usage(inp=1000, out=200, searches=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=0,
                           server_tool_use=SimpleNamespace(web_search_requests=searches))


def json_response(data: dict, model="claude-opus-5-5"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(data), citations=None)], stop_reason="end_turn",
                           usage=usage(), model=model, stop_details=None)


def tool_response(name: str, data: dict, *, seen_urls=(), searches=2, model="claude-opus-5-5"):
    blocks = []
    if seen_urls:
        blocks.append(SimpleNamespace(type="web_search_tool_result",
                                      content=[SimpleNamespace(type="web_search_result", url=u, title="") for u in seen_urls]))
    blocks.append(SimpleNamespace(type="tool_use", name=name, input=data, id="toolu_test"))
    return SimpleNamespace(content=blocks, stop_reason="tool_use", usage=usage(20000, 3000, searches), model=model, stop_details=None)


def refusal_response():
    return SimpleNamespace(content=[], stop_reason="refusal", usage=usage(100, 0), model="claude-opus-5-5",
                           stop_details=SimpleNamespace(category="cyber", explanation=""))


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("FakeClient: no more responses queued")
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)
        self.beta = SimpleNamespace(messages=self.messages)
