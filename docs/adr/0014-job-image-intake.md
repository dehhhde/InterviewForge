# ADR-0014：图片录入岗位——草稿·确认两步，字段规则优先、模型兜底必核出处

日期：2026-10-21
状态：已决定（M6 之后的写入侧补齐）
相关：`app/api/v1/jobs.py`（`POST /jobs/inspect-image`）、`app/services/job_service.py`
（`draft_job_from_image` / `allowed_image_suffixes`）、`app/extractors/job_extractor.py`、
`app/parsers/image_parser.py`（复用）、`app/prompts/templates/v1/job_fields.*.j2`、
`app/schemas/job.py`（`JobDraftOut`）、`app/dependencies.py`（`get_llm` / `LLMDep`）、
`.env`（`LLM_MODEL_VISION`）、`tests/test_extractors/test_job_extractor.py`、
`tests/test_api/test_job_image_api.py`、`frontend/{index.html,app.js}`、
ADR-0008（扫描件走多模态 LLM，编号仍预留）、ADR-0012（控制台）、ADR-0013（写入侧）

## 背景

ADR-0013 把岗位写入口开齐了，但录入仍然要求人**亲手填三个字段**：`title`、`company`、
`min_years`，JD 文件只负责提供 `jd_text`。而现实中的 JD 常常是一张图——招聘海报、微信截图、
PPT 里的一页。用户诉求："传一张图片即可识别并自动录入，缺的信息留空，必填的提示用户。"

手边已有的东西比想象的多：`app/parsers/image_parser.py`（视觉模型逐页抄成纯文本 + `ocr@v1`
模板 + 长边下采样 + 四条失败分支）与 `purpose="vision"` 的路由都已就位，缺的只是
`LLM_MODEL_VISION` 的选型，以及"从抄下来的文本里认岗位字段"这一段。

三条约束决定了做法：

1. 抽取层铁律：**无证据即丢弃**（`app/extractors/base.py`），值必须能在原文逐字找到出处；
2. 确定性优先：能用正则/词典解决的绝不花 token（AGENTS.md §5）；
3. 实测事实：模型判日期/数字会算错（AGENTS.md §7，2026-10-16），所以年限不许交给模型。

## 决策

### 1. 端点回**草稿**，不落库；保存仍是 `POST /jobs`

`POST /jobs/inspect-image`（multipart，只收 `IMAGE_SUFFIXES`）返回 `JobDraftOut`：
认出的 `title` / `company` / `min_years` / `jd_text` / `required_skills`，加上
`missing_required`（目前只可能是 `["title"]`）与 `notes`。页面把非空字段灌进表单，
人确认后走既有的 `POST /jobs`。

**为什么不一步到位建岗**：`title` 是自然键 `uq_job_title_company` 的一半。机器猜出来的岗位名
静默入库，等于用一条脏记录占住这个键——下一次真录入会撞 409，而 409 的错误体只会指着那条
错行。拆成两步之后：写路径、409/422、`required_skills` 派生全是 ADR-0013 的现成品，
新代码一行都不必碰数据库（端点连 `SessionDep` 都不注入）。

代价见"权衡"第一条：浏览器要同步等一次模型往返。

### 2. 字段来源分三层，`min_years` 只有前两层

`app/extractors/job_extractor.py`：

1. **标签正则**：`岗位名称：…`、`【职位】…`、`公司名称：…`、公司后缀词典
   （有限公司/集团/研究院/…）、"经验/年限"同行内的 `3-5 年`（**取下界**）；
2. **海报标题启发式**：前 3 个非空行里，短、无冒号、非列表项、含职位词的那一行当岗位名；
3. **LLM 兜底**：只有 `title` 或 `company` 前两层都没认出来时才发一次
   `chat_json("extract", …)`，模板 `job_fields@v1`。

第三层有两条硬核对：`evidence` 必须 `evidence_belongs(jd_text, evidence)`，
且 `squeeze(value)` 必须就含在 `squeeze(evidence)` 里——后者比简历侧多一层，因为岗位名是自然键，
模型把证据抄成"高级后端工程师"却把值写成"后端开发工程师"属于**换了个岗位**，只比证据会放行。
任一条不过就丢进 `notes` 并留空，由 `missing_required` 提示人补。

`min_years` 永不进第三层：AGENTS.md §7 的实测就是模型连日期先后都会算错，
"3-5 年取下界"这种数字判断必须留在规则层。规则认不出就是 `0.0`（"不限"）。

