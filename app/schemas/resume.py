"""简历 DTO：Pydantic 模型 + 校验器〔阶段 1 · §4.4，阶段 5 抽取目标〕

抽取层（`app/extractors/`）的输出契约就是本文件。两条硬规则：
1. 事实条目必须带 `evidence`（简历原文片段），无证据即视为幻觉，宁可缺失不可编造。
2. 对 LLM 多返回的未知键采取忽略而非报错——`extra="forbid"` 会让每次多余输出
   都触发一轮重试，成本远高于丢掉一个键。
"""

from datetime import date, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

Degree = Literal["college", "bachelor", "master", "phd", "other"]
SkillLevel = Literal["basic", "familiar", "proficient"]

#: 档位高低，"哪个技能写得更强"的唯一判据。放这里而不是抽取层：本文件的 `_richer`
#: （重复合并）与抽取层、`match_service`（"JD 要精通、简历只写熟悉"）、`profile_service`
#: （`skill_depth` 分档）四处都要用它，而 schema 反向 import 抽取器就成环了。
LEVEL_RANK: dict[str, int] = {"basic": 0, "familiar": 1, "proficient": 2}

DEGREE_ALIASES: dict[str, Degree] = {
    "大专": "college",
    "专科": "college",
    "本科": "bachelor",
    "学士": "bachelor",
    "硕士": "master",
    "研究生": "master",
    "博士": "phd",
    # 在职学位的两种写法：字样归抽取层认，档位归这里定，否则它们会被静默归档成 other
    # 并让 M2 的枚举规则误报（AGENTS.md §5：宁可缺失，不许编造——但认得出的不该报）。
    "MBA": "master",
    "EMBA": "master",
    # 中等学历：`Degree` 枚举只到专科，所以它们**合法地**落在 other——
    # 收进别名表的目的是让校验层知道"这是认出来的，不是漏抽的"，别再为此报 issue。
    "中专": "other",
    "高中": "other",
}
_LEVEL_ALIASES: dict[str, SkillLevel] = {
    "了解": "basic",
    "会用": "basic",
    "熟悉": "familiar",
    "掌握": "familiar",
    "精通": "proficient",
    "专家": "proficient",
}


