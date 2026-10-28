# ADR-0023：同一对简历·岗位只允许一份在跑——生成入口的 409

日期：2026-10-28
状态：已决定（D-5 选 (a)：`(resume_id, job_id)` 且 `status == 'running'`）
相关：`app/services/interview_service.py::start_generation`、
`app/repositories/interview_repository.py::get_running_for`、`app/exceptions/base.py`（`ConflictError`）、
`tests/test_api/test_m6_api.py`、ADR-0011（后台化与成本护栏按单次运行计）、
ADR-0013（撞自然键 409 的先例）、`docs/TASK_PLAN.md` T-209 / D-5、
`docs/OPTIMIZATION_BACKLOG.md` P0-6 / OPT-4.2

## 背景

`POST /interviews/generate` 原先只查两个 404（简历、岗位），然后就建报告头。
同一对 `(resume_id, job_id)` 连点两次 = **两个报告头 + 两张图**，每张几十次上游调用。

成本护栏拦不住它：`agent.max_tokens_per_run` 是按**单次运行**的累计用量判的
（ADR-0011），两次运行各在自己的用量账本里，谁都不越线。控制台的按钮没有防抖之外
的保护——刷新页面再点一次就绕过去了。

## 决策

| 环节 | 选择 | 被否决的方案 |
|---|---|---|
| 判重键 | `(resume_id, job_id)` | 只按 `resume_id`：同一份简历换岗位重出卷是主流程，只按简历会把它挡掉 |
| 状态口径 | 只认 `status == 'running'`；`completed` / `aborted` 一律放行 | 任何既有报告都挡（改完简历再跑是真实需求）；任何既有报告都不挡（就是本 ADR 要修的现状） |
| 响应 | `ConflictError` → 409 `already_exists`，`details.{interview_id, resume_id, job_id}` | 409 不带 id（客户端只剩"刷新列表自己找"这一条路）；200 + 复用旧任务（等于偷偷把 id 换掉，前端以为又开了一次） |
| 实现 | 先查后插（`get_running_for` 返回 `None` 才 `create`） | 加唯一索引（见下）；`SELECT … FOR UPDATE`（SQLite 没有行锁语义可言，用不上） |
| 判据位置 | 服务层 `start_generation`，与两个 404 并排 | 端点里（API 会开始拼业务判据）；repository 里（"能不能开跑"不是仓储语义） |
| 前端 | 提示文案已有 409 分支，`details.interview_id` 供"去报告"链接 | 前端自己防抖（只能挡住自己那次误触，挡不住第二个标签页） |

## 理由

1. **先到的那份已经有结论可等**。同一对再跑一遍不会给出不同的卷子——`plan` 是确定性的
   （ADR-0009/0019），`raw_text` 与 `jd_text` 都没变。挡住的不是"用户想再跑一次"，
   而是"用户在不知道已经跑着的情况下又点了一次"。
2. **终态必须放行**。改名、改岗位、重传简历之后重出卷都是常规操作；
   把"有报告"当锁会让工具变成只读的。
3. **`details` 里三个 id 都要给**。409 是客户端唯一能自动处理的错误码，
   而它要做的动作是"把用户带到那份正在跑的报告"——只给 `interview_id` 的话，
   前端还得反查简历与岗位才能摆出上下文。
4. **检查放在服务层是因为它要读库**。端点不 import models、不拼查询（AGENTS.md §3），
   而这个判据必须读一行 `interview`，所以它天然属于 `services`。
5. **它不改变后台路径的任何一行**。`start_generation` 依旧当场提交两行
   （ADR-0011 的"后台在依赖收尾前跑"），只是现在提交之前多一次 SELECT。

## 权衡与代价

- **先查后插不是原子操作**。两个请求恰好同时通过检查，仍可能各建一行：
  SQLite 是单写者，但"查"与"插"不构成一个写事务的临界区。挡的是误触双击，
  不是恶意并发——后者要鉴权 + 配额（D-7），单机自用不做。
- **修好它的方式本该是唯一索引**，但 `(resume_id, job_id)` 上建唯一约束是**错的**：
  同一对合法地有多份历史报告（终态就可以再跑），约束会把正常操作变成 500。
  真要做成原子，得是"部分唯一索引"（`WHERE status='running'`），SQLite 支持，
  但那要给 `interview` 加一条迁移，且并发写入时返回的是 `IntegrityError`——
  错误码与 409 的映射得再写一层。当前规模（单机自用、一个人点）不值这个复杂度，
  已记进 `docs/TODO.md`。
- **`aborted` 与 `failed` 都算放行**。AGENTS.md 里 `aborted` 是"后台没跑到收尾"，
  那正是最该允许重跑的状态；把它当"在跑"会让一次崩溃把这一对锁死。
- **僵尸 `running` 行会挡住重跑**。回收靠 `python -m app.scripts.reap_tasks`
  （T-015，默认 dry-run），不是本 ADR 的范围。一行停在 `running` 的报告同时是
  "永远在生成中"的列表行与"这一对不能重跑"的锁——两份症状，一个成因。

## 后续影响

- **`AGENTS.md` §3 的写入侧四条**要补一句：生成入口有一条"同一对只跑一份"的先查后插判据，
  它不替代鉴权，也不替代任务行的僵尸回收。
- **`test_m6_api.py` 有一条老用例被迫改写**：它原先对同一对连续 POST 两次来验"两次都能开跑"，
  现在第二次会 409，于是先把第一份收成 `completed`。这不是放宽断言——
  它测的是"参数确实传到了任务里"，而"第二份能不能开跑"已由新用例单独钉住。
- **控制台无需改动**：`GenerateView` 的 409 分支原本就存在（撞自然键的先例来自 ADR-0013），
  只是这次会给到 `interview_id`。
