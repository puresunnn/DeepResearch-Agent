"""Bounded concurrent acceptance run; keep all outcomes in one frozen manifest."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research_baseline.config import Settings
from research_baseline.evaluation import grade, load_cases
from research_baseline.llm import ModelClient
from research_baseline.report import write_experiment_report
from research_baseline.runner import client, new_run, run_question, signature, summarize
from research_baseline.trace import Trace


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--ids", required=True)
    parser.add_argument("--concurrency", type=int, default=2, choices=(1, 2, 3))
    args = parser.parse_args()
    settings = Settings.load()
    if settings.missing_credentials():
        raise ValueError("Missing credentials: " + ", ".join(settings.missing_credentials()))
    all_cases = {c.id: c for c in load_cases(args.dataset)}
    ids = args.ids.split(",")
    if len(ids) != len(set(ids)) or set(ids) - all_cases.keys():
        raise ValueError("IDs must be unique and present in the dataset")
    cases = [all_cases[ident] for ident in ids]
    directory = new_run()
    manifest = {"signature": signature(settings, args.dataset, cases, False, True),
                "dataset": str(args.dataset.resolve()), "concurrency": args.concurrency,
                "started_at": datetime.now(timezone.utc).isoformat()}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Run: {directory}", flush=True)
    gate = asyncio.Semaphore(args.concurrency)
    rows = {}
    started = time.monotonic()

    async def one(case):
        async with gate:
            print(f"{case.id}: started", flush=True)
            # Solver receives no answers, reference steps, labels or Case object.
            result = await run_question(case.question, case.id, settings, directory)
            judge_trace = Trace(Path(result["trace_dir"]) / "judge", settings.secrets())
            async with client() as http:
                judge = ModelClient(settings, judge_trace, http)
                verdict = (await grade(case, result["answer"], judge, settings.judge_model)
                           if result["status"] in {"completed", "best_effort"}
                           else {"status": "graded", "score": 0, "method": "execution_failure"})
                result["grade"] = judge_trace.redact(verdict)
                result["judge_metrics"] = judge.metrics()
                judge_trace.write_json("grade.json", verdict)
            rows[case.id] = result
            with (directory / "results.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            print(f"{case.id}: {result['status']}, answer={result['answer']}, score={verdict.get('score')}, {result['metrics']['elapsed_seconds']}s", flush=True)

    try:
        await asyncio.gather(*(one(case) for case in cases))
    finally:
        summary = summarize(cases, rows, False)
        summary["wall_clock_seconds"] = round(time.monotonic() - started, 3)
        (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        write_experiment_report(directory, args.dataset, cases, rows, summary, settings.public(), True)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if len(rows) == len(cases) and all(row["grade"].get("score") == 1 for row in rows.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
