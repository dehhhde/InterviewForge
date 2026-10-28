# InterviewForge 全面优化文档（2026-10-27 审计）

本文是三份账的**总复核 + 新发现**：

| 文档 | 定位 | 本文与它的关系 |
| --- | --- | --- |
| `docs/TODO.md` | 里程碑挂账（多被外部条件卡住：缺样本/余额/定夺） | 不重复，仅在交叉处引用 |
| `docs/OPTIMIZATION_BACKLOG.md` | 2026-10-23 全仓审计，P0→P3 | **逐条重验**：仍成立 / 已修复 / 已过时 / 恶化 |
| `docs/code-wiki/` | 2026-10-27 生成的结构化代码文档 | 抽查其断言，见 §7 |

审计方法：三路并行读码核对（后端 API/服务/持久化、工作流/抽取器/成本、前端/配置/测试/工具链），
关键断言用 grep / 目录实测钉死，标 `（实测 2026-10-27）` 的都有当场证据。

**为什么 10-23 的清单才四天就要重审**：中间发生了两件事——ADR-0015（10-24）删掉 review
质量门、ADR-0016 把前端从零构建静态页重写为 Vue 3 + Vite + Element Plus。backlog 里所有引用
`frontend/app.js` / `index.html` 的条目针对的已是 `legacy-console/` 归档物，必须映射到新代码重判。

---

## 0. 总体结论（TL;DR）

1. **P0 六条里五条原样还在**（上传无上限、limit 无约束、task.error 泄路径、prompt 注入面、
   生成无去重）。唯一动过的是 PII 那条：`.gitignore` 已补 `reports/` 与 `data/*.bak*`（已修复），
   但**磁盘上的 PII 生成物与重复副本没清**，且 uploads 副本从 5 份涨到 **12 份**（实测 2026-10-27）。
2. **P1 正确性缺陷 6 条全部仍成立**，没有一条被顺手修掉。性价比最高的仍是
   技能等级判据（一处比较逻辑，简历侧 + JD 侧双收益）。
3. **P2 成本项 9 条全部仍成立**，且 `checkpoints.db` 从 17.9MB 涨到 **21.7MB**、
   uploads 涨到 12 份——"每跑一次 live 脚本就加一份脏数据、没人清"这条账在**复利式变贵**。
4. **P3 里前端两条已随 Vue 重写实质解决**：轮询有了取消票号与连续失败停止
   （`frontend/src/utils/task.ts:26-57`），导出入口在三个视图都齐了
   （`GenerateView.vue:347`、`ReportDetailView.vue:71`、`ReportsView.vue:114`）。
   旧 backlog 那两条可以销账，残余见 §6。
5. 新发现集中在**运维卫生**（数据目录无 prune、12 份重复 PDF）与**前端小缺口**
   （轮询无退避、错误区无 `role="alert"`），没有新的 P0。

基线口径：AGENTS.md 写的是 485 passed / ruff clean / mypy 102 files。本轮为只读审计未跑测试，
动工前每条改完仍须三条全绿。

---

## 1. P0 — 安全与数据外泄

- [ ] **P0-1 真人 PII 生成物与重复副本仍在磁盘**（backlog 原判：部分修复，残余恶化）
  `.gitignore:44` 已含 `reports/`、`:37` 已含 `data/*.bak*` —— ignore 这半条**已修复**。
  但实测（2026-10-27）：`reports/面试题-梁清生-前端开发工程师.md`（43KB，含真名+手机号）
  还在盘上；`data/uploads/` 有 **12 份** 127630 字节同尺寸 PDF 副本（backlog 时是 5 份）。
  卡在：删重复副本前必须核对没有 `resume.stored_path` 指着它（同 TODO §2.5 prune 脚本）。
  → 修法：写 `app/scripts/prune_uploads.py`（判据：文件名对不上任何 `resume.stored_path`，
  先 dry-run 报数）；`reports/` 下的 PII 生成物手动清或重跑覆盖为脱敏样例。
  → 恢复后要验：uploads 只剩有行对应的文件；`grep -l 手机号段 reports/` 零命中。

