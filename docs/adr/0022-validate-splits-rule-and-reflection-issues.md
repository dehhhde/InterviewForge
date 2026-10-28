# ADR-0022：`validate` 的规则问题与反思问题分家——只有规则问题驱动回退

日期：2026-10-28
状态：已决定（改计划书 M2 的约定：`validate_issues` 不再混装两类问题）
相关：`app/agents/nodes/validate_node.py`、`app/agents/state.py`（新通道 `reflect_issues`）、
`app/agents/workflow.py::route_after_validate`、`app/services/export_service.py`、
`app/scripts/live_check.py`、`tests/test_agents/test_validate_node.py`、
ADR-0009（确定性分工：分数归规则、措辞归模型）、`docs/TASK_PLAN.md` T-201 / D-4、
`docs/TODO.md` §2.1、`docs/OPTIMIZATION_BACKLOG.md` OPT-2.1

## 背景

`validate` 同时做两件事：跑四条确定性规则（日期倒挂、年限与经历对齐、学历别名、证据核对），
再花一次 LLM 调用让模型对着原文反思一遍。两批问题原先一起写进 `validate_issues`，
而 `route_after_validate` 只认这一个通道：**有任何一条 issue 就自增 `validate_retries`
并回 `parse`**。

问题在于 `parse` 是确定性的。同一份 `raw_text` 重跑必然给出逐字段相同的 `parsed`，
所以一条模型挑出来的语义问题（"公司名填成了岗位名"）会把 `validate_max_retries`
的预算原样烧完，三轮结果一模一样。2026-10-16 的真调已经把这件事钉成事实：
真简历规则层全过，反思挑出 6 条真实且 `parse` 修不掉的问题，于是整图白跑 3 轮。

`parse` 侧后来（T-013）砍掉了它的**付费机制**——文件指纹未变时不重读原件、不重跑 OCR——
但白跑仍是白跑。真正的问题是**两类问题被塞进了同一条回退判据**：一类 `parse` 有可能修，
另一类它结构上修不了（那是抽取层的设计缺口，不是这一遍抽取的偶然错误）。

## 决策

| 环节 | 选择 | 被否决的方案 |
|---|---|---|
| 通道 | `validate_issues` 只装**规则**判出来的问题，`reflect_issues` 装模型反思挑出的问题 | 继续混装：`validate_retries` 无法区分"这一轮花了钱有没有可能改善" |
| 回退判据 | `route_after_validate` **只读** `validate_issues`，`validate_retries` 仍由本节点自增（`<=` 口径不变） | 让路由看两个通道：那等于"反思也能回退"，本 ADR 要修的就是它 |
| 反思问题的落地 | 追加进 `errors`（`validate(reflect): <issue>`，纯观测通道）并返回 `reflect_issues` 增量 | 只写 `reflect_issues`：`errors` 是唯一"给人看的流水账"，不进去就没人看见；写进 `validate_issues` 但不自增计数：路由以外的读者会误读成"该回退" |
| 反思的闸门 | 保留"意见必须点得出 `ResumeSchema` 的字段名"才采纳 | 放宽成"任何批评都收"：一条凭感觉的批评既修不掉又会污染导出报告 |
| 送问的形状 | 不变：仍送 `raw_text` + `parsed` + 今天的日期 | 顺手改成"只送规则没过的字段"：那要改调用约定与模板，属于另一张卡（TODO §2.1 的"只重抽被点名的字段"） |
| 计数 | 一轮只加一（`state.get("validate_retries", 0) + 1`），不按条数加 | 按条数加：一条问题就会把预算吃光，而预算的单位本来就是"轮" |
| 导出与 CLI | `export_service` 的报告注记分开报两笔（回退 N 次 / 反思另 M 条），`live_check.py` 分两段打印 | 只报 `validate_issues`：反思问题就变成只存在于 trace 里的东西 |

## 理由

