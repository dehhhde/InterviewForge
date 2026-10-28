"""`run_question_generation`：后台跑图 + 每步进度落库〔M6〕

节点表用 `tests.test_agent_infra` 的假节点骨架（图的连通性属 `test_agent_infra` 与
M4/M5 图级用例的活）；这里验的是**跑图外面的那层壳**——super-step 到 `task.progress`
的映射、失败时 `task`/`interview` 两行的收尾、成本护栏在生产装配里确实接线。
checkpoint 落 `tmp_path`，不碰 `data/checkpoints.db`。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.nodes.generate_node import make_generate_node
from app.agents.state import NodeName
from app.agents.workflow import read_thread_state
from app.config import get_settings
from app.exceptions.base import BudgetExceededError, ValidationFailedError
from app.repositories.interview_repository import InterviewRepository
from app.repositories.resume_repository import ResumeRepository
from app.services import task_service
from app.tasks.question_generation import make_live_nodes, run_question_generation
from app.utils.db import session_scope
from app.utils.llm_client import LLMClient
from tests.test_agent_infra import _nodes
from tests.test_agents._fake_llm import FakeCompletions

_MAIN_STEPS = ["parse", "validate", "match", "profile", "plan", "generate", "review"]
_PLAN = {
    "items": [{"topic": "并发", "difficulty": "medium", "count": 1, "source": "generic"}],
    "total": 1,
}


async def _seed(db_sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[str, str, str]:
    async with session_scope(db_sessionmaker) as session:
        resume = await ResumeRepository(session).create(stored_path="a.pdf", file_name="a.pdf")
        head = await InterviewRepository(session).create(
            resume_id=resume.id, plan={}, status="running"
        )
        task = await task_service.create_task(
            session, kind="question_generation", ref_id=head.id
        )
        return str(resume.id), str(head.id), str(task.id)


async def _rows(
    db_sessionmaker: async_sessionmaker[AsyncSession], interview_id: str, task_id: str
) -> tuple[Any, Any]:
    async with db_sessionmaker() as session:
        head = await InterviewRepository(session).get_by_id(interview_id)
        task = await task_service.read_task(session, task_id)
    assert head is not None and task is not None
    return head, task


async def test_happy_path_records_every_superstep_and_completes(
    db_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    resume_id, interview_id, task_id = await _seed(db_sessionmaker)

    out = await run_question_generation(
        task_id,
        resume_id,
        interview_id,
        "需要 Redis 与并发经验",
        sessionmaker=db_sessionmaker,
        nodes=_nodes(),
        checkpoint_path=tmp_path / "ckpt.db",
    )

    assert out["ok"] is True
    head, task = await _rows(db_sessionmaker, interview_id, task_id)
    # 假节点不写通道：`finished` 不成立，任务却是 completed——"跑完了"与"跑合格"是两件事
    assert task.status == "completed"
    assert [step["node"] for step in task.progress["steps"]] == _MAIN_STEPS
    assert task.result["finished"] is False and task.result["question_total"] == 0
    # 假收尾不落题：报告头仍挂在 `running`，等真 `review` 的终局判定
    assert head.status == "running"


async def test_the_callers_thread_id_owns_the_checkpoint_run(
    db_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    """T-304：调用方给的线程 id 就是这次 checkpoint 的归属，导出据此读单线程。

    以前这里每次随机 uuid 且不回写，"那次运行的图终态在哪个线程"跑完就没人知道了，
    于是只能扫全库。现在 HTTP 侧传的是 `interview.thread_id`（开跑前已落库）。
    """
    resume_id, interview_id, task_id = await _seed(db_sessionmaker)
    ckpt = tmp_path / "ckpt.db"

    await run_question_generation(
        task_id,
        resume_id,
        interview_id,
        "需要 Redis 与并发经验",
        sessionmaker=db_sessionmaker,
        nodes=_nodes(),
        checkpoint_path=ckpt,
        thread_id="thread-304",
    )

    values = await read_thread_state("thread-304", checkpoint_path=ckpt)
    assert values["interview_id"] == interview_id
    assert [step["node"] for step in values["trace"]] == _MAIN_STEPS
    # 别的线程读不到这份：寻址是真的，不是"返回最近一次运行"
    assert await read_thread_state("thread-other", checkpoint_path=ckpt) == {}


@pytest.mark.parametrize(("payload", "expected"), [({}, False), ({"auto_fix": True}, True)])
async def test_the_run_hands_the_redo_authorization_to_the_graph(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Any,
    payload: dict[str, Any],
    expected: bool,
) -> None:
    """`auto_fix` 是**调用方给的键**，不是这里的判据〔T-404〕：外壳只把它送进初始 state。

    默认那一路也钉住（不传键 = `False`）：定向重做要多烧一轮扇出，
    要是后台自己把它打开，"一次成卷"就成了没人签字的默认行为。
    """
    resume_id, interview_id, task_id = await _seed(db_sessionmaker)
    seen: list[Any] = []

    async def spy(state: dict[str, Any]) -> dict[str, Any]:
        seen.append(state.get("auto_fix"))
        return {}

    await run_question_generation(
        task_id,
        resume_id,
        interview_id,
        "JD",
        sessionmaker=db_sessionmaker,
        nodes=_nodes(generate=spy),
        checkpoint_path=tmp_path / "ckpt.db",
        **payload,
    )

    assert seen == [expected]


async def test_node_crash_fails_task_and_aborts_the_still_running_report(
    db_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    resume_id, interview_id, task_id = await _seed(db_sessionmaker)

    async def exploding(state: dict[str, Any]) -> dict[str, Any]:
        raise ValidationFailedError("抽取结果不可用", details={"why": "test"})

    out = await run_question_generation(
        task_id,
        resume_id,
        interview_id,
        "JD",
        sessionmaker=db_sessionmaker,
        nodes=_nodes(validate=exploding),
        checkpoint_path=tmp_path / "ckpt.db",
    )

    assert out["ok"] is False
    assert isinstance(out["error"], ValidationFailedError)
    head, task = await _rows(db_sessionmaker, interview_id, task_id)
    assert task.status == "failed" and task.error["code"] == "validation_failed"
    # 中止那一步也进进度（T-008）：`traced()` 把失败步骤挂在异常上，外壳在这里把它落成一步，
    # 否则"停在哪个节点、什么码"只能从错误正文里猜。成功步不带 `ok` 键，失败步才显式写 False。
    steps = task.progress["steps"]
    assert [step["node"] for step in steps] == ["parse", "validate"]
    assert "ok" not in steps[0]
    assert steps[-1]["ok"] is False and steps[-1]["detail"] == "validation_failed"
    assert head.status == "aborted"


async def test_boundary_logs_a_terminal_write_that_failed(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Any,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收尾写库自己炸了：任务停在 running，但补刀照旧跑、栈留在日志里〔T-009〕

    这条是"边界吞掉异常"的第二层：`mark_failed` 失败时既不重抛（没人接），也不能静默——
    否则僵尸任务行和挂着 running 的报告头一起变成无人知晓的暗雷。
    """
    resume_id, interview_id, task_id = await _seed(db_sessionmaker)

    async def exploding(state: dict[str, Any]) -> dict[str, Any]:
        raise ValidationFailedError("半路崩了")

    async def broken_mark_failed(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("库被锁住了")

    monkeypatch.setattr(task_service, "mark_failed", broken_mark_failed)
    with caplog.at_level(logging.ERROR, logger="app.services.task"):
        out = await run_question_generation(
            task_id,
            resume_id,
            interview_id,
            "JD",
            sessionmaker=db_sessionmaker,
            nodes=_nodes(validate=exploding),
            checkpoint_path=tmp_path / "ckpt.db",
        )

    assert out["ok"] is False
    assert any(record.exc_info for record in caplog.records)
    head, task = await _rows(db_sessionmaker, interview_id, task_id)
    assert task.status == "running"
    assert head.status == "aborted"


async def test_abort_that_cannot_be_written_is_logged(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Any,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """补刀写不进库时报告头停在 running：原来的 `except: pass` 什么也没留下，现在留栈〔T-009〕"""
    resume_id, interview_id, task_id = await _seed(db_sessionmaker)

    async def exploding(state: dict[str, Any]) -> dict[str, Any]:
        raise ValidationFailedError("半路崩了")

    async def broken_set_status(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("库被锁住了")

    monkeypatch.setattr(InterviewRepository, "set_status", broken_set_status)
    with caplog.at_level(logging.ERROR, logger="app.tasks.question_generation"):
        out = await run_question_generation(
            task_id,
            resume_id,
            interview_id,
            "JD",
            sessionmaker=db_sessionmaker,
            nodes=_nodes(validate=exploding),
            checkpoint_path=tmp_path / "ckpt.db",
        )

    assert out["ok"] is False
    assert interview_id in caplog.text


async def test_a_completed_run_whose_terminal_write_fails_is_logged(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Any,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """图跑完了、终态没写进去：返回值仍是成功，日志里有栈〔T-009〕

    原来这两笔（`record_step` 之后的 `mark_completed`）裸在 `try` 外面，DB 一抖就冒出
    后台协程：报告头已经 `completed`，任务却停在 `running`，两边各自挂着都没留痕迹。
    """
    resume_id, interview_id, task_id = await _seed(db_sessionmaker)

    async def broken_mark_completed(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("库被锁住了")

    monkeypatch.setattr(task_service, "mark_completed", broken_mark_completed)
    with caplog.at_level(logging.ERROR, logger="app.tasks.question_generation"):
        out = await run_question_generation(
            task_id,
            resume_id,
            interview_id,
            "JD",
            sessionmaker=db_sessionmaker,
            nodes=_nodes(),
            checkpoint_path=tmp_path / "ckpt.db",
        )

    assert out["ok"] is True
    assert any(record.exc_info for record in caplog.records)
    _, task = await _rows(db_sessionmaker, interview_id, task_id)
    assert task.status == "running"


async def test_crash_never_rewrites_an_already_terminal_report(
    db_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    """补刀只翻仍挂着的 `running`：`review` 已判过的终局不被外壳覆盖。"""
    resume_id, interview_id, task_id = await _seed(db_sessionmaker)
    async with session_scope(db_sessionmaker) as session:
        await InterviewRepository(session).set_status(interview_id, "completed")

    async def exploding(state: dict[str, Any]) -> dict[str, Any]:
        raise ValidationFailedError("半路崩了")

    await run_question_generation(
        task_id,
        resume_id,
        interview_id,
        "JD",
        sessionmaker=db_sessionmaker,
        nodes=_nodes(review=exploding),
        checkpoint_path=tmp_path / "ckpt.db",
    )
    head, task = await _rows(db_sessionmaker, interview_id, task_id)
    assert head.status == "completed"
    assert task.status == "failed"


async def test_cost_guard_trips_inside_the_graph_run(
    db_sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    """图内视角：`generate` 抛的 `BudgetExceededError` 被外壳记成 failed + aborted。"""
    settings = get_settings().model_copy(deep=True)
    settings.agent.max_tokens_per_run = 10
    client = LLMClient(settings=settings, client=FakeCompletions([]))
    client.usage.record("generate-slot", 8, 8)

    resume_id, interview_id, task_id = await _seed(db_sessionmaker)

    async def plan_writer(state: dict[str, Any]) -> dict[str, Any]:
        # 假 `plan` 节点把配额表放进 state：`generate` 的护栏在"无配额"判定**之后**，
        # 没有这张表就先撞 `generate_no_plan`，测不到预算分支。
        return {"plan": dict(_PLAN)}

    out = await run_question_generation(
        task_id,
        resume_id,
        interview_id,
        "JD",
        sessionmaker=db_sessionmaker,
        nodes=_nodes(plan=plan_writer, generate=make_generate_node(llm=client, settings=settings)),
        checkpoint_path=tmp_path / "ckpt.db",
    )

    assert out["ok"] is False
    head, task = await _rows(db_sessionmaker, interview_id, task_id)
    assert task.error["code"] == "cost_budget_exceeded"
    assert task.error["details"]["used_tokens"] == 16
    assert head.status == "aborted"


async def test_production_assembly_wires_the_guard(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """装配表漏接 `llm` 给 `generate`，护栏就成了摆设——这里把接线钉死。"""
    settings = get_settings().model_copy(deep=True)
    settings.agent.max_tokens_per_run = 10
    client = LLMClient(settings=settings, client=FakeCompletions([]))
    client.usage.record("generate-slot", 8, 8)

    nodes = make_live_nodes(client, db_sessionmaker, settings=settings)
    try:
        await nodes[NodeName.GENERATE]({"plan": _PLAN})  # type: ignore[misc]
        raise AssertionError("预算越线却没有中断，说明护栏没接进装配表")
    except BudgetExceededError as exc:
        assert exc.code == "cost_budget_exceeded"


async def test_client_built_by_the_run_is_closed_in_finally(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行自建的真客户端在收尾被关闭，失败图也关〔T-011 / BACKLOG P1-6〕

    注入 `llm` 的那条路不关（调用方管替身），所以这里走"不给 nodes、不给 llm"的生产装配，
    并把模块里的 `LLMClient` 换成会记关闭次数的替身——真构造会读 `.env` 的密钥，
    本用例只关心生命周期收尾有没有执行。
    """
    from app.tasks import question_generation as qg_module

    class _RecordingClient:
        def __init__(self, settings: Any = None) -> None:
            self.settings = settings or get_settings()
            self.usage = LLMClient(settings=self.settings, client=FakeCompletions([])).usage
            self.closed = 0

        async def aclose(self) -> None:
            self.closed += 1

    created: list[_RecordingClient] = []

    def factory(settings: Any = None) -> _RecordingClient:
        client = _RecordingClient(settings)
        created.append(client)
        return client

    monkeypatch.setattr(qg_module, "LLMClient", factory)

    resume_id, interview_id, task_id = await _seed(db_sessionmaker)
    out = await run_question_generation(
        task_id,
        resume_id,
        interview_id,
        "JD",
        sessionmaker=db_sessionmaker,
        checkpoint_path=tmp_path / "ckpt.db",
    )

    assert out["ok"] is False
    assert len(created) == 1 and created[0].closed == 1
