# LangGraph 业务搭建计划书

> 生成于 2026-10-15。范围：**把七个节点从空壳填成能交付业务的实现**。
> 本文件是执行清单，不是设计文档；每条验收标准都必须是可执行的命令或可断言的事实。

---

## 1. 现状盘点（实测，非推测）

已完成并通过 `ruff` / `mypy` / `pytest`（9 passed）：

| 模块 | 提供 |
|---|---|
| `app/config.py` | 25 个字段的 `Settings`，`.env` + `configs/*.yaml` 双来源 |
| `app/agents/state.py` | `AgentState`、`NodeName`、`PIPELINE`、只追加通道约定 |
| `app/agents/workflow.py` | `build_agent_graph()`、SQLite checkpointer、两张回退门、`traced()` |
| `app/utils/llm_client.py` | `LLMClient.chat/chat_json`、传输重试、并发限流、token 统计 |
| `app/exceptions/base.py` | `AppError` 层级（`LLMError` / `LLMResponseError` 已分家） |

M0 契约层已完成（`ruff` / `mypy` / `pytest` 27 passed）：`schemas/resume.py`、`schemas/question.py`、`prompts/registry.py` + `prompts/templates/v1/*`、`utils/decorators.py`。

图形状已可用，**七个节点全部是一行 docstring 的空壳**，`app/parsers|extractors|schemas|models|repositories|services|prompts|vectorstore|api` 同样为空壳。

---

## 2. 阻塞关系

```
M0 契约层 ──┬──► M1 解析链 ──► M2 validate
            │                          │
            ├──► M3 持久化出口 ◄────────┤（review 通过后落库需要它）
            │                          │
            ├──► M4 match/profile/plan ◄┘
            │
            └──► M5 generate/review ──► M6 端到端评测
```

`match` 的向量检索依赖 embedding 选型（已决定推到阶段 6），因此 M4 里 `match` 先做**无检索降级版**，把 embedding 依赖隔离在这一个节点内。

---

## 3. 里程碑

### M0 契约先行（约 4h，无 LLM 调用）

产出：`schemas/resume.py`、`schemas/question.py`、`prompts/registry.py`、`utils/decorators.py`

- [x] `ResumeSchema`：姓名/年限/技能/学历/工作履历/项目履历，字段级 `field_validator`
- [x] `QuestionSchema`：题干 / 考察点 / 参考答案 / 评分标准 / 追问 / `evidence`（对应 `schemas/question.py` 的既定职责）
- [x] `prompts/registry.py`：`get_prompt(node, version)`，模板落 `app/prompts/templates/<version>/<node>.{system,user}.j2`，版本号写进 `trace`
- [x] `utils/decorators.py`：`@log_call`（带 request_id）与 `@retry`
- [x] 每个 schema 配 3 条测试：正常样例、缺必填、幻觉型脏数据

**验收**：`python -m pytest -q` 全绿（27 passed）；`mypy app` 无错；
`ResumeSchema.model_validate(json.load(open('tests/fixtures/resume_ok.json', encoding='utf-8')))` 不抛异常。
注意 `encoding='utf-8'` 不能省——Windows 默认按 GBK 解码， fixture 里的中文会直接抛
`UnicodeDecodeError`，那是读文件方式的问题，不是 schema 的问题。

### M1 解析链 + `parse_node`（约 8h，第一次真调 LLM）

产出：`parsers/base.py`、`parsers/pdf_parser.py`、`parsers/docx_parser.py`、`parsers/image_parser.py`、`extractors/*`、`agents/nodes/parse_node.py`

- [x] 先定 PDF 引擎：**PyMuPDF**，`requirements.txt` 已由 pypdf 换成 `PyMuPDF>=1.28`，实测与理由见 `docs/adr/0006-pdf-engine.md`
- [x] `BaseParser` + `ParserRegistry`：按扩展名选实现，未知类型/文件缺失/坏文件/加密各抛一种 `ParseError` code
- [x] `pdf_parser.py`：`pymupdf` 解析，`sort=True` 取阅读顺序；整份扫描件抛 `parse_scanned_image` 交图片路径，单页文字过薄进 `warnings`（真简历 `附件简历-梁清生.pdf` 已验，CJK 无乱码）
- [x] docx：段落 + 表格按 `body` 文档流顺序拉平（`paragraphs`+`tables` 两个列表会把表格挤到文末）；页眉进正文前、页脚不取；单元格用 `	` 分隔；`page_count` 取自 `docProps/app.xml` 且只做日志用
- [x] 图片：走多模态 LLM（PaddleOCR 在 Python 3.14 无分发，已决定不用）。`ImageParser` 是 **async**，
      不进 `ParserRegistry`（那是同步契约）：扩展名是图片、或 `PdfParser` 抛 `parse_scanned_image` 时，
      由 `parse_node` 直接 `await`。逐页 JPEG data URL（长边压到 1560、去 alpha、CMYK→RGB）喂 `purpose="vision"`
      的 `LLMClient.chat`，产出 `is_scanned=True` + `prompt_version`；`LLM_MODEL_VISION` 未配置就直接抛
      `parse_vision_unconfigured`，**绝不退回纯文本模型**（文本模型"读"图不报错，只会编出一份完整简历）。
      其余失败分支：`parse_vision_empty` / `parse_bad_image` / `parse_too_many_pages`（上限 20 页）/
      单页不足 20 字记 `warnings`。测试用注入替身跑离线，只断言路由、正文内容块结构、页数与失败码。
      **真扫描件实测搁置**（没模型 id 也没样本），待办见 `docs/TODO.md` §1
