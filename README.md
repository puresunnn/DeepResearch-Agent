# xbench ReAct Baseline

基于工作区第一名 Research Agent 项目改造的单主控基线。默认模型为 `qwen3.7-plus`，通过 `https://llm.talkweb.com.cn/v1` 调用 New API 兼容接口。搜索、网页/PDF 读取和受限算术计算已接入；每道题都有独立运行轨迹。

当前验证：41 项自动化测试通过。2026-09-15 针对 105、127、191 的工程修复完成两轮真实复测，最终一轮 3/3 正常完成，答案评分 2/3；105 的推理错误单列保留。[工程修复与复测记录](RELIABILITY_FIXES.md)

历史验证：10/10 题模拟链路完成；真实 10 题本地适配评分为 5/10。[测试记录](<D:/_Deepresearch agent/baseline/TEST_REPORT.md>)

这是文本工具 baseline。完整浏览器交互、视觉/视频、复杂表格专用解析和动态证据依赖图暂未接入。10 题冒烟集含视觉等题型，因此链路跑通不代表所有题型具备解题能力。

2026-09-16 新增轻量约束状态、针对缺口的复查、已下载原文的 `find/read` 定位续读。实现边界与验收记录见 [推理改进记录](REASONING_IMPROVEMENTS.md)。

## Git 管理

本项目的独立 Git 仓库根目录为 `A0_baseline`，默认分支 `main`。代码、测试、配置模板、许可证及报告纳入版本管理；`.env`、虚拟环境、缓存和 `runs/` 实验原始产物只保留在本机。未配置远程仓库。

从 `A0_baseline` 目录执行 `git status` 查看改动、`git log --oneline` 查看历史。验收脚本会记录对应提交及代码哈希。外部评测数据位于工作区同级 `xbench-evals/data/`，未复制进仓库；在其他机器评测时需自行提供数据集路径。

## 1. 填写密钥

编辑 [本地 .env](<D:/_Deepresearch agent/baseline/.env>)：

```dotenv
NEWAPI_API_KEY=你在llm.talkweb.com.cn创建的API令牌
NEWAPI_BASE_URL=https://llm.talkweb.com.cn/v1
AGENT_MODEL=qwen3.7-plus

SEARCH_PROVIDER=serper
SERPER_API_KEY=你的Serper搜索Key
```

`NEWAPI_API_KEY` 不是网页登录密码。模型网关 Key 不能替代搜索 Key。若已有 IQS，将 `SEARCH_PROVIDER=iqs` 并填 `IQS_API_KEY`，不需要 Serper。网页首先直接 HTTP 获取，`JINA_API_KEY` 是可选备用。

环境变量优先于 `.env`；可用 `--env-file` 显式指定其他配置。默认不会自动读取参考项目里的密钥。所有输出自动过滤已配置密钥和 Authorization 字段。

