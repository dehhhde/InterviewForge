# 03 · 解析与抽取层

这一层负责把「任意格式的简历文件」变成「可被 schema 校验的结构化字段」。设计原则一句话：
**确定性优先、无证据即丢弃**。

相关代码：

- 文件解析：`app/parsers/`
- 字段抽取：`app/extractors/`
- 输入 schema：`app/schemas/resume.py`

## 1. 文件解析器

### 1.1 公共契约

[`app/parsers/base.py`](file:///d:/code/InterviewForge/app/parsers/base.py)

```python
@dataclass(frozen=True)
class ParsedDocument:
    raw_text: str
    page_count: int
    is_scanned: bool = False
    parser: str = ""
    warnings: tuple[str, ...] = ()
    prompt_version: str = ""

class BaseParser(ABC):
    extensions: tuple[str, ...] = ()
    def parse(self, path: Path) -> ParsedDocument: ...

class ParserRegistry:
    def register(self, parser: BaseParser) -> None: ...
    @property
    def supported(self) -> tuple[str, ...]: ...
    def for_path(self, path: Path) -> BaseParser: ...
    def parse(self, path: Path) -> ParsedDocument: ...
```

- 解析器是**同步**的（PDF 解码 CPU 密集），节点用 `asyncio.to_thread(registry.parse, path)` 调用。
- 读不出文本必须抛 `ParseError`，**绝不返回空 `raw_text`**——空文本会让下游抽出一份
  「看起来干净」的空简历。
- `ParserRegistry` 先判扩展名再判文件存在（`.txt` 该报「不支持」而不是「文件不存在」）。
- 注册表没有内置默认实现；`parse_node` 里显式 `ParserRegistry([PdfParser(), DocxParser()])`。

### 1.2 PDF — `PdfParser`

[`pdf_parser.py`](file:///d:/code/InterviewForge/app/parsers/pdf_parser.py)

- 依赖 PyMuPDF（选型见 `docs/adr/0006`）。
- `get_text("text", sort=True)` 按坐标排序，避免内容流顺序把姓名块排到教育块后面。
- 加密 PDF → `parse_encrypted`；无页面 → `parse_empty_pdf`；打不开 → `parse_bad_pdf`。
- **扫描件判定**：平均每页文字 `< 40` 字（`_MIN_CHARS_PER_PAGE`）的页视为无文字层；
  若所有页都如此，抛 `ParseError(code="parse_scanned_image")`，由节点转 `ImageParser`。
- 个别页文字层过薄只记 `warnings`，不阻断。

### 1.3 DOCX — `DocxParser`

[`docx_parser.py`](file:///d:/code/InterviewForge/app/parsers/docx_parser.py)

- 依赖 python-docx，只支持 OOXML `.docx`；`.doc` 由 registry 按扩展名拒绝。
- **必须按 `document.element.body.iterchildren()` 顺序遍历**：`paragraphs` 与 `tables` 是两个
  独立列表，先段落后表格会把简历里的表格整块挪到文末，脱离它上面的标题。
- 页眉按视觉顺序放在正文前（中文简历常把姓名/联系方式写页眉）；页脚不取。
- 表格单元格用**制表符**连接，不用 `|`（正文里 `dict | None` 很常见，会被误判为表格行）。
- 空文档 → `parse_empty_docx`。

### 1.4 图片 / 扫描件 — `ImageParser`

[`image_parser.py`](file:///d:/code/InterviewForge/app/parsers/image_parser.py)

- **不在 `ParserRegistry` 里**：`BaseParser.parse` 是同步契约，这里要等 LLM 网络往返。
- 支持 `IMAGE_SUFFIXES`（jpg/jpeg/png/webp/bmp/tif/tiff）与扫描 PDF。
- 构造时若 `settings.llm_model_vision` 为空 → `ParseError("parse_vision_unconfigured")`；
  **绝不退回纯文本模型**（文本模型「读」图不会报错，只会编出完整简历）。
- 每页转 data URL（最长边 1560px、JPEG q85），交 `ocr` prompt 给视觉模型，温度 0。
- 每页少于 20 字记 `warnings`；全空 → `parse_vision_empty`；超过 20 页拒绝（成本护栏）。
- 返回 `ParsedDocument(is_scanned=True, prompt_version="ocr@v1")`。

## 2. 证据铁律

[`app/extractors/base.py`](file:///d:/code/InterviewForge/app/extractors/base.py)

```python
@dataclass(frozen=True)
class Evidence:
    value: str
    evidence: str      # 原文出处
    source: Literal["regex", "llm"]

def collapse(text: str) -> str:   # 合并空白、删除零宽字符
def squeeze(text: str) -> str:    # 删除所有空白，用于归属核对
def evidence_belongs(raw_text: str, evidence: str) -> bool
```

- **任何值必须能在原文里逐字找到出处**（比对前抹掉空白与零宽字符）。
- LLM 给的 `evidence` 核对不上 → 当没给，并记进 `notes`。
- 宁可缺失，不许编造。
- `notes` 是给人看的流水账，节点把它并进 `errors`；需要机器判定的事实走结构化字段
  （例：`TimelineResult.dropped` 带 `reason`）。

## 3. 栏目切分 — `sections.py`

[`app/extractors/sections.py`](file:///d:/code/InterviewForge/app/extractors/sections.py)

```python
SectionName = Literal["education", "work", "project", "skills"]

@dataclass(frozen=True)
class SectionMap:
    sections: dict[SectionName, list[str]]
    preamble: list[str]                  # 第一个标题之前的行
    unknown_headers: list[str]           # 像标题但词典没命中

def header_kind(line) -> tuple[SectionName | None, SubName | None]
def looks_like_header(line) -> bool
def is_bullet(line) -> bool
def merge_wrapped(lines) -> list[str]
```

- 中文栏目名极随意，靠 `SECTION_KEYWORDS` 别名词表识别（教育经历/教育背景/学习经历…）。
- 子标题（`SUBSECTION_KEYWORDS`）用于项目块内分「描述 / 技术栈 / 成果」。
- 标题判据三道守卫：短（≤12 字）、无数字、命中词表。避免「2年工作经验 | 求职意向」
  这种正文行把头图误判成栏目。
- 像标题却没命中 → `unknown_headers`，提示「该补词典」，不硬猜。

## 4. 四个简历抽取器

### 4.1 `NerExtractor` — 联系人四项（唯一可花 LLM）

[`ner_extractor.py`](file:///d:/code/InterviewForge/app/extractors/ner_extractor.py)

- `NerResult`：`name / email / phone / years_experience`（`Evidence | None`）、
  `prompt_version`、`notes`、`missing`。
- **正则打底**：
  - 手机号 11 位 `1[3-9]`，支持 `+86` 与空格/横线，前后断言挡 18 位身份证；
  - 邮箱标准正则；
  - 年限必须跟「经验/经历/从业」上下文；
  - 姓名只认显式标签（`姓名：` / `name:`），版式猜测交给 LLM。
- 缺字段才调 LLM（`ner` prompt，温度 0），每个值校验 `evidence_belongs`；
  对不上的丢弃并记 `notes`。
- 每份简历正常只花 **1 次** LLM 调用（其余抽取器纯规则）。

### 4.2 `extract_skills` — 技能词典（不调 LLM）

[`skill_extractor.py`](file:///d:/code/InterviewForge/app/extractors/skill_extractor.py)

- `SKILL_ALIASES`：别名（小写）→ 展示名，覆盖语言/框架/数据库/中间件/运维/前端/AI 等。
- `LEVEL_WORDS`：程度词 → `SkillLevel`（`proficient/familiar/...`）；**只认表内词**，
  表外留 `None`，绝不猜高一级。
- `level` 取「名字左边最近的程度词」：`熟悉 MySQL、PostgreSQL` 两个都算熟悉；
  名字右边的程度词不认（多半在描述别的对象）。
- 词典外拉丁词进 `unmapped`——那是「该补词典」的开发提示，**不进 `errors`**（不是简历缺陷）。

### 4.3 `extract_timeline` — 工作/教育时间线 + 年限

[`timeline_extractor.py`](file:///d:/code/InterviewForge/app/extractors/timeline_extractor.py)

- `DateRange(start, end|None, text, head)`；`end is None` 表示「至今」。
- `_RANGE_RE` 支持 `2024.06-2026.06` / `2021-2025` / `2024/6~2025/7` /
  `2024年6月-至今` / `2021年-现在` 等写法；只有年份时按年初/年末补齐。
- `TimelineResult`：
  - `work_experiences` / `educations`
  - `years_experience: Evidence | None`（**算出来的**，evidence 记所依据的区间原文）
  - `notes`
  - `dropped: tuple[DroppedEntry]`，`DropReason = inverted | no_company | no_school | invalid`
- 区间倒挂不硬凑：丢掉该段，同时写 `notes`（给人看）与 `dropped`（给校验层机器判定）。
  `WorkExperience._check_date_order` 会拒绝倒挂区间，所以只能走 `dropped` 传下去。
- `as_resume_fields()` 拼成 `ResumeSchema` 的一部分。

### 4.4 `extract_projects` — 项目经历

[`project_extractor.py`](file:///d:/code/InterviewForge/app/extractors/project_extractor.py)

- 只在「项目经历」栏目里切块；栏目不在就明说，不拿全文硬猜项目名。
- 项目标题两级判据：带日期区间最可靠（`OA 系统 2025.07-2026.07`）；整段没有日期才退回
  版式（短行、无句读、不是列表项、紧跟子标题）。
- 每条 `ProjectExperience` 带整块 `evidence`（空白折叠，≤400 字），
  必须 `evidence_belongs(raw_text, evidence)`，对不上就丢弃并记 `notes`。
- `highlights` 收编号要点，散文归 `summary`；角色行（`担任/负责/角色：/职位：`）存 `role`。

## 5. 岗位字段抽取 — `job_extractor.py`

[`app/extractors/job_extractor.py`](file:///d:/code/InterviewForge/app/extractors/job_extractor.py)

用于「岗位图片/海报录入」：`POST /jobs/inspect-image` 识别成草稿（详见
[06-api-and-tasks.md](./06-api-and-tasks.md)）。

```python
@dataclass(frozen=True)
class JobFieldResult:
    title: Evidence | None
    company: Evidence | None
    min_years: Evidence | None        # value 是数字字符串
    prompt_version: str
    notes: tuple[str, ...]
    missing: tuple[str, ...]

def extract_by_rules(jd_text: str) -> JobFieldResult
class JobFieldExtractor:
    async def extract(self, jd_text: str) -> JobFieldResult
```

- **规则层**：岗位名标签（岗位名称/职位/职务/【岗位】）与海报版式关键词；公司名标签 +
  公司后缀词典（不含裸「公司」，避免咬住「公司介绍：」）；年限必须有「经验/年限」上下文，
  区间 `3-5 年` 取下界。
- **`min_years` 永不交给模型**：标签正则、海报标题、公司后缀可以再让 `job_fields` v1 兜底，
  唯独年限只走规则层（模型连日期先后都会算错）。规则认不出就是 0（页面显示「不限」）。
- LLM 兜底只补 `title` / `company`；每个值都要过两遍核对：`evidence_belongs(jd_text, evidence)`
  且值就含在证据里；任一处不过就丢进 `notes` 留空，由 `missing_required` 让人补。
- `title` 是岗位自然键的一半，**草稿不落库**，避免机器猜歪一次就占住 `uq_job_title_company`。

## 6. 抽取结果如何汇入 `parsed`

`parse_node` 的合并顺序：

1. `ner.as_resume_fields()` → name / email / phone / years_experience
2. `timeline.as_resume_fields()` → work_experiences / educations / years_experience（覆盖自称值）
3. `skills.as_resume_fields()` → skills
4. `projects.as_resume_fields()` → projects

随后 `ResumeSchema.model_validate(fields)` 保证形状。`years_experience` 缺失时按 `0.0`
占位并记 `errors`，把「这份简历成色如何」交给 `validate` 节点判定。

## 7. 已知边界与待办

- PDF 会把一行拆成两行，`sections.py` **不做行合并**（判据容易误伤日期行）；待办见 `docs/TODO.md` §1。
- 中文栏目词典需要持续补；未命中的标题与技能会以 `unknown_headers` / `unmapped` 提示。
- OCR 依赖多模态模型；未配置 `LLM_MODEL_VISION` 时扫描件直接 422，不做「假装成功」的降级。
