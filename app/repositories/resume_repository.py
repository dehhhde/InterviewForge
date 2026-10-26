"""简历 CRUD〔阶段 3〕"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import select

from app.models.resume import Resume
from app.repositories.base import BaseRepository


class ResumeRepository(BaseRepository[Resume]):
    model_class: ClassVar[type[Resume]] = Resume

    async def get_by_stored_path(self, stored_path: str) -> Resume | None:
        """按落盘路径找简历。

        存在的理由只有一个：`seed.py` 与上传重放要靠它判断"这份文件是不是已经入过库"，
        而我们没有第二个自然键——`id` 是随机的，文件名可能被用户重复上传。
        """
        stmt = select(Resume).where(Resume.stored_path == stored_path)
        return (await self.session.execute(stmt)).scalar_one_or_none()
