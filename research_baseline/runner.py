from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .agent import Agent
from .config import PROJECT
from .evaluation import code_hash, file_hash, grade
from .llm import APIError, ModelClient
from .mock import transport
from .report import write_experiment_report
from .tools import ToolRunner
from .trace import Trace


def new_run():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    directory = PROJECT / "runs" / (stamp + "_" + uuid.uuid4().hex[:6])
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def task_directory(run_dir, case_id):
    label = re.sub(r"[^a-zA-Z0-9_-]", "_", str(case_id))[:60] or "question"
    label += "_" + hashlib.sha256(str(case_id).encode()).hexdigest()[:8]
    parent = run_dir / "tasks" / label
    parent.mkdir(parents=True, exist_ok=True)
    attempt = 1
    while (parent / f"attempt_{attempt:03}").exists():
        attempt += 1
    return parent / f"attempt_{attempt:03}"


def client(mock=False):
    return httpx.AsyncClient(transport=transport() if mock else None, follow_redirects=False, trust_env=not mock)


async def run_question(question, case_id, settings, run_dir, *, mock=False):
    trace = Trace(task_directory(run_dir, case_id), settings.secrets())
    started = time.monotonic()
    trace.write_json("task.json", {"id": case_id, "question": question, "mode": "mock" if mock else "live", "config": settings.public()})
    trace.emit("task_started", id=case_id, mode="mock" if mock else "live")
    async with client(mock) as http:
        llm = ModelClient(settings, trace, http)
        tool_runner = ToolRunner(settings, trace, http, llm, mock=mock)
        agent = Agent(settings, trace, llm, tool_runner)
        try:
            result = await asyncio.wait_for(agent.run(question), timeout=settings.task_timeout_seconds + 0.5)
        except asyncio.TimeoutError:
            result = {"status": "timeout", "answer": agent.candidate if tool_runner.evidence else "", "stop_reason": "global_deadline", "rounds": agent.rounds}
        except asyncio.CancelledError:
            trace.emit("task_cancelled")
            trace.write_json("result.json", {"id": case_id, "status": "cancelled", "answer": agent.candidate})
            raise
        except Exception as error:
            result = {"status": "api_error" if isinstance(error, APIError) else "error", "answer": agent.candidate if tool_runner.evidence else "", "stop_reason": type(error).__name__, "error": str(error), "http_status": getattr(error, "status", None), "rounds": agent.rounds}
        result.update({"id": case_id, "mode": "mock" if mock else "live", "metrics": {**llm.metrics(), **tool_runner.counts, "protocol_normalized": agent.normalizations, "elapsed_seconds": round(time.monotonic() - started, 3)}, "trace_dir": str(trace.directory)})
        result["estimated_model_cost_cny"] = estimate_cost(settings, result["metrics"], mock)
        result["estimated_search_cost_cny"] = estimate_search_cost(settings, result["metrics"], mock)
        trace.write_json("messages.json", agent.messages)
        trace.write_json("evidence.json", tool_runner.evidence)
        result = trace.redact(result)
        trace.write_json("result.json", result)
        trace.emit("task_finished", result=result)
    return result


def estimate_cost(settings, metrics, mock):
    if mock or settings.extractor_enabled or metrics["missing_usage_calls"] or settings.input_price_per_million is None or settings.output_price_per_million is None:
        return None
    cached = min(metrics["cached_tokens"], metrics["prompt_tokens"])
    cache_price = settings.cached_input_price_per_million
    if cache_price is None:
        cache_price = settings.input_price_per_million
    return round(((metrics["prompt_tokens"] - cached) * settings.input_price_per_million + cached * cache_price + metrics["completion_tokens"] * settings.output_price_per_million) / 1_000_000, 8)


def estimate_search_cost(settings, metrics, mock):
    if mock or settings.search_price_per_1000_requests is None:
        return None
    return round(metrics.get("search_api_requests", 0) * settings.search_price_per_1000_requests / 1000, 8)


def signature(settings, dataset, cases, mock, judge):
    return {"config": settings.public(), "dataset_sha256": file_hash(dataset), "code_sha256": code_hash(),
            "case_ids": [c.id for c in cases], "mode": "mock" if mock else "live", "judge_enabled": judge,
            "grader": "xbench_prompt_newapi_v1; judge errors remain unscored"}


def load_checkpoint(run_dir, expected):
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest["signature"] != expected:
        raise ValueError("Resume refused: dataset, selected cases, code, config, mode or judge differs. Start a new run.")
    rows = {}
    path = run_dir / "results.jsonl"
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1:
                    # Preserve interrupted tail for debugging, then restore appendable JSONL.
                    (run_dir / f"checkpoint_tail_{uuid.uuid4().hex[:8]}.txt").write_text(line, encoding="utf-8")
                    path.write_text("\n".join(lines[:index]) + ("\n" if index else ""), encoding="utf-8")
                    break
                raise
            if row["id"] not in expected["case_ids"]:
                raise ValueError("Checkpoint contains unexpected case id")
            rows[row["id"]] = row
    return rows


