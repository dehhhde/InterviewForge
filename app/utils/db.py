"""异步引擎与会话工厂〔阶段 3 · M3〕

引擎只在装配期建一次（`app/dependencies.py` 持有单例），repository 只拿到 `AsyncSession`。
三条 SQLite 特有的坑都在这个文件里处理：

1. **PRAGMA 必须在 connect 事件里发，不能写进 URL**。`foreign_keys` 默认是 OFF，而我们的表靠
   `ON DELETE CASCADE` 收尾——没开这个 pragma，级联删除会"成功"且不报错，只是孤儿题留在表里。
   这类静默失效比崩溃贵。
2. **`expire_on_commit=False`**。默认行为是 commit 后把属性全标成过期，下次读取触发懒加载；
   异步会话里懒加载直接抛 `MissingGreenlet`。repository 返回的对象要在 commit 之后仍然可读，
   所以关掉，代价是"会话内对象可能与库不一致"改由调用方负责。
3. **相对路径的 sqlite 文件要先建目录**，否则首次 `alembic upgrade head` 报
   `unable to open database file`，而报错点和真实原因隔了三层。

内存库（`:memory:`）在这里**不支持**：每个连接一份独立库，测试会"刚写入就查不到"。
用临时文件库（`tests/conftest.py` 的 fixture），别加 `StaticPool` 把这层差异藏起来。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, make_url
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

#: SQLite 的锁等待。默认 0 意味着立刻 `database is locked`；API 进程与后台任务共用一个
#: 文件库时，这个值就是"愿意排队等多久"。5 秒足够覆盖一次批量写入。
SQLITE_BUSY_TIMEOUT_MS = 5000


def _sqlite_file_path(url: URL) -> Path | None:
    """从 sqlite URL 里取本地文件路径；非 sqlite 或内存库返回 None。"""
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    if not database or ":memory:" in database or database.startswith("file:"):
        return None
    # URL 里的相对路径以进程 CWD 为基准，与 aiosqlite 的口径一致。
    path = Path(database)
    return path if path.is_absolute() else Path.cwd() / path


def _apply_sqlite_pragmas(engine: AsyncEngine | Any, busy_timeout_ms: int) -> None:
    """给每个新连接发三条 PRAGMA。`event.listens_for` 只认底层同步引擎。"""
    sync_engine = getattr(engine, "sync_engine", engine)

    @event.listens_for(sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()


def _guard_sqlite_url(url: URL) -> None:
    if url.get_backend_name() != "sqlite":
        return
    if ":memory:" in (url.database or ""):
        raise ValueError(
            "本项目不支持 sqlite 内存库（每连接一份，跨会话读不到）；改用临时文件库。"
        )
    file_path = _sqlite_file_path(url)
    if file_path is not None:
        file_path.parent.mkdir(parents=True, exist_ok=True)


def build_engine(
    database_url: str,
    *,
    busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS,
    echo: bool = False,
    **kwargs: Any,
) -> AsyncEngine:
    """建异步引擎。`settings.database_url` 必须是 `sqlite+aiosqlite://…` 这类异步方言。"""
    url = make_url(database_url)
    _guard_sqlite_url(url)
    engine = create_async_engine(url, echo=echo, **kwargs)
    if url.get_backend_name() == "sqlite":
        _apply_sqlite_pragmas(engine, busy_timeout_ms)
    return engine


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """会话工厂。见模块 docstring 第 2 条：`expire_on_commit=False` 是异步下的必需品。"""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """一个事务边界：正常退出 commit，异常 rollback 后原样抛出。

    repository 内部**不**调 commit——提交时机由调用方（service / 收尾节点）决定，
    否则一次业务写入会被切成多个事务，§4.6 要求的幂等就没法保证。
    """
    async with sessionmaker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        await session.commit()


def build_sync_engine(database_url: str, **kwargs: Any) -> Any:
    """同步引擎，只给 alembic 的 migration 上下文用。

    alembic 的 `env.py` 跑在同步上下文里；把 async 引擎塞进去要到处 `run_sync`，
    更容易出错。所以这里把驱动名从 `sqlite+aiosqlite` 换成 `sqlite`，其余参数一致。
    """
    url = make_url(database_url)
    _guard_sqlite_url(url)
    sync_url = url.set(drivername="sqlite") if url.get_backend_name() == "sqlite" else url
    engine = create_engine(sync_url, **kwargs)
    if sync_url.get_backend_name() == "sqlite":
        _apply_sqlite_pragmas(engine, SQLITE_BUSY_TIMEOUT_MS)
    return engine
