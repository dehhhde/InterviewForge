# TODO

搁置项与挂账项的记录处。规则：每条写清**被什么卡住**、**恢复后要验什么**，
免得过几天回来只能重猜一遍。

本文件记的是**里程碑挂账**（多数被外部条件卡住：缺样本、缺余额、缺定夺）。
2026-10-23 全仓审计出的**可立即动工的优化与缺陷**另记在
`docs/OPTIMIZATION_BACKLOG.md`（按 P0→P3 排，两边互引不重复）。

## 1. 扫描件 / 图片简历（视觉 OCR）— 【预留：2026-10-16 决定暂停，代码已就绪】

卡在哪：`.env` 的 `LLM_MODEL_VISION` 还是空值，且手上没有真扫描件样本。
代码路径已通（`app/parsers/image_parser.py` + `parse_node` 的 `parse_scanned_image` 分流），
离线测试用注入替身跑绿；2026-10-21 补岗位图片录入时拿合成海报真读了一次图（见 §2.6），
所以缺的是**真扫描件样本**与下面几条校准，不再是"模型能不能吃图"。
2026-10-16 用户明确"先暂时不做视觉以及扫描件提取，但进行预留任务"→ 本节整体冻结，
恢复时从第一条开始，不必回头重做已完成部分。

- [x] 定视觉模型 id，填进 `.env` 与 `.env.example` 的 `LLM_MODEL_VISION`
      （2026-10-21 定为 `deepseek-v4-flash-vision-exp`，与文本模型同一个 base_url、同一把 key，
      所以 `config.py` 不需要第二条路由。当晚拿合成海报真跑通了一次读图（见 §2.6），
      所以本节剩下的**不是"模型能不能吃图"**，而是样本、校准与成本这几条）
- [ ] 放 1 份真扫描件进 `tests/fixtures/resumes/`（图片型 PDF 或 JPG/PNG 都行）
- [ ] 真调一次，把响应录成 `tests/fixtures/llm/ocr_*.json`，之后按 §5 回放
- [ ] 校准三个凭推断定的数：`_MIN_OCR_CHARS = 20`（多少字算糊）、
      `_MAX_SIDE = 1560`（下采样上限）、`_JPG_QUALITY = 85`——只看实测，不看注释
- [ ] 验多页扫描件的页序与"跨页被切断的一行"怎么合并（当前只是 `"\n\n"` 拼接）
- [ ] 记一次成本：单页 token 与耗时，决定要不要改成 `asyncio.gather` 并发扇出
      （现在逐页串行，3 页 = 3 次往返）
- [ ] 决定 `.tif` / `.webp` 留不留：MuPDF 对多页 TIFF 是否只取首帧，没验过
- [ ] 补 M1 验收的第三份真简历（PDF 文字版 / DOCX 已通，图片这份卡在上面第一条）

已完成的离线部分，恢复时不必重做：路由分流、`parse_vision_unconfigured` /
`parse_vision_empty` / `parse_bad_image` / `parse_too_many_pages` 四条失败分支、
页数上限 20、去 alpha 与 CMYK→RGB、长边下采样。

## 2. 抽取层剩下的账（四个抽取器已上线，2026-10-16）

- [ ] **PDF 换行不是语义换行**：一行被拆成两行时本层不合并（`sections.merge_wrapped()` 只删空行）。
      真简历里"…具备 SQL / 调优实践经验。"就是这么被切成两条的——技能与项目证据因此带半截话。
      合并判据待定（下一行以标点收尾？不以栏目名/日期开头？），先按行取证据不影响命中，
      但 M4 画像读到半截句子会掉链子
      → **M2 真调已拿到硬证据**（2026-10-16，`tests/fixtures/llm/validate_liang_qingsheng.json`）：
      模型 6 条意见里 5 条是这一件事，形如 `成功验收交给市场部推；广`、`目前服务于；本公司及母公司`。
      **根因不止"不合并"**：`timeline_extractor._close()` 与 `project_extractor` 用 `"；".join(...)`
      把逐行的片段拼成 summary/highlights，于是分号被**插进词中间**——"推广"变成"推；广"。
      恢复后要验：先去掉/改判这个连接符（行末无标点就不该补标点），再看模型意见是否归零
