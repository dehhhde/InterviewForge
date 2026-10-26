"""`app/dependencies.py`：进程级引擎单例与 `get_db` 的事务边界〔M3 收尾，无 LLM〕

值得单独测的两件事：

1. `get_db` 是"repository 只 flush、提交归调用方"这条铁律唯一的落点。它是 FastAPI 的
   **生成器依赖**，而生成器的收尾时机（响应生成之后）决定了 commit 真正发生在哪一刻，
   所以"提交/回滚"两条都用真 ASGI 应用跑，不用手工驱动生成器去近似。
2. 引擎是**全局状态**。全局状态必须能被复位，否则上一个用例建的连接池会漏进下一个用例
   的临时库——autouse fixture 负责每次复位，用例本身就不必关心执行顺序。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import get_settings
from app.dependencies import dispose_engine, get_db, get_engine, get_sessionmaker
from app.repositories.resume_repository import ResumeRepository
from app.utils.db import build_engine, build_sessionmaker, session_scope

#: 依赖写在注解里而不是参数默认值里：后者会被 ruff B008 判成"默认值里做函数调用"，
#: 而 M4 的 API 层本来就该用这种形状，测试顺便把它钉住。
SessionDep = Annotated[AsyncSession, Depends(get_db)]


@pytest.fixture(autouse=True)
async def _singleton_points_at_this_tests_database(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """把单例指到本用例的临时库，前后各复位一次。

    先 dispose 是为了丢掉**上一个用例**缓存下来的引擎：单例按 URL 失效，而 URL 刚被
    monkeypatch 换掉，不复位就可能拿着一个已关闭的池继续用。
    """
    monkeypatch.setattr(get_settings(), "database_url", db_url)
    await dispose_engine()
    yield
    await dispose_engine()


def _point_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> str:
    url = f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}"
    monkeypatch.setattr(get_settings(), "database_url", url)
    return url


async def test_the_engine_is_reused_while_the_url_stays_put() -> None:
    """同一进程里两次取引擎必须是同一个对象——两台引擎就是两套连接池抢一个 SQLite 文件。"""
    first = get_engine()
    assert get_engine() is first
    await dispose_engine()
    assert get_engine() is not first  # 复位之后重新建，不复用已关池的那台


async def test_a_changed_url_gets_an_engine_bound_to_the_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """换库要看到新引擎：测试靠改 `database_url` 隔离，缓存不能把它吃掉。"""
    first = get_engine()
    _point_at(tmp_path, monkeypatch, "other.db")
    second = get_engine()
    assert second is not first
    assert str(second.url).endswith("other.db")


async def test_the_sessionmaker_is_bound_to_the_singleton_engine() -> None:
    """`expire_on_commit=False` 不能丢：异步会话 commit 后属性过期，读取就抛 MissingGreenlet。"""
    maker = get_sessionmaker()
    assert maker.kw["bind"] is get_engine()
    assert maker.kw["expire_on_commit"] is False


async def test_a_memory_url_is_refused_instead_of_quietly_working(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """单例不能替工厂兜底。`:memory:` 每连接一份库，接住它只会把问题推成"刚写入就查不到"。"""
    monkeypatch.setattr(get_settings(), "database_url", "sqlite+aiosqlite:///:memory:")
    with pytest.raises(ValueError, match="内存库"):
        get_engine()


async def _count_rows(db_url: str) -> int:
    """换一台引擎去数行——同会话里读得到只证明缓存，不证明提交。"""
    reader = build_engine(db_url)
    try:
        async with session_scope(build_sessionmaker(reader)) as second:
            return await ResumeRepository(second).count()
    finally:
        await reader.dispose()


async def test_a_successful_request_is_visible_to_the_next_connection(engine: AsyncEngine) -> None:
    """端点正常返回 → `get_db` 提交。判据是**另一台引擎**读得到，而不是同会话里读得到。"""
    app = FastAPI()

    @app.post("/resumes")
    async def _write(session: SessionDep) -> dict[str, bool]:
        await ResumeRepository(session).create(name="林可", raw_text="林可")
        return {"written": True}

    db_url = str(get_engine().url)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://if.test") as client:
        response = await client.post("/resumes")
    assert response.status_code == 200
    assert response.json() == {"written": True}
    assert await _count_rows(db_url) == 1


async def test_a_failing_request_leaves_nothing_behind(engine: AsyncEngine) -> None:
    """端点抛 `HTTPException` → 回滚。写了一半的请求不该在库里留下孤行。"""
    app = FastAPI()

    @app.post("/resumes")
    async def _write_then_fail(session: SessionDep) -> None:
        await ResumeRepository(session).create(name="周砚", raw_text="周砚")
        raise HTTPException(status_code=404, detail="岗位不存在")

    db_url = str(get_engine().url)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://if.test") as client:
        response = await client.post("/resumes")
    assert response.status_code == 404
    assert await _count_rows(db_url) == 0
