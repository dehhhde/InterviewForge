# 09 · 运行、测试与排障

## 1. 环境事实（先读，能省很多时间）

| 事实 | 影响 |
| --- | --- |
| Python **3.14.7**，只认 `.venv/Scripts/python.exe` | 裸 `python` 在本机退出码 49 |
| 没有 `make`、没有 `uv` | `Makefile` 目标不可用，用等价命令 |
| Shell 是 Git Bash（POSIX sh），控制台是 **GBK** | 读写文本必须显式 `encoding="utf-8"`，否则中文 `UnicodeDecodeError` |
| 源码不写字面零宽字符 | 统一用 `"\u200b"` 转义 |
| `sqlalchemy` 必须带 `[asyncio]`（greenlet） | 缺它 `import sqlalchemy.ext.asyncio` 直接失败 |
| SQLite 不支持 `:memory:` | `build_engine()` 显式拒绝；测试用 `tmp_path` 文件库 |
| `alembic.ini` 只能 ASCII | locale=GBK，中文注释会在迁移前炸 |

## 2. 首次安装

```bash
py -3.14 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # ruff / mypy / pytest
cp .env.example .env    # 然后填 DEEPSEEK_API_KEY / DATABASE_URL / JWT_SECRET
```

`.env` 至少需要：

```dotenv
APP_ENV=development
DATABASE_URL=sqlite+aiosqlite:///data/interview_forge.db
DEEPSEEK_API_KEY=...          # 必填，不要提交、不要回显
JWT_SECRET=...                # Settings 必填字段（鉴权尚未接线）
# 可选：LLM_MODEL_EXTRACT / LLM_MODEL_GENERATE / LLM_MODEL_REVIEW / LLM_MODEL_VISION
#      EMBEDDING_PROVIDER / EMBEDDING_MODEL / EMBEDDING_DIM
#      UPLOAD_DIR / CORS_ORIGINS
```

> `Settings` 里 `deepseek_api_key`、`database_url`、`jwt_secret` 没有默认值，缺一个就启动失败。

## 3. 初始化数据库

```bash
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m alembic upgrade head
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m alembic check          # 模型/迁移漂移检查
```

迁移会创建 `data/interview_forge.db` 与四张业务表 + `task` 表。checkpoint 库
`data/checkpoints.db` 由 LangGraph 首次运行时自动创建。

## 4. 种子数据

```bash
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m app.scripts.seed
```

灌 2 个岗位（后端/前端）+ 3 份合成简历。**幂等**：岗位按自然键 upsert，简历用固定 id
`seed-resume-*` 先查后写；`raw_text` 与 `parsed.evidence` 自洽（能过校验层）。
不打印简历内容（GBK 控制台 + 个人信息）。

## 5. 启动后端

```bash
./start.sh              # 默认 127.0.0.1:8000
./start.sh 9000         # 指定端口
RELOAD=1 ./start.sh     # 热重载（只盯 .py）
```

`start.sh` 做四件事：定位 `.venv`、提示缺库/缺 `frontend/dist`、固定 `PYTHONPATH=.` 与
`-X utf8`、`exec uvicorn app.main:app`。

等价手敲：

```bash
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- 控制台：`http://127.0.0.1:8000/`
- 接口文档：`http://127.0.0.1:8000/docs`
- 健康检查：`GET /api/v1/health`

## 6. 前端

```bash
cd frontend
npm install
npm run dev        # 127.0.0.1:3000；/api /docs /redoc /openapi.json 代理到 8000
npm run typecheck  # 改完先过这一关
npm run build      # 产出 dist/，后端只读它
```

- `dist/` 与 `node_modules/` 不进库。
- 访问 `http://127.0.0.1:8000/` 时看到的是**上次 build 的产物**；改源码后必须重新 build。
- 后端缺 `frontend/dist` 不报错，只是 `/` 404（SPA 挂载有目录保护）。

## 7. 质量门（提交前三条全绿）

```bash
.venv/Scripts/python.exe -m ruff check app tests migrations
.venv/Scripts/python.exe -m mypy app
.venv/Scripts/python.exe -m pytest -q
```

- 当前基线：**485 passed / ruff clean / mypy 102 files clean**（ADR-0015 删质量门时同步
  删了两条门限用例）。
- 动了 `frontend/` 另加 `cd frontend && npm run typecheck`。
- `ruff`：line-length=100（按东亚宽度计），`target-version=py312`；
  `migrations/versions/*` 豁免 E501。
- `mypy`：启用 pydantic 插件，`check_untyped_defs=true`。
- `pytest`：`asyncio_mode=auto`；`DeprecationWarning` 视为错误，但 langgraph 1.2.15 内部的
  `asyncio.iscoroutinefunction` 例外。

## 8. 测试结构

