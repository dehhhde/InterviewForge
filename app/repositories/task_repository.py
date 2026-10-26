"""任务进度 CRUD〔阶段 8 · M6〕"""

from __future__ import annotations

from typing import Any, ClassVar

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
