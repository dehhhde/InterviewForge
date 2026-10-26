"""M5 的图级验收：扇出、批次覆盖、一次成卷、收尾落库〔阶段 7〕

这里是唯一一处把**真 `generate` / 真分支 / 真 `review`** 装进真图跑起来的地方，
因为风险 5 的三件事（累积通道不叠题、题号与完成顺序无关、引用可回溯）只有
并行分支真的各自返回时才验得出来。模型仍然全程替身：断言只看结构、次数、批次。

ADR-0015 之后 `review` 是终点：批注再多也不重掷 `generate`，题目一律落库。
"""

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.nodes.generate_node import make_generate_item_node, make_generate_node
from app.agents.nodes.review_node import make_review_node
from app.agents.state import GRAPH_NODES, NodeName
from app.agents.workflow import build_agent_graph, make_thread_config, open_checkpointer
from app.config import get_settings
from app.repositories.interview_repository import InterviewRepository
from app.repositories.question_repository import QuestionRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.question_service import evidence_ratio, order_questions
from app.utils.db import session_scope
from app.utils.llm_client import LLMClient
from tests.test_agents._fake_llm import FakeCompletions, prompt_block

EVIDENCE = "使用 Redis 实现滑动窗口限流"
RAW = f"项目经历\n{EVIDENCE}，QPS 从 800 提升到 2000。"
RESUME_ID = "e" * 32
INTERVIEW_ID = "f" * 32

TOPICS = ("并发", "存储", "系统设计")
PLAN: dict[str, Any] = {
    "items": [
        {"topic": topic, "difficulty": "medium", "count": 1, "source": "resume"}
        for topic in TOPICS
    ],
    "total": len(TOPICS),
}


def _settings() -> Any:
    copy = get_settings().model_copy(deep=True)
    copy.llm_model_generate = "generate-slot"
    copy.llm_model_review = "review-slot"
    return copy


def _completion(text: str) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


