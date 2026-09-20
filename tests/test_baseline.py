import asyncio
import json
import hashlib
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from research_baseline.config import DEFAULT_DATASET, Settings
from research_baseline.agent import Agent
from research_baseline.evaluation import Case, grade, load_cases
from research_baseline.llm import APIError, ModelClient, Reply
from research_baseline.mock import transport
from research_baseline.report import render_experiment_report
from research_baseline.runner import estimate_search_cost, load_checkpoint, run_dataset, run_question, summarize
from research_baseline.tools import ToolRunner, calculate
from research_baseline.trace import Trace
from research_baseline.vendor.protocol import _extract_tool_call


def run(coro):
    return asyncio.run(coro)


def test_smoke_dataset_shape():
    cases = load_cases(DEFAULT_DATASET)
    assert len(cases) == 10
    assert [c.id for c in cases] == ["152", "140", "139", "123", "110", "137", "186", "169", "191", "104"]


def test_entire_mock_pipeline_traces_and_gold_isolation(tmp_path):
    # Reference fields live in the loader/evaluator, not solver input.
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps({"id": "test", "question": "Find a value using an external source.", "answer": "SECRET_GOLD_6fb4c2", "reference_steps": "SECRET_REFERENCE_196d9a", "type": "SECRET_CATEGORY_429bcc"}) + "\n")
    cases = load_cases(path)
    directory = tmp_path / "run"
    directory.mkdir()
    settings = Settings(api_key="unused")
    result = run(run_question(cases[0].question, cases[0].id, settings, directory, mock=True))
    assert result["status"] == "completed"
    assert result["answer"] == "MOCK_PIPELINE_OK"
    assert result["metrics"]["search_queries"] == 1
    assert result["metrics"]["search_api_requests"] == 1
    assert result["metrics"]["search_api_successes"] == 1
    assert result["metrics"]["search_api_failures"] == 0
    assert result["metrics"]["visit_pages"] == 1
    assert result["metrics"]["calculate_calls"] == 1
    trace_dir = Path(result["trace_dir"])
    events = [json.loads(line) for line in (trace_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["seq"] for row in events] == list(range(1, len(events) + 1))
    assert {"task_started", "llm_request", "llm_response", "search_response", "page_read", "decision", "answer_candidate", "task_finished"} <= {row["event"] for row in events}
    for artifact in trace_dir.rglob("*"):
        if artifact.is_file():
            text = artifact.read_text(encoding="utf-8")
            assert "SECRET_GOLD_6fb4c2" not in text
            assert "SECRET_REFERENCE_196d9a" not in text
            assert "SECRET_CATEGORY_429bcc" not in text
    assert result["estimated_model_cost_cny"] is None
    assert result["estimated_search_cost_cny"] is None


def test_redaction_all_persistent_fields(tmp_path):
    trace = Trace(tmp_path / "trace", ["actual-secret-123"])
    trace.emit("error", message="Bearer actual-secret-123", payload={"Authorization": "anything", "x-api-key": "another-value"})
    trace.write_json("result.json", {"error": "actual-secret-123"})
    artifact = trace.artifact("page", "actual-secret-123\nsecond line\n")
    assert hashlib.sha256((trace.directory / artifact["path"]).read_bytes()).hexdigest() == artifact["sha256"]
    assert "actual-secret-123" not in "".join(p.read_text() for p in trace.directory.rglob("*") if p.is_file())
    assert "another-value" not in (trace.directory / "events.jsonl").read_text()


def test_401_does_not_retry_or_leak_key(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        return httpx.Response(401, text="invalid test-secret")
    trace = Trace(tmp_path / "trace", ["test-secret"])
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            model = ModelClient(Settings(api_key="test-secret"), trace, http)
            with pytest.raises(APIError) as caught:
                await model.complete([{"role": "user", "content": "hello"}])
            assert caught.value.status == 401
    run(scenario())
    assert len(calls) == 1
    assert "test-secret" not in (trace.directory / "events.jsonl").read_text()


def test_retry_429_then_success(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, text="try later")
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}})
    async def no_wait(_):
        return None
    monkeypatch.setattr("research_baseline.llm.asyncio.sleep", no_wait)
    async def scenario():
        trace = Trace(tmp_path / "trace")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            model = ModelClient(Settings(), trace, http)
            reply = await model.complete([{"role": "user", "content": "hello"}])
            assert reply.content == "OK"
            assert model.metrics()["prompt_tokens"] == 5
    run(scenario())
    assert len(calls) == 2


