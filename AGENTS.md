# InterviewForge — 项目规则集（AGENTS.md）

本文件是**给 AI 协作者读的**执行约定。设计细节看 `docs/LANGGRAPH_BUILD_PLAN.md`，
欠账看 `docs/TODO.md`，架构决策看 `docs/adr/`。三份文件冲突时以本文件的"分层铁律"和
"节点契约"为准，其余以计划书为准。

## 1. 这个项目是什么

简历解析 → 面试题生成的 LangGraph 工作流（7 个节点：parse → validate → match → profile
→ plan → generate → review）。**学习优先**：每个模块文件顶部一行 docstring 标注它的职责
与"应在哪个阶段实现"，改动时别把这类标注删掉。

进度：M0 契约层、M1 解析链、M2 `validate_node`、M3 持久化出口、M4 `match`/`profile`/`plan`、
M5 `generate`/`review`、M6 评测与后台化已完成（M0–M6 全部落地）。M5 的"质量门"在
M6 之后被拆掉（见 ADR-0015）：`review` 不再拦题目，只出批注，图里只剩 `validate → parse`
一张回退边。M6 之后另外补了三块：
`frontend/` 控制台（Vue 3 + Vite + Element Plus 的构建产物挂在 `/`，见 ADR-0016；
它取代了 ADR-0012 的零构建静态页）、写入侧 CRUD
（岗位建/上传/改/删、简历改名/删、报告头的简历·岗位快照列，见 ADR-0013）与
岗位的图片录入（`POST /jobs/inspect-image` 回草稿，见 ADR-0014）。
里程碑由维护者**引用计划书的小节标题**来开工，不要自己挑任务。

## 2. 环境与命令（实测，别再试）

- Python **3.14.7**，只能用 `.venv/Scripts/python.exe`。裸 `python` 在此机器上退出码 49。
- 没有 `make`、没有 `uv`。`Makefile` 里的目标在本机不可用，用下面的等价命令。
- Shell 是 Git Bash（POSIX sh），控制台是 **GBK**。任何读写文本必须显式
  `encoding="utf-8"`，否则中文直接抛 `UnicodeDecodeError`。
- 源码里不要写字面零宽字符（会被 GBK 控制台打印成乱码），统一用 `"\u200b"` 转义。
- 临时探针脚本需要 `PYTHONPATH=.`。
- **`alembic.ini` 只能 ASCII**。alembic 用 `ConfigParser.read(..., encoding="locale")` 读它，
  本机 locale 是 GBK，写中文注释会在任何迁移执行之前抛 `UnicodeDecodeError`（实测 2026-10-16）。
  说明文字放 `migrations/env.py` 或 `docs/adr/`。
- `sqlalchemy` 必须带 `[asyncio]` extra 安装（它拉 `greenlet`）。Py 3.14 上 pip 不会自动装，
  缺它时 `import sqlalchemy.ext.asyncio` 直接失败。已写进 `requirements.txt`。
- SQLite **不支持 `:memory:`**：每个连接一份独立库，异步连接池一换连接就看不到上一个连接的表。
  `app/utils/db.build_engine()` 显式拒绝它，测试一律用 `tmp_path` 下的临时文件库。

```bash
.venv/Scripts/python.exe -m ruff check app tests migrations   # line-length=100，按东亚宽度计
.venv/Scripts/python.exe -m mypy app                          # mypy 2.4 + pydantic 插件
.venv/Scripts/python.exe -m pytest -q                         # 离线；只有迁移用例真起子进程
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m alembic upgrade head   # `make migrate` 的等价命令
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m alembic check          # 模型/迁移漂移检查
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m app.scripts.seed       # 灌 2 岗位 + 3 简历，可反复跑

cd frontend && npm run typecheck                              # vue-tsc，构建前先过这一关
cd frontend && npm run build                                  # 产出 frontend/dist；后端只读它
cd frontend && npm run dev                                    # 调 UI：127.0.0.1:3000，/api 与 /docs 代理到 8000
```

**提交前必须三条全绿**（当前基线：485 passed / ruff clean / mypy 102 files clean；
ADR-0015 删掉质量门时一并删了两条门限用例）。动了 `frontend/` 另加
`npm run typecheck` 通过；产物 `frontend/dist/` 与 `node_modules/` 不进库。

## 3. 分层铁律

`api → services → repositories`。`api` 不 import models，`service` 不写 SQL，
**节点不直连数据库**。所有 LLM 调用经 `app/utils/llm_client.py`，节点/抽取器内部
绝不 `new LLMClient`——依赖在装配期注入（`make_parse_node(llm=…, registry=…)`）。

