"""BaseRepository 的泛型 CRUD 行为〔阶段 3 · M3〕

这一层最值得测的不是"能存能取"，而是**边界**：谁提交、白名单挡谁、找不到时返回什么。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.resume import Resume
from app.repositories.resume_repository import ResumeRepository


async def test_create_then_get_by_id_round_trips(session) -> None:
    repo = ResumeRepository(session)
    created = await repo.create(name="林可", raw_text="林可 · 后端", parsed={"name": "林可"})
    fetched = await repo.get_by_id(created.id)
    assert fetched is created
    assert fetched.parsed == {"name": "林可"}


async def test_create_populates_id_before_commit(session) -> None:
    """`id` 是 Python 侧默认值，`flush` 之后就绪——这是节点收尾能立刻拿到外键的前提。"""
    created = await ResumeRepository(session).create(name="周砚", raw_text="周砚")
    assert len(created.id) == 32
    assert created.created_at is not None


async def test_repository_never_commits(engine, session) -> None:
    """另一个连接读不到未提交的行——提交时机归调用方（`session_scope`）管。"""
    await ResumeRepository(session).create(name="苏澈", raw_text="苏澈")
    async with engine.connect() as other:
        visible = (await other.execute(text("select count(*) from resume"))).scalar_one()
    assert visible == 0
    await session.commit()
    async with engine.connect() as again:
        after = (await again.execute(text("select count(*) from resume"))).scalar_one()
    assert after == 1


async def test_get_by_id_returns_none_instead_of_raising(session) -> None:
    """404 是 API 层的语义；repository 不知道自己在服务 HTTP 请求。"""
    assert await ResumeRepository(session).get_by_id("0" * 32) is None


async def test_list_orders_by_the_newest_first_and_pages(session) -> None:
    repo = ResumeRepository(session)
    for index in range(5):
        await repo.create(name=f"简历{index}", raw_text=f"raw {index}")
    first_page = await repo.list(limit=2)
    second_page = await repo.list(limit=2, offset=2)
    assert len(first_page) == 2
    assert len(second_page) == 2
    assert {row.id for row in first_page}.isdisjoint({row.id for row in second_page})
    # 默认倒序按 created_at：同一微秒内插的两行时间戳相等，所以只断言"不递增"，不断言具体次序。
    stamps = [row.created_at for row in first_page]
    assert stamps == sorted(stamps, reverse=True)


async def test_list_rejects_unknown_order_by(session) -> None:
    """白名单是 `order_by` 唯一的注入口——`__table__`、`metadata` 这类属性名必须被挡住。"""
    for hostile in ("password", "__table__", "metadata; drop table resume"):
        with pytest.raises(ValueError, match="无列"):
            await ResumeRepository(session).list(order_by=hostile)


async def test_list_applies_sql_filters(session) -> None:
    repo = ResumeRepository(session)
    await repo.create(name="林可", raw_text="A")
    await repo.create(name="林可", raw_text="B")
    await repo.create(name="周砚", raw_text="C")
    matched = await repo.list(filters=[Resume.name == "林可"])
    assert {row.raw_text for row in matched} == {"A", "B"}
    assert await repo.count(filters=[Resume.name == "林可"]) == 2


async def test_update_touches_only_the_given_columns(session) -> None:
    repo = ResumeRepository(session)
    created = await repo.create(name="林可", raw_text="原文", file_name="old.pdf")
    updated = await repo.update(created.id, raw_text="新原文")
    assert updated is not None
    assert updated.raw_text == "新原文"
    assert updated.file_name == "old.pdf"
    assert updated.name == "林可"


async def test_update_rejects_unknown_columns(session) -> None:
    repo = ResumeRepository(session)
    created = await repo.create(name="林可", raw_text="原文")
    with pytest.raises(ValueError, match="没有列"):
        await repo.update(created.id, is_admin=True)


async def test_update_and_delete_report_a_missing_row(session) -> None:
    repo = ResumeRepository(session)
    assert await repo.update("0" * 32, name="没人") is None
    assert await repo.delete("0" * 32) is False
    created = await repo.create(name="林可", raw_text="原文")
    assert await repo.delete(created.id) is True
    assert await repo.get_by_id(created.id) is None


async def test_model_columns_are_the_whitelist_source(session) -> None:
    """`column_names()` 与真实列一一对应，白名单才不会随着加列而失真。"""
    assert set(Resume.column_names()) == {c.key for c in Resume.__table__.columns}
