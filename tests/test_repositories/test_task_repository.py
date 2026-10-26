"""任务表自有查询〔阶段 8 · M6〕

`task` 行的写入路径只有两条：API 建行（`pending`）与后台任务翻状态。
这里验的是 repository 这一层守住的边界：状态取值域、进度追加语义、CHECK 兜底。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task
from app.repositories.task_repository import TaskRepository


async def test_new_task_starts_pending(session: AsyncSession) -> None:
    task = await TaskRepository(session).create(kind="resume_parsing", ref_id="r-1")
    assert task.status == "pending"
    assert task.progress == {} and task.result == {} and task.error == {}


async def test_status_flips_and_steps_append(session: AsyncSession) -> None:
    repo = TaskRepository(session)
    task = await repo.create(kind="question_generation", ref_id="i-1")
    await repo.set_status(task.id, "running")
    await repo.append_step(task.id, {"node": "parse", "ms": 12.5})
    await repo.append_step(task.id, {"node": "validate"})

    fresh = await repo.get_by_id(task.id)
    assert fresh is not None and fresh.status == "running"
    steps = fresh.progress["steps"]
    assert [step["node"] for step in steps] == ["parse", "validate"]
    assert steps[0]["ms"] == 12.5 and "ms" not in steps[1]


async def test_finish_writes_terminal_status_with_result_or_error(session: AsyncSession) -> None:
    repo = TaskRepository(session)
    task = await repo.create(kind="resume_parsing", ref_id="r-2")
    await repo.finish(task.id, "completed", result={"question_total": 15})
    done = await repo.get_by_id(task.id)
    assert done is not None and done.status == "completed"
    assert done.result["question_total"] == 15 and done.error == {}


async def test_unknown_status_rejected_before_any_sql(session: AsyncSession) -> None:
    repo = TaskRepository(session)
    task = await repo.create(kind="resume_parsing", ref_id="r-3")
    with pytest.raises(ValueError, match="complated"):
        await repo.set_status(task.id, "complated")


async def test_finish_refuses_non_terminal_status(session: AsyncSession) -> None:
    repo = TaskRepository(session)
    task = await repo.create(kind="resume_parsing", ref_id="r-4")
    with pytest.raises(ValueError):
        await repo.finish(task.id, "running")


async def test_database_check_constraint_rejects_unknown_kind(session: AsyncSession) -> None:
    """`create()` 不校验 `kind`（repository 照单全收），兜底的是表的 CHECK。"""
    session.add(Task(kind="email_marketing", status="pending"))
    with pytest.raises(IntegrityError):
        await session.flush()
