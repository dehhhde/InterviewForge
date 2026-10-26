"""岗位 DTO：读取形状 + 创建/更新的入参〔阶段 3 建壳，M6 只读列表，本次补齐 CRUD〕

`JobOut` 现在把 `jd_text` 一起发出去：出题链路要在页面上显示"这份卷子按哪句 JD 出的"，
而 `match` 的每条判定都锚在 JD 原文的逐字片段上——不给原文，页面上就只剩技能名。
JD 本来也不算秘密：它是要发给候选人的岗位描述。

创建/更新是分开的两个模型：`JobUpdateRequest` 全字段可选（部分更新），
`JobCreateRequest` 的 `title` 与 `jd_text` 必填。`required_skills` 留空时由服务层用
`SKILL_ALIASES` 词典从 `jd_text` 里派生——确定性优先，不花 token，也不会和 `match`
用的判据分叉。
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    company: str
    jd_text: str
    required_skills: list[str] = Field(default_factory=list)
    min_years: float = 0.0


class JobDraftOut(BaseModel):
    """`POST /jobs/inspect-image` 的回执：认出什么 + 还缺什么，**不落库**。

    之所以回草稿而不是直接建岗：图片这条路要过 OCR 与模型两次手，识别失败的字段一律留空，
    而 `title` 是 `job` 表的自然键组成部分——让机器猜出来的名字静默入库，等于用一条脏记录
    占住 `uq_job_title_company`。人确认过的值再走 `POST /jobs`，写路径与错误码都和现在同一条。

    `missing_required` 是给页面标红用的（目前只有 `title` 一项必填）；`notes` 是识别过程里
    值得让人知道的事，不参与判定。空字符串就是"没认出来"，别用 "未知" 之类的占位——
    那会顺着自然键变成另一个岗位。
    """

    title: str = ""
    company: str = ""
    jd_text: str
    required_skills: list[str] = Field(default_factory=list)
    #: `0.0` 与 `Job.min_years` 同义："不限"，不是"没认出来"
    min_years: float = 0.0
    missing_required: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class JobCreateRequest(BaseModel):
    """`POST /jobs`。自然键是 `title + company`，重复提交返 409。"""

    title: str = Field(min_length=1, max_length=200)
    company: str = Field(default="", max_length=200)
    jd_text: str = Field(min_length=5)
    #: 小写归一交给服务层，与 `parse_node` 的技能口径一致。
    required_skills: list[str] = Field(default_factory=list)
    min_years: float = Field(default=0.0, ge=0, le=60)

    @field_validator("title", "company", mode="before")
    @classmethod
    def _trim_names(cls, value: object) -> object:
        """自然键由这两列组成：留首尾空白的岗位名，`title="后端"` 与 `title="后端 "` 会是
        两个"不同"的岗位，去重形同虚设。剥完再走 `min_length`，纯空白就落进 422。"""
        return value.strip() if isinstance(value, str) else value


class JobUpdateRequest(BaseModel):
    """`PUT /jobs/{id}` 的部分更新：给了哪个改哪个，一个都没给就没什么可做的。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    jd_text: str | None = Field(default=None, min_length=5)
    required_skills: list[str] | None = None
    min_years: float | None = Field(default=None, ge=0, le=60)

    @field_validator("title", "company", mode="before")
    @classmethod
    def _trim_names(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> Self:
        if not self.model_dump(exclude_unset=True):
            raise ValueError("至少要给出一个待修改的字段")
        return self
