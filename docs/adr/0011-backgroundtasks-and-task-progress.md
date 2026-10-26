# ADR-0011：后台化用 FastAPI `BackgroundTasks` + `task` 表进度，提交事务归服务层

日期：2026-10-20
状态：已决定（M6）
相关：`app/tasks/{resume_parsing,question_generation}.py`、`app/api/v1/{tasks,resumes,interviews}.py`、
`app/dependencies.py`、`app/services/{resume_service,interview_service,task_service}.py`、
`app/agents/nodes/generate_node.py`（`_require_budget`）、`app/utils/llm_client.py`、
`app/exceptions/base.py`、`scripts/probe_background_order.py`、`scripts/eval_e2e.py`、
`scripts/eval_rag.py`、计划书 M6 与 §6 风险 3、ADR-0007（持久化层）、ADR-0010（扇出形状）
（计划书 §8 把它列在"待写 ADR"的第 4 条短编号上，而 `docs/adr/README.md` 的 0004 已被"检索方案"
占着——两套编号并存的收尾账在 `docs/TODO.md` §3，本条按 `docs/adr/` 的 000x 序列编成 0011）

## 背景

阶段 8 的原始设计是 Celery + Redis 队列。本机没有 Docker，计划书 §7 已把 Celery/Redis
列入"明确不做"，所以"后台跑图"这件事只剩一条路：**在同进程里把一个协程排在响应之后**。
`BackgroundTasks` 能做到这点，但它带来三个必须实测才能确定的问题：

1. **后台协程看得见数据吗？** FastAPI 的依赖（`get_db`）在响应生成之后才收尾，而
   `BackgroundTasks` 的时机并不如直觉那样"在请求结束之后"。实测（FastAPI 0.143 /
   Starlette 1.8，`scripts/probe_background_order.py` 可复跑）顺序是
   **`dep-enter → endpoint → background → dep-exit`** ——后台协程跑在依赖收尾**之前**，
   所以它拿着的 SQLite 库里还没有本次请求写的那行数据。第一次做端到端时正是这里翻车：
   后台任务读 `resume` 读到 `None`。
2. **"进度写库"要靠什么驱动？** 用 `ainvoke` 一次跑完整图最简单，但整个图跑完之前
   轮询方只能看到 `running`，计划书那条"进度写库"的验收就落空了。
3. **一次运行的钱谁把关？** M5 真跑数据显示一轮端到端要 33 次调用、15.7 万 prompt token，
   回退还会翻倍。没有一个运行级上限，一个坏简历可以无限烧。

## 决策

| 环节 | 选择 | 被否决的方案 |
|---|---|---|
| 异步执行 | `BackgroundTasks`：API 立即返回 `task_id`，图在本进程跑 | Celery + Redis（本机无 Docker，§7 已排除；单机 15 题量级不需要队列） |
| 任务状态载体 | `task` 表一行：`kind` / `ref_id` / `status` / `progress.steps` / `result` / `error` | 内存字典（进程重启即失忆，轮询接口也读不到） |
| 提交时机 | 会唤起后台任务的写服务接口收 `sessionmaker`，自己用 `session_scope` 提交；端点注入 `SessionMakerDep` 而不是 `SessionDep` | 让 `get_db` 在依赖退出时提交（**实测太晚**：`background` 在 `dep-exit` 之前） |
| 进度来源 | `question_generation` 用 `astream(stream_mode="updates")`，每个 super-step 记一步 `{node, ms}`；`ms` 从 `trace` 通道的 `TraceStep` 取 | `ainvoke` 一把跑完（轮询全程只看到 `running`）；或自建 callback（多一套与图并行的观测面） |
| 终态读取 | 跑完再 `aget_state` 拿 `questions`/`finished`——`astream` 只吐增量 | 从最后一条 chunk 猜（扇出分支的增量合并不保证含最终 `questions`） |
| 任务边界 | 异常一律吞在任务边界：`mark_failed` 把错误码与 message 落进 `task` 行，不再上抛 | 抛出（后台协程抛出去只被 Starlette 记一条日志，没人接，轮询方只看到卡在 `running`） |
| 报告头补刀 | 中途崩溃时只把仍是 `running` 的 `interview` 翻成 `aborted`；已是终态的行不覆盖 | 无条件翻（`review` 收尾自己写过 `completed`，再翻一次就把一份好报告判死） |
| 成本护栏位置 | `generate` **扇出之前**查 `llm.usage` 的 prompt+completion 累计，`>= agent.max_tokens_per_run` 抛 `BudgetExceededError`（429） | 在 `generate_item` 分支里查（分支只看得见 `Send` 载荷，看不见累计）；跑完再统计（钱已经花了） |
| 上游 4xx/5xx | `openai.APIStatusError` 单独分一类 → `LLMError`，`status_code` 进 `details`，**不重试** | 走传输重试分支（实测 402 "Insufficient Balance"：重试三轮只是白烧三次往返） |
| 轮询端点 | `GET /tasks/{task_id}` 只读一行 `task`：不 join、不进图、不碰 checkpoint | 顺带读 `interview` 与题目（轮询是图运行那 80 秒里的唯一观察窗口，它必须便宜） |
| 评测脚本 | `scripts/eval_e2e.py` 复用生产接缝（`accept_upload` → `start_generation` → `run_question_generation`），不进 pytest | 另写一套调用序列（脚本绿、生产链路红的经典分叉） |

