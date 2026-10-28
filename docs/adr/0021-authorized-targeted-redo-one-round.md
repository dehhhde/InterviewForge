# ADR-0021：定向重做回路——`review → generate` 作为"显式授权、按槽位、最多一轮"的回炉边复活

日期：2026-10-28
状态：已决定（在 ADR-0015 划定的前提内补一条边，不改它的任何结论）
相关：`app/services/redo_service.py`、`app/agents/workflow.py`、`app/agents/state.py`、
`app/agents/nodes/generate_node.py`、`app/agents/nodes/review_node.py`、
`app/prompts/templates/v3/generate.{system,user}.j2`、`configs/development.yaml`、
`app/schemas/interview.py`、`app/api/v1/interviews.py`、`app/tasks/question_generation.py`、
`frontend/src/views/GenerateView.vue`、`frontend/src/api/index.ts`、
`tests/test_services/test_redo_service.py`、`tests/test_agents/test_m5_pipeline.py`、
ADR-0010（扇出形状与载荷清单）、ADR-0015（评审纯批注与其"定向重做"前提）、
ADR-0019（槽位自带 `angles`）、`docs/TASK_PLAN.md` T-404 / EXT-4、`docs/TODO.md` §2.3

## 背景

ADR-0015 拆掉质量门的时候，没有说"回炉这件事错了"，它说的是**这一种回炉错了**，并且把
正确的形状写死了：

> "定向重做"取代了"整批重掷"的位置：如果以后要自动修题，正确形状是把上一轮批注装进
> `GenerateJob` 并只重派被点名的槽位——那时 `question_batch` 会有第二个取值，
> 今天留下的休眠机制正好接住它。

同文件的"后续影响"里给这条留了 TODO §2.3 第 1 条，ADR-0019 又补上了它缺的那半边：
`plan_item` 现在自带 `angles`，所以重派时能一起带上"这题本来的角度"与"它上一版写成什么样"。
前提凑齐了，这张边就该回来——但回来时必须带着那三条区别，否则它还是盲重掷。

三份真实报告（`61dc1bd9ed95…` 等）显示的形状是"15 题 + 5 条批注"，人拿到批注之后
能做的只有手工重写这 5 题。这仍然是本次产品的主路径（`auto_fix` 默认关），
定向重做是**可选的第二遍**，不是新的默认终局。

## 决策

