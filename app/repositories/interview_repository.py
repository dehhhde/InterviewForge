"""面试报告 CRUD〔阶段 3〕"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from sqlalchemy import select

from app.models.interview import Interview
from app.repositories.base import BaseRepository


class InterviewRepository(BaseRepository[Interview]):
    model_class: ClassVar[type[Interview]] = Interview

    async def list_for_resume(self, resume_id: str) -> Sequence[Interview]:
        """一份简历跑过几次报告，倒序。API 的 `GET /interviews?resume_id=` 直接用它。"""
        stmt = (
            select(Interview)
            .where(Interview.resume_id == resume_id)
            .order_by(Interview.created_at.desc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def set_status(self, id_: str, status: str) -> Interview | None:
        """收尾专用：`running` -> `completed` / `aborted`。

        单独一个方法而不是让调用方 `update(status=…)`，是因为取值域只有三个，
        而 `update(**values)` 会照单全收——把拼错的 `complated` 写进库这种事
        不该依赖调用方自觉。
        """
        if status not in {"running", "completed", "aborted"}:
            raise ValueError(f"未知的报告状态：{status!r}")
        return await self.update(id_, status=status)
