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
- [x] 记一次成本：单页 token 与耗时，决定要不要改成 `asyncio.gather` 并发扇出
      （现在逐页串行，3 页 = 3 次往返）——已改 2026-10-27（T-014，TASK_PLAN 的判据是"3 页
      串行 = 3 倍延迟"，不必再等成本数字）：`gather` + 按提交顺序回填页号，限流仍只有
      `LLMClient` 那把 `Semaphore`。**仍欠**：真扫描件下单页 token 与墙钟耗时的实测（卡 D-1/D-2），
      以及"跨页被切断的一行"合并（上一条）
- [ ] 决定 `.tif` / `.webp` 留不留：MuPDF 对多页 TIFF 是否只取首帧，没验过
- [ ] 补 M1 验收的第三份真简历（PDF 文字版 / DOCX 已通，图片这份卡在上面第一条）

已完成的离线部分，恢复时不必重做：路由分流、`parse_vision_unconfigured` /
`parse_vision_empty` / `parse_bad_image` / `parse_too_many_pages` 四条失败分支、
页数上限 20、去 alpha 与 CMYK→RGB、长边下采样。

## 2. 抽取层剩下的账（四个抽取器已上线，2026-10-16）

- [x] **PDF 换行不是语义换行**：一行被拆成两行时本层不合并（`sections.merge_wrapped()` 只删空行）。
      真简历里"…具备 SQL / 调优实践经验。"就是这么被切成两条的——技能与项目证据因此带半截话。
      合并判据待定（下一行以标点收尾？不以栏目名/日期开头？），先按行取证据不影响命中，
      但 M4 画像读到半截句子会掉链子
      → **M2 真调已拿到硬证据**（2026-10-16，`tests/fixtures/llm/validate_liang_qingsheng.json`）：
      模型 6 条意见里 5 条是这一件事，形如 `成功验收交给市场部推；广`、`目前服务于；本公司及母公司`。
      **根因不止"不合并"**：`timeline_extractor._close()` 与 `project_extractor` 用 `"；".join(...)`
      把逐行的片段拼成 summary/highlights，于是分号被**插进词中间**——"推广"变成"推；广"。
      恢复后要验：先去掉/改判这个连接符（行末无标点就不该补标点），再看模型意见是否归零
      ——已修 2026-10-28（T-102，**只做了连接符这半边**）：判据收进 `app/extractors/base.py::join_wrapped_lines()`
      （行末已有 `。；，、！？：` 及半角同族就不补，否则补**一个空格**），`timeline_extractor._close()`
      与 `project_extractor._build_project()` 的**桶内**拼接都走它；桶间（描述 / 技术栈）仍用"；"，
      因为那是真话题边界而不是版式换行。实测（真简历走 `PdfParser`）：工作摘要与两条项目描述抹掉空白后
      **整段命中原文**（`…_invents_no_punctuation` 两例：工作摘要 1 条、项目描述 2 条各跑一遍）——改之前是 `推；广`、`服务于；本公司`。
      判据为什么只补空格：空格在 `squeeze()` 里会被抹掉，所以它不声称原文此处有过标点；
      分号则是替原文造了一个不存在的话题边界。
- [ ] **换行合并（上一条的 ②）没做**：`sections.merge_wrapped()` 仍然只 strip + 丢空行，
      所以录制件里"鉴权接 / 口平均响应时间""递问题，Caffeine TTL…"这类**半截 highlights** 原样留着。
      判据候选仍是"下一行不以栏目名/日期开头、且上一行不以句末标点收尾"。
      卡在：**要 T-101 的黄金标注（D-1 真简历 ≥3 份）**——卡片原话"不要凭注释推断"，一份样本
      上的合并率不足以定阈值。
      → 恢复后要验：合并后 `evidence_belongs` 通过率不降（现有核对断言必须仍绿），
      且技能/项目证据里没有以半个词结尾的行；合并要发生在 `split_sections` 之后、抽取器之前，
      别改 `collapse()` 的语义（证据核对与它同源）。
- [x] `project_extractor` 把分组标题行当成果：真简历的 `1. 权限管理体系`、`2. 智能审批工作流引擎`
      是编号小标题，不是 highlight，现在全进了 `highlights`（模型也报了这个）。
      判据候选：以编号开头且句内没有动词/逗号 → 当子标题处理并开新桶
      ——已修 2026-10-28（T-103）：`_is_numbered_subheading()`（`project_extractor.py:46`）＝
      `^\d{1,2}\s*[.、)）]` 前缀 + 剩余正文 ≤14 字 + 体内无任何句读（`，,、；;：:。！!？?`）
      + 不以 24 个动词前缀（`_ACTION_LEADING`：负责/实现/基于/搭建…）开头。真简历项目段实测
      **9 行编号行分对 9 行**（5 行 `1. 权限管理体系` 这类标题切桶、4 行 `1、设计…` 这类真成果入桶），
      `OA办公自动化系统` highlights 17→12、`对象存储中心` 仍 6 条、`notes` 为空。
      **不写 `notes`**：这种版式是常态不是异常，为它报流水账会把健康路径的 `errors` 变成噪音
      （标题本身仍在 `evidence` 里，人要查随时查得到）。顺带把成果桶直接建成 `highlights`，
      删掉 `highlights += outcome` 那次追加——它把"编号条目"和"被换行切断的尾巴"分成两段，
      模型据此报"错位"（录制件第 3 条）。
      还欠：误判率只有**一份**样本支撑（卡 D-1）；`_ACTION_LEADING` 是词典，换一份简历可能要补词。
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

- [x] **反思问题不再驱动回退**（2026-10-28，T-201 / ADR-0022）：`validate_issues` 只剩
      规则判出来的（parse 有可能修），模型反思挑出的 6 条语义问题改走 `reflect_issues`
      通道 + `errors` 流水账，**不自增 `validate_retries`**。实测（
      `test_reflection_issues_never_buy_a_second_parse_round`）：反思有话说时 parse 只跑 1 轮。
      **还欠**：这些反思问题本身没被任何环节消费——它们只落库给人看，"只重抽被点名的字段"
      仍是空的（归 §2.9 的载荷瘦身那条一起想，两者都要动 parse 的入参约定）
- [ ] **规则问题仍会把预算烧完**：parse 是确定性的，同一份简历重跑必然给出同一个 `parsed`，
      所以一条规则问题（日期倒挂这类）还是会把 `validate_max_retries` 跑满再放行。
      2026-10-16 的真调已证明这不是理论问题。真正的修法是把问题当提示喂回抽取环节
      （只重抽被点名的字段），但那要改 `route_after_validate` 与 parse 的入参约定。
      → 2026-10-27（T-013）先砍掉了它的**付费机制**：文件指纹（mtime+size）未变时 parse
      不再重读原件、不再重跑逐页 OCR，所以这几轮现在只花抽取的钱。**本条不算销账**：
      白跑仍是白跑，`parsed` 一模一样，只是不再贵；在此之前别把 `validate_max_retries` 调大