持久化层（M3，ADR-0007）追加四条：

- **只有 repository 写 SQL**，且 repository **只 `flush` 不 `commit`**；提交时机归调用方
  （`app/utils/db.session_scope`）。一份报告要写 `interview` + N 行 `question`，
  拆成多个事务就没有幂等可言。
- **`get_by_id` 找不到返回 None**，不抛 `NotFoundError`——404 是 API 层的语义。
- **模型只用 `sqlalchemy.JSON`，不写 `relationship()`**；`order_by` 之类的字符串必须过
  `model_class.column_names()` 白名单。表名在每个模型里显式写 `__tablename__`。
- **新增模型必须登记进 `app/models/__init__.py`**，否则 autogenerate 生成一份"没有表"的
  空迁移，而它照样 upgrade 成功。
- **引擎只在 `app/dependencies.py` 建**（`get_engine()` 按 `settings.database_url` 缓存，URL 变了
  才重建）。脚本要会话就取 `get_sessionmaker()`，别自己 `build_engine`——两台引擎就是两套连接池
  抢同一个 SQLite 文件。HTTP 侧用 `SessionDep = Annotated[AsyncSession, Depends(get_db)]`
  （写成参数默认值会撞 ruff B008），提交/回滚由 `get_db` 负责。

- 密钥只进 `.env`（已 gitignore，永不提交，也不回显给维护者）；行为配置只进 `configs/*.yaml`。
- 每个架构决策写一条 ADR 到 `docs/adr/`，编号续 `0016` 之后（`0007` 持久化层、`0008` 预留"扫描件
  走多模态 LLM"、`0009` M4 确定性分工、`0010` M5 扇出形状（质量门一节已被 `0015` 取代）、
  `0011` M6 后台化与任务进度、
  `0012` 控制台静态页面同源挂载（已由 `0016` 取代）、`0013` 岗位/简历写入侧与报告快照列、`0014` 岗位图片录入的
  草稿·确认两步与字段出处核对、`0015` 评审退回为纯批注与"一次成卷"、`0016` Vue 3 控制台
  构建产物与 SPA 回退；
  计划书 §8 的短编号 001~005 落文件时改成 000x，其中 ADR-004 = `0011`）。
- 传输重试（tenacity，`llm_client` 内部）与校验重试（`chat_json` 的 `max_attempts`）是两层，
  参数来源不同，别混用。上游明确回了 4xx/5xx（`openai.APIStatusError`，实测 402 余额不足）
  **不进传输重试**，直接 `LLMError` 并把 `status_code` 放进 `details`。

后台化（M6，ADR-0011）追加两条：

- **`BackgroundTasks` 跑在依赖收尾之前**（实测 FastAPI 0.143 + Starlette 1.8，
  `scripts/probe_background_order.py` 顺序是 `dep-enter → endpoint → background → dep-exit`）。
  所以会唤起后台任务的写端点注入 `SessionMakerDep`，由服务层用 `session_scope` **自己提交**；
  纯读的端点继续用 `SessionDep`。别相信"响应发完才跑后台"这个直觉，它会让后台第一件事就读到
  "行不存在"。
- **任务边界吞掉一切异常**并落进 `task` 行（`mark_failed`），后台协程抛出去没人接。
  成本护栏在 `generate` **扇出之前**读 `llm.usage` 累计用量越线抛 `BudgetExceededError`（429），
  口径与 `scripts/eval_e2e.py` 报数同源；实测会有约"一轮扇出"量的 overshoot，这是位置的代价。

控制台（ADR-0016，取代 ADR-0012 的零构建形态）追加四条：

- **`frontend/` 是 Vue 3 + TS + Vite + Element Plus 工程**，源码在 `src/`，后端只读
  `frontend/dist/`。运行期不引 CDN；`node_modules/` 与 `dist/` 不进库。要动 UI 就动源码并重新构建。
- **挂载只排在 `include_router` 之后**，且必须 `if FRONTEND_DIST.is_dir()` 保护：`/` 上的
  `StaticFiles` 排在前会吃掉 `/api/v1/...`，缺目录时 `check_dir=True` 会让 `create_app()` 直接抛错。
- **SPA 回退只给导航请求**（`_SpaStaticFiles`）：仅当静态 404、路径不以 `api/` 开头、最后一段
  不含 `.` 时返回 `index.html`。`/api/v1/typo` 与缺失的 `/assets/*.js` 必须继续 404。
  Windows 上 `get_path` 会给反斜杠，判形前统一成正斜杠。
