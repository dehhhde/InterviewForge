# ADR-0024：任务端点派生报告可用性——`completed` 不等于"有题可看"

日期：2026-10-28
状态：已决定（D-6 选 (a)：`GET /tasks/{id}` 增四个派生字段，不新增状态）
相关：`app/services/task_service.py::report_availability`、`app/schemas/task.py::TaskOut`、
`app/api/v1/tasks.py::get_task`、`frontend/src/api/types.ts`、
`frontend/src/views/GenerateView.vue`、`tests/test_api/test_m6_api.py`、
ADR-0015（`review` 纯批注、0 题也 `completed`）、`docs/TASK_PLAN.md` T-309 / D-6、
`docs/TODO.md` §2.4

## 背景

ADR-0015 把质量门拆掉之后，`review` 不再拦题：一条题都没攒到也照常收尾，
`interview.status` 落 `completed`。于是"任务完成了"与"报告能用"第一次分家：

| 情形 | `task.status` | `interview.status` | 用户看到 |
|---|---|---|---|
| 正常 | `completed` | `completed`，`total_questions=15` | 15 道题 |
| 全部被规则层丢光（0 题） | `completed` | `completed`，`total_questions=0` | 一份空报告 |
| 报告行被删（`DELETE /resumes?force=true`） | `completed` | 行不存在 | 轮询拿到 404 |
| 后台没跑到收尾 | `failed` | `aborted` | 失败信息 |

第二、三行的共同点是**任务没说谎**：后台确实跑完了。但控制台原来把 `completed`
一律当成功（绿色提示 + "去看报告"），于是 0 题那份被当成功庆祝。
真判据（`result.finished` + `total_questions > 0`）散落在读的人手里，
三个客户端（控制台、脚本、将来的导出页）会各写一遍，而且没人记得同步。

## 决策

| 环节 | 选择 | 被否决的方案 |
|---|---|---|
| 在哪儿判 | 服务端 `task_service.report_availability()` 一处推导 | 前端各算各的：同一句话被抄三份，且前端拿不到 `total_questions`（列表 DTO 不带正文，ADR-0020） |
| 字段 | `report_status` / `question_count` / `degraded` / `degraded_reason`，全部 Optional，默认空 | 只给 `degraded` 布尔：看到 `true` 却不知道是"0 题"还是"报告被删"，文案就写不出来 |
| 原因枚举 | `no_questions` / `report_gone` 两个常量 | 自由文案：前端要做文案映射时只能靠字符串匹配 |
| 覆盖范围 | 只对 `kind == question_generation` 派生；其余任务四项恒空 | 所有任务都派生：简历解析任务的 `ref_id` 指向 `resume`，那里没有"几道题"，恒 `None` 反而让客户端去猜它何时有意义 |
| 状态机 | **不动**：`interview.status` 仍是 `running` / `completed` / `aborted` 三态 CHECK | 加第四种"降级"状态：降级是**任务视角的读法**，报告头不该为一个展示问题多一个终态；那还要一条迁移 + 三处分支 + 前端类型改 |
| 触发条件 | `degraded` 只在 `task.status == 'completed'` 时成立 | 任何时候都为真：`running` 中途 `total_questions` 就是 0（题目只在收尾落库），那会把每一份在跑的报告都标成降级 |
| 端点成本 | 多一次按主键的 SELECT（`InterviewRepository.get_by_id`） | join 进任务的读查询：`GET /tasks/{id}` 的价值是"便宜"，但一次主键查询仍然是常数级；为省它把两条聚合揉进一句话不划算 |
| 控制台 | `degraded` 为真时弹 `warning` + 页内常驻说明，并按原因给不同文案 | 仍报成功、让用户到报告页自己发现：那是这次改动要消灭的体验 |

## 理由

1. **判据必须只有一个出处**。`completed` 与"能用"是两个事实，而两者都只有服务端知道
   （`task` 行 + `interview` 行）。把它写进 DTO，客户端就从"判断"退化成"分支"。
2. **不加状态是对的，因为降级不是生命周期**。一份 0 题的报告永远不会自己变成有题，
   但它也不是失败——重传简历只会得到同一批语料，说失败会诱导无效重试。
   把这条写进 `degraded_reason` 的文案里（"重跑会得到同一批语料"）比加一个终态更省。
3. **`report_gone` 单独成原因是因为它与 404 不同**。轮询一个还在跑的任务、报告却没了，
   这不该报"任务失败"（后台跑完了，只是没有落点，ADR-0018 的 `resume_gone` 是同一族事实）。
4. **四项都 Optional 让客户端能区分"没有这项数据"与"这项数据是假的"**。
   简历解析任务拿到的 `degraded` 是 `False`（默认值），而不是"没算过"——`kind` 一句就解释清了，
   不需要额外一个 `availability_computed` 布尔。
5. **前端文案里明写"重跑无用"**。`degraded` 为真时最容易发生的错误动作就是再点一次生成，
   而 `plan` 是确定性的（ADR-0009/0019），同一份语料同一份配额表必然给出同样的 0 题。

## 权衡与代价

- **多一次 SELECT 在轮询路径上**。前端 2 秒一次 `GET /tasks/{id}`（`POLL_INTERVAL_MS`），
  现在每次多一条按主键的查询——SQLite 上这是微秒级，且任务终态后轮询就停了。
  真要省可以缓存，但那要引入失效判据，不值。
- **`review_issues` 条数没进 DTO**。批注是纯观测（ADR-0015），它不改变"能不能用"；
  控制台在详情页看批注。要放进列表得先回答"'有批注'该不该影响排序"，那是产品问题。
- **`degraded` 的语义是"完成了但不可用"，不是"质量差"**。带 5 条批注的 15 题卷子
  `degraded` 为假——这是有意的：批注不拦门，把"有批注"算成降级等于在 DTO 里复活质量门。
- **`report_gone` 只在任务 `completed` 时出现**。出题任务的报告头是**先于任务行**建的
  （`start_generation` 同一个事务里），所以"行不在"通常意味着它被删了
  （`DELETE /resumes?force=true`）；但一个 `failed` / `running` 的任务遇到同一现象时
  不报降级，因为那时"没有落点"这件事已经由任务自己的状态说清了，再叠一个原因会让
  两个通道讲同一件事。

## 后续影响

- **`AGENTS.md` §3 后台化六条**要补一句：`GET /tasks/{id}` 有四个派生字段，
  判据在 `task_service.report_availability()`，新客户端不要再自己读 `total_questions`
  拼"能不能用"。
- **`docs/TODO.md` §2.4** 那条"`task.status == completed` 不等于报告可用"已销，
  理由是本 ADR 把判据收进了服务端。
- **控制台的 `degradedNote` 是文案，不是判据**。它只做原因到句子的映射，
  任何"什么算降级"的改动都发生在服务端。
- **`TaskOut` 从此不能只靠 `model_validate(task)` 构造**：端点要 `model_copy(update=…)`
  拼上派生的四项。以后若有人图省事把 `report_availability` 塞进 `Task` 模型，
  那就是把两行数据揉进一个 ORM 对象，别这么做。