- [ ] 候选规则：`educations[0].graduate_year` 晚于 `work_experiences[*].start` 意味着"在校即全职"，
      真调时模型主动报了这条——它是纯日期比较，应该由规则层算而不是问模型
- [ ] 四条规则只能查"结构自洽"，判不出"看着干净其实抽错了"（公司名填进学校这类全靠那一次
      LLM 反思，而它又被"必须点名已知字段"的闸门锁住）。要往这里加力气，先补
      `scripts/eval_extraction.py` + 黄金标注（§2 已挂），拿到 P/R/F1 再决定加哪条规则，
      别凭感觉堆校验
- [x] `DEGREE_ALIASES` 缺 `MBA` / `EMBA`（`DEGREE_WORDS` 认得出但归一成 `other`），
      所以 validate 会对写 MBA 的简历报一条"别名表缺口"。补进 `app/schemas/resume.py`
      的表这条 issue 就消失——已修 2026-10-27（T-012）：别名表补 `MBA`/`EMBA`→`master`、
      `中专`/`高中`→`other`（枚举只到专科，中等学历**合法地**是 other，收进表是为了让规则层
      知道"这是认出来的"），词表补 `中专`/`高中`，拉丁字样改词边界匹配（`ABMATIC` 里的
      `MBA` 不算学历）且大小写归一（`mba` 同样归 master）。
      实测：`tests/test_extractors/test_timeline_extractor.py` 4 例 + `tests/test_services/test_resume_service.py`
      1 条参数化（四种字样全链路 issue 为 0）+ 原"别名表缺口"用例改造成"抹掉一个归属"，
      判据本身留着挡未来新增字样。
      **还欠**：真简历里带 MBA/中专/高中的样本各跑一次（卡 D-1，见 §2.4 的样本账）
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
      卡在：删除是维护者的决定（D-8）。~~仓库不是 git 库，删了没有第二条路~~ **这条前提已经
      过时**：2026-10-27 实测仓库是 git 库（`main`，1 个提交，`git ls-files` 301 个文件），
      已跟踪文件删掉可以从提交里取回；但当前所有改动尚未提交，所以现在删仍然等于真删。
      → 恢复后要验：若决定留作将来"让模型解释配额"的入口，需升版本目录并补节点接线；
      若删，记得同步 `tests/test_prompts_and_decorators.py` 的 `CONTEXT[PLAN]` 与
      `PIPELINE` 无关的那份模板遍历断言，否则模板数与 `CONTEXT` 键数会对不上
- [ ] **同族的另一组：`app/prompts/templates/v1/parse.*.j2` 也没有调用点**（BACKLOG P3-5 的后半条，
      开关那半条已由 T-107 于 2026-10-28 结掉）。`parse_node.py:87-91` 只用 `ner` 与 `ocr`。
      卡在：与上一条同一个 D-8 决定，且删之前要先改 `tests/test_prompts_and_decorators.py`
      里 5 处 `NodeName.PARSE` 引用（实测 2026-10-28 在 `:26,61,68,118,131`；模板遍历断言与
      `CONTEXT` 的键数期望跟着变）。
      → 恢复后要验：`available_versions()` 与模板计数断言仍对得上；留着当"以后让模型直接整份抽取"
      的入口也行，但那要补调用点，不然 docstring 又是一句广告。
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

- [x] **定向重做（只补被点名的槽位）**——ADR-0015 拆掉质量门之后，这条从"省钱优化"升格为
      "自动修题的唯一路径"。现在 `review` 只出批注、不再唤起 `generate`，所以批注本身不会
      让任何题变好；要让它变好，得把上一轮的 `review_issues` 装进 `GenerateJob` 载荷，
      并只重派被点名的槽位。
      卡在：① `Send` 载荷要包含"本批已收的题目"，会同时推翻风险 6 的"分支看不见旧题"这条
      隔离，得先想清楚怎么**只给同槽位的旧题**；② 需要一个"要不要自动修一轮"的新开关，
      它不是把 `should_redo` 请回来（阈值判据已随 ADR-0015 删除，`question_batch` 恒为 1）。
      → 恢复后要验：`question_batch` 会出现 2，`test_m5_pipeline.py` 那条
      "一次成卷 = `generate` 恰好扇一次"的断言要跟着改；落库侧 `latest_batch` 换批是现成的。
      〔2026-10-28 进展：T-202 落了 `angles`，这条的前置"载荷里要有依据区分各题"第一次成立——
      定向重做可以把"上一轮这题写成什么样"和"它本来的角度"一起塞进 `GenerateJob`，
      重派不再是盲重掷。开卡请走 T-404，需要新 ADR（编号 0020）。〕
      已修 2026-10-28（T-404 / **ADR-0021**，编号 0020 已被 S3 的列表形状占用）：
      ① 的答案是"**只给同槽位的旧题，且只给四栏**"——`GenerateJob.previous` 每条装
      `stem` / `difficulty` / `source` / `evidence` + 点名它的 `issues`，
      **不带 `reference_answer` / `rubric`**（喂旧答案等于请模型抄回去），
      兄弟槽位的题仍然看不见，风险 6 的隔离一字未破。
      ② 的答案是 `auto_fix` 布尔：授权来自调用方（请求体 → 后台任务 → 初始 state），
      `route_after_review` 里没有占比、没有阈值、没有重做次数，只回答"有没有题被 `index` 点名"，
      并且只在 `question_batch == 1` 时路由得回去 ⇒ **结构上最多一轮**。
      点名判据、号位与"哪些题被带走"全部收在 `app/services/redo_service.py` 一处，
      `generate` 节点把算好的槽位写进新通道 `fanout_slots`，`route_after_generate` 只读它
      （`_plan_items` 从 `workflow.py` 删除，`order_start` 不再有两处算式）。
      原卡没写但必须处理的一件事：**批次 2 必须仍是一份完整卷子**——
      `sort_key = (batch, order_index)` 先比 `batch`、列表与导出默认只读 `latest_batch`，
      所以未点名的题要**原样带走并改写 `batch`**，否则一份 15 题的卷子重做后只剩 2 题。
      恢复后要验的那两条都做了：`question_batch` 确实出现 2
      （`test_the_redo_round_lands_one_complete_latest_batch` 在库级断言批次 2 是 3 题、
      号位 0/1/2、状态 `completed`），"一次成卷"的断言改成
      `test_an_authorized_round_redoes_only_the_slot_that_was_named`（授权时恰好两轮、
      只重派被点名的档）与参数化的"授权时恰好回炉一次"（`test_agent_infra.py`）。
      **`latest_batch` 换批是现成的**这句成立，但落库幂等只认"批次号与库里最新批次不同"，
      所以第二轮收尾仍是一次覆盖式写入，没有重复行。
      **仍欠的两件**记在 §2.12：`POST /interviews/{id}/redo`（卡在报告头没快照语料）
      与"重出来的题是否真的更好"（卡在 D-2 真调）。
