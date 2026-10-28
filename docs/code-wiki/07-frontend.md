# 07 · 前端控制台（Vue 3）

`frontend/` 是**独立构建**的 Vue 3 + TypeScript + Vite + Element Plus 工程。后端只读
`frontend/dist/`，运行期不引 CDN。ADR-0016 取代了早期「零构建静态页」的形态（ADR-0012）。

## 1. 工程与命令

```bash
cd frontend
npm run dev        # 127.0.0.1:3000，/api /docs /redoc /openapi.json 代理到 8000
npm run typecheck  # vue-tsc --noEmit，构建前必过
npm run build      # 产出 dist/
```

- 路径别名 `@ → src/`。
- `node_modules/` 与 `dist/` 不进库；改 UI 必须改 `src/` 再重新构建。
- 依赖：`vue`、`vue-router`、`pinia`、`element-plus`、`echarts`、`marked`（Markdown 渲染）。
- 构建产物 `frontend/dist/` 由 FastAPI 在 `/` 同源挂载（见 [06-api-and-tasks.md](./06-api-and-tasks.md)）。

## 2. 目录结构

```
frontend/src/
├── main.ts                 # createApp + pinia + router + Element Plus + 样式
├── App.vue                 # 根布局
├── router/index.ts         # 路由表（history 模式）
├── stores/app.ts           # 全局状态：健康检查、主题
├── api/
│   ├── http.ts             # fetch 封装、ApiError、失败提示
│   ├── index.ts            # 每个端点的类型化函数
│   └── types.ts            # 后端 DTO 的 TS 镜像
├── utils/
│   ├── task.ts             # createTaskPoller：轮询任务、可取消
│   ├── markdown.ts         # 报告 → Markdown 导出
│   ├── format.ts           # 标签映射、时间、id 缩写、issuesByIndex
│   └── version.ts          # 构建版本号（vite define 注入）
├── layouts/ConsoleLayout.vue
├── components/             # PageHeader / StatCard / StatusPill / ChartPanel /
│                           # TaskTimeline / ReportViewer / QuestionCard /
│                           # ResumeProfile / JobFormDrawer / EmptyBlock / 各类 Tag
└── views/
    ├── DashboardView.vue   # 总览：统计卡、流水线示意、最近报告
    ├── GenerateView.vue    # 选简历 + 岗位 → 发起生成 → 轮询 → 就地展示报告
    ├── ReportsView.vue     # 报告列表 + 筛选
    ├── ReportDetailView.vue# 单份报告：计划配额、批注、打印/导出
    ├── ResumesView.vue     # 简历上传/列表/详情/改名/删除
    ├── JobsView.vue        # 岗位列表/新建/编辑/导入 JD/图片识别
    └── SystemView.vue      # 健康检查、端点清单、运行环境
```

## 3. 路由

| 路径 | 页面 | 标题 | 说明 |
| --- | --- | --- | --- |
| `/` | 重定向 `/dashboard` | — | |
| `/dashboard` | `DashboardView` | 总览 | 简历/岗位/报告数量、运行中报告数、难度分布、最近报告 |
| `/generate` | `GenerateView` | 生成面试题 | 生成工作台 |
| `/reports` | `ReportsView` | 报告中心 | 报告列表 |
| `/reports/:id` | `ReportDetailView` | 报告详情 | 报告详情；`id` 变化会重新 load；侧栏高亮 `/reports` |
| `/resumes` | `ResumesView` | 简历库 | 简历管理 |
| `/jobs` | `JobsView` | 岗位库 | 岗位管理 |
| `/system` | `SystemView` | 系统状态 | 健康检查与端点速查 |
| `/:pathMatch(.*)*` | `NotFoundView` | 页面不存在 | SPA 内兜底 |

外层是 `ConsoleLayout`（`SideNav` 侧边导航 + `HealthBadge` 健康徽标 + 内容区）。
路由使用 `createWebHistory`，`afterEach` 统一设置 `document.title`。

## 4. API 客户端

