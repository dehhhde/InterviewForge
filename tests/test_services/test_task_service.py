"""任务进度服务〔阶段 8 · M6〕

覆盖四条约定：后台写的每一步**立即提交可见**（轮询方的全部依赖就在这里）；
失败详情的形状由异常类型决定（`AppError` 三件套 / 其余归 `internal_error`），且
`details` 落库前先过键白名单——`GET /tasks/{task_id}` 无鉴权且全量透传；
对不存在的任务行静默跳过而不是炸后台协程；
停在 `running` 的僵尸行由 `reap_stale` 收口（T-015）。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.exceptions.base import ParseError
from app.models.base import utcnow_naive
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


async def _stagnate(
    db_sessionmaker: async_sessionmaker[AsyncSession], task_id: str, *, minutes: int
) -> None:
    """把 `updated_at` 造旧 N 分钟——**不 sleep**（AGENTS.md §6 时间戳条）。

    显式赋值会盖掉 `onupdate`：列在 UPDATE 语句里出现了，SQLAlchemy 就不再自动补戳。
    """
    async with session_scope(db_sessionmaker) as session:
        await TaskRepository(session).update(
            task_id, updated_at=utcnow_naive() - timedelta(minutes=minutes)
        )


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
        db_sessionmaker,
        task_id,
        ParseError("读不出文本", details={"resume_id": "abc", "page_count": 3}),
    )
    row = await _load(db_sessionmaker, task_id)
    assert row.status == "failed"
    assert row.error == {
        "code": "parse_failed",
        "message": "读不出文本",
        "details": {"resume_id": "abc", "page_count": 3},
    }


async def test_mark_failed_keeps_only_whitelisted_detail_keys(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """白名单是默认拒绝：认不出的键只留键名，内容不进库〔T-004〕

    `upload_dir` 就是那条把服务器绝对路径透传给无鉴权 `GET /tasks/{id}` 的来路，
    这里断言它**只剩键名**——不是断言它变成什么，所以不依赖任何文案。
    """
    task_id = await _seed_task(db_sessionmaker, "resume_parsing")
    await task_service.mark_failed(
        db_sessionmaker,
        task_id,
        ParseError(
            "读不出文本",
            details={"upload_dir": "D:\\data\\uploads", "reason": "boom", "resume_id": "abc"},
        ),
    )
    row = await _load(db_sessionmaker, task_id)
    assert row.error["details"] == {"resume_id": "abc", "_dropped_keys": ["reason", "upload_dir"]}
    assert "/" not in str(row.error) and "\\" not in str(row.error)


async def test_mark_failed_drops_unsafe_values_under_listed_keys(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """键在白名单里也救不了值：带路径分隔符的字符串、以及塞进来的大对象。"""
    task_id = await _seed_task(db_sessionmaker, "resume_parsing")
    await task_service.mark_failed(
        db_sessionmaker,
        task_id,
        ParseError(
            "读不出文本",
            details={"size": "D:\\data\\x.pdf", "characters": 900, "attempts": {"last": 2}},
        ),
    )
    row = await _load(db_sessionmaker, task_id)
    assert row.error["details"] == {"characters": 900, "_dropped_keys": ["attempts", "size"]}


async def test_mark_failed_wraps_unexpected_exception(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    task_id = await _seed_task(db_sessionmaker, "resume_parsing")
    await task_service.mark_failed(db_sessionmaker, task_id, RuntimeError("池子里没连接了"))
    row = await _load(db_sessionmaker, task_id)
    assert row.error["code"] == "internal_error"
    assert "池子里没连接了" in row.error["message"]


async def test_mark_failed_logs_when_the_terminal_write_fails(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务边界用的收尾：连"报失败"都写不进库时，栈必须留在日志里〔T-009〕

    不重抛是刻意的——调用 `mark_failed_logged` 的那一层本来就是"吞掉一切并落库"，
    再往上抛只会被 Starlette 记一条没有上下文的日志。但这样一来僵尸行
    （停在 `running`）就只剩日志一个证据，所以断言的是"日志里有栈"而不是"异常没了"。
    """
    task_id = await _seed_task(db_sessionmaker, "resume_parsing")

    async def broken_mark_failed(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("库被锁住了")

    monkeypatch.setattr(task_service, "mark_failed", broken_mark_failed)
    with caplog.at_level(logging.ERROR, logger="app.services.task"):
        await task_service.mark_failed_logged(db_sessionmaker, task_id, ParseError("读不出文本"))

    assert task_id in caplog.text
    assert any(record.exc_info for record in caplog.records)
    row = await _load(db_sessionmaker, task_id)
    assert row.status == "pending"


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


# --- 僵尸任务回收（T-015 / BACKLOG OPT-4.1）---


async def test_reap_stale_flips_an_abandoned_running_row(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """进程重启留下的 `running`：翻 failed + `task_stale`，并把停滞时长留在 details 里。

    `details` 只有白名单键（`task_id` / `stale_minutes`），所以断言里没有 `_dropped_keys`——
    这条同时证明回收写库走的是同一张外出口。
    """
    task_id = await _seed_task(db_sessionmaker, "question_generation")
    await task_service.mark_running(db_sessionmaker, task_id)
    await _stagnate(db_sessionmaker, task_id, minutes=90)

    reaped = await task_service.reap_stale(db_sessionmaker)

    assert [row.task_id for row in reaped] == [task_id]
    assert reaped[0].kind == "question_generation"
    assert reaped[0].stalled_minutes >= 90
    row = await _load(db_sessionmaker, task_id)
    assert row.status == "failed"
    assert row.error["code"] == "task_stale"
    assert set(row.error["details"]) == {"task_id", "stale_minutes"}
    assert row.error["details"]["task_id"] == task_id


async def test_reap_stale_is_idempotent(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """反复跑第二次必须返回空——脚本会被人连着按，翻过的行不许再被翻一遍。"""
    task_id = await _seed_task(db_sessionmaker, "resume_parsing")
    await task_service.mark_running(db_sessionmaker, task_id)
    await _stagnate(db_sessionmaker, task_id, minutes=90)

    first = await task_service.reap_stale(db_sessionmaker)
    second = await task_service.reap_stale(db_sessionmaker)

    assert len(first) == 1
    assert second == []
    assert (await _load(db_sessionmaker, task_id)).status == "failed"


async def test_reap_stale_leaves_live_and_queued_rows_alone(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """刚写过进度的 running、还没排上的 pending、已完成的终局都不该被翻。"""
    live = await _seed_task(db_sessionmaker, "resume_parsing")
    await task_service.mark_running(db_sessionmaker, live)
    queued = await _seed_task(db_sessionmaker, "resume_parsing")
    done = await _seed_task(db_sessionmaker, "question_generation")
    await task_service.mark_running(db_sessionmaker, done)
    await task_service.mark_completed(db_sessionmaker, done, {"question_total": 15})
    await _stagnate(db_sessionmaker, live, minutes=3)
    await _stagnate(db_sessionmaker, queued, minutes=3)
    await _stagnate(db_sessionmaker, done, minutes=90)

    assert await task_service.reap_stale(db_sessionmaker, minutes=30) == []
    assert (await _load(db_sessionmaker, live)).status == "running"
    assert (await _load(db_sessionmaker, queued)).status == "pending"
    assert (await _load(db_sessionmaker, done)).status == "completed"


async def test_dry_run_names_the_rows_without_writing_them(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """`apply=False` 是脚本的默认档：改的是别人的终态，得让人先看一眼清单。"""
    task_id = await _seed_task(db_sessionmaker, "resume_parsing")
    await task_service.mark_running(db_sessionmaker, task_id)
    await _stagnate(db_sessionmaker, task_id, minutes=90)

    reaped = await task_service.reap_stale(db_sessionmaker, apply=False)

    assert [row.task_id for row in reaped] == [task_id]
    assert (await _load(db_sessionmaker, task_id)).status == "running"


async def test_reap_threshold_comes_from_the_config_file(
    db_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """阈值不是魔数：放宽 `task.stale_after_minutes` 之后，同一条 90 分钟的旧行不该再中。"""
    settings = get_settings()
    monkeypatch.setattr(settings.task, "stale_after_minutes", 200)
    task_id = await _seed_task(db_sessionmaker, "resume_parsing")
    await task_service.mark_running(db_sessionmaker, task_id)
    await _stagnate(db_sessionmaker, task_id, minutes=90)

    assert await task_service.reap_stale(db_sessionmaker) == []

    assert await task_service.reap_stale(db_sessionmaker, minutes=30) != []


async def test_list_stale_rejects_an_unknown_status(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """状态取值域由模型守着：`compleated` 这种拼写错误不该静默捞不到东西。"""
    async with db_sessionmaker() as session:
        with pytest.raises(ValueError, match="compleated"):
            await task_service.list_stale(session, statuses=("compleated",))


async def test_list_stale_orders_the_longest_stuck_first(
    db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """清单按停滞时长倒排：人只看头几行就能判断是不是同一次重启留下一地的。"""
    older = await _seed_task(db_sessionmaker, "resume_parsing")
    newer = await _seed_task(db_sessionmaker, "question_generation")
    await task_service.mark_running(db_sessionmaker, older)
    await task_service.mark_running(db_sessionmaker, newer)
    await _stagnate(db_sessionmaker, newer, minutes=70)
    await _stagnate(db_sessionmaker, older, minutes=180)

    async with db_sessionmaker() as session:
        found = await task_service.list_stale(session, minutes=30)

    assert [row.id for row in found] == [older, newer]