- [x] **同主题多难度档的考察角度归 `plan` 管**。
      已修 2026-10-28（T-202 / ADR-0019）：配额表每个槽位多一条 `angles`（**长度 = `count`，
      第 i 条给第 i 题**，不是单个 `angle`——角度如果是个标量，重复就能挪到槽位内部去）。
      角度按难度分池（`ANGLES_BY_DIFFICULTY`：easy 名词/场景/用法/差别，medium 原理/落地/配置/误用，
      hard 取舍/瓶颈/复盘/代价），由 `(difficulty, 题序)` 查表取模，不看随机数也不看时间，
      所以 ADR-0009 的"同一份 profile 逐字节相同的表"照旧成立。
      消费侧只改 v2 模板（`generate.user.j2` 多一行"每题角度"，`generate.system.j2` 规则 6
      从"猜兄弟槽位在问什么"改成"照给定角度问"），`roster` 保留并升格。
      **卡里那句"加了 `angle` 之后 `roster` 能不能退掉"的答案是不能**，而且理由值得记下来：
      角度起作用靠的正是"和别人的不一样"，而兄弟分支出的题是 `Send` 分支唯一看不见的东西
      （实测 langgraph 1.2.15，`scripts/probe_send_fanout.py`）——退了 `roster` 等于让模型拿着
      自己的角度去猜别人有没有撞车。这不是留了两份重复信息，`roster` 现在给的是"别的槽位
      打算从哪儿问"，与 `angles` 给自己的"我该从哪儿问"是同一张表的两面。
      **它的效果离线证不了**：`angles` 不进 `question` 表、没有任何规则层消费它，唯一作用是
      改变模型的提问方向，所以"重复率降了"要真调才有数字（卡 D-2）→ 记进 §2.9。
      原描述：`plan.items` 现在只给 `topic`+`difficulty`，
      TypeScript 的 easy/medium/hard 三档因此会各自写出"那道最经典的题"，`review` 稳定报
      换皮重复（实测第三跑 4 条批注里 3 条是这一类）。现在的缓解是出题载荷带 `roster` 摘要
      （同批别的槽位在问哪个档）+ prompt 硬规则，属于间接手段。
      卡在：正经解法是给配额表加 `angle`（考察角度）字段，那是 M4 的产出形状，会牵动
      `profile.focus_topics` 与 ADR-0009 的确定性验收。〔实际未牵动 `focus_topics`：它在
      `profile` 层，配额表在 `plan` 层，加字段只往下传，2026-10-28〕
      → 恢复后要验：加了 `angle` 之后 `roster` 能不能退掉（它能，就不该留两份重复信息）。
      〔已答：不能，见上面 已修 块〕
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
      → 2026-10-27（T-015）**清扫入口已落地**：`task_service.reap_stale()` +
      `python -m app.scripts.reap_tasks`（默认 dry-run，`--apply` 才翻 `failed` +
      `task_stale`；阈值 `configs/*.yaml` 的 `task.stale_after_minutes`）。
      `POST /tasks/reap` **没做**——写端点要鉴权，卡在 D-7（单机 vs 多租户）没定。
      **本条不算销账**：失联本身还在，还欠两件事——① 同一 `thread_id` 从 checkpoint 续跑
      （现在只是把僵尸标成失败，题不会自己补上）；② `pending` 要不要进回收口径（现在只收
      `running`，理由写在 `reap_stale` 的 docstring 里）。
      另附一条实测发现：回收只改 `task` 行，`interview.status` 会停在 `running`，
      报告列表里就是"永远在生成中"的那一行——要不要顺手翻成 `aborted` 归 T-309 一起判。
- [x] **`task.status == completed` 不等于报告可用**（2026-10-28，T-309 / D-6(a)）：
      `GET /tasks/{id}` 现在带四个派生字段——`report_status` / `question_count` / `degraded` /
      `degraded_reason`（`no_questions` / `report_gone`），推导只写在
      `task_service.report_availability()`，且**只对 `question_generation` 类任务**算
      （简历解析任务这四项恒为 `None`/`False`）。控制台据此在收尾时弹"0 题"警告而不是成功提示。
      **没改的**：`interview.status` 仍是三态 CHECK，没加第四种"降级"——降级是**任务视角**的
      读法，报告头不该为一个展示问题再多一个终态。**还欠**：`review_issues` 条数没进 DTO
      （批注纯观测，控制台在详情页看；要放进列表得先想清楚"有批注"该不该影响排序）
- [ ] **`GET /metrics` 没实现**（计划书阶段 8 列过它）。现在三指标只能靠脚本跑真调拿，
      没有常驻可观测面。
      卡在：不在 M6 产出清单里，且真实数字还卡在本节第一条的余额与样本上。
      → 恢复后要验：等 `eval_e2e.py` 能稳定出数字后，把它的输出落成一张 `eval_run` 表再由端点读；
      **不要在端点里现跑评测**（一次就是几十次上游调用、80 秒）。
- [ ] **认证与限流仍没接线**（2026-10-28 更新：三个中间件空壳已按 D-8(b) 删除，
      见 T-312；**账本身没销**，只是不再有"看起来有中间件"的假象）。
      `create_app()` 只挂 CORS；所有表都没有 `user_id`，所以 `GET /tasks/{task_id}` 与
      `GET /interviews/{id}` 是**知道 id 就能读**。要重启这块得从零写中间件，别再照抄旧壳。
      卡在：与 §3 的 `app/models/user.py` 同一条账（属阶段 2 欠账，D-5 未定）。
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
- [x] **迁移形状没有机器护栏**：ADR-0013 §2 要求给已有表加 NOT NULL 列走裸
      `ALTER TABLE … DEFAULT`，禁用 `op.batch_alter_table`（实测它 `DROP TABLE` 重建 +
      `PRAGMA foreign_keys=ON` + `question` 的 `ON DELETE CASCADE`，级联删空过 30 行题目）。
      现在只写在 ADR 与 `AGENTS.md` 里。
      → 恢复后要验：在 `tests/test_migrations.py` 加一条静态断言（扫
      `migrations/versions/*.py` 里 `batch_alter_table` 的出现并拒绝，或要求它带
      `recreate="never"` 注释），别靠人记住。
      已加 2026-10-27（T-016）：`tests/test_migrations.py::scan_migration` 走 AST，
      判据不是"出现即拒绝"（那样三条 autogenerate 的历史迁移第一天就红，见 TASK_PLAN §5），
      而是 **batch 块体内只许 `create_index` / `drop_index`**，外加"手写 `ALTER TABLE`
      出现 `NOT NULL` 必须同带 `DEFAULT`"（f-string 也解析，ADR-0013 那条就是 f-string）。
      实测：现有 3 份迁移直接绿；4 种事故形状（add/drop/alter_column、
      create_check_constraint）逐个判红；缺 DEFAULT 的裸 ALTER 判红。
