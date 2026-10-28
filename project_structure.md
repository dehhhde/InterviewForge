# InterviewForge — 项目架构与包结构

> **这份文件是初始设计快照。** 骨架按它建了，实现却按 `docs/LANGGRAPH_BUILD_PLAN.md`
> 的里程碑（M0–M6）走，走的过程中换掉了一批基建选型（见下方"技术选型速查"里已更正的那几行、
> `docs/adr/0007` 与 AGENTS.md §8）。看结构认目录可以用它，看"这个项目实际怎么跑的"请以
> `AGENTS.md` 与 `docs/adr/` 为准。

## 项目概述

**InterviewForge** 是一个基于 Agent 架构的简历解析与面试题生成系统。用户上传简历后，Agent 自动完成简历解析、信息结构化、岗位匹配、能力画像构建、面试题生成、质量审查等全流程，最终交付结构化的面试题清单。

**技术栈核心：** FastAPI + LangGraph + DeepSeek（OpenAI 兼容端点）+ **SQLite + sqlite-vec** +
**`BackgroundTasks`**（进程内协程，无 Celery/Redis）+ **`SqliteSaver`** checkpoint
（独立落在 `data/checkpoints.db`）。没有 LangFuse，链路与用量靠 `trace` 通道与 `task` 行。

---

## 系统架构图

这里原先挂的是 `![InterviewForge 架构图](architecture.png)`，而**仓库里没有 `architecture.png`**
（实测 2026-10-27，所以它一直是个坏链接）。实际可读的图是两份 HTML：
`docs/interviewforge-langgraph-workflow.html`（7 节点流程）与
`docs/diagrams/interviewforge-langgraph-execution-graph.v2.html`（含扇出与回退边的执行图）。

---

## 包结构