```
tests/
├── conftest.py                 # db_url / engine / session / db_sessionmaker
├── test_agent_infra.py         # 图骨架（假节点）
├── test_agents/                # 每个节点 + _fake_llm + M4/M5 管线
├── test_services/              # 纯规则服务（match/profile/question/review/task/resume）
├── test_repositories/          # 仓储契约、子类、隔离
├── test_models/                # ORM 形状与引擎 PRAGMA
├── test_extractors/            # 四个抽取器 + sections
├── test_parsers.py             # PDF/DOCX/图片解析
├── test_api/                   # M6 API、CRUD、图片随录
├── test_tasks/                 # 两个后台任务
├── test_migrations.py          # 真子进程 upgrade/downgrade/check
├── test_prompts_and_decorators.py
├── test_llm_client.py
├── test_dependencies.py
├── test_schemas.py
├── test_scripts/test_seed.py
└── test_vectorstore/
```

原则：

1. **不断言 LLM 具体文案**，只断言结构、数量、预算、通道变化。
2. 单节点注入 `_FakeCompletions` 形状的替身。
3. 真实响应录制回放：`tests/fixtures/llm/*.json`，测试只读 `response` 字段。
4. 抽取质量用 `scripts/eval_extraction.py` 出 P/R/F1，**只看不断言**（模型抖年会红）。
5. 时间戳断言只比函数身份/不倒挂，不 sleep。
6. `get_settings()` 是 `lru_cache` 单例，测试用 `monkeypatch.setattr` 换，不改 `.env`。

## 9. 手动脚本（联网、花 token、不进 pytest）

| 脚本 | 用途 | 跑法 |
| --- | --- | --- |
| `app/scripts/hello_llm.py` | 最小连通性检查 | `PYTHONPATH=. ... -m app.scripts.hello_llm` |
| `app/scripts/live_check.py` | 真简历走 parse + validate，录 `validate_liang_qingsheng.json` | 同上 |
| `app/scripts/eval_extraction.py` | 抽取字段 P/R/F1 | 同上 |
| `scripts/eval_e2e.py` | 端到端评测（要简历目录 + JD 文件） | `... -X utf8 scripts/eval_e2e.py [简历目录] [JD]` |
| `scripts/run_e2e_live.py` | 真跑整图 | 直接运行 |
| `scripts/export_report_md.py` | 导出报告 Markdown | 直接运行 |
| `scripts/eval_rag.py` | 检索评测（目前只是壳） | 见文件头 |
| `scripts/probe_send_fanout.py` | 验证 `Send` 分支只拿载荷 | 直接运行 |
| `scripts/probe_background_order.py` | 验证 `dep-enter → endpoint → background → dep-exit` | 直接运行 |

录制 fixture 的脚本会**覆盖标准答案**，改 prompt 前后各跑一次对比，别当自动回归刷。

## 10. 常见故障

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 裸 `python` 退出码 49 | 机器上的 Python 不可用 | 只用 `.venv/Scripts/python.exe` |
| `UnicodeDecodeError` 读配置/迁移 | 控制台 GBK，用了 locale 编码 | 显式 `encoding="utf-8"`；alembic.ini 保持 ASCII |
| `import sqlalchemy.ext.asyncio` 失败 | 缺 greenlet | `pip install "sqlalchemy[asyncio]"` |
| `unable to open database file` | 库父目录不存在 | `build_engine` 会 mkdir；确认路径可写 |
| `database is locked` | 两台引擎/长事务 | 只经 `get_engine()`；确认 WAL + busy_timeout 生效 |
| 首请求 500「no such table」 | 没跑迁移 | `alembic upgrade head` |
| 后台任务读不到行 | 端点用了 `SessionDep` 而非 `SessionMakerDep`，事务未提交 | 服务层 `session_scope` 自己提交 |
| 扫描件 422 | `LLM_MODEL_VISION` 为空 | 在 `.env` 配置视觉模型；不会退回文本模型 |
| `/` 404 | `frontend/dist` 不存在 | `cd frontend && npm run build` |
| `/api/v1/typo` 返回 HTML | SPA 回退不该吞 api 路径 | 检查 `_SpaStaticFiles` 的 `api/` 与 `.` 判断 |
| 岗位建两次 409 | `title+company` 自然键 | 用错误体 `details.job_id` 转 PUT |
| 生成任务瞬间 failed | 余额不足 402 / 预算 429 / 配置缺失 | 看 `task.error.code`；402 不进传输重试 |
| 题目数为 0 但状态 completed | 收尾拿到空批次（ADR-0015 一次成卷） | 看 `review_issues` 与 `task.error`，不是「评审拦了」 |

## 11. 升级/改动后的自检

1. 改模型 → `alembic revision --autogenerate` + `alembic check` + 跑 `test_migrations.py`。
2. 改节点/服务 → 跑对应 `test_agents/*` / `test_services/*`，再看 `trace` 与 `errors` 通道。
3. 改 prompt → 备份旧 fixture，`live_check.py` 重录，比较 `response` 差异；不要直接提交新基线。
4. 改前端 → `npm run typecheck && npm run build`，用浏览器验收 `/` 与 `/docs`。
5. 改配置字段 → 三份 yaml 同步，跑 `test_dependencies.py` 与启动自检。