def test_separate_reasoning_cannot_execute_tool(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "<answer>real</answer>", "reasoning_content": '<tool_call>{"name":"search","arguments":{"query":["do not execute"]}}</tool_call>'}, "finish_reason": "stop"}]})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            model = ModelClient(Settings(), Trace(tmp_path / "trace"), http)
            reply = await model.complete([{"role": "user", "content": "hello"}])
            assert "do not execute" not in reply.content
    run(scenario())


def test_concurrent_shared_budget_and_duplicate_queries(tmp_path):
    async def scenario():
        settings = Settings(max_search_queries=1)
        trace = Trace(tmp_path / "trace")
        async with httpx.AsyncClient(transport=transport()) as http:
            tools = ToolRunner(settings, trace, http, ModelClient(settings, trace, http), mock=True)
            result = await tools.execute("search", {"query": ["one", "two", "one"]})
            assert result.success
            assert tools.counts["search_queries"] == 1
            assert tools.counts["search_api_requests"] == 1
            assert tools.counts["search_api_successes"] == 1
            assert tools.counts["duplicate_calls"] == 1
            assert "Search budget exhausted" in result.content
    run(scenario())


@pytest.mark.parametrize("expression", ["__import__('os').system('x')", "[1,2]", "2**99999", "1/0", "True", "1e300"])
def test_calculator_rejects_unsafe_or_unbounded_expressions(expression):
    with pytest.raises((ValueError, ZeroDivisionError)):
        calculate(expression)


def test_calculator_supported_operations():
    assert calculate("(120/100-1)*100") == pytest.approx(20)
    assert calculate("6*7") == 42


def test_private_source_url_rejected(tmp_path):
    async def scenario():
        settings = Settings()
        trace = Trace(tmp_path / "trace")
        async with httpx.AsyncClient(transport=transport()) as http:
            tools = ToolRunner(settings, trace, http, None)
            tools.observed_urls.add("http://127.0.0.1/private")
            result = await tools.execute("visit", {"url": ["http://127.0.0.1/private"]})
            assert not result.success
            assert "blocked" in result.content
    run(scenario())


def test_grade_errors_are_unscored_and_exact_match_skips_api():
    class Broken:
        async def complete(self, *args, **kwargs):
            raise APIError("down")
    case = Case("1", "q", "gold")
    assert run(grade(case, "gold", Broken(), "judge"))["score"] == 1
    failed = run(grade(case, "prediction", Broken(), "judge"))
    assert failed["score"] is None
    assert failed["status"] == "judge_error"


def test_resume_rejects_config_changes_and_recovers_tail(tmp_path):
    signature = {"case_ids": ["a"], "model": "one"}
    (tmp_path / "manifest.json").write_text(json.dumps({"signature": signature}))
    (tmp_path / "results.jsonl").write_text('{"id":"a","status":"error"}\n{"partial"')
    rows = load_checkpoint(tmp_path, signature)
    assert rows["a"]["status"] == "error"
    assert (tmp_path / "results.jsonl").read_text().endswith("\n")
    assert list(tmp_path.glob("checkpoint_tail_*.txt"))
    with pytest.raises(ValueError, match="Resume refused"):
        load_checkpoint(tmp_path, {**signature, "model": "two"})


def test_accuracy_retains_failures_in_denominator():
    cases = [Case("1", "a", "b"), Case("2", "c", "d")]
    metrics = {"prompt_tokens": 1, "completion_tokens": 1, "elapsed_seconds": 1}
    rows = {"1": {"status": "completed", "grade": {"score": 1}, "metrics": metrics}, "2": {"status": "error", "grade": {"score": 0}, "metrics": metrics}}
    assert summarize(cases, rows, False)["accuracy"] == 0.5
    rows["2"]["grade"]["score"] = None
    assert summarize(cases, rows, False)["accuracy"] is None
    assert summarize(cases, rows, True)["accuracy"] is None


def test_experiment_report_contains_cost_usage_and_diagnostics(tmp_path):
    case = Case("1", "Which value is correct?", "gold")
    metrics = {"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 10, "reasoning_tokens": 5,
               "llm_calls": 2, "elapsed_seconds": 3.5, "search_queries": 2, "search_api_requests": 2,
               "search_api_successes": 1, "search_api_failures": 1, "search_api_cancelled": 0,
               "search_api_latency_seconds": 0.4, "visit_pages": 1, "calculate_calls": 0,
               "duplicate_calls": 1, "tool_errors": 1}
    row = {"id": "1", "status": "completed", "answer": "wrong", "stop_reason": "model_finished", "rounds": 2,
           "metrics": metrics, "grade": {"score": 0, "status": "graded", "method": "judge", "response": "Expected another value."},
           "judge_metrics": {"llm_calls": 1, "prompt_tokens": 30, "completion_tokens": 5},
           "estimated_model_cost_cny": 0.01, "estimated_search_cost_cny": 0.002}
    summary = summarize([case], {"1": row}, False)
    summary["wall_clock_seconds"] = 4.0
    text = render_experiment_report(tmp_path, tmp_path / "data.jsonl", [case], {"1": row}, summary,
                                    {"agent_model": "agent", "judge_model": "judge", "search_provider": "serper"}, True)
    assert "搜索 API 实际请求 | 2" in text
    assert "Agent prompt tokens | 100" in text
    assert "ID 1：答案未通过评分" in text
    assert "Expected another value." in text


