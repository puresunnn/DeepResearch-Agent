from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path

from .config import DEFAULT_DATASET, PROJECT, Settings
from .evaluation import load_cases
from .llm import ModelClient
from .runner import client, new_run, run_dataset, run_question
from .trace import Trace


async def check(settings, live=False):
    missing = settings.missing_credentials()
    result = {"config_valid": True, "env_file": str(PROJECT / ".env"), "base_url": settings.base_url,
              "agent_model": settings.agent_model, "search_provider": settings.search_provider,
              "missing": missing, "model_api_checked": False}
    if live and not missing:
        directory = new_run()
        trace = Trace(directory / "preflight", settings.secrets())
        try:
            async with client() as http:
                response = await http.get(settings.base_url + "/models", headers={"Authorization": f"Bearer {settings.api_key}"}, timeout=settings.llm_timeout_seconds)
                response.raise_for_status()
                models = [item["id"] for item in response.json()["data"]]
                result["agent_model_listed"] = settings.agent_model in models
                result["configured_models_listed"] = {name: name in models for name in {settings.agent_model, settings.extractor_model, settings.judge_model}}
                # Models listing alone does not prove the completion endpoint works.
                model = ModelClient(settings, trace, http)
                reply = await model.complete([{"role": "user", "content": "Reply with exactly OK."}], max_tokens=128)
                result["completion_received"] = bool(reply.content)
                result["model_api_checked"] = True
        except Exception as error:
            result["error"] = trace.redact(str(error))
        trace.write_json("check.json", result)
        result["trace_dir"] = str(trace.directory)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if missing or result.get("error") else 0


async def main():
    parser = argparse.ArgumentParser(description="Traceable xbench ReAct baseline")
    parser.add_argument("--env-file", type=Path, help="Default: baseline/.env (environment variables take precedence)")
    sub = parser.add_subparsers(dest="command", required=True)
    smoke = sub.add_parser("smoke", help="Run the 10-question local smoke dataset")
    smoke.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    smoke.add_argument("--mock", action="store_true", help="Deterministic HTTP fixtures, never a benchmark score")
    smoke.add_argument("--judge", action="store_true", help="Grade via a separate New API judge call")
    smoke.add_argument("--limit", type=int)
    smoke.add_argument("--ids", help="Comma-separated dataset ids")
    smoke.add_argument("--resume", type=Path, help="Retain ALL completed attempts; never rerun only wrong answers")
    checker = sub.add_parser("check", help="Validate configuration without exposing secrets")
    checker.add_argument("--live", action="store_true", help="Also call model list and a minimal completion (billed)")
    ask = sub.add_parser("ask", help="Run one question with an independent trace")
    ask.add_argument("question")
    ask.add_argument("--mock", action="store_true")
    inspect = sub.add_parser("inspect", help="Print a concise task trajectory")
    inspect.add_argument("task_dir", type=Path)
    args = parser.parse_args()
    if args.command == "inspect":
        for line in (args.task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["event"] in {"task_started", "round_start", "decision", "tool_start", "page_read", "parse_error", "llm_error", "forced_finish", "task_finished"}:
                print(json.dumps(row, ensure_ascii=False))
        return 0
    settings = Settings.load(args.env_file)
    if args.command == "check":
        return await check(settings, args.live)
    if args.command == "ask":
        if not args.mock and settings.missing_credentials():
            return await check(settings)
        if args.mock:
            settings = replace(settings, api_key="mock-model-key", serper_api_key="mock-search-key", iqs_api_key="mock-search-key", extractor_enabled=False, jina_api_key="")
        result = await run_question(args.question, "custom", settings, new_run(), mock=args.mock)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"completed", "best_effort"} else 1
    if args.mock and args.judge:
        parser.error("--mock cannot be combined with --judge; fixtures are not benchmark answers")
    cases = load_cases(args.dataset)
    if args.ids:
        ids = args.ids.split(",")
        if set(ids) - {c.id for c in cases}:
            parser.error("Some --ids do not exist in the selected dataset")
        cases = [c for c in cases if c.id in ids]
    if args.limit is not None:
        if args.limit <= 0:
            parser.error("--limit must be positive")
        cases = cases[:args.limit]
    if not cases:
        parser.error("Dataset selection is empty")
    return await run_dataset(settings, args.dataset, cases, mock=args.mock, judge=args.judge, resume=args.resume)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (ValueError, FileNotFoundError) as error:
        print(f"Configuration/input error: {error}")
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("Interrupted. Flushed task events and checkpoint are retained.")
        raise SystemExit(130)
