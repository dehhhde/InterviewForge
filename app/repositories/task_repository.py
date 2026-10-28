"""任务进度 CRUD〔阶段 8 · M6〕"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import select

from app.models.task import Task
from app.repositories.base import BaseRepository


class TaskRepository(BaseRepository[Task]):
    model_class: ClassVar[type[Task]] = Task

    async def set_status(self, id_: str, status: str) -> Task | None:
        """状态迁移专用入口，理由与 `InterviewRepository.set_status` 同：
        `update(**values)` 照单全收，拼错的 `compleated` 不该靠调用方自觉挡。"""
        if status not in Task.STATUSES:
            raise ValueError(f"未知的任务状态：{status!r}")
        return await self.update(id_, status=status)

    async def append_step(self, id_: str, step: dict[str, Any]) -> Task | None:
        """往 `progress["steps"]` 追加一条。

        JSON 列不能增量更新（ADR-0007 的代价），所以这里是整份读出来、append、再整份写回。
        调用点是后台任务的每个 super-step 一次，量级完全无所谓。
        """
        task = await self.get_by_id(id_)
        if task is None:
            return None
        progress = dict(task.progress or {})
        steps = list(progress.get("steps") or [])
        steps.append(step)
        progress["steps"] = steps
        return await self.update(id_, progress=progress)

    async def finish(
        self,
        id_: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> Task | None:
        """终局一次性写：状态 + 成功摘要或失败详情。"""
        if status not in {"completed", "failed"}:
            raise ValueError(f"任务终局只能是 completed/failed，收到 {status!r}")
        values: dict[str, Any] = {"status": status}
        if result is not None:
            values["result"] = result
        if error is not None:
            values["error"] = error
        return await self.update(id_, **values)

    async def list_stale(self, *, before: datetime, statuses: Sequence[str]) -> Sequence[Task]:
        """`updated_at` 早于 `before` 且状态在 `statuses` 里的行，按停滞时长倒序。

        这是回收判据唯一的落 SQL 处（AGENTS.md §3：只有 repository 写 SQL），
        所以它只负责"捞出来"，翻状态与提交归 `task_service.reap_stale`。
        `statuses` 由调用方给：`pending` 算不算僵尸是产品口径，不该藏在查询里。
        """
        for status in statuses:
            if status not in Task.STATUSES:
                raise ValueError(f"未知的任务状态：{status!r}")
        stmt = (
            select(Task)
            .where(Task.status.in_(statuses), Task.updated_at < before)
            .order_by(Task.updated_at.asc())
        )
        return (await self.session.execute(stmt)).scalars().all()
