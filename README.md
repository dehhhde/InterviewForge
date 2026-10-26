# InterviewForge

基于 Agent 架构的简历解析与面试题生成系统。
技术栈：FastAPI + LangGraph + DeepSeek/Qwen + PostgreSQL + pgvector + Celery + LangFuse。

## 这是什么

本仓库当前处于**学习阶段**，按 [`LEARNING_ROADMAP.md`](./LEARNING_ROADMAP.md) 自底向上逐层实现。
目录骨架已按 `project_structure.md` 建好，每个模块文件顶部的一行 docstring 标注了它的职责与**应在哪个阶段实现**。

## 环境

- Python **3.14.7**，虚拟环境位于 `.venv/`
- 依赖按 `LEARNING_ROADMAP.md` §16 逐阶段引入，当前 `pyproject.toml` 的 `dependencies` 为空是正常的
- 图片简历 OCR 不走 PaddleOCR（3.14 无分发），改用多模态 LLM —— 见 §8.4

## 快速开始

```bash
# 激活虚拟环境
.venv\Scripts\activate         # Windows PowerShell / cmd
source .venv/bin/activate      # WSL / Linux

cp .env.example .env           # 填入 DEEPSEEK_API_KEY 等
pip install openai httpx python-dotenv
python -m app.scripts.hello_llm
```

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
| API Gateway | `app/api/` `app/middleware/` `app/schemas/` | 阶段 2 |
| Service | `app/services/` | 阶段 2 建壳，逐阶段填充 |
| Agent | `app/agents/` | 阶段 7（核心） |
| Data | `app/models/` `app/repositories/` `migrations/` | 阶段 3 |
| Parse / Extract | `app/parsers/` `app/extractors/` | 阶段 5 |
| Retrieve | `app/vectorstore/` | 阶段 6 |
| Async | `app/tasks/` | 阶段 8 |
| Cross-Cutting | `app/utils/` `docker/` `docs/adr/` | 阶段 4 / 9 / 10 |

## 约定

- 分层铁律：`api -> services -> repositories`，api 不 import models，service 不写 SQL。
- 所有 LLM 调用走 `app/utils/llm_client.py`，不许在节点里直接建 client。
- 每个架构决策写一条 ADR 到 `docs/adr/`。
- 密钥只进 `.env`，行为配置只进 `configs/*.yaml`。

## 开发命令

`make dev` `make lint` `make test` `make migrate` —— 见 `Makefile`。
