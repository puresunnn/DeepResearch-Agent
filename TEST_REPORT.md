# Baseline 验证记录

日期：2026-09-13。环境：Windows、Python 3.12.13、独立 `.venv`，依赖版本锁定于 `requirements.txt`。

## 已执行

| 检查 | 结果 | 能说明什么 |
|---|---|---|
| `python -m pytest -q` | **26 passed** | 协议、完整模拟链路、gold 隔离、脱敏、HTTP 鉴权/重试、共享预算、计算限制、超时、并行任务隔离、搜索 API 计量与独立费用、自动实验报告、评分错误、恢复及复用源码哈希校验通过 |
| `python -m research_baseline smoke --mock` | **10/10 completed，0 failures** | 本地真实冒烟集可加载，模拟模型→搜索→网页→计算→答案→轨迹落盘链路可执行 |
| `python -m research_baseline check` | 明确提示缺少 `NEWAPI_API_KEY`、`SERPER_API_KEY` | 配置检查不会打印密钥或发起模型调用 |
| `python -m research_baseline smoke --limit 1 --judge`（未填 Key） | `blocked_configuration`，`real_api_called=false` | 缺密钥时提前退出，不伪造真实结果 |
| `run.ps1 smoke --mock --resume <原运行目录>` | 10 条记录全部保留，无新推理 | PowerShell 入口与断点续跑已验证 |

模拟运行的 token 数来自 fixture，耗时主要是本地处理；不能推断真实模型成本、延迟或准确率。模拟结果的 `accuracy` 为 `null`。

## 原始产物

- [10 题最终模拟运行汇总](<D:/_Deepresearch agent/baseline/runs/20260913T022825_961487Z_4347cf/summary.json>)
- [逐题结果及 trace_dir](<D:/_Deepresearch agent/baseline/runs/20260913T022825_961487Z_4347cf/results.jsonl>)
- [缺密钥时的预检结果](<D:/_Deepresearch agent/baseline/runs/20260913T022000_613170Z_0009df/preflight.json>)

## 尚待用户填 Key 后验证

1. Talkweb 网关的账户鉴权、`qwen3.7-plus` 模型访问权限与真实生成。
2. Serper 或 IQS 的搜索额度和真实结果。
3. 目标网页可访问性、真实题目的答案质量与 Judge 得分。

建议先执行 `run.ps1 check --live`，再执行 `run.ps1 smoke --limit 1 --judge`，最后完整跑 `run.ps1 smoke --judge`。这些命令会产生真实模型/工具费用。
