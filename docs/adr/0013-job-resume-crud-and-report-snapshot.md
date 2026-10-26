# ADR-0013：岗位/简历写接口，与报告头里的简历·岗位快照

日期：2026-10-21
状态：已决定（M6 之后的写入侧补齐）
相关：`app/api/v1/{jobs,resumes,interviews}.py`、`app/services/{job,resume}_service.py`、
`app/models/interview.py`、`migrations/versions/2fe4f8fcb3c6_*.py`、
`app/schemas/{job,resume,interview}.py`、`app/exceptions/base.py`（`ConflictError`）、
`tests/test_api/test_crud_api.py`、`frontend/app.js`、ADR-0007（持久化层）、ADR-0012（控制台）

## 背景

到这里为止写入侧是残缺的：简历只有 `POST /resumes/upload`，岗位**只有读**（`GET /jobs`），
JD 只能靠 `app/scripts/seed.py` 灌。同时报告列表 `GET /interviews/list` 每行只有
`resume_id` / `job_id` 两个 uuid，页面要显示"哪份简历 × 哪个岗位"只能再回查两张源表——
而其中岗位是**可被下线**的（`interview.job_id` 是 `ON DELETE SET NULL`），回查早晚拿不到。

三条约束决定了做法：

1. 分层铁律：`api → services → repositories`，只有 repository 写 SQL，repository 只 flush 不 commit；
2. 抽取层铁律：`raw_text` / `parsed` 是证据链的真相源，报告里每条"依据出处"指着它；
3. 这台机器的实测事实：SQLite 的 `batch_alter_table` 会 `DROP TABLE` 重建表，
   而 `migrations/env.py` 用 `build_sync_engine`（发 `PRAGMA foreign_keys=ON`）。

## 决策

### 1. 报告头存四列快照，写入发生在生成那一刻

`interview` 新增 `resume_name` / `resume_file_name` / `job_title` / `job_company`，
全部 `NOT NULL DEFAULT ''`，由 `interview_service.start_generation` 在建 `running` 头时一次写死
（简历名取 `resume.name or resume.file_name`）。列表 DTO `InterviewOut` 直接带这四列，
页面不再回查源表。

**为什么是副本而不是 join**：岗位删除后 `job_id` 变 NULL，简历改名后 `resume.name` 变了，
但"这份卷子是给谁、按哪个岗位出的"是**报告的事实**，必须跟着报告一起活着。
代价见"权衡"第一条。

### 2. 迁移形状：裸 `ALTER TABLE`，不许用 `batch_alter_table`

`migrations/versions/2fe4f8fcb3c6_*.py` 用手写的
`op.execute("ALTER TABLE interview ADD COLUMN … NOT NULL DEFAULT ''")` 加列、
`UPDATE … COALESCE((SELECT … FROM resume/job …), '')` 回填、`ALTER TABLE … DROP COLUMN` 回退。

这不是风格偏好，是**实测踩过坑之后的强制形状**：同一份迁移最初由 autogenerate 生成，
走 `op.batch_alter_table` 给 `interview` 加 NOT NULL 列；SQLite 的实现是"建临时表 → 拷数据 →
`DROP TABLE interview`"，而当时连接上 `PRAGMA foreign_keys=ON` 生效，`question.interview_id`
声明的是 `ON DELETE CASCADE`——**开发库里 30 行题目当场被级联删空**。题目是从
`data/checkpoints.db` 的终态状态里逐份还原回来的（去掉非列键 `topic`、按 `order_index` 排），
本机不是 git 仓库，没有第二条恢复路径。

配套的模型写法：四列都带 `server_default=text("''")`，否则 `alembic check` 会说模型与
迁移出来的库不一致（`text("''")` 而不是 `text("")`——空串渲染出来什么都不是）。

### 3. 岗位写入是同步的，且不建 `task` 行

`POST /jobs`（JSON）与 `POST /jobs/upload`（`.txt` / `.md` / `.pdf` / `.docx`）都直接返 201。
和简历上传的 202 + `BackgroundTasks` 不同，因为这条链路一次 LLM 调用都不花：
`required_skills` 缺失时由 `SKILL_ALIASES` 词典逐行认出（`job_service.derive_required_skills`），
**与 `match` 用的是同一本词典**——两侧各认一遍就会造出"入库的技能名和比对时认出的名字对不上"
的假缺口。人显式给了清单就以人的为准，只做小写归一与去重。
`PUT` 改了 `jd_text` 又没给清单时重新派生一遍：留着旧清单等于让 `match` 拿过期的要求比新的 JD。

