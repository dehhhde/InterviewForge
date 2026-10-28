# ADR-0018：失败信息的四条通道——details 过白名单、失败步骤挂异常、收尾写库自带日志、无落点报 `resume_gone`

日期：2026-10-27
状态：已决定（补充 ADR-0011 的"任务边界吞掉一切异常"一节）
相关：`app/services/task_service.py`、`app/agents/workflow.py`、`app/agents/state.py`、
`app/tasks/resume_parsing.py`、`app/tasks/question_generation.py`、
`tests/test_services/test_task_service.py`、`tests/test_agent_infra.py`、
`tests/test_tasks/`、ADR-0011（后台化与任务进度）、ADR-0013（`DELETE /resumes` 与 `force` 级联）

## 背景

ADR-0011 定了"任务边界吞掉一切异常并落进 `task` 行"。这次核对发现那条约定在四个地方
只做到了字面，没做到目的——**失败了，但没人知道是怎么失败的**：

1. `mark_failed` 把 `AppError.details` 原样写库，而 `GET /tasks/{task_id}` 是无鉴权的读端点
   且全量透传。实测来路里 `parse_node` 的 `NotFoundError.details` 带 `upload_dir` 与 `tried`
   两个服务器绝对路径——它们就这样进了公开 JSON。
2. `TraceStep` 的 `ok` / `detail` 两个字段是为失败归因设计的，但唯一调用点（`traced()` 的
   成功路径）从不传，全仓 `ok=False` 零命中。中止的运行在进度里看不出停在哪个节点。
3. 收尾那几笔写库（`mark_failed` / `mark_completed` / `record_step` / `_abort_interview`）
   有的在 `try` 外，`_abort_interview` 干脆是 `except Exception: pass`。DB 一抖，
   任务停在 `running`、报告头停在 `running`，连一条日志都不留。
4. `run_resume_parsing` 忽略 `ResumeRepository.update` 的返回值。行在解析期间被删
   （`DELETE /resumes/{id}` 或 `force=true` 级联，ADR-0013）时，任务照样标 `completed`，
   result 里还报着 `name`——轮询方拿到"成功"，库里什么都没有。

## 决策

| 环节 | 选择 | 被否决的方案 |
|---|---|---|
| `task.error.details` | 落库前过 `ERROR_DETAIL_KEYS` 键白名单（14 键，判据是"标识与计数"），**默认拒绝**；被挡下的键名记进 `_dropped_keys` | 黑名单（`upload_dir`/`path`/`reason`）——未来的节点会因为是"新写的"而自动拿到外泄许可；只滤值不滤键（`{"note": "D:\\data"}` 照样出门） |
| 值的第二层判定 | 带 `/` 或 `\` 的字符串、以及 dict 一律丢（`missing` 这种白名单键也可能装进整段路径） | 只信键名（同一条路径换个键名就过了） |
| 失败 TraceStep 的载体 | `traced()` 在 `except` 里重抛**之前**把 `ok=False` + `detail=<异常码>` 的 step 挂到异常属性 `if_trace_failed_step`，读取口是 `failed_trace_step(exc)`；任务边界拿它落成进度步 | 写进 `errors` 通道并加 `trace_fail:<node>:<code>` 前缀（任务书原案）——节点抛异常时任何 state 通道都落不了库，checkpoint 停在失败前一步，写进去的东西没人读得到；前缀还会造出第二套解析判据 |
| `detail` 的取值 | `AppError.code`；非 `AppError` 用 `type(exc).__name__` | 留空（"停在哪个节点"有了，"为什么停"还是要去翻日志） |
| 失败终态写不进库 | `mark_failed_logged`：内部 try，写不进去 `logger.exception`，**不重抛** | 重抛（调用它的那层本来就是"吞掉一切"，再抛只被 Starlette 记一条没上下文的日志）；静默（僵尸行彻底无人知晓） |
| 成功终态写不进库 | 两个任务模块各收进 `_record_completed`，失败只记日志，返回值仍是成功 | 改成报失败（图确实跑完了、结果已在业务行里，报失败会诱导重传/重跑，反而造出重复数据） |
| 补刀失败 | `_abort_interview` 的 `except: pass` → `logger.exception` | 保持静默 |
| 解析结果没有落点 | `update` 返 `None` ⇒ `mark_failed(AppError(code="resume_gone", status_code=404, details={"resume_id": ...}))`，进度里**不**写 `parse` 步 | 标 `completed`（假成功，本条的起因）；标 `completed` + 批注（轮询方读不到"失败"就不会重试，而重试正是这里想要的动作） |

## 理由

1. **默认拒绝才叫白名单**。这条防的不是某个节点写错，而是"以后新写的节点不知道自己会往外说"。
2. **键名与值是两本账**。`missing` 是合法键，但它的值可能是整条路径；白名单管前者，
   值判定管后者，缺一个都漏。
3. **异常是唯一同时知道"节点名"和"为什么停"的东西**。调用方只拿到异常；state 通道在异常
   路径上根本不会被合并。把失败 step 交给异常携带，是它唯一能被读到的形状。
4. **"写失败"与"跑失败"必须分开**。前者不该改变业务结论（题已经出完、简历已经解析完），
   只需要留下证据；证据的唯一去处是日志，因为库本身就是写不进去的那个。
5. **`resume_gone` 的严重度是 404，但它是任务终态不是 HTTP 响应**。`status_code` 字段
   在这个路径上只是给 ADR-0011 的读端点准备的（`task.error` 里没用它），留着是为了
   以后 `GET /tasks` 想按语义映射时不用再翻代码。

## 权衡与代价

- **白名单会把有用的诊断信息一起挡掉**。`_dropped_keys` 只留键名，运维要看真值得回日志——
  这是"公开端点"与"自助排障"之间的取舍，选前者。要往外给新字段，必须显式加键。
- **僵尸行仍然存在**，只是从"无人知晓"变成"日志里有栈"。回收闭环是 T-015（`docs/TASK_PLAN.md`），
  本 ADR 明确不解决它：清扫要判"多久没动算僵尸"，那是另一个决策。
- **`ok` 只在失败时显式写**。历史进度行里没有 `ok` 键，读侧 `step.get("ok")` 真值即通过，
  所以不需要迁移或补写。
- **`traced()` 给异常挂了属性**：`AppError` 与内置异常都有 `__dict__`，可行；但这意味着
  同一个异常对象被别处捕获时也会带这个属性。目前唯一的读取口是任务边界，影响面为零。
- **`resume_gone` 让一次"上传后秒删"变成一条失败任务**，前端会显示红色失败而不是空白成功。
  控制台还没有针对这个码的文案，只有 `error.message`。

## 后续影响

- 测试基线 **485 → 522 passed**（本次九张卡新增 37 条用例，其中本 ADR 覆盖 T-004/008/009/010 共 12 条）。
- `AGENTS.md` §4 节点契约第 3 条补一句："失败步骤由 `traced()` 挂在异常上，节点不要自己塞 `trace`"；
  §3 后台化那节追加白名单与"收尾写库自带日志"两条铁律。
- `docs/OPTIMIZATION_BACKLOG.md` 的 P0-4 / P1-3 / P1-4 / P1-5 四条销账，实测行在本文件对应。
- 前端要认 `resume_gone` / `upload_too_large` / `upload_kind_mismatch` 三个新码的文案（未做，已挂账）。
- T-106 的 canary 活体验证与本文件无关，但 `tests/test_agents/_fake_llm.py` 的
  `fenced_block` 定位方式变了：断言 prompt 内容的用例从此只认围栏，不认中文小标题。
