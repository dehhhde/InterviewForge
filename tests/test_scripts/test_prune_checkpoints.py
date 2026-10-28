"""checkpoint 清理判据：认领、保留窗口、running 保护、无主要显式授权〔S3 · T-311〕

`data/checkpoints.db` 是这台机器上真跑过的 13 份报告，测试一律把 `workflow.CHECKPOINT_DB`
指到 `tmp_path`——它同时是 `read_thread_state` 默认打开的那个库，两处不指到同一个文件，
"清单里的线程去库里查不到"就会变成一个只有 CI 换机器才会撞的 bug。

线程不是靠"文件里的行"被测出来的，是靠**真跑一次图**写出来的（`_write_checkpoint`）：
自己 `INSERT INTO checkpoints` 就是在照抄 langgraph 的表格式，而它没有兼容性承诺。
只有造假 id 那一条例外，因为它测的正是"格式不对时不许猜时钟"。
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.agents import workflow
from app.agents.workflow import (
    build_agent_graph,
    make_thread_config,
    open_checkpointer,
    read_thread_state,
)
from app.config import get_settings
from app.dependencies import dispose_engine, get_engine, get_sessionmaker
from app.models import Base
from app.models.base import utcnow_naive
from app.repositories.interview_repository import InterviewRepository
from app.repositories.resume_repository import ResumeRepository
from app.scripts.prune_checkpoints import (
    DEFAULT_KEEP_DAYS,
    KEEP,
    ORPHAN,
    PROTECTED,
    PRUNE,
    _parse_args,
    _reports,
    classify,
    prune,
)
from app.utils.db import session_scope
from tests.test_agent_infra import _nodes

_PLAN: dict[str, Any] = {"total": 1, "items": [{"topic": "限流", "count": 1, "source": "resume"}]}


@pytest.fixture(autouse=True)
async def _isolated_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """业务库与 checkpoint 库都指到本用例的 `tmp_path`，并建好表。"""
    settings = get_settings()
    monkeypatch.setattr(
        settings, "database_url", f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    )
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(workflow, "CHECKPOINT_DB", tmp_path / "data" / "checkpoints.db")

    await dispose_engine()
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


async def _seed_report(
    *,
    thread_id: str = "",
    status: str = "completed",
    name: str = "林可",
    age_days: int = 0,
) -> str:
    async with session_scope(get_sessionmaker()) as session:
        resume = await ResumeRepository(session).create(
            name=name, raw_text=f"{name} 的简历原文", file_name=f"{name}.pdf"
        )
        head = await InterviewRepository(session).create(
            resume_id=resume.id,
            plan=_PLAN,
            status=status,
            thread_id=thread_id,
            resume_name=name,
            job_title="后端工程师",
            total_questions=1,
            updated_at=utcnow_naive() - timedelta(days=age_days),
        )
        return head.id


async def _write_checkpoint(thread_id: str, interview_id: str = "") -> None:
    """真跑一次图（假节点），把这个线程的终态留在 checkpoint 库里。

    `interview_id` 写进 state 是为了钉住旧报告那条路：T-304 之前落库的行 `thread_id`
    是空串，脚本只能靠正文里这个字段认领线程。
    """

    async def write_ids(state: dict[str, Any]) -> dict[str, Any]:
        return {"interview_id": interview_id}

    async with open_checkpointer(workflow.CHECKPOINT_DB) as saver:
        graph = build_agent_graph(_nodes(parse=write_ids), checkpointer=saver)
        await graph.ainvoke({}, make_thread_config(thread_id, recursion_limit=30))


def _thread_count(path: Path) -> int:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        found = connection.execute("select count(distinct thread_id) from checkpoints").fetchone()
        return int(found[0])
    finally:
        connection.close()


# --- 判据：四档各就各位 ---


async def test_a_terminal_thread_past_the_window_is_the_only_prunable_one() -> None:
    old = await _seed_report(thread_id="thread-old", age_days=400)
    fresh = await _seed_report(thread_id="thread-fresh")
    await _write_checkpoint("thread-old", old)
    await _write_checkpoint("thread-fresh", fresh)

    audits = await classify(get_sessionmaker(), keep_days=90)
    by_thread = {audit.thread_id: audit for audit in audits}

    assert by_thread["thread-old"].verdict == PRUNE
    assert by_thread["thread-old"].interview_id == old
    assert by_thread["thread-fresh"].verdict == KEEP


async def test_a_running_reports_thread_is_never_prunable() -> None:
    """正在跑的那次运行靠父 checkpoint 走下一步：删它等于把运行中的图判死刑。"""
    head = await _seed_report(thread_id="thread-live", status="running")
    await _write_checkpoint("thread-live", head)

    audits = await classify(get_sessionmaker(), keep_days=0)
    assert audits[0].verdict == PROTECTED

    assert await prune(get_sessionmaker(), keep_days=0, apply=True) == []
    assert await read_thread_state("thread-live") != {}


async def test_the_aborted_branch_is_treated_as_finished() -> None:
    """`aborted` 是终态：那次运行不会再回到这条线上，留着只是占体积。"""
    head = await _seed_report(thread_id="thread-dead", status="aborted", age_days=200)
    await _write_checkpoint("thread-dead", head)

    audits = await classify(get_sessionmaker(), keep_days=90)
    assert audits[0].verdict == PRUNE


# --- 认领：先看行，认不上才读正文 ---


async def test_a_legacy_report_is_claimed_by_its_checkpoint_state() -> None:
    """`thread_id` 是空串的历史报告不是垃圾。

    本机实测 13 份全是这个形状。把它们判成"无主"并删掉，等于一次性废掉这些报告
    重新导出图终态的能力——而 `export_report_md.py` 兜底那条路读的正是这份 state。
    """
    head = await _seed_report(thread_id="", age_days=400)
    await _write_checkpoint("thread-legacy", head)

    audits = await classify(get_sessionmaker(), keep_days=90)
    audit = audits[0]
    assert audit.interview_id == head
    assert audit.status == "completed"
    # 认得回来，所以走的是"终态 + 超窗"那档，而不是 `--include-orphans` 才动得了的无主档
    assert audit.verdict == PRUNE


async def test_an_unclaimed_thread_waits_for_explicit_authorization() -> None:
    await _write_checkpoint("thread-garbage", "iv-not-in-the-business-db")

    audits = await classify(get_sessionmaker(), keep_days=0)
    assert audits[0].verdict == ORPHAN
    assert audits[0].interview_id == ""

    # 默认：报数不删
    assert await prune(get_sessionmaker(), keep_days=0, apply=True) == []
    assert await read_thread_state("thread-garbage") != {}

    picked = await prune(get_sessionmaker(), keep_days=0, apply=True, include_orphans=True)
    assert [audit.thread_id for audit in picked] == ["thread-garbage"]
    assert await read_thread_state("thread-garbage") == {}


# --- dry-run 与 --apply ---


async def test_dry_run_reports_the_list_but_touches_nothing() -> None:
    head = await _seed_report(thread_id="thread-scan", age_days=400)
    await _write_checkpoint("thread-scan", head)

    chosen = await prune(get_sessionmaker(), keep_days=90, apply=False)
    assert [audit.thread_id for audit in chosen] == ["thread-scan"]
    assert await read_thread_state("thread-scan") != {}
    assert _thread_count(Path(workflow.CHECKPOINT_DB)) == 1


async def test_apply_removes_exactly_the_chosen_thread() -> None:
    old = await _seed_report(thread_id="thread-old", age_days=400)
    live = await _seed_report(thread_id="thread-live", status="running")
    await _write_checkpoint("thread-old", old)
    await _write_checkpoint("thread-live", live)

    chosen = await prune(get_sessionmaker(), keep_days=90, apply=True)
    assert [audit.thread_id for audit in chosen] == ["thread-old"]

    assert await read_thread_state("thread-old") == {}
    assert await read_thread_state("thread-live") != {}
    assert _thread_count(Path(workflow.CHECKPOINT_DB)) == 1


async def test_deleting_clears_the_attached_writes_too() -> None:
    """删必须走 `adelete_thread`：只清 `checkpoints` 会把 `writes` 的孤儿行留在库里。"""
    head = await _seed_report(thread_id="thread-old", age_days=400)
    await _write_checkpoint("thread-old", head)
    path = Path(workflow.CHECKPOINT_DB)

    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    before = int(connection.execute("select count(*) from writes").fetchone()[0])
    connection.close()
    assert before > 0

    await prune(get_sessionmaker(), keep_days=90, apply=True)

    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        assert int(connection.execute("select count(*) from writes").fetchone()[0]) == 0
    finally:
        connection.close()


async def test_vacuum_does_not_lose_the_kept_thread(tmp_path: Path) -> None:
    """`--vacuum` 只动文件布局，不许动内容：留下的线程必须还读得出来。"""
    old = await _seed_report(thread_id="thread-old", age_days=400)
    live = await _seed_report(thread_id="thread-live", status="running")
    await _write_checkpoint("thread-old", old)
    await _write_checkpoint("thread-live", live)

    await prune(get_sessionmaker(), keep_days=90, apply=True, vacuum=True)

    assert await read_thread_state("thread-old") == {}
    state = await read_thread_state("thread-live")
    assert state.get("interview_id") == live


# --- 时钟与读取范围 ---


async def test_a_non_uuid6_checkpoint_id_refuses_to_guess_its_age() -> None:
    """解不出年龄就抛错，而不是"当成很旧"。判错的代价是删数据。"""
    head = await _seed_report(thread_id="thread-old", age_days=400)
    await _write_checkpoint("thread-old", head)
    path = Path(workflow.CHECKPOINT_DB)

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "insert into checkpoints (thread_id, checkpoint_ns, checkpoint_id,"
            " parent_checkpoint_id, type, checkpoint, metadata) values (?,?,?,?,?,?,?)",
            ("thread-fake", "", uuid.uuid4().hex, "", "msg", "{}", "{}"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="UUIDv6"):
        await classify(get_sessionmaker(), keep_days=90)


async def test_every_report_is_read_even_past_the_default_page_size() -> None:
    """漏读一份报告，它的线程就看起来无主——所以 `_reports` 不能有隐式上限。"""
    for index in range(60):
        await _seed_report(thread_id=f"thread-{index}", name=f"林{index}")

    assert len(await _reports(get_sessionmaker())) == 60


async def test_a_missing_checkpoint_library_is_nothing_to_do(tmp_path: Path) -> None:
    assert await classify(get_sessionmaker(), checkpoint_path=tmp_path / "none.db") == []
    assert await prune(get_sessionmaker(), checkpoint_path=tmp_path / "none.db") == []


# --- 命令行外壳 ---


def test_dry_run_and_a_generous_window_are_the_defaults() -> None:
    args = _parse_args([])
    assert args.apply is False
    assert args.include_orphans is False
    assert args.vacuum is False
    assert args.keep_days == DEFAULT_KEEP_DAYS


async def test_the_summary_line_names_all_four_verdicts(capsys: pytest.CaptureFixture[str]) -> None:
    old = await _seed_report(thread_id="thread-old", age_days=400)
    fresh = await _seed_report(thread_id="thread-fresh")
    live = await _seed_report(thread_id="thread-live", status="running")
    for thread, head in [("thread-old", old), ("thread-fresh", fresh), ("thread-live", live)]:
        await _write_checkpoint(thread, head)
    await _write_checkpoint("thread-garbage", "")

    await prune(get_sessionmaker(), keep_days=90, apply=False)
    printed = capsys.readouterr().out
    assert "prune=1 keep=1 protected=1 orphan=1" in printed
    assert "--include-orphans" in printed