- [ ] **409 是"先查后插"判出来的，不是约束兜出来的**：并发下两个请求可能都查空、
      后插的那个撞 `uq_job_title_company` 变 500。单机 SQLite 单写者，眼下接受。
      → 恢复后要验：真上多进程时在 `JobRepository.create` 捕 `IntegrityError` 翻成
      `ConflictError`，并把这条从"约定"降级为"防御"。
- [ ] **孤儿上传文件没人清**：`force=true` 删简历时文件删除失败只是忽略（留痕都没有），
      上传后从未成功解析的简历也占着 `data/uploads/`。
      → 恢复后要验：写一个 `app/scripts/prune_uploads.py`，判据是
      "`upload_dir` 里的文件名对不上任何 `resume.stored_path`"，先跑 dry-run 报数。
- [x] **`GET /jobs` 现在每条都带 `jd_text`**〔已销 2026-10-28 / T-301〕：列表行换成
      `JobListItem`（`app/schemas/job.py:42`），正文只留前 `JD_EXCERPT_CHARS = 160` 字，
      全文在 `GET /jobs/{id}` 给。共用列声明在 `JobFields` 一处，**没有拆两份 DTO 真相**；
      `jd_excerpt` 由 `from_row` 从同一列切出来，不是第二份存储。
      → 恢复后要验：真要长列表时给列表端点加 `?fields=` 或单独的 `GET /jobs/{id}` 取正文，
      别把 DTO 拆成两份真相。
      **走的是第二条**（`GET /jobs/{id}` 早就存在，缺的只是列表别再全量下发）。
      `?fields=` 动态投影被 ADR-0020 明确否决，所以口径改成"每端点一份固定投影模型"。
      验收换成：`tests/test_api/test_list_projection_api.py` 钉住三种列表响应都不含正文列。
- [ ] **控制台无鉴权照旧；翻页是"当页翻页"，不是服务端翻页**〔2026-10-28 T-307 只做了一半〕。
      `frontend/src/api/index.ts` 的 `DEFAULT_LIST_LIMIT = 200` 仍然一次取满服务端上限，
      `ReportsView` 的 `el-pagination` 翻的是**这 200 行**，状态筛选（`status` chips）也在本地；
      所以 `total` 说的是真话（服务端数的），但**超过 200 份报告的那部分点不到**。
      卡在：① 服务端列表端点还没有 `status` 查询参数（只有 `q`/`limit`/`offset`）；
      ② 翻页 UI 要改成"换 `offset` 重新请求"，那会牵动静默刷新与筛选保持的交互，
      是一次独立的交互设计而不是补一个参数。鉴权那条是 D-7。
      → 恢复后要验：加 `status` 参数后 `total` 必须与筛选同源
      （否则"共 N 份"与筛出的行数又开始各说各话——ADR-0020 立的正是这条）；
      chips 的计数改成服务端给的分组计数，而不是当页累加。

## 2.6 岗位图片录入的账（`POST /jobs/inspect-image`，2026-10-21）

落地的部分：图片 → `ImageParser`（复用 `ocr@v1`）→ `job_extractor` 三层字段来源 →
`JobDraftOut` 草稿 → 页面灌表单 → `POST /jobs` 落库。决策见
`docs/adr/0014-job-image-intake.md`。**全部离线测试**（485 passed 里 22 条是这一节的）。

2026-10-21 拿合成海报真跑通过一次（`deepseek-v4-flash-vision-exp` 可用，账号 402 只在文本模型上
撞到过），一次调用出草稿：`title`/`company`/`min_years` 三项全对（`3-5 年`→`3.0`）。
但同一张图暴露了两个实测问题，都记在下面。

- [x] **视觉模型把拉丁字母逐个加空格**：实测 OCR 出来是 `P y t h o n` / `R e d i s` / `My`，
      于是 `derive_required_skills` 只认出 `["高并发"]`，Python/Redis/MySQL 全漏，
      下游 `match` 的缺口判定就少三条要求。
      卡在：修法未定——在 OCR 层折叠"单字母+空格"是最省事的，但它会同时改坏 `Base 上海`
      这类本来正确的排版；在词典侧忽略空格又要动 `SKILL_ALIASES` 的判据（与 `match` 共用的那条）。
      → 恢复后要验：先量一张真海报的 OCR 文本形状再选边，改完必须重跑
      `tests/test_services`（`match` 侧的判据不能分叉）。
      ——已修 2026-10-28（T-104，选了 OCR 层这条路）：`app/parsers/image_parser.py::despace_latin()`，
      判据保守在 `(?:[A-Za-z][ \t]+){2,}[A-Za-z]`——**同一处要连成 3 个以上单字母**才算逐字失真，
      且只认 ASCII 字母、不跨换行。所以 `Base 上海`（两个字母断开，中间还是中文）、`C C++`（只有 1 个空格）、
      `GPA 3.8` 全部原样留着；`P y t h o n`→`Python`、`R e d i s`→`Redis`。
      归一发生在 `parse()` 收尾、**按字数判糊之前**，所以长度阈值数的是真正留下的文本。
      **没有动词典侧判据**，`SKILL_ALIASES`/`match` 共用那条一字未改（这就是"选 OCR 层"的全部理由）。
      实测：`tests/test_services` + `tests/test_parsers.py` + `tests/test_api` 全绿；端到端用例
      `test_spaced_latin_in_ocr_still_reaches_the_skill_dictionary` 断假件 OCR 的 `required_skills`
      含 `python`/`redis`，`test_vision_spaced_latin_letters_are_glued_back` 断 `C C++` 不被粘坏。
      还欠：真海报一张都没试过（下一条），所以"≥3"这个阈值只在合成图上验过。
