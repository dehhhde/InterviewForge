# ADR-0007：持久化层落在 SQLite + SQLAlchemy JSON，主键用 uuid hex，repository 不提交事务

日期：2026-10-16
状态：已决定（M3）
相关：`app/models/`、`app/repositories/`、`app/utils/db.py`、`migrations/env.py`、`alembic.ini`、计划书 §6 风险 2

## 背景

M3 要给出"简历 → 报告"这条链唯一的数据出口。四个约束同时成立：

1. 本机没有 Docker，也不打算起 Postgres（见记忆 No-Docker embedded infra）。
   LangGraph 的 checkpoint 已经在用 `data/checkpoints.db`，再引入一个服务端只会让
   "clone 下来就能跑"这条学习项目的底线失效。
2. 抽取结果（`parsed`）的字段集还在动：M1/M2 期间 `TimelineResult` 加了 `dropped`、
   `DEGREE_ALIASES` 补过条目。
3. 全栈是 asyncio（FastAPI + `AsyncSqliteSaver`），ORM 必须异步可用。
4. 收尾落库要求幂等（AGENTS.md §4.6）：回退重跑不能叠出半份报告或重复题目。

## 决策

| 项 | 选择 | 被否决的方案 |
|---|---|---|
| 数据库 | SQLite 文件（`sqlite+aiosqlite:///./data/interview_forge.db`） | PostgreSQL + JSONB |
| JSON 列 | `sqlalchemy.JSON`（方言中立） | `postgresql.JSONB` |
| 主键 | `String(32)`，`uuid4().hex`，Python 侧默认 | 自增 `Integer` / `UUID` 类型 |
| 时间戳 | `created_at` / `updated_at` 由 Python 侧 `default` / `onupdate` 取值，**naive UTC** | `server_default=func.now()` |
| 表间关系 | 只留外键列 + 索引，**不写 `relationship()`** | ORM 关系 + 懒加载 |
| 事务边界 | repository 只 `flush`，提交由调用方的 `session_scope()` 决定 | repository 内部 commit |
| DDL 管理 | alembic（`migrations/` + `alembic.ini`，URL 从配置取） | `create_all()` 一把梭 |
| 迁移引擎 | 同步 `pysqlite`（`build_sync_engine` 把 `+aiosqlite` 摘掉） | async engine + `run_sync` 到处包 |
| SQLite PRAGMA | connect 事件里发 `foreign_keys=ON` / `busy_timeout=5000` / `journal_mode=WAL` | URL 参数或每次手写 |

## 理由

- **JSON 不拆子表**：抽取结果只有"整份读出来喂下游节点"一种用法，没人按
  `work_experiences[1].company` 查询。拆表等于 schema 每动一次就写一篇 migration。
  真要按语义检索的时候是 sqlite-vec 的活，不是关系查询的活。
- **uuid hex 主键**：`resume_id` 在上传 PDF 那一刻就要有值（文件名即 id），
  自增 id 要到 insert 之后才拿得到，会强迫上传接口与工作流之间多一次往返。
- **naive UTC + Python 侧默认**：服务端默认值需要 flush 后回读，异步下触发
  `MissingGreenlet`；而 `created_at` 在本项目只有"排序"一种用途，不值得为它开
  `eager_defaults`。
- **不写 `relationship()`**：懒加载在 asyncio 下同样是 `MissingGreenlet`；更麻烦的是它把
  "这次读了几张表"藏进属性访问。显式 join 让读放大在代码评审时就能被看见。
- **repository 不 commit**：一份报告要写 `interview` + N 行 `question`，必须同一个事务；
  切成多个事务后，中途失败就是半份报告，幂等重跑的前提直接崩塌。
- **`ON DELETE`**：`interview.resume_id` 与 `question.interview_id` 级联删除（脱离父行就没有意义），
  `interview.job_id` 用 `SET NULL`——岗位可以下线，但已经发给候选人的面试题不能跟着消失。

## 权衡与代价

- SQLite 单写者。`busy_timeout=5000` 把"立刻 `database is locked`"换成"最多排队 5 秒"，
  并发写多起来会先表现为延迟而不是错误。真要上量就是换 Postgres 的时候，
  届时只有 `JSON` → `JSONB` 与 URL 两处要改。
- JSON 列**不能增量更新**：改一个字段要整份读出来、改好、写回去。并发写同一份 `parsed`
  会最后写入者胜（当前写入方只有收尾节点，不构成问题；加 API 编辑功能时要想清楚）。
- `expire_on_commit=False` 换来了"commit 之后对象还可读"，代价是会话里的对象可能与库不一致。
- 内存库（`:memory:`）被 `build_engine` 显式拒绝：每个连接一份独立库，测试会
  "刚写入就查不到"，用 `StaticPool` 掩盖它只会让下一层更困惑。测试一律走 `tmp_path` 下的临时文件库。
- **`alembic.ini` 只能是 ASCII**。alembic 用 `ConfigParser.read(..., encoding="locale")`
  读它，本机 locale 是 GBK，中文注释会在任何迁移执行之前就 `UnicodeDecodeError`
  （实测 2026-10-16）。所以这些决策的说明写在 `migrations/env.py` 与本文里，ini 只留英文。
- `sqlalchemy` 必须带 `[asyncio]` extra 安装（拉 `greenlet`）。Python 3.14 上 pip 不会自动装，
  缺它时 `import sqlalchemy.ext.asyncio` 直接失败（实测 2026-10-16，`greenlet 3.5.6` 可用）。

## 后续影响

- `app/dependencies.py` 持有引擎单例（按 `settings.database_url` 缓存，URL 变了才重建）与
  `get_db` 生成器依赖，**这是 `app/` 里唯一调用 `build_engine()` 的地方**——两台引擎就是两套
  连接池抢同一个 SQLite 文件。当前两个消费者是 `app/scripts/seed.py` 与
  `tests/test_dependencies.py`（真 ASGI 应用，钉住"响应后提交 / 异常回滚"）；
  M4 的 API 层照 `SessionDep = Annotated[AsyncSession, Depends(get_db)]` 的形状接入，
  并在 lifespan 收尾调 `dispose_engine()`。
- `models/user.py` 保持空壳：认证是阶段 2 的活，本阶段四张表之间不存在 `user_id` 外键。
  加用户时`interview`/`resume` 都要补列，属于新迁移。
- 迁移序列从 `2056a1287299 (initial tables)` 开始。`tests/test_migrations.py` 有一条
  `alembic check` 用例，专门拦"改了模型忘了生成迁移"。
- 验收由三条测试固定：子进程 `alembic upgrade head` 建库 / 子进程 seed 后另一进程读得到数据 /
  `tmp_path` 保证用例间库隔离。