| 环节 | 选择 | 被否决的方案 |
|---|---|---|
| 要不要修 | 调用方显式授权：`GenerateRequest.auto_fix`（默认 `False`）→ `run_question_generation(auto_fix=…)` → 初始 state `auto_fix` 通道 → 前端开关 | 服务端按批注自己决定（那是 `should_redo` 换皮；且一次多花一轮扇出的钱，得付钱的人点头） |
| 修哪些 | **按槽位（whole-slot）**：`redo_plan` 只在"这一档里至少有一题被 `index` 点名"时重派该档，整档 `count` 题一起重出 | 按题重派（一道题一次调用等于把扇出的成本再乘一遍，且分支拿不到同档别题，重复率反而升）；按主题合并重派（`order_start` 是槽位算出来的，合并会让号位漂移） |
| 反馈放哪儿 | 装进 `GenerateJob` 新键 `previous: list[dict]`，条目是 `{stem, difficulty, source, evidence, issues}`，`issues` 是点名这一题的批注正文 | 把批注塞进 `plan_item`（槽位是 `plan` 的产物，混进运行时批注会让"同一 profile 两次跑出逐字相同的表"这条 ADR-0009 不变量失效）；把整题（含 `reference_answer`/`rubric`）带过去（喂旧答案等于请模型抄回去） |
| 点名怎么读 | `redo_service._flagged_texts`：`index=None` 的整批批注**不重派任何东西**，越界与负数序号丢弃 | 让 `index=None` 触发全批重做（那正是 ADR-0015 打死的"整批判死"形状；负数在 Python 里会静默指到批次尾部） |
| 批次形状 | 批次 2 是**一份完整的卷子**：被点名的档重出，未点名的题原样带走并把 `batch` 改写成 2（`RedoPlan.carried` → `Overwrite(value=carried)`） | 批次 2 只装重做的题（`sort_key = (batch, order_index)` 先比 `batch`，且 `list_for_interview` 默认只取 `latest_batch`，于是一份卷子只剩被点名的两题，其余凭空消失） |
| 扇出依据从哪儿来 | `generate` 节点把算好的槽位写进新通道 `fanout_slots`，`route_after_generate` 只读它 | 路由自己从 `state["plan"]` 推（第二次扇出的答案不是整份配额表，推出来的是错的；`order_start` 的算式也就分成了两处） |
| 上界 | `route_after_review` 只在 `question_batch == 1` 时才回 `GENERATE` ⇒ 结构上最多一轮 | 靠 `recursion_limit` 兜（那是一图通用的 30，成本口径不该由它决定）；加"重做次数"计数通道（ADR-0015 删掉 `review_retries` 的理由一字没变：checkpoint 与 `task.progress` 已经是痕迹） |
| 判据写在哪儿 | 全在 `app/services/redo_service.py`（`plan_slots` / `redo_plan` / `_flagged_texts`），节点与路由只调用 | 把"哪些题被点名"写进 `generate` 节点或路由函数（`order_start` 的算式与点名判据一旦分两处，"整批仍是 `planned` 题的号位"这条不变量就没地方守了） |
| 模板 | 新增 **v3** = v2 + `<previous>` 段与规则 7；`configs/development.yaml` → `v3`，production/testing 留 `v1` | 改 v2（改了就没法归因"哪版模板变差了"）；把 `<previous>` 写进 v1（生产环境还没做过一次真调对比就换措辞） |
| 端点形状 | 只开 `POST /interviews/generate` 的一个布尔字段 | `POST /interviews/{id}/redo`（重做要 `raw_text` 与 `jd_text`，而 ADR-0013 的报告头只快照了名字：岗位可以 `SET NULL` 下线、简历原文不是报告的一部分，凭 `interview_id` 反查不到依据） |
| 表结构 | **不动**：`question.batch`、`(interview_id, batch, order_index)` 唯一约束、`latest_batch` 原样复用——休眠机制被唤醒，不是新造 | 批次 2 覆盖批次 1 的行（`Overwrite` 只换 state，换不掉库里已落的行；真要覆盖得先删行，而"哪份卷子被扔掉过"就再无痕迹） |

## 理由

1. **ADR-0015 的反证现在成立了**。旧门被拆，是因为"重掷拿不到反馈"；这条边把上一轮的题
   与点名它们的批注一起装进载荷，于是第二次调用与第一次**不是同一次调用**。
   `test_the_branch_carries_the_previous_version_into_its_prompt` 钉的就是这件事：
   `<previous>` 只出现在重做轮，第一轮恒渲染 0 个闭栏。
2. **存在性判定不是比例判定**。`route_after_review` 里没有占比、没有阈值、没有"值不值得"，
   它回答的是一个二元问题——"有没有题被点名"。所以它不构成 ADR-0015 禁掉的那类判据，
   也没有引入需要调参的东西（也就没有需要写进 `configs/*.yaml` 的新阈值）。
3. **一轮是有码可证的界，不是注释里的约定**。`question_batch == 1` 这个条件让第二次
   走到 `review` 之后**必然**回 `END`：批次 2 的 `question_batch` 是 2。成本上界因此是
   "至多多一轮扇出"，而不是"至多 `recursion_limit` 轮"。
4. **完整的批次 2 是展示层的既有前提**。前端与 Markdown 导出都只读 `latest_batch`；
   "点名的档重出、其余原样带走"让那份不变量继续成立，代价只是未点名的题在库里多留一行
   历史（`batch=1` 的行不删）。
5. **授权来自调用方，因为这一轮花的是真钱**。一轮扇出实测 11 次调用、约 13.7 万 prompt
   token；把它做成服务端的一个自动决定，等于让使用者为一次他没要求的重做付账。

## 权衡与代价

- **开关开着时最多多花一轮扇出**。这是功能本身，不是缺陷；缓解是默认关 +
  前端开关文案明写代价，成本护栏（`agent.max_tokens_per_run`）照旧在**扇出之前**判，
  所以第二轮仍然要过同一道闸。