- [x] **空白图回的是模型的元话**：实测一张纯灰度图 OCR 出 `（图片中未出现可识别的文字）`，
      14 字 ≥ 5 字的门槛，于是这条短语原样进了 `jd_text`。这条链路靠 `missing_required`
      把用户拦在了保存之前，所以不算脏数据，但草稿正文很莫名。
      → 恢复后要验：要么把 `jd_too_short` 的门槛与 `_MIN_OCR_CHARS`（§1 也在等同一条校准）
      一起按实测提高，要么让 `ImageParser` 认这种"自述没读到字"的回复并直接抛
      `parse_vision_empty`。**别用关键词列表去黑名单模型的措辞**，那是第二套判据。
      ——已修 2026-10-28（T-104）：**做了"每页单独判糊"这一步，没做元话识别**。低于
      `_MIN_OCR_CHARS = 20` 的页不再进 `raw_text`（原来会把糊页拼进正文），全部页都不够就抛
      `parse_vision_empty`（422）。卡片原本要求"`jd_text` 留空 + 记 `notes`"，实现改了形状：
      `/jobs/inspect-image` 的 `notes` 只在成功响应里回，而糊图根本给不出可保存的草稿，
      返 422 让用户重传比回一张"看着填好了"的空草稿诚实。
      **仍然存在的边界**（诚实记下）：一句 ≥20 字的自述（"这张图片里没有可识别的文字内容。"）
      照样进 `jd_text`——按上面那条告诫，不打算用关键词去认，只能靠 `_MIN_OCR_CHARS` 提高，
      而提高阈值要真海报样本。已开 §2.6 下一条复选框。实测：合成 1 页糊图 → 422 +
      `parse_vision_empty`，且假件只被叫了 1 次（`test_blank_poster_fails_as_empty_read_not_as_meta_prose`）。
- [ ] **`_MIN_OCR_CHARS` 的校准没做，元话长句因此仍进正文**：现在 20 字是 2026-10-16 凭推断定的，
      它同时承担两件事——"这页糊"与"模型在自述而没读图"。前者要样本量，后者要一个不用关键词的判据
      （候选：视觉回复里**没有出现图片中应有的排版痕迹**，比如整段无任何数字/无换行；但这仍是猜）。
      卡在：**D-1 要 ≥1 张真招聘海报**（现在是 pymupdf 渲染的合成图，字体、版式、噪点都是最理想情况）。
      → 恢复后要验：拿真海报跑一次，把 OCR 文本存进 `tests/fixtures/llm/job_image_*.json` 回放，
      报"一页正常海报多少字 / 一张空白海报多少字"两个数再定阈值；改阈值要同时看 §1 那条同源的账。
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

## 2.7 S0 止血九张卡剩下的账（T-002~T-010，2026-10-27）

- [ ] **前端不认三个新错误码**：`upload_too_large`（413）、`upload_kind_mismatch`（422）、
      `resume_gone`（任务终态）。控制台只会原样显示 `error.message`，中文文案是服务端给的，
      所以功能上不难看，但**没有"该做什么"的指引**（413 → 换小文件；422 → 换文件或改后缀；
      `resume_gone` → 这份简历已经被删，别等进度了）。
      → 卡点：无。恢复后要验：`frontend/src/utils/http.ts` 的响应拦截器按 `error.code`
      分支补三条提示；动 `frontend/` 必须 `npm run typecheck` + `npm run build` 全过。
- [ ] **"灌 2GB 假 PDF 看 RSS"这条验收没做**（T-002 的原判据）。用例里造 2GB 字节本身就是
      自伤，所以换成了等价的形状断言：`read_capped` 喂**无限假流**只读 1 块（64KB）就抛
      （`tests/test_api/test_upload_limits.py`）。
      → 卡点：要真数字得拿真进程量，属手工验证，不属于 CI。恢复后要验：`resource`/`psutil`
      读一次 RSS，边传边看，确认增量在一个上限之内。
- [ ] **`configs/testing.yaml` 的 `upload:` 段同样不生效**（`APP_ENV` 没被 conftest 设置，
      见 BACKLOG P3-3 / T-313）。新用例是靠 `monkeypatch` 把上限调到 4KB 跑的，
      所以它测的是行为不是配置。**别为了让它生效去改 `.env`**。
- [ ] **`_dropped_keys` 还没有读侧**：`task.error.details` 被白名单收走过字段时，
      控制台看不到"有东西被挡下了"。要给人排障得在报告页或任务详情里露出这个键
      ——但露之前先想清楚：`GET /tasks/{id}` 是无鉴权的，键名本身也算元数据。
      → 卡点：D-5（单机自用 vs 多租户）。

## 2.8 T-017（`log_level` 真生效）剩下的账（2026-10-27）

- [ ] **`@log_call` 与 `request_id_var` 仍是死代码**（T-017 只做级别，这条是另一半）。
      2026-10-28 按 D-8(b)（T-312）做了那条"不接就连带三个中间件壳一起删"的另一半：
      `app/middleware/` 整个目录没了，`auth` / `logging` / `rate_limit` 三个空壳不再存在。
      **但 `@log_call` 与 `request_id_var` 本身留着**（`app/utils/decorators.py`，
      `tests/test_prompts_and_decorators.py` 测的是装饰器本身，不是接线）。
      级别现在真的生效了，所以"要不要每请求一条带 `request_id` 的访问日志"仍是个有意义的选择：
      接线要在 HTTP 入口 `set` `request_id_var`（从零写一个中间件），并把 `@log_call`
      挂到服务层入口。**没有中间件壳可捡了，下次动手是新建而非补全。**
      → 卡点：无阻塞，只是没排在某个里程碑里（归 `docs/TODO.md` §2.8 跟踪）。
      恢复后要验：若接线，`curl` 一次 `/api/v1/jobs` 要在 stdout 看到**同一条 rid** 出现在
      访问日志与被装饰函数的日志里；若确认不接，`grep -rn "log_call\|request_id_var" app/`
      必须零命中，且 `tests/test_prompts_and_decorators.py` 要跟着删装饰器用例。

## 2.9 S2 成本与回路剩下的账（T-202~T-208，2026-10-28）

- [ ] **`GenerateJob` 里 `parsed_json` 的槽位合并**（T-205 只做掉的那一半）。
      现在每个扇出分支都带**整份** `parsed`（实测 6 分支合计 20,451 字，占出题 prompt 的 40%），
      而单题实际只消费自己那条 `plan_item` + 一份技能/经历摘要。
      已合入的 T-205 只做了"按 `source` 带语料"（`corpora_for` 单判据，`raw_text` 从 6 份降到 3 份），
      **实测总字符只降 0.4%（50,808 → 50,595）——因为种子 JD 才 90 字**，这条要记着：
      载荷瘦身的收益取决于语料规模，不取决于省下了哪个字段。
      卡在：① 合并槽位要改 `plan.items` 与扇出的**一对多**关系（一个分支出多题），
      那是 ADR-0010 的形状，需要新 ADR；② `angles`（T-202）刚刚提供了"一次调用里区分各题"的依据，
      在此之前合并等于把重复率从一个槽位内部放大到多个；③ 收益金额要真调才有 token 数字（**D-2**）。
      → 恢复后要验：先跑 `scripts/probe_fanout_payload.py` 记离线字符基线，再动形状；
      改完必须复测"带可回溯引用比例"仍是 1.0（离线用例 `test_a_slimmed_payload_still_quotes_from_every_source`
      已经钉住这个不变量，别让它在合并时悄悄变成 0.9）。