- [ ] `project_extractor` 把分组标题行当成果：真简历的 `1. 权限管理体系`、`2. 智能审批工作流引擎`
      是编号小标题，不是 highlight，现在全进了 `highlights`（模型也报了这个）。
      判据候选：以编号开头且句内没有动词/逗号 → 当子标题处理并开新桶
- [ ] `SKILL_ALIASES` 是硬编码词典：真简历实测漏了 `CompletableFuture` / `JUC` / `Starter` / `SDD`。
      换一份简历就会漏更多——补词典，或者让 `unmapped` 非空时再花一次 token 问模型
- [ ] 教育经历的归属已定：归 `timeline_extractor`（同一种字形，M2 的自洽校验也要两段一起看）。
      若 M2 之后还要独立的"学历校验器"，再从 `TimelineResult` 里拆出去
- [ ] `ResumeSchema` 的联系人四项没有 evidence 槽位（只有 skills/educations/work/projects 有）。
      要不要给 name/email/phone 补证据字段，等 M2 校验层需要归因时再定，别提前改契约
- [ ] `scripts/eval_extraction.py` 还没写（M1 验收的最后一项），`tests/golden/` 也还没有
      人工标注的 `resume_*.json`。标注得手写——拿抽取结果当标准答案，P/R/F1 就是自证
- [ ] 只有一份真简历样本。DOCX 路径靠合成的 .docx 测过，没拿真人简历验过

## 2.1 校验层剩下的账（M2 上线，2026-10-16）

- [ ] **回退现在是纯浪费**：parse 是确定性的，同一份简历重跑必然给出同一个 `parsed`，
      所以一条规则问题会把 `validate_max_retries` 的预算原样烧完再放行（实测 3 轮同一句话）。
      **2026-10-16 真调已证明这不是理论问题**：真简历规则层全过（`issues=[] / repairs=[]`），
      反思却挑出 6 条**真实且 parse 修不掉**的语义问题，于是 `validate_retries=1` →
      整图白跑 3 轮 parse，每轮结果一模一样。M4 之后每个候选人都会白付这 3 轮钱。
      真正的修法是把 `validate_issues` 当提示喂回抽取环节（例如只重抽被点名的字段），
      但那要改 `route_after_validate` 与 parse 的入参约定——**卡在 M3 之后**，
      在此之前别把 `validate_max_retries` 调大，那只是多烧两轮同样的钱
- [ ] 反思环节挑出的问题该不该走同一条回退边？现在 `validate_issues` 同时装着
      "规则判的（parse 有可能修）"和"模型挑的（parse 修不了，是抽取层的设计缺口）"。
      分家（例如只有规则问题才自增计数）比继续调 prompt 更有效，但会改计划书 M2 的约定，
      **要先定夺再动手**
- [ ] 候选规则：`educations[0].graduate_year` 晚于 `work_experiences[*].start` 意味着"在校即全职"，
      真调时模型主动报了这条——它是纯日期比较，应该由规则层算而不是问模型
- [ ] 四条规则只能查"结构自洽"，判不出"看着干净其实抽错了"（公司名填进学校这类全靠那一次
      LLM 反思，而它又被"必须点名已知字段"的闸门锁住）。要往这里加力气，先补
      `scripts/eval_extraction.py` + 黄金标注（§2 已挂），拿到 P/R/F1 再决定加哪条规则，
      别凭感觉堆校验
- [ ] `DEGREE_ALIASES` 缺 `MBA` / `EMBA`（`DEGREE_WORDS` 认得出但归一成 `other`），
      所以 validate 会对写 MBA 的简历报一条"别名表缺口"。补进 `app/schemas/resume.py`
      的表这条 issue 就消失——**恢复后要验**：真简历里带 MBA/中专/高中 的样本各跑一次，
      确认报的是补映射而不是报 issue
- [ ] 证据核对在校验层是第二道闸：`parsed` 由 `ResumeSchema` 产出时抽取器已经核过一遍，
      所以真实链路上 `repairs` **应该永远是空**。哪天真出现 `repairs`，说明抽取器绕过了核对，
      那是抽取层的 bug——去查 `evidence_belongs` 的调用点，别放宽校验层的容忍度
