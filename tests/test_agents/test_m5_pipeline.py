"""M5 的图级验收：扇出、批次覆盖、一次成卷、收尾落库〔阶段 7〕

这里是唯一一处把**真 `generate` / 真分支 / 真 `review`** 装进真图跑起来的地方，
因为风险 5 的三件事（累积通道不叠题、题号与完成顺序无关、引用可回溯）只有
并行分支真的各自返回时才验得出来。模型仍然全程替身：断言只看结构、次数、批次。

ADR-0015 之后 `review` 是终点：批注再多也不重掷 `generate`，题目一律落库。
ADR-0021 只开一个有界的例外——调用方带 `auto_fix=true` 且第一轮有点名的题，才回一次
`generate` 重做**被点名的那几档**（`tests/test_agents/test_m5_pipeline.py` 末尾两条）。
"""

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.nodes.generate_node import make_generate_item_node, make_generate_node
from app.agents.nodes.plan_node import ANGLES_BY_DIFFICULTY
from app.agents.nodes.review_node import make_review_node
from app.agents.state import GRAPH_NODES, NodeName
from app.agents.workflow import (
    build_agent_graph,
    make_thread_config,
    open_checkpointer,
    route_after_generate,
)
from app.config import Settings
from app.repositories.interview_repository import InterviewRepository
from app.repositories.question_repository import QuestionRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.question_service import evidence_ratio, order_questions
from app.services.redo_service import plan_slots
from app.utils.db import session_scope
from app.utils.llm_client import LLMClient
from tests.test_agents._fake_llm import (
    FakeCompletions,
    completion,
    fenced_block,
    prompt_block,
    slot_settings,
    stamp,
)

EVIDENCE = "使用 Redis 实现滑动窗口限流"
RAW = f"项目经历\n{EVIDENCE}，QPS 从 800 提升到 2000。"
RESUME_ID = "e" * 32
INTERVIEW_ID = "f" * 32

TOPICS = ("并发", "存储", "系统设计")
#: 手写配额表也得带角度：`plan` 节点现在恒给 `angles`，测试里的表要跟产出同形〔T-202〕
PLAN: dict[str, Any] = {
    "items": [
        {
            "topic": topic,
            "difficulty": "medium",
            "count": 1,
            "source": "resume",
            "angles": [ANGLES_BY_DIFFICULTY["medium"][index]],
        }
        for index, topic in enumerate(TOPICS)
    ],
    "total": len(TOPICS),
}


def _settings() -> Settings:
    return slot_settings(generate="generate-slot", review="review-slot")


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
        # 来源跟着槽位走，引用则从 prompt 里 `<jd>` 围栏逐字抄—— jd 槽位抄简历会被闸门丢掉
        located = re.search(r"本题来源：(.+)", user)
        source = located.group(1).strip() if located else "resume"
        evidence = fenced_block(user, "jd") if source == "jd" else EVIDENCE
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
        return completion(json.dumps(payload, ensure_ascii=False))


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
        roster = {entry["topic"] for entry in prompt_block(user, "roster")}
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
    stamps = [step.get("prompt_version") for step in trace][5:]
    assert stamps == [stamp("generate"), stamp("review")]
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


_MIXED_PLAN: dict[str, Any] = {
    "items": [
        {
            "topic": "并发",
            "difficulty": "medium",
            "count": 1,
            "source": "resume",
            "angles": ["实现原理"],
        },
        {
            "topic": "存储",
            "difficulty": "medium",
            "count": 1,
            "source": "jd",
            "angles": ["动手落地"],
        },
        {
            "topic": "系统设计",
            "difficulty": "medium",
            "count": 1,
            "source": "generic",
            "angles": ["配置与参数取舍"],
        },
    ],
    "total": 3,
}

