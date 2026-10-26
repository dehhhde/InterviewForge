"""岗位 CRUD〔阶段 3〕"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import select

from app.models.job import Job
from app.repositories.base import BaseRepository


class JobRepository(BaseRepository[Job]):
    model_class: ClassVar[type[Job]] = Job

    async def get_by_title_and_company(self, title: str, company: str) -> Job | None:
        """按自然键查——`uq_job_title_company` 的 Python 侧镜像。

        seed 要能反复跑（M3 验收），靠的是"先按自然键查，有则改无则插"，
        而不是拿随机 uuid 去猜上一次插了哪行。
        """
        stmt = select(Job).where(Job.title == title, Job.company == company)
        return (await self.session.execute(stmt)).scalar_one_or_none()