- [x] 反思环节的 prompt 真调过了（2026-10-16，`app/scripts/live_check.py`，3 轮共 6 次调用）。
      结论：模型不会算日期，`today` 给了也白给，所以时间判断全留给规则层；
      响应已录进 `tests/fixtures/llm/validate_liang_qingsheng.json` 并配回放测试
- [x] `live_check.py` 目前只跑 parse + validate。M5 之后把 generate/review 也接进去，
      一次跑完整 7 节点才算真冒烟（那时也要顺手把 token 上限护栏接上，见 M6）
      —— 2026-10-20 判定**由 M6 侧满足**：跑完整图用 `scripts/run_e2e_live.py` 与
      `scripts/eval_e2e.py`（两者都走 `app.tasks.question_generation.make_live_nodes`，
      成本护栏随 `generate` 一起挂上）。`live_check.py` 保持只做 parse + validate：
      它的独有价值是**录回放 fixture**，接完整图会把每次真调响应都变成要维护的样本

## 2.2 M4 匹配 / 画像 / 配额的账（三节点上线，2026-10-20）

- [ ] **sqlite-vec 版的 `SkillRetriever`**：现在只有 `NullRetriever`（恒召回 0 条），
      `match_node` 走的是无检索降级版。
      卡在：embedding 选型未决（见 §3 第 3 条），没有向量就没有召回。
      → 恢复后要验：① 提供一个真实现并让 `tests/test_agents/test_match_node.py` 里那条
      "记录型假向量库"的用例换成真库跑一遍，`top_k_final` 的传参形状不许变；
      ② `make_gap(..., source="retrieval")` 的归因要能在 `matched_gaps` 里分开统计，
      三种判据（词典 / 模型 / 检索）的召回率必须能分别算——分不开就等于没接
- [ ] `app/prompts/templates/v1/plan.*.j2` **没有调用点**：`plan` 是纯算术节点，`make_plan_node()`
      刻意不收 `llm` 参数（有测试断 `TypeError`）。
      卡在：仓库不是 git 库，删文件不可逆，得维护者点头。
      → 恢复后要验：若决定留作将来"让模型解释配额"的入口，需升版本目录并补节点接线；
      若删，记得同步 `tests/test_prompts_and_decorators.py` 的 `CONTEXT[PLAN]` 与
      `PIPELINE` 无关的那份模板遍历断言，否则模板数与 `CONTEXT` 键数会对不上
- [ ] **rubric 的三张阈值表没拿真简历校准过**（`_BREADTH_STEPS` / `_YEARS_STEPS` / `_FIT_STEPS`）。
      现在的是"看着合理"定的：15 项技能给 5 分、8 年给 5 分、命中率 0.9 给 5 分。
      卡在：`tests/golden/` 还没有人工标注（§2 已挂），没有对照就调参是自证
      → 恢复后要验：拿 3 份真简历跑 `score_dimensions`，把五维分数与"人眼看着该几分"并排看，
      偏离 ≥ 2 档的才动表；动表必须同时升 `RUBRIC_VERSION`，否则 trace 里的归因会指向错的版本
- [ ] **match / profile 还没拿真人 JD 调过**。两条链路的模型分支（软要求判定、画像叙述）目前只有
      替身与闸门测试。
      2026-10-20 更新：完整图已经真调过（M5 对照跑 + M6 后台链路各一次），所以"一次都没跑过"
      不再成立；但配的仍是 `app/scripts/seed.py` 的合成岗位，本条下面两个观察点
      （引用被降级成 `unverified` 的比例、叙述闸门丢弃率）只在"词典正好覆盖得到"的
      理想输入下成立，所以**不勾**。
      卡在：**缺一份与真简历配对的真人 JD 文本**
      → 恢复后要验：把脚本接到 match→profile→plan，真调一次，重点看两件事——
      ① 模型给的 `evidence` 有多少比例过得了 `evidence_belongs`（若大面积被降级成
      `unverified`，说明 prompt 里的引用要求没写清）；② 叙述闸门丢弃率（若 strengths
      全被挡，闸门就太紧了）。响应录进 `tests/fixtures/llm/match_*.json` / `profile_*.json`，
      之后按 §5 回放。**要维护者提供**：一份真人 JD（贴文本或给文件路径都行）