## 理由

1. **后台协程不能依赖请求事务**，这是实测出来的、不是推测的。修法有两种：端点自己提交，
   或入口服务自己提交。选后者是因为它与 ADR-007 那条"repository 只 `flush` 不 `commit`、
   提交时机归调用方"完全同构——后台任务就是**新的调用方**，它理应在服务层持有事务边界；
   把提交上提到端点会让"几个端点共用这个服务"时又散成多份提交纪律。
   `task_service` 早就按每步一个 `session_scope` 在写，这是同一件事的既成先例。
2. **进度用 `astream` 而不是 `ainvoke`**：验收要求"上传简历 → 轮询 → 拿到 15 题"，
   轮询要看得见"跑到哪一步"。`updates` 模式每个 super-step 吐 `{节点名: 通道增量}`，
   分支并行时同一轮 `generate_item` 会并成一个条目，粒度正好是"阶段"。
3. **护栏放在扇出之前**，语义是"当前不接受这一批"，所以 429 与它匹配；客户端拿到失败时
   这一轮还没花出题的钱。口径与 `eval_e2e` 报数同源（同一个 `llm.usage`），否则"护栏说没超、
   报表说超了"没法对齐。
4. **错误吞在任务边界**：后台协程没有请求上下文，异常上抛只会进 Starlette 的日志，
   而日志不是客户端能读的接口。`task` 行才是那条通道。

## 权衡与代价

- **`BackgroundTasks` 不持久**。进程重启，排队中的任务就地消失，`task` 行会永远停在
  `pending`/`running`（没有心跳、没有超时回收）。checkpoint 因为用 `SqliteSaver` 落在
  `data/checkpoints.db`，理论上能续跑，但**当前没有任何续跑入口**——已挂
  `docs/TODO.md` §2.4。真要靠这个交付，得先给 CLI 或端点做"从 checkpoint 恢复线程"。
- **单进程吞吐**。一次运行 30+ 次上游调用、实测 80–83 秒；并发上传共享同一事件循环
  和同一个 SQLite 文件的写锁。计划书 §6 风险 3 的结论是单机先不引入限流，
  `middleware/rate_limit.py` 仍是空壳。真出现排队积压时，第一个要动的不是这里的代码，
  而是"任务要不要挪出进程"。
- **成本护栏会有 overshoot**。实测一条完整链路跑了 **213,783 tokens**（prompt 137,542 +
  completion 19,470 + 回退批次），而上限是 200,000，护栏**没有触发**——因为检查点在每次
  扇出之前，越线的那一批已经在飞行中； overshoot 上界约等于**一轮扇出的花费**。
  要收紧只能改成分支级预算，而分支看不见累计用量（`Send` 载荷隔离，ADR-0010），
  得先把用量装进载荷。当前判断：一条链路的 overshoot 在可接受范围内，先不改。
- **`task` 行 `completed` ≠ 报告成功**。图跑到终点就算完成，哪怕 `review` 门没过、
  一题没落库；真正的判据是 `result.finished` 与 `interview.status`。这个契约有误导风险，
  是否改成"门没过就 `failed`"挂在 `docs/TODO.md` §2.4 等维护者定夺。
- **轮询接口没有任何鉴权**。所有表都还没有 `user_id`（`docs/TODO.md` §3），
  拿到 `task_id` 的人就能读它的进度与错误详情。`details` 里只放 id 与计数，
  不放简历正文——这条纪律在抛错处守着，处理器侧不兜第二遍。
- **`scripts/eval_rag.py` 是留壳**。它现在只打印"缺什么、卡在哪"并返回 2：没有真检索器
  （只有 `NullRetriever`）、`settings.embedding_provider` 为空、没有人工标注集，
  三个指标（Recall@k / Precision@k / 检索开闭消融）一个都算不出来。写壳是为了把
  "要测哪三件事"定下来，而不是留一个假绿脚本。

## 后续影响

- `app/tasks/` 是 API 唯一的后台入口。将来若真挪到独立 worker，换的是调度器，
  `task` 表契约与 `progress.steps` 的形状不动，轮询端点与前端不用改。
- M5 挂账的"定向重做"现在有了数据支撑：`eval_e2e.py` 报的 token 成本与 `task` 行里的
  回退轮数就是那笔账的分子（`docs/TODO.md` §2.3 第一条要求的正是这两个数）。
- 成本护栏读的是 `llm.usage`，所以任何**绕过 `LLMClient` 的调用**（未来直连第三方 OCR、
  自建 embedding）都不会计入预算——接那些链路时必须把用量并回同一个累计器。
- `SessionMakerDep` 这个形状会被后续所有"响应后还要读库"的端点复用（发通知、生成 PDF）。
  它们都会踩同一个坑，这条 ADR 就是给下一个人的答案。