def test_upstream_xml_and_function_protocol():
    assert json.loads(_extract_tool_call('<tool_call>{"name":"search","arguments":{"query":["test"]}}</tool_call>'))["name"] == "search"
    result = _extract_tool_call('<function=visit><parameter=url>["https://example.org"]</parameter></function>')
    assert json.loads(result)["arguments"]["url"] == ["https://example.org"]


def test_settings_block_override_of_model_payload():
    with pytest.raises(ValueError):
        Settings(llm_extra_body={"messages": []}).validate()


def test_search_api_failure_and_separate_cost_are_recorded(tmp_path):
    def handler(_request):
        return httpx.Response(429, text="quota exceeded")

    async def scenario():
        settings = Settings(serper_api_key="unused", search_price_per_1000_requests=2.5)
        trace = Trace(tmp_path / "trace")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            tools = ToolRunner(settings, trace, http, None)
            result = await tools.execute("search", {"query": ["one query"]})
            assert not result.success
            assert tools.counts["search_api_requests"] == 1
            assert tools.counts["search_api_successes"] == 0
            assert tools.counts["search_api_failures"] == 1
            assert estimate_search_cost(settings, tools.counts, False) == pytest.approx(0.0025)
        events = [json.loads(line) for line in (trace.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        response = next(row for row in events if row["event"] == "search_api_response")
        assert response["status"] == 429
        assert response["success"] is False

    run(scenario())


def test_vendored_manifest_tracks_actual_file_bytes():
    vendor = Path(__file__).resolve().parents[1] / "research_baseline/vendor"
    for row in json.loads((vendor / "SOURCES.json").read_text(encoding="utf-8")):
        assert hashlib.sha256((vendor / row["file"]).read_bytes()).hexdigest() == row["vendored_sha256"]


def test_global_deadline_cancels_slow_llm_and_retains_trace(tmp_path):
    async def scenario():
        settings = Settings(task_timeout_seconds=0.2, final_reserve_seconds=0.05)
        trace = Trace(tmp_path / "trace")
        class Slow:
            async def complete(self, *args, **kwargs):
                await asyncio.sleep(10)
        async with httpx.AsyncClient(transport=transport()) as http:
            tools = ToolRunner(settings, trace, http, Slow(), mock=True)
            agent = Agent(settings, trace, Slow(), tools)
            result = await asyncio.wait_for(agent.run("q"), timeout=1)
            assert result["status"] == "no_answer"
            assert result["stop_reason"] == "research_timeout"
            assert (trace.directory / "messages.json").is_file()
    run(scenario())


def test_mixed_answer_and_tool_is_not_accepted(tmp_path):
    async def scenario():
        settings = Settings()
        trace = Trace(tmp_path / "trace")
        class Mixed:
            async def complete(self, *args, **kwargs):
                return Reply('<tool_call>{"name":"search","arguments":{"query":["q"]}}</tool_call><answer>fake</answer>', "stop")
        async with httpx.AsyncClient(transport=transport()) as http:
            tools = ToolRunner(settings, trace, http, Mixed(), mock=True)
            result = await Agent(settings, trace, Mixed(), tools).run("q")
            assert result["status"] == "protocol_error"
            assert not result["answer"]
            assert tools.counts["search_queries"] == 0
    run(scenario())


def test_parallel_tasks_do_not_share_trace_or_evidence(tmp_path):
    async def scenario():
        results = await asyncio.gather(run_question("UNIQUE_QUESTION_A", "a", Settings(), tmp_path, mock=True), run_question("UNIQUE_QUESTION_B", "b", Settings(), tmp_path, mock=True))
        assert results[0]["trace_dir"] != results[1]["trace_dir"]
        for result, forbidden in zip(results, ["UNIQUE_QUESTION_B", "UNIQUE_QUESTION_A"]):
            assert result["status"] == "completed"
            text = (Path(result["trace_dir"]) / "events.jsonl").read_text(encoding="utf-8")
            assert forbidden not in text
    run(scenario())