- [ ] **P0-2 三个上传端点无上限整份读内存，只查扩展名**（仍成立）
  `app/api/v1/jobs.py:45-68`（POST /jobs/upload）、`jobs.py:71-85`（inspect-image）、
  `app/api/v1/resumes.py:27-38` 全是 `await file.read()`；
  `app/schemas/job.py:53-61` 的 `jd_text` 只有 `min_length=5` 无上限；
  承诺做校验的 `app/utils/file_handler.py` 仍是空壳且**全仓 0 import**（实测 grep）。
  卡在：上限数值要先定（简历 PDF 常态 <5MB、海报 <10MB）。
  → 修法：分块读 + 超限 413；魔数认 `%PDF`/zip/图片签名；`jd_text` 补 `max_length`。
  → 恢复后要验：灌 2GB 假 PDF 进程 RSS 不涨；改扩展名的假 PDF 被魔数拦下。

- [ ] **P0-3 `limit` 裸 int，负数绕过分页**（仍成立）
  `app/api/v1/jobs.py:32-35`、`resumes.py:43-48`、`interviews.py:58-64` 三处 `limit: int = 50`
  直穿 `.limit(limit)`；SQLite `LIMIT -1` 返回全表（backlog 已实测）。
  卡在：无。
  → 修法：三处一起换 `Annotated[int, Query(ge=1, le=200)]`。
  → 恢复后要验：`?limit=-1` 与 `?limit=9999` 都 422。

- [ ] **P0-4 `task.error` 回显服务器绝对路径**（仍成立）
  `app/agents/nodes/parse_node.py:46-65` 的 `NotFoundError.details` 带 `upload_dir`/`tried`
  → `app/services/task_service.py:72-86` `mark_failed` 原样写进 `task.error`
  → `GET /tasks/{id}` 无鉴权全量返回。
  卡在：无。
  → 修法：`mark_failed` 侧对 `details` 做键白名单（一处挡所有节点）。
  → 恢复后要验：删落盘文件跑解析，`task.error` 只剩 `resume_id`。

- [ ] **P0-5 prompt 注入面：多数模板裸贴用户可控文本**（仍成立）
  逐模板核对（实测 2026-10-27）：`ner.user.j2` 有 `<resume>` 围栏；
  `review.user.j2:5-13` 裸贴 `jd_text` 与 `questions_json`，`parse.user.j2` 裸贴 `raw_text`，
  generate/validate/match/profile/ocr 同病。简历与 JD 都是用户可控文本。
  卡在：改模板走版本目录规则（AGENTS.md §7）：原地改 v1 或开 v2，开 v2 则 trace 归因要跟。
  → 恢复后要验：带 canary 指令的 JD 跑一次，`review_issues` 不被带跑、产出仍过 schema。

- [ ] **P0-6 `POST /generate` 无并发去重**（仍成立）
  `app/services/interview_service.py:27-62` 只查两个 404，不查同 `(resume_id, job_id)`
  是否已有 `running` 报告；双开 = 两份报告头 + 两次完整图（预算护栏按单次运行计，拦不住）。
  卡在：判重口径（按 pair 还是按 resume_id）是产品决策，**要维护者定夺**。
  → 恢复后要验：并发第二次 POST 返 409（复用 `ConflictError`，`details` 给已存在 id）。

---

## 2. P1 — 已复现的正确性缺陷（6 条全部仍成立）

- [ ] **P1-1 技能等级判据错：`熟练运用 MySQL` 判成 `basic`**
  `app/extractors/skill_extractor.py:259-267` `_level_before` 只比起始位置，后缀 `运用`(pos 2)
  胜过 `熟练运用`(pos 0)；同一判据被 JD 侧 `app/services/match_service.py:102-128`
  （`skill_mentions_in`）复用 → `required_level`、缺口判定、rubric 的 `skill_depth` 连锁错。
  本清单**性价比最高**的一条。
  → 修法：按 `(pos, -len)` 排序取更长的等级词；补单测钉 `熟练运用→familiar`、`精通→expert`。
  → 恢复后要验：改完重跑 `tests/test_services`，两侧判据不许分叉（AGENTS.md §5）。

