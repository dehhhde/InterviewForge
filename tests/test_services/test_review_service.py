"""`review_service` 的规则层、批注收口与落库〔M5〕

落库那组用例是 M5 验收的下半段：一份报告 = `interview` + N 行 `question`，必须**一个事务**。
中途失败要能整体退回，否则库里留半份报告，读的人分不清它是坏了还是还没跑完（ADR-0007）。
"""

from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.interview import Interview
from app.repositories.interview_repository import InterviewRepository
from app.repositories.question_repository import QuestionRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.review_service import (
    ReviewPayload,
    adopt_model_issues,
    make_issue,
    persist_report,
    rule_issues,
)
from app.utils.db import session_scope

EVIDENCE = "使用 Redis 实现滑动窗口限流"
RAW = f"项目经历\n{EVIDENCE}，QPS 从 800 提升到 2000。"
PLAN: dict[str, Any] = {"items": [{"count": 2}, {"count": 1}], "total": 3}
INTERVIEW_ID = "f" * 32


def _question(stem: str, *, index: int, batch: int = 1, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "stem": stem,
        "difficulty": "medium",
        "source": "resume",
        "evidence": EVIDENCE,
        "checkpoints": ["考察限流取舍"],
        "reference_answer": "讲清算法与边界",
        "rubric": ["能说出两种算法的差别"],
        "follow_ups": ["热点 key 怎么办"],
        "topic": "并发",
        "batch": batch,
        "order_index": index,
    }
    base.update(overrides)
    return base


def _batch(count: int, *, batch: int = 1, stem_of=lambda i: f"第 {i} 道题的题干足够长") -> list:
    return [_question(stem_of(index), index=index, batch=batch) for index in range(count)]


def test_a_clean_complete_batch_produces_no_rule_issues() -> None:
    assert rule_issues(_batch(3), raw_text=RAW, plan=PLAN) == []


def test_literal_duplicate_stems_are_flagged_on_the_later_one() -> None:
    questions = _batch(3)
    questions[2]["stem"] = questions[0]["stem"]

    issues = rule_issues(questions, raw_text=RAW, plan=PLAN)

    assert len(issues) == 1
    assert issues[0]["index"] == 2
    assert issues[0]["origin"] == "rule"
    assert "第 3 题与第 1 题" in issues[0]["problem"]


def test_an_unverifiable_resume_claim_is_flagged_even_after_generate_let_it_through() -> None:
    """整批复查不是重复劳动：注入的 state、手改的通道都从这里被拦住。"""
    questions = _batch(3)
    questions[1] = _question("第二道题的题干足够长", index=1, evidence="我拿过 ACM 金牌")

    issues = rule_issues(questions, raw_text=RAW, plan=PLAN)

    assert [issue["index"] for issue in issues] == [1]
    assert "回指不到原文" in issues[0]["problem"]


def test_a_jd_sourced_question_is_checked_against_the_job_ad() -> None:
    """评审与出题共用一个判据：jd 题的出处是 JD 全文。只拿简历核的话 21/26 个槽位永远过不了门。"""
    jd = "岗位职责：负责高并发服务的容量规划与限流方案评审。"
    questions = _batch(3)
    questions[1] = _question(
        "第二道题的题干足够长", index=1, source="jd", evidence="负责高并发服务的容量规划"
    )

    assert rule_issues(questions, raw_text=RAW, plan=PLAN, jd_text=jd) == []


def test_a_jd_question_copying_the_resume_is_flagged() -> None:
    questions = _batch(3)
    questions[0] = _question("第一道题的题干足够长", index=0, source="jd", evidence=EVIDENCE)

    issues = rule_issues(questions, raw_text=RAW, plan=PLAN, jd_text="岗位职责：负责容量规划。")

    assert [issue["index"] for issue in issues] == [0]
    assert "岗位 JD 原文里找不到" in issues[0]["problem"]


def test_missing_questions_become_one_issue_each() -> None:
    """缺题按条记：一条"缺 3 题"的批注会让人以为只缺一题，页面上也对不上号。"""
    issues = rule_issues(_batch(1), raw_text=RAW, plan=PLAN)

    assert len(issues) == 2
    assert all(issue["index"] is None for issue in issues)
    assert "第 2 题缺失" in issues[0]["problem"]


def test_a_plan_without_items_never_counts_shortfall() -> None:
    assert rule_issues(_batch(2), raw_text=RAW, plan={"items": []}) == []
    assert rule_issues(_batch(2), raw_text=RAW, plan=None) == []


def test_model_issues_are_kept_only_when_they_point_at_a_real_question() -> None:
    payload = ReviewPayload.model_validate(
        {
            "issues": [
                {"index": 1, "problem": "标 hard 却只考一句回忆", "fix": "改成要求设计方案"},
                {"index": 9, "problem": "序号不存在"},
                {"index": 0, "problem": "   "},
                {"index": 1, "problem": "标 hard 却只考一句回忆", "fix": "重复的同一条"},
            ]
        }
    )

    issues, notes = adopt_model_issues(payload, total=3)

    assert len(issues) == 1
    assert issues[0]["origin"] == "model"
    assert issues[0]["index"] == 1
    assert len(notes) == 3, "越界、空正文、重复各记一条"