def summarize(cases, rows, mock):
    values = list(rows.values())
    scores = [row.get("grade", {}).get("score") for row in values]
    scored = [score for score in scores if score is not None]
    all_graded = len(scored) == len(cases)
    def sum_metric(name):
        return sum(row["metrics"].get(name, 0) for row in values)

    def sum_cost(name):
        costs = [row.get(name) for row in values]
        return round(sum(costs), 8) if costs and all(cost is not None for cost in costs) else None

    search_requests = sum_metric("search_api_requests")
    search_successes = sum_metric("search_api_successes")
    return {"mode": "mock" if mock else "live", "total": len(cases), "finished": len(values),
            "completed": sum(row["status"] == "completed" for row in values),
            "best_effort": sum(row["status"] == "best_effort" for row in values),
            "failures": sum(row["status"] not in {"completed", "best_effort"} for row in values),
            "scored": len(scored), "correct": sum(scored) if scored else None,
            "accuracy": sum(scored) / len(cases) if all_graded and not mock and cases else None,
            "confirmed_correct_over_total": sum(scored) / len(cases) if scored and not mock else None,
            "accuracy_note": "MOCK checks plumbing only; no benchmark score." if mock else "Accuracy is null until every selected item is scored. All failures remain in the denominator.",
            "agent_prompt_tokens": sum(row["metrics"]["prompt_tokens"] for row in values),
            "agent_completion_tokens": sum(row["metrics"]["completion_tokens"] for row in values),
            "search_queries": sum_metric("search_queries"),
    "protocol_normalized": sum_metric("protocol_normalized"),
            "search_api_requests": search_requests,
            "search_api_successes": search_successes,
            "search_api_failures": sum_metric("search_api_failures"),
            "search_api_cancelled": sum_metric("search_api_cancelled"),
            "search_api_success_rate": round(search_successes / search_requests, 6) if search_requests else None,
            "search_api_latency_seconds": round(sum_metric("search_api_latency_seconds"), 3),
            "estimated_model_cost_cny": sum_cost("estimated_model_cost_cny"),
            "estimated_search_cost_cny": sum_cost("estimated_search_cost_cny"),
            "total_task_seconds": round(sum(row["metrics"]["elapsed_seconds"] for row in values), 3)}


async def run_dataset(settings, dataset, cases, *, mock=False, judge=False, resume=None):
    batch_started = time.monotonic()
    if mock:
        settings = replace(settings, api_key="mock-model-key", serper_api_key="mock-search-key", iqs_api_key="mock-search-key", jina_api_key="", extractor_enabled=False)
    expected = signature(settings, dataset, cases, mock, judge)
    directory = Path(resume).resolve() if resume else new_run()
    if resume:
        rows = load_checkpoint(directory, expected)
    else:
        rows = {}
        (directory / "manifest.json").write_text(json.dumps({"signature": expected, "dataset": str(dataset.resolve()), "started_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2), encoding="utf-8")
    missing = settings.missing_credentials()
    if missing and not mock:
        report = {"status": "blocked_configuration", "missing": missing, "env_file": str(PROJECT / ".env"), "real_api_called": False}
        (directory / "preflight.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({**report, "run_dir": str(directory)}, ensure_ascii=False, indent=2), flush=True)
        return 2
    print(f"Run: {directory}\nMode: {'MOCK — no accuracy measurement' if mock else 'LIVE'}", flush=True)
    try:
        for index, case in enumerate(cases, 1):
            if case.id in rows:
                print(f"[{index}/{len(cases)}] {case.id}: checkpoint retained ({rows[case.id]['status']})", flush=True)
                continue
            print(f"[{index}/{len(cases)}] {case.id}: started", flush=True)
            # Only these two strings are passed into solver; never the Case or dataset row.
            result = await run_question(case.question, case.id, settings, directory, mock=mock)
            if judge and not mock:
                judge_trace = Trace(Path(result["trace_dir"]) / "judge", settings.secrets())
                async with client() as http:
                    judge_llm = ModelClient(settings, judge_trace, http)
                    if result["status"] not in {"completed", "best_effort"}:
                        verdict = {"status": "graded", "score": 0, "method": "execution_failure"}
                    else:
                        verdict = await grade(case, result["answer"], judge_llm, settings.judge_model)
                    result["grade"] = judge_trace.redact(verdict)
                    result["judge_metrics"] = judge_llm.metrics()
                    judge_trace.write_json("grade.json", verdict)
            else:
                result["grade"] = {"status": "mock_unscored" if mock else "unscored", "score": None}
            rows[case.id] = result
            with (directory / "results.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                handle.flush()
            print(f"[{index}/{len(cases)}] {case.id}: {result['status']}, {result['metrics']['elapsed_seconds']}s, score={result['grade']['score']}", flush=True)
            if result.get("http_status") in {401, 402, 403}:
                print("Stopping batch after authentication/billing failure. Remaining items are pending.", flush=True)
                break
    finally:
        report = summarize(cases, rows, mock)
        report["wall_clock_seconds"] = round(time.monotonic() - batch_started, 3)
        (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report_path = write_experiment_report(directory, dataset, cases, rows, report, settings.public(), judge)
    print(json.dumps({**report, "run_dir": str(directory), "report_path": str(report_path)}, ensure_ascii=False, indent=2), flush=True)
    return 0 if len(rows) == len(cases) and report["failures"] == 0 else 1
