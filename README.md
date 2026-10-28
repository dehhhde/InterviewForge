# InterviewForge

基于 Agent 架构的简历解析与面试题生成系统。
技术栈：FastAPI + LangGraph + DeepSeek（OpenAI 兼容端点）+ **SQLite / sqlite-vec** +
FastAPI **`BackgroundTasks`** + **`SqliteSaver`** checkpoint。
没有 PostgreSQL / pgvector / Celery / Redis / Langfuse——那套是初始设计快照，
实际落地换了形态（`docs/adr/0007`、`docs/adr/0011`，AGENTS.md §8 有一份"已被替换掉的基建"清单）。

## 这是什么

本仓库处于**学习优先**的建设模式：每个模块文件顶部一行 docstring 标注它的职责与
**应在哪个阶段实现**。里程碑 M0–M6 见 [`docs/LANGGRAPH_BUILD_PLAN.md`](./docs/LANGGRAPH_BUILD_PLAN.md)
（进度、执行约定与"别踩这些坑"写在仓库根的 [`AGENTS.md`](./AGENTS.md)；欠账见 `docs/TODO.md`；
决策见 `docs/adr/`）。M0–M6 已全部落地，写入侧 CRUD 与控制台是它之后补的。

## 环境

- Python **3.14.7**，虚拟环境位于 `.venv/`；**只能用 `.venv/Scripts/python.exe`**，
  裸 `python` 在本机以退出码 49 失败。
- 依赖清单在 `requirements.txt` / `requirements-dev.txt`（`pyproject.toml` 的 `dependencies`
  是空的，只装工具配置——这条欠账挂在 `docs/TODO.md` §3）。
  `sqlalchemy` 必须带 `[asyncio]` extra（它拉 `greenlet`），Py 3.14 上 pip 不会自动装。
- 控制台是 **GBK**：任何文本读写都要显式 `encoding="utf-8"`，起服务用 `-X utf8`。
- 图片简历 OCR 不走 PaddleOCR（3.14 无分发），改用多模态 LLM：`LLM_MODEL_VISION` 没配时
  `ImageParser` 直接抛 `parse_vision_unconfigured`（422）——文本模型"读"图不会报错，
  只会编出一份看起来很完整的简历，比失败更糟。

## 快速开始

```bash
cp .env.example .env            # 填 DEEPSEEK_API_KEY 等（.env 永不进版本库，也别回显它）

.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt

# 建库：迁移不会自动跑，缺 data/interview_forge.db 时服务只会告诉你一句
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m alembic upgrade head
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m app.scripts.seed   # 灌 2 岗位 + 3 简历，可反复跑

PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m app.scripts.hello_llm  # 连通性自检

bash start.sh                   # 打开 http://127.0.0.1:8000/
```

`.env` 里 `LOG_LEVEL` 的优先级**压过** `configs/<APP_ENV>.yaml` 的 `app.log_level`——
想开调试日志改前者，不是改后者。

## 控制台（Vue 3）

同源控制台在 `frontend/`：Vue 3 + TypeScript + Vite + Element Plus。后端只读构建产物，
所以首次启动或改完前端后要构建一次：

```bash
cd frontend
npm install
npm run build        # 先跑 vue-tsc，再产出 frontend/dist

cd ..
./start.sh           # 打开 http://127.0.0.1:8000/（/docs 仍是接口文档）
```

只调前端时用 `npm run dev`：Vite 跑在 `127.0.0.1:3000`，`/api` 与 `/docs` 已代理到 8000，
不需要额外改 CORS。架构与挂载约定见 `docs/adr/0016-vue3-console-dist-mount.md`。

## 六层架构

| 层 | 目录 | 实现阶段 |
|---|---|---|
| API Gateway | `app/api/` `app/schemas/` | 阶段 2（`app/middleware/` 三个空壳已在 T-312 删除） |
| Service | `app/services/` | 阶段 2 建壳，逐阶段填充 |
| Agent | `app/agents/` | 阶段 7（核心） |
| Data | `app/models/` `app/repositories/` `migrations/` | 阶段 3 |
| Parse / Extract | `app/parsers/` `app/extractors/` | 阶段 5 |
| Retrieve | `app/vectorstore/` | **未接线**：只有 `retriever.py` 的 `SkillRetriever` 协议 + `NullRetriever`，embedding 选型未决（`docs/TASK_PLAN.md` D-3） |
| Async | `app/tasks/` | 阶段 8 的形态变了：`BackgroundTasks` 起协程，不是 Celery（`celery_app.py` 是空壳，ADR-0011） |
| 控制台 | `frontend/` | Vue 3 + Vite 工程，后端只读 `frontend/dist/`（ADR-0016） |
| Cross-Cutting | `app/utils/` `docs/adr/` | 阶段 4 / 9 |
| 容器化 | `docker/` | 无。目录里只剩一份说明自己"没有 Docker"的 README |

## 约定

- 分层铁律：`api -> services -> repositories`，api 不 import models，service 不写 SQL。
- 所有 LLM 调用走 `app/utils/llm_client.py`，不许在节点里直接建 client。
- 每个架构决策写一条 ADR 到 `docs/adr/`。
- 密钥只进 `.env`，行为配置只进 `configs/*.yaml`。

## 开发命令

本机**没有 `make`，也没有 `uv`**，`Makefile` 只是同一批命令的速查表（文件顶已注明），
直接复制下面这些跑：

```bash
.venv/Scripts/python.exe -m ruff check app tests migrations   # 三关必须全绿
.venv/Scripts/python.exe -m mypy app
.venv/Scripts/python.exe -m pytest -q                         # 全离线；只有迁移用例真起子进程

PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m alembic upgrade head   # = make migrate
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m alembic check          # 模型/迁移漂移
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m app.scripts.seed       # 2 岗位 + 3 简历，可反复跑
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m app.scripts.reap_tasks  # 僵尸任务，默认 dry-run
```

`PYTHONPATH=.` 与 `-X utf8` 不是风格问题：前者因为包名是 `app`，后者因为控制台是 GBK。