```
interview_forge/
│
├── app/
│   ├── main.py                          # FastAPI 应用入口 / 生命周期管理
│   ├── config.py                        # 全局配置管理 (Pydantic Settings)
│   ├── dependencies.py                  # 依赖注入 / 共享依赖 (DB session、LLM 客户端、分页参数)
│   │
│   ├── api/                             # API 路由层 (RESTful)
│   │   ├── __init__.py
│   │   └── v1/                          # API v1 版本管理
│   │       ├── __init__.py
│   │       ├── resumes.py               # POST /upload, GET /{id}, GET /list
│   │       ├── interviews.py            # POST /generate, GET /{id}, GET /list, GET /export.md
│   │       ├── jobs.py                  # POST /upload-jd, POST /inspect-image, CRUD, GET /list
│   │       ├── tasks.py                 # GET /{id}（进度 + 报告可用性派生字段）
│   │       └── health.py                # GET /health（/metrics 仍欠，T-310）
│   │
│   ├── services/                        # 业务逻辑层 (Use Cases)
│   │   ├── __init__.py
│   │   ├── resume_service.py            # 简历上传/解析/存储/查询
│   │   ├── match_service.py             # JD匹配/能力缺口分析
│   │   ├── question_service.py          # 面试题生成/分页/导出
│   │   ├── review_service.py            # 质量审查/幻觉检测/去重
│   │   └── profile_service.py           # 候选人画像/评分/对比
│   │
│   ├── agents/                          # Agent 编排层 (LangGraph StateMachine)
│   │   ├── __init__.py
│   │   ├── workflow.py                  # 主工作流图定义 (StateGraph)
│   │   ├── state.py                     # Agent 状态定义 (TypedDict)
│   │   └── nodes/                       # 各 Agent 节点实现
│   │       ├── __init__.py
│   │       ├── parse_node.py            # 简历解析节点 (调用 Parser + LLM)
│   │       ├── validate_node.py         # 信息校验节点 (规则+LLM反思)
│   │       ├── match_node.py            # 岗位匹配节点 (RAG检索+对比)
│   │       ├── profile_node.py          # 能力画像节点 (聚合+评分)
│   │       ├── plan_node.py             # 题目规划节点 (DAG规划+题型路由)
│   │       ├── generate_node.py         # 题目生成节点 (Prompt+Few-shot)
│   │       └── review_node.py           # 质量审查节点 (Reviewer Agent)
│   │
│   ├── models/                          # 数据库模型层 (SQLAlchemy 2.0)
│   │   ├── __init__.py
│   │   ├── base.py                      # 模型基类 (DeclarativeBase)
│   │   ├── user.py                      # 用户模型
│   │   ├── resume.py                    # 简历模型
│   │   ├── job.py                       # 岗位JD模型
│   │   ├── question.py                  # 面试题模型
│   │   └── interview.py                 # 面试报告模型
│   │
│   ├── schemas/                         # Pydantic 请求/响应模型
│   │   ├── __init__.py
│   │   ├── resume.py                    # 简历 DTO
│   │   ├── job.py                       # 岗位 DTO
│   │   ├── question.py                  # 题目 DTO
│   │   └── interview.py                 # 面试报告 DTO
│   │
│   ├── repositories/                    # 数据访问层 (Repository Pattern)
│   │   ├── __init__.py
│   │   ├── base.py                      # 基础 Repository
│   │   ├── resume_repository.py         # 简历 CRUD
│   │   ├── job_repository.py            # 岗位 CRUD
│   │   ├── question_repository.py       # 题目 CRUD
│   │   └── interview_repository.py      # 面试报告 CRUD
│   │
│   ├── parsers/                         # 文档解析器 (策略模式)
│   │   ├── __init__.py
│   │   ├── base.py                      # 解析器抽象基类
│   │   ├── pdf_parser.py                # PDF 简历解析 (PyMuPDF)
│   │   ├── docx_parser.py               # Word 简历解析 (python-docx)
│   │   └── image_parser.py              # 图片简历解析 (OCR)
│   │
│   ├── extractors/                      # 信息抽取模块
│   │   ├── __init__.py
│   │   ├── ner_extractor.py             # NER 实体抽取 (姓名/公司/技能等)
│   │   ├── skill_extractor.py           # 技术栈/技能抽取
│   │   ├── project_extractor.py         # 项目经历抽取
│   │   └── timeline_extractor.py        # 工作时间线抽取
│   │
│   ├── prompts/                          # Prompt 模板（原清单漏了这一整个目录）
│   │   ├── registry.py                   # `get_prompt(node, version)`；版本目录 = 归因单位
│   │   └── templates/v1/                 # `<node>.{system,user}.j2`；新增一版=复制一个目录
│   │
│   ├── scripts/                          # 手动脚本（不进 pytest，coverage 已排除）
│   │   ├── seed.py                       # 灌 2 岗位 + 3 简历，可反复跑
│   │   ├── hello_llm.py                  # LLM 连通性自检（1 次调用）
│   │   ├── live_check.py                 # 真简历 parse+validate + 录制回放 fixture
│   │   ├── reap_tasks.py                 # 僵尸任务回收（默认 dry-run，T-015）
│   │   └── eval_*.py                     # 评测脚本（三指标目前还没有真数字）
│   │
│   ├── vectorstore/                     # 向量检索模块（**未接线**：`retriever.py` 是
│   │   │                               # `SkillRetriever` 协议 + `NullRetriever`，
│   │   │                               # embedding 选型未决 = TASK_PLAN D-3）
│   │   ├── __init__.py
│   │   ├── client.py                    # 向量库客户端封装（原写 pgvector，实际会是 sqlite-vec）
│   │   ├── job_indexer.py               # JD 向量索引器
│   │   ├── question_dedup.py            # 题目相似度去重
│   │   └── retriever.py                 # `SkillRetriever` / `NullRetriever`
│   │
│   ├── tasks/                           # 后台任务：FastAPI `BackgroundTasks` 的协程
│   │   │                               # （ADR-0011。**不是 Celery**，没有 broker 也没有 worker）
│   │   ├── __init__.py
│   │   ├── celery_app.py                # 已被 `BackgroundTasks` 取代，"不实现"（ADR-0011）
│   │   ├── resume_parsing.py            # 简历解析后台任务
│   │   └── question_generation.py       # 面试题生成后台任务
│   │
│   ├── utils/                           # 公共工具
│   │   ├── __init__.py
│   │   ├── file_handler.py              # 上传读取：大小上限 + 魔数校验（ADR-0017）
│   │   ├── llm_client.py                # LLM API 客户端 (统一接口)
│   │   ├── logging.py                   # 日志装配：`settings.log_level` 唯一的落地点
│   │   ├── validators.py                # 数据校验工具
│   │   └── formatters.py                # 输出格式化
│   │
│   └── exceptions/                      # 异常处理
│       ├── __init__.py
│       ├── base.py                      # 基础异常定义
│       └── handlers.py                  # 全局异常处理器
│
├── tests/                               # 测试
│   ├── __init__.py
│   ├── conftest.py                      # 测试 fixtures
│   ├── test_api/                        # API 接口测试
│   ├── test_services/                   # 业务逻辑测试
│   └── test_agents/                     # Agent 节点测试
│
├── migrations/                          # 数据库迁移 (Alembic)
│   ├── env.py
│   └── versions/
│
├── configs/                             # 环境配置
│   ├── development.yaml
│   ├── production.yaml
│   └── testing.yaml
│
├── docker/                              # 只有一个 README.md：**没有容器编排**，
│                                        # 部署形态是单进程 `bash start.sh`（见 docker/README.md）
│
├── frontend/                            # 同源控制台（ADR-0016）：Vue 3 + TS + Vite + Element Plus
│   ├── src/                             # 源码；后端只读下面的构建产物
│   └── dist/                            # `npm run build` 产出，挂在 `/`（不进版本库）
│
├── requirements.txt                     # 生产依赖（实际清单在这里，不在 pyproject 的 dependencies）
├── requirements-dev.txt                 # 开发依赖
├── .env.example                         # 环境变量模板
├── README.md                            # 项目说明
├── Makefile                             # 命令速查表（本机没有 make/uv，跑不了它）
└── pyproject.toml                       # 工具配置（ruff / mypy / pytest / coverage）
```

