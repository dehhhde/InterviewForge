"""岗位 JD 模型〔阶段 3〕

`match_node`（M4）的输入就是这张表：`required_skills` 与简历 `parsed["skills"]` 求差集，
差集进 `matched_gaps`。所以 JD 原文和它的技能要求要**分开存**——
只有原文的话，每次 match 都得重新抽一遍技能；只有技能的话，就没法回溯"这个要求是从哪句话来的"。
"""

from __future__ import annotations

from sqlalchemy import JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import ORMModel


class Job(ORMModel):
    """一个招聘岗位。"""

    __tablename__ = "job"

    title: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    company: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    jd_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 岗位要求技能，小写归一后的字符串列表（归一规则与 `parse_node` 的技能表一致）。
    required_skills: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: 期望年限；`0.0` 表示"不限"，别拿 `None` 当"不限"——排序时 None 在 SQLite 里最小。
    min_years: Mapped[float] = mapped_column(nullable=False, default=0.0)

    __table_args__ = (
        # seed 要能反复跑（§M3 验收：重启进程后数据仍在），幂等靠这个自然键：
        # 同 title + company 只有一行，第二次跑是更新而不是插重复。
        UniqueConstraint("title", "company", name="uq_job_title_company"),
    )
