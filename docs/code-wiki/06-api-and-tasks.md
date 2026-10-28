# 06 · HTTP API、后台任务与错误处理

## 1. 路由总览

所有路由挂在 `api_router = APIRouter(prefix="/api/v1")`
（[app/api/v1/__init__.py](file:///d:/code/InterviewForge/app/api/v1/__init__.py)），
按 health → resumes → interviews → tasks → jobs 顺序 `include_router`。

> **静态段必须注册在动态段之前**：Starlette 按注册顺序匹配，`/resumes/{resume_id}` 写在
> `/resumes/list` 前面会让 `list` 被当成 id 直接 404。

### 1.1 健康检查

| 方法 | 路径 | 返回 |
| --- | --- | --- |
| `GET` | `/api/v1/health` | `{status:"ok", app, env}` |

### 1.2 简历

[`app/api/v1/resumes.py`](file:///d:/code/InterviewForge/app/api/v1/resumes.py)

| 方法 | 路径 | 状态码 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/resumes/upload` | 202 | `multipart file`；服务层落盘 + 建 `resume`/`task` 并**当场提交**，后台 `run_resume_parsing`；返回 `{resume_id, task_id}`（进度看 task） |
| `GET` | `/resumes/list` | 200 | `limit`（1..200）/ `offset` / `q`（≤64 字）；`Page[ResumeListItem]`——**不含 `parsed`**，给 `overview`（ADR-0020） |
| `GET` | `/resumes/{id}` | 200 / 404 | `ResumeOut`（正文在这里） |
| `PUT` | `/resumes/{id}` | 200 / 404 | 只改 `name`（`ResumeUpdateRequest`） |
| `DELETE` | `/resumes/{id}?force=false` | 204 / 404 / 409 | 名下有报告且未 `force` → 409（`details.interview_count`）；`force=true` 级联删报告与题目 |

上传端点注入 `SessionMakerDep`（不是 `SessionDep`），因为后台任务在依赖收尾前就开跑。

### 1.3 岗位

[`app/api/v1/jobs.py`](file:///d:/code/InterviewForge/app/api/v1/jobs.py)

| 方法 | 路径 | 状态码 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/jobs` | 200 | `Page[JobListItem]`：`limit`/`offset`/`q`（只比 `title`、`company`），正文只给 `jd_excerpt`（ADR-0020） |
| `POST` | `/jobs` | 201 / 409 | JSON 建岗；`required_skills` 留空时词典派生；自然键撞车 409 并回带 `details.job_id` |
| `POST` | `/jobs/upload` | 201 / 409 / 422 | `file` + `title` + `company` + `min_years` 表单；JD 文件读成文本后走同一条建岗路 |
| `POST` | `/jobs/inspect-image` | 200 / 422 | 图片 → 视觉模型 → **草稿** `JobDraftOut`，**不落库、不查库**，只注 `LLMDep` |
| `GET` | `/jobs/{id}` | 200 / 404 | |
| `PUT` | `/jobs/{id}` | 200 / 404 / 409 | 部分更新；改 `jd_text` 未给技能时重派生；撞键 409 |
| `DELETE` | `/jobs/{id}` | 204 / 404 | 报告保留（`job_id` SET NULL + 快照列） |

岗位写端点都是**同步返回**：JSON/文件建岗零 LLM；图片识别虽有 1–2 次模型调用，但产出是
要人过目的草稿，不造任务行。

### 1.4 面试报告

[`app/api/v1/interviews.py`](file:///d:/code/InterviewForge/app/api/v1/interviews.py)

| 方法 | 路径 | 状态码 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/interviews/generate` | 202 / 404 | body `{resume_id, job_id}`；建 `interview(running)` + `task(pending)` 并提交，后台跑整图；返回 `{interview_id, task_id, status:"running"}` |
| `GET` | `/interviews/list` | 200 | `Page[InterviewListItem]`：报告头 + `total`，`q` 比四列快照与 `id`；**没有** `questions` 这个键（ADR-0020） |
| `GET` | `/interviews/{id}` | 200 / 404 | `InterviewOut`：报告头 + 最新批次题目 |
| `GET` | `/interviews/{id}/export.md` | 200 / 404 | `PlainTextResponse`（`text/markdown`）+ `Content-Disposition` 文件名；渲染与 CLI 脚本同一个函数，按 `thread_id` 定位图终态（T-302 / T-304） |

`InterviewOut` 的题目字段取 `question` 表真实列（`order_index/stem/difficulty/source/
evidence/checkpoints/reference_answer/rubric/follow_ups`），`batch` 等工序列不外泄。

### 1.5 任务进度

| 方法 | 路径 | 状态码 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/tasks/{task_id}` | 200 / 404 | `TaskOut`：`kind/status/ref_id/progress/result/error` |

客户端按 `status ∈ {pending, running, completed, failed}` 分支；DTO 不埋「下一步该干嘛」。

## 2. DTO 与校验

| 模型 | 位置 | 要点 |
| --- | --- | --- |
| `UploadOut` / `ResumeListItem` / `ResumeOut` / `ResumeUpdateRequest` | `schemas/resume.py` | 列表行继承 `ResumeFields`，只多一个 `overview`（由 `build_overview(parsed)` 读时算）；`parsed` 原样透传（不套今天的 schema 回读历史数据）；`name` 去空白后非空 |
| `JobListItem` / `JobOut` / `JobDraftOut` / `JobCreateRequest` / `JobUpdateRequest` | `schemas/job.py` | 列表行只带 `jd_excerpt`（前 `JD_EXCERPT_CHARS` 字），全文在 `JobOut`；`title/company` 先 strip 再判长度；`JobUpdateRequest` 至少给一个字段 |
| `GenerateRequest` / `InterviewStartOut` / `InterviewListItem` / `InterviewOut` / `ReportQuestion` | `schemas/interview.py` | 列表行继承 `InterviewHeadFields`（无 `questions`）；`ReportQuestion = QuestionSchema + order_index` |
| `Page[DataT]` | `schemas/common.py` | `{items,total}`，`total` 是**过滤后**的总数；ADR-0020 |
| `TaskOut` | `schemas/task.py` | `from_attributes=True` |
| `ResumeSchema` 及子模型 | `schemas/resume.py` | 抽取输出契约：事实条目必须带 `evidence`；未知键忽略（不 `extra="forbid"`） |
| `QuestionSchema` | `schemas/question.py` | `source=resume` 时 `evidence ≥ 4` 字；`checkpoints` 至少 1 条；列表不许空白项 |

## 3. 异常与响应

[`exceptions/base.py`](file:///d:/code/InterviewForge/app/exceptions/base.py) /
[`handlers.py`](file:///d:/code/InterviewForge/app/exceptions/handlers.py)

```json
{"error": {"code": "...", "message": "...", "details": {}}}
```

| 异常 | HTTP | code |
| --- | --- | --- |
| `NotFoundError` | 404 | `not_found` |
| `ValidationFailedError` | 422 | `validation_failed` |
| `ParseError` | 422 | `parse_failed` |
| `ConflictError` | 409 | `already_exists` |
| `LLMError` | 502 | `llm_error` |
| `LLMResponseError` | 502 | `llm_response_invalid` |
| `BudgetExceededError` | 429 | `cost_budget_exceeded` |
| 未预期 `Exception` | 500 | `internal_error`（固定文案，堆栈只进日志） |
| `RequestValidationError` | 422 | `validation_failed`（只留 `loc` / `msg`，不回显简历原文） |

`details` 由抛出方显式构造，不得含密钥/原始响应/绝对路径。

## 4. 后台任务

`BackgroundTasks` 取代 Celery（ADR-0011），任务在**同一进程**里跑。

### 4.1 执行顺序（实测）

```
dep-enter → endpoint → background → dep-exit
```

因此会排后台任务的写端点必须：

1. 注入 `SessionMakerDep`；
2. 在返回响应前由服务层用 `session_scope` **自己提交**；
3. 后台协程只认 id（`task_id` / `resume_id` / `interview_id`），不认 ORM 对象。

### 4.2 简历解析：[`run_resume_parsing`](file:///d:/code/InterviewForge/app/tasks/resume_parsing.py)

```
mark_running
  → parse 节点（脱离图直接跑，与主图同一份抽取代码）
  → 一个事务回写 resume.raw_text / parsed / name
  → record_step("parse", ms)
  → mark_completed({resume_id, name, note_count, prompt_version?})
```

失败：`mark_failed` 后返回 `{"ok": False}`，**不上抛**。

### 4.3 题目生成：[`run_question_generation`](file:///d:/code/InterviewForge/app/tasks/question_generation.py)

```
mark_running
  → open_checkpointer(checkpoint_path)            # AsyncSqliteSaver
  → build_agent_graph(make_live_nodes(...))
  → astream(初始 state, config, stream_mode="updates")
       每个 super-step → _record_updates → task.progress.steps.append({node, ms?})
  → aget_state → final_state
  → mark_completed({interview_id, finished, question_total, question_batch})
```

- 进度来自 `stream_mode="updates"`；`ms` 从该步 `trace` 里取（扇出分支不包 `traced`，
  所以可能没有 `ms`）。
- 异常：`mark_failed` + `_abort_interview`（只把仍 `running` 的报告头翻成 `aborted`），
  **吞在任务边界内**。
- `make_live_nodes()` 在装配期注入全部依赖（llm / sessionmaker / settings），
  节点内部不 `new` 客户端。
- 测试/脚本可注入 `nodes` / `llm` / `sessionmaker` / `checkpoint_path`。

### 4.4 任务行状态机

```
pending ──mark_running──► running ──┬── mark_completed ──► completed
                                    └── mark_failed ─────► failed
```

`pending` 由 API 在响应事务里建；`mark_running/mark_completed/mark_failed` 各开一个小事务。
行不存在时静默跳过（API 事务回滚但后台已排上的竞态）。

## 5. 中间件现状

**没有 `app/middleware/` 目录了**（T-312 / D-8(b)，2026-10-28）：原先的 `auth.py` /
`logging.py` / `rate_limit.py` 三个空壳已删除——它们一行实现都没有，留着只会让读者以为
"改改就能用"。`create_app()` 目前只挂 `CORSMiddleware`（`settings.cors_origins` 非空时）。

真要接：认证的前置是给所有业务表加 `user_id`（`docs/TODO.md`，D-7 未定）；限流按计划书
§6 风险 3 的结论单机不引入；访问日志的 request_id 那条决策在 `docs/TODO.md` §2.8。
新中间件从零写，不要怀念空壳。

## 6. 静态托管与 SPA 回退

[`app/main.py`](file:///d:/code/InterviewForge/app/main.py#L37-L87)：

- `FRONTEND_DIST = <repo>/frontend/dist`；目录不存在时**不挂载**（避免 `check_dir=True` 抛错）。
- 挂载排在 `include_router` 之后，`/` 不会吃掉 `/api/v1/...`。
- `_SpaStaticFiles.get_response`：仅静态 404 + 非 `api/` 前缀 + 最后一段不含 `.` 时回退
  `index.html`；`/api/v1/typo` 与缺失的 `/assets/*.js` 继续 404。
- lifespan 启动 `get_engine()`、停机 `await dispose_engine()`。

## 7. 端点 → 服务 → 数据 的对应

```mermaid
graph LR
    U1[POST /resumes/upload] --> RS[resume_service.accept_upload] --> R1[(resume+task)]
    U2[POST /interviews/generate] --> IS[interview_service.start_generation] --> R2[(interview+task)]
    U3[GET /interviews/id] --> IS2[fetch_report] --> R3[(interview+question)]
    U4[POST /jobs/inspect-image] --> JS[job_service.draft_job_from_image] --> L[LLM + 不落库]
    U5[POST|PUT /jobs] --> JS2[create/update_job] --> R4[(job)]
    U6[DELETE /resumes/id] --> RS2[delete_resume] --> R5[(resume → cascade interview/question)]
```