1. **回退只在"重跑有机会不同"时才是合理的**。`parse` 的确定性是设计（ADR-0009 的
   "确定性优先"），所以"重跑"能改变的东西只有"它这次读了什么"——而文件指纹没变，
   读的还是同一份。把反思问题塞进回退判据，等于向一个必然给出同答案的函数要新答案。
2. **两类问题的处置方向本来就相反**。规则问题指向"这一遍抽错了"（重跑可能修），
   反思问题指向"抽取层没有这条判据"（重跑必然不修，只能改抽取器或改规则）。
   把它们分开之后，`reflect_issues` 才第一次成为"抽取层欠账清单"的机器可读来源。
3. **`errors` 通道保住了可见性**。ADR-0018 之后 `errors` 是批注式流水账的既定去处，
   纯观测、无机器后果；反思问题进去之后，报告页与导出都能看到它，
   而路由完全不受影响。
4. **闸门保留的理由没有变**。`parse` 是确定性的，所以"一条点不出字段名的批评"
   连重抽都不知道该动哪儿——它只会变成一条永远不消失的噪音。真调录制件
   （`tests/fixtures/llm/validate_liang_qingsheng.json`）在回放测试里证明了闸门的作用：
   真模型的每条意见都点了字段名，于是它们全被采纳、且**一条都不触发回退**。
5. **这条改动让 M2 的老约定退役，但没让任何断言变松**。原来那句"反思有问题就回退"
   的验收（`test_a_dirty_resume_uses_up_the_budget…`）换成"规则有问题才回退"，
   新增 `test_reflection_issues_never_buy_a_second_parse_round` 钉住反面：
   反思有话说时 `parse` 只跑 1 轮、`validate_retries` 不出现。

## 权衡与代价

- **反思问题现在没有消费者**。它们落进 `errors` 与 `reflect_issues`，报告里能看到，
  但没有任何环节会因此重跑或重抽。"只重抽被点名的字段"仍是空账（TODO §2.1）——
  那条要改 `parse` 的入参约定，与载荷瘦身（TODO §2.9）是同一个改动面。
- **规则问题仍会把预算烧完**。日期倒挂这类问题重跑必然复现，三轮照旧。本 ADR 只砍掉
  了"反思也来烧"的那部分；按预期，真实简历里规则问题比反思问题少得多，
  所以总浪费从"每份简历 3 轮"降到"规则真有问题时 3 轮"。
- **`reflect_issues` 是新通道，但不在 `GenerateJob` 载荷里**。它属于校验期信息，
  出题环节读不到它；要让它影响出题得先决定"批注要不要参与出题"，那是 ADR-0015/0021
  的地盘，本 ADR 不碰。
- **`export_service` 的注记多了一句**。报告尾部的说明从"批注 N 条"变成
  "批注 N 条 + 回退 X 次（规则）+ 反思另有 Y 条（只记录）"，读的人要知道
  "Y 条没被修"是设计而不是漏修。
- **`live_check.py` 的输出格式变了**。它原本把两批问题混排；现在分两段打印，
  下游若有脚本按旧格式解析要跟着改（仓库内没有这种脚本，都在 `scripts/` 里人工看）。

## 后续影响

- **AGENTS.md §4 节点契约第 4 条要改**：原来写"`validate` 仍写 `validate_issues` + 自增计数"，
  现在要写成"只有规则问题写 `validate_issues` 并驱动回退，反思问题走 `reflect_issues`
  与 `errors`，不进计数"。
- **`state.AgentState` 多一个通道**（`reflect_issues: list[str]`，最后写入者胜，
  与 `validate_issues` 同型）。它不是累积通道，重跑会整批覆盖——这是对的，
  因为"这一遍看到了什么"不该跨轮累加。
- **`docs/TODO.md` §2.1 的第一条与第二条**（"回退现在是纯浪费" / "反思该不该走同一条边"）
  合并成了一条：反思那条已销，规则那条仍挂着并写明了理由。
- **`docs/OPTIMIZATION_BACKLOG.md` OPT-2.1** 记的是同一件事，措辞按本 ADR 改。