- [ ] **P1-2 重复技能合并判据失效**
  `app/schemas/resume.py:143-146` `_richer` 用 `years`（抽取恒 None→-1）比大小 →
  永远保留第一条，更强的 `level` 被静默丢；`skill_extractor.py` 自己合并用的却是
  LEVEL_RANK——同一概念两套判据。
  → 修法：先比 LEVEL_RANK，`years` 只做同档次级判据。单测：`[familiar, proficient]`→`proficient`。

- [ ] **P1-3 `TraceStep.ok` 恒 True，中止运行无法归因**
  `app/agents/workflow.py:201-214` `_trace_step` 有 `ok/detail` 参数但调用点从不传。
  ADR-0015 之后 review 不拦门，`BudgetExceededError` 扇出前抛出时 trace 静默停在上一节点，
  失败归因全靠猜。
  → 修法：`app/tasks/question_generation.py` 异常处理补一条 `ok=False` + 异常码的 step；
  或者删掉这两个死参数——别留永远为真的字段。

- [ ] **P1-4 后台生成任务收尾写库在 try 外、补刀失败静默吞**
  `app/tasks/question_generation.py:101-118`（收尾在 try 外）、`:163-174`
  （`_abort_interview` 是 `except Exception: pass`，连日志都没有）。
  后果：补刀失败报告永远卡 `running`；写库失败异常冒出协程，task 同样卡 `running`。
  → 修法：吞异常处改 `logger.exception`；收尾纳入 try。注入一次写库失败，
  task 必须落 `failed` 且日志有堆栈。

- [ ] **P1-5 解析期间简历被删 → task 报假成功**
  `app/tasks/resume_parsing.py:51-83`：`update` 返回 None 被忽略，照样 `completed`。
  → 修法：None 时 `mark_failed(code="resume_gone")`；补用例"解析中删行→task=failed"。

- [ ] **P1-6 `LLMClient` 无关闭入口，连接池等 GC**
  `app/utils/llm_client.py` 全类无 `aclose()`（实测 grep 零命中）；
  `get_llm()` 每请求新建不改（AGENTS.md §3 的量纲决策），但生命周期收尾没人关。
  → 修法：加 `aclose()`；`get_llm` 改 async generator 依赖收尾关闭；后台任务 `finally` 关。
  → 恢复后要验：连打 20 次 inspect-image，进程句柄/连接数稳定。

---

## 3. P2 — 成本与延迟（9 条全部仍成立，两条有量化恶化）

- [ ] **P2-1 扇出把同一份上下文复制 N 遍**：`app/agents/nodes/generate_node.py:160-171`
  每个槽位载荷仍带完整 `parsed_json`+`jd_text`+`roster_json`。15 题 = 15 份同样上下文。
  卡在：ADR-0010 的 `Send` 形状不能破；正经解法是槽位粒度合并，牵动 TODO §2.3 的 `angle`。
  低风险先手：`source=resume` 不带 `jd_text`、`source=jd` 不带完整 `parsed`——
  但**载荷字段不能漏**（漏 `jd_text` 曾让引用比例掉到 25%，AGENTS.md §4）。
  动之前先跑 `scripts/eval_e2e.py` 存 token 基线（TODO §2.4：没数字别动扇出）。

- [ ] **P2-2 两层重试相乘、无总时限、回灌完整历史**：`app/utils/llm_client.py:148-189`
  `chat_json` 3 次逻辑重试 × tenacity 3 次传输 = 最坏 9 次 HTTP × 60s ≈ 10 分钟；
  校验重试把上一轮完整原始输出塞回 payload。
  → 修法：逻辑调用套 `asyncio.timeout`；回灌内容截到 N 字。两层本身不合并（AGENTS.md §3）。