- [x] `ner_extractor`：正则打底（手机 / 邮箱 / "N 年经验"）+ LLM 兜底，**每个字段带 `evidence` 原文片段**。
      兜底有硬门：模型给的 `evidence` 抹掉空白后必须在原文里逐字找得到，找不到就当没给并记 `notes`
      （宁可缺失不许编造）。真简历实测 `17626460250`、`3405628625@qq.com`、`2年工作经验` 全部正则白拿，
      只有第一行裸奔的姓名需要问一次模型。手机号正则用前后断言挡住 18 位身份证误命中，`+86` 不进值
- [x] `skill_extractor`：别名归一词典 + 熟练度分级（stub 标注"幻觉高发区"，必须留 evidence）
      → 实做时**没用 LLM**：技术名与程度词都有稳定字形，词典扫描即可，逐条挂整行 evidence。
      程度词取"名字左边最近的那个"，右边的不认；长别名优先（`Spring Boot` 吃掉 `Spring`）；
      同一技能多处出现留程度更高的；命不中的拉丁词进 `unmapped`（开发提示，不进 `errors`）
- [x] `timeline_extractor`：区间合并、重叠检测、年限计算
      → 教育经历也归这里（同一种字形：`名称 [+ 学历/专业] + 空格 + 区间`），M2 的自洽校验本来就要两段一起看。
      年限是**算**出来的，`evidence` 记所依据的区间原文；倒挂区间丢弃并记 `notes`；重叠区间合并后求和。
      字段切分先按 2 个以上空格（PDF 对齐），只切出一段时退回单空格（DOCX/手打）
- [x] `project_extractor`：切块判据分两级——带日期区间的行最可靠，整段没区间才退回
      "短行 + 紧跟子标题"（并写进 `notes` 说明名字是猜的）；子标题分"描述/技术栈/成果"三桶，
      编号要点进 `highlights`，块证据回核原文，对不上就丢
- [x] `parse_node`：`locate_resume_file(upload_dir, resume_id)` →
      `asyncio.to_thread(registry.parse, path)`（图片/扫描件转 async 的 `ImageParser`）→
      **四个抽取器**（ner / timeline / skills / projects）→ 过 `ResumeSchema` 写 `raw_text` / `parsed`，
      模板标记经保留键写进 trace
      （形如 `ner@v1+ocr@v1`，没调 LLM 则空串）。失败分工：文件不存在 `NotFoundError`
      （`parse_file_missing`）、`resume_id` 含 `..`/`/`/`.` 前缀一律 `parse_bad_resume_id`（防路径穿越）、
      抽不出必填字段 `parse_resume_invalid`；年限取时间线复算值，与简历自称的值差超过 1 年时
      把分歧写进 `errors` 交给 M2 判自洽；两边都没有才按 0.0 占位。依赖全在装配期注入
      （`make_parse_node(llm=…, registry=…, upload_dir=…)`），节点内部不 new client

**验收**：用 3 份真简历（PDF 文字版 / DOCX / 图片）跑通；`parsed` 过 `ResumeSchema`；`eval_extraction.py` 能输出字段级 P/R/F1。

进度：PDF 文字版已真跑通（`附件简历-梁清生.pdf` → 1 次 LLM 调用，prompt 1664 / completion 20 tokens，
`parsed` 拿到姓名+手机+邮箱+2 年，`prompt_version=ner@v1`，`errors=[]`）；DOCX 走 `parse_node` 的离线测试已通，
但没拿真 .docx 简历验过；图片那份卡在视觉模型未选型——剩余项与原因记在 `docs/TODO.md`。
2026-10-16 补：四个抽取器全部上线，同一份真简历离线跑（姓名仍走 1 次兜底）产出
`skills` 29 条、`work_experiences` 1 段、`educations` 1 条、`projects` 2 段、`years_experience` 2.1，
`errors=[]`；`pytest` 114 绿。**验收里还差的只有 `scripts/eval_extraction.py` 与另外两份真简历样本。**

### M2 `validate_node`（约 5h）

产出：`agents/nodes/validate_node.py`、`services/resume_service.py`（校验部分）

- [x] 规则先行：必填、日期不倒挂、学历枚举、年限与时间线自洽
      → 全在 `resume_service.validate_resume(raw_text, parsed, today=…)`，纯函数、不碰 LLM 也不碰库。
      **日期不倒挂这条只能重跑 `extract_timeline(raw_text)` 才看得见**：倒挂区间被
      `WorkExperience._check_date_order` 挡在 `parsed` 之外，所以抽取器新增了结构化的
      `TimelineResult.dropped`（带 `reason`）供规则层判定——`notes` 是给人看的，不能当证据。
      学历枚举除了查值域（`get_args(Degree)`），还查"字样在抽取词表里但不在别名表里"
      （`MBA` 会被静默归成 `other`，枚举合法但信息丢了）。
      顺带一道免费的证据核对：`parsed` 里证据在原文找不到的条目直接删（记 `repairs` 不记 `issues`，
      幻觉删掉就行，不值得为它重跑 parse）
- [x] 规则全过再让 LLM 反思"是否抽取错位"，产出 `validate_issues`
      → `get_prompt("validate")` + `chat_json("extract", …, temperature=0.0)`，
      schema `{"issues": ["…"]}`。规则没过时**一次都不问**（测试断言 `fake.calls == []`）。
      模型的意见必须点出已知字段名才放行（`_KNOWN_FIELDS` 由 `ResumeSchema.model_fields` 推出来），
      否则确定性重跑会把它的主观批评变成空烧预算。`LLMError` / `LLMResponseError` 当"审稿人缺席"
      吞掉：规则已经点头，别为了反思环节坏了就把整图拦住或让 parse 白跑
