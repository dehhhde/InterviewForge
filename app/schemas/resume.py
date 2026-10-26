"""简历 DTO：Pydantic 模型 + 校验器〔阶段 1 · §4.4，阶段 5 抽取目标〕

抽取层（`app/extractors/`）的输出契约就是本文件。两条硬规则：
1. 事实条目必须带 `evidence`（简历原文片段），无证据即视为幻觉，宁可缺失不可编造。
2. 对 LLM 多返回的未知键采取忽略而非报错——`extra="forbid"` 会让每次多余输出
   都触发一轮重试，成本远高于丢掉一个键。
"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

Degree = Literal["college", "bachelor", "master", "phd", "other"]
SkillLevel = Literal["basic", "familiar", "proficient"]

DEGREE_ALIASES: dict[str, Degree] = {
    "大专": "college",
    "专科": "college",
    "本科": "bachelor",
    "学士": "bachelor",
    "硕士": "master",
    "研究生": "master",
    "博士": "phd",
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
            return DEGREE_ALIASES.get(text, text if text in DEGREE_ALIASES.values() else "other")
        return value

    @model_validator(mode="after")
    def _check_year_order(self) -> "EducationItem":
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
    def _check_date_order(self) -> "WorkExperience":
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
    left_years = left.years if left.years is not None else -1
    right_years = right.years if right.years is not None else -1
    return right if right_years > left_years else left


class ResumeOut(BaseModel):
    """`GET /resumes/{id}` 的读取 DTO（M6）。

    `parsed` 按原样透传抽取 JSON，不重新套 `ResumeSchema`：库里存的形状是当年通过校验的
    那份 dump，而 `ResumeSchema` 还在长（M1/M2 加过 `dropped`、别名表）——用今天的 schema
    回读昨天的行，等于让 schema 变更把历史数据判成脏数据。
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    file_name: str
    name: str
    parsed: dict[str, Any]
    created_at: datetime


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