- [ ] JD 侧的词典缺口没有提示通道：简历侧命不中的拉丁词进 `unmapped`，JD 侧命不中的直接
      算软要求、花钱问模型。卡在：不确定该"提示补词典"还是"就该问模型"，两者成本要实测比较
- [ ] `profile_node` 的模型叙述**始终会花 1 次调用**，哪怕 rubric 结果一句话就能说清。
      没做"输入太薄就不问"的降级门。要不要加，取决于真调后的单位成本（上一条做完就知道了）

## 2.3 M5 出题 / 评审的账（两节点上线，2026-10-20）

- [ ] **定向重做（只补被点名的槽位）**——ADR-0015 拆掉质量门之后，这条从"省钱优化"升格为
      "自动修题的唯一路径"。现在 `review` 只出批注、不再唤起 `generate`，所以批注本身不会
      让任何题变好；要让它变好，得把上一轮的 `review_issues` 装进 `GenerateJob` 载荷，
      并只重派被点名的槽位。
      卡在：① `Send` 载荷要包含"本批已收的题目"，会同时推翻风险 6 的"分支看不见旧题"这条
      隔离，得先想清楚怎么**只给同槽位的旧题**；② 需要一个"要不要自动修一轮"的新开关，
      它不是把 `should_redo` 请回来（阈值判据已随 ADR-0015 删除，`question_batch` 恒为 1）。
      → 恢复后要验：`question_batch` 会出现 2，`test_m5_pipeline.py` 那条
      "一次成卷 = `generate` 恰好扇一次"的断言要跟着改；落库侧 `latest_batch` 换批是现成的。
- [ ] **同主题多难度档的考察角度归 `plan` 管**。`plan.items` 现在只给 `topic`+`difficulty`，
      TypeScript 的 easy/medium/hard 三档因此会各自写出"那道最经典的题"，`review` 稳定报
      换皮重复（实测第三跑 4 条批注里 3 条是这一类）。现在的缓解是出题载荷带 `roster` 摘要
      （同批别的槽位在问哪个档）+ prompt 硬规则，属于间接手段。
      卡在：正经解法是给配额表加 `angle`（考察角度）字段，那是 M4 的产出形状，会牵动
      `profile.focus_topics` 与 ADR-0009 的确定性验收。
      → 恢复后要验：加了 `angle` 之后 `roster` 能不能退掉（它能，就不该留两份重复信息）。
- [ ] **批注该不该按题号合并成"这题有几条意见"**。门没了，"一题两报顶穿阈值"这个代价消失了，
      但展示层现在会同一题挂两条黄条。没做的理由：合并会丢掉"这道题错得离谱"的严重度信号。
      卡在：要一份真人标注的批注样本才知道面试官读的是"条数"还是"题数"。
      → 恢复后要验：拿 3 份真简历各跑一次，统计"每题批注条数"分布；若普遍是 1，就直接合并。
      （原 `fail_ratio` 分子口径这条账已被 ADR-0015 吸收——分子分母都不再参与判定了。）
- [ ] **`state` 里没有 `job_id`**。落库只按 `interview_id`（调用方在开跑前建好报告头），
      报告与岗位的关联只能从 `jd_text` 反查。卡在：API/后台任务那层还没有 `job_id` 入口
      （M6 建 `tasks/` 时才有），现在加是空字段。
      → 恢复后要验：M6 起 `POST` 入口时一并加进 `AgentState`，并让 `run_e2e_live.py` 传它，
      别把关联留在 `errors` 的流水账里。
- [ ] **真简历只跑过一份**。验收是在 `附件简历-梁清生.pdf` + 种子岗位 1 上拿到的
      （15 题 / 引用 100% / 1 轮回退 / `completed`）。两份不同的简历（尤其是一份 JD 要求
      几乎都在词典里的、一份大量软要求的）没跑过。
      卡在：`docs/TODO.md` §2.2 那条"要维护者提供真人 JD"仍未交付第二份样本。
      → 恢复后要验：`scripts/run_e2e_live.py <pdf> [job_id]` 各跑一次，只看两个数——
      `带可回溯引用比例` 与 `批注点名了几题`；若引用比例掉到 90% 以下，先查 `errors` 里
      "丢弃一题"的原因分布，再动 prompt。