- [x] **计数由本节点自增**（路由用 `<=`，语义见 `workflow.py` 注释）
      → 只在 `validate_issues` 非空时 `+1`，没判出问题就不写这个键
- [x] 修正后的 `parsed` 覆盖写，原始结果留在 `raw_text` 以便回放
      → 规则层在 `dict(parsed)` 副本上修，入参永不改动（有测试盯着）；`errors` 里逐条写清改了什么。
      `parsed` 是 None 时**不写这个键**，别把 None 覆盖成空 dict
- [x] 测试：构造"工作区间倒挂"样本，断言 `parse` 恰好重跑 `validate_max_retries + 1` 次
      → `tests/test_agents/test_validate_node.py::test_a_dirty_resume_uses_up_the_budget_and_still_walks_to_the_end`，
      真节点 + 假 parse 装进 `build_agent_graph`，跑 `AsyncSqliteSaver`

**验收**：脏简历能在预算内收敛；预算耗尽后带 `validate_issues` 继续往下走（不中断整图）。

进度（2026-10-16）：倒挂样本的实测形状是 parse 跑 3 次（`validate_max_retries=2` + 1）、
validate 3 次、`validate_retries=3`、`validate_issues` 剩 1 条（规则是确定性的，同一条问题重复三轮），
然后 `match`/`profile`/`plan`/`generate`/`review` 照常走完。真简历 `附件简历-梁清生.pdf` 离线跑规则层
`issues=[] / repairs=[]`，也就是说干净简历只可能花一次反思调用。新增 24 条测试，全仓 `pytest` 138 绿。

**真调用实测**（`app/scripts/live_check.py`，一次 2 个调用 ≈ 6.5k prompt / 0.4k completion token）：
第一版 prompt 让模型自己判时间，它把 `2026-06-30` 说成"晚于今天"，给了 `today` 之后**照样说错**
——日期比较不是它的强项。于是把时间判断收回规则层：`validate.system.j2` 明写"时间/日期/年限一律不要判"，
`validate.user.j2` 传入 `today`。改完再跑，7 条意见里的 3 条日期幻觉消失，剩下 6 条全是可点名的语义错位
（`highlights` 混入 `1. 权限管理体系` 这类分组标题行、`成功验收交给市场部推；广` 这种被切断的半截句），
已录成 `tests/fixtures/llm/validate_liang_qingsheng.json` 并配回放测试
（只断言"每条意见都点得出 `ResumeSchema` 字段名"这类闸门行为，不锁文案）。
遗留见 `docs/TODO.md` §2 与 §2.1：规则层判不出"看着干净其实抽错了"的简历，
而反思挑出来的真问题恰恰证明**回退预算现在是纯浪费**（parse 确定性重跑，三轮结果一模一样）。

### M3 持久化出口（约 6h，无 LLM）— 已完成 2026-10-16

产出：`models/*`、`repositories/*`、`migrations/`、`alembic.ini`、`scripts/seed.py`

- [x] `models/base.py`：`DeclarativeBase` + id/created_at/updated_at mixin
      `NAMING_CONVENTION` 让约束名可预期（SQLite 不能 `ALTER ... RENAME CONSTRAINT`，
      将来 `op.drop_constraint("名字")` 得写得出这个名字）。主键 `String(32)` = `uuid4().hex`；
      时间戳**在 Python 侧**取 naive UTC——服务端默认值要 flush 后回读，异步下是
      `MissingGreenlet`。`__tablename__` 在每个模型里显式写死（2.1 上缺它会直接
      `InvalidRequestError`，类名推导不可靠）。
- [x] `resume` / `job` / `question` / `interview` 四张表；**JSONB 改 SQLAlchemy `JSON`**（§6 风险 2）
      抽取结果整份存 JSON，不拆子表（字段集还在动，而它只有"整取整放"一种用法）。
      `question` 带 `batch` + `order_index` 与唯一约束（§6 风险 5 的落库侧），
      `interview.job_id` 是 `SET NULL`、其余外键 `CASCADE`。取值域用 `CheckConstraint` 兜底。
      **不写 `relationship()`**：见 ADR-0007。
- [x] `BaseRepository` 泛型 CRUD + 四个子类
      泛型用 PEP 695 语法（`class BaseRepository[ModelT: ORMModel]`）。四条边界：
      不 commit（只 flush）、`get_by_id` 找不到返回 None 而不抛 404、`order_by` 走
      `model_class.column_names()` 白名单、只返回 ORM 实例不返回 dict。
- [x] 配 `alembic.ini` + `migrations/env.py`（现在 `make migrate` 必失败，因为 alembic 配置文件根本不存在）
      `alembic init` 落进临时目录再搬 `env.py` / `script.py.mako`（`migrations/` 已存在时
      alembic 拒绝初始化）。URL 从 `ALEMBIC_DATABASE_URL` 或 `get_settings().database_url` 取，
      ini 里 `sqlalchemy.url` 留空；迁移走同步引擎。**`alembic.ini` 必须是纯 ASCII**：
      alembic 用 locale(GBK) 读它，中文注释会在迁移执行前就崩。
      首个迁移 `2056a1287299 (initial tables)`，`alembic check` 无漂移。
- [x] `seed.py`：灌 2 个岗位 + 3 份简历
      `app/scripts/seed.py`，跑法 `PYTHONPATH=. .venv/Scripts/python.exe -m app.scripts.seed`。
      幂等靠自然键：岗位按 `(title, company)` upsert，简历用固定 id `seed-resume-1..3`。
      全是合成样本（林可/周砚/苏澈），不用带 PII 的真简历；每条 `evidence` 都能在
      `raw_text` 里逐字找到，且**通得过我们自己的 `validate_resume`（0 issues / 0 repairs）**。
      栏目名必须独占一行、不含数字且短于 12 字，否则 `extract_timeline` 一个区间都找不到。