- [ ] **P2-3 信号量包住 tenacity 循环**：`llm_client.py:115-146`，退避睡眠占着
  `max_concurrency=4` 的槽。→ acquire 挪进重试循环；探针验证退避期间其它分支在推进。

- [ ] **P2-4 `ImageParser` 在 async 里同步栅格化**：`app/parsers/image_parser.py:50-52`
  直调同步 `_to_data_urls`，大图期间整个 API 停摆。同仓其它解析器都已 `to_thread`，这条是漏网。
  → 一行 `await asyncio.to_thread(...)`。验证：处理大图时并发 `GET /health` 立即返回。

- [ ] **P2-5 文本 PDF 无页数/字数上限**：`app/parsers/pdf_parser.py:39-57`
  （图片路径有 `_MAX_PAGES=20`，文本没有）。300 页 PDF = 每轮几百 KB prompt。
  → 定上限、超限抛带 code 的 `ParseError`。

- [ ] **P2-6 `split_sections` 每轮跑 3 次**：project/skill/timeline 三个抽取器各算一遍
  （`app/extractors/sections.py:102-116`），乘上 validate 回退轮数。
  → parse 节点建一次 SectionMap 传入；验证产出逐字节不变（现有 fixture 对比）。

- [ ] **P2-7 match 模型分支看不到它要引用的文本**：`app/agents/nodes/match_node.py:131-140`
  只给 `skills_json`，但 evidence 要对着完整 `raw_text` 核（`match_service.py`）→
  软要求判定大面积 `unverified`，钱花了结论丢了。
  → 把 `work_experiences[*].summary` / `projects[*].highlights` 加进模板（升模板版本）。

- [ ] **P2-8 超过 12 条软要求永远不判**：`match_node.py:35-37` `_MAX_UNJUDGED_PER_CALL=12`
  没有第二批；每个 `unverified` 缺口还放大成 focus topic + 无证据出题槽位。
  → 分批 + `asyncio.gather`；造 20 条软要求的 JD 验证全拿到 `source=llm` 判定。

- [ ] **P2-9 checkpoint 与 uploads 无清理，体积在涨**（恶化，实测 2026-10-27）
  `checkpoints.db` 17.9MB → **21.7MB**；`data/uploads/` 5 份 → **12 份**同尺寸 PDF。
  `scripts/export_report_md.py:60-92` 每次导出仍遍历全部线程逐个 `aget_state`。
  卡在：`thread_id` 落库是新迁移（AGENTS.md §3：裸 `ALTER TABLE … NOT NULL DEFAULT ''`，
  **禁用 `batch_alter_table`**）。
  → 与 P0-1 的 prune 脚本合并做：一个清 uploads 孤儿文件，一个清终态报告的 checkpoint 线程。

---

## 4. P3 — 可观测性与工程卫生（后端部分全部仍成立）

- [ ] **P3-1 `settings.log_level` 从未生效**：全仓无 `basicConfig`/`dictConfig`（实测 grep）。
  `@log_call` 的 INFO 日志一条看不见；`request_id_var` 恒 `"-"`。
  → lifespan 里按 cfg 做一次 `dictConfig`；顺手决定 `middleware/logging.py` 接线还是删。

- [ ] **P3-2 空壳模块在广告不存在的功能**（实测 2026-10-27 全部仍 0 import）：
  `app/middleware/*`、`app/utils/{guardrails,validators,formatters,text,file_handler}.py`、
  `app/api/v1/candidates.py`、`app/vectorstore/{client,job_indexer,question_dedup}.py`。
  危害是误导：`file_handler.py` 看着像有上传校验（P0-2 的缺口正源于此）。
  卡在：**仓库不是 git 库，删文件不可逆，要维护者点头**。
  `vectorstore/retriever.py` 有真实 import（`match_node.py:33`），不在此列。

- [ ] **P3-3 `configs/testing.yaml` 不生效**：`tests/` 无任何地方设 `APP_ENV`（实测 grep），
  整套测试解析的是 development.yaml。
  → 要么 conftest 设 env + 清 `get_settings` 缓存跑一遍看基线，要么删掉该文件别留假象。