New API 的 Chat Completions 路径为 `/v1/chat/completions`，认证是 Bearer token；模型列表路径为 `/v1/models`。[接口文档](https://docs.newapi.pro/zh/docs/api/ai-model/chat/openai/createchatcompletion)

具体模型的访问权限、额度、思考参数与非流式支持仍取决于网关渠道。默认 `LLM_EXTRA_BODY={}`，不写死 DashScope 专用参数；如渠道要求禁用思考，可设置 `LLM_EXTRA_BODY={"enable_thinking":false}`。API 错误会写入轨迹，不会伪装成正常答案。

## 2. 本机运行

当前工作区已创建独立 `baseline/.venv`。在 PowerShell 中：

```powershell
Set-Location 'D:\_Deepresearch agent\baseline'

# 不联网，检查配置是否齐全
.\run.ps1 check

# 不需要任何 Key；全部 HTTP 调用使用确定性模拟响应
.\run.ps1 smoke --mock

# 填写 Key 后检查模型列表和一次最小生成（会产生少量模型调用费用）
.\run.ps1 check --live

# 先跑 1 题，检查真实的 模型→搜索→网页→答案→Judge 链路
.\run.ps1 smoke --limit 1 --judge

# 完整 10 题；逐题执行，单题内部最多 3 个工具请求并发
.\run.ps1 smoke --judge

# 单题调试，不进行 gold 评分
.\run.ps1 ask '查找并回答你的问题'

# 仅测试指定样本；保持数据集原始顺序
.\run.ps1 smoke --ids '152,140' --judge
```

如果 PowerShell 执行策略阻止脚本，不必修改全局策略，直接用：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m research_baseline smoke --mock
```

新机器需要 Python 3.12+，在 `baseline` 目录创建环境并安装锁定依赖：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env # 仅首次执行，不要覆盖已填写配置
```

## 3. 运行轨迹与恢复

每次运行创建新目录；每道题、每次尝试隔离：

```text
runs/<UTC时间_随机ID>/
  manifest.json               数据/代码哈希、公开配置、题目ID、mock/live 模式
  results.jsonl               每题最终结果与评分，完成一题立即追加
  summary.json               完成率、准确率、失败数、累计 token、搜索 API 调用与费用
  REPORT.md                  自动生成的实验总览、逐题指标和错误诊断
  tasks/<题目ID_哈希>/attempt_001/
    task.json                只有题目与推理配置，不含gold和reference_steps
    events.jsonl             按顺序记录，事件逐条flush
    messages.json            模型对话快照（包括压缩后的当前上下文）
    evidence.json            搜索线索与读到的网页证据
    result.json              状态、答案、停止原因、耗时、调用统计
    artifacts/*.txt          网页正文及引用片段对应的原始文本
    judge/                   与Agent轨迹隔离的离线评分记录
```

`events.jsonl` 包含模型请求与响应、返回的 usage/reasoning_content（若提供）、简短行动说明、工具参数和返回、搜索 API 请求/响应状态及耗时、网页正文路径、候选答案、错误、压缩和任务结束事件。返回的 reasoning_content 仅作为接口原始响应记录，不拿来解析工具调用或答案。

每次 `smoke` 数据集实验结束（包括部分失败）都会在 run 目录生成 `REPORT.md`。报告包含任务完成率、本地适配评分、Agent/Judge token、墙钟时间、页面与计算工具调用、搜索 API 请求量和独立费用，以及未通过任务的答案、Judge 说明与执行异常。旧 run 没有采集到的指标会显示“未采集”，不会按 0 处理。

模型 API 失败、解析失败、工具失败、无答案、预算不足分别记录。`completed` 表示模型已输出答案，不等于答案正确；`best_effort` 表示预算/轮数不足时的候选回退。网页证据状态为 `observed`，不会仅因抓取成功就宣称 `verified`。

快速浏览轨迹：

```powershell
.\run.ps1 inspect 'D:\_Deepresearch agent\baseline\runs\实际run目录\tasks\实际task目录\attempt_001'
Get-Content '实际任务目录\events.jsonl' -Tail 20 -Wait
```

断点续跑使用原来完全相同的选择和模式参数：

```powershell
.\run.ps1 smoke --judge --resume 'D:\_Deepresearch agent\baseline\runs\实际run目录'
```

已完成尝试全部保留，包括错误题；不会只重跑错误然后拼高分。配置、代码、数据、题目选择或 Judge 模式变化将拒绝续跑，应开启新 run。修复鉴权后可以继续未执行题；之前记为失败的题需要另开运行评测。任务执行到一半中断时，下次为该题创建新 attempt，旧轨迹保留。

## 4. 冒烟与评分口径

默认读取工作区 `xbench-evals/data/DeepSearch-2510.smoketest.csv`，10 个 id 为 `152,140,139,123,110,137,186,169,191,104`。也支持其他明文/原始加密 CSV，以及 question/answer JSONL。

- `--mock` 使用真实数据集作为输入，但搜索、模型、网页全部是模拟 HTTP fixtures，固定最终答案 `MOCK_PIPELINE_OK`。用它验证加载、协议、工具执行、计算、落盘与恢复；**不代表真实联网成功，不输出 benchmark 准确率**。不允许与 `--judge` 混用。
- 默认 live 只跑推理链路；`--judge` 才离线评分。先严格匹配答案，否则复用本地 xbench 的 Judge prompt，通过配置的 `JUDGE_MODEL` 调用网关。
- 评分器与官方 Judge 模型可能不同，报告应称“本地适配评分”。Judge/API/解析故障单独标为未评分，不默认为错误；直到全体被评分才给出 accuracy，同时保留确认正确数/全体题数。
- Agent 执行失败在有 Judge 模式时记 0，仍在总题数分母中。没有密钥时生成 `preflight.json`，标明阻塞原因，不制造推理结果。
- gold、reference_steps、人工类别从不交给 solver。Judge 的请求与结果写入独立目录。
- 默认没有币价假设：费用显示 `null`，但始终记录真实调用量。可填 `.env` 的人民币/百万 token 价格估算单模型费用；混合抽取模型时不使用一个单价误算。搜索 API 单独记录请求数、成功/失败/取消数、累计耗时，并可通过 `SEARCH_PRICE_PER_1000_REQUESTS` 按当前套餐折算人民币费用。Jina、失败请求是否实际计费和 Judge 费用仍需按供应商账单核对。

## 5. 复用范围与代码

没有直接导入原项目 runtime：原代码在 import 时创建厂商固定客户端，且使用全局状态，不适合逐题隔离。保留其可复用部分并拆分配置/日志/HTTP 执行层：

| 来源 | 复用内容 |
|---|---|
| 第一名项目 agent_loop.py | ReAct 执行方式、XML 工具/答案协议、多格式容错解析、答案清理 |
| 第一名项目 tools_search.py | Serper/IQS 结果格式化、搜索证据结构与解析 |
| 第一名项目 tools_visit.py / prompts.py | BeautifulSoup 正文提取、结构化网页抽取解析与抽取提示 |
| 第一名项目 tool_types.py | 原样复用 ToolResult / EvidenceItem |
| xbench-evals/eval_grader.py | 原样提取 LLM_JUDGE_PROMPT；更换传输与可观测错误处理 |

源码位于 `research_baseline/vendor`，MIT 许可证和来源 SHA256 记录在同目录。提取脚本是 `scripts/vendor_sources.py`；生成文件的修改应更新来源记录。没有沿用原提示中强制最低轮数、“多数来源可忽略年份冲突”等策略。原基线的同步搜索传输改为异步，使批量工具请求和统一超时生效；未启用 embedding、独立路由模型和多Agent角色。

新增模块：`agent.py` 为有界 ReAct 控制层；`llm.py` 对接 New API；`tools.py` 执行工具；`trace.py` 保存轨迹；`runner.py/evaluation.py` 管理逐题运行、恢复和评分。

## 6. 测试

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q
```

测试针对真实风险：HTTP 鉴权/重试、推理内容不被当工具执行、共享预算、计算约束、私网访问拦截、gold隔离、密钥脱敏、错误评分、断点配置一致性和完整模拟链路。

`.env`、`runs/` 和本地虚拟环境均被忽略。轨迹可能包含 benchmark 明文与网页内容，仅留本地调试，不上传公开仓库。