- [ ] **评审模型的误报只做了个案修补**。两轮真跑分别暴露"看不见 JD 就把合法引用判成幻觉"
      和"自己算日期先后"，都已在 `review.system.j2` 里禁掉；但这两类是**同一族**问题
      （评审缺少判据所需的输入 / 被鼓励判它判不了的机械事实）。
      → 恢复后要验：换一份简历跑时先读 `errors` 里的批注，凡"程序已经核对过"的类别再出现，
      就归到这一族里补 prompt——ADR-0015 之后没有阈值可调，也没有重试可甩锅。

## 2.4 M6 后台化与评测的账（2026-10-20）

- [ ] **`eval_e2e.py` 的三个指标还没有真实数字**。脚本与算式都到位（可用率 = 题目过
      `QuestionSchema` 再校验且引用核得上；幻觉率 = 引用核不上的占比；换皮重复单独计一列），
      算式用合成终态离线验过（可用 1/4、幻觉 1、重复 1）。
      卡在两件事：① **DeepSeek 账户余额不足**——实测上游回 `402 Insufficient Balance`，
      第三次真跑死在这里，前两次也因为下面那条门限抖动没走到落库；
      ② **样本简历只有 1 份**（`附件简历-梁清生.pdf`），计划书要求的 10 份没有素材。
      → 恢复后要验：先跑 `scripts/run_e2e_live.py` 确认链路活着，再跑
      `scripts/eval_e2e.py`，把三个数字写进计划书 M6 那一段；样本凑不够 10 份时脚本会打印
      "样本不足"警告，**不要**把那份数字当 10 份的结论。
      **要维护者提供**：账户充值 + ≥9 份可用的简历样本（PDF/DOCX，放仓库外给路径也行）。
- [x] **评审门在阈值附近抖，"拿到 15 题"这条验收因此不稳**——2026-10-24 按 ADR-0015
      直接拆掉门来解。当时的现场：同简历同 JD 两次后台真跑分别 6/15=0.40、5/15=0.33 判死
      （阈值 0.3，`interview=aborted`、零题落库），而对照 `run_e2e_live.py` 是 PASS
      （15 题、3 条批注、引用 100%）。**门一删，"什么时候算合格"这个问题就不再需要回答了。**
      两类根因仍然挂着，只是从"能不能发"降级成"批注好不好看"：
      ① `plan` 给同主题的多个难度档各留槽位，TypeScript / 两栏布局这类反复出现换皮题
      （→ §2.3 的 `angle` 那条）；
      ② 题干写"至今/目前"这类**日期相对措辞**，评审没有"今天"这个输入就判它无法作答
      （→ §2.3 的"评审误报只做了个案修补"那条，同 AGENTS.md §7"别让模型判日期"一族）。
- [ ] **`BackgroundTasks` 的任务会失联**。进程重启，排队中与运行中的协程就地消失，
      `task` 行永远停在 `pending`/`running`——没有心跳，也没有超时回收。checkpoint 落在
      `data/checkpoints.db`，理论上能从断点续跑。
      卡在：仓库里没有任何"从 checkpoint 恢复线程"的入口，也没有第二个进程能回收僵尸行；
      真要解决属于"任务要不要挪出进程"的决策，加个字段解决不了。
      → 恢复后要验：先加一个清扫入口（CLI 或 `POST /tasks/reap`）把 `running` 超过 N 分钟的
      任务标 `failed`，并实测同一 `thread_id` 续跑能否接上断点。
- [ ] **`task.status == completed` 不等于报告可用**。ADR-0015 之后两者终于对齐了
      （`review` 跑到哪一步都落 `interview.status=completed`，中途崩才记 `failed` + `aborted`），
      但"可用"仍然要读**内容**：整批 0 题也会是 `completed`，批注点名的题也照样在卷子里。
      真判据是 `result.finished` + `interview.total_questions > 0` + `review_issues` 的条数。
      卡在：要不要把这层判断挪进 `GET /tasks/{task_id}` 的响应（那是契约改动，动了客户端读
      进度的方式），还是让读的人自己去看报告头。要维护者定夺。
      → 恢复后要验：若决定让"0 题"报成可见的降级态，`GET /tasks/{task_id}` 与 `GET /interviews/{id}`
      两处要同时改，别只动一边留下两种真相。
