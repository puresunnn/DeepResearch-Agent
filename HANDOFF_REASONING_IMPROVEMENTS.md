# Handoff: baseline 推理能力改进

更新：2026-09-20（Asia/Shanghai）

## 当前目标

在 `A0_baseline` 中落地一套通用的深度研究推理改进：**轻量约束状态 + 针对缺口的复查 + 原文定位读取**。验收标准不是架构看起来完整，而是之前答错的 benchmark 题能改答正确；回归题不能明显退化。

## 已完成的代码方向

- `research_baseline/research_state.py`：为问题拆出有限个、带来源锚点的约束；支持引用原文或经校验的来源字符区间，合并约束状态；限制使用已下载来源中的原文片段。约束状态是辅助研究，不是“答案正确”的证明。
- `research_baseline/agent.py`：围绕 unresolved gap 做有限次数的复查；研究连续数轮没有增加有证据约束时触发进度检查；审查时保留研究状态和上下文。当前允许在仍有未解决约束时输出答案，因此 `evidence_complete=false` 不能解释为答错，反之亦然。
- `research_baseline/tools.py`：新增/完善 `find` 和 `read`，可对已下载来源定位并续读；保留网页标题、章节标题及命名链接，便于追踪文章章节；批量搜索摘要尽量保留首尾信息；支持原文字符偏移引用。
- `scripts/validate_reasoning.py`：live benchmark 批量跑分和事后 judge，记录 manifest、逐题结果与 trace；求解阶段不读取 gold/reference steps。需要区分 solver 输出、judge 评分和运行错误。

## Git 与工作区

仓库：`D:\_Deepresearch agent\A0_baseline`，分支 `main`，最近提交：

1. `36e7027 chore: initialize A0 baseline with current reasoning improvements`
2. `9292343 fix: review stalled research with source context and balanced excerpts`
3. `05b6f39 fix: preserve named source links and support exact passage offsets`
4. `4484098 chore: preserve vendored file bytes in Git`

最后一次检查时工作区干净，没有配置 Git remote。vendor 文件有上游哈希校验；`.gitattributes` 对 `research_baseline/vendor/**` 禁止换行符转换，避免 CRLF 源文件被 Git checkout 改写后校验失败。`.env`、虚拟环境、`runs/` 和外部 benchmark 数据不纳入版本控制。不要提交 `.env`。

当前目录的项目说明和背景可继续读 `README.md`、`REASONING_IMPROVEMENTS.md`、`IMPLEMENTATION_PLAN.md`。数据集在仓库外：`D:\_Deepresearch agent\xbench-evals\data\DeepSearch-2510.classified.csv`；跑验收需要确认该路径及运行所需密钥在本机可用，不要把密钥写入日志/交接文档。

## 验收现状：尚未达到“之前错误的题能答对”

此前基线在六道错题上的完整尝试（run `20260920T082215_305805Z_d876f9`，代码提交 `9292343`）只答对 110（LE SSERAFIM），其余题包含错误答案、空答案或 API 错误；因此这不是可宣称完成的改进。

随后针对新版工具改动开始第二次错题验收（run `runs/20260920T084827_359207Z_ebdb31`，commit `05b6f39`，dirty=false，IDs `140,105,102,172,110,123`，并发 3）。会话中断时只完成并评分了：

| ID | 结果 | Judge | 说明 |
|---|---|---:|---|
| 102 | `Vidu4D: Single Generated Video to High-Fidelity 4D Reconstruction with Dynamic Gaussian Surfels` | 1 | 正确；13 轮，13 次搜索，1 次原文 read。是此次改动首个明确的新纠错信号。 |
| 105 | `11` | 1 | 正确；该题曾有答案漂移，需以当前冻结题目及 judge 为准。 |
| 140 | `no_answer`（空答案） | — | 已产生 task trace / research state / 下载来源；没有可供 judge 评分的答案。 |
| 172 | 未完成 | — | 有 task 目录及事件/证据文件，未产生最终结果。 |
| 110、123 | 尚未启动或无结果记录 | — | 逐题目录未生成。 |

另一组既有正确题回归（run `runs/20260920T085107_537200Z_9dda12`，commit `4484098`，dirty=false，IDs `101,127,191`，并发 1）只看到 ID 101 完成并得分 1；127 已有部分 trace，尚无 result；191 未见结果。不能据此判断回归集通过。

上述 `runs/` 被 Git 忽略，只存在本机，不会随仓库克隆交付。它们的 manifest 中记录数据集哈希及代码提交，可用于本地续查；如果另一台机器没有这些运行产物，就按同样命令重跑。

## 交接后的优先步骤

1. 先检查两份 `results.jsonl` 是否仍有新增结果，并确认 run 是否完整；检查对应 `tasks/<id>*/attempt_001/` 下的 `events.jsonl`、`research_state.json`、`evidence.json`、`artifacts/`。本次交接时没有发现仍在运行的 `validate_reasoning` 进程。
2. 从 ID 140 的 trace 诊断“研究找到了什么、缺的约束是什么、`find/read` 有没有定位到原文”，优先找可复现的通用失败模式；不要针对 benchmark ID 硬编码答案或关键词。
3. 完成错题组剩余题，再完成 127/191 回归。报告需列每题 judge 分数和运行错误，不能只给总分；API 504 属于基础设施失败，但仍须在总表里如实列出，必要时单独重跑并保留原始记录。
4. 如果代码有改动，先用小而相关的离线验证，再在固定配置、相同数据集和相同 judge 下重跑。不要在求解 prompt/context 中泄露答案或 reference steps；比较时注明 Git commit、dataset hash、并发、模型/搜索配置。

## 当前看到的风险与下一步研究点

- 约束账本目前有时会给出大量 unresolved 条目，即便最终答案正确（102 为 1/4 支持，105 为 0/6 支持）；说明状态抽取/证据绑定与正确性不是一回事。可以检查约束是否过碎、证据定位是否匹配及最终审计是否能据此选出下一步研究动作，但不要把“提高 coverage 数字”本身当成功能目标。
- 140 是优先诊断对象：改动增加命名链接索引与 passage offset，需确认页面原文是否已下载、标题链接是否被保留、具体缺口能否用 `find`/`read` 补齐。当前无最终评分。
- 172 属于稀有因果事实题，应观察通用搜索/来源交叉验证能力；不要把特定物种、历史事件或答案塞进程序规则。
- 123 之前出现 HTTP 504 空结果；新一轮尚未完成。应将服务可用性与推理质量分别记录，不应把网关失败说成推理机制效果。
- 105 目前 11 得分正确，但历史运行结果可能因版本/查询路径波动。关注可复现性与来源链，而不是只看单次答案。
- 可视题 186 是独立能力短板，不属于当前三个改进点；除非重新确认范围，先不扩展到视觉工具链。

## 运行与验收约定

从仓库根目录运行前先查看 `scripts/validate_reasoning.py --help` 和 README 命令，确保传入仓库外的 benchmark CSV。当前最近一次 manifest 使用 `qwen3.7-plus` 求解、`qwen3.7-max` judge、Serper、最大 30 轮、任务 600 秒、并发 3。不要把未评分、API 错误或未完成任务当作 0 分或通过；报告里明确区分这些状态。

## 本交接点的限制

当前重点是给后续 AI 可直接续做的事实记录；两个 live run 均不完整，验收结论为“出现局部正向信号，整体仍未验收通过”。所有运行状态与时间以本地 `runs/` 文件为准；外部数据集、凭据及运行目录都不在 Git 提交中。