- [ ] **`app/utils/text.py` 没有被 T-208 征用**（`docs/TASK_PLAN.md` 的 D-8/T-312 行有一句错前提）。
      那边写的是"`text.py` 归 T-208"，实际**决定不合并**：`llm_client._clip`（截断处**造**一个
      "已截断"记号，因为回灌给模型时必须让它知道后半段没了）与 `match_node._resume_evidence`
      （截到边界必须是**逐字前缀**，因为产物要过 `evidence_belongs`）是两个不同的判据。
      做成一个带 `marker=` 参数的公共函数就是假抽象：参数一变，语义就变。
      卡在：无（这是决定，不是等待）。
      → 恢复后要验：真要合并必须先回答"截断产物还需不需要过证据闸"；
      T-312 的删除清单里 `text.py` 仍是空壳，**可以删**（但按 D-8 走，由维护者点头）。
- [ ] **S2 三处"效果"只有真调能证**。`angles` 有没有降低换皮重复（T-202）、
      `<resume_evidence>` 有没有提高 `source=llm` 的 matched/partial 采纳率（T-203）、
      总时限与截断回灌在真实网络上省下多少（T-206）——三条的**形状**都有离线用例，
      **数字**全没有。
      卡在：**D-2**（DeepSeek 余额）。离线用例不许改成断言模型文案（AGENTS.md §6），
      所以这三条永远不可能靠测试套件关掉。
      → 恢复后要验：一次 `scripts/eval_e2e.py` 端到端就能同时拿到三个数
      （引用比例、批注点名题数、`llm.usage` 与墙钟），跑之前先确认 `agent.prompt_version`
      在 dev 是 `v2`（否则 T-202/T-203 的新增量根本没进 prompt，测出来的是 v1 的旧数字）。
- [x] **`configs/testing.yaml` 里新加的 `llm.retry_deadline_seconds=60` / `refeed_max_chars=400`
      现在同样不生效**。卡在：T-313（`APP_ENV` 未被 conftest 设置、`get_settings()` 是 `lru_cache`
      单例）——这不是新账，是旧账长出了新字段。→ 恢复后要验：见 §2 / T-313；
      生效前别拿 testing.yaml 的数字推断测试环境行为。
      已修 2026-10-28（T-313）：`tests/conftest.py` 模块级设 `APP_ENV=testing` + 清缓存，
      **现在生效了**，整个 `testing.yaml` 都是几百条用例的运行期前提（见 §2.10）。

## 2.10 S3 后端批次剩下的账（T-302 / T-303 / T-304 / T-311 / T-313，2026-10-28）

- [ ] **prune 的 90 天保留窗口是估的，且 `--apply` 从未真跑过**。
      `app/scripts/prune_checkpoints.py` 的 dry-run 在本机开发库上是
      `线程 13 个：prune=0 keep=13 protected=0 orphan=0`（默认窗口内），
      `--keep-days 0` 才把 13 个全列为候选——**13 个线程、21,770,240 字节一个都没删**：
      删的是历史运行痕迹，得维护点头。
      90 天没有依据支撑，它是"这一卷大概还会被重新导出"的保守估值；
      真实依据是"上一次重新导出三个月前的报告是什么时候"，这个数没人记。
      卡在：① 授权（这是写操作）；② 校准需要至少一条使用记录。
      → 恢复后要验：先 `cp data/checkpoints.db data/checkpoints.db.bak`，
      再 `... -m app.scripts.prune_checkpoints --apply --keep-days N`；
      删完必须复验**旧报告仍导出成功**（走 `NO_STATE_NOTE` 分支，
      用例 `test_a_report_whose_checkpoint_is_gone_still_exports` 就是这个形状）、
      以及仍在跑的任务不受影响（`protected` 那档判据是 `status == running`，
      它只在同一进程里被 `BackgroundTasks` 推进）。
- [ ] **`--vacuum` 只在"没有别的进程在写这个库"时安全**，而这条只能在真机上证。
      离线用例覆盖的是"vacuum 之后剩下的线程还读得出来"（`test_vacuum_does_not_lose_the_kept_thread`），
      覆盖不到"uvicorn 正在写 checkpoint 时 VACUUM 撞 `database is locked`"——那是进程间锁，
      测试里只有一个进程。
      卡在：无（是文档性缺口，跑之前先把 API 停掉就行）。
      → 恢复后要验：服务运行时执行 `--apply --vacuum`，确认报的是 SQLite busy 而不是半删状态。
- [ ] **P2-10 欠的"导出耗时对比"仍然没有绝对数字**。新报告一次定位、零扫描已经由用例钉住
      （`_thread_ids` 被换成"一调就炸"），但"21MB 的库上扫一次多少毫秒"没测过，
      所以这条优化的收益目前是**形状**而不是**金额**。
      卡在：开发库上真跑一次扫描需要 13 个线程逐个 `aget_state`，可以做但没人做过。
      → 恢复后要验：`scripts/probe_*` 里加一次性计时（读，不写），拿到毫秒数再回写 BACKLOG P2-10。
- [ ] **prune 的认领成本是 O(没有 `thread_id` 的线程数)**：每认一个旧线程要建一次壳图 +
      `aget_state`。本机 13 个无所谓，报告多起来（或 checkpoint 库长期不清）就会变成
      "清理脚本自己很慢"。
      卡在：无解——旧行的 `thread_id` 是空串，正文里的 `interview_id` 是唯一线索（不回填，见 T-304）。
      → 恢复后要验：T-304 之后新报告全走 `thread_id` 哈希表，这一档只减不增；
      若它反而变慢，说明有脚本在绕过 `start_generation` 建报告。
- [x] **列表信封 `Page{items,total}` 是契约变更，前端还没跟上**〔已销 2026-10-28 / T-307〕。
      `GET /resumes/list`、`GET /jobs`、`GET /interviews/list` 三个端点的返回形状已改
      （T-303），而 `frontend/src/api/index.ts` 仍按裸数组读——**页面此刻是坏的**，
      直到 T-307 同步 `types.ts` 与调用点。
      卡在：无（就在这批里做，见 §2.10 之后）。
      → 恢复后要验：`cd frontend && npm run typecheck && npm run build` 全绿，
      且列表页、报告页、生成页三处的搜索框与分页在服务端过滤下仍正确（不再是本地过滤 200 条）。
      **已跟上**：`api/index.ts` 三个列表函数返回 `Page<T>`，`types.ts` 用 `Omit<>` 从详情类型
      派生列表行（少一列就 `typecheck` 红），三处本地过滤换成服务端 `q` 参数
      （`utils/search.ts` 是那次收的公共入口）。`typecheck` + `build` 全绿实测 2026-10-28。
      **但"服务端过滤下仍正确"这半句只验到类型与请求形状**：页面渲染没在浏览器里逐项点过
      （本机 `browser-use` 截不到像素），所以"翻页时总数对不对"仍是一次人眼验收，见 §2.11。

