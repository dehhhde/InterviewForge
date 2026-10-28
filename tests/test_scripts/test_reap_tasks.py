"""僵尸任务回收脚本〔T-015〕

只断言两件事：**默认档不动库**、`--apply` 才动，以及清单里出现的标识是那两行 id。
控制台是 GBK，所以用例不断言中文提示的具体字形，按 id 命中即可。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.base import utcnow_naive
from app.repositories.task_repository import TaskRepository
from app.scripts.reap_tasks import main, reap
from app.services import task_service
from app.utils.db import session_scope


async def _stagnant_running(
    db_sessionmaker: async_sessionmaker[AsyncSession], *, minutes: int = 90
) -> str:
    async with session_scope(db_sessionmaker) as session:
        task = await task_service.create_task(session, kind="resume_parsing", ref_id="ref-9")
        task_id = task.id
    await task_service.mark_running(db_sessionmaker, task_id)
    async with session_scope(db_sessionmaker) as session:
        await TaskRepository(session).update(
            task_id, updated_at=utcnow_naive() - timedelta(minutes=minutes)
        )
    return task_id


async def test_reap_defaults_to_dry_run_and_prints_the_row(
    db_sessionmaker: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = await _stagnant_running(db_sessionmaker)

    reaped = await reap(db_sessionmaker, apply=False, minutes=30)

    assert [row.task_id for row in reaped] == [task_id]
    assert task_id in capsys.readouterr().out
    async with db_sessionmaker() as session:
        row = await task_service.read_task(session, task_id)
    assert row is not None and row.status == "running"


async def test_reap_apply_writes_the_terminal_state(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    task_id = await _stagnant_running(db_sessionmaker)

    await reap(db_sessionmaker, apply=True, minutes=30)
    again = await reap(db_sessionmaker, apply=True, minutes=30)

    async with db_sessionmaker() as session:
        row = await task_service.read_task(session, task_id)
    assert row is not None
    assert (row.status, row.error["code"]) == ("failed", "task_stale")
    assert again == [], "第二次没有可翻的行：反复跑同一份库不会越清越多"


def test_main_defaults_to_dry_run_without_the_apply_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`main([])` 走的是 `--apply` 缺省值 False：入口的默认必须是保守的那一档。"""
    seen: dict[str, object] = {}

    async def fake_reap(
        sessionmaker: object, *, apply: bool, minutes: int | None
    ) -> list[object]:
        seen["apply"] = apply
        seen["minutes"] = minutes
        return []

    async def fake_dispose() -> None:
        return None

    monkeypatch.setattr("app.scripts.reap_tasks.reap", fake_reap)
    monkeypatch.setattr("app.scripts.reap_tasks.dispose_engine", fake_dispose)

    main([])

    assert seen == {"apply": False, "minutes": None}
