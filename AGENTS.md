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
M6 之后被拆掉（见 ADR-0015）：`review` 不再拦题目，只出批注，题目一律照常落库。
图里的回退边原本是 `validate → parse` 一张，ADR-0021（S4 / T-404）在它旁边补了
**第二张、且只在授权时成立**的 `review → generate`（`auto_fix=true`、按槽位、最多一轮）。
M6 之后另外补了三块：
`frontend/` 控制台（Vue 3 + Vite + Element Plus 的构建产物挂在 `/`，见 ADR-0016；
它取代了 ADR-0012 的零构建静态页）、写入侧 CRUD
（岗位建/上传/改/删、简历改名/删、报告头的简历·岗位快照列，见 ADR-0013）与
岗位的图片录入（`POST /jobs/inspect-image` 回草稿，见 ADR-0014）。
M6 之后又合入 S0 止血的九张卡（`docs/TASK_PLAN.md` T-002~T-010，2026-10-27）：
上传口的大小上限与魔数校验（ADR-0017）、`task.error.details` 键白名单、7 个 user 模板补齐
注入围栏、技能等级与重复合并判据、失败步骤由异常携带、收尾写库自带日志、`resume_gone`；
S0 第二批 T-011~T-018 与 **S1 前四张卡 T-102/103/104/107**（2026-10-28）随后合入——
后者是抽取质量的三处判据单点（换行连接、编号分组标题、OCR 逐字空格与糊页）
加 `agent.prompt_version` 开关（EXT-9 的起点）。S1 剩下的 T-101/105/106/108 全卡在外部条件
（D-1 真简历、D-2 充值、D-3 embedding 选型），见 `docs/TASK_PLAN.md` §5。
**S2 的无卡点七张**（T-202/203/204/205/206/207/208，2026-10-28）也合入了：出题角度归 `plan`
（ADR-0019）、`match` 的模型分支拿到可引用叙述、软要求分批、扇出载荷按来源带语料、
重试总时限与回灌截断、信号量粒度、文本 PDF 的页数/字数上限 + `SectionMap` 单点切分。
S2 剩下的 T-201（D-4）、T-209（D-5）、T-210（要 T-105）仍在卡着。
**S3 后端五张**（T-302/303/304/311/313，2026-10-28）随后合入：Markdown 渲染收进
`app/services/export_service.py` 并挂成 `GET /interviews/{id}/export.md`、三个列表端点改成
`Page{items,total}` 信封 + 服务端关键词筛选（ADR-0020，与 T-301 的字段投影同一条契约）、
`interview.thread_id` 落库
让"读某份报告的图终态"变成一次定位、`app/scripts/prune_checkpoints.py` 治 checkpoint 库、
`configs/testing.yaml` **第一次真的生效**。S3 剩下的 T-309（D-6）、T-310（D-2）、T-312（D-8）
仍卡着。
**S3 的契约与前端五张**（T-301/305/306/307/308，同日）随后合入：列表出参改固定投影
（`parsed` / `jd_text` 不再整列外泄）、前端吃精简 DTO 并把正文挪进详情抽屉、
任务轮询改指数退避（`ReportsView` 的私有 `setInterval` 并进公共轮询器）、
报告列表补打印入口、a11y 与文案一致性在 Vue 源码上重核。
**S4 的无卡点一张 T-404**（同日）把 ADR-0015 留下的休眠批次机制唤醒：`auto_fix=true` 时
`review → generate` 有一张**授权驱动、按槽位、最多一轮**的回炉边，上一版的题与点名它的
批注装进 `GenerateJob.previous`（只有 v3 模板渲染它），批次 2 仍是一份完整卷子（ADR-0021）。
S4 剩下的 T-401（D-3）、T-402、T-403 与整个 S5（T-501~T-505）仍卡着。
**D-4/D-5/D-6/D-8 四条决策按建议项落地后，四张卡也已合入**（2026-10-28）：`validate` 的
规则问题与反思问题分家（ADR-0022）、生成入口对同一对简历·岗位返 409（ADR-0023）、
`GET /tasks/{id}` 派生报告可用性（ADR-0024）、删掉 `candidates.py` 与整个 `app/middleware/`（ADR-0025）。
现在 §3 剩下的**每一张都是真阻塞**（D-1 样本 / D-2 余额 / D-3 选型 / D-7 多租户，
或依赖链 T-105 / T-403），没有"只等点头"的卡了。
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
- **测试跑在 `APP_ENV=testing`**（T-313）：`tests/conftest.py` 在**模块级**设环境变量并清
  `get_settings` 的 `lru_cache`，所以整套用例解析的是 `configs/testing.yaml`，不是 development.yaml。
  两条后果：① 用例**不许靠环境的宽**——要 4 路并发就在用例里 `model_copy(deep=True)` 自己放大
  （`tests/test_parsers.py` 的三页 OCR 是样板）；② 断"模板形状"的用例必须自己钉
  `prompt_version`（`test_match_node.py::_pin_template_version`），因为 testing 停在 v1
  而开发灰度已经走到 v2/v3（dev 现在是 **v3**，只为 `<previous>` 那一段，见 ADR-0021），
  跟着配置走会造出"新版没被断过却全绿"。