class SkillItem(BaseModel):
    """一项技能。`years` 缺失优于瞎猜——抽取器拿不到年限就留 None。"""

    name: str = Field(min_length=1)
    level: SkillLevel | None = None
    years: float | None = Field(default=None, ge=0, le=60)
    evidence: str = Field(min_length=2)

    @field_validator("level", mode="before")
    @classmethod
    def _normalize_level(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            return _LEVEL_ALIASES.get(text, text)
        return value


class EducationItem(BaseModel):
    school: str = Field(min_length=1)
    degree: Degree = "other"
    major: str | None = None
    start_year: int | None = Field(default=None, ge=1950, le=2100)
    graduate_year: int | None = Field(default=None, ge=1950, le=2100)
    evidence: str = Field(min_length=2)

    @field_validator("degree", mode="before")
    @classmethod
    def _normalize_degree(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            # 拉丁学位按大写查：模型与 OCR 都会回 `mba`/`Mba`，大小写不该变成"认不出"
            key = text.upper() if text.isascii() else text
            return DEGREE_ALIASES.get(key, key if key in DEGREE_ALIASES.values() else "other")
        return value

    @model_validator(mode="after")
    def _check_year_order(self) -> EducationItem:
        if (
            self.start_year is not None
            and self.graduate_year is not None
            and self.start_year > self.graduate_year
        ):
            raise ValueError(f"教育经历年份倒挂：{self.start_year} > {self.graduate_year}")
        return self


class WorkExperience(BaseModel):
    """一段工作履历。`end=None` 表示在职，这与"日期没抽出来"是两回事，
    后者由抽取器负责留空整条记录，不在这里混用。"""

    company: str = Field(min_length=1)
    role: str | None = None
    start: date | None = None
    end: date | None = None
    summary: str | None = None
    evidence: str = Field(min_length=2)

    @model_validator(mode="after")
    def _check_date_order(self) -> WorkExperience:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError(f"工作时间倒挂：{self.start} 晚于 {self.end}")
        return self


class ProjectExperience(BaseModel):
    name: str = Field(min_length=1)
    role: str | None = None
    summary: str | None = None
    highlights: list[str] = Field(default_factory=list)
    evidence: str = Field(min_length=2)


class ResumeSchema(BaseModel):
    """一份简历的结构化抽取结果，即 `AgentState.parsed` 的形状。"""

    name: str = Field(min_length=1)
    email: EmailStr | None = None
    phone: str | None = None
    years_experience: float = Field(ge=0, le=60)
    skills: list[SkillItem] = Field(default_factory=list)
    educations: list[EducationItem] = Field(default_factory=list)
    work_experiences: list[WorkExperience] = Field(default_factory=list)
    projects: list[ProjectExperience] = Field(default_factory=list)

    @field_validator("phone", mode="before")
    @classmethod
    def _strip_phone_separators(cls, value: object) -> object:
        if isinstance(value, str):
            digits = "".join(ch for ch in value if ch.isdigit())
            if not 7 <= len(digits) <= 15:
                raise ValueError(f"电话号码归一后只剩 {len(digits)} 位数字，判为抽取错误")
            return digits
        return value

    @field_validator("skills")
    @classmethod
    def _merge_duplicate_skills(cls, items: list[SkillItem]) -> list[SkillItem]:
        """LLM 常把 "Redis" 和 "redis" 抽成两条，按小写名合并，保留更强的证据。"""
        merged: dict[str, SkillItem] = {}
        for item in items:
            key = item.name.strip().lower()
            previous = merged.get(key)
            if previous is None:
                merged[key] = item
                continue
            merged[key] = _richer(previous, item)
        return list(merged.values())


def _richer(left: SkillItem, right: SkillItem) -> SkillItem:
    """同一技能的两条记录留更强的那条：先比档位，同档再比年限。

    为什么不能只比 `years`：抽取器从不填它（"缺失优于瞎猜"写在上面对它的注释里），
    于是比较恒等于 `平手 → 留左边`，合并结果就是"LLM 先说的那条"——
    `[Java(了解), Java(精通)]` 会留下 `了解`，把画像和难度分档一起带低。
    """
    left_level = LEVEL_RANK.get(left.level or "", -1)
    right_level = LEVEL_RANK.get(right.level or "", -1)
    if left_level != right_level:
        return right if right_level > left_level else left
    left_years = left.years if left.years is not None else -1
    right_years = right.years if right.years is not None else -1
    return right if right_years > left_years else left


class ResumeFields(BaseModel):
    """简历的**列表行与详情共用**的列，字段名只在这里声明一次〔T-301 / OPT-6.1〕。

    拆成两份平行模型是这张卡明确禁止的做法：加一列要改两处，漏一处就出现"列表里有、详情没有"
    的字段，而页面会拿它当真相。继承让详情多带的那一列必须被显式命名，共用列则无法各写一遍。
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    file_name: str
    name: str
    created_at: datetime


#: 概览里带几个技能名：列表页与出题下拉都只渲染前 8 个，多发的部分是白占带宽。
SKILL_PREVIEW_LIMIT = 8


class ResumeOverview(BaseModel):
    """列表行的抽取概览：三个数 + 前几项技能名。

    它是从 `parsed` **当场算出来的**，不在 `resume` 表上存副本——表里加一列 `skill_count`
    就是第二份真相（`app/models/resume.py` 对 `years_experience` 明确拒过这种复制）。
    """

    skill_count: int = 0
    #: 只前 `SKILL_PREVIEW_LIMIT` 项；逐条技能（含证据）在 `GET /resumes/{id}` 给。
    skill_names: list[str] = Field(default_factory=list)
    years_experience: float | None = None
    work_experience_count: int = 0


def build_overview(parsed: dict[str, Any]) -> ResumeOverview:
    """`parsed` → 概览。历史行的键可能比今天的 schema 少，所以逐项容错而不是套 `ResumeSchema`。

    算不出的就留默认值：概览是展示用的，把"这一行没有年限"报成 500 或 0 都比"没抽出来"更糟。
    """
    skills = parsed.get("skills")
    work = parsed.get("work_experiences")
    years = parsed.get("years_experience")
    names = [
        str(item["name"])
        for item in (skills if isinstance(skills, list) else [])
        if isinstance(item, dict) and item.get("name")
    ]
    return ResumeOverview(
        skill_count=len(skills) if isinstance(skills, list) else 0,
        skill_names=names[:SKILL_PREVIEW_LIMIT],
        years_experience=float(years) if isinstance(years, (int, float)) else None,
        work_experience_count=len(work) if isinstance(work, list) else 0,
    )


class ResumeListItem(ResumeFields):
    """`GET /resumes/list` 的行：共用列 + 概览，**不带 `parsed` 正文**〔T-301〕。

    列表 DTO 与详情 DTO 原本是同一个模型，于是一页 50 行把 skills/educations/work/projects
    连同每条 `evidence` 全量下发（BACKLOG P3-14）。列表页要的是"几项技能、几年经验"，
    正文明细在 `GET /resumes/{id}` 给。
    """

    overview: ResumeOverview = Field(default_factory=ResumeOverview)

    @classmethod
    def from_row(cls, row: Any) -> Self:
        """共用列取 `ResumeFields` 的字段表，这里不重列列名；概览从 `parsed` 现算。

        `row` 是 `Resume` ORM 行，但 schema 反向 import models 会搅乱 §3 的分层方向，
        所以按属性取而不是按类型取。
        """
        shared = {key: getattr(row, key) for key in ResumeFields.model_fields}
        return cls(**shared, overview=build_overview(dict(row.parsed or {})))


class ResumeOut(ResumeFields):
    """`GET /resumes/{id}` 的读取 DTO（M6）：共用列 + 抽取结果全文。

    `parsed` 按原样透传抽取 JSON，不重新套 `ResumeSchema`：库里存的形状是当年通过校验的
    那份 dump，而 `ResumeSchema` 还在长（M1/M2 加过 `dropped`、别名表）——用今天的 schema
    回读昨天的行，等于让 schema 变更把历史数据判成脏数据。
    """

    parsed: dict[str, Any]


class UploadOut(BaseModel):
    """`POST /resumes/upload` 的回执：立即返回两个 id，解析在后台跑。"""

    resume_id: str
    task_id: str
    status: str = "pending"


class ResumeUpdateRequest(BaseModel):
    """`PUT /resumes/{id}` 的可改字段。

    只开 `name`：它是展示名，抽取或人工纠正都可能要改。`raw_text` 与 `parsed` **不开写入口**——
    它们是"抽取结果 + 证据原文"这一份真相，`validate` 的回放、`review` 的证据核对、报告里的
    每条"依据出处"都指着它；手改一处就让整条证据链失效，比空着不改正大得多。
    真要重抽，走 `POST /resumes/upload` 重新上传。
    """

    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def _strip_and_reject_blank(cls, value: str) -> str:
        """`min_length=1` 挡不住三个空格：列表页照样是空列。去掉首尾空白再判，
        顺带把提交值归一（展示名带尾空格会在报告快照列里一路带下去）。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("展示名不能只由空白组成")
        return stripped