- [ ] **`GET /metrics` 没实现**（计划书阶段 8 列过它）。现在三指标只能靠脚本跑真调拿，
      没有常驻可观测面。
      卡在：不在 M6 产出清单里，且真实数字还卡在本节第一条的余额与样本上。
      → 恢复后要验：等 `eval_e2e.py` 能稳定出数字后，把它的输出落成一张 `eval_run` 表再由端点读；
      **不要在端点里现跑评测**（一次就是几十次上游调用、80 秒）。
- [ ] **认证与限流仍没接线**：`app/middleware/{auth,logging,rate_limit}.py` 是空壳，
      `create_app()` 只挂 CORS；所有表都没有 `user_id`，所以 `GET /tasks/{task_id}` 与
      `GET /interviews/{id}` 是**知道 id 就能读**。
      卡在：与 §3 的 `app/models/user.py` 同一条账（属阶段 2 欠账）。
      → 恢复后要验：加列是**新迁移**（`resume`/`interview` 都要补 FK）；限流按计划书 §6 风险 3
      的结论单机先不引入，真引入时先给 `POST /interviews`（它一次烧几十次上游调用）而不是全局。
- [ ] **图片/扫描件走后台链路必须有 `LLM_MODEL_VISION`**。`run_resume_parsing` 复用
      `make_parse_node`，分流逻辑已在，但配置为空时用户上传图片只会拿到
      `parse_vision_unconfigured` 记进 `task.error`。卡在：同 §1（视觉模型 id 未定）。
      → 恢复后要验：配好模型后真上传一张 JPG，走 `POST /resumes` → 轮询 → 读 `resume.parsed`
      三段全链路，而不只是单测节点。

## 2.5 写入侧的账（岗位/简历 CRUD + 报告快照，2026-10-21）

落地的部分：`POST/PUT/DELETE /jobs`、`POST /jobs/upload`、`PUT/DELETE /resumes/{id}`、
`interview` 的四列简历·岗位快照、`ConflictError`（409）、控制台写侧。决策与实测事故见
`docs/adr/0013-job-resume-crud-and-report-snapshot.md`。

- [ ] **`POST /match` 仍不存在**：阶段 6 的"这份简历配哪些岗位、按相似度排序"没有端点。
      卡在：embedding 选型未决（§3）， sqlite-vec 侧只有 `NullRetriever` 实现。
      → 恢复后要验：`match_service` 的判据（`SKILL_ALIASES` + `split_requirements`）
      已经被 `POST /jobs` 的技能派生复用，检索层接上去时**别再引第二本词典**。
- [ ] **迁移形状没有机器护栏**：ADR-0013 §2 要求给已有表加 NOT NULL 列走裸
      `ALTER TABLE … DEFAULT`，禁用 `op.batch_alter_table`（实测它 `DROP TABLE` 重建 +
      `PRAGMA foreign_keys=ON` + `question` 的 `ON DELETE CASCADE`，级联删空过 30 行题目）。
      现在只写在 ADR 与 `AGENTS.md` 里。
      → 恢复后要验：在 `tests/test_migrations.py` 加一条静态断言（扫
      `migrations/versions/*.py` 里 `batch_alter_table` 的出现并拒绝，或要求它带
      `recreate="never"` 注释），别靠人记住。
- [ ] **409 是"先查后插"判出来的，不是约束兜出来的**：并发下两个请求可能都查空、
      后插的那个撞 `uq_job_title_company` 变 500。单机 SQLite 单写者，眼下接受。
      → 恢复后要验：真上多进程时在 `JobRepository.create` 捕 `IntegrityError` 翻成
      `ConflictError`，并把这条从"约定"降级为"防御"。
- [ ] **孤儿上传文件没人清**：`force=true` 删简历时文件删除失败只是忽略（留痕都没有），
      上传后从未成功解析的简历也占着 `data/uploads/`。
      → 恢复后要验：写一个 `app/scripts/prune_uploads.py`，判据是
      "`upload_dir` 里的文件名对不上任何 `resume.stored_path`"，先跑 dry-run 报数。