- [x] 引擎工厂：`create_async_engine(settings.database_url)`，SQLite 开 `PRAGMA journal_mode=WAL` 与 `busy_timeout`
      `app/utils/db.py`：`build_engine` / `build_sessionmaker`（`expire_on_commit=False`）/
      `session_scope` / `build_sync_engine`。三条 PRAGMA 在 connect 事件里发，其中
      `foreign_keys=ON` 是级联删除能生效的前提。相对路径的库文件会先建目录。
      **显式拒绝 `:memory:`**（每连接一份库，跨会话读不到）。
      踩坑：`sqlalchemy` 必须带 `[asyncio]` extra（拉 `greenlet`），Py 3.14 上 pip 不会自动装。

**验收**：`python -m alembic upgrade head` 成功建库；重启进程后数据仍在；`pytest` 用独立临时库互不干扰。
→ 三条都由测试固定住，且前两条是**真子进程**：`tests/test_migrations.py` 用
`subprocess` 跑 `upgrade head` / `downgrade base` / `check`，并让 seed 进程与读取进程分开；
`tests/test_repositories/test_isolation.py` 靠 `tmp_path` 证明用例互不干扰。
基线从 140 涨到 **193 passed**（+53），ruff / mypy（94 files）全绿。

**收尾补的最后一块**（2026-10-16 同日）：`app/dependencies.py` 的引擎单例与 `get_db` 依赖。
`get_engine()` 按 `settings.database_url` 建进程内唯一引擎（URL 变了才重建，换时
`dispose(close=False)` 不阻塞事件循环），`get_sessionmaker()` 不缓存工厂，`get_db` 是
生成器依赖：端点正常返回才提交，抛异常回滚后原样抛出。`app/scripts/seed.py` 改成走同一个
单例，于是 **`app/` 里 `build_engine()` 只剩 `app/dependencies.py` 一个调用点**
（测试为了造"第二台引擎"会直接调工厂，那是刻意的对照，不算绕过）。
`tests/test_dependencies.py` 用真 ASGI 应用钉住提交/回滚两条（生成器依赖的收尾时机在响应之后，
手工驱动生成器近似不出来）。仍待 M4 的是 lifespan 里调 `dispose_engine()`。
`models/user.py` 仍是空壳（认证属阶段 2，见 `docs/TODO.md` §3）。

### M4 `match` / `profile` / `plan`（约 8h）— 已完成 2026-10-20

产出：三个节点 + `services/match_service.py`、`services/profile_service.py`

- [x] `match`：JD 要求项拆分 → 与 `parsed.skills` 对齐 → 输出 `matched_gaps`，每条带 `requirement` / `level` / `evidence`
- [x] `match` 的检索分支留接口不留实现（阶段 6 接 sqlite-vec）
- [x] `profile`：按 rubric 给每个维度 1–5 分 + 证据句，输出 `profile`
- [x] `plan`：读 `settings.question.total` 与 `difficulty_ratio`，先攻弱项，输出 `plan.items`
- [x] `plan` 必须**确定性**：同一 `profile` 两次结果一致（不调 LLM 或调了也固定 seed）

**验收**：`plan.items` 的 `count` 之和 == `settings.question.total`；难度分布与 `difficulty_ratio` 一致（误差 ≤ 1 题）。

**验收记录（2026-10-20）**：

- 两条验收都在**真图**上验的，不只是单函数：`tests/test_agents/test_m4_pipeline.py` 用
  match/profile/plan 三个真节点 + 其余四个占位节点装配 `build_agent_graph`，跑通后断言
  `sum(item["count"]) == settings.question.total`（15）且每个难度档与 `ratio × total` 差 ≤ 1
  题；同一份 state 跑两遍，`plan` 的 `json.dumps(..., sort_keys=True)` 逐字节相等。
- 配额算法是**最大余数法**（Hamilton）：`difficulty_quotas(15, 0.3/0.5/0.2)` =
  `{easy: 5, medium: 7, hard: 3}`。参数化网格覆盖 total ∈ {1,2,3,7,15,17,40} × 5 组配比，
  全部满足"总和恰等于 total、每档误差 ≤ 1"——包括 `total=1` 这种余数会全给同一档的极端值。
- 槽位按弱项优先轮转分配，`profile.focus_topics` 为空时退到"最弱维度 + 综合考察"，总数照样对得上。
- LLM 花费：JD 通篇点得出词典技术名时 `match` **一次模型都不花**（测试直接断
  `fake.calls == []`）；有软要求才花 1 次，`profile` 固定 1 次，`plan` 0 次。整条 M4 链路上限 2 次。
- `plan` 不调模型这一点是被测试钉住的：`make_plan_node(llm=…)` 抛 `TypeError`——
  防止后来者"顺手"给它接个模型，把确定性赌在固定 seed 上（DeepSeek 也不给 seed）。
- 三道闸门各自有测试：模型的 JD 判定必须对得上待判定清单、证据必须能在简历原文里逐字找到，
  否则降级 `unverified`；`profile` 的叙述句子必须点名一个已知维度或技能，否则丢弃并换规则版；
  模型自评置信度只作上限（`min(规则版, 模型版)`）。
- 检索分支落在 `app/vectorstore/retriever.py`：`SkillRetriever` Protocol + `NullRetriever`。
  测试用一个记录型假向量库钉住调用形状（收到要求原文 + `settings.retrieval.top_k_final`，
  召回片段进 `passages_json`），阶段 6 换实现时 `match_node.py` 一行都不用改。
