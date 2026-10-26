"""四个子类的自有查询〔阶段 3 · M3〕

`batch` 那组用例是计划 §6 风险 5 的落库侧保险：同一份报告可以落下多批题，
报告只能展示最新那一批，而且**旧批次不许被删掉**——它是"上一版长什么样"的唯一痕迹。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.interview import Interview
from app.models.resume import Resume
from app.repositories.interview_repository import InterviewRepository
from app.repositories.job_repository import JobRepository
from app.repositories.question_repository import QuestionRepository
from app.repositories.resume_repository import ResumeRepository

_QUESTION = {"stem": "讲讲线程池的核心参数", "difficulty": "medium", "source": "generic"}


async def _report(session) -> Interview:
    resume = Resume(name="林可", raw_text="林可")
    session.add(resume)
    await session.flush()
    report = Interview(resume_id=resume.id, plan={})
    session.add(report)
    await session.flush()
    return report


async def test_resume_is_found_by_its_stored_path(session) -> None:
    repo = ResumeRepository(session)
    created = await repo.create(name="林可", raw_text="林可", stored_path="seed/seed-resume-1.md")
    found = await repo.get_by_stored_path("seed/seed-resume-1.md")
    assert found is created
    assert await repo.get_by_stored_path("seed/nope.md") is None


async def test_job_natural_key_finds_the_same_row(session) -> None:
    repo = JobRepository(session)
    created = await repo.create(title="后端开发工程师", company="蓝鲸云计算", jd_text="JD")
    again = await repo.get_by_title_and_company("后端开发工程师", "蓝鲸云计算")
    assert again is created
    assert await repo.get_by_title_and_company("后端开发工程师", "别的公司") is None


async def test_same_title_at_a_different_company_is_allowed(session) -> None:
    repo = JobRepository(session)
    await repo.create(title="后端开发工程师", company="蓝鲸云计算", jd_text="JD")
    other = await repo.create(title="后端开发工程师", company="灯塔智能", jd_text="JD")
    assert other.id
    assert await repo.count() == 2


async def test_same_title_and_company_is_rejected_by_the_database(session) -> None:
    repo = JobRepository(session)
    await repo.create(title="后端开发工程师", company="蓝鲸云计算", jd_text="JD")
    with pytest.raises(IntegrityError):
        await repo.create(title="后端开发工程师", company="蓝鲸云计算", jd_text="抄来的 JD")
    await session.rollback()


async def test_an_empty_report_has_no_questions(session) -> None:
    report = await _report(session)
    repo = QuestionRepository(session)
    assert await repo.latest_batch(report.id) is None
    assert await repo.list_for_interview(report.id) == []


async def test_reading_a_report_gives_the_newest_batch_only(session) -> None:
    report = await _report(session)
    repo = QuestionRepository(session)
    await repo.add_many([_QUESTION, _QUESTION], interview_id=report.id, batch=1)
    redone = dict(_QUESTION, stem="线程池的拒绝策略有哪些？")
    await repo.add_many([redone], interview_id=report.id, batch=2)

    assert await repo.latest_batch(report.id) == 2
    newest = await repo.list_for_interview(report.id)
    assert [q.stem for q in newest] == ["线程池的拒绝策略有哪些？"]
    # 旧批次还在，只是默认不展示——回退痕迹要能查回来。
    assert len(await repo.list_for_interview(report.id, batch=1)) == 2


async def test_questions_are_numbered_within_a_batch(session) -> None:
    report = await _report(session)
    rows = await QuestionRepository(session).add_many(
        [_QUESTION, dict(_QUESTION, stem="RPC 与 HTTP 调用的差别是什么？")],
        interview_id=report.id,
        batch=1,
    )
    assert [q.order_index for q in rows] == [0, 1]
    assert all(q.interview_id == report.id for q in rows)


async def test_the_same_question_twice_in_one_batch_is_rejected(session) -> None:
    """收尾节点必须幂等：同一批内 `order_index` 撞车就是重复落库。"""
    report = await _report(session)
    repo = QuestionRepository(session)
    await repo.add_many([_QUESTION], interview_id=report.id, batch=1)
    with pytest.raises(IntegrityError):
        await repo.add_many([_QUESTION], interview_id=report.id, batch=1)
    await session.rollback()


async def test_reports_of_one_resume_come_back_newest_first(session) -> None:
    resume = Resume(name="林可", raw_text="林可")
    session.add(resume)
    await session.flush()
    repo = InterviewRepository(session)
    first = await repo.create(resume_id=resume.id, plan={})
    second = await repo.create(resume_id=resume.id, plan={})
    listed = await repo.list_for_resume(resume.id)
    assert {r.id for r in listed} == {first.id, second.id}
    # 只断言"不递增"：同一微秒内创建的两行谁前谁后 SQLite 不作保证。
    stamps = [r.created_at for r in listed]
    assert stamps == sorted(stamps, reverse=True)
    assert await repo.list_for_resume("0" * 32) == []


async def test_status_moves_only_among_the_three_known_values(session) -> None:
    repo = InterviewRepository(session)
    report = await _report(session)
    created = await repo.create(resume_id=report.resume_id, plan={})
    assert created.status == "running"
    completed = await repo.set_status(created.id, "completed")
    assert completed is not None and completed.status == "completed"
    aborted = await repo.set_status(created.id, "aborted")
    assert aborted is not None and aborted.status == "aborted"
    with pytest.raises(ValueError, match="未知的报告状态"):
        await repo.set_status(created.id, "done")


async def test_a_report_without_a_resume_is_rejected(session) -> None:
    with pytest.raises(IntegrityError):
        await InterviewRepository(session).create(resume_id="0" * 32, plan={})
    await session.rollback()
