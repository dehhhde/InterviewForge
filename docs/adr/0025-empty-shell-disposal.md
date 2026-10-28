# ADR-0025：空壳模块的处置——删 `candidates` 与整个 `app/middleware/`，留"有主"的那几个

日期：2026-10-28
状态：已决定（D-8 选 (b)：删一部分，不是全删也不是全留）
相关：`app/main.py`、`app/api/v1/__init__.py`、被删的 `app/api/v1/candidates.py` 与
`app/middleware/{__init__,auth,logging,rate_limit}.py`、保留的 `app/models/user.py` /
`app/api/v1/auth.py` / `app/vectorstore/*` / `app/utils/{text,file_handler,guardrails,validators,formatters}.py`、
`tests/test_prompts_and_decorators.py`、`docs/TASK_PLAN.md` T-312 / D-8、
`docs/OPTIMIZATION_BACKLOG.md` OPT-8.5、`docs/TODO.md` §2.4 / §2.8

## 背景

仓库里有一批"一行 docstring"的文件，从 milestone 初建壳时就存在，一直 0 import。
它们的危害不是占几 KB，而是**广告**：`guardrails.py` 看着像有成本护栏（真那份在
`generate_node._require_budget`）、`middleware/logging.py` 看着像有访问日志、
`candidates.py` 看着像有一条候选人 API（这个领域早在 M3 就被 `resumes` 取代）。
读码的人（包括 AI）会据此得出"项目确实做了 X"的错结论——这条账在
`docs/OPTIMIZATION_BACKLOG.md` P0 段里记了两次，理由都是"删除是维护者的决定"。

2026-10-27 起仓库是 git 库（`main`、1 个提交、301 个跟踪文件），删除从"不可逆"变成
"可以从提交取回"，D-8 的卡点理由只剩"这批改动还没提交"这一条运营层面的顾虑。

## 决策

| 文件 | 处置 | 依据 |
|---|---|---|
| `app/api/v1/candidates.py` | **删** | 领域已被 `resumes` 取代（ADR-0013），没有未来使用者；它也不在 `api_router` 里，删掉不改变任何路由 |
| `app/middleware/__init__.py` | **删** | 目录里只有空壳 |
| `app/middleware/auth.py` | **删** | 见下"为什么 auth 中间件删而 auth 端点留" |
| `app/middleware/logging.py` | **删** | 访问日志是"接不接"的取舍（TODO §2.8），不是待填的壳；留着会让人以为 `request_id` 已经在传 |
| `app/middleware/rate_limit.py` | **删** | 单机不引入限流（计划书 §6 风险 3 的结论）；真要引入时先给 `POST /interviews`，那是新写在 `main.py` 里的一段，不是补全这个壳 |
| `app/models/user.py` | **留** | T-501（多用户与鉴权）要它；`models/__init__.py` **有意不登记**，所以它不会生成迁移（ADR-0007 的那条"新增模型必须登记"在这里是刻意例外） |
| `app/api/v1/auth.py` | **留** | 同上，是 T-501 的端点落点；它没有挂进 `api_router`，所以在 OpenAPI 里不存在，不构成"广告" |
| `app/vectorstore/{client,job_indexer,question_dedup}.py` | **留** | 卡在 D-3（embedding 选型），是"已知欠账"而不是"忘记删"；`docs/TODO.md` §2.2 挂着它们 |
| `app/utils/{text,file_handler}.py` | **留** | 已有真实现：`file_handler` 是 T-002 的上传闸（ADR-0017），`text.py` 是 T-208 决定**不合并**的取舍（TODO §2.9） |
| `app/utils/{guardrails,validators,formatters}.py` | **留（记账）** | 仍 0 import、仍没有主人；删不删没有争议，只是这次没有依据说"谁将来要用它们"，所以记进 BACKLOG 等下一次 |

## 理由

