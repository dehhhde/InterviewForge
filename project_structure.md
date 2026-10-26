# InterviewForge — 项目架构与包结构

## 项目概述

**InterviewForge** 是一个基于 Agent 架构的简历解析与面试题生成系统。用户上传简历后，Agent 自动完成简历解析、信息结构化、岗位匹配、能力画像构建、面试题生成、质量审查等全流程，最终交付结构化的面试题清单。

**技术栈核心：** FastAPI + LangGraph + Qwen/DeepSeek + PostgreSQL + pgvector + Celery + LangFuse

---

## 系统架构图

![InterviewForge 架构图](architecture.png)

---

## 包结构

```
interview_forge/
│
├── app/
│   ├── main.py                          # FastAPI 应用入口 / 生命周期管理
│   ├── config.py                        # 全局配置管理 (Pydantic Settings)
│   ├── dependencies.py                  # 依赖注入 / 共享依赖 (DB session, Redis, etc.)
│   │
│   ├── middleware/                      # 中间件层
│   │   ├── __init__.py
│   │   ├── auth.py                      # JWT 鉴权中间件
│   │   ├── logging.py                   # 请求/响应日志中间件
│   │   └── rate_limit.py                # 接口限流中间件
│   │
│   ├── api/                             # API 路由层 (RESTful)
│   │   ├── __init__.py
│   │   └── v1/                          # API v1 版本管理
│   │       ├── __init__.py
│   │       ├── resumes.py               # POST /upload, GET /{id}, GET /list
│   │       ├── interviews.py            # POST /generate, GET /{id}, GET /list
│   │       ├── jobs.py                  # POST /upload-jd, GET /{id}, POST /match
│   │       ├── candidates.py            # GET /profile, GET /comparison
│   │       └── health.py                # GET /health, GET /metrics
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
│   ├── vectorstore/                     # 向量检索模块
│   │   ├── __init__.py
│   │   ├── client.py                    # pgvector 客户端封装
│   │   ├── job_indexer.py               # JD 向量索引器
│   │   └── question_dedup.py            # 题目相似度去重
│   │
│   ├── tasks/                           # Celery 异步任务
│   │   ├── __init__.py
│   │   ├── celery_app.py                # Celery 应用配置
│   │   ├── resume_parsing.py            # 简历解析异步任务
│   │   └── question_generation.py       # 面试题生成异步任务
│   │
│   ├── utils/                           # 公共工具
│   │   ├── __init__.py
│   │   ├── file_handler.py              # 文件上传/下载/临时存储
│   │   ├── llm_client.py                # LLM API 客户端 (统一接口)
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
├── docker/                              # Docker 部署
│   ├── Dockerfile                       # 应用容器
│   ├── Dockerfile.worker                # Worker 容器
│   └── docker-compose.yaml              # 本地一键启动
│
├── requirements.txt                     # 生产依赖
├── requirements-dev.txt                 # 开发依赖
├── .env.example                         # 环境变量模板
├── README.md                            # 项目说明
├── Makefile                             # 开发命令别名
└── pyproject.toml                       # 项目元数据 (可选)
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
| **检索层** | `vectorstore/` | 向量检索 (pgvector)，支撑 JD 匹配与题目去重 |
| **异步层** | `tasks/` | Celery 异步任务，处理耗时的解析/生成操作 |
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
    │       ├──▶ Match Node → VectorStore (pgvector) + LLM
    │       ├──▶ Profile Node → Aggregation + Scoring
    │       ├──▶ Plan Node → DAG Planning + Type Routing
    │       ├──▶ Generate Node → Prompt Templates + Few-shot
    │       └──▶ Review Node → Quality Gate + Deduplication
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
| Agent 编排 | LangGraph | 状态机工作流 / 多 Agent 协作 |
| 大模型 | Qwen / DeepSeek / Claude | 简历理解 / 题目生成 / 质量审查 |
| 关系数据库 | PostgreSQL | 业务数据持久化 |
| 向量检索 | pgvector | JD 匹配 / 题目去重 / 相似召回 |
| 文档解析 | PyMuPDF / python-docx | PDF / Word 简历解析 |
| 异步任务 | Celery + Redis | 耗时任务异步执行 |
| 可观测性 | LangFuse / OpenTelemetry | 链路追踪 / 调试 / 成本监控 |
| 容器化 | Docker + K8s | 服务部署 / 弹性伸缩 |