## 2.11 S3 契约与前端批次剩下的账（T-301 / T-305 / T-306 / T-307 / T-308，2026-10-28）

落地的部分：三种列表的固定投影（`ResumeListItem` + `ResumeOverview`、`JobListItem` +
`jd_excerpt`、`InterviewListItem`）、前端吃 `Page<T>` 并把正文挪进详情抽屉、
任务轮询指数退避 + 可中断等待 + 列表静默刷新并入公共循环、
列表行"打印"跳详情并自动起打印、`utils/markdown.ts` 删除（导出只走服务端那一份）、
搜索框/开关的 `aria-label` 与错误提示的 `role="alert"`、页面上的轮询文案改从常量取。
决策见 `docs/adr/0020-list-page-envelope-and-projection.md`。

- [ ] **本批前端改动没有在浏览器里逐项点过**。护栏只有 `npm run typecheck` +
      `npm run build` + 服务端用例（`test_list_projection_api.py` / `test_list_filter_api.py`）。
      `browser-use` 在本机点击与截图都失败（0×0 视口），只跑得动 `take_snapshot` /
      `evaluate_script`，所以"打印按钮真的起打印""退避在 kill 后端后真的停在第 3 次"
      这两类**行为**是没验过的，不是通过了。
      卡在：无（是人眼验收，不是外部条件）。
      → 恢复后要验：`bash start.sh` + `cd frontend && npm run dev`，
      亲验三件事：① 列表行"打印"落到详情页并弹出打印框、`?print=1` 随后被清掉；
      ② 中途 kill uvicorn，报告页在 3 次连续失败后停并报一句人话；
      ③ 关键词搜索命中数与服务端 `total` 一致（不是当页累加）。
- [ ] **a11y 只补了"能听见"，没补"能走到"**（T-308 的一半）。
      加的是 `aria-label` / `role="alert"` / `role="status"` / `aria-pressed`，
      **键盘焦点顺序、Tab 可达性、对比度、读屏器实测一条都没做**——
      原描述里那句"tabs 缺 `role=\"tablist\"`"在 Vue 侧由 `el-tabs` 自带，所以不算欠账，
      但 Element Plus 组件的键盘行为是它的实现事实，不是本项目验过的事实。
      卡在：无（需要一次带读屏器的手动走查）。
      → 恢复后要验：只用键盘走完"上传简历 → 出题 → 打开报告 → 打印"；
      对比度按 4.5:1 抽查正文与小字（`.switch-hint`、`.tiny` 这类 `--text-muted` 最可能不合格）。
- [ ] **`SKILL_PREVIEW_LIMIT = 8` 这个数是拍的**。列表概览只带前 8 项技能名，
      出题页选中简历后也是这 8 项 + `+N` 提示。它不影响任何判据（`match`/`plan` 读服务端全文），
      只影响人看见多少。
      卡在：需要一份"真简历平均几项技能"的分布，而那是 D-1（真简历样本）的数据。
      → 恢复后要验：拿 5–10 份真简历跑 `scripts/eval_extraction.py`，看技能项数分布再定。
- [ ] **报告翻页仍是"当页翻页"**，见 §2.5 最后一条（`status` 参数与真翻页一起欠）。
      本批把**关键词**交给服务端了，**状态筛选没有**，所以超过 200 份报告时两者口径不一致。
      卡在：服务端还缺 `status` 查询参数（一次小的契约变更）+ 翻页交互设计。
      → 恢复后要验：`total` 必须跟着 `status` 一起过滤，否则"共 N 份"再次变成第二份真相。

## 2.12 S4 定向重做剩下的账（T-404 / ADR-0021，2026-10-28）

落地的部分：`auto_fix` 从请求体传到图（`GenerateRequest` → `run_question_generation` →
初始 state）、`route_after_review` 那张授权驱动最多一轮的回炉边、
`app/services/redo_service.py`（`plan_slots` / `redo_plan` / 点名判据 / 带走改写）、
`fanout_slots` 通道（`order_start` 与重派集合的唯一出处）、`GenerateJob.previous`、
v3 模板的 `<previous>` 段、批次 2 是完整卷子、控制台开关与文案。

- [ ] **`POST /interviews/{id}/redo` 没有做**——卡片给了两条授权入口，只落了布尔字段那条。
      现在的语义是"**发起时就声明要重做**"，不是"看完报告再点重做"。
      卡在：重做一个线程要 `raw_text`（简历正文）与 `jd_text`（岗位正文），而 ADR-0013 的
      报告头只快照了**名字**（`resume_name` / `resume_file_name` / `job_title` / `job_company`）。
      `interview.job_id` 是 `SET NULL`、简历可被删，所以凭 `interview_id` 反查经常拿不到语料；
      而"拿不到就静默降级成不带引用"正是 ADR-0015 反对的盲重掷。
      → 恢复后要验：先定"补 `jd_text`/`raw_text` 快照列"（4 张表里挑 1 张加 NOT NULL 列，
      照 ADR-0013 裸 `ALTER TABLE … DEFAULT`，禁 `batch_alter_table`）
      还是"只允许简历与岗位都还在时重做"（端点对 `resume` 行不存在 / `raw_text` 为空返 409）。
      两条路都要重新验一次**幂等**：同一份报告点两次重做，不能落两个批次 3。
- [ ] **v3 模板从未真调过，`<previous>` 的效果是纸面收益**。离线用例只证"重做轮的 prompt 里
      确实带了上一版的题与批注"（`test_only_the_redo_template_renders_the_previous_version`），
      **不证"重出来的题更好"**——后者要人评分或真调对比。
      卡在：D-2（DeepSeek 余额）+ T-105（真调评测基线）。
      → 恢复后要验：`configs/development.yaml` 现在指 **v3**，一次开 `auto_fix=true` 的
      真跑约等于"两轮扇出"，成本口径按 ADR-0021 的"至多多一轮"核；
      评分维度沿用 `scripts/eval_e2e.py` 的可用率/幻觉率/token 三件事，
      并额外记一条"批次 2 里被同一类批注再次点名的题数"——这个数字才说明重做有没有用。