1. **"广告"是删除的唯一硬理由**。中间件三件套最坏：`create_app()` 只挂了 CORS，
   而 `middleware/` 的存在让"有没有鉴权"这个问题在 grep 层面有两个答案。
2. **`candidates.py` 与 `auth.py` 处置不同，因为它们的未来存在性不同**。
   候选人不是路线图上的东西（简历就是那个领域的实体）；登录是 T-501 的明确产物，
   而且 `jwt_secret` 已经是必填配置——那说明"迟早要鉴权"是已经付出的成本。
   留一个未挂载的 `auth.py` 不会骗任何人：OpenAPI 里看不到它，唯一的读者是将来写 T-501 的人。
3. **空目录比空文件更糟**。`app/middleware/` 现在整个不存在，`grep -rn middleware app/`
   只剩 `fastapi.middleware.cors` 与 `decorators.py` 的一句历史注释——后者是**解释**
   而不是**广告**，所以留着。
4. **删掉的文件没有测试依赖**。实测：`candidates.py`、三个中间件壳、`__init__.py`
   全仓 0 import（那批账本来就是"0 调用点"才立起来的），删完 `ruff` / `mypy` / `pytest`
   三条不变红。
5. **`user.py` 与 `auth.py` 是"有主空壳"与"无主空壳"的分界**。这条分界能复用：
   下次再看见一行 docstring 的模块，问的不是"它有没有实现"，而是"T-5xx 或 ADR 里
   有没有它"。

## 权衡与代价

- **鉴权与限流仍然没有**。删除不改变任何运行时行为——原先也没接线。`GET /tasks/{id}`、
  `GET /interviews/{id}` 依旧是"知道 id 就能读"，这条账挂在 TODO §2.4，
  归 D-7 / T-501，本 ADR 只是在文档层把"看起来有人管"的错觉去掉了。
- **`@log_call` 与 `request_id_var` 还在，且仍没有被 `set` 过**。
  `tests/test_prompts_and_decorators.py` 测的是装饰器本身，不是接线；
  删访问日志壳之后，"要不要每请求一条 rid 日志"变成一个纯取舍（TODO §2.8），
  下次动手是**新建**一个中间件而不是补全。
- **`app/models/user.py` 留在 `app/models/` 里而不登记**是个持续的陷阱：
  有人若"顺手"把它登记进 `__init__.py`，autogenerate 会生成一张现在没用的 `user` 表。
  这条约束本来写在 ADR-0007 里，本 ADR 把它再点名一次。
- **`guardrails` / `validators` / `formatters` 没删**。它们与"删除是为了消除误导"这条
  理由相比弱一档（三个都是工具函数名，不像"中间件"那样暗示一条链路已接线），
  但留着仍会消耗下一次审计的时间。
- **`docker/` 目录与 `hello_llm.py` 不在此列**：T-018 已把它们改成诚实形态
  （"这里没有编排" / 真的会打印状态），留着不再有危害。

## 后续影响

- **`project_structure.md`、`README.md`、`docs/code-wiki/{01,06,README}.md`** 都删掉了
  `middleware/` 与 `candidates.py` 的行，`06-api-and-tasks.md` §5 改写为
  "没有 `app/middleware/` 目录了"并列出重新引入鉴权/限流/访问日志各自的前置条件。
- **`docs/TASK_PLAN.md` T-501 的前置描述**里"（`app/api/v1/auth.py`、`app/middleware/auth.py`
  均空壳"要改成"`auth.py` 仍是空壳、`middleware/auth.py` 已随 T-312 删除"。
- **`AGENTS.md` §3** 里讲到日志的那条（"新模块直接 `logging.getLogger`，别自己挂 handler"）
  不受影响；但任何"中间件壳还在"的措辞都不再有依据。
- **`docs/TODO.md` §2.4 的认证条目与 §2.8 的访问日志条目**已按本 ADR 改写：
  账还在，但"壳在"这句话不成立了。
