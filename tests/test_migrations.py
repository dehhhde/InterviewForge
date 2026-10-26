"""迁移与跨进程持久化〔阶段 3 · M3 验收〕

这三条验收（`alembic upgrade head` 能建库 / 重启进程后数据仍在 / 用例互不干扰）
本质上都是**进程边界**问题，所以前两组用例必须真起子进程：
在测试进程里 `create_all` 出来的库，永远证明不了迁移能跑。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TABLES = {"resume", "job", "question", "interview"}


def _run(*args: str, database: Path) -> subprocess.CompletedProcess[str]:
    """在项目根跑一个子进程，并把库指到 `database`。

    `encoding="utf-8"` 是必需的：alembic 的输出走 GBK 控制台时会把中文截成乱码，
    失败信息就没法读了。
    """
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT),
        "PYTHONIOENCODING": "utf-8",
        # 迁移侧用同步驱动（`migrations/env.py` 会再降级一次驱动名）
        "ALEMBIC_DATABASE_URL": f"sqlite:///{database.as_posix()}",
        "DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}",
    }
    return subprocess.run(
        [sys.executable, *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _tables_in(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute("select name from sqlite_master where type='table'").fetchall()
    return {row[0] for row in rows}


def test_upgrade_head_creates_the_four_tables(tmp_path: Path) -> None:
    database = tmp_path / "migrated.db"
    result = _run("-m", "alembic", "upgrade", "head", database=database)
    assert result.returncode == 0, result.stderr[-2000:]
    assert _TABLES.issubset(_tables_in(database))
    assert "alembic_version" in _tables_in(database)


def test_downgrade_base_removes_everything(tmp_path: Path) -> None:
    """迁移必须能退回来。`downgrade()` 没人跑过一次，就等于没有。"""
    database = tmp_path / "roundtrip.db"
    assert _run("-m", "alembic", "upgrade", "head", database=database).returncode == 0
    result = _run("-m", "alembic", "downgrade", "base", database=database)
    assert result.returncode == 0, result.stderr[-2000:]
    assert _tables_in(database) == {"alembic_version"}


def test_models_and_migrations_have_not_drifted(tmp_path: Path) -> None:
    """`alembic check`：模型定义与迁移序列不一致就失败。

    这条是防"加了列忘了生成迁移"——那种 bug 在生产上表现为新列读不到，
    而不是任何一条报错，靠人肉想起来是不可能的。
    """
    database = tmp_path / "drift.db"
    assert _run("-m", "alembic", "upgrade", "head", database=database).returncode == 0
    result = _run("-m", "alembic", "check", database=database)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "New upgrade operations detected" not in result.stdout


def test_a_process_that_ran_migrations_can_be_read_by_another(tmp_path: Path) -> None:
    """建库进程与读写进程分开——这就是"重启进程后数据仍在"的可执行版本。"""
    database = tmp_path / "seeded.db"
    assert _run("-m", "alembic", "upgrade", "head", database=database).returncode == 0
    seeded = _run("-m", "app.scripts.seed", database=database)
    assert seeded.returncode == 0, seeded.stderr[-2000:]

    with sqlite3.connect(database) as connection:
        resume_rows = connection.execute("select id, name, raw_text from resume").fetchall()
        job_rows = connection.execute("select title, company from job").fetchall()
        # WAL 的附属文件在最后一次连接干净关闭时会被合并回主库，所以这里只要求主库文件在。
        assert database.exists()

    assert len(resume_rows) == 3
    assert len(job_rows) == 2
    assert all("seed-resume-" in row[0] for row in resume_rows)
    assert all(len(row[2]) > 40 for row in resume_rows)