def test_issue_text_is_clamped_so_one_run_on_cannot_flood_the_report_head() -> None:
    issue = make_issue("长" * 400, index=0)

    assert len(issue["problem"]) == 300


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """给外键准备一份真简历和一行 `running` 的报告头（正常由 API 在建任务时写）。"""
    async with session_scope(sessionmaker) as session:
        resume = await ResumeRepository(session).create(name="林可", raw_text=RAW)
        await InterviewRepository(session).create(
            id=INTERVIEW_ID, resume_id=resume.id, plan=PLAN, status="running"
        )


async def _read(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[Interview, int]:
    async with sessionmaker() as session:
        head = await InterviewRepository(session).get_by_id(INTERVIEW_ID)
        rows = await QuestionRepository(session).list_for_interview(INTERVIEW_ID)
        assert head is not None
        return head, len(rows)


async def test_a_passing_review_writes_one_head_and_the_latest_batch(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_sessionmaker)

    written = await persist_report(
        db_sessionmaker,
        interview_id=INTERVIEW_ID,
        resume_id="ignored",
        questions=_batch(3),
        batch=1,
        plan=PLAN,
        review_issues=[],
    )

    head, rows = await _read(db_sessionmaker)
    assert (written, rows) == (3, 3)
    assert head.status == "completed"
    assert head.total_questions == 3
    assert head.plan == PLAN


async def test_replaying_the_same_batch_does_not_double_the_report(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """幂等（AGENTS.md §4.6）：收尾被重跑时题一行不多，状态照样落定。"""
    await _seed(db_sessionmaker)
    args = dict(
        interview_id=INTERVIEW_ID,
        resume_id="ignored",
        questions=_batch(2),
        batch=2,
        plan=PLAN,
        review_issues=[make_issue("上一版的批注", index=0)],
    )

    await persist_report(db_sessionmaker, **args)
    await persist_report(db_sessionmaker, **args)

    head, rows = await _read(db_sessionmaker)
    assert rows == 2
    assert head.total_questions == 2
    assert len(head.review_issues) == 1


async def test_a_retry_batch_replaces_the_previous_one_on_read(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """同一线程被重放 = 新批次。旧批次留在表里当痕迹，读报告只取最新那一批。"""
    await _seed(db_sessionmaker)
    common = dict(interview_id=INTERVIEW_ID, resume_id="ignored", plan=PLAN, review_issues=[])

    await persist_report(db_sessionmaker, questions=_batch(3), batch=1, **common)
    await persist_report(
        db_sessionmaker,
        questions=_batch(3, batch=2, stem_of=lambda i: f"重做后的第 {i} 题题干足够长"),
        batch=2,
        **common,
    )

    async with db_sessionmaker() as session:
        repo = QuestionRepository(session)
        assert await repo.latest_batch(INTERVIEW_ID) == 2
        assert len(await repo.list_for_interview(INTERVIEW_ID)) == 3
        assert len(await repo.list_for_interview(INTERVIEW_ID, batch=1)) == 3


async def test_a_heavily_annotated_batch_still_lands_with_all_its_questions(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """ADR-0015：批注只点名哪几题有问题，不再决定这份卷子能不能要。"""
    await _seed(db_sessionmaker)

    written = await persist_report(
        db_sessionmaker,
        interview_id=INTERVIEW_ID,
        resume_id="ignored",
        questions=_batch(3),
        batch=1,
        plan=PLAN,
        review_issues=[make_issue("这道题的语义硬伤", index=i) for i in range(3)],
    )

    head, rows = await _read(db_sessionmaker)
    assert (written, rows) == (3, 3)
    assert head.status == "completed"
    assert head.total_questions == 3
    assert len(head.review_issues) == 3


async def test_a_bad_row_rolls_the_whole_report_back(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """一题写坏 = 整份不写。半份 `completed` 报告比没有报告更糟。"""
    await _seed(db_sessionmaker)
    broken = _batch(2)
    broken[1]["difficulty"] = "impossible"

    with pytest.raises(IntegrityError):
        await persist_report(
            db_sessionmaker,
            interview_id=INTERVIEW_ID,
            resume_id="ignored",
            questions=broken,
            batch=1,
            plan=PLAN,
            review_issues=[],
        )

    head, rows = await _read(db_sessionmaker)
    assert head.status == "running"
    assert rows == 0


async def test_a_missing_head_row_is_created_from_the_state(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """脚本与测试也走这条出口：没建行就补一行，`resume_id` 由外键验真伪。"""
    async with session_scope(db_sessionmaker) as session:
        resume = await ResumeRepository(session).create(name="林可", raw_text=RAW)

    await persist_report(
        db_sessionmaker,
        interview_id="a" * 32,
        resume_id=resume.id,
        questions=_batch(1),
        batch=1,
        plan=PLAN,
        review_issues=[],
    )

    async with db_sessionmaker() as session:
        head = await InterviewRepository(session).get_by_id("a" * 32)
        assert head is not None and head.resume_id == resume.id
        assert await QuestionRepository(session).latest_batch("a" * 32) == 1


async def test_a_fabricated_resume_id_leaves_nothing_behind(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_sessionmaker)

    with pytest.raises(IntegrityError):
        await persist_report(
            db_sessionmaker,
            interview_id="b" * 32,
            resume_id="0" * 32,
            questions=_batch(1),
            batch=1,
            plan=PLAN,
            review_issues=[],
        )

    async with db_sessionmaker() as session:
        assert await InterviewRepository(session).get_by_id("b" * 32) is None
