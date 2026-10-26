"""GET /tasks/{task_id} 查询异步任务进度〔阶段 8 · §11.4，M6 落地〕

轮询端点的全部技术含量在于它**必须便宜**：只读一行 `task`，不 join、不进图、
不碰 checkpoint——"上传简历 → 轮询 → 拿到 15 题，全程不阻塞事件循环"这条验收里，
后台图在烧几十次 LLM 调用，这里的一次 SELECT 就是客户端观察进度的唯一窗口。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.dependencies import SessionDep
from app.exceptions.base import NotFoundError
from app.schemas.task import TaskOut
from app.services import task_service

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: str, session: SessionDep) -> TaskOut:
    task = await task_service.read_task(session, task_id)
    if task is None:
        raise NotFoundError("任务不存在", details={"task_id": task_id})
    return TaskOut.model_validate(task)