#: `GenerateJob` 的全部键。少一个键，分支里 `job.get(...)` 拿到 None，
#: 和"这一档没有语料"长得一模一样（实测 2026-10-20 漏 `jd_text` → 引用率 25%）
_JOB_KEYS = {
    "plan_item",
    "order_start",
    "question_batch",
    "raw_text",
    "jd_text",
    "roster",
    "parsed",
    "previous",
}


def _jobs(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """照 `generate` 节点跑完后的形状喂路由〔T-404〕：路由只打包 `fanout_slots`，不自己挑槽位。

    这里手动补上那个通道而不是让路由回退去读 `plan`：回退就会造出第二个"这一轮扇给谁"
    的出处，而定向重做轮的答案恰恰不是整卷。
    """
    state = {**_input(plan=plan), "question_batch": 1, "fanout_slots": plan_slots(plan)}
    sends = route_after_generate(state)
    assert isinstance(sends, list), "三条有配额的槽位必须各派一个分支"
    return [dict(send.arg) for send in sends]


def test_a_slot_carries_only_the_corpus_its_source_can_quote() -> None:
    """扇出瘦身：语料按槽位来源取值，**键恒在**〔T-205〕。

    15 题 = 15 份同样上下文，而每份里最多只有一侧原文进得了闸门（`corpora_for`）。
    断言写成"值清空、键还在"而不是"载荷变小"，是因为键少了才是真事故：
    分支读 `job.get("jd_text")`，缺键会让 `source=jd` 那档永远留不下引用，
    而它看起来和"这个岗位没给 JD"完全一样。
    """
    resume, jd, generic = _jobs(_MIXED_PLAN)

    assert [set(job) for job in (resume, jd, generic)] == [_JOB_KEYS] * 3
    assert (resume["raw_text"], resume["jd_text"]) == (RAW, "")
    assert (jd["raw_text"], jd["jd_text"]) == ("", _input()["jd_text"])
    assert (generic["raw_text"], generic["jd_text"]) == (RAW, _input()["jd_text"])


def test_the_roster_shows_which_angles_the_other_slots_already_took() -> None:
    """`roster` 不退掉，而是把角度一起带上〔T-202〕——分支看不见兄弟分支，只能靠这张表错开。

    TODO §2.3 原本问"加了 `angle` 之后 `roster` 能不能退掉"：答案是不能。退掉它等于把
    "整批还问了哪些角度"重新变成盲区，而角度恰恰是靠"和别人的不一样"才起作用的信息。
    """
    resume, jd, generic = _jobs(_MIXED_PLAN)

    assert [entry["angles"] for entry in resume["roster"]] == [["动手落地"], ["配置与参数取舍"]]
    assert [entry["topic"] for entry in jd["roster"]] == ["并发", "系统设计"]
    assert all("angles" in entry for entry in generic["roster"]), "每档都带上它打算从哪儿问"


async def test_a_slimmed_payload_still_quotes_from_every_source(tmp_path: Any) -> None:
    """瘦身不许掉引用率：三种来源各出一题，比例仍须是 1.0（基线 100%）。

    这条是上一条的**后果**验收：载荷里清掉一份语料后，闸门在分支真正拿得到的那份里
    必须还找得到出处。替身按 `本题来源` 决定引用抄自哪一侧，所以它复现的是真调用里
    最贵的那种错法——`source=jd` 的题目抄简历句子，会被整题丢弃。
    """
    fakes = _nodes(review_replies=_CLEAN)
    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(fakes.nodes, checkpointer=saver)
        result = await graph.ainvoke(
            _input(plan=_MIXED_PLAN), make_thread_config("m5-9", recursion_limit=40)
        )

    assert [q["source"] for q in order_questions(result["questions"])] == [
        "resume",
        "jd",
        "generic",
    ]
    assert evidence_ratio(result["questions"]) == pytest.approx(1.0)
    assert not [note for note in result["errors"] if "找不到" in note or "没给原文引用" in note]


# --- 定向重做回路〔T-404 / ADR-0021〕---

#: 点名第 2 题（`TOPICS` 里的「存储」那一档）。`FakeCompletions` 越界后一直吐最后一条，
#: 所以第二轮评审同样点它——正好用来钉"最多一轮"那条上界。
_NAMED_STORAGE = ['{"issues": [{"index": 1, "problem": "题干信息不全", "fix": "补上数据量级"}]}']


def _asked_topics(fake: Any) -> list[str]:
    """每个出题分支问的是哪一档——重做轮该只多出一条，而不是整卷再来一遍。"""
    return [
        str(re.search(r"所属主题：(.+)", str(call["messages"][-1]["content"])).group(1)).strip()
        for call in fake.calls
    ]


async def test_an_authorized_round_redoes_only_the_slot_that_was_named(tmp_path: Any) -> None:
    """`auto_fix=true`：整卷 3 次调用 + 重做档 1 次调用，第二批仍是完整 3 题、号位 0-2。

    这条断的是回路的**形状**，不是修复效果（效果要看真模型，卡 T-105/D-2）：
    - 只多花一条槽位的调用，没被点名的两档一题不重出；
    - 没被点名的题原样带进新批次——`review` 读的是最新批次，少带就是交出一张缺页的卷子；
    - 第二轮评审照样点它，但批次号已经是 2，路由回 END，**不会有第三轮**。
    """
    fakes = _nodes(review_replies=_NAMED_STORAGE)
    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(fakes.nodes, checkpointer=saver)
        result = await graph.ainvoke(
            _input(auto_fix=True), make_thread_config("m5-10", recursion_limit=60)
        )

    ran = [step["node"] for step in result["trace"]]
    assert ran.count(NodeName.GENERATE.value) == 2
    assert ran.count(NodeName.REVIEW.value) == 2, "第二轮的批注不再换第三次扇出"
    assert _asked_topics(fakes.generate).count("存储") == 2
    assert [t for t in _asked_topics(fakes.generate) if t != "存储"] == ["并发", "系统设计"]
    assert result["question_batch"] == 2

    ordered = order_questions(result["questions"])
    assert [q["order_index"] for q in ordered] == [0, 1, 2]
    assert {q["batch"] for q in ordered} == {2}, "最新批次必须是完整一张卷子，不是只有被修的那一档"
    assert [q["stem"].split("的第")[0].removeprefix("关于") for q in ordered] == list(TOPICS)


async def test_the_redo_round_lands_one_complete_latest_batch(
    tmp_path: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """落库这一端：报告读到的最新批次是 3 题（2 道带走 + 1 道重出），不是 1 题。

    这是"缺页"事故的现场验收。上一批的行留在表里（`(interview_id, batch, order_index)`
    各占一格），但 `list_for_interview` 默认只读最新批——计划书 §6 风险 5 的那条口径。
    """
    async with session_scope(db_sessionmaker) as session:
        await ResumeRepository(session).create(id=RESUME_ID, name="林可", raw_text=RAW)
        await InterviewRepository(session).create(
            id=INTERVIEW_ID, resume_id=RESUME_ID, plan=PLAN, status="running"
        )

    fakes = _nodes(review_replies=_NAMED_STORAGE, sessionmaker=db_sessionmaker)
    async with open_checkpointer(tmp_path / "ckpt.db") as saver:
        graph = build_agent_graph(fakes.nodes, checkpointer=saver)
        await graph.ainvoke(
            _input(interview_id=INTERVIEW_ID, auto_fix=True),
            make_thread_config("m5-11", recursion_limit=60),
        )

    async with db_sessionmaker() as session:
        repo = QuestionRepository(session)
        assert await repo.latest_batch(INTERVIEW_ID) == 2
        latest = await repo.list_for_interview(INTERVIEW_ID)
        assert [row.order_index for row in latest] == [0, 1, 2]
        assert {row.batch for row in latest} == {2}
        head = await InterviewRepository(session).get_by_id(INTERVIEW_ID)
        assert head is not None and head.status == "completed"
        assert head.total_questions == len(TOPICS)