- [ ] **production 环境上 `auto_fix=true` 是刻意不可用的组合，但没有机器拦住它**。
      production 的 `prompt_version` 仍是 v1，v1 不渲染 `<previous>`：
      槽位照样重派、反馈进不了 prompt，退化成 ADR-0015 打死的盲重掷，还多花一轮钱。
      现在只有 ADR 与注释在说这件事。
      卡在：要么在配置层加一条"v1 + auto_fix 拒绝"的启动期/请求期检查（那是新判据，
      要先想清楚它属于配置校验还是端点校验），要么等 v3 推上 production 后自然消失。
      → 恢复后要验：若选"加检查"，必须同时测两端（配置加载与 422 响应），
      别让它变成"只有文档知道"的规则；若选"等 v3"，就把这条连同 D-2 一起销。
- [ ] **批次 1 的行永久留在 `question` 表**，开授权的那份报告会有 2N 行。
      这是"改过哪些题"的证据，不是垃圾；但没有任何地方报过"哪个批次是最新的"以外的历史视图。
      卡在：需要展示层决定要不要给"历史批次"一个入口（与 §2.4 的报告形态、T-503 是同一片地）。
      → 恢复后要验：`list_for_interview` 默认只取 `latest_batch` 这条不变量不许松；
      若加历史视图，必须同时处理"批次 1 的题与批次 2 的题同号"（`order_index` 在批内唯一）。

## 2.13 D-4/D-5/D-6/D-8 解卡四张剩下的账（T-201/209/309/312，2026-10-28）

落地的部分：`validate` 规则/反思分家（ADR-0022，`reflect_issues` 通道）、生成入口 409 去重
（ADR-0023）、`GET /tasks/{id}` 四个派生字段（ADR-0024）、空壳处置（ADR-0025，
删 `candidates.py` 与整个 `app/middleware/`）。§2.1、§2.4、§2.8 里对应的复选框已就地销账。

- [ ] **去重是"先查后插"，不是原子**。两个请求恰好同时通过检查仍会各建一行。
      卡在：真做成原子要建**部分唯一索引**（`WHERE status='running'`），那是一条新迁移，
      且 `IntegrityError` → 409 的映射还得再写一层；单机自用（D-7 的 (a)）不值这份复杂度。
      → 恢复后要验：若加索引，必须同时测"终态不受约束"（同一对可以有多份 completed 报告）
      与"并发插入的第二条拿到 409 而不是 500"；迁移形状照 ADR-0013（禁 `batch_alter_table`，
      索引式的 batch 是唯一合法形状）。
- [ ] **`reflect_issues` 没有消费者**。反思问题进了 `errors` 与导出注记，能看到，
      但没有任何环节会因此重抽或改规则。
      卡在：要"只重抽被点名的字段"得改 `parse` 的入参约定与三个抽取器的调用形状
      （与 §2.9 的 `parsed` 槽位合并是同一个改动面）。
      → 恢复后要验：动完必须证明 `parsed` 在"无反思问题"的样本上逐字段不变（幂等），
      且 `validate_retries` 的语义没有被顺手改回"按条数加"。
- [ ] **`review_issues` 条数没进 `TaskOut`**。批注纯观测（ADR-0015），所以没放进去；
      但报告页之外的读者（比如将来的仪表盘）要显示"这份卷子有几条批注"就得再拉一次详情。
      卡在：要先回答"'有批注'该不该影响列表排序/筛选"——那是产品问题，不是实现问题。
      → 恢复后要验：若加字段，`test_m6_api.py` 里四条派生字段用例要同步（它们断的是
      `report_availability` 的返回值，不是 DTO 全集）。
- [ ] **`utils/{guardrails,validators,formatters}.py` 仍是 0 import**。ADR-0025 只删了
      "广告一条链路"的那几个；这三个是工具函数名，危害弱一档但仍是审计噪音。
      卡在：无——只是这次没有依据说"谁将来要用"，所以没删。
      → 恢复后要验：删前 `grep -rn` 三处零命中；删后三条门禁全绿。
- [ ] **`degraded` 的前端体验没在浏览器里验过**。`GenerateView` 的 `degradedNote`
      与 `ElMessage.warning` 分支只有类型检查背书（`npm run typecheck` 过），
      没有真的造一份 0 题报告点进去看。
      卡在：造 0 题报告要真跑一次生成（D-2 余额），或手工改库把 `total_questions` 置 0。
      → 恢复后要验：0 题那份弹 warning 不弹 success、页内说明出现且文案是
      "重跑会得到同一批语料"；报告被删那份（`?force=true`）文案换成"报告已不存在"。

## 3. 仓库挂账（与本里程碑无关，但别忘）

- [ ] `pyproject.toml` 的 `dependencies` / `dev` 仍是空的，与 `requirements*.txt` 脱节
      （2026-10-27 T-018 只把**版本口径**对齐了：`requires-python = ">=3.14"`、
      ruff `target-version = "py314"`、mypy `python_version = "3.14"`，并把依赖注释指向
      `requirements.txt`；装什么仍由 `requirements*.txt` 说了算。要并进来就得选一套打包工具，
      那是另一个决定）
- [x] `README.md` 与 `project_structure.md` 还在说 Postgres / pgvector / Celery，
      实际是 SQLite + sqlite-vec + `BackgroundTasks`（见记忆 No-Docker embedded infra）
      —— **2026-10-27 T-018 已改完**：两份文件顶部都声明自己是"初始设计快照"，技术选型表
      重写为 DeepSeek 四个模型槽位 + SQLite WAL + `NullRetriever` + `SqliteSaver` +
      `BackgroundTasks`，`vectorstore/` 标成**未接线**，`docker/` 只剩一份说明"没有 Docker"
      的 README。恢复后要验：`grep -rn "pgvector\|Celery\|Langfuse" README.md project_structure.md docker/`
      的每一处命中都必须是"我们不用它"的否定句，不能是能力描述
- [ ] **仓库已经是 git 库了**（2026-10-27 实测：`main` 分支、1 个提交 `1d02ae1`、
      `git ls-files` 301 个文件、`.env` 与 `data/*.db` 由 `.gitignore` 挡住）。
      这条要改的是**别处的旧前提**：`AGENTS.md` §3 的 CASCADE 事故叙述、`docs/TASK_PLAN.md`
      的 T-312/D-8 行、本文件与 `docs/OPTIMIZATION_BACKLOG.md` 里"非 git 库所以删除不可逆"
      的卡点理由。
      卡在：**结论不变，理由变了**。已跟踪文件删掉确实能从提交取回，但 S0 这批改动还全部躺在
      工作区里没提交（`git status --porcelain` 85 行），此刻删文件依旧是真删。
      → 恢复后要验：先由维护者把 S0 的改动落成提交（删不删由他点头，AI 不擅自 commit/push），
      之后 D-8 那批空壳文件的删除才真的变成可逆操作
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
