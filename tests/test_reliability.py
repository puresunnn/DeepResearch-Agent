import asyncio
import json
from pathlib import Path

import httpx
import pytest

from research_baseline.agent import Agent
from research_baseline.config import Settings
from research_baseline.contracts import normalize_final, parse_action
from research_baseline.llm import APIError, ModelClient, Reply
from research_baseline.mock import transport
from research_baseline.tools import ToolRunner
from research_baseline.trace import Trace


SEARCH = '<tool_call>{"name":"search","arguments":{"query":["one"]}}</tool_call>'


def events(trace):
    return [json.loads(line) for line in (trace.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def test_http_504_exhaustion_recovers_same_turn_without_repeating_search(tmp_path, monkeypatch):
    attempts = []
    responses = [SEARCH, 504, 504, 504, '<answer>value</answer>', '<answer>value</answer>']
    fixture = transport()
    async def handler(request):
        if request.url.path != "/v1/chat/completions":
            return await fixture.handle_async_request(request)
        attempts.append(json.loads(request.content))
        value = responses.pop(0)
        if isinstance(value, int):
            return httpx.Response(value, text="gateway timed out")
        return httpx.Response(200, json={"choices": [{"message": {"content": value}, "finish_reason": "stop"}]})
    async def no_wait(_):
        pass
    monkeypatch.setattr("research_baseline.llm.asyncio.sleep", no_wait)
    async def scenario():
        trace = Trace(tmp_path / "trace")
        settings = Settings()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            llm = ModelClient(settings, trace, http)
            tools = ToolRunner(settings, trace, http, llm, mock=True)
            result = await Agent(settings, trace, llm, tools).run("Find value")
        assert result["status"] == "completed"
        assert result["answer"] == "value"
        assert tools.counts["search_api_requests"] == 1
        assert attempts[1:5] == [attempts[1]] * 4
        assert len([e for e in events(trace) if e["event"] == "api_recovery"]) == 1
    asyncio.run(scenario())


def test_persistent_504_is_bounded_and_preserves_candidate(tmp_path, monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr("research_baseline.agent.asyncio.sleep", no_wait)
    class Model:
        calls = 0
        async def complete(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Reply(SEARCH, "stop")
            if self.calls == 2:
                return Reply("<answer>candidate</answer>", "stop")
            raise APIError("gateway timed out", 504)
    async def scenario():
        settings, trace, model = Settings(), Trace(tmp_path / "trace"), Model()
        async with httpx.AsyncClient(transport=transport()) as http:
            tools = ToolRunner(settings, trace, http, model, mock=True)
            result = await Agent(settings, trace, model, tools).run("Find value")
        assert result["answer"] == "candidate"
        assert result["status"] == "best_effort"
        assert result["stop_reason"] == "api_recovery_exhausted"
        assert model.calls == 5  # two successes, failed turn + recovery, one finalization
        assert any(e["event"] == "finish_error" for e in events(trace))
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 402, 403])
def test_permanent_api_error_never_enters_agent_recovery(tmp_path, status):
    class Model:
        calls = 0
        async def complete(self, *args, **kwargs):
            self.calls += 1
            raise APIError("denied", status)
    async def scenario():
        settings, trace, model = Settings(), Trace(tmp_path / "trace"), Model()
        async with httpx.AsyncClient(transport=transport()) as http:
            tools = ToolRunner(settings, trace, http, model, mock=True)
            with pytest.raises(APIError):
                await Agent(settings, trace, model, tools).run("q")
        assert model.calls == 1
    asyncio.run(scenario())


def test_recorded_protocol_failure_sequence_repairs_without_duplicate_execution(tmp_path):
    malformed = '<tool_call>{"name":"search",arguments":{"query":["one"]}}</tool_call>'
    xml = '<tool_call><name>search</name><arguments>{"query":["one"]}</arguments></tool_call>'
    class Model:
        def __init__(self):
            self.replies = [malformed, '<tool_call>' + xml + xml, xml, '<answer>value</answer>', '<answer>value</answer>']
            self.histories = []
        async def complete(self, messages, **kwargs):
            self.histories.append(json.loads(json.dumps(messages)))
            return Reply(self.replies.pop(0), "stop")
    async def scenario():
        settings, trace, model = Settings(), Trace(tmp_path / "trace"), Model()
        async with httpx.AsyncClient(transport=transport()) as http:
            tools = ToolRunner(settings, trace, http, model, mock=True)
            agent = Agent(settings, trace, model, tools)
            result = await agent.run("Find value")
        assert result["status"] == "completed"
        assert tools.counts["search_api_requests"] == 1
        assert 'Valid example: <tool_call>{"name":"search"' in model.histories[1][-1]["content"]
        assert not any(m["role"] == "assistant" and m["content"] == malformed for m in model.histories[1])
        assert any(e["event"] == "protocol_repaired" for e in events(trace))
        # Of the three recorded malformed forms only the XML child-tag one is locally recoverable.
        # The misplaced quote in `arguments":{"query"` is unparseable by json and json5 alike, and the
        # nested duplicate is ambiguous, so both correctly consume a tier-2 regeneration instead.
        assert agent.normalizations == 1
        assert [e["steps"] for e in events(trace) if e["event"] == "protocol_normalized"] == [["xml_child_tags"]]
    asyncio.run(scenario())


@pytest.mark.parametrize("content,steps", [
    # Boundary of what tier 1 repairs locally. These are synthetic variants; the recorded 127 forms are
    # asserted by the replay test above.
    (SEARCH, []),
    ('<tool_call>{"name":"search",arguments:{"query":["one"]}}</tool_call>', ["relaxed_json"]),
    ("<tool_call>{'name':'search','arguments':{'query':['one']}}</tool_call>", ["relaxed_json"]),
    ('<tool_call>{"name":"search","arguments":{"query":["one"],},}</tool_call>', ["relaxed_json"]),
    ('{"name":"search","arguments":{"query":["one"]},}', ["relaxed_json"]),
    ('<tool_call><name>search</name><arguments>{"query":["one"]}</arguments></tool_call>', ["xml_child_tags"]),
    ('<tool_call><name>search</name><arguments>{"query":["one"],}</arguments></tool_call>', ["xml_child_tags", "relaxed_json"]),
    ('<function=search><parameter=query>["one"]</parameter></function>', ["function_wrapper"]),
])
def test_tier1_normalization_steps_are_reported(content, steps):
    notes = []
    kind, call = parse_action(content, "stop", notes)
    assert kind == "tool"
    assert call == {"name": "search", "arguments": {"query": ["one"]}}
    assert notes == steps


@pytest.mark.parametrize("content", [
    SEARCH + SEARCH, SEARCH + '<answer>fake</answer>',
    '{"name":"search","arguments":{"query":["one"]}} {"name":"search","arguments":{"query":["two"]}}',
    '<think>' + SEARCH, '<tool_call>{"name":"search","arguments":{"query":["one"]}}',
    '<answer>first</answer><answer>second</answer>',
    '<tool_call>{"name":"search","arguments":{"query":null}}</tool_call>',
    # Recorded 127 form: the quote sits after `arguments` instead of before it. Neither json nor json5
    # can read that key, so tier 1 must not invent an interpretation for it.
    '<tool_call>{"name":"search",arguments":{"query":["one"]}}</tool_call>',
])
def test_ambiguous_incomplete_actions_remain_rejected(content):
    with pytest.raises(ValueError):
        parse_action(content, "stop")


def test_exact_message_normalization_requires_matching_source_and_question():
    source = 'raise\nValueError\n(\n"invalid width: expected 3."\n)'
    candidate = "ValueError: invalid width: expected 3."
    assert normalize_final("请从源代码中获取错误消息字符串", candidate, [source])[0] == "invalid width: expected 3."
    assert normalize_final("请提供错误消息字符串和异常类型", candidate, [source])[0] == candidate
    assert normalize_final("请提供错误消息字符串", candidate, ["unrelated"])[0] == candidate
    # Preserve literal wrapping quotes when they are part of the requested message.
    assert normalize_final("exact error message", '"quoted".', [])[0] == '"quoted".'


def test_historical_191_source_and_answer_without_gold():
    root = Path(__file__).resolve().parents[1]
    task = root / "runs/20260913T025152_411358Z_3b79b2/tasks/191_70260742/attempt_001"
    if not task.exists():
        pytest.skip("Local historical artifacts not distributed")
    question = json.loads((task / "task.json").read_text(encoding="utf-8"))["question"]
    answer = json.loads((task / "result.json").read_text(encoding="utf-8"))["answer"]
    sources = [p.read_text(encoding="utf-8") for p in (task / "artifacts").glob("*.txt")]
    corrected, reason = normalize_final(question, answer, sources)
    assert reason == "removed_exception_type_verified_in_source"
    assert corrected == answer.split(": ", 1)[1]
