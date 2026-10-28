"""面试题生成任务：asyncio.run 跑 LangGraph + 进度回报〔阶段 8 · §11.3〕

跑的是完整七节点图（parse → … → review），落库仍只在 `review` 的收尾出口——
本模块只负责"让图转起来 + 把每一步记到 `task.progress`"，不碰业务写库。

进度来源是 `astream(stream_mode="updates")`：每个 super-step 吐一份
`{节点名: 通道增量}`，分支并行时同一轮 `generate_item` 会并成一个条目，正好按"阶段"
记。跑完用 `aget_state` 拿终态——`astream` 只给增量，最终 `questions` 要靠它。
用 `ainvoke` 当然更简单，但整个图跑完前轮询方只能看到 `running`，
"进度写库"这条验收就落空了。

失败处理分两本账：
- `task` 行记错误（`mark_failed`），轮询方看得到为什么停了；
- `interview` 行只有在仍是 `running` 时才翻成 `aborted`——ADR-0015 之后 `review` 的收尾
  只会写 `completed`，所以挂着的就是"中途崩了、没人跑到终点"，补刀才轮得到这里。
和 `resume_parsing` 一样，异常吞在任务边界内：后台协程抛出去只会被记日志，没人接。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.nodes.generate_node import make_generate_item_node, make_generate_node
from app.agents.nodes.match_node import make_match_node
from app.agents.nodes.parse_node import make_parse_node
from app.agents.nodes.plan_node import make_plan_node
from app.agents.nodes.profile_node import make_profile_node
from app.agents.nodes.review_node import make_review_node
from app.agents.nodes.validate_node import make_validate_node
from app.agents.state import NodeName
from app.agents.workflow import (
    FanoutNode,
    Node,
    build_agent_graph,
    failed_trace_step,
    make_thread_config,
    open_checkpointer,
)
from app.config import Settings, get_settings
from app.dependencies import get_sessionmaker
from app.repositories.interview_repository import InterviewRepository
from app.services import task_service
from app.utils.db import session_scope
from app.utils.llm_client import LLMClient

logger = logging.getLogger("app.tasks.question_generation")


def make_live_nodes(
    llm: LLMClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    settings: Settings | None = None,
) -> dict[NodeName, Node | FanoutNode]:
    """装配真节点。全部依赖在装配期注入，节点内部不 new client（AGENTS.md §3）。

    放在 tasks 层而不是 workflow 层：workflow 只提供组装设施，不认识任何具体节点实现；
    而"哪些实现算生产装配"是部署决策。`scripts/run_e2e_live.py` 与 `scripts/eval_e2e.py`
    都复用这张表，`generate` 因此同样挂上 M6 成本护栏（它只读 `llm.usage`，自己一次调用都不花）。
    """
    return {
        NodeName.PARSE: make_parse_node(llm=llm, settings=settings),
        NodeName.VALIDATE: make_validate_node(llm=llm, settings=settings),
        NodeName.MATCH: make_match_node(llm=llm, settings=settings),
        NodeName.PROFILE: make_profile_node(llm=llm, settings=settings),
        NodeName.PLAN: make_plan_node(settings=settings),
        NodeName.GENERATE: make_generate_node(llm=llm, settings=settings),
        NodeName.GENERATE_ITEM: make_generate_item_node(llm=llm, settings=settings),
        NodeName.REVIEW: make_review_node(llm=llm, settings=settings, sessionmaker=sessionmaker),
    }


async def run_question_generation(
    task_id: str,
    resume_id: str,
    interview_id: str,
    jd_text: str,
    *,
    llm: LLMClient | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    nodes: Mapping[NodeName, Node | FanoutNode] | None = None,
    checkpoint_path: str | Path | None = None,
    thread_id: str | None = None,
    auto_fix: bool = False,
) -> dict[str, Any]:
    """后台跑完整工作流，进度逐步写 `task.progress`。

    `interview` 行必须由调用方在调度前建好（`status=running`），`interview_id` 从初始
    state 传给 `review` 收尾。`nodes` 与 `llm` 是测试/脚本注入口：缺省用生产装配 + 真
    client。返回图的终态（`scripts/eval_e2e.py` 靠它算三指标），API 的后台调用不看返回值。

    `thread_id` 由调用方给（HTTP 侧就是 `interview.thread_id`，开跑前已落库），这样
    "报告的图终态在哪个线程"是一个查得到的事实而不是只有运行时知道〔T-304〕；
    不给就退回每次随机的老行为（`scripts/run_e2e_live.py` 这类不建报告线程归属的入口）。

    `auto_fix` 是**调用方授权的定向重做**〔T-404 / ADR-0021〕，原样进初始 state：
    它不是本模块的判据，只是那条授权在 checkpoint 里可读的落点。开一轮就多一轮扇出，
    所以默认关，且成本护栏（`agent.max_tokens_per_run`）照旧在 `generate` 扇出前判。
    """
    settings = get_settings()
    own_client = llm is None
    sessions = get_sessionmaker() if sessionmaker is None else sessionmaker
    client = llm or LLMClient(settings)
    try:
        if nodes is None:
            live_nodes = make_live_nodes(client, sessions, settings=settings)
        else:
            live_nodes = dict(nodes)

        await task_service.mark_running(sessions, task_id)
        final_state: dict[str, Any] = {}
        try:
            # `open_checkpointer` 是 async context：图崩在中途时 checkpoint 停在失败前一步，
            # 与 workflow.traced() 的"异常不写 state"约定接得上（计划书 §4 契约 4）。
            async with open_checkpointer(checkpoint_path) as saver:
                graph = build_agent_graph(live_nodes, checkpointer=saver)
                config = make_thread_config(thread_id)
                async for chunk in graph.astream(
                    {
                        "resume_id": resume_id,
                        "interview_id": interview_id,
                        "jd_text": jd_text,
                        "auto_fix": auto_fix,
                    },
                    config,
                    stream_mode="updates",
                ):
                    await _record_updates(sessions, task_id, chunk)
                snapshot = await graph.aget_state(config)
                final_state = dict(snapshot.values)
        except Exception as exc:  # 任务边界兜一切：错误落 task 行，不再上抛
            await _record_failed_step(sessions, task_id, exc)
            await task_service.mark_failed_logged(sessions, task_id, exc)
            await _abort_interview(sessions, interview_id)
            return {"ok": False, "error": exc}

        await _record_completed(sessions, task_id, interview_id, final_state)
        return {"ok": True, "state": final_state, "usage": client.usage}
    finally:
        # 只关自己建的那个：`llm` 是调用方（测试/脚本）注入的，替身多半没有 `aclose`，
        # 而且"谁建谁关"——本函数自建 `AsyncOpenAI` 却不关，长跑进程就会攒下一堆连接池。
        if own_client:
            await client.aclose()


async def _record_completed(
    sessions: async_sessionmaker[AsyncSession],
    task_id: str,
    interview_id: str,
    final_state: dict[str, Any],
) -> None:
    """成功终态落库。写不进去只记日志：图确实跑完了，不该因为进度行没落成而改口报失败。

    这一段原先裸在 `try` 外面——DB 一抖异常就冒出后台协程，task 停在 `running`、
    报告头却是 `completed`，两边各自挂着且都没留下痕迹（BACKLOG P1-4）。
    """
    try:
        await task_service.mark_completed(
            sessions,
            task_id,
            {
                "interview_id": interview_id,
                "finished": bool(final_state.get("finished")),
                "question_total": len(final_state.get("questions") or []),
                "question_batch": int(final_state.get("question_batch") or 0),
            },
        )
    except Exception:
        logger.exception("任务 %s 跑完了但终态没写进去，轮询方会一直看到 running", task_id)


async def _record_updates(
    sessions: async_sessionmaker[AsyncSession],
    task_id: str,
    chunk: Mapping[str, Any],
) -> None:
    """把一个 super-step 的 `{节点名: 增量}` 记成进度步。

    步长从 `trace` 通道里拿（`traced()` 刚写过这一步的毫秒数），拿不到就不记 `ms`——
    扇出分支不被 `traced()` 包，它的增量里没有这一步。
    """
    for node, update in chunk.items():
        ms = _last_trace_ms(update, node)
        await task_service.record_step(sessions, task_id, str(node), ms=ms)


def _last_trace_ms(update: Any, node: str) -> float | None:
    if not isinstance(update, Mapping):
        return None
    trace = update.get("trace")
    if not isinstance(trace, list):
        return None
    for step in reversed(trace):
        # `TraceStep` 是 TypedDict，运行时就是普通 dict；对它做 isinstance 在 Py 3.14 直接
        # `TypeError`（TypedDict 不支持实例检查），所以按 Mapping 读键。
        if isinstance(step, Mapping) and step.get("node") == node and step.get("ok"):
            ms = step.get("ms")
            return float(ms) if ms is not None else None
    return None


async def _record_failed_step(
    sessions: async_sessionmaker[AsyncSession],
    task_id: str,
    exc: BaseException,
) -> None:
    """把 `traced()` 挂在异常上的失败步骤记成进度步：中止的运行由此能归因到节点与错误码。

    节点抛异常时任何 state 通道都落不了库（checkpoint 停在失败前一步），进度行是它唯一的
    去处。这条写不进库时只记日志：原始异常已经在 `mark_failed` 那一本账里，不该再往上抛。
    """
    step = failed_trace_step(exc)
    if step is None:
        return
    try:
        await task_service.record_step(
            sessions,
            task_id,
            str(step.get("node") or ""),
            ms=float(step["ms"]) if step.get("ms") is not None else None,
            ok=False,
            detail=str(step.get("detail") or ""),
        )
    except Exception:
        logger.exception("任务 %s 的失败节点步进不去，中止无法归因到节点", task_id)


async def _abort_interview(
    sessions: async_sessionmaker[AsyncSession], interview_id: str
) -> None:
    """中途崩溃时补刀报告头：只翻仍挂着的 `running`，终态行不覆盖。"""
    try:
        async with session_scope(sessions) as session:
            repo = InterviewRepository(session)
            head = await repo.get_by_id(interview_id)
            if head is not None and head.status == "running":
                await repo.set_status(interview_id, "aborted")
    except Exception:
        # 补刀失败不该再吞掉原始错误的记录，所以只尽力而为；但"报告头还挂着 running"
        # 这件事必须留下痕迹，否则它和僵尸任务行一起变成无人知晓的暗雷。
        logger.exception("报告 %s 的中止补刀没写进去，状态会停在 running", interview_id)
