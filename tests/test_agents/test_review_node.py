"""`review` 节点：规则先判、模型补语义、无条件收尾落库〔M5，ADR-0015 改批注制〕

三类断言各占一坨：**批注的收口**（越界序号、空正文、规则层与模型层各指到哪些题）、
**调用行为**（独立模型槽位、温度 0、规则层不再短路模型评审）、
**收尾**（无论批注多少都 `completed` 落题、评审端点挂了也落题、没给落库入口就只记账）。

ADR-0015 之前这里断言的是"占比过阈值就自增 `review_retries`、预算用尽写 `aborted` 且不写题"，
那三样东西已经连配置项一起下线了。
"""

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.nodes.review_node import make_review_node
from app.config import Settings, get_settings
from app.exceptions.base import LLMError, ValidationFailedError
from app.repositories.interview_repository import InterviewRepository
from app.repositories.question_repository import QuestionRepository
from app.repositories.resume_repository import ResumeRepository
from app.utils.db import session_scope
from app.utils.llm_client import LLMClient
from tests.test_agents._fake_llm import FakeCompletions

EVIDENCE = "使用 Redis 实现滑动窗口限流"
RAW = f"项目经历\n{EVIDENCE}，QPS 从 800 提升到 2000。"
INTERVIEW_ID = "c" * 32
RESUME_ID = "d" * 32


def _settings() -> Settings:
    copy = get_settings().model_copy(deep=True)
    copy.llm_model_review = "review-slot"
    copy.llm_model_generate = "generate-slot"
    return copy


def _question(index: int, *, batch: int = 1, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "stem": f"第 {index} 道题的题干写得足够长",
        "difficulty": "medium",
        "source": "resume",
        "evidence": EVIDENCE,
        "checkpoints": ["考察限流取舍"],
        "reference_answer": "讲清算法与边界",
        "rubric": ["能说出两种算法的差别"],
        "follow_ups": [],
        "topic": "并发",
        "batch": batch,
        "order_index": index,
    }
    base.update(overrides)
    return base


def _state(count: int = 3, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "resume_id": RESUME_ID,
        "raw_text": RAW,
        "parsed": {"name": "林可", "years_experience": 3.0},
        "plan": {"items": [{"topic": "并发", "count": count}], "total": count},
        "questions": [_question(index) for index in range(count)],
        "question_batch": 1,
    }
    base.update(overrides)
    return base


def _node(*replies: str, **kwargs: Any):
    fake = FakeCompletions(list(replies) or ['{"issues": []}'])
    node = make_review_node(llm=LLMClient(settings=_settings(), client=fake), **kwargs)
    return node, fake


def _review_json(*issues: dict[str, Any]) -> str:
    return json.dumps({"issues": list(issues)}, ensure_ascii=False)


# --- 批注与调用行为 ---


async def test_a_clean_batch_finishes_and_records_the_template_version() -> None:
    node, fake = _node(_review_json())

    update = await node(_state())

    assert update["finished"] is True
    assert update["review_issues"] == []
    assert update["prompt_version"] == "review@v1"
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == "review-slot", "评审走独立槽位，不与出题混用"
    assert fake.calls[0]["temperature"] == pytest.approx(0.0)
    assert fake.calls[0]["max_tokens"] == _settings().llm.default_max_tokens


async def test_the_reviewer_is_shown_the_job_ad_and_checks_jd_quotes_against_it() -> None:
    """`source=jd` 的题引用 JD 原句：不给评审看 JD，它就把合法引用报成"简历里查无此句"
    （实测 2026-10-20 的 15 条批注里 9 条是这种误报）。"""
    jd = "岗位职责：负责高并发服务的容量规划与限流方案评审。"
    node, fake = _node(_review_json())
    state = _state(count=3, jd_text=jd)
    state["questions"][1] = _question(1, source="jd", evidence="负责高并发服务的容量规划")

    update = await node(state)

    assert jd in fake.user_text
    assert update["review_issues"] == []
    assert update["finished"] is True


async def test_issues_are_annotated_and_the_batch_still_finishes() -> None:
    """ADR-0015：批注只点名。哪怕半数题目被批，这一批也照样是终局。"""
    node, _ = _node(
        _review_json(
            {"index": 0, "problem": "标 hard 却只考回忆", "fix": "要求设计方案"},
            {"index": 1, "problem": "题干信息不全", "fix": "补上量级"},
        )
    )

    update = await node(_state(count=3))

    assert update["finished"] is True
    assert len(update["review_issues"]) == 2
    assert [issue["index"] for issue in update["review_issues"]] == [0, 1]


async def test_a_few_issues_below_the_old_threshold_still_passes() -> None:
    node, _ = _node(_review_json({"index": 0, "problem": "rubric 区分不了水平", "fix": "加一档"}))

    update = await node(_state(count=4))

    assert update["finished"] is True
    assert len(update["review_issues"]) == 1