- **页面只做展示与轮询**，一切判断留在服务端；文本走 Vue 插值（默认转义），不引入 `v-html`。
  用户作答草稿只进 `localStorage`（键 `if-console-answer:<report_id>:<index>`），不提交服务端；
  标准答案默认折叠，打印时强制展开，作答框不打印。
  保留既有客户端语义：任务轮询可取消、空报告按 `status` 分支、删简历 409 后二次确认才 `force`、
  写请求网络失败提示"先刷新确认，别直接重试"。

写入侧（ADR-0013，岗位/简历 CRUD + 报告快照）追加四条：

- **给已有表加 NOT NULL 列一律裸 `ALTER TABLE … ADD COLUMN … NOT NULL DEFAULT ''`**，
  并在模型里写 `server_default=text("''")`（空串写成 `text("")` 渲染出来什么都没有，
  `alembic check` 会报漂移）。**不要用 `op.batch_alter_table`**：它的实现是建临时表 +
  `DROP TABLE`，而 `migrations/env.py` 的 `build_sync_engine` 发过 `PRAGMA foreign_keys=ON`，
  于是 `question.interview_id` 的 `ON DELETE CASCADE` 会被触发——实测这样删空过开发库里
  30 行题目（只能从 `data/checkpoints.db` 的终态逐份还原，仓库不是 git，没有第二条路）。
  回退用 `ALTER TABLE … DROP COLUMN`（SQLite 3.50 起支持）。
- **报告头存"生成那一刻"的简历与岗位名**（`interview.resume_name` / `resume_file_name` /
  `job_title` / `job_company`，`start_generation` 写死）。列表页不许回 join 源表认出身：
  岗位可被下线（`job_id` 是 `SET NULL`）、简历可被改名，而"这份卷子是给谁、按哪个岗位出的"
  属于报告的事实。副作用是快照是第二份真相，**不回写**，页面要说明它是历史值。
- **撞自然键返 409 `ConflictError`，靠先查后插**（`JobRepository.get_by_title_and_company`），
  别等 `uq_job_title_company` 抛 `IntegrityError` 变成 500——错误体里的 `details.job_id`
  是客户端能转 PUT 的唯一依据。
- **删除的传播方向不一样**：`DELETE /jobs/{id}` 直接放行（报告留着），
  `DELETE /resumes/{id}` 名下有报告时先返 409、要 `?force=true` 才连报告一起删。
  级联本身是 ADR-0007 的取舍，改的是默认值：不可撤销的动作必须让人显式授权第二次；
  落盘文件在行删除**之后**删且忽略失败（先删文件再回滚就造出"库里有、盘上没有"的坏行）。
  `PUT /resumes/{id}` 只开 `name`——`raw_text` / `parsed` 是证据链的真相源，不开写入口。

图片录入（ADR-0014，`POST /jobs/inspect-image`）追加三条：

- **草稿不落库**：端点不注入 `SessionDep`，识别完 `job` 表必须还是空的；保存走 `POST /jobs`，
  于是 409/422 与技能派生全是 ADR-0013 的现成品。理由：`title` 是自然键的一半，
  机器猜歪一次就占住 `uq_job_title_company`，下一次真录入会被 409 指向一条错行。
- **`min_years` 永不交给模型**：标签正则、海报标题、公司后缀词典都可以再让 `job_fields@v1`
  兜 `title`/`company`，唯独年限只走规则层（§7 的实测：模型连日期先后都会算错）。
  兜底给的每个值要过两遍核对：`evidence_belongs(jd_text, evidence)` **且** 值就含在证据里，
  任一处不过就丢进 `notes` 留空，由 `missing_required` 让人补——宁可缺失，不许编造。
- **HTTP 侧的 LLM 客户端只从 `app/dependencies.get_llm()` 拿**（`LLMDep`）。它与引擎相反，
  **每请求新建、不缓存**：`Semaphore` 与 `UsageTotals` 是"一次运行"的量纲，
  单例会让用量跨请求累加。测试一律 `app.dependency_overrides[get_llm]`，别 monkeypatch 服务层。

评审纯批注（ADR-0015，"一次成卷"）追加三条：

- **`review` 不拦门**：它只写 `review_issues`（纯观测通道）并落库，状态恒 `completed`。
  不要新增"占比 / 阈值 / 重做次数"这类判据，也不要在 `review` 后面接路由函数——
  想让自动修复回来，先把上一轮批注装进 `GenerateJob` 载荷，否则重做只是盲重掷。