- 一处由测试暴露的行为修正：栏目标题的判定必须**早于**长度过滤。`加分项` 只有 3 个字，
  先过长度门槛就被当残句丢掉，它下面那批加分要求会被错标成正经要求，`profile` 的
  岗位匹配度分母跟着错。
- 基线：193 → **321 passed**（+128），ruff / mypy（95 files）全绿。
- `docs/adr/0009-deterministic-m4-layers.md` 记这次的分层决定（分数为什么不能来自模型、
  rubric 阈值为什么留在代码常量而不是 yaml）。

### M5 `generate` + `review`（约 10h，本计划最重）— 已完成 2026-10-20

产出：两个节点 + `services/question_service.py`、`services/review_service.py`

- [x] `generate`：逐条 `plan.items` 出题，题干强制带 `source_quote`（简历原文引用），无引用即判不合格
- [x] `Send` 并行扇出：按 `plan.items` 动态派发
- [x] ⚠️ 扇出前必须把 `questions` 通道改成带 reducer 的累积通道，且**回退重做时要能清空历史**（见 §6 风险 5）
- [x] `review`：独立 prompt、独立模型槽位（`llm_model_review`），检查重复 / 无依据 / 难度名不副实
- [x] `review` 通过后写 `finished=True` 并落库（调 M3 的 repository）
- [x] ~~测试：`review_issues` 占比 > `review_fail_ratio_threshold` 时，`generate` 恰好重跑预算次数后收尾~~
  **2026-10-24 作废**，见下面那条注。

**验收**：一份真简历端到端产出 15 题，其中带 `evidence` 的比例 ≥ 90%；`trace` 里能看到每次回退。

> **ADR-0015（2026-10-24）把 M5 的"质量门"整块拆了**，本节以下凡提到占比 / 阈值 /
> `should_redo` / `review_retries` / 回退重做 / `aborted` 不落题目的，都是**当时的落法，现已失效**：
> `review` 现在只出批注，`generate` 一次成卷，题目一律落库、状态恒 `completed`，
> `trace` 里 generate/review 各只出现一次。仍然有效的部分是扇出形状、批次与 `Overwrite`、
> 题号在扇出前算、`GenerateJob` 载荷清单、引用按来源核对、独立评审槽位与温度 0。
> 决策与代价看 `docs/adr/0015-review-annotates-only-no-gate.md`。

**验收记录（2026-10-20）**：

- 两条验收都在真简历上跑出来的：`scripts/run_e2e_live.py tests/fixtures/resumes/附件简历-梁清生.pdf`
  → **15 题、带可回溯引用比例 100%、`trace` 里 2 轮 `generate`/`review` 交替（1 次回退）、
  落库 `status=completed` + 15 行 `question`**。脚本自己判 PASS/FAIL（退出码非 0 即不合格），
  顺带打印 trace、配额表、每题引用标记和全部评审批注——不合格时不用查库就能定位。
- 字段名落的是 `evidence` 而不是 `source_quote`：M0 的 `QuestionSchema` 与 M3 的 `question`
  表都用前者，抽取层四个抽取器也用它（同一判据 `evidence_belongs`）。语义没变，只是改名。
- **引用按来源分档核对**，不是只认简历原文：`source=resume` 对 `raw_text`、`source=jd`
  对 `jd_text`、`generic` 可选。第一次端到端只有 **25%**，根因是 26 个槽位里 21 个是
  `source=jd`，而 prompt 只教模型抄简历——那一类题永远带不上引用，重试多少次都过不了 90%。
  出题闸门（`verify_evidence`）与评审规则层共用同一个判据（`evidence_problem`），
  不允许出现"generate 按 JD 放行、review 按简历判废"的两套标准。
- 风险 5 实测（`scripts/probe_send_fanout.py`）：`Send` 的载荷**就是分支看到的全部输入**，
  分支返回值仍按主图 reducer 合并。所以 `questions` 是 `operator.add` 累积通道，
  回退重做由扇出节点写 `{"questions": Overwrite([])}` 整批换掉；机制与"不叠题"都有测试。
  副产品是风险 6 免费拿到了：旧批次的题天然进不了分支，模型抄不到上一版。
- 题号（`order_index`）在扇出**之前**用 `count` 累加算好，分支各自拿一段偏移；
  测试故意让最后一条分支先完成，报告里的题号照样是 0/1/2。
- 一条槽位 = 一次 LLM 调用（`temperature=0.3` 抖措辞，避免重做时逐字复读）。
  不改成"一次出 15 题"：`default_max_tokens=2048` 装不下 15 题的题干+参考答案+评分标准，
  而一次长输出任何一处 JSON 破损就整批归零。单槽位失败只少那一槽的题，
  差额由 `review` 按"距 `plan.total` 少几题"判成批注驱动回退，分支自己不抛异常也不计重试。
- 质量门判据只有一个来源 `should_redo`（节点与路由函数都调它），`fail_ratio` 分母取实际到手
  题数；**空批次算 1.0**（有批注就算 100% 失败），否则"整批出题失败"会被当成 0 占比放行。
  预算用尽仍不合格 → `status=aborted` 且**一行题目都不写**，批注留在 `interview.review_issues`。
- 评审是**独立槽位 + 独立 prompt**（`llm_model_review`，`temperature=0.0`），生成时的推理一个字都不传。
  规则层先判（字面重复、引用回溯、数量缺口），规则已超阈值就**不再花模型的钱**；
  评审模型坏了则**原样重抛**——它是质量门的另一半，吞掉等于替没人检查过的题开绿灯。