JD 文件不落盘（与简历不同）：`job.jd_text` 存的就是文本本身，岗位没有"稍后再解析一遍"的阶段。
`.txt` 解码按 `utf-8-sig → gb18030` 顺序试——这台机器的记事本另存默认 GBK，只按 UTF-8 硬解会
在中文 JD 上抛 500，而错误原因（编码）对人完全不可见。

### 4. 撞自然键返 409，不是等数据库抛约束

新增 `ConflictError`（`status_code=409`，`code="already_exists"`）。`POST /jobs` 先按
`title + company` 查（`JobRepository.get_by_title_and_company`，seed 的幂等早就依赖它），
命中就 409 并在 `details` 里回带已存在行的 id，调用方能直接转 PUT。

### 5. 删除的传播方向：岗位放行，简历默认拒

- `DELETE /jobs/{id}` 直接放行：`interview.job_id` 是 `SET NULL`，加上快照列，历史报告留得下。
- `DELETE /resumes/{id}` **名下有报告时返 409**（`details.interview_count`），要 `?force=true` 才删。
  理由：`interview.resume_id` 是 `ON DELETE CASCADE`，这一删会把报告与题目一起带走，
  而库里没有软删列、仓库不是 git、没有任何回收站。级联本身是 ADR-0007 的原始取舍
  （没了简历的报告没有意义），这里改的是**默认值**：不可撤销的动作必须由人显式授权第二次。
  页面侧同样是"先 409 → 二次确认 → 带 `force=true` 重试"，不静默升级。
- 落盘文件在 `session.delete` **之后**删、且忽略失败：先把原件删掉再让事务回滚，
  留下的是"库里有、盘上没有"的坏行；反过来只占点磁盘。

### 6. `PUT /resumes/{id}` 只开 `name`

`raw_text` 与 `parsed` 不开写入口。它们是抽取结果加证据原文，`validate` 的回放、
`review` 的证据核对、报告里每条"依据出处"都指着它；手改一处就让整条证据链失效，
比"改错了再改回来"严重得多。要重抽就重新 `POST /resumes/upload`。

### 7. 控制台补齐写侧

岗位页：建 / 上传 / 编辑 / 删（表单复用同一套字段，编辑态回填）；简历页：改名 / 删；
报告行与报告头标题一律显示快照（`林清生 × 前端开发工程师（灯塔智能）`），岗位被删时
额外挂一个"岗位已下线"徽标——这正是快照存在的理由，让它在页面上看得见。

## 理由

- 快照 + 拒绝式删除是同一个问题的两面：报告要能被解释，而解释依据不能依赖外部行的存活。
- 409 而非 500/422：`already_exists` 是"这行已经有了"的**事实**，客户端据此分支（转 PUT /
  换名 / 放弃），而约束冲突抛上来的 `IntegrityError` 不带那个已存在行的 id。
- 写入侧同步化省掉了一整套任务簿记（`task` 行、轮询、失败态），因为这条链路本来就没有等待。

## 权衡与代价

- **快照是第二份真相**：简历改名后，旧报告仍写旧名（这是有意的），但"当前名字"和
  "报告里的名字"会长期不一致，页面必须清楚它说的是"生成那一刻"。没有回写机制，也不打算加。
- **409 靠先查后插**，并发下仍可能两个请求都查空、后插的撞 `uq_job_title_company` 变成 500。
  SQLite 单写者 + 本机单人使用，接受这个窗口；真上多进程要在 repository 里捕 `IntegrityError`
  再翻 409。
- **`JobOut` 现在带 `jd_text`**：列表响应变大（JD 通常几百到几千字），也把工作描述原文外泄到
  读接口。JD 本来就是要发给候选人的东西，可读性优先；`ResumeOut` 的 `stored_path` 仍是工序列，不外泄。
- **`force=true` 的删除不可撤销**，且删除的粒度是"简历 + 其全部报告 + 全部题目"。
- **`required_skills` 派生有词典上限**：词典里没有的新技能（例：某个新框架名）不会被认出来，
  表现为岗位少一条要求。补词典是正解，换 LLM 不是（会引入第二个判据）。

## 后续影响

- 计划书阶段 6 的 `POST /upload-jd` 由 `POST /jobs/upload` 提前兑现，但 `POST /match`
  （简历↔岗位的相似度检索/排序）仍缺，`docs/TODO.md` 挂账。
- "给已有表加 NOT NULL 列一律裸 `ALTER TABLE … DEFAULT`，禁用 `batch_alter_table` 重建"
  升格为 `AGENTS.md` §2 的硬规则，附本次事故。
- 迁移测试目前只跑 `upgrade` / `downgrade` / `check`；本文件的形状要求写成一条
  "**不许**出现 `batch_alter_table`" 的静态断言更稳，见 `docs/TODO.md`。
