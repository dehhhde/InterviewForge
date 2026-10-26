"""面试报告模型〔阶段 3〕

一张表对应一次工作流的**收尾产物**：`review` 收尾时由 service 落库（AGENTS.md §4.6——
中间的每个节点都不写库，图崩在中途才不会留下半份报告）。

`job_id` 用 `SET NULL` 而不是 `CASCADE`：岗位可能被下线删除，但已经发给候选人的那份
面试题不能跟着一起消失。这是四张表里唯一一个"删除不该传播"的关系。
"""

from __future__ import annotations

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import ORMModel


class Interview(ORMModel):
    """一次「简历 + 岗位 -> 面试题」的报告头。"""

    __tablename__ = "interview"

    resume_id: Mapped[str] = mapped_column(
        ForeignKey("resume.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("job.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: 生成那一刻的简历与岗位名（`start_generation` 写入，之后不随源表变化）。
    #: 存副本而不是靠 join 回读：岗位可被下线（`SET NULL`）、简历可被改名或删除，
    #: 而"这份卷子是给谁、按哪个岗位出的"属于报告的事实，必须跟着报告一起活着。
    #:
    #: `server_default` 是迁移逼出来的：SQLite 只允许给已有表加"带默认值的 NOT NULL 列"，
    #: 而走 `batch_alter_table` 重建表会触发 `question` 的 `ON DELETE CASCADE`（实测删空过题目）。
    #: 模型声明它，`alembic check` 才不与迁移建出来的库报漂移；写入永远由服务层给值。
    resume_name: Mapped[str] = mapped_column(
        String(120), nullable=False, default="", server_default=text("''")
    )
    resume_file_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=text("''")
    )
    job_title: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=text("''")
    )
    job_company: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=text("''")
    )
    #: 生命周期只有三个值，且**不由 API 传入**：`running` 是插行时的默认，
    #: `completed` / `aborted` 只在收尾写。API 层给状态字段开写入口就会造出假 completed。
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running", index=True)
    #: `plan_node` 的产物（题目分布 / weak_points），整份存 JSON，理由同 `resume.parsed`。
    plan: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    total_questions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: `review` 的批注列表（`list[dict]`，含 rule/model 来源标签）。
    review_issues: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list
    )

    __table_args__ = (
        # name 只给后缀：`NAMING_CONVENTION["ck"]` 会拼成 `ck_interview_status`。
        CheckConstraint("status IN ('running', 'completed', 'aborted')", name="status"),
    )