[`src/api/http.ts`](file:///d:/code/InterviewForge/frontend/src/api/http.ts)

- 统一 `fetch` 封装，空响应体返回 `null`（204）。
- 非 2xx 解析后端 `{"error": {code, message, details}}`，抛 `ApiFailure`
  （`Error` 扩展，带 `status / code / details / network`）。
- `isApiFailure(error)` 用于窄化；`writeFailureHint(...)` 给「写请求网络失败」统一提示：
  **先刷新确认，别直接重试**（避免重复建岗/重复生成）。
- `apiBase()` 在 dev 下走 Vite 代理，生产同源。

[`src/api/index.ts`](file:///d:/code/InterviewForge/frontend/src/api/index.ts) 按资源分组导出：

| 分组 | 函数 |
| --- | --- |
| 健康 | `getHealth()` |
| 简历 | `uploadResume(file)`、`listResumes(limit, offset)`、`getResume(id)`、`renameResume(id, name)`、`deleteResume(id, force)` |
| 岗位 | `listJobs`、`getJob`、`createJob`、`updateJob`、`deleteJob`、`uploadJob(form)`、`inspectJobImage(file)` |
| 报告 | `startGeneration(resumeId, jobId)`、`listInterviews`、`getInterview(id)` |
| 任务 | `getTask(taskId)` |

`src/api/types.ts` 是后端 DTO 的镜像（`Resume`、`Job`、`JobDraft`、`Interview`、`Task`、
`GenerateReceipt`、`ReviewIssue` 等），字段名与 JSON 完全一致，不做驼峰转换。

## 5. 任务轮询

[`src/utils/task.ts`](file:///d:/code/InterviewForge/frontend/src/utils/task.ts)

```ts
const poller = createTaskPoller({ interval: 2000, onError })
const finalTask = await poller.run(taskId, (current) => { ... })
poller.stop()
```

- 页面上**每 2 秒**轮询 `GET /api/v1/tasks/{id}`（`SystemView` 亦如此说明）。
- 轮询可取消：离开页面（`onBeforeUnmount`）或点「停止跟踪」时 `stop()`；
  `run()` 在停止后返回 `null`，调用方把 `stopped=true` 与真实失败区分开。
- `completed` / `failed` 都算终态，停止轮询。

## 6. 关键页面行为

### 6.1 生成工作台 `GenerateView`

1. 并行加载简历与岗位（各 200 条），默认选中第一项；
2. `POST /interviews/generate` → 拿到 `{interview_id, task_id}`；
3. 轮询任务，展示 `TaskTimeline`（每个节点的步骤与耗时）；
4. 成功后在当前页拉 `GET /interviews/{id}` 就地渲染 `ReportViewer`；
5. 失败按 `task.error.message` 展示，不把 500 当成功。

### 6.2 报告详情 `ReportDetailView`

- 报告头显示**生成当时的简历/岗位快照**（副标题明确写「快照」）。
- 展示 `plan.difficulty_quotas` 与 `plan.items`（计划做了什么）。
- 评审批注按题目序号聚合（`issuesByIndex`）。
- **打印**（`window.print()`）：标准答案强制展开、作答框不打印（CSS `@media print`）。
- **导出 Markdown**（`utils/markdown.ts`）：把报告头、计划、题目、答案、批注转成 md 并下载。

### 6.3 报告查看器 `ReportViewer`

- 空报告按 `status` 分支解释，而不是按「批注有无」猜：
  - `running` → 「仍在生成中，题目由评审收尾一次性落库」；
  - `completed` → 「收尾拿到空批次，请看批注与任务错误」；
  - 其他（`aborted`）→ 「后台任务没跑到收尾」。
- 难度/来源/关键词三重筛选；筛选不改变题目序号（作答草稿键仍用原始 `index`）。
- 顶部汇总条：题数、难度分布、来源分布、批注数。

### 6.4 题目卡 `QuestionCard`

- 展示题干、考察点、依据出处（`source=generic` 且无引用时隐藏出处框）。
- **用户作答草稿只进 `localStorage`**，键：
  `if-console-answer:<report_id>:<index>`。切换报告/题目时重新载入；清空即删除键。
- 草稿**不提交服务端**（没有作答持久化接口）。

### 6.5 简历 `ResumesView`

- 上传（PDF/DOCX/图片，图片是否可选取决于后端配置）→ 轮询 `resume_parsing` 任务；
- 列表支持关键词筛选（展示名/文件名/解析姓名）；
- 详情抽屉展示 `ResumeProfile`（解析结果可视化）；
- 改名只改 `name`；删除遇到 409 弹二次确认，确认后带 `?force=true` 再删。

### 6.6 岗位 `JobsView` + `JobFormDrawer`

- 新建/编辑共用一个抽屉表单；`title/company` 必填约束、`jd_text ≥ 5` 字；
- 技能多选，常用技能候选内置 `COMMON_SKILLS`，也可自由输入；
- 409（自然键冲突）时表单展示冲突提示，可跳转编辑既有岗位；
- 「导入 JD」支持 txt/md/pdf/docx；
- 「图片识别」→ `POST /jobs/inspect-image` 得到草稿 → 打开抽屉预填，**确认后才创建**；
  草稿的 `missing_required` 与 `notes` 会提示人补齐。

### 6.7 系统页 `SystemView`

- 调 `/api/v1/health` 并测延迟，展示 `app/env`；
- 列出全部端点与用途（与本文档端点表同源）；
- dev 下 `/docs` 按钮指向 `http://<host>:8000/docs`（不走 Vite 的 `/api` 代理）。

## 7. 前端约束（必须守）

1. **页面只做展示与轮询**，一切判断留在服务端；不要在浏览器里重算匹配/评分。
2. 文本一律走 Vue 插值（默认转义）；**不引入 `v-html`**。
3. 报告头是快照，页面必须说明它是历史值，不能暗示「跟随源表更新」。
4. 空报告按 `status` 分支解释，不按批注有无猜成因。
5. 写请求网络失败提示「先刷新确认，别直接重试」；删除类 409 必须二次确认。
6. 作答草稿只进 `localStorage`，不上传。

## 8. 与后端的契约

- 开发期：Vite 代理 `/api`、`/docs`、`/redoc`、`/openapi.json` 到 `127.0.0.1:8000`。
- 生产期：同源，`/` 返回 `index.html`，`/assets/*` 是静态文件，`/api/v1/*` 是接口。
- 前端依赖的 DTO 形状由 `app/schemas/` 定义；改后端字段必须同步 `src/api/types.ts`，
  并跑 `npm run typecheck`。