- **`auto_fix=true` 也不保证被点名的题变好**。批注是模型的判断，重做也是模型的判断，
  两遍之间没有机器可判的进步保证。因此落库后批次 2 的题仍可能带批注——展示层照旧点名。
- **`batch=1` 的行永远留在库里**。它们不再是"最新卷子"，但是"这一份卷子改过哪些题"的
  唯一证据。`question` 表因此会比旧实现大 1–2 倍（只在开授权时）；不引入删旧行的清理，
  因为那需要一个新的判据"什么时候允许销毁历史"。
- **`previous` 只带四栏加批注**。不给模型看上一版的标准答案与评分要点是有意的：
  那两栏越全，模型越倾向于"照抄再改两个字"，而 ADR-0019 已实测换皮重写正是被点名最多的一类。
  代价是重做时如果上一版答案本身是对的，模型要重写一遍（成本归入上面那条"多一轮"）。
- **`_flagged_texts` 丢弃 `index=None` 意味着"整批缺题"这类批注不触发重做**。这是对的
  （缺题不是某一题的毛病），但它是静默的：`generate` 节点的流水账会写明"定向重做 N 条槽位"，
  N 为 0 时整条路由回 `END`，所以看不到重做的人至少能看到"没重做的理由"。
- **production 拿不到收益**（`prompt_version` 仍是 `v1`，`v1` 不渲染 `<previous>`）。
  即使 API 带着 `auto_fix=true` 打生产环境，重做的槽位会照旧重派、只是不带反馈——
  那正是 ADR-0015 反对的形状。**所以这条组合在生产环境是刻意不可用的**，
  要放开得先做 T-105 的真调对比把 v3 推上 production。
- **重做端点仍欠着**。`POST /interviews/{id}/redo` 卡在报告头没有 `jd_text`/`raw_text` 快照
  （ADR-0013 的取舍），已写进 `docs/TODO.md`；现在的形状是"发起时就说要重做"，
  不是"看完报告再点重做"。

## 后续影响

- **`AGENTS.md` 两处措辞要改**：§4 节点契约第 5 条与"评审纯批注（ADR-0015）追加三条"
  都断言"`review` 是终点、已经没有路由函数了"。现状是**有一张授权驱动、最多一轮的回炉边**；
  ADR-0015 那三条禁令（不要占比/阈值/重做次数）本身一字不改仍然成立。
- **ADR-0010 的载荷清单**多了 `previous`。那份清单原本是
  `plan_item / raw_text / jd_text / parsed / roster / order_start / question_batch`。
- **ADR-0015 说的"休眠机制"不再休眠**：`question_batch` 有了第二个取值，
  `latest_batch` 有了第二个读取者，`test_the_redo_round_lands_one_complete_latest_batch`
  替它保序。当年"不许顺手删"的三条理由里，第二条现在真的用上了。
- **`order_start` 的算式收进一处**：`plan_slots`（`redo_service`）是唯一的号位来源，
  `route_after_generate` 不再自己 `sum(count)`。`_plan_items` 从 `workflow.py` 删除。
- **本卡新增的用例全部离线，没有新增任何真调**：`tests/test_services/test_redo_service.py`
  新建 18 条（号位、越界序号、整档替换、带走题改写、批次隔离），
  `test_m5_pipeline.py` 加 2 条图级验收（只重派被点名的档、批次 2 是完整卷子），
  `test_generate_node.py` 加 4 条（槽位级重做、未授权不重做、第一轮不声明 `previous`、
  只有 v3 渲染 `<previous>`），`test_agent_infra.py` 把"review 是终点"改写成参数化的
  "授权时恰好回炉一次"，任务层与 API 层各加 1 条把 `auto_fix` 的传递钉住。
  总基线数字见 `AGENTS.md` §2。
- **"看完报告再修"的入口**是这张边的下一步：它需要 `interview_id → 语料` 的来路
  （给报告头补 `jd_text` 快照，或允许从仍存在的简历/岗位反查），已挂 `docs/TODO.md`。
