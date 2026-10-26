"""任务进度服务〔阶段 8 · M6〕

覆盖两条约定：后台写的每一步**立即提交可见**（轮询方的全部依赖就在这里）；
失败详情的形状由异常类型决定（`AppError` 三件套 / 其余归 `internal_error`），
且对不存在的任务行静默跳过而不是炸后台协程。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.exceptions.base import ParseError
from app.models.task import Task
from app.repositories.task_repository import TaskRepository
from app.services import task_service
from app.utils.db import session_scope


async def _seed_task(db_sessionmaker: async_sessionmaker[AsyncSession], kind: str) -> str:
    async with session_scope(db_sessionmaker) as session:
        task = await task_service.create_task(session, kind=kind, ref_id="ref-1")
        return task.id


async def _load(db_sessionmaker: async_sessionmaker[AsyncSession], task_id: str) -> Task:
    async with db_sessionmaker() as session:
        found = await task_service.read_task(session, task_id)
    assert found is not None
    return found


async def test_create_task_starts_pending_and_reads_back(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    task_id = await _seed_task(db_sessionmaker, "resume_parsing")
    row = await _load(db_sessionmaker, task_id)
    assert (row.kind, row.status, row.ref_id) == ("resume_parsing", "pending", "ref-1")


async def test_create_task_rejects_unknown_kind(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with session_scope(db_sessionmaker) as session:
        try:
            await task_service.create_task(session, kind="fax", ref_id=None)
            raise AssertionError("未知 kind 应在建行前就被挡下")
        except ValueError as exc:
            assert "fax" in str(exc)


async def test_lifecycle_writes_are_committed_step_by_step(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    task_id = await _seed_task(db_sessionmaker, "question_generation")
    await task_service.mark_running(db_sessionmaker, task_id)
    await task_service.record_step(db_sessionmaker, task_id, "parse", ms=9.0)
    await task_service.record_step(db_sessionmaker, task_id, "generate_item")
    await task_service.mark_completed(db_sessionmaker, task_id, {"question_total": 2})

    row = await _load(db_sessionmaker, task_id)
    assert row.status == "completed"
    assert [step["node"] for step in row.progress["steps"]] == ["parse", "generate_item"]
    assert row.result["question_total"] == 2


async def test_mark_failed_records_app_error_shape(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    task_id = await _seed_task(db_sessionmaker, "resume_parsing")
    await task_service.mark_failed(
        db_sessionmaker, task_id, ParseError("读不出文本", details={"file": "x.pdf"})
    )
    row = await _load(db_sessionmaker, task_id)
    assert row.status == "failed"
    assert row.error == {
        "code": "parse_failed",
        "message": "读不出文本",
        "details": {"file": "x.pdf"},
    }


async def test_mark_failed_wraps_unexpected_exception(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    task_id = await _seed_task(db_sessionmaker, "resume_parsing")
    await task_service.mark_failed(db_sessionmaker, task_id, RuntimeError("池子里没连接了"))
    row = await _load(db_sessionmaker, task_id)
    assert row.error["code"] == "internal_error"
    assert "池子里没连接了" in row.error["message"]


async def test_progress_writes_for_missing_task_are_silent(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """API 响应事务回滚而后台已排上执行时，进度写空行不该把后台协程炸掉。"""
    ghost = "0" * 32
    await task_service.mark_running(db_sessionmaker, ghost)
    await task_service.record_step(db_sessionmaker, ghost, "parse")
    await task_service.mark_completed(db_sessionmaker, ghost, {})
    async with db_sessionmaker() as session:
        assert await TaskRepository(session).get_by_id(ghost) is None


async def test_read_task_returns_none_for_miss(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as session:
        assert await task_service.read_task(session, "nope") is None