- [ ] **`GET /jobs` 现在每条都带 `jd_text`**：JD 几 KB，列表 50 条就是几百 KB，且页面上
      岗位卡片全量渲染正文。没分页只有 `limit/offset`。
      → 恢复后要验：真要长列表时给列表端点加 `?fields=` 或单独的 `GET /jobs/{id}` 取正文，
      别把 DTO 拆成两份真相。
- [ ] **控制台仍然无鉴权、无分页**：与 §2.4 的认证欠账同一条；`?limit=100` 是写死的。

## 2.6 岗位图片录入的账（`POST /jobs/inspect-image`，2026-10-21）

落地的部分：图片 → `ImageParser`（复用 `ocr@v1`）→ `job_extractor` 三层字段来源 →
`JobDraftOut` 草稿 → 页面灌表单 → `POST /jobs` 落库。决策见
`docs/adr/0014-job-image-intake.md`。**全部离线测试**（485 passed 里 22 条是这一节的）。

2026-10-21 拿合成海报真跑通过一次（`deepseek-v4-flash-vision-exp` 可用，账号 402 只在文本模型上
撞到过），一次调用出草稿：`title`/`company`/`min_years` 三项全对（`3-5 年`→`3.0`）。
但同一张图暴露了两个实测问题，都记在下面。

- [ ] **视觉模型把拉丁字母逐个加空格**：实测 OCR 出来是 `P y t h o n` / `R e d i s` / `My`，
      于是 `derive_required_skills` 只认出 `["高并发"]`，Python/Redis/MySQL 全漏，
      下游 `match` 的缺口判定就少三条要求。
      卡在：修法未定——在 OCR 层折叠"单字母+空格"是最省事的，但它会同时改坏 `Base 上海`
      这类本来正确的排版；在词典侧忽略空格又要动 `SKILL_ALIASES` 的判据（与 `match` 共用的那条）。
      → 恢复后要验：先量一张真海报的 OCR 文本形状再选边，改完必须重跑
      `tests/test_services`（`match` 侧的判据不能分叉）。
- [ ] **空白图回的是模型的元话**：实测一张纯灰度图 OCR 出 `（图片中未出现可识别的文字）`，
      14 字 ≥ 5 字的门槛，于是这条短语原样进了 `jd_text`。这条链路靠 `missing_required`
      把用户拦在了保存之前，所以不算脏数据，但草稿正文很莫名。
      → 恢复后要验：要么把 `jd_too_short` 的门槛与 `_MIN_OCR_CHARS`（§1 也在等同一条校准）
      一起按实测提高，要么让 `ImageParser` 认这种"自述没读到字"的回复并直接抛
      `parse_vision_empty`。**别用关键词列表去黑名单模型的措辞**，那是第二套判据。
- [ ] **一张真海报都没试过**：上面那次是 pymupdf 渲染的合成图（字体、版式、噪点都是最理想情况）。
      → 恢复后要验：拿 1 张真实招聘海报走 `POST /jobs/inspect-image`，把响应录进
      `tests/fixtures/llm/job_image_*.json` 按 §5 回放；重点看多栏海报被读成什么顺序，
      `_TITLE_CUT_RE` / `_ROLE_KEYWORD_RE` 这两条启发式在新版式上还顶不顶用。
- [ ] **海报标题启发式的误报率没有数字**：`_title_from_heading` 只看前 3 行、要求含职位词、
      不含冒号、不是列表项。现在只有单测里那几个形状，没有"真海报认错了几个"的统计。
      → 恢复后要验：脚本化跑 10 张图报"认错 / 认不出"两个数，**只看不钉**（与
      `scripts/eval_extraction.py` 同一条策略）；认错的代价是自然键脏值，不是崩溃。
- [ ] **同步等待没有上限**：一次录入 = 1 次读图 +（至多）1 次补字段，`request_timeout_seconds`
      60 秒是 `llm_client` 层的，端点级没有超时，浏览器 fetch 也没设。
      → 恢复后要验：若实测常态 > 30s，考虑照简历侧改成 202 + `task` 行（ADR-0011），
      但这会引入"草稿要不要落库"的新问题，先量时间再决定，别提前建表。