- 两次真跑暴露的两个 prompt 缺陷，都按"确定性优先"补掉了：
  1. 评审拿不到 JD 原文，把逐字出自 JD 的合法引用报成"简历里查无此句"（15 条批注里 9 条误报）；
  2. 扇出的固有盲区——分支看不见兄弟槽位，同主题的各难度档各自写出"那道最经典的题"，
     换皮重复稳定在 5/15=0.33，正好压在 0.3 的门上出不来。现在的载荷里带一份 `roster`
     （同批其它槽位的 `topic`/`difficulty`/`count`，不含自己），配合"再考同一个知识点就换层面"的
     硬规则，第三跑降到 4 条批注、1 轮回退收工。
- 评审 prompt 显式禁止日期推算（"2026.06 是否晚于今天"这类），沿用 AGENTS.md §7 那条实测结论。
- 花费：端到端 3 模型轮次 = 33 次调用、prompt 137,542 / completion 19,470 tokens（deepseek-chat）。
  其中一次槽位空手而归（模型给的 `reference_answer` 是空串被闸门挡下），缺口由回退补上——
  这正是设计想要的行为，不是故障。
- 基线：321 → **391 passed**（+70：规则层 30 + 两个节点 22 + 图级 9 + 落库 9），
  ruff / mypy（95 files）全绿。测试全程离线，LLM 一律替身，只断言结构/数量/批次/预算。
- `docs/adr/0010-send-fanout-and-batch-overwrite.md` 记这一层的四个决定：扇出与批次覆盖、
  引用按来源核对、评审独立槽位与失败必须重抛、`aborted` 不落题目。

### M6 评测与后台化（约 6h）

产出：`scripts/eval_e2e.py`、`scripts/eval_rag.py`（留壳）、`tasks/resume_parsing.py`、`tasks/question_generation.py`

- [x] 用 `BackgroundTasks` 替代 Celery：API 立即返回 `task_id`，图在后台跑，进度写库
      → `app/tasks/{resume_parsing,question_generation}.py` + `app/api/v1/{resumes,interviews}.py`
- [x] `GET /tasks/{task_id}` 读进度（`api/v1/tasks.py`）
- [x] `eval_e2e.py`：可用率 / 幻觉率 / token 成本三指标，跑 10 份样本简历
      → 脚本与三指标口径已落地（`scripts/eval_e2e.py`，算式有离线校验）；
      **样本只有 1 份、真实数字被账户余额 402 卡住** → `docs/TODO.md` §2.4
- [x] 成本护栏：单次运行 token 上限，超限在 `generate` 前中断并抛 `AppError`
      → `generate_node._require_budget` 抛 `BudgetExceededError`（429 / `cost_budget_exceeded`）

**验收**：上传简历 → 轮询 → 拿到 15 题，全程不阻塞事件循环；`eval_e2e.py` 输出三指标数字。

#### 验收记录（2026-10-20，M6 收尾）

- 后台链路**真跑通了**：`POST /resumes/upload`（上传 PDF）与 `POST /interviews/generate` 都立刻返回
  `202` + `task_id`；图在后台跑完整七节点，`task.progress.steps` 逐 super-step 增长，
  回退轮在步序里看得见（`parse` / `validate` 各出现多次、`generate` 之后紧跟扇出批次）。
  图跑了 **80–83 秒**，同期反复轮询 `GET /tasks/{task_id}` **中位延迟 5.0 ms**，
  事件循环没被堵住——这条验收的"全程不阻塞"是实测数字，不是推断。
- **"拿到 15 题"这一条要分两次看**：图里每轮都造满 15 题的批次，但 `review` 质量门两次把它判死
  ——批注占比 6/15=0.40 与 5/15=0.33，阈值 0.3，于是 `interview=aborted`、`total_questions=0`、
  一行题目都没落库（ADR-0010 的
  "留状态、不留内容"）。同简历同 JD 用 `scripts/run_e2e_live.py` 对照跑一次是 **PASS**
  （15 题落库、`completed`、带可回溯引用 100%、3 条批注、213,783 tokens）——
  所以卡住验收的是 **M5 就存在的模型质量在门限附近抖动**，不是 M6 的后台化回归。
  根因写进 `docs/TODO.md` §2.4（`plan` 槽位主题重复 + 题干带"至今/目前"这类日期相对措辞），
  没去调阈值。
- 第三次真跑被上游 **402 Insufficient Balance** 挡住。顺带补了一条分类：`openai.APIStatusError`
  以前会被当未分类异常落库（`internal_error`），现在进 `LLMError`（502）并把 `status_code`
  写进 `details`，且**不走传输重试**（余额不足重试三轮只是白烧三次往返）。
- 提交前必须知道的一条实测结论：FastAPI 0.143 的 `BackgroundTasks` 跑在依赖收尾**之前**
  （`dep-enter → endpoint → background → dep-exit`，`scripts/probe_background_order.py` 可复跑），
  所以唤起后台任务的写服务**自己持有事务**（收 `sessionmaker`、用 `session_scope` 提交），
  端点注入 `SessionMakerDep`。这条与其余四个决定记在 `docs/adr/0011-backgroundtasks-and-task-progress.md`。
- 成本护栏按契约就位，但实测**有 overshoot**：一条完整链路烧了 213,783 tokens，上限 200,000，
  护栏没触发——检查点在每次扇出**之前**，越线那批已在飞行中，超出量约等于一轮扇出。
  触发路径有测试：`tests/test_agents/test_generate_node.py` 与
  `tests/test_tasks/test_question_generation.py` 各把 `max_tokens_per_run` monkeypatch 成一个
  小于替身用量的小数，断 `BudgetExceededError`（以及图内视角下它被记成 `failed` + `aborted`）。