- [ ] **P3-4 死配置键**：`retrieval.top_k_coarse` / `dedup_similarity_threshold`
  仍只有定义与校验器（`app/config.py:79-86`），零消费者（实测 grep）。
  校验器守着一个没人读的键。→ 删键或注释成"阶段 6 预留"并一起挂账。

- [ ] **P3-5 工具链口径漂移**：`pyproject.toml:5,15,26` 仍标 py312（运行时 3.14.7）；
  Makefile 本机不可用（AGENTS.md §2）；`app/scripts/hello_llm.py` 仍是一行 docstring
  的静默 exit 0 空壳（实测读文件）；`README.md`/`project_structure.md` 的
  Postgres/Celery 描述未改（TODO §3 已挂）。
  → hello_llm 改非零退出 + 可见提示（照 `scripts/eval_rag.py` 的好空壳样板）。

- [ ] **P3-6 测试脚手架重复**：`_settings_point_at_this_test` 仍逐字重复 3 处
  （`test_crud_api.py` / `test_m6_api.py` / `test_job_image_api.py`，实测 count）；
  私有 `_settings()` 散在 `tests/test_agents/` 4 处。
  → 提到 `tests/conftest.py`；验证 485 passed 不变、总时长不涨。

- [ ] **P3-7 `tests/conftest.py` docstring 宣传不存在的 fixture**（backlog 结论，未见修复痕迹）。

- [ ] **P3-8 列表端点载荷过重**：`GET /jobs` 每条带 `jd_text`（`app/schemas/job.py:20-28`）、
  `GET /resumes/list` 每行带完整 `parsed`。与 P0-3 叠加可放大。
  → 统一口径（TODO §2.5）：`?fields=` 或详情端点取正文，别拆两份 DTO 真相。

---

## 5. ADR-0015 遗留检查（删质量门之后的悬空物）

- [ ] **`question_batch` / `latest_batch` 是休眠机制，恒为 1——不许顺手删**
  （AGENTS.md §3 明文）。本轮确认无新增引用错误；`Overwrite` 累积通道测试仍在。
- [ ] 前端死文案检查：`frontend/src` 全量 grep `阈值|质量门|fail_ratio|should_redo`，
  唯一命中是 `ReportsView.vue:310` 的**正确解释文案**（"批注不再拦门"）——无悬空，**已核对无问题**。
- [ ] 后端悬空引用（`should_redo`/`fail_ratio` 配置键）：backlog 记载已随 ADR-0015 删除，
  本轮抽查 yaml 未见残留——**已核对无问题**。

---

## 6. 前端专章（Vue 3 重写后的重判）

旧 backlog 针对 `frontend/app.js`/`index.html` 的条目映射到新代码的结果：

| 旧条目 | 新状态 |
| --- | --- |
| 轮询无退避、无取消、失败后继续捶 | **已修复大半**：`frontend/src/utils/task.ts:26-57` 有票号取消 + 连续 3 次失败即停。残余：仍是固定 2s 间隔，无指数退避 |
| 报告列表页打开的报告导不出 | **已修复**：导出入口三处齐备（`GenerateView.vue:347`、`ReportDetailView.vue:71,78`、`ReportsView.vue:114`） |
| tabs 缺 `role="tablist"`/`aria-selected` | **已过时**：新控制台是 `SideNav.vue` 侧栏导航，不是 tabs |
| `#foot-error` 缺 `role="alert"` | **仍成立**：全 `src/` 无 `role="alert"`（实测 grep），fetch 错误对读屏器不可见 |
| 两套分叉的 Markdown 导出 | **仍成立**：`frontend/src/utils/markdown.ts`（前端卷）与 `scripts/export_report_md.py`（脚本卷）并存，字段覆盖面不同 |
| 写请求网络失败提示语义 | **已保留**：`frontend/src/api/http.ts:40-70` 明确提示"先刷新确认，别直接重试" |
| XSS | **已核对无问题**：全 `src/` 零 `v-html`（实测 grep） |

