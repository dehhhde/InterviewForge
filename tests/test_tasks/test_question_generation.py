"""`run_question_generation`：后台跑图 + 每步进度落库〔M6〕

节点表用 `tests.test_agent_infra` 的假节点骨架（图的连通性属 `test_agent_infra` 与
M4/M5 图级用例的活）；这里验的是**跑图外面的那层壳**——super-step 到 `task.progress`
的映射、失败时 `task`/`interview` 两行的收尾、成本护栏在生产装配里确实接线。
checkpoint 落 `tmp_path`，不碰 `data/checkpoints.db`。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.nodes.generate_node import make_generate_node
from app.agents.state import NodeName
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
    assert [step["node"] for step in task.progress["steps"]] == ["parse"]
    assert head.status == "aborted"


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
