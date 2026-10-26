"""任务进度的服务层：API 建行/读进度 + 后台任务写进度〔阶段 8 · M6〕

分层口径（AGENTS.md §3）：这里不写 SQL，全部走 `TaskRepository`；提交时机也归这里，
因为**后台任务的每一步都要立刻可见**——轮询方隔一秒读一次，看不到中途状态
就等于没有进度。所以写进度的四个函数各自开一个 `session_scope`（一次小事务），
而不是像 `persist_report` 那样攒一个跨表大事务：报告要"要么全有要么全无"，
进度只要"看到哪算哪"。

`AppError` 的失败详情只落 `{code, message, details}` 三个字段——异常里可能带
原始响应或文件路径，`details` 由抛出方显式构造，本来就该是脱敏过的。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.exceptions.base import AppError
from app.models.task import TASK_KINDS, Task
from app.repositories.task_repository import TaskRepository
from app.utils.db import session_scope


async def create_task(session: AsyncSession, *, kind: str, ref_id: str | None) -> Task:
    """API 在返回响应前插任务行（`pending`）。只 flush，提交归 `get_db`。"""
    if kind not in TASK_KINDS:
        raise ValueError(f"未知的任务类型：{kind!r}，可用：{TASK_KINDS}")
    return await TaskRepository(session).create(kind=kind, status="pending", ref_id=ref_id)


async def read_task(session: AsyncSession, task_id: str) -> Task | None:
    """轮询读取。找不到返回 None，404 是 API 层的语义。"""
    return await TaskRepository(session).get_by_id(task_id)


async def mark_running(sessionmaker: async_sessionmaker[AsyncSession], task_id: str) -> None:
    """后台协程真正开始执行时翻 `pending -> running`。

    行不存在时静默跳过（`finish` 同理）：任务行是 API 响应事务里提交的，API 回滚了
    而后台已经排上执行是可能发生的（同一请求内 background 在响应后才跑，若响应事务
    提交失败）。给这种竞态抛异常没有意义，进度丢了就丢了，业务行本来也不存在。
    """
    async with session_scope(sessionmaker) as session:
        await TaskRepository(session).set_status(task_id, "running")


async def record_step(
    sessionmaker: async_sessionmaker[AsyncSession],
    task_id: str,
    node: str,
    *,
    ms: float | None = None,
) -> None:
    async with session_scope(sessionmaker) as session:
        repo = TaskRepository(session)
        step: dict[str, Any] = {"node": node}
        if ms is not None:
            step["ms"] = ms
        await repo.append_step(task_id, step)


async def mark_completed(
    sessionmaker: async_sessionmaker[AsyncSession],
    task_id: str,
    result: dict[str, Any],
) -> None:
    async with session_scope(sessionmaker) as session:
        await TaskRepository(session).finish(task_id, "completed", result=result)


async def mark_failed(
    sessionmaker: async_sessionmaker[AsyncSession],
    task_id: str,
    exc: BaseException,
) -> None:
    if isinstance(exc, AppError):
        error: dict[str, Any] = {"code": exc.code, "message": exc.message, "details": exc.details}
    else:
        error = {
            "code": "internal_error",
            "message": f"{type(exc).__name__}: {exc}"[:500],
            "details": {},
        }
    async with session_scope(sessionmaker) as session:
        await TaskRepository(session).finish(task_id, "failed", error=error)
