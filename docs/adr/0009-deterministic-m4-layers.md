# ADR-0009：M4 三层的确定性分工——分数归规则、措辞归模型、配额归算术

日期：2026-10-20
状态：已决定（M4）
相关：`app/services/match_service.py`、`app/services/profile_service.py`、
`app/agents/nodes/{match,profile,plan}_node.py`、`app/vectorstore/retriever.py`、
`configs/default.yaml` 的 `question:` 段、计划书 M4 与 §6 风险 5

## 背景

M4 一次性交付三个节点，而它们是**一条因果链**：`matched_gaps` → `profile.dimensions[*].score`
→ `plan.items[*].count`。验收条款里那条"同一 `profile` 两次结果一致"不是三个独立要求之一，
而是整条链的性质——链上任何一环引入随机性，末端就一起抖。三个约束摆在这儿：

1. **验收是硬算术**：`sum(count) == settings.question.total`，每档与 `difficulty_ratio × total`
   误差 ≤ 1 题。这个等式不允许"模型觉得这题难一点"插进来。
2. **模型判数值不可信**是本项目已经踩过的坑：AGENTS.md §7 记了实测——`deepseek-chat` 把
   `2026-06-30` 说成"晚于今天 2026-10-16"，哪怕把 `today` 写进 prompt 也照样算错。
   打分与日期是同一类任务（离散、要复核、错了没人能看出为什么错）。
3. **向量检索还没选型**（embedding 推到阶段 6），但计划书要求 M4 就把检索分支留下，
   否则阶段 6 得回头改节点。

## 决策

| 环节 | 选择 | 被否决的方案 |
|---|---|---|
| JD 拆分 | 正则 + 栏目标题，`split_requirements()` 纯函数 | 让模型切要求（切法每次不同，缺口清单成噪声） |
| 技能对齐 | 复用简历侧同一套词典 `SKILL_ALIASES`，判 `matched/partial/missing` | 为 JD 再造一份词典（两份必然漂移） |
| 软要求 | 词典判不了的才打包给模型，**上限 12 条/次**，判定过三道闸门才回收 | 整份 JD 交模型（花钱且把已判对的重判一遍） |
| 检索分支 | `SkillRetriever` Protocol + `NullRetriever` 默认实现，节点只认接口 | 先写正则"假装召回"（降级版与真实现看起来一样，接上时无人知道哪段是假的） |
| 画像打分 | 五维 1–5 全由 `score_dimensions()` 阈值阶梯算出，带 `evidence` 依据句 | 模型给分（无法复核，且 `plan` 的配额跟着漂） |
| 画像叙述 | 模型只写 summary/strengths/weaknesses，句子必须点名一个已知维度或技能，否则丢弃并换规则版 | 让模型自由发挥（凭空冒出"沟通能力强"，下游会给简历里没有的东西出题） |
| 置信度 | `min(规则版, 模型自评)`，模型给 0 时视为"没自评" | 直接采信模型自评（普遍偏高） |
| 配额分配 | **最大余数法（Hamilton）** 分难度 + 弱项优先轮转分主题，0 次 LLM | 比例四舍五入（`sum` 不等于 total）／按主题数平均分（弱项被稀释） |
| `plan` 与模型 | `make_plan_node()` 刻意不收 `llm` 参数，测试断 `TypeError` | 固定 seed（DeepSeek 不给 seed 这个口子） |
| rubric 阈值 | 留在 `profile_service.py` 的常量表 + `RUBRIC_VERSION` | 搬进 `configs/*.yaml`（见"理由"第 3 条） |
| 缺口档位 | 四档，第四档 `unverified` 是**系统内部状态**，模型不许自己声明 | 三档 + 丢掉判不了的（缺口清单会谎称"JD 没这条要求"） |

## 理由