---

## 分层说明

| 层级 | 目录 | 职责 |
|------|------|------|
| **API 层** | `api/` | 接收 HTTP 请求，参数校验，委托 Service 层处理 |
| **业务层** | `services/` | 业务用例实现，协调多个 Agent/工具完成业务逻辑 |
| **Agent 层** | `agents/` | LangGraph 状态机构编排，定义解析→校验→匹配→画像→规划→生成→审查的完整工作流 |
| **数据层** | `models/` + `repositories/` | 数据库模型定义 + CRUD 数据访问封装 |
| **解析层** | `parsers/` + `extractors/` | 文档解析 (PDF/Word/Image) + 结构化信息抽取 |
| **检索层** | `vectorstore/` | 向量检索的**协议位**（`SkillRetriever`/`NullRetriever`）。选型是 sqlite-vec 而非 pgvector，且 embedding 未决（D-3），所以现在不参与产出任何判断 |
| **异步层** | `tasks/` | 耗时的解析/生成走 FastAPI `BackgroundTasks`（同进程协程 + `task` 表记进度），**不是 Celery** |
| **工具层** | `utils/` | 跨层公共工具 (文件处理、LLM客户端、校验器等) |

---

## 核心数据流

```
用户请求 (FastAPI)
    │
    ▼
API Router (参数校验)
    │
    ▼
Service Layer (业务编排)
    │
    ├──▶ Agent Workflow (LangGraph StateMachine)
    │       │
    │       ├──▶ Parse Node → Parsers → Extractors → LLM
    │       ├──▶ Validate Node → Rules + LLM Reflection
    │       ├──▶ Match Node → 技能词典判等 + LLM 补判（向量检索位现在是 NullRetriever）
    │       ├──▶ Profile Node → Aggregation + Scoring（五维打分在代码里，模型只写叙述）
    │       ├──▶ Plan Node → 配额分配（最大余数法，不调模型）
    │       ├──▶ Generate Node → Prompt Templates + Send 扇出
    │       └──▶ Review Node → 纯批注（ADR-0015：不再拦门）→ 落库即 completed
    │
    ▼
Repository Layer (持久化)
    │
    ▼
Response (JSON)
```

---

## 技术选型速查

| 组件 | 选型 | 用途 |
|------|------|------|
| 后端框架 | FastAPI | API 服务 / 异步请求处理 |
| Agent 编排 | LangGraph | 状态机工作流 / 扇出与回退边 |
| 大模型 | **DeepSeek**（OpenAI 兼容端点，四个槽位：extract / generate / review / vision） | 简历兜底抽取 / 题目生成 / 批注 / 图片简历 OCR |
| 关系数据库 | **SQLite**（`data/interview_forge.db`，WAL；异步驱动 aiosqlite） | 业务数据持久化 |
| 向量检索 | **sqlite-vec**（选型未决，当前 `NullRetriever`） | JD 匹配 / 题目去重 / 相似召回 |
| 会话状态 | **`SqliteSaver`**（独立文件 `data/checkpoints.db`） | LangGraph checkpoint，可断点重放 |
| 文档解析 | PyMuPDF / python-docx | PDF / Word 简历解析 |
| 异步任务 | **FastAPI `BackgroundTasks`**（进程内协程，无 broker、无 worker） | 耗时任务异步执行 + `task` 表记进度 |
| 可观测性 | **`trace` 通道 + `task` 行 + `logging`（`app/utils/logging.py`）** | 链路追踪 / 调试 / 成本统计（没有 LangFuse） |
| 前端 | **Vue 3 + TS + Vite + Element Plus**，零运行时依赖，构建产物挂 `/`（ADR-0016） | 控制台 |
| 容器化 | **无**。单进程 `bash start.sh`，理由见 `docker/README.md` | — |