class _TopicAwareCompletions:
    """出题替身：从 prompt 里读"所属主题"，按主题产出题目。

    不能像 `FakeCompletions` 那样按调用序号预置回复——并行分支的完成顺序由调度决定，
    序号和主题的对应关系不稳定。这里让回复只依赖**它自己看到的 prompt**，
    于是同一轮里三条分支各出各的题，而题号仍由扇出前算好的 `order_start` 决定。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        user = str(kwargs["messages"][-1]["content"])
        matched = re.search(r"所属主题：(.+)", user)
        counted = re.search(r"请出 (\d+) 道题", user)
        topic = matched.group(1).strip() if matched else "未知"
        total = int(counted.group(1)) if counted else 1
        # 来源跟着槽位走，引用则从 prompt 里那一块逐字抄—— jd 槽位抄简历会被闸门丢掉
        located = re.search(r"本题来源：(.+)", user)
        source = located.group(1).strip() if located else "resume"
        quoted = re.search(r"岗位 JD 原文[^\n]*\n\n(.+)", user)
        evidence = EVIDENCE
        if source == "jd" and quoted:
            evidence = quoted.group(1).strip()
        # 故意的倒序延迟：让最后一题先完成，看落库的题号会不会跟着调度跑
        await asyncio.sleep(0.01 * (len(TOPICS) - TOPICS.index(topic) if topic in TOPICS else 0))
        payload = {
            "questions": [
                {
                    "stem": f"关于{topic}的第 {i + 1} 道题题干足够长",
                    "difficulty": "medium",
                    "source": source,
                    "evidence": evidence,
                    "checkpoints": [f"考察{topic}的深度"],
                    "reference_answer": "讲清方案与边界",
                    "rubric": ["能说出两种做法的取舍"],
                    "follow_ups": [],
                }
                for i in range(total)
            ]
        }
        return _completion(json.dumps(payload, ensure_ascii=False))


class _Fakes:
    """装配出来的替身，用来断言调用次数——节点表本身不该混装非节点值。"""

    def __init__(self, nodes: dict[NodeName, Any], generate: Any, review: Any) -> None:
        self.nodes = nodes
        self.generate = generate
        self.review = review


def _nodes(
    *,
    review_replies: list[str],
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> _Fakes:
    async def _noop(state: Any) -> dict[str, Any]:
        return {}

    generate_fake = _TopicAwareCompletions()
    review_fake = FakeCompletions(review_replies)
    settings = _settings()
    nodes: dict[NodeName, Any] = {name: _noop for name in GRAPH_NODES}
    nodes.update(
        generate=make_generate_node(),
        generate_item=make_generate_item_node(
            llm=LLMClient(settings=settings, client=generate_fake)
        ),
        review=make_review_node(
            llm=LLMClient(settings=settings, client=review_fake), sessionmaker=sessionmaker
        ),
    )
    return _Fakes(nodes, generate_fake, review_fake)


def _input(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "resume_id": RESUME_ID,
        "jd_text": "需要熟并发与存储",
        "raw_text": RAW,
        "parsed": {"name": "林可", "years_experience": 3.0},
        "plan": PLAN,
    }
    base.update(overrides)
    return base


_CLEAN = ['{"issues": []}']
#: 每轮都报 1 条批注：ADR-0015 之前这个比例（1/3）会关掉回退门，现在它只是批注。
_DIRTY = ['{"issues": [{"index": 0, "problem": "题干信息不全", "fix": "补上数据量级"}]}']


async def test_one_round_fans_out_one_call_per_slot_and_finishes(tmp_path: Any) -> None:
    """一条 `plan.items` 槽位 = 一次调用：不扇出就装不下 15 题的输出（`default_max_tokens`）。"""
    fakes = _nodes(review_replies=_CLEAN)
    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(fakes.nodes, checkpointer=saver)
        result = await graph.ainvoke(_input(), make_thread_config("m5-1", recursion_limit=40))

    assert len(fakes.generate.calls) == len(TOPICS)
    assert len(result["questions"]) == len(TOPICS)
    assert result["question_batch"] == 1
    assert result["finished"] is True
    assert result["review_issues"] == []
    # 每条分支都收到"别的槽位在问哪一档"，且那份摘要里不含它自己
    for call in fakes.generate.calls:
        user = str(call["messages"][-1]["content"])
        own = str(re.search(r"所属主题：(.+)", user).group(1).strip())
        roster = {entry["topic"] for entry in prompt_block(user, "同批其它槽位")}
        assert roster == set(TOPICS) - {own}


async def test_question_numbers_do_not_follow_completion_order(tmp_path: Any) -> None:
    """题号是扇出**之前**算好的：分支倒序完成也不能把报告里的题号打乱。"""
    fakes = _nodes(review_replies=_CLEAN)
    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(fakes.nodes, checkpointer=saver)
        result = await graph.ainvoke(_input(), make_thread_config("m5-2", recursion_limit=40))

    ordered = order_questions(result["questions"])
    assert [q["order_index"] for q in ordered] == [0, 1, 2]
    assert [q["stem"].split("的第")[0].removeprefix("关于") for q in ordered] == list(TOPICS)


async def test_the_fanout_payload_carries_the_job_ad_into_every_branch(tmp_path: Any) -> None:
    """分支看不到主图 state：`Send` 载荷里不带 `jd_text`，`source=jd` 的槽位就一条引用都留不下
    ——实测（2026-10-20 首次端到端）26 个槽位 21 个是 jd 来源，带引用比例被压到 25%。"""
    plan: dict[str, Any] = {
        "items": [{"topic": "容量规划", "difficulty": "medium", "count": 1, "source": "jd"}],
        "total": 1,
    }
    fakes = _nodes(review_replies=_CLEAN)
    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(fakes.nodes, checkpointer=saver)
        result = await graph.ainvoke(
            _input(plan=plan), make_thread_config("m5-jd", recursion_limit=40)
        )

    assert result["finished"] is True
    assert result["questions"][0]["evidence"] == "需要熟并发与存储"
    assert evidence_ratio(result["questions"]) == pytest.approx(1.0)


async def test_a_heavily_annotated_review_still_generates_exactly_once(tmp_path: Any) -> None:
    """ADR-0015 取代了 M5 原来那条"占比超阈值就重跑预算次数"的验收：批注只标注、不拦门。

    这条用例存在的全部意义是**证明重掷没了**：以前 `_DIRTY` 会让 `generate` 跑
    `review_max_retries + 1` 轮，现在它必须恰好一轮。
    """
    fakes = _nodes(review_replies=_DIRTY)
    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(fakes.nodes, checkpointer=saver)
        result = await graph.ainvoke(_input(), make_thread_config("m5-3", recursion_limit=60))

    ran = [step["node"] for step in result["trace"]]
    assert ran.count(NodeName.GENERATE.value) == 1
    assert ran.count(NodeName.REVIEW.value) == 1
    assert len(fakes.generate.calls) == len(TOPICS)
    assert len(fakes.review.calls) == 1
    assert result["finished"] is True
    assert [issue["index"] for issue in result["review_issues"]] == [0]
    assert len(result["questions"]) == len(TOPICS), "批注不减少落库的题数"


async def test_a_single_round_stamps_one_of_each_prompt_version(tmp_path: Any) -> None:
    """一轮成卷后 `trace` 里 generate/review 各只有一条，模板版本仍逐次盖章。"""
    fakes = _nodes(review_replies=_DIRTY)
    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(fakes.nodes, checkpointer=saver)
        result = await graph.ainvoke(_input(), make_thread_config("m5-4", recursion_limit=60))

    trace = result["trace"]
    assert [step["node"] for step in trace][5:] == ["generate", "review"]
    assert [step.get("prompt_version") for step in trace][5:] == ["generate@v1", "review@v1"]
    assert not [note for note in result["errors"] if "覆盖批次" in note]


async def test_the_accumulating_channel_holds_exactly_this_run(
    tmp_path: Any,
) -> None:
    """风险 5 的最终形态：`questions` 是 `operator.add` 通道，全靠扇出节点整批换掉。
    ADR-0015 之后没有第二批，但"叠成 2N 题"这个失效模式仍然只能由这里拦住。"""
    fakes = _nodes(review_replies=_DIRTY)
    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(fakes.nodes, checkpointer=saver)
        result = await graph.ainvoke(_input(), make_thread_config("m5-5", recursion_limit=60))

    assert {q["batch"] for q in result["questions"]} == {1}
    assert len(result["questions"]) == len(TOPICS), "叠成 2N 就是 Overwrite 没生效"


async def test_a_passing_run_lands_the_report_in_the_database(
    tmp_path: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """端到端的接缝：`review` 通过 -> 一个事务里写 `interview` + 3 行 `question`。"""
    async with session_scope(db_sessionmaker) as session:
        await ResumeRepository(session).create(id=RESUME_ID, name="林可", raw_text=RAW)
        await InterviewRepository(session).create(
            id=INTERVIEW_ID, resume_id=RESUME_ID, plan=PLAN, status="running"
        )

    fakes = _nodes(review_replies=_CLEAN, sessionmaker=db_sessionmaker)
    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(fakes.nodes, checkpointer=saver)
        result = await graph.ainvoke(
            _input(interview_id=INTERVIEW_ID), make_thread_config("m5-6", recursion_limit=40)
        )

    assert result["finished"] is True
    async with db_sessionmaker() as session:
        head = await InterviewRepository(session).get_by_id(INTERVIEW_ID)
        rows = await QuestionRepository(session).list_for_interview(INTERVIEW_ID)
        assert head is not None and head.status == "completed"
        assert head.total_questions == len(TOPICS)
        assert [row.order_index for row in rows] == [0, 1, 2]
        assert {row.batch for row in rows} == {1}
        assert all(row.evidence == EVIDENCE for row in rows)


async def test_a_heavily_annotated_run_lands_completed_with_every_question(
    tmp_path: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """ADR-0015 的下半段：批注再满也不拦落库。报告是 `completed` + 全部题目 + 批注清单。

    替代原来那条"回退到预算用尽只落一行 `aborted`、零题"的验收——那是"质量门判死整批"
    唯一的现场，判死没了，现场也就没了。
    """
    async with session_scope(db_sessionmaker) as session:
        await ResumeRepository(session).create(id=RESUME_ID, name="林可", raw_text=RAW)
        await InterviewRepository(session).create(
            id=INTERVIEW_ID, resume_id=RESUME_ID, plan=PLAN, status="running"
        )

    fakes = _nodes(review_replies=_DIRTY, sessionmaker=db_sessionmaker)
    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(fakes.nodes, checkpointer=saver)
        result = await graph.ainvoke(
            _input(interview_id=INTERVIEW_ID), make_thread_config("m5-8", recursion_limit=60)
        )

    async with db_sessionmaker() as session:
        head = await InterviewRepository(session).get_by_id(INTERVIEW_ID)
        rows = await QuestionRepository(session).list_for_interview(INTERVIEW_ID)
        assert head is not None and head.status == "completed"
        assert head.total_questions == len(TOPICS)
        assert len(rows) == len(TOPICS)
        assert [issue["index"] for issue in head.review_issues] == [0]
    assert any("批注 1 条（点名 1 题）" in note for note in result["errors"])


async def test_the_evidence_ratio_of_a_honest_batch_is_one(tmp_path: Any) -> None:
    """M5 验收口径（≥ 0.9）在这里的可控版：全部题目带可回溯引用时比例是 1.0。"""
    fakes = _nodes(review_replies=_CLEAN)
    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(fakes.nodes, checkpointer=saver)
        result = await graph.ainvoke(_input(), make_thread_config("m5-7", recursion_limit=40))

    assert evidence_ratio(result["questions"]) == pytest.approx(1.0)