- `scripts/eval_e2e.py` 三指标的算式离线验过（合成终态：可用 1/4、幻觉 1、重复 1，
  数字与手算一致），`scripts/eval_rag.py` 是**留壳**：它打印要测的三件事、卡住的原因
  （没有真检索器、没有 embedding、没有标注集）并返回 2。
- 基线：391 → **438 passed**（+47，落在任务层、API 端点、服务层事务边界、LLM 错误分类与
  成本护栏五处），ruff / mypy（**101 files**）全绿。API 用例真起 `create_app()`，
  后台任务用假协程注入，LLM 全程替身。

---

## 4. 节点契约（写节点前必读，违反即返工）

1. **只返回增量**：`return {"parsed": ...}`，绝不返回整个 state，绝不原地改入参。
2. **只追加通道**（`trace` / `errors`）返回 `[新元素]`；返回完整列表会冲掉历史。
3. **不许自己 new LLM client**：一律 `LLMClient`（README 铁律）。
4. **prompt 必须走 registry 且带版本号**，版本号写进该节点的 `trace` 条目，否则无法归因"哪版模板变差了"。
   落地方式（`workflow.PROMPT_VERSION_KEY`，已实现并有测试）：节点在返回 dict 里带保留键
   `"prompt_version": prompt.version`，`traced()` 把它**取走**（`pop`）后写进 `TraceStep.prompt_version`，
   因此它不会变成 state 通道；节点不要自己往 `trace` 里塞步骤。
5. **可恢复失败**分两种（ADR-0015 之后只剩一种有门）：`validate` 写 `validate_issues` + 自增计数，
   让 `validate → parse` 的回退门生效；`review` 的 `review_issues` 是**纯观测批注**，不驱动路由。
   **不可恢复失败**抛 `AppError` 子类。不要在节点里 `except` 后静默返回空结果——吞掉必须同时留下一条批注。
6. **节点必须幂等**：回退会让 `parse`/`validate` 重跑，同一线程重放会让 `generate` 重跑，重复执行不得产生重复数据（落库只在 `review` 的收尾处做，且只写一次）。
7. **节点不直连数据库**：读走 service/repository，写只在收尾，保持 `api → services → repositories` 分层铁律。
8. **每个 LLM 调用声明 token 预算**，取 `settings.llm.default_max_tokens`，不许裸调。

### 4.1 `AgentState` 通道速查（`app/agents/state.py`）

15 个键全部 `total=False`：节点未写的键沿用上一个 checkpoint 的值。

| 分组 | 键 | 写入者 | 合并语义 |
|---|---|---|---|
| 输入 | `resume_id` `jd_text` | API / BackgroundTasks 启动时 | 覆盖 |
| parse·validate | `raw_text` `parsed` | `parse` | 覆盖 |
| validate | `validate_issues` `validate_retries` | `validate`（计数自增） | 覆盖 |
| match·profile·plan | `matched_gaps` `profile` `plan` | 各节点自己 | 覆盖 |
| generate·review | `questions` | `generate`（扇出节点） | **累积 `operator.add` + 回退时 `Overwrite([])` 整批换掉**（M5 实测，见 §6 风险 5 与 ADR-0010） |
| review | `review_issues` | `review` | 覆盖。**纯观测批注**：`review_retries` 通道与质量门已随 ADR-0015 删除 |
| 收尾 | `finished` | 末节点 | 覆盖 |
| 观测 | `trace` | `traced()` 包装层 | `operator.add` 只追加 |
| 观测 | `errors` | 节点写可恢复失败 | `operator.add` 只追加 |

两条容易踩的规则：

- **回退预算的 `<=`**：`route_after_validate` / `route_after_review` 用 `<=` 比较，因为计数由失败节点
  先自增。`validate_max_retries=2` 表示 `parse` 最多额外重做 2 次（共 3 次）；写成 `<` 会少跑一轮。
- **保留键 `prompt_version`**：不是通道。`traced()` 会 `pop` 掉它写进 trace；节点若指望它留在 state 里
  一定拿不到。
- **`validate` 的数据源是 `raw_text` 不是 `parsed`**：`parsed` 只装得下"合格"的条目，脏数据
  （倒挂区间、越界枚举、幻觉证据）恰恰都在它外面。所以校验层重跑 `extract_timeline(raw_text)`
  读 `TimelineResult.dropped`，而不是去 `parsed` 里找倒挂。（M2 踩过的坑，见 §3 M2 第一条。）

---

## 5. 测试策略

| 层 | 手段 | 进 CI |
|---|---|---|
| 图骨架 | 假节点（已有 `tests/test_agent_infra.py` 范式） | ✅ |
| 单节点 | `LLMClient` 注入替身（`_FakeCompletions` 形状） | ✅ |
| 真实响应 | 录制回放：真调一次存 `tests/fixtures/llm/*.json`，之后离线跑 | ✅ |
| 抽取质量 | `eval_extraction.py` 出 P/R/F1，只看不断言 | ❌（会因模型抖动红） |
| 端到端成本 | `eval_e2e.py` 手动跑 | ❌ |

原则：**任何断言 LLM 具体文案的测试都不许存在**，只断言结构、数量、预算、通道变化。

已录制的回放：`tests/fixtures/llm/ner_liang_qingsheng.json`（ner 兜底对真简历的一次真调，
测试只回放它的 `response` 字段，验证据核对门能否放行 `梁清生`）；
`tests/fixtures/llm/validate_liang_qingsheng.json`（M2 反思对真简历的一次真调，
回放它只验"意见必须点得出字段名"这道闸门）。两份都由 `app/scripts/live_check.py` /
真调脚本手动生成，**改 prompt 前后各跑一次才看得出差别**，别让测试自动刷它们。

---

