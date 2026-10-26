"""模型层与引擎工厂的行为〔阶段 3 · M3〕

断言全部落在结构与约束上：这个里程碑不碰 LLM，也就没有任何文案可以断言。
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import Base, Interview, Job, Question, Resume
from app.utils.db import SQLITE_BUSY_TIMEOUT_MS, build_engine


async def test_sqlite_pragmas_are_applied_on_every_connection(engine) -> None:
    """三条 PRAGMA 是引擎工厂的全部承诺；缺任一条都是静默失效，不是报错。"""
    async with engine.connect() as connection:
        assert (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
        busy = (await connection.execute(text("PRAGMA busy_timeout"))).scalar_one()
        assert busy == SQLITE_BUSY_TIMEOUT_MS
        mode = (await connection.execute(text("PRAGMA journal_mode"))).scalar_one()
        assert str(mode).lower() == "wal"


def test_memory_database_is_refused() -> None:
    """:memory: 在异步连接池下"每连接一份库"，本项目直接拒绝而不是偷偷支持。"""
    with pytest.raises(ValueError, match="内存库"):
        build_engine("sqlite+aiosqlite:///:memory:")


def test_relative_sqlite_path_creates_its_directory(tmp_path, monkeypatch) -> None:
    """建库前要把目录造出来，否则 sqlalchemy 报的是 `unable to open database file`，
    而真实原因（目录不存在）在报错信息里完全看不出来。"""
    monkeypatch.chdir(tmp_path)
    engine = build_engine("sqlite+aiosqlite:///nested/dir/app.db")
    assert (tmp_path / "nested" / "dir").is_dir()
    engine.sync_engine.dispose()


def test_four_tables_are_registered() -> None:
    assert {"resume", "job", "question", "interview"} <= set(Base.metadata.tables)


async def test_primary_key_and_timestamps_come_from_python(session) -> None:
    resume = Resume(name="林可", raw_text="林可")
    session.add(resume)
    await session.flush()
    assert re.fullmatch(r"[0-9a-f]{32}", resume.id)
    assert resume.created_at.tzinfo is None
    # 两个时间戳是两次 `utcnow()` 调用，微秒级不等是正常的；只断言"同一时刻附近且不倒挂"。
    assert resume.updated_at >= resume.created_at
    assert (resume.updated_at - resume.created_at).total_seconds() < 1


def test_updated_at_is_stamped_by_the_mixin_not_by_the_caller() -> None:
    """`onupdate` 挂在列上，所以调用方不需要（也不该）手填时间戳。

    断言的是"这一列绑了一个取时间的可调用"，而不是"时间真的往前走了"：
    后者要靠 sleep 才稳定，而微秒级的 SQLite 时间戳本来就常常相等。
    """
    updated_at = Resume.__table__.c.updated_at
    assert callable(updated_at.onupdate.arg)
    assert updated_at.onupdate.arg.__name__ == "utcnow_naive"
    assert updated_at.server_default is None


async def test_interview_status_is_checked_in_the_database(session) -> None:
    resume = Resume(name="林可", raw_text="林可")
    session.add(resume)
    await session.flush()
    report = Interview(resume_id=resume.id, plan={})
    session.add(report)
    await session.flush()

    report.status = "complated"
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_question_difficulty_is_checked_in_the_database(session) -> None:
    """schema 的 Literal 之外还有一道 DB CHECK：写入路径不止 API 一条。"""
    resume = Resume(name="林可", raw_text="林可")
    session.add(resume)
    await session.flush()
    report = Interview(resume_id=resume.id, plan={})
    session.add(report)
    await session.flush()

    session.add(
        Question(
            interview_id=report.id,
            batch=1,
            order_index=0,
            stem="讲讲事务隔离级别",
            difficulty="extreme",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_deleting_a_resume_removes_its_reports_and_questions(session) -> None:
    """`ON DELETE CASCADE` 真的生效——它依赖 `PRAGMA foreign_keys=ON`，上面已单独验过。"""
    resume = Resume(name="林可", raw_text="林可")
    job = Job(title="后端开发工程师", company="蓝鲸云计算", jd_text="JD")
    session.add_all([resume, job])
    await session.flush()

    report = Interview(resume_id=resume.id, job_id=job.id, plan={})
    session.add(report)
    await session.flush()
    session.add(
        Question(
            interview_id=report.id,
            batch=1,
            order_index=0,
            stem="Spring 的事务传播行为有哪些？",
            difficulty="medium",
        )
    )
    await session.flush()

    # 先取 id 再 expire：级联是 SQLite 干的，身份映射里的旧对象不会自己变没，
    # 而访问已过期对象的属性会触发同步懒加载（异步下直接 MissingGreenlet）。
    report_id = report.id
    await session.delete(resume)
    await session.flush()
    session.expire_all()
    assert await session.get(Interview, report_id) is None
    remaining = (await session.execute(text("select count(*) from question"))).scalar_one()
    assert remaining == 0


async def test_deleting_a_job_keeps_the_report_and_nulls_the_link(session) -> None:
    """唯一不级联的关系：岗位下线不该把已发出的面试题一起删掉。"""
    resume = Resume(name="林可", raw_text="林可")
    job = Job(title="前端开发工程师", company="灯塔智能", jd_text="JD")
    session.add_all([resume, job])
    await session.flush()
    report = Interview(resume_id=resume.id, job_id=job.id, plan={})
    session.add(report)
    await session.flush()

    report_id = report.id
    await session.delete(job)
    await session.flush()
    session.expire_all()
    reloaded = await session.get(Interview, report_id)
    assert reloaded is not None
    assert reloaded.job_id is None