- **`aborted` 只剩一条来路**：后台任务没跑到收尾（`tasks/question_generation.py` 的
  `mark_failed`）。`persist_report` 已经不接 `status` 参数。前端解释空报告按 `status` 分支，
  不再按"批注有无"猜成因。
- **`question_batch` / `question.batch` / `latest_batch` 是休眠机制，不许顺手删**。一次成卷
  之后它们恒为 1，但拆掉要动表（正撞 ADR-0013 的 `batch_alter_table` 禁令），而且定向重做
  回来时直接用得上。`Overwrite` 那条"累积通道整批换掉"的测试要留着。

## 4. 节点契约（写节点前必读，违反即返工）

1. **只返回增量**：`return {"parsed": ...}`；绝不返回整个 state，绝不原地改入参 dict。
2. **只追加通道**（`trace` / `errors`，带 `operator.add` reducer）返回 `[新元素]`；
   返回完整列表会冲掉历史。其余通道是最后写入者胜。
3. **prompt 必须走 registry 且带版本号**：返回 dict 里放保留键 `"prompt_version"`，
   `workflow.traced()` 会 `pop` 掉它写进 `TraceStep`，所以它**不是** state 通道。
   节点不要自己往 `trace` 里塞步骤。
4. **可恢复失败**分两种门（ADR-0015 之后只剩一种）：`validate` 仍写 `validate_issues` +
   **由本节点自增计数**，让 `validate → parse` 的回退门生效；`review` 的 `review_issues`
   已降为**纯观测**，只标出"哪几题有问题"，不驱动任何路由。**不可恢复失败**抛 `AppError`
   子类。不要 `except` 后静默返回空结果——吞掉的同时必须留一条批注或流水账，
   否则"检查没做完"这件事没有任何地方记录它。
5. **回退路由用 `<=`**：因为计数先自增。`validate_max_retries=2` = parse 最多额外重做 2 次
   （共 3 次）。写成 `<` 会少跑一轮。`review` 已经没有路由函数了，它是终点。
6. **节点必须幂等**：回退会让 `parse`/`validate` 重跑，同一线程重放会让 `generate` 重跑，
   重复执行不得产生重复数据（落库只在 `review` 的收尾，且只写一次）。
7. **每个 LLM 调用声明 token 预算**，取 `settings.llm.default_max_tokens`，不许裸调。
8. **扇出分支看到的不是 state，而是 `Send` 载荷**（实测 langgraph 1.2.15，
   `scripts/probe_send_fanout.py`）：出题要读的一切（`plan_item` / `raw_text` / `jd_text` /
   `parsed` / `roster` / `order_start` / `question_batch`）必须在 `GenerateJob` 里点齐——
   漏一个通道，那一类题就永远拿不到依据（实测：漏 `jd_text` → 带可回溯引用比例 25%）。
   分支的**返回值**仍按主图 reducer 合并，所以累积通道只能由**扇出节点**用
   `{"questions": Overwrite([])}` 整批换掉（每个 super-step 只允许一个 `Overwrite`）。
9. **批注的采纳判据只写一处**：`review_service.adopt_model_issues`（越界序号丢弃、空正文丢弃、
   同题同话去重）。ADR-0015 删掉了 `should_redo` / `fail_ratio`——不要为了"以后可能还要拦门"
   把它们留着；门要回来，得先把批注装进 `GenerateJob` 载荷（见 ADR-0015 后续影响）。

## 5. 抽取层铁律：无证据即丢弃

`app/extractors/` 四个抽取器共用 `app/extractors/base.py` 的
`Evidence` / `collapse` / `squeeze` / `evidence_belongs`：**任何值必须能在原文里逐字找到
出处**（比对前抹掉空白与零宽字符）。LLM 给的 `evidence` 核对不上就当没给，并记进 `notes`。
宁可缺失，不许编造。

- `notes` 是给人看的流水账 → 节点把它并进 `errors` 通道；需要机器判定的事实走**结构化字段**
  （例：`TimelineResult.dropped` 带 `reason`，M2 的"日期不倒挂"规则从这里取，因为倒挂的区间
  进不了 `parsed`——`WorkExperience._check_date_order` 会 raise）。
- `errors` 与 `validate_issues` 不是一回事：前者只追加、纯观测、无机器后果；
  后者优先级更高，**驱动回退路由**。
