from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def _pct(value, total):
    return f"{value / total:.1%}" if total else "—"


def _number(value):
    return f"{value:,}" if isinstance(value, int) else "—"


def _seconds(value):
    return f"{float(value):,.3f} s" if value is not None else "—"


def _cost(value):
    return f"¥{float(value):,.6f}" if value is not None else "未配置单价"


def _inline(value, limit=240):
    text = " ".join(str("" if value is None else value).split())
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text.replace("|", "\\|") or "—"


def _diagnostic_kind(row):
    status = row.get("status")
    score = (row.get("grade") or {}).get("score")
    if status == "api_error":
        return "模型/API 失败"
    if status in {"timeout", "no_answer", "best_effort"}:
        return "研究未完整解决"
    if status not in {"completed", "best_effort"}:
        return "执行失败"
    if score == 0:
        return "答案未通过评分"
    if score is None:
        return "尚未评分"
    return ""


def render_experiment_report(run_dir, dataset, cases, rows, summary, config, judge_enabled):
    values = [rows[case.id] for case in cases if case.id in rows]
    questions = {case.id: case.question for case in cases}
    total = summary["total"]
    answer_count = summary["completed"] + summary["best_effort"]
    judge_prompt = sum((row.get("judge_metrics") or {}).get("prompt_tokens", 0) for row in values)
    judge_completion = sum((row.get("judge_metrics") or {}).get("completion_tokens", 0) for row in values)
    judge_cached = sum((row.get("judge_metrics") or {}).get("cached_tokens", 0) for row in values)
    judge_reasoning = sum((row.get("judge_metrics") or {}).get("reasoning_tokens", 0) for row in values)
    agent_calls = sum(row.get("metrics", {}).get("llm_calls", 0) for row in values)
    judge_calls = sum((row.get("judge_metrics") or {}).get("llm_calls", 0) for row in values)
    visits = sum(row.get("metrics", {}).get("visit_pages", 0) for row in values)
    calculations = sum(row.get("metrics", {}).get("calculate_calls", 0) for row in values)
    duplicates = sum(row.get("metrics", {}).get("duplicate_calls", 0) for row in values)
    tool_errors = sum(row.get("metrics", {}).get("tool_errors", 0) for row in values)
    exact_search_metrics = any("search_api_requests" in row.get("metrics", {}) for row in values)
    normalization_metrics = any("protocol_normalized" in row.get("metrics", {}) for row in values)

    lines = [
        "# Agent 实验报告",
        "",
        f"- Run：`{Path(run_dir).name}`",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat()}",
        f"- 数据集：`{Path(dataset).resolve()}`",
        f"- 模式：`{summary['mode']}`；Judge：`{'enabled' if judge_enabled else 'disabled'}`",
        f"- Agent 模型：`{config.get('agent_model', '—')}`；Judge 模型：`{config.get('judge_model', '—')}`；搜索：`{config.get('search_provider', '—')}`",
        "",
        "## 整体结果",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 总任务数 | {total} |",
        f"| 已产生最终记录 | {summary['finished']}/{total}（{_pct(summary['finished'], total)}） |",
        f"| completed | {summary['completed']}/{total}（{_pct(summary['completed'], total)}） |",
        f"| completed + best_effort | {answer_count}/{total}（{_pct(answer_count, total)}） |",
        f"| 执行失败 | {summary['failures']}/{total}（{_pct(summary['failures'], total)}） |",
        f"| 已评分 | {summary['scored']}/{total} |",
        f"| 正确 | {_number(summary.get('correct'))} |",
        f"| 准确率 | {_pct(summary['correct'], total) if summary.get('accuracy') is not None else '未形成完整准确率'} |",
        "",
        "## 调用、时间与费用",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 实验墙钟时间 | {_seconds(summary.get('wall_clock_seconds'))} |",
        f"| Agent 单题耗时累计 | {_seconds(summary.get('total_task_seconds'))} |",
        f"| Agent LLM 调用 | {_number(agent_calls)} |",
        f"| Agent prompt tokens | {_number(summary.get('agent_prompt_tokens'))} |",
        f"| Agent completion tokens | {_number(summary.get('agent_completion_tokens'))} |",
        f"| Agent tokens 合计 | {_number(summary.get('agent_prompt_tokens', 0) + summary.get('agent_completion_tokens', 0))} |",
        f"| Judge LLM 调用 | {_number(judge_calls)} |",
        f"| Judge prompt tokens | {_number(judge_prompt)} |",
        f"| Judge completion tokens | {_number(judge_completion)} |",
        f"| Judge tokens 合计 | {_number(judge_prompt + judge_completion)} |",
        f"| Agent + Judge tokens 合计 | {_number(summary.get('agent_prompt_tokens', 0) + summary.get('agent_completion_tokens', 0) + judge_prompt + judge_completion)} |",
        f"| Judge cached / reasoning tokens | {_number(judge_cached)} / {_number(judge_reasoning)} |",
        f"| 模型估算费用 | {_cost(summary.get('estimated_model_cost_cny'))} |",
        f"| 逻辑搜索 query | {_number(summary.get('search_queries'))} |",
        f"| 搜索 API 实际请求 | {_number(summary.get('search_api_requests')) if exact_search_metrics else '未采集（旧版运行）'} |",
        f"| 搜索 API 成功 / 失败 / 取消 | {_number(summary.get('search_api_successes'))} / {_number(summary.get('search_api_failures'))} / {_number(summary.get('search_api_cancelled'))} |" if exact_search_metrics else "| 搜索 API 成功 / 失败 / 取消 | 未采集（旧版运行） |",
        f"| 搜索 API 成功率 | {_pct(summary.get('search_api_successes', 0), summary.get('search_api_requests', 0)) if exact_search_metrics else '未采集（旧版运行）'} |",
        f"| 搜索 API 累计耗时 | {_seconds(summary.get('search_api_latency_seconds')) if exact_search_metrics else '未采集（旧版运行）'} |",
        f"| 搜索 API 估算费用 | {_cost(summary.get('estimated_search_cost_cny')) if exact_search_metrics else '未采集（旧版运行）'} |",
        f"| 页面读取 / 计算 | {_number(visits)} / {_number(calculations)} |",
        f"| 重复调用拦截 / 工具错误 | {_number(duplicates)} / {_number(tool_errors)} |",
        f"| 协议本地归一化（无额外 API 调用） | {_number(summary.get('protocol_normalized')) if normalization_metrics else '未采集（旧版运行）'} |",
        "",
        "> cached token 是 prompt token 的子集，reasoning token 通常是 completion token 的子集，未重复加入 token 总量。费用是配置单价下的估算，供应商账单为准。",
        "",
        "## 逐题结果",
        "",
        "| ID | 状态 | 得分 | 轮次 | Agent 耗时 | Agent tokens | 搜索请求 | 搜索失败 | 页面 | 工具错误 | 停止原因 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in values:
        metrics = row.get("metrics", {})
        tokens = metrics.get("prompt_tokens", 0) + metrics.get("completion_tokens", 0)
        requests = metrics.get("search_api_requests") if "search_api_requests" in metrics else None
        failures = metrics.get("search_api_failures") if "search_api_failures" in metrics else None
        lines.append(
            f"| {_inline(row.get('id'))} | {_inline(row.get('status'))} | {_inline((row.get('grade') or {}).get('score'))} | "
            f"{metrics.get('rounds', row.get('rounds', '—'))} | {_seconds(metrics.get('elapsed_seconds'))} | {_number(tokens)} | "
            f"{_number(requests)} | {_number(failures)} | {metrics.get('visit_pages', 0)} | {metrics.get('tool_errors', 0)} | {_inline(row.get('stop_reason'))} |"
        )

    diagnostics = [(row, _diagnostic_kind(row)) for row in values]
    diagnostics = [(row, kind) for row, kind in diagnostics if kind and kind != "尚未评分"]
    lines += ["", "## 错误与未通过任务", ""]
    if not diagnostics:
        lines.append("本次没有执行失败或已评分错误任务。")
    else:
        for row, kind in diagnostics:
            metrics = row.get("metrics", {})
            grade = row.get("grade") or {}
            detail = row.get("error") or grade.get("response") or grade.get("status") or row.get("stop_reason")
            lines += [
                f"### ID {row.get('id')}：{kind}",
                "",
                f"- 问题：{_inline(questions.get(row.get('id')), 500)}",
                f"- 状态：`{row.get('status')}`；停止原因：`{row.get('stop_reason', '—')}`；评分方法：`{grade.get('method', '—')}`",
                f"- Agent 答案：{_inline(row.get('answer'), 600)}",
                f"- 错误说明：{_inline(detail, 900)}",
                f"- 执行信号：{row.get('rounds', 0)} 轮，{metrics.get('search_queries', 0)} 个逻辑查询，"
                f"{metrics.get('visit_pages', 0)} 次页面读取，{metrics.get('duplicate_calls', 0)} 次重复拦截，"
                f"{metrics.get('tool_errors', 0)} 次工具错误。",
                "",
            ]

    lines += [
        "## 产物位置",
        "",
        f"- 汇总：`{Path(run_dir) / 'summary.json'}`",
        f"- 逐题结果：`{Path(run_dir) / 'results.jsonl'}`",
        f"- 逐题轨迹：`{Path(run_dir) / 'tasks'}`",
        "",
    ]
    return "\n".join(lines)


def write_experiment_report(run_dir, dataset, cases, rows, summary, config, judge_enabled):
    path = Path(run_dir) / "REPORT.md"
    path.write_text(render_experiment_report(run_dir, dataset, cases, rows, summary, config, judge_enabled), encoding="utf-8", newline="\n")
    return path
