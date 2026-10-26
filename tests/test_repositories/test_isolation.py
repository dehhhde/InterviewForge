"""用例之间的库隔离〔阶段 3 · M3 验收第三条〕

"独立临时库互不干扰"不是配置出来的性质，是 `tmp_path` 给的：pytest 为每个用例造一个
专属目录，库文件放在里面，于是"上一个用例写的行"在物理上就到不了下一个用例。
下面三条把这件事钉住——最后两条的顺序依赖是刻意的：`test_a_fresh_database_starts_empty`
必须排在写入用例之后跑，它断言的正是"前面灌过数据的库不会被我看见"。
"""

from __future__ import annotations

from pathlib import Path

from app.repositories.resume_repository import ResumeRepository
from app.utils.db import build_engine, build_sessionmaker, session_scope


async def test_the_database_file_lives_inside_this_tests_tmp_dir(
    db_url: str, tmp_path: Path
) -> None:
    assert tmp_path.as_posix() in db_url
    assert db_url.startswith("sqlite+aiosqlite:///")


async def test_a_committed_write_is_readable_by_a_second_engine(db_url: str, session) -> None:
    """换一台引擎去读，比查文件存在更有意义：文件在不代表事务提交了。

    真正的**跨进程**持久化由 `tests/test_migrations.py` 用子进程验，
    这条只保证"跨连接可见"，两层别混。
    """
    await ResumeRepository(session).create(name="林可", raw_text="林可")
    await session.commit()

    other = build_engine(db_url)
    try:
        async with session_scope(build_sessionmaker(other)) as second:
            assert await ResumeRepository(second).count() == 1
    finally:
        await other.dispose()


async def test_a_fresh_database_starts_empty(session) -> None:
    """本用例的库是刚建的：上一条灌进去的那行不在这里。"""
    assert await ResumeRepository(session).count() == 0
