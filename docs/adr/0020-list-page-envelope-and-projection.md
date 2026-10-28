# ADR-0020：列表的形状——`Page{items,total}` 信封 + 每端点固定投影

日期：2026-10-28
状态：已决定
相关：`app/schemas/common.py`（`Page`）、`app/schemas/{resume,job,interview}.py`
（`ResumeFields` / `ResumeListItem` / `JobFields` / `JobListItem` / `InterviewHeadFields` /
`InterviewListItem`）、`app/repositories/base.py`（`count()`、`keyword_filter()`、`contains()`）、
`app/services/{resume,job,interview}_service.py`（`list_*`）、`frontend/src/api/{index,types}.ts`、
ADR-0007（repository 只 flush、`column_names()` 白名单）、ADR-0013（报告头快照列，本 ADR 收口它
关于"列表响应变大"的那条代价）、ADR-0016（Vue 控制台）、`docs/TASK_PLAN.md` T-301 / T-303 / T-307

## 背景

三个列表端点（`GET /resumes/list`、`GET /jobs`、`GET /interviews/list`）直接把**详情 DTO**
当列表行返回：每行带 `parsed` 整坨 JSON、带 `jd_text` 全文、带 `questions` 全部题干。
代价是可算的——一份简历的 `parsed` 比它的标识列大一到两个数量级，而列表页只用到三四个数。

后果在前端那一侧显形：`frontend/src/api/index.ts` 返回裸数组，页面拉满 200 条然后在浏览器里
`filter`。裸数组只能回答"这次给了几条"，回答不了"这些条件下一共多少条"，所以分页控件永远做不出来；
`BaseRepository.count()`（`app/repositories/base.py:65`）写好了却全仓零调用者。

`app/schemas/common.py` 原本是一行 docstring，宣传过一个从未实现的 `ApiResponse[T]`
（BACKLOG P3-2 把它记在空壳模块里）。本 ADR 是它第一个真正的用户。

## 决策

| 环节 | 选择 | 被否决的方案 |
|---|---|---|
| 一页的形状 | `Page[DataT]{items,total}` 作为**响应体**，`total` 是**过滤后**的总行数 | `{total}` 走响应头：前端要么去读 header 要么改 JSON 形状，而 OpenAPI 表达不了它；类型系统里"这批数据"和"这批之外还有多少"就该是一个整体 |
| 投影机制 | **每个端点一份固定的精简模型**（`ResumeListItem` / `JobListItem` / `InterviewListItem`），正文列不在其上 | T-301 卡片提的 `?fields=` 动态投影：列名白名单要再造一遍（ADR-0007 的 `column_names()` 只管 `order_by`），OpenAPI 退化成 `dict`，前端类型得跟着查询参数变 |
| 列的出处 | 共享列声明在 `*Fields` / `*HeadFields` 基类里，列表模型与详情模型各继承一次 | 手写两份字段清单：改一列要记得改两处，忘了就是列表与详情对同一列各说各话 |
| 派生值 | 读时从正文列现算，不落库：`overview`（技能数/技能名/工作年限/经历段数）、`jd_excerpt`（前 N 字） | 给 `resume`/`job` 加冗余列：那是第二份真相，而 `parsed` 会被重解析改写；而且 `test_list_projection_api.py` 得跟着搬 |
| 正文怎么拿 | 详情端点原样保留（`GET /resumes/{id}` 等），抽屉/详情页按需拉（T-307） | 列表继续带正文，"以后再说"：这正是本 ADR 要解决的问题本身 |
| 关键词筛选 | `q`（≤64 字）→ `keyword_filter(model, q, 白名单列)`：标识列 + 报告的 `id`；`%`/`_`/`\` 转义后 `LIKE '%…%'` | 搜 `raw_text`/`jd_text`/`parsed`：SQLite 无全文索引，整表扫，且把"搜一个人的名字"变成"搜所有提过这词的正文"。回 join 源表搜报告出身：岗位可被下线（`job_id` 是 `SET NULL`），那样会漏掉确实存在的报告（ADR-0013） |
| `total` 与 `items` 同源 | 服务层把**同一份** `filters` 交给 `repository.list()` 与 `.count()` | 两处各拼条件：页脚会显示一个和列表无关的数字 |
| 前端类型 | `ResumeListItem = Omit<Resume, 'parsed'> & {overview}` 一类派生，配合 `npm run typecheck` | 手抄一份 TS 接口：正文列被服务端摘掉后，页面读到 `undefined` 只会渲染空白，不会报错 |

## 理由

- **投影必须是类型层的既成事实，而不是"约定别读"**。`Omit<>` + `vue-tsc` 让任何还在读
  `row.parsed` / `row.jd_text` / `row.questions` 的视图直接编译失败；报告行干脆**没有**
  `questions` 这个键（不是空数组），空报告和"这一页不给正文"就分得开。
- **`count()` 的调用者只能是列表端点**：分页控件要的是"条件下总量"，不是"本页条数"。
  把它放进同一个响应体，两个数才可能来自同一次条件构造。
- **不加 `?fields=`**：动态投影需要一套"哪些列允许被投影"的白名单，而这套白名单在仓库里
  已经有了两次先例（`order_by` 的 `column_names()`、`task.error.details` 的键白名单）——第三份
  判据就意味着第三个必须同步的地方。固定投影把这件事留在类型里。

## 权衡与代价

- 每次列表多一条 `COUNT`。SQLite 单写者、这几张表都是小表，现在换得起；将来大了该上的是
  索引或估算，不是把 `total` 藏回头里。
- **岗位搜索不再覆盖 JD 正文**。旧的浏览器本地过滤能命中正文，服务端只比 `title`/`company`
  两列，所以搜索框占位与空态文案都跟着改了口（`JobsView.vue`）。这是契约变更的连带后果，
  不是 bug。
- 分页 UI 还没做：`listParams()` 默认一次取满服务端上限（`limit=200`，`le=200`），
  保住"一次看全"的现有行为。`status` 过滤（报告列表）与翻页仍是**前端本地**的，服务端没有
  `status` 参数——这条欠账记在 `docs/TODO.md`。
- 抽屉/详情页多一次请求。换来的是列表响应体积只随标识列增长。
- ADR-0013 里"`JobOut` 现在带 `jd_text`：列表响应变大"那条代价由本 ADR 收口，详情侧结论不变。

## 后续影响

- **新增列表端点照这个形状**：一页 `Page<精简模型>` + 服务层同一份 `filters` 喂 `list`/`count` +
  搜索列走 `keyword_filter` 白名单；正文一律详情端点取。别为省事复用详情模型当 `response_model`。
- 给 `Page` 加字段要三处同步：`app/schemas/common.py`、`frontend/src/api/types.ts` 的 `Page<T>`、
  `tests/test_api/test_list_filter_api.py` 的 `total` 断言。
- 分页控件落地时，`limit`/`offset` 已经在契约里，前端只需把 `DEFAULT_LIST_LIMIT` 换成页大小；
  `status` 若要走服务端，得先在 `interview_service.list_reports` 的条件里加等值过滤（不是 LIKE）。
- `app/schemas/common.py` 不再是空壳：T-312 的"空壳模块处置决议"清单要划掉它。
