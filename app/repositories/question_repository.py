"""题目 CRUD〔阶段 3〕"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from sqlalchemy import func, select

from app.models.question import Question
from app.repositories.base import BaseRepository


class QuestionRepository(BaseRepository[Question]):
    model_class: ClassVar[type[Question]] = Question

    async def latest_batch(self, interview_id: str) -> int | None:
        """这份报告当前最大的批次号；还没有题时返回 None。

        计划 §6 风险 5：累积通道里可能并存两个批次，报告只该展示最新批次。
        没有这个入口的话，"取最新批"就只能靠调用方记得先查 batch 再查题。
        """
        stmt = select(func.max(Question.batch)).where(Question.interview_id == interview_id)
        value = (await self.session.execute(stmt)).scalar_one_or_none()
        return None if value is None else int(value)

    async def list_for_interview(
        self, interview_id: str, *, batch: int | None = None
    ) -> Sequence[Question]:
        """按批次读题；`batch=None` 表示"最新那一批"，不是"全部批次"。

        默认收紧成最新批次：读报告时把重做前后的两批题混在一起展示，
        看起来像是生成了 2N 道题，这种错很难在 UI 上被发现。
        """
        if batch is None:
            latest = await self.latest_batch(interview_id)
            if latest is None:
                return []
            batch = latest
        stmt = (
            select(Question)
            .where(Question.interview_id == interview_id, Question.batch == batch)
            .order_by(Question.order_index.asc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def add_many(
        self, rows: Sequence[Mapping[str, Any]], *, interview_id: str, batch: int
    ) -> Sequence[Question]:
        """批量写题。一次 `add_all` + 一次 flush——收尾是单事务，逐行 flush 只是白白多跑 N 次。"""
        instances = [
            Question(interview_id=interview_id, batch=batch, order_index=index, **row)
            for index, row in enumerate(rows)
        ]
        self.session.add_all(instances)
        await self.session.flush()
        return instances