## 6. 风险与待决策

| # | 风险 | 处置建议 |
|---|---|---|
| 1 | ~~PyMuPDF vs pypdf 未定~~ **已决（2026-10-15）** | 选 PyMuPDF：三家实测只有它默认按块分栏，pypdf/pdfplumber 都会把双栏粘成一行；代价是 AGPL-3.0 双许可与 54MB 体积。见 `docs/adr/0006-pdf-engine.md`，遗留：需拿真中文简历复验 CJK 解码 |
| 2 | ~~`models/resume.py` 写着 JSONB，SQLite 不支持~~ **已决（2026-10-16，M3）** | 四张表全用 `sqlalchemy.JSON`（方言中立），抽取结果整份存不拆子表；见 `docs/adr/0007-sqlite-persistence-layer.md`。代价：JSON 列不能增量更新，改一个字段要整份读写 |
| 3 | `middleware/rate_limit.py` 要 slowapi，未声明依赖 | M6 前定；单机限流也可用 `aiolimiter` 或自研令牌桶 |
| 4 | SQLite 并发写：`BackgroundTasks` 并发 + checkpoint 同库可能 `database is locked` | WAL + `busy_timeout=5000` 已由 `app/utils/db.py` 在 connect 事件里统一发（顺带 `foreign_keys=ON`，级联删除的前提）；checkpoint 已独立成 `data/checkpoints.db` |
| 5 | ~~`Send` 扇出与回退重做冲突~~ **已验（2026-10-20，M5）** | 机制先由 `scripts/probe_send_fanout.py` 实测：分支只收到载荷，`Overwrite` 每个 super-step 只生效一次。落法是 `questions` 走 `operator.add`，回退时**扇出节点**写 `{"questions": Overwrite([])}`；题号用扇出前算好的 `order_start`。落库侧：`question.batch` + `(interview_id, batch, order_index)` 唯一约束，`list_for_interview()` 默认只取最新批次，旧批次留成"上一版为什么被否"的痕迹。见 ADR-0010 |
| 6 | ~~`review` 与 `generate` 同上下文时模型倾向给自己高分~~ **已处置（2026-10-20，M5）** | 独立 prompt + 独立模型槽位（`llm_model_review`，温度 0）+ 不传生成时的思维过程。附带拿到两件事：分支看不见旧批次（`Send` 载荷隔离），重做时靠 `temperature=0.3` 抖措辞避免逐字复读；实测中还暴露出评审必须看见 JD，否则把合法引用判成幻觉——见 ADR-0010 与计划书 M5 验收记录 |
| 7 | `LEARNING_ROADMAP.md` 不存在，全仓 §编号无处可查 | 要么补写，要么把 §编号从 docstring 里去掉，别留假引用 |
| 8 | 本机无 `make` / `uv` | README 补等价 PowerShell 命令，或提供 `*.ps1` |

---

## 7. 明确不做

PostgreSQL / pgvector / Celery / Redis / Langfuse 自托管 / PaddleOCR —— 全部已用嵌入式方案替代或推迟，见 `docs/adr/` 与 README。

---

## 8. 待写 ADR

- [ ] ADR-001 LangGraph checkpoint 用独立 SQLite 文件，不与业务库共表
- [ ] ADR-002 传输重试（tenacity）与校验重试（chat_json）分两层，参数来源不同
- [ ] ADR-003 langgraph 1.2.15 `add_node` 类型重载与 mypy 2.4 不兼容，采用单点定向 ignore
- [x] ADR-004 异步任务用 FastAPI `BackgroundTasks`，不引入 Redis/Celery
      → `docs/adr/0011-backgroundtasks-and-task-progress.md`（短号 004 与 `docs/adr/README.md`
      里已占用的 0004"检索方案"撞号，故按 000x 序列编成 0011——两套编号并存的收尾账在
      `docs/TODO.md` §3；含实测的 `dep-enter → endpoint → background → dep-exit` 顺序与它逼出来的
      "唤起后台任务的写服务自己提交事务"这条后果）
- [ ] ADR-005 回退计数由失败节点自增、路由用 `<=`，使 `*_max_retries` 表示"额外重做次数"
- [x] ADR-0006 PDF 解析引擎选型（PyMuPDF，AGPL-3.0 代价与退路）→ `docs/adr/0006-pdf-engine.md`
- [x] ADR-0007 持久化层（SQLite + `sqlalchemy.JSON`、uuid hex 主键、repository 不提交事务）
      → `docs/adr/0007-sqlite-persistence-layer.md`
- [ ] ADR-0008 扫描件不做本地 OCR，交给多模态 LLM（Python 3.14 无 PaddleOCR 分发；代价是必须配 `LLM_MODEL_VISION`、逐页计费、识别质量取决于模型，且 `□` 占位规则要防幻觉）
      —— 编号原为 0007，M3 的持久化层决策先占用了它，故后移一位
- [x] ADR-0009 M4 三层的确定性分工（分数归规则、措辞归模型、配额归算术；检索留接口；rubric 阈值留在代码常量）
      → `docs/adr/0009-deterministic-m4-layers.md`
- [x] ADR-0010 出题层的扇出形状（`Send` + `Overwrite` 批次覆盖、引用按来源核对且两侧同判据、
      评审独立槽位与失败必须重抛、`aborted` 不落题目、扇出盲区用 `roster` 摘要补）
      → `docs/adr/0010-send-fanout-and-batch-overwrite.md`

ADR-0006 是 §6 风险 1 的处置记录，编号接在 `docs/adr/README.md` 的 0001-0005 之后；上面 ADR-001~005 是计划书内的短编号，落文件时按 README 的 000x 续号，别两套编号并存太久。