1. **确定性的代价要放在能算清的那一层**。`plan` 是全链唯一"结果必须是整数分配"的节点，
   所以它一点模型都不碰；`profile` 的分数被 `plan` 直接消费，所以它也不碰；模型的话只进
   两处人读的东西（缺口的 `level` 建议、画像的措辞），而这两处各自有闸门，且**坏了有规则版兜底**。
2. **`unverified` 这一档是为了让"清单"和"依据"分离**。词典判不了、模型也没给出可核对证据的要求，
   既不能当满足（假阳性），也不能删（假阴性）。所以它进 `matched_gaps`（清单完整），
   但不进 `job_fit` 的分母（不算依据），而是进 `focus_topics`（score 3，没依据的更要现场问）。
3. **阈值不搬进 yaml**：这些数字不是部署差异，是评分标准的定义——同一份简历在两台机器上
   必须打同一个分，否则"本地调出来的画像"和"服务器上跑出来的"不可比。而它又必须集中成一张表
   （`_BREADTH_STEPS` / `_YEARS_STEPS` / `_FIT_STEPS`），因为"改 rubric"是改一张表而不是改五处 if。
   版本用 `RUBRIC_VERSION` 常量随 `profile` 一起落进产物，归因看它，不看配置文件的时间戳。
   真要 A/B 两版 rubric 时，是复制一份表 + 升版本号，那时再决定它值不值得进 yaml。
4. **检索留接口而不是留 `if settings.retrieval.enabled`**：开关式的"暂未实现"会在节点里
   长出分支，而分支就是没人测的代码。Protocol + `NullRetriever` 让"无检索"成为唯一合法的
   实现状态，阶段 6 只提供一个新的类。

## 权衡与代价

- **词典外的技术名一律算软要求**，于是每条都要花一次模型判定。简历侧已有 `unmapped` 提示补
  词典，JD 侧没有——它落到 `unverified` 里，看得见但不自动。这是刻意的：JD 由用户随时上传，
  补词典这件事的决策权不在代码里。
- **模型判定有丢包**：`_MAX_UNJUDGED_PER_CALL = 12`，超出的按 `unverified` 处理并留一条
  "本轮只送 12 条"的说明。一份写了 30 条软要求的 JD 会有一多半没判，但清单不撒谎。
- **画像措辞会被闸门削掉一部分**：模型"说得对但没点名"的句子照样丢弃。宁可要一个略干瘪的
  画像，也不要一个混进未知实体的画像——后者会让 `generate` 出题问简历里没写的东西。
- **最大余数法的平局要靠固定顺序破**（`(余数降序, DIFFICULTY_ORDER 下标)` 升序）。
  平局给谁没有客观正确答案，但必须有**同一个**答案。
- **`plan` 不用模板**，`app/prompts/templates/v1/plan.*.j2` 因此成了无调用点的文件。
  仓库不是 git 库，删除不可逆，已挂 `docs/TODO.md` 等维护者定去留。
- **单测数量超过实现本身**（+128 用例 vs 三个节点约 700 行）。这类算术与闸门的代码，
  能钉住它的只有用例。

## 后续影响

- M5 `generate` 按 `plan.items` 扇出：每条 item 已经带 `topic / difficulty / count /
  dimension / source / evidence / rationale`，出题 prompt 不用再回头解释"为什么问这个"。
- M5 的回退重做会重跑 `match`/`profile`/`plan` 吗？不会——回退边只从 `review` 指回 `generate`。
  但 `plan` 的幂等仍要保住：万一将来加了"配额不足重排"的回退边，这三个节点已经是纯函数。
- 阶段 6 接 sqlite-vec：只需提供一个 `SkillRetriever` 实现，并在 `MatchResult` 侧新增
  `source="retrieval"` 的归因（`make_gap(..., source=...)` 已经留了这个参数）。
- 若将来要让模型参与打分，先升 `RUBRIC_VERSION` 并补一条 ADR——因为那会同时改掉 M4 的
  验收性质（`plan` 的确定性），不是局部改动。