- 确定性优先：能用正则/词典解决的绝不调 LLM。现在整份简历 parse 只花 1 次 LLM 调用。
- **引用按来源核对，且出题与评审共用同一个判据**。`source=resume` 对 `raw_text`、
  `source=jd` 对 `jd_text`、`generic` 可选（`question_service.verify_evidence`；
  评审规则层走 `evidence_problem`）。两侧各写一遍 `evidence_belongs` 会造出两套标准——
  generate 按 JD 放行、review 按简历判废，批注就会永远指着其实没毛病的那几题。

## 6. 测试策略

- 任何**断言 LLM 具体文案**的测试都不许存在。只断言结构、数量、预算、通道变化。
- 单节点：注入 `LLMClient` 替身（`_FakeCompletions` 形状，见 `tests/test_agents/`）。
- 真实响应：录制回放，存 `tests/fixtures/llm/*.json`，测试只回放它的 `response` 字段。
- 图骨架：假节点（`tests/test_agent_infra.py` 的 `_noop` / `_nodes(**overrides)` 范式）。
- 抽取质量用 `scripts/eval_extraction.py` 出 P/R/F1，**只看不断言**（模型抖年会红）。
- `get_settings()` 是 `lru_cache` 单例，测试里用 `monkeypatch.setattr` 换，别改 `.env`。
- 数据库用例：`tests/conftest.py` 的 `db_url` / `engine` / `session` / `db_sessionmaker`
  四个 fixture，**每个用例一个 `tmp_path` 下的临时文件库**。别换成 `:memory:`（每连接一份库），
  也别让用例去碰 `data/interview_forge.db`。建表走 `Base.metadata.create_all`，
  迁移本身只在 `tests/test_migrations.py` 里用**真子进程**跑 `upgrade` / `downgrade` / `check`。
- 时间戳断言只比"函数身份/不倒挂"，不 sleep 等时间往前走（微秒级常常相等）。

## 7. Prompt 模板

`app/prompts/templates/<version>/<node>.{system,user}.j2`；取模板用 `get_prompt(node)`。
环境已定死 `autoescape=False`（产物喂模型不喂浏览器）与 `StrictUndefined`（漏传变量必须报错）。
新增一版模板 = 复制一个版本目录，节点代码不动，只换 `version`，这样"哪版模板变差了"能用
trace 里的版本号归因。

**不要让模型判日期。** 实测（2026-10-16）：`deepseek-chat` 把 `2026-06-30` 说成"晚于今天
2026-10-16"，就算把 `today` 写进 prompt 也照样算错。日期/年限/区间的判断一律留在确定性规则层
（`services/resume_service.py`），prompt 里显式禁止这类判断。

## 8. 已被替换掉的基建（文档里还写着，别被骗）

实际用 **SQLite + sqlite-vec + FastAPI `BackgroundTasks` + SqliteSaver checkpoint**
（checkpoint 独立成 `data/checkpoints.db`）。`README.md` 与 `project_structure.md` 里的
PostgreSQL / pgvector / Celery / Redis / Langfuse 是**过时描述**，`docs/TODO.md` §3 已挂账。
M3 之后代码侧已经彻底 SQLite 化（`app/models/` 用 `sqlalchemy.JSON`，`alembic` +
`data/interview_forge.db`），过时描述只剩文档那两份。
`LEARNING_ROADMAP.md` 全仓不存在，docstring 里的 §编号一律指 `docs/LANGGRAPH_BUILD_PLAN.md`。

## 9. 与维护者协作的方式

- 需要他提供的信息（API key、模型选择、样本文件）**直接开口要**，不要猜、不要把密钥复述回去。
- 长产物与推迟的工作写进仓库内 Markdown，用 `- [ ]` 复选框，并写明**被什么卡住**、
  **恢复后要验什么**。聊天里只给概览。
- 回复结尾给 1→N 的编号后续项。他引用编号范围即授权整组改动。
- 中文注释：解释**为什么**这么做，不复述代码在做什么。

## 10. 工具与外部依赖

- 本项目**不依赖任何 MCP server**：`ruff` / `mypy` / `pytest` 全走本地 venv，测试全离线。
  需要浏览器验收 API 时才用 `browser-use`。
- AI 侧的 skills/rules 通过 TeamAI 团队仓库同步（`/teamai`）。全局技能
  `interviewforge-build` 是本规则集的技能化副本，仓库 `AGENTS.md` 变更后同步它。
- **不要手改** `C:\Users\admin\.qoder-cn\mcp-router.json`：那是运行期注册表，含 API key。
