"""测试 fixtures：sample_resume / db session / LLM mock〔阶段 1 · §4.7〕

数据库侧的 fixture 只有一种形态：**每个测试一个独立的临时文件库**。

- 不用 `:memory:`——SQLite 内存库是"每个连接一份库"，异步引擎的连接池一换连接，
  上一个连接建好的表就消失了（`app/utils/db.py` 里对此有显式拒绝）。
- 不用共享的 `data/interview_forge.db`——那会让测试之间互相看见，
  也让"跑测试顺手把开发库清了"成为可能。M3 验收里"互不干扰"就是靠 `tmp_path`
  （pytest 的目录按测试函数唯一）保证的。
- 建表走 `Base.metadata.create_all`，**不走 alembic**。迁移本身有专门的测试
  （subprocess 跑 `alembic upgrade head`），别让 200 个测试都去付一次迁移的成本。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models import Base
from app.utils.db import build_engine, build_sessionmaker


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """本测试独占的 sqlite 文件。文件名带上测试 id，失败时一眼能认出是哪个用例留下的。"""
    return f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    """建好表的异步引擎。建表用同步 `create_all`，通过 `run_sync` 塞进同一个连接。"""
    built = build_engine(db_url)
    async with built.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield built
    await built.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """一个事务内的会话：测试结束统一回滚，脏数据不会活到下一个用例。

    需要验证"提交之后还能读到"的用例请直接用 `session_scope`，别在这里 commit。
    """
    async with AsyncSession(engine, expire_on_commit=False) as new:
        yield new
        await new.rollback()


@pytest.fixture
def db_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """提交型会话工厂，给"commit 之后还能不能读到"这类用例用。"""
    return build_sessionmaker(engine)
