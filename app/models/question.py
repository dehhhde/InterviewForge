"""面试题模型〔阶段 3〕

列与 `schemas/question.py::QuestionSchema` 一一对应，但**不给每题存分数**：
`review` 的打分对象是整批题（`review_issues` 是列表），落在 `interview` 上才贴近它的形状。

`batch` 是计划 §6 风险 5 要的机制：`questions` 是累积通道，同一线程被重放时新旧批次会
并存，收尾只取最大 batch。落库同样按批次写，所以"这份报告最终用了哪几道题"能从表里复原，
而不是被第二次 insert 覆盖掉。（ADR-0015 之后一轮成卷，正常跑永远是批次 1。）
"""

from __future__ import annotations

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import ORMModel


class Question(ORMModel):
    """一道面试题。"""

    __tablename__ = "question"

    #: 没有 `relationship()`，读取由 repository 显式 join（见 `models/base.py` 第 3 条）。
    #: 级联删除：一道题脱离报告就没有意义，报告删了题跟着走。
    interview_id: Mapped[str] = mapped_column(
        ForeignKey("interview.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: 批内顺序，就是报告里的题号。
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    stem: Mapped[str] = mapped_column(Text, nullable=False)
    #: 取值域由 `QuestionSchema` 的 Literal 定义；这里再写一遍 CheckConstraint，
    #: 是因为写入路径不止 API 一条（seed 脚本、后台任务、将来的迁移脚本），
    #: DB 是最后一道兜得住的边界。
    difficulty: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="easy | medium | hard"
    )
    source: Mapped[str] = mapped_column(
        String(8), nullable=False, default="generic", comment="resume | jd | generic"
    )
    #: `source="resume"` 时指向原文的一句话。
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")

    checkpoints: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    reference_answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rubric: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    follow_ups: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    __table_args__ = (
        # 收尾节点必须幂等（AGENTS.md §4.6）：同一份报告重跑不能叠出重复题。
        UniqueConstraint(
            "interview_id",
            "batch",
            "order_index",
            name="uq_question_interview_id_batch_order_index",
        ),
        # 与 QuestionSchema 的 Literal 对齐；SQLite 会执行 CHECK，不只是装饰。
        # 这里的 name 只给后缀——`NAMING_CONVENTION["ck"]` 会自动拼上 `ck_<表名>_`。
        CheckConstraint("difficulty IN ('easy', 'medium', 'hard')", name="difficulty"),
        CheckConstraint("source IN ('resume', 'jd', 'generic')", name="source"),
    )