async def test_rule_issues_no_longer_short_circuit_the_reviewer() -> None:
    """缺题缺到原来会判死的程度，评审模型照问：这一轮是这批题唯一的一次语义检查。"""
    node, fake = _node(_review_json({"index": 0, "problem": "任何评语", "fix": "任何改法"}))

    update = await node(_state(count=3, questions=[_question(0)]))

    assert len(fake.calls) == 1
    # 2 条缺题（index=None）+ 1 条模型批注
    assert [issue["index"] for issue in update["review_issues"]] == [None, None, 0]


async def test_an_empty_batch_skips_the_reviewer_and_annotates_the_shortfall() -> None:
    """没有题可评就别花一次调用；缺的 3 题逐条记进批注，报告页会点名缺了哪几题。"""
    node, fake = _node(_review_json())

    update = await node(_state(count=3, questions=[]))

    assert fake.calls == []
    assert update["finished"] is True
    assert len(update["review_issues"]) == 3, "计划 3 题、到手 0 题 = 3 条批注"
    assert any("跳过语义评审" in note for note in update["errors"])


async def test_issues_with_no_such_index_are_dropped_not_trusted() -> None:
    node, _ = _node(
        _review_json(
            {"index": 99, "problem": "指向不存在的题", "fix": "无"},
            {"index": 0, "problem": "有多个正确答案", "fix": "限定场景"},
        )
    )

    update = await node(_state(count=3))

    assert [issue["index"] for issue in update["review_issues"]] == [0]
    assert any("超出题目范围" in note for note in update["errors"])


async def test_a_dead_reviewer_still_delivers_the_questions_with_a_warning() -> None:
    """评审端点挂了不再拖走整份卷子（ADR-0015），但必须留下一条指不到具体题的批注，
    否则"只做了规则检查"会被读成"检查过、没问题"。"""
    fake = FakeCompletions([], raises=LLMError("评审端点不通"))
    node = make_review_node(llm=LLMClient(settings=_settings(), client=fake))

    update = await node(_state())

    assert update["finished"] is True
    assert len(update["review_issues"]) == 1
    assert update["review_issues"][0]["index"] is None
    assert "语义评审这一轮没跑成" in update["review_issues"][0]["problem"]
    assert any("评审调用失败，已按规则层批注落库" in note for note in update["errors"])


async def test_missing_extraction_is_an_unrecoverable_failure() -> None:
    node, _ = _node(_review_json())

    with pytest.raises(ValidationFailedError, match="缺少简历抽取结果"):
        await node({"resume_id": RESUME_ID, "questions": _state()["questions"]})


# --- 收尾落库 ---


async def _seed(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(db_sessionmaker) as session:
        await ResumeRepository(session).create(id=RESUME_ID, name="林可", raw_text=RAW)
        await InterviewRepository(session).create(
            id=INTERVIEW_ID, resume_id=RESUME_ID, plan={}, status="running"
        )


async def test_a_clean_run_writes_the_report_and_the_questions(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_sessionmaker)
    node, _ = _node(_review_json(), sessionmaker=db_sessionmaker)

    update = await node(_state(count=3, interview_id=INTERVIEW_ID))

    assert update["finished"] is True
    assert any("落库 3 题，批注 0 条" in note for note in update["errors"])
    async with db_sessionmaker() as session:
        head = await InterviewRepository(session).get_by_id(INTERVIEW_ID)
        assert head is not None and head.status == "completed"
        assert len(await QuestionRepository(session).list_for_interview(INTERVIEW_ID)) == 3


async def test_a_heavily_annotated_run_lands_completed_with_every_question(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """原来这一支写的是 `aborted` + 0 题。现在批注只是注脚：题一行不少，状态照样终。"""
    await _seed(db_sessionmaker)
    node, _ = _node(
        _review_json(
            {"index": 0, "problem": "题干信息不全", "fix": "补量级"},
            {"index": 1, "problem": "无法评分", "fix": "拆成两档"},
        ),
        sessionmaker=db_sessionmaker,
    )

    update = await node(_state(count=3, interview_id=INTERVIEW_ID))

    assert update["finished"] is True
    assert any("落库 3 题，批注 2 条（点名 2 题）" in note for note in update["errors"])
    async with db_sessionmaker() as session:
        head = await InterviewRepository(session).get_by_id(INTERVIEW_ID)
        assert head is not None and head.status == "completed"
        assert head.total_questions == 3
        assert len(head.review_issues) == 2
        assert len(await QuestionRepository(session).list_for_interview(INTERVIEW_ID)) == 3


async def test_without_an_interview_id_the_run_still_finishes_but_writes_nothing(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """"调用方没打算入库"不是质量问题：跳过落库、记一行流水账。"""
    node, _ = _node(_review_json(), sessionmaker=db_sessionmaker)

    update = await node(_state(count=2))

    assert update["finished"] is True
    assert any("收尾未落库（缺 interview_id）" in note for note in update["errors"])
    async with db_sessionmaker() as session:
        assert await InterviewRepository(session).count() == 0
