# docs/adr/

架构决策记录。路线里指定了 5 篇必写：

| 编号 | 主题 | 阶段 |
|---|---|---|
| 0001 | 为什么 api 不能直接访问 models（分层） | 2 |
| 0002 | LLM 与 Embedding 模型选型 | 4 |
| 0003 | Agent 范式：Workflow vs Agent、有界回退、生成-评审循环 | 7 |
| 0004 | 检索方案：纯向量 / 混合 / rerank 的消融结论 | 6 |
| 0005 | 评测方法与基线数字 | 9 |
| 0006 | PDF 解析引擎选型（PyMuPDF vs pypdf vs pdfplumber，AGPL 代价） | 5 |
| 0007 | 持久化层：SQLite + `JSON`、uuid hex 主键、repository 不提交事务 | 3（M3，已决定） |
| 0008 | 扫描件不做本地 OCR，交给多模态 LLM（**尚未落文件**，编号已预留） | 5 |
| 0009 | M4 三层的确定性分工：分数归规则、措辞归模型、配额归算术 | 7（M4，已决定） |
| 0010 | 出题层扇出形状：`Send` + 批次覆盖、引用按来源核对、评审独立槽位（**质量门那一节已被 0015 取代**） | 7（M5，部分作废） |
| 0011 | 后台化：`BackgroundTasks` + `task` 表进度，不引入 Redis/Celery；**后台先于依赖收尾跑** ⇒ 提交事务归服务层 | 8（M6，已决定） |
| 0012 | 控制台页面：零构建静态目录 `frontend/`，`create_app()` 同源挂在 `/`（API 之后）；页面只展示与轮询（**已被 0016 取代**，同源挂载结论保留） | 8（M6 之后，已决定） |
| 0013 | 写入侧：岗位/简历 CRUD + 报告头的简历·岗位快照列；加 NOT NULL 列禁走 `batch_alter_table`（实测级联删空过题目）；撞自然键 409；删简历默认拒、`force=true` 才连报告 | 3/8（M6 之后，已决定） |
| 0014 | 图片录入岗位：`POST /jobs/inspect-image` 回**草稿**不落库，保存仍走 `POST /jobs`；字段规则优先、模型只兜 `title`/`company` 且值必须含在证据里，`min_years` 不进模型；`get_llm()` 每请求新建 | 3/5（M6 之后，已决定） |
| 0015 | 评审退回为**纯批注**：删 `review → generate` 回退边与 `should_redo`/`review_retries`/两个阈值配置，一次成卷、题目必落库、状态恒 `completed`；`aborted` 只剩"后台任务没跑到收尾"；不动表结构 | 7（M5 之后，已决定，部分取代 0010） |
| 0016 | 控制台改为 **Vue 3 + Vite + Element Plus** 构建产物：后端只读 `frontend/dist`，同源挂载 + 仅导航请求的 SPA 回退；`api/` 与带扩展名路径保持 404；开发期 Vite 3000 代理；测试用临时 dist | 8（M6 之后，已决定，取代 0012） |
| 0017 | 上传口的两道闸：`configs/*.yaml` 新增 `upload:` 段（文档 5MB / 图片 10MB）、`read_capped` 分块越线即抛 413、`sniff_kind`/`check_kind` 用魔数核对后缀（错配 422 `upload_kind_mismatch`）；`file_handler.py` 不再是空壳 | 2/4（S0 止血，已决定） |
| 0018 | 失败信息的四条通道：`task.error.details` 过键白名单 + 值判定（默认拒绝）、失败 TraceStep 由**异常携带**而不是 state 通道、收尾写库自带 `logger.exception`、解析结果无落点报 `resume_gone`；补充 0011 的"任务边界吞异常"一节 | 8（S0 止血，已决定） |
| 0019 | 考察角度归 `plan` 管：配额表每个槽位带一条 `angles`（长度 = `count`，按难度分池、超出取模），`roster` 保留并升格，只改 v2 模板；重复要在出题之前解，ADR-0015 之后批注不改变任何题 | 7（S2，已决定） |
| 0020 | 列表的形状：`Page{items,total}` 作为响应体（`total` 与 `items` 同源）+ **每端点一份固定投影模型**，正文只走详情端点；否决 `?fields=` 动态投影与 `{total}` 响应头；搜索列走 `keyword_filter` 白名单（标识列 + 报告 `id`），前端类型用 `Omit<>` 派生让 `typecheck` 当护栏 | 6/8（S3，已决定） |
| 0021 | 定向重做回路：`review → generate` 作为**显式授权（`auto_fix`）、按槽位、最多一轮（`question_batch == 1`）**的回炉边复活；上一版的题与点名它的批注装进 `GenerateJob.previous`，未点名的题原样带走并改写批次 ⇒ 批次 2 仍是一份完整卷子；不引入占比/阈值/重做次数（ADR-0015 的三条禁令照旧成立），`<previous>` 只由 v3 渲染 | 7（S4，已决定，在 0015 划定的前提内补边） |
| 0022 | `validate` 的规则问题与反思问题分家：`validate_issues` 只装规则判出来的（`parse` 重跑有可能修），模型反思走 `reflect_issues` + `errors`（`validate(reflect): …`），**不进回退计数**；`route_after_validate` 只读 `validate_issues`；改计划书 M2 的旧约定 | 2（S2，已决定） |
| 0023 | 同一对 `(resume_id, job_id)` 已有 `status='running'` 的报告时 `POST /interviews/generate` 返 409（`details` 带 `interview_id`）；终态放行；先查后插而非唯一索引（部分唯一索引的代价与僵尸 `running` 行的锁效应都写在权衡里） | 8（S2，已决定） |
| 0024 | `GET /tasks/{id}` 派生报告可用性：`report_status` / `question_count` / `degraded` / `degraded_reason`（`no_questions` / `report_gone`），只对出题任务派生，且只在 `completed` 时按 `total_questions == 0` 判降级；**不给 `interview.status` 加第四态** | 8（S3，已决定） |
| 0025 | 空壳模块处置：删 `app/api/v1/candidates.py` 与整个 `app/middleware/`；留"有主"的 `models/user.py`、`api/v1/auth.py`（T-501）、`vectorstore/*`（D-3）、已填实的 `file_handler`/`text`；鉴权与访问日志仍是空账 | 2/8（S3，已决定） |

模板：标题 / 背景 / 决策 / 理由 / 权衡与代价 / 后续影响。