`required_skills` 继续由 `SKILL_ALIASES` 词典派生，不走模型，与 `match` 同一本词典（ADR-0013）。

### 3. HTTP 侧的 LLM 客户端有了装配点：`get_llm()`，每请求新建

`app/dependencies.py` 的引擎是进程单例，LLM 客户端**不是**：`LLMClient` 里的 `Semaphore`
与 `UsageTotals` 都是"一次运行"的量纲，缓存成单例会让用量统计跨请求累加，而成本护栏
（ADR-0011）说的是"这一次生成跑烧了多少"。测试从 `app.dependency_overrides[get_llm]` 换替身，
不必 monkeypatch 服务层内部。

### 4. 视觉模型选型：`deepseek-v4-flash-vision-exp`

写进 `.env` 与 `.env.example`，与文本模型同一个 `DEEPSEEK_BASE_URL`、同一把 key，所以
`config.py` 不需要第二条 base_url。`LLM_MODEL_VISION` 为空时 `ImageParser` 直接抛
`parse_vision_unconfigured`（422）——**绝不退回纯文本模型**：文本模型"读"图不会报错，
只会编出一份看着完整的 JD。

实测（2026-10-21，合成中文海报）：这条链路端到端跑通，一次请求出草稿，
`title` / `company` / `min_years` 三项全对。同时暴露的两个 OCR 副作用记在
`docs/TODO.md` §2.6，不在本 ADR 里掩盖。

### 5. 扫描件 PDF 不走这个端点

`allowed_image_suffixes()` 只列 `IMAGE_SUFFIXES`，不含 `.pdf`。`POST /jobs/upload` 已经收 PDF，
只有 `PdfParser` 明确报 `parse_scanned_image` 的原件才值得交给视觉模型，那是 ADR-0008
预留的账；把它搅进写入侧会让"哪种文件走哪条路"变成运行时判断。

## 理由

- 草稿·确认是"缺则留空、必填提示"这句话的最小实现：留空 = 字段空串 + `missing_required`，
  提示 = 页面把 `job-hint` 写成必填提示并聚焦输入框，全程不需要新状态、新表、新任务行。
- 三层字段来源把"便宜且可靠"放在前面：一份典型中文 JD 的三项都能在正则层拿到，
  于是**大多数图片只花 1 次模型调用**（读图），兜底那次是例外而非常态。
- 证据核对复用 `app/extractors/base.py` 而不是新写一套：出题与评审已经共用同一个判据
  （AGENTS.md §5），写入侧再分叉就等于同一份简历两种真相。

## 权衡与代价

1. **同步等待**：一张图 = 1 次读图 +（至多）1 次补字段，慢的时候是几十秒。没有 `task_id`
   可轮询，页面只能显示"识别中…"。选择这条而不是简历侧的 202+task，是因为产出是一份要被覆盖
   的表单而不是一行要落库的数据；真觉得难等，下一步把它做成 task 也不破坏任何契约。
2. **草稿是第二份真相，不回写**：识别结果不入库，`notes` 里"没认出来"这件事随响应消失，
   事后无法归因"当时为什么录成这个名字"。要归因得先看 trace，而这条链路不在图里，没有 trace。
3. **标题启发式必然认错**：`职位描述见附件` 这类行会被 `_TITLE_STOPWORD_RE` 挡掉，
   但"急招 后端开发"这种没有标点、位于前三行的句子仍可能被当成标题。兜法是预览——
   人对着表单看一眼的成本远低于一条脏自然键。
4. **上限 20 页沿用 `ImageParser`**：一次录入一张海报是常态，多页拼接图会被
   `parse_too_many_pages` 拒掉，这是 M1 定的护栏，没为岗位场景放松。

## 后续影响

- `docs/TODO.md` §1 第一条（定视觉模型 id）已勾掉，其余几条要验的是真扫描件样本与校准；
  §2.6 新增本节的账（真海报实测、OCR 把拉丁字母拆开、空白页回元话、标题启发式误报率）；
  §2.5 保持原样。
- `AGENTS.md` §3 追加"图片录入"小节：草稿不落库、`min_years` 不进模型、`LLMDep` 是唯一装配点。
- `app/services/job_service.py` 那句"零 LLM 调用"的注释已按路径改写：文件建岗仍零调用，
  图片建岗 1-2 次，但不产出任何行。