新增观察（非旧账）：

- [ ] **轮询加轻退避**：`task.ts:53` 固定 `sleep(interval)`，服务器过载时 2s 一捶不变。
  连续错误时至少该 2s→5s→10s。改动只在 `task.ts` 一个文件。
- [ ] **作答草稿无清理**：`QuestionCard.vue:21-42` 键名符合约定（`if-console-answer:<id>:<i>`），
  但报告被删后 localStorage 里的草稿成孤儿，永不清理。量小，低优先。
- [ ] **组件规模健康**：最大视图 465 行（`JobsView.vue`），无巨型单文件组件；
  无 `: any`/`as any`（实测 grep）。前端暂不需要结构性重构。

---

## 7. 文档一致性（code-wiki 与 README）

- [ ] `README.md` / `project_structure.md` 的 Postgres/pgvector/Celery/Redis/Langfuse
  描述仍是过时（TODO §3 挂账未动；AGENTS.md §8 已声明"别被骗"）。
  → 与 P3-5 合并处理：要么改文档，要么在 README 顶部加一行指向 AGENTS.md §8。
- [ ] `docs/code-wiki/` 生成于 2026-10-27，抽查其断言与源码一致
  （技术栈表、7 节点 + 扇出形状、目录速览、端点清单均对得上）。
  注意：wiki 是快照，**P0/P1 任何一条修复落地后需同步修订对应篇目**，否则文档先于代码过期。

---

## 8. 建议路线图（按性价比排序）

**第一批：小改动、大代价解除（半天量级，全是独立点）**

1. P0-3 `limit` 加 `Query(ge=1, le=200)`（三处一起）
2. P0-4 `mark_failed` details 键白名单（一处挡所有节点）
3. P2-4 `image_parser` 一行 `to_thread`
4. P1-1 + P1-2 技能等级/合并判据（正确性双修，两侧收益）
5. P3-1 lifespan `dictConfig`（之后所有排障才有日志可看）

**第二批：需要定数值/定夺，但不碰契约**

6. P0-2 上传上限 + 魔数校验（先定 5MB/10MB 数值）
7. P0-5 prompt 围栏（决定 v1 原地改还是开 v2）
8. P1-4/P1-5 后台任务收尾与假成功（同族：任务状态诚实性）
9. P1-6 `LLMClient.aclose`
10. P0-1 + P2-9 prune 脚本（uploads 孤儿 + checkpoint 清理，一次写两个入口）

**第三批：需要维护者点头或外部条件**

11. P0-6 生成去重（判重口径要定夺）
12. P3-2 空壳模块删除（非 git 库，删不可逆）
13. P2-1/P2-2/P2-3 成本三连（先跑 `eval_e2e.py` 存基线——卡在余额与样本，TODO §2.4）
14. P2-7/P2-8 match 判定质量（升模板版本 + 分批，与真人 JD 真调一起做，TODO §2.2）

**用法**：每条做完把 `- [ ]` 改 `- [x]` 并补一行实测结论（照 TODO.md 写法）。
与 `OPTIMIZATION_BACKLOG.md` 的分工：backlog 保留 10-23 原始现场，**新进度记在本文**；
里程碑相关挂账仍进 `TODO.md`，三处互引不重复。

---

## 9. 已核对无问题（别重复审计）

本轮（2026-10-27）复核确认仍然干净的：

- `.gitignore` 已覆盖 `reports/`、`data/*.db`、`data/*.bak*`、`frontend/dist/`、`node_modules/`。
- 前端零 `v-html`、零 `: any`；轮询有取消票号；"先刷新别重试"语义保留。
- ADR-0015 之后前后端无质量门/阈值悬空引用（唯一命中是正确解释文案）。
- `vectorstore/retriever.py` 有真实消费者（`NullRetriever`），与三个空壳不是一回事。
- backlog"已核对无问题"清单里的 db.py PRAGMA、repository 只 flush、persist_report 单事务、
  order_by 白名单、上传落盘名无穿越——本轮抽查未发现回归。
