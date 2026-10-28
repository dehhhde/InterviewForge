# InterviewForge Code Wiki

本目录是 **InterviewForge** 仓库的结构化代码文档（Code Wiki）。目标读者是第一次接手
这个仓库的工程师：先读本页建立全景，再按需进入各专题。

> 项目定位：**简历解析 → 岗位匹配 → 面试题生成**的 LangGraph 工作流，附带
> FastAPI 后端、SQLite 持久化、后台任务与 Vue 3 控制台。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [01-architecture.md](./01-architecture.md) | 分层架构、启动生命周期、配置体系、依赖注入、一次请求的完整链路 |
| [02-workflow.md](./02-workflow.md) | LangGraph 主图、7 个业务节点 + 扇出节点、State 通道与节点契约 |
| [03-extraction.md](./03-extraction.md) | 文件解析器、四个抽取器、证据核对（无证据即丢弃） |
| [04-domain-services.md](./04-domain-services.md) | 领域服务：简历、岗位、匹配、画像、计划、出题、评审、报告、任务 |
| [05-persistence.md](./05-persistence.md) | ORM 模型、仓储层、事务边界、迁移与数据文件 |
| [06-api-and-tasks.md](./06-api-and-tasks.md) | REST API、错误处理、后台任务、中间件、鉴权现状 |
| [07-frontend.md](./07-frontend.md) | Vue 3 控制台：路由、页面、组件、API 客户端、任务轮询 |
| [08-configuration.md](./08-configuration.md) | 配置项、环境变量、YAML 行为参数、LLM 客户端与成本护栏 |
| [09-run-and-test.md](./09-run-and-test.md) | 安装、运行、迁移、种子数据、测试、评测脚本、排障 |
| [10-dependency-map.md](./10-dependency-map.md) | 模块依赖图、跨层调用规则、关键文件速查表 |

## 技术栈

| 层 | 技术 |
| --- | --- |
| Web 框架 | FastAPI（工厂模式 `create_app()`）、Starlette 静态挂载 |
| 工作流 | LangGraph `StateGraph` + `AsyncSqliteSaver` checkpoint + `Send` 扇出 |
| 持久化 | SQLAlchemy 2.x async + SQLite（aiosqlite）+ Alembic |
| 配置 | pydantic-settings + `.env`（密钥）+ `configs/<env>.yaml`（行为参数） |
| LLM | DeepSeek OpenAI 兼容接口，经 `LLMClient` 统一封装（tenacity 重试 + 并发信号量 + 用量统计） |
| 抽取 | 正则/词典优先，Jinja2 prompt + LLM 兜底，全部结果做「原文证据」核对 |
| 前端 | Vue 3 + TypeScript + Vite + Element Plus，构建产物 `frontend/dist/` 由后端同源挂载 |
| 测试 | pytest（全离线）、ruff、mypy、vue-tsc |

## 一分钟理解数据流

```
上传简历 PDF/DOCX/图片
   │  POST /api/v1/resumes/upload（解析端点）
   ▼
ResumeParser ──► 纯文本 raw_text
   │  BackgroundTasks：run_resume_parsing
   ▼
LangGraph：parse → validate ─(有问题且未超预算)→ parse（回退边）
                     │
                     ▼
              match → profile → plan → generate ──Send──► generate_item ×N
                                                        │
                                                        ▼
                                                      review ──► 落库 interview + question
   │
   ▼
GET /api/v1/interviews/{id} 读取报告（题目、引用、批注）
```

## 目录速览

```
InterviewForge/
├── app/
│   ├── main.py                 # FastAPI 工厂 + lifespan + SPA 静态挂载
│   ├── config.py               # pydantic-settings：.env + configs/*.yaml
│   ├── dependencies.py         # 引擎单例、会话、LLM 客户端、SessionDep/LLMDep
│   ├── agents/                 # state / workflow / nodes（7 节点 + generate_item）
│   ├── services/               # 领域服务（不写 SQL）
│   ├── repositories/           # 唯一写 SQL 的层（只 flush）
│   ├── models/                 # SQLAlchemy ORM（无 relationship）
│   ├── schemas/                # Pydantic 出入参
│   ├── api/v1/                 # REST 路由
│   ├── tasks/                  # BackgroundTasks 任务边界
│   ├── extractors/             # 简历字段抽取 + 证据核对
│   ├── parsers/                # PDF / DOCX / 图片 → 文本
│   ├── prompts/                # Jinja2 模板 + 注册表
│   ├── utils/                  # db / llm_client / decorators / text / validators …
│   ├── vectorstore/            # sqlite-vec 检索、岗位索引、题目去重
│   └── scripts/                # seed、评测、探针、live_check（无 middleware/，T-312 已删）
├── frontend/                   # Vue 3 源码；dist/ 是后端唯一读取的产物
├── migrations/                 # Alembic 迁移
├── configs/                    # development / testing / production.yaml
├── tests/                      # pytest（离线）
├── scripts/                    # 端到端评测、导出报告、探针
├── docs/                       # 计划书、ADR、TODO、本 Wiki
├── legacy-console/             # ADR-0016 之前的零构建静态页（历史留档）
├── docker/                     # 容器化配置
├── reports/                    # 评测/报告输出目录
├── start.sh / start.bat        # Git Bash / cmd 启动脚本
├── requirements.txt / requirements-dev.txt / pyproject.toml / alembic.ini
├── .env.example                # 环境变量样例（复制成 .env 后填密钥）
└── data/                       # 运行期数据：interview_forge.db、checkpoints.db、uploads/
```

## 状态与里程碑

M0–M6 已全部落地（契约层、解析链、校验、持久化、M4 确定性匹配、M5 出题/评审、
M6 评测与后台化）。M6 之后补充了 `frontend/` 控制台、岗位/简历写入侧 CRUD、
岗位图片录入。关键决策见 `docs/adr/0007`–`0016`。

## 阅读顺序建议

1. 本页 + [01-architecture.md](./01-architecture.md)：知道东西放在哪一层。
2. [02-workflow.md](./02-workflow.md)：理解 `AgentState` 与节点契约，这是改业务逻辑的主战场。
3. [04-domain-services.md](./04-domain-services.md) / [05-persistence.md](./05-persistence.md)：理解数据怎么落库。
4. [06-api-and-tasks.md](./06-api-and-tasks.md) + [07-frontend.md](./07-frontend.md)：理解入口与界面。
5. [09-run-and-test.md](./09-run-and-test.md)：把项目跑起来并验证改动。