```bash
.venv/Scripts/python.exe -m ruff check app tests migrations   # line-length=100，按东亚宽度计
.venv/Scripts/python.exe -m mypy app                          # mypy 2.4 + pydantic 插件
.venv/Scripts/python.exe -m pytest -q                         # 离线；只有迁移用例真起子进程
                                                              # 解析的是 configs/testing.yaml（T-313）
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m alembic upgrade head   # `make migrate` 的等价命令
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m alembic check          # 模型/迁移漂移检查
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m app.scripts.seed       # 灌 2 岗位 + 3 简历，可反复跑
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m app.scripts.reap_tasks  # 僵尸 running 行；默认 dry-run，--apply 才翻
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m app.scripts.prune_checkpoints
                                                              # checkpoint 库旧线程；默认 dry-run + 留 90 天
                                                              # --apply 才删，无主线程还要 --include-orphans
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 scripts/export_report_md.py <报告id前缀> [输出.md]

cd frontend && npm run typecheck                              # vue-tsc，构建前先过这一关
cd frontend && npm run build                                  # 产出 frontend/dist；后端只读它
cd frontend && npm run dev                                    # 调 UI：127.0.0.1:3000，/api 与 /docs 代理到 8000
```

**提交前必须三条全绿**（当前基线：**718 passed / ruff clean / mypy 102 files clean**，
实测 2026-10-28，`pytest -q` 用时 54.44s（同日只跑 `tests/test_api` + `tests/test_parsers.py`
是 12.94s，103 条——**用时随机器负载漂，别拿它当门禁**）。这条数字被改过几次：ADR-0015 删质量门时去掉两条
门限用例，S0 止血九张卡（T-002~T-010）加 37 条，S0 第二批 T-011~T-018 再加 47 条——
T-018 的脚手架收敛本身**一条都没增减**，只删副本；S1 前四张卡 T-102/103/104/107 再加 19 条，
S2 七张卡（T-202~T-208）再加 27 条（593 → 603 小卡四张 → 607 → 614 → 620，分四次测得），
S3 后端五张再加 50 条（620 → 636 T-302/304 → 656 T-303 → **670** T-311；T-313 一条都没加，
它改的是环境而不是行为），
S3 契约与前端五张再加 11 条，T-404 定向重做再加 30 条（`test_redo_service.py` 一份新文件 18 条
+ 节点/图/任务/API 各若干），
D-4/D-5/D-6/D-8 解卡后的四张卡（T-201/209/309/312）再加 7 条（其中 T-312 一条没加，它只删代码；
`mypy` 的 **102** 也是它删掉 5 个文件的结果，不是覆盖面缩水），
全部是抽取判据、出题形状与重试口径的离线用例，没有新增任何真调）。动了
`frontend/` 另加 `npm run typecheck` 通过；产物 `frontend/dist/` 与 `node_modules/` 不进库。

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
- **列表端点的形状只有一种**（ADR-0020，T-302/303）：`response_model=Page[出参模型]`，
  `Page` 在 `app/schemas/common.py`，字段是 `items` + `total`，**`total` 数的是过滤后的总行数**
  而不是本页。不要回退成"裸数组 + `X-Total-Count` 响应头"——头会被代理与缓存吃掉，
  而前端每一页都要显示总数。筛选走 `repositories.base.keyword_filter(model, q, 列名)`：
  它是 `contains()` 的唯一出口（`%` `_` `\` 全部转义，关键词是用户输入，不转义就等于让人手写
  LIKE 模式串），列名过 `column_names()` 白名单（与 `order_by` 同一个口径，这是第二处
  "字符串来自调用方却参与 SQL 构造"）。**只搜标识列**（名称/标题/公司/文件名），
  `raw_text` / `jd_text` / `parsed` 这类正文列不许进关键词列——列表搜索不是全文检索，
  而把正文带进 `LIKE` 会让"报告搜不到"与"报告不存在"两种答案混在一起。
  `list()` 与 `count()` 必须拿**同一份 `filters`**，否则 `total` 与 items 各说各话。

- 密钥只进 `.env`（已 gitignore，永不提交，也不回显给维护者）；行为配置只进 `configs/*.yaml`。
- **`configs/*.yaml` 的顶层段是白名单**（`app:` / `llm:` / `agent:` / `question:` / `retrieval:` /
  `upload:` / `task:` / `parse:`，见 `app/config.py` 的 `_PARAM_SECTIONS`）。加新段要同时改
  那张白名单和三套 yaml，否则启动直接报"未识别的配置段"。**判据：能被部署差异改变的才进 yaml，
  定义本节点送问形状的阈值留在代码常量**（`_MAX_UNJUDGED_PER_CALL`、`_MAX_JUDGE_BATCHES`、
  `_MAX_EVIDENCE_CHARS`、`_MIN_CHARS_PER_PAGE` 都是这一类）——混进去的下一次校准没人知道该改哪个。
- 每个架构决策写一条 ADR 到 `docs/adr/`，编号续 `0025` 之后（`0007` 持久化层、`0008` 预留"扫描件
  走多模态 LLM"、`0009` M4 确定性分工、`0010` M5 扇出形状（质量门一节已被 `0015` 取代）、
  `0011` M6 后台化与任务进度、
  `0012` 控制台静态页面同源挂载（已由 `0016` 取代）、`0013` 岗位/简历写入侧与报告快照列、`0014` 岗位图片录入的
  草稿·确认两步与字段出处核对、`0015` 评审退回为纯批注与"一次成卷"、`0016` Vue 3 控制台
  构建产物与 SPA 回退、`0017` 上传口的大小上限与魔数校验、`0018` 失败信息的四条通道、
  `0019` 考察角度归 `plan` 管（配额表每题一条 `angles`）、
  `0020` 列表的形状（`Page{items,total}` 信封 + 每端点固定投影，正文只走详情端点）、
  `0021` 定向重做回路（授权驱动、按槽位、最多一轮的 `review → generate` 回炉边，
  在 `0015` 划定的前提内补边）、`0022` `validate` 规则问题与反思问题分家（只有规则问题驱动回退）、
  `0023` 同一对简历·岗位只跑一份（409 先查后插）、`0024` `GET /tasks/{id}` 派生报告可用性
  （`degraded` 两个原因，不加第四态）、`0025` 空壳处置（删 `candidates` 与整个 `app/middleware/`）；
  计划书 §8 的短编号 001~005 落文件时改成 000x，其中 ADR-004 = `0011`）。
  **新增 ADR 必须在 `docs/adr/README.md` 的索引表追加一行**，否则等于没登记。
- 传输重试（tenacity，`llm_client` 内部）与校验重试（`chat_json` 的 `max_attempts`）是两层，
  参数来源不同，别混用。上游明确回了 4xx/5xx（`openai.APIStatusError`，实测 402 余额不足）
  **不进传输重试**，直接 `LLMError` 并把 `status_code` 放进 `details`。
- **两层相乘的总代价由一条墙钟时限封顶**（T-206）：`chat_json` 整段循环套
  `asyncio.timeout(settings.llm.retry_deadline_seconds)`，越线抛 `LLMError`（`code="llm_timeout"`）
  而不是 `LLMResponseError`——前者说"没等到答案"，后者说"等到了但不合格"，别把它改成后者来"统一异常"。
  回灌给模型的上一轮输出走 `_clip(raw, settings.llm.refeed_max_chars)`，**截断处必须留显式记号**，
  否则模型会去修结尾的括号而真正缺的东西在看不见的后半段。
- **`max_concurrency` 那把信号量包的是"一次尝试"，不是"整个重试循环"**（T-207）。
  挪回外层会让指数退避的睡眠占着槽（dev 只有 4 把），上游一限流整批分支就干等。
  语义因此是"约束在途请求数"，不是"约束同时进行几个逻辑调用"——要改并发上限前先确认挡的是哪个。

后台化（M6，ADR-0011 + ADR-0018；后两条来自 ADR-0023/0024 的两次读法收紧）追加八条：

- **`BackgroundTasks` 跑在依赖收尾之前**（实测 FastAPI 0.143 + Starlette 1.8，
  `scripts/probe_background_order.py` 顺序是 `dep-enter → endpoint → background → dep-exit`）。
  所以会唤起后台任务的写端点注入 `SessionMakerDep`，由服务层用 `session_scope` **自己提交**；
  纯读的端点继续用 `SessionDep`。别相信"响应发完才跑后台"这个直觉，它会让后台第一件事就读到
  "行不存在"。
- **任务边界吞掉一切异常**并落进 `task` 行（`mark_failed`），后台协程抛出去没人接。
  成本护栏在 `generate` **扇出之前**读 `llm.usage` 累计用量越线抛 `BudgetExceededError`（429），
  口径与 `scripts/eval_e2e.py` 报数同源；实测会有约"一轮扇出"量的 overshoot，这是位置的代价。
- **`task.error.details` 落库前过键白名单**（`task_service.ERROR_DETAIL_KEYS`，默认拒绝）+
  值判定（含 `/` `\` 的字符串与 dict 一律丢），被挡下的键名记进 `_dropped_keys`。
  `GET /tasks/{id}` 无鉴权且全量透传，节点随手构造的 `details` 里出现过服务器绝对路径。
  新节点要往外给字段就显式加键——别把判据写回节点里。
- **收尾写库自带日志**：`mark_failed_logged` 包住失败终态，成功终态收进各任务的
  `_record_completed`。写不进去只 `logger.exception`，**不改口报失败、也不重抛**——
  业务结论已经落在业务行里了，报失败只会诱导重传。僵尸行（停在 `running`）的回收是 T-015。
- **`update` 的返回值要看**：`get_by_id`/`update` 找不到返回 `None` 是 repository 约定，
  后台收尾拿到 `None` 就是"结果没有落点"，报 `resume_gone`（404 语义）而不是 `completed`。
- **僵尸行只由 CLI 翻**：`python -m app.scripts.reap_tasks`（默认 dry-run，`--apply` 才动手），
  阈值取 `configs/<APP_ENV>.yaml` 的 **`task:` 段**（`stale_after_minutes`，默认 30）。
  没有 `POST /tasks/reap`：那是个"不鉴权就能改库"的写端点，而 D-7 还没定。
  判据只认 `status == running`——一行 `pending` 可能只是"响应已提交、后台还没排上"。
- **`completed` 不等于"报告能用"**（ADR-0024）：`GET /tasks/{id}` 带四个派生字段
  （`report_status` / `question_count` / `degraded` / `degraded_reason`），判据只在
  `task_service.report_availability()` 一处，且只对 `question_generation` 任务算
  （`no_questions` / `report_gone` 两个原因）。**不要给 `interview.status` 加第四态**，
  也不要让客户端自己读 `total_questions` 拼"能不能用"——那是把同一条判据抄成三份。
  控制台的文案映射（"重跑会得到同一批语料"）是文案，不是判据。
- **同一对简历·岗位同时只跑一份**（ADR-0023）：`start_generation` 在服务层先查
  `InterviewRepository.get_running_for(resume_id, job_id)`，命中抛 `ConflictError`（409，
  `details` 带 `interview_id`）。只认 `running`，终态放行。这是**先查后插**不是原子操作
  ——挡误触双击，恶意并发归 D-7；僵尸 `running` 行会挡住重跑，回收靠上面那条 CLI。
- **checkpoint 库只由 CLI 清**：`python -m app.scripts.prune_checkpoints`（T-311，默认 dry-run，
  `--apply` 才删，`--vacuum` 才缩文件——DELETE 只把页还给 freelist）。四档判据里两条不许改：
  认领它的报告还在 `running` 的线程**永不删**（正在跑的下一步要读父 checkpoint），
  **没有报告认领的线程**要 `--include-orphans` 才动——T-304 之前落库的报告 `thread_id` 是空串，
  它们的线程靠 checkpoint 正文里的 `interview_id` 认领，把"行上没写名字"当垃圾会一次性废掉
  所有历史报告重新导出图终态的能力。删除只走 `AsyncSqliteSaver.adelete_thread()`
  （`checkpoints` + `writes` 一起清并提交），手写 `DELETE FROM checkpoints` 会留孤儿行。
  线程年龄不用猜也不用猜错：`checkpoint_id` 是 UUIDv6，时间就在 id 里，版本不是 6 当场抛错拒绝删。

控制台（ADR-0016，取代 ADR-0012 的零构建形态）追加五条：

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
- **无障碍判据（T-308）**：自定义的 `.alert-inline.bad/.warn` 块要带 `role="alert"`（`el-alert`
  自己已经渲染了 `role`，不用补）；只有图标没有文字的按钮要么留可见文案要么给 `aria-label`；
  搜索框/下拉框这类无 label 的输入补 `aria-label`；筛选 chip 用 `aria-pressed` 表达选中态，
  不用 `class` 表达。文案里的数字要和常量同源（见下面"列表读侧"）。

列表读侧（ADR-0020 + T-302/T-305/T-306/T-307/T-308）追加四条：

- **列表端点返回 `Page[精简模型]{items,total}`**（`app/schemas/common.py`），`total` 由服务层把
  **同一份** `filters` 交给 `repository.list()` 与 `.count()` 得出；正文（`parsed` / `jd_text` /
  `questions`）**只在详情端点出现**。别为省事把详情模型当列表 `response_model`，也别加 `?fields=`
  动态投影（那是第三份列白名单）。前端类型用 `Omit<详情, 正文列>` 派生，`npm run typecheck`
  就是投影的护栏——它会让还在读正文的视图编译失败，而不是渲染成空白。
- **搜索列走 `keyword_filter` 的白名单**（标识列 + 报告的 `id`），`q` ≤64 字、`%`/`_`/`\` 转义。
  不许 LIKE 正文列（SQLite 无全文索引，整表扫），也不许回 join 源表搜报告出身——岗位可被下线
  （`job_id` 是 `SET NULL`），那样会漏掉确实存在的报告（ADR-0013）。前端搜索框要跟着设
  `maxlength="64"`，否则超长关键词变成 422 而不是"没搜到"。
- **间隔常量只有一处**：`frontend/src/utils/task.ts` 的 `POLL_INTERVAL_MS` / `REFRESH_INTERVAL_MS` /
  `MAX_INTERVAL_MS`。文案一律插值（`{{ POLL_INTERVAL_MS / 1000 }}`），不写死"每 2 秒"。
  任务轮询走 `createTaskPoller`（票号取消 + 退避到 10s + 连续 3 次熔断 + 可打断 sleep），
  列表静默刷新走 `createRefreshLoop`（`more()` 决定这一轮要不要发请求），**页面不许自己开
  `setInterval`**。熔断后要留一句能说清"已停"的文案和一个重新开始的按钮（`重新轮询进度`），
  且重试轮询**不许**顺手重跑一次付费生成。
- **Markdown 导出只有一份渲染**：服务端 `app/services/export_service.py::render_markdown`，
  CLI 脚本与 `GET /interviews/{id}/export.md` 共用；前端只把浏览器指向那个 URL，文件名来自
  `Content-Disposition`（`frontend/src/utils/download.ts`）。打印入口是 `/reports/{id}?print=1`，
  由详情页消费掉 query 再 `window.print()`——打印样式只有详情页知道，列表页不许复刻一套。

写入侧（ADR-0013，岗位/简历 CRUD + 报告快照）追加四条：

- **给已有表加 NOT NULL 列一律裸 `ALTER TABLE … ADD COLUMN … NOT NULL DEFAULT ''`**，
  并在模型里写 `server_default=text("''")`（空串写成 `text("")` 渲染出来什么都没有，
  `alembic check` 会报漂移）。**不要用 `op.batch_alter_table`**：它的实现是建临时表 +
  `DROP TABLE`，而 `migrations/env.py` 的 `build_sync_engine` 发过 `PRAGMA foreign_keys=ON`，
  于是 `question.interview_id` 的 `ON DELETE CASCADE` 会被触发——实测这样删空过开发库里
  30 行题目（当时只能从 `data/checkpoints.db` 的终态逐份还原：那句"仓库不是 git，没有第二条路"
  是 2026-10-20 的事实，现在仓库已经是 git 库，见 §8）。
  回退用 `ALTER TABLE … DROP COLUMN`（SQLite 3.50 起支持）。
  **这条现在是机器护栏**：`tests/test_migrations.py::scan_migration` 用 AST 扫
  `migrations/versions/*.py`，`batch_alter_table` 块体内只许 `create_index` / `drop_index`
  （autogenerate 给已有表加索引会自己套 batch，那是唯一合法形状），手写 `ALTER TABLE`
  出现 `NOT NULL` 必须同带 `DEFAULT`。新增迁移过不了它，就说明形状错了。
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

图片录入（ADR-0014，`POST /jobs/inspect-image`）追加四条：

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
- **OCR 产出的两处修正只写在 `ImageParser.parse()` 收尾**（T-104）：`despace_latin()` 把
  `P y t h o n` 压回 `Python`（保守判据 `(?:[A-Za-z][ \t]+){2,}[A-Za-z]`——低于三连不算失真，
  否则 `C C++`、`Base 上海` 这类正常写法会被粘坏；只认 ASCII、不跨换行），以及**每页单独**过
  `_MIN_OCR_CHARS`（不够的页不进 `raw_text`，全丢光抛 `parse_vision_empty`/422）。归一必须发生在
  判糊**之前**，否则字数算的是被丢掉的东西。**不要用关键词列表去黑名单模型的元话**，那是第二套判据
  （代价已知：≥20 字的自述仍会进 `jd_text`，校准欠账见 `docs/TODO.md` §2.6）。
  词典侧与 `match` 共用的 `SKILL_ALIASES` 判据一字未改——OCR 文本的问题在 OCR 层收口。

评审纯批注（ADR-0015，"一次成卷"）追加三条：

- **`review` 不拦门**：它只写 `review_issues`（纯观测通道）并落库，状态恒 `completed`。
  不要新增"占比 / 阈值 / 重做次数"这类判据。`review` 后面**只**允许 ADR-0021 那张
  授权驱动、最多一轮的回炉边——它成立的前提是"上一轮批注已经装进 `GenerateJob` 载荷"，
  没有反馈的重掷仍然是盲重掷，那张边不因为存在过就变得合理。
- **`aborted` 只剩一条来路**：后台任务没跑到收尾（`tasks/question_generation.py` 的
  `mark_failed`）。`persist_report` 已经不接 `status` 参数。前端解释空报告按 `status` 分支，
  不再按"批注有无"猜成因。
- **`question_batch` / `question.batch` / `latest_batch` 不许顺手删**。ADR-0015 之后它们
  本来恒为 1（"休眠机制"），ADR-0021 给了它们第二个取值：授权那一轮的第二批是新的完整卷子。
  拆掉仍要动表（正撞 ADR-0013 的 `batch_alter_table` 禁令）。
  `Overwrite` 那条"累积通道整批换掉"的测试要留着。

定向重做（ADR-0021，`auto_fix` 授权的回炉边）追加三条：

- **判据只有一处**：`app/services/redo_service.py`。`plan_slots` 是 `order_start` 的唯一出处，
  `redo_plan` 决定"哪一档被重派、哪些题被带走"。`generate` 节点把算好的槽位写进
  `fanout_slots` 通道，`route_after_generate` **只读**它——路由自己从 `plan` 推会算错第二轮
  （第二轮的答案不是整份配额表）。点名判据：`index=None` 的整批批注**不重派任何东西**，
  越界与负数序号丢弃（负数会静默指到批次尾部）。
- **批次 2 必须是一份完整的卷子**：被点名的档整档重出（不是按题重派），未点名的题**原样带走
  并把 `batch` 改写成 2**。因为 `sort_key = (batch, order_index)` 先比 `batch`、而列表与导出
  默认只读 `latest_batch`——只装重做题的批次 2 会让其余题目凭空消失。`batch=1` 的行不删，
  它是"这份卷子改过哪些题"的唯一证据。
- **反馈的形状是白名单，不是整题**：`GenerateJob.previous` 每条只带
  `stem` / `difficulty` / `source` / `evidence` + 点名它的 `issues`。**不带
  `reference_answer` / `rubric`**（喂旧答案等于请模型抄回去），且只有 **v3** 模板渲染
  `<previous>`（`configs/development.yaml` 现在指 v3，production/testing 仍 v1）。
  没有 `POST /interviews/{id}/redo`：重做要 `raw_text`/`jd_text`，而 ADR-0013 的报告头
  只快照了名字，凭 `interview_id` 反查不到语料（欠账见 `docs/TODO.md`）。

上传口（ADR-0017，`app/utils/file_handler.py`）追加三条：

- **新增上传端点一律走 `read_capped` + `check_kind`**，不要再自己 `await file.read()`。
  上限取 `configs/*.yaml` 的 `upload:` 段（`document_bytes` 文档 / `image_bytes` 图片），
  代码里不写魔数；越线抛 `PayloadTooLargeError`（413），签名与后缀错配抛
  `upload_kind_mismatch`（422）。
- **只认后缀等于什么都不认**：`sniff_kind` 认 `%PDF` / PK 三种本地头 / 六种图片签名，
  认不出按 `text`。未知后缀与空文件**放手不判**，那是 `upload_bad_suffix` /
  `upload_empty_file` 的活儿——同一次上传不该有两个候选错误码。`.docx` 归 `zip`。
- **诚实边界写进 docstring**：这一闸保证"进来的文件后缀与内容一致、且不巨大"，
  不保证"能被解析"；`autoescape=False` 的 prompt 围栏也挡不住正文自带的闭栏
  （欠账见 BACKLOG P0-5 下面那条与 T-106）。

日志（T-017，无新 ADR——它只是把一个既有配置项接上）追加两条：

- **装配点只有一处**：`app/utils/logging.configure_logging()`，lifespan 无参调一次。
  它只点名 `app` 这一个 logger、只挂一个 stdout handler、**不碰 root**，并且显式写
  `disable_existing_loggers=False`（dictConfig 默认是 `True`，会把这次没点名的 uvicorn
  三个 logger 静默关掉——不报错，只是访问日志没了）。新模块直接
  `logging.getLogger("app.xxx")` 就继承这套配置，不要在自己的模块里 `basicConfig` 或挂 handler。
- **级别照配置的优先级走**：`.env` 的 `LOG_LEVEL` 压过 `configs/<APP_ENV>.yaml` 的
  `app.log_level`（本机实测 `.env:LOG_LEVEL=INFO`，所以 dev yaml 里的 `DEBUG` 落不了地）。
  要开调试改 `.env`，别改完 yaml 就断定"日志没生效"。

## 4. 节点契约（写节点前必读，违反即返工）

1. **只返回增量**：`return {"parsed": ...}`；绝不返回整个 state，绝不原地改入参 dict。
2. **只追加通道**（`trace` / `errors`，带 `operator.add` reducer）返回 `[新元素]`；
   返回完整列表会冲掉历史。其余通道是最后写入者胜。
3. **prompt 必须走 registry 且带版本号**：返回 dict 里放保留键 `"prompt_version"`，
   `workflow.traced()` 会 `pop` 掉它写进 `TraceStep`，所以它**不是** state 通道。
   节点不要自己往 `trace` 里塞步骤。节点抛异常时 `traced()` 也**不写 state**：失败的那一步
   （`ok=False` + `detail=<异常码>`）挂在异常属性上，由 `failed_trace_step(exc)` 读取、
   任务边界落成进度步——异常路径上任何通道都落不了库（checkpoint 停在失败前一步）。
4. **可恢复失败**分两种门（ADR-0015 之后只剩一种）：`validate` 写 `validate_issues` +
   **由本节点自增计数**，让 `validate → parse` 的回退门生效——但**只有规则判出来的问题**进去
   （ADR-0022）：模型反思挑出的写 `reflect_issues` 并追加进 `errors`（`validate(reflect): …`），
   **不自增计数**。理由是 `parse` 是确定性的，重跑修不掉反思问题，让它进回退等于白烧预算。
   `review` 的 `review_issues` 已降为**纯观测**，只标出"哪几题有问题"，**不自己决定要不要重做**
   ——它只在调用方授权（`auto_fix`）时被 `route_after_review` 读一次"有没有题被点名"（ADR-0021）。
   **不可恢复失败**抛 `AppError` 子类。不要 `except` 后静默返回空结果——吞掉的同时必须留一条批注或流水账，
   否则"检查没做完"这件事没有任何地方记录它。
5. **回退路由用 `<=`**：因为计数先自增。`validate_max_retries=2` = parse 最多额外重做 2 次
   （共 3 次）。写成 `<` 会少跑一轮。`route_after_validate` **只读 `validate_issues`**
   （不读 `reflect_issues`，ADR-0022）。`review` 后面**只有一张授权驱动的回炉边**
   （`route_after_review`，ADR-0021）：`auto_fix` 为假时它恒回 `END`，为真时也只在
   `question_batch == 1` 且确有题被 `index` 点名时回 `generate`——**最多一轮**，
   不是比例、不是阈值、也不是重做次数。
6. **节点必须幂等**：回退会让 `parse`/`validate` 重跑，同一线程重放会让 `generate` 重跑，
   重复执行不得产生重复数据（落库只在 `review` 的收尾，且只写一次）。
7. **每个 LLM 调用声明 token 预算**，取 `settings.llm.default_max_tokens`，不许裸调。
8. **扇出分支看到的不是 state，而是 `Send` 载荷**（实测 langgraph 1.2.15，
   `scripts/probe_send_fanout.py`）：出题要读的一切（`plan_item` / `raw_text` / `jd_text` /
   `parsed` / `roster` / `order_start` / `question_batch` / `previous`）必须在 `GenerateJob` 里点齐——
   漏一个通道，那一类题就永远拿不到依据（实测：漏 `jd_text` → 带可回溯引用比例 25%）。
   分支的**返回值**仍按主图 reducer 合并，所以累积通道只能由**扇出节点**用
   `{"questions": Overwrite([])}` 整批换掉（每个 super-step 只允许一个 `Overwrite`）；
   定向重做那一轮换进去的不是空表，而是"这一档没被点名的题"改写 `batch` 后的
   `Overwrite(value=carried)`——批次 2 必须仍是一份**完整的卷子**（ADR-0021）。
   **载荷里的语料按 `source` 现取，判据只有一处**：`question_service.corpora_for(source)`
   同时驱动扇出打包（`workflow._slot_corpora`）与引用核对（`verify_evidence` / `evidence_problem`）
   ——瘦身只能减少"装进载荷的值"，永远不能减少"闸门读的语料"，否则就是两套标准。
   键名恒在、值可以空（`source=resume` 的载荷仍带 `jd_text`，只是不装正文；空的那格渲染成
   `_NO_JD_NOTE`），因为"字段不见了"和"这题本来不该引用 JD"必须长得不一样的样子〔T-205〕。
9. **批注的采纳判据只写一处**：`review_service.adopt_model_issues`（越界序号丢弃、空正文丢弃、
   同题同话去重）。ADR-0015 删掉了 `should_redo` / `fail_ratio`——它们**不许回来**；
   "值不值得重做"不是判据，ADR-0021 的回炉边只读"有没有题被点名"，授权来自调用方，
   而"哪一档被点名、上一版写成什么样"的判据只写在 `app/services/redo_service.py`
   （`plan_slots` 也是 `order_start` 的唯一出处，路由与节点都只调它）。
10. **配额表每题一条 `angles`**（ADR-0019，ADR-0009 的确定性延伸）：角度由 `plan` 查表给出
    （`ANGLES_BY_DIFFICULTY[difficulty]` 按题序取模），**不是模型选的**——分支之间看不见彼此，
    让模型挑角度等于把去重交给运气。列表长度恒等于 `count`，所以 `count > len(pool)` 时
    重复是明写的兜底而不是缺陷（有专门用例钉住"取模而不是截断"：截断会破坏 `sum(count) == total`）。
    `roster` 因此**不许退掉**：角度靠"和别人的不一样"起作用，而别人的角度是分支唯一看不见的东西。

## 5. 抽取层铁律：无证据即丢弃

`app/extractors/` 四个抽取器共用 `app/extractors/base.py` 的
`Evidence` / `collapse` / `squeeze` / `evidence_belongs`：**任何值必须能在原文里逐字找到
出处**（比对前抹掉空白与零宽字符）。LLM 给的 `evidence` 核对不上就当没给，并记进 `notes`。
宁可缺失，不许编造。

- `notes` 是给人看的流水账 → 节点把它并进 `errors` 通道；需要机器判定的事实走**结构化字段**
  （例：`TimelineResult.dropped` 带 `reason`，M2 的"日期不倒挂"规则从这里取，因为倒挂的区间
  进不了 `parsed`——`WorkExperience._check_date_order` 会 raise）。
- `errors` 与 `validate_issues` 不是一回事：前者只追加、纯观测、无机器后果；
  后者优先级更高，**驱动回退路由**。ADR-0022 之后 `validate` 的第三类事实也走前者：
  模型反思挑出的问题进 `reflect_issues` 并同时追加 `validate(reflect): …` 到 `errors`，
  **不进回退计数**（parse 重跑修不掉它们）。
- 确定性优先：能用正则/词典解决的绝不调 LLM。现在整份简历 parse 只花 1 次 LLM 调用。
- **引用按来源核对，且出题与评审共用同一个判据**。`source=resume` 对 `raw_text`、
  `source=jd` 对 `jd_text`、`generic` 可选（`question_service.verify_evidence`；
  评审规则层走 `evidence_problem`）。两侧各写一遍 `evidence_belongs` 会造出两套标准——
  generate 按 JD 放行、review 按简历判废，批注就会永远指着其实没毛病的那几题。
- **一次 parse 只切一份 `SectionMap`**：`parse_node` 切好再以可选关键字 `sections=` 传给三个抽取器
  （T-208）。抽取器单用时仍自己切，所以 `tests/test_extractors/` 的调用形状没变。
  `SectionMap.lines_of()` **返回副本**——共享之后一个抽取器的排序会串到下一个，
  而那种污染只在真简历的特定段落上现形。这条是纯省开销：产出必须逐字段相等，有用例钉着。
- **文本层 PDF 超限是拒收，不是截断**（T-208）：`configs/*.yaml` 的 `parse:` 段给
  `max_pdf_pages` / `max_raw_chars`，页数闸在 `load_page` 循环**之前**（读就是被限制的开销），
  字数闸在整份 join **之后**。截断 `raw_text` 等于让 `evidence_belongs` 去核一份
  并不是用户上传内容的事实，而 `raw_text` 是整条证据链的真相源。
- **送给模型引用的简历叙述，必须是过得了证据闸的那两处**：`match` 只送
  `work_experiences[*].summary` 与 `projects[*].highlights`（`_resume_evidence`，T-203），
  不送整份 `raw_text`——栏目标题、日期串都是"原文里有、但不是证据"的东西，模型抄上去
  会让整批判定降级成 `unverified`，钱花了结论还是丢了。超预算时截成**逐字前缀**（仍过闸）
  并留一句说明；静默截断等于骗模型"简历就写了这么多"。
- **软要求分批，批数封顶**（T-204）：一批一次调用、`asyncio.gather` 并发，结果按**提交顺序**合并
  以保证缺口清单次序确定（幂等，§4 第 6 条）；一批失败只折这一批。
  本节点不许再叠一层信号量——限流点只有 `LLMClient` 里那把（T-207 刚把它挪进重试循环）。
- **被 PDF 换行劈开的同一段话，连接判据只有一处**：`app/extractors/base.py::join_wrapped_lines()`
  （T-102）。行末已有句读就直拼，没有只补**一个空格**——空格在 `squeeze()` 里会被抹掉，所以它不声称
  原文此处有过标点；补"；"则是替原文造一个不存在的话题边界（实测：`推；广`、`服务于；本公司`）。
  "；"只允许出现在**桶间**（描述 / `技术栈：`），因为那是真话题边界。验收判据是"抹掉空白后整段命中原文"
  （`test_..._invents_no_punctuation`），不是"不含某个片段"。
- **编号行分「分组标题」与「成果条目」的判据只有一处**：`project_extractor._is_numbered_subheading()`
  （T-103）：编号前缀 + 正文 ≤14 字 + 体内无句读 + 不以动词开头。标题只切桶、**不入 `highlights`、
  不写 `notes`**（这种版式是常态，为它报流水账会把健康路径的 `errors` 变成噪音；标题仍在 `evidence` 里）。

## 6. 测试策略

- 任何**断言 LLM 具体文案**的测试都不许存在。只断言结构、数量、预算、通道变化。
- 单节点：注入 `LLMClient` 替身。**替身全仓只有一份**：`tests/test_agents/_fake_llm.py`
  的 `FakeCompletions`（T-018 收敛，原先 7 个文件各抄一份）。三种注入方式都在这一个类里：
  `raises=`（每次都坏）、回复列表里放异常实例（只有那一次坏，用来测传输重试与修复回路）、
  什么都不注入（越界后一直吐最后一条，所以"重做 N 轮"不必备 N 条回复）。
  同一文件还有 `completion(text)`（补全的最小形状，"按 prompt 决定回复"的替身必须用它，
  usage 形状一分叉成本护栏就只在一处成立）和 `slot_settings(**slots)`（深拷贝 settings +
  可区分的模型槽位假名，`model_for(purpose)` 的路由才有断言余地）。
- **`tests/test_api/conftest.py` 是 HTTP 用例唯一的 app/client 出处**（T-018）：库与上传目录
  由那里的 autouse 指到 `tmp_path`。各文件多出来的需求走钩子，不许再复制一份 fixture：
  还要改配置就自己再加一个 autouse（conftest 的先跑，改的是不同属性）；要换依赖就覆盖
  `app_overrides`，**绝不覆盖 `app`**（`test_upload_limits` 的 `get_llm` 替身就是这么留的）。
  样本数据不是 fixture：`tests/fixtures/resumes/`（真简历，缺文件即 skip）与
  `tests/fixtures/resume_ok.json`。
- 真实响应：录制回放，存 `tests/fixtures/llm/*.json`，测试只回放它的 `response` 字段。
- 图骨架：假节点（`tests/test_agent_infra.py` 的 `_noop` / `_nodes(**overrides)` 范式）。
- **碰 checkpoint 库的用例**（T-311 / T-302 / T-304）：把 `workflow.CHECKPOINT_DB` 指到
  `tmp_path`（`monkeypatch.setattr`，它是模块属性，读的时候才取——三处必须一起挪，
  否则"清单里的线程去库里查不到"），线程用**真跑一次假图**写出来，别手写
  `INSERT INTO checkpoints`：那是照抄 langgraph 的表格式，而它没有兼容性承诺。
  唯一的例外是"格式不对时不许猜时钟"那类用例（造一条非 UUIDv6 的 id 就是为了炸）。
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
新增一版模板 = 复制一个版本目录，节点代码不动，只改 `configs/<APP_ENV>.yaml` 的
`agent.prompt_version`（T-107；**只有 yaml 这条路**——`Settings` 没配 `env_nested_delimiter`，
`.env` 里的 `AGENT__PROMPT_VERSION` 之类不会生效），这样"哪版模板变差了"能用 trace 里的
版本号归因。**调用点一律不写 `version` 参数**：版本是部署参数而不是代码事实，写进代码就没法按环境切换。
`DEFAULT_VERSION` 已删除（两条真相源），所以配置指到一个不存在的目录会**当场** `UnknownPromptError`
而不是静默退回 v1；值本身受路径形状校验（`^[A-Za-z0-9][A-Za-z0-9._-]*$`，`../x`、`v1/v2` 被
`ValidationError` 挡在配置层）。`_load` 的缓存键含模板根目录——测试把 `TEMPLATE_DIR` 指到 `tmp_path`
时不会读到上一用例的缓存，自建临时模板目录测版本切换，别污染 `app/prompts/templates/`。
**给模板加新变量时，`tests/test_prompts_and_decorators.py` 的 `CONTEXT` 表要同步补齐**
（三张"渲染全节点模板"的用例按那张表跑，缺一个键会在 `StrictUndefined` 上红三条）。
新能力一律写进**最新那一版**（现在是 **v3**：v2 比 v1 多"每题角度"与 `match` 的
`<resume_evidence>` 围栏，v3 只在 v2 之上多 `<previous>`，见 ADR-0021），调用点**恒传**新变量、
让旧模板不消费它——
按版本分支决定传不传等于把缺陷藏进配置，v1 环境出问题时报的是"缺变量"而不是"缺模板"。

**不要让模型判日期。** 实测（2026-10-16）：`deepseek-chat` 把 `2026-06-30` 说成"晚于今天
2026-10-16"，就算把 `today` 写进 prompt 也照样算错。日期/年限/区间的判断一律留在确定性规则层
（`services/resume_service.py`），prompt 里显式禁止这类判断。

## 8. 已被替换掉的基建（与两处"曾经的过时描述"）

实际用 **SQLite + sqlite-vec + FastAPI `BackgroundTasks` + SqliteSaver checkpoint**
（checkpoint 独立成 `data/checkpoints.db`）。M3 之后代码侧已经彻底 SQLite 化（`app/models/`
用 `sqlalchemy.JSON`，`alembic` + `data/interview_forge.db`）。
`README.md` 与 `project_structure.md` 里的 PostgreSQL / pgvector / Celery / Redis / Langfuse
**已在 2026-10-27（T-018）改完**：两份文件顶部都自称"初始设计快照"，技术选型表按实测重写。
读到那份改动之前的历史文档（含 `docs/adr/` 早期的取舍句）仍要拿本节当现状。
`docker/` 目录现在只剩一份说明"这里没有编排"的 README。
`LEARNING_ROADMAP.md` 全仓不存在，docstring 里的 §编号一律指 `docs/LANGGRAPH_BUILD_PLAN.md`。

**仓库现在是 git 库了**（实测 2026-10-27：`main` 分支、1 个提交、`git ls-files` 301 个文件，
`.env` / `data/*.db` / `reports/` / `frontend/dist/` 均已 ignore 且未被跟踪）。
本节以上以及若干旧 ADR 里"仓库不是 git，删了没有第二条路"的句子是**当时的事实**：
删已跟踪文件现在可以从提交取回，但 `git commit` / `git push` 一律由维护者做，AI 不擅自提交，
也不在未提交的工作区上跑 `reset --hard` / `checkout --` / `clean`。

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