- [ ] **`required_skills` 仍只走词典**：OCR 把 `Redis` 糊成 `Red1s` 时技能就漏了，而 JD 侧
      漏一项等于 `match` 少一条缺口判定。目前没有任何提示通道（§2.2 同一条账）。
      → 恢复后要验：先在草稿的 `notes` 里报"这一行像技能但词典没有"，别急着让模型补。
- [ ] **扫描件 PDF 仍进不了岗位链路**：`allowed_image_suffixes()` 不含 `.pdf`（ADR-0014 §5），
      `POST /jobs/upload` 碰到图片型 PDF 会拿 `parse_scanned_image` 报错。
      → 恢复后要验：分流规则写在 ADR-0008（编号仍预留），落它时把两条路合并成一次判断，
      别在两个端点各写一套后缀分支。

## 3. 仓库挂账（与本里程碑无关，但别忘）

- [ ] `pyproject.toml` 的 `dependencies` / `dev` 仍是空的，与 `requirements*.txt` 脱节
- [ ] `README.md` 与 `project_structure.md` 还在说 Postgres / pgvector / Celery，
      实际是 SQLite + sqlite-vec + `BackgroundTasks`（见记忆 No-Docker embedded infra）
- [ ] embedding 选型未决：`fastembed` 本地跑 vs 暂时不做
- [ ] `LEARNING_ROADMAP.md` 缺失
- [ ] 仓库根新增 `AGENTS.md`（2026-10-16，AI 协作者的执行约定）。它与 `README.md` 的
      「约定」章节有重叠，README 那边应改成一句话指过去；全局技能
      `interviewforge-build`（`~/.qoder-cn/skills` 与 `~/.agents/skills` 各一份）是它的副本，
      **改 AGENTS.md 记得同步它**，否则两处规矩会分叉
- [ ] 真简历 `附件简历-梁清生.pdf` 含姓名/手机/邮箱，是否长期留在仓库待定（PII）
- [ ] ADR-0001 ~ 0005 落文件，A 两套编号（§8 短号与 `docs/adr/README.md` 的 000x）并存的收尾
- [ ] ADR-0008 扫描件走多模态 LLM 而非本地 OCR，理由与代价正式成文
      （编号从 0007 改成 0008：0007 已被 M3 的持久化层决策占用，见 `docs/adr/0007-sqlite-persistence-layer.md`）
- [x] `app/dependencies.py` 接上了（2026-10-16 同日，M3 收尾）：`get_engine()` 引擎单例 +
      `get_sessionmaker()` + `get_db` 生成器依赖 + `dispose_engine()`。
      原先卡在"M3 产出清单里没有它，且没有消费者"——现在它有了两个消费者：
      `app/scripts/seed.py`（进程内跑脚本）与 `tests/test_dependencies.py`（真 ASGI 应用）。
      实测判据：`grep -rn "build_engine(" app` 只命中 `app/dependencies.py:42`
      （测试里那 4 处是造对照用的，刻意为之）
- [x] `get_db` 还没被真实 API 端点用过 —— **2026-10-20 M6 已闭环**：`app/api/v1/` 除
      `GET /health` 外的八个端点都吃会话依赖，读用 `SessionDep`、唤起后台任务的两个 POST 用
      `SessionMakerDep`（`Annotated` 别名而不是参数默认值，默认值形式会撞 ruff B008），
      `app/main.py` 的 lifespan 收尾调 `dispose_engine()`。
      M6 追加的一条不在原计划里：`BackgroundTasks` 跑在依赖收尾**之前**（实测，
      `scripts/probe_background_order.py`），所以那两个 POST 的提交由服务层自己扛
      —— 见 `docs/adr/0011-backgroundtasks-and-task-progress.md`
- [ ] `app/models/user.py` 仍是空壳：四张表里没有任何 `user_id` 外键。
      卡在：认证属阶段 2，计划书 M3 明确不含它
      → 恢复后要验：给它加列是**新迁移**（`resume`/`interview` 都要补 FK），
      别指望 `alembic revision --autogenerate` 能看见没 import 的模型
