"""迁移与跨进程持久化〔阶段 3 · M3 验收〕

这三条验收（`alembic upgrade head` 能建库 / 重启进程后数据仍在 / 用例互不干扰）
本质上都是**进程边界**问题，所以前两组用例必须真起子进程：
在测试进程里 `create_all` 出来的库，永远证明不了迁移能跑。

最后一组不跑 alembic，只**读迁移源码**：ADR-0013 记过一次数据事故（`batch_alter_table`
重建表时 `DROP TABLE` 触发 `ON DELETE CASCADE`，清空了开发库 30 行题目），
那种形状只有静态护栏挡得住——真跑到那一步就已经是事故现场了。
"""

from __future__ import annotations

import ast
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TABLES = {"resume", "job", "question", "interview"}
_MIGRATIONS_DIR = PROJECT_ROOT / "migrations" / "versions"

#: `with op.batch_alter_table(...) as b:` 块体内允许出现的调用名。
#: `f` 是 alembic 自己的命名辅助（`b.f('ix_task_kind')`），不碰表结构。
BATCH_ALLOWED_CALLS: frozenset[str] = frozenset({"create_index", "drop_index", "f"})


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


def _columns(database: Path, table: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(database) as connection:
        return list(connection.execute(f"pragma table_info({table})").fetchall())


def test_thread_id_column_lands_on_rows_that_already_exist(tmp_path: Path) -> None:
    """T-304 的形状：给已有表加非空列，旧行拿到的是**空串**而不是"猜一个线程"。

    `interview.thread_id` 是图终态的唯一寻址方式，但历史线程当初没留名字，回填等于编造。
    所以这条用例钉两件事：裸 `ALTER TABLE … NOT NULL DEFAULT ''` 在**有行的表**上真的过得去
    （SQLite 只在这一种组合下允许），以及旧行读出来是空串——导出那条路据此知道自己该降级。
    """
    database = tmp_path / "legacy.db"
    assert (
        _run("-m", "alembic", "upgrade", "2fe4f8fcb3c6", database=database).returncode == 0
    ), "退到 thread_id 之前的那一版才叫历史库"

    with sqlite3.connect(database) as connection:
        connection.execute(
            "insert into interview (id, resume_id, status, plan, total_questions,"
            " review_issues, created_at, updated_at)"
            " values ('iv-old', 'r-1', 'completed', '{}', 0, '[]',"
            " '2026-10-16 09:00:00', '2026-10-16 09:00:00')"
        )

    assert _run("-m", "alembic", "upgrade", "head", database=database).returncode == 0
    with sqlite3.connect(database) as connection:
        row = connection.execute("select thread_id from interview where id = 'iv-old'").fetchone()
    assert row == ("",)

    assert _run("-m", "alembic", "downgrade", "-1", database=database).returncode == 0
    assert "thread_id" not in {column[1] for column in _columns(database, "interview")}


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


# --- 迁移形状静态护栏（T-016 / BACKLOG OPT-4.6）---


def _call_name(func: ast.expr) -> str:
    """`batch_op.add_column` → `add_column`；`op.execute` → `execute`。取不到就回空串。"""
    return func.attr if isinstance(func, ast.Attribute) else ""


def _batch_blocks(tree: ast.AST) -> Iterator[tuple[ast.With, str]]:
    """产出 `with op.batch_alter_table(...) as x:` 那种 With 节点，连同它的别名 `x`。

    只认挂在别名上的调用：块体里 `sa.Column(...)`、`op.f(...)` 是参数的一部分，
    不是"对表做的改动"，把它们算进判据会让护栏见谁报谁。
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            call = item.context_expr
            if isinstance(call, ast.Call) and _call_name(call.func) == "batch_alter_table":
                alias = item.optional_vars
                yield node, alias.id if isinstance(alias, ast.Name) else ""
                break


def _literal_text(node: ast.expr) -> str:
    """把 `op.execute(...)` 的实参摊成纯文本，f-string 的插值位留成空格。

    必须处理 f-string：ADR-0013 那条裸 ALTER 就是 `f"ALTER TABLE ... {name} ..."`，
    只认 `ast.Constant` 会让它整条从护栏里漏出去。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            else:
                parts.append(" ")
        return "".join(parts)
    return ""


def scan_migration(source: str) -> list[str]:
    """返回这份迁移的**违规形状清单**（空列表 = 干净）。

    两条判据，都来自实测过的事故，不是风格偏好：

    1. `with op.batch_alter_table(...) as b:` 的块体内只许 `create_index` / `drop_index`。
       batch 模式的实现是"建临时表 + `DROP TABLE` 旧表"，而 `migrations/env.py` 的
       `build_sync_engine` 已经发过 `PRAGMA foreign_keys=ON`，于是带 `ON DELETE CASCADE`
       的外键会真的把数据带走。索引不涉及重建数据，才是安全的例外。
       （原判据"出现即拒绝"会把三条 autogenerate 的历史迁移全判红 —— 见 TASK_PLAN §5。）
    2. 手写 `op.execute("ALTER TABLE ...")` 里出现 `NOT NULL` 就必须同时出现 `DEFAULT`：
       SQLite 给已有表加非空列只在这一种组合下不报错。
    """
    tree = ast.parse(source)
    problems: list[str] = []

    for block, alias in _batch_blocks(tree):
        for node in ast.walk(block):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            target = node.func.value
            if not (isinstance(target, ast.Name) and target.id == alias):
                continue
            if node.func.attr in BATCH_ALLOWED_CALLS:
                continue
            problems.append(
                f"第 {node.lineno} 行：batch_alter_table 块体内出现 {node.func.attr}()"
                "（只允许 create_index / drop_index；给已有表加列请写裸 ALTER TABLE ADD COLUMN）"
            )

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _call_name(node.func) == "execute"):
            continue
        text = _literal_text(node.args[0]) if node.args else ""
        upper = text.upper()
        if "ALTER TABLE" in upper and "NOT NULL" in upper and "DEFAULT" not in upper:
            problems.append(
                f"第 {node.lineno} 行：手写 ALTER TABLE 加 NOT NULL 列却没有 DEFAULT 子句"
            )

    return problems


def _migration_sources() -> list[tuple[Path, str]]:
    return [
        (path, path.read_text(encoding="utf-8"))
        for path in sorted(_MIGRATIONS_DIR.glob("*.py"))
        if path.name != "__init__.py"
    ]


def test_every_shipped_migration_has_a_safe_shape() -> None:
    """现有迁移必须逐份干净——护栏第一次跑就红，它挡不住未来的第二次事故。"""
    sources = _migration_sources()
    assert sources, f"{_MIGRATIONS_DIR} 下一份迁移都没有，护栏在空转"
    for path, source in sources:
        assert scan_migration(source) == [], path.name


def test_the_guard_passes_the_index_only_batch_shape() -> None:
    """autogenerate 的标准产物（只有索引）判干净，护栏不许变成红门。"""
    source = '''
def upgrade() -> None:
    with op.batch_alter_table("question", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_question_batch"), ["batch"], unique=False)

def downgrade() -> None:
    with op.batch_alter_table("question", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_question_batch"))
    op.drop_table("question")
'''
    assert scan_migration(source) == []


@pytest.mark.parametrize(
    "body",
    [
        "batch_op.add_column(sa.Column('x', sa.String(), nullable=False))",
        "batch_op.drop_column('x')",
        "batch_op.alter_column('x', nullable=True)",
        "batch_op.create_check_constraint('ck_x', 'x > 0')",
    ],
)
def test_the_guard_rejects_column_changes_inside_batch(body: str) -> None:
    """事故形状逐个点名：这四种调用会把 `question` 表连同 CASCADE 一起重建掉。"""
    source = f'''
def upgrade() -> None:
    with op.batch_alter_table("question", schema=None) as batch_op:
        {body}
'''
    problems = scan_migration(source)
    assert len(problems) == 1, problems
    assert "batch_alter_table" in problems[0]


def test_the_guard_requires_a_default_on_hand_written_alter() -> None:
    """裸 `ALTER TABLE` 是 ADR-0013 指定的写法，但它必须自带 DEFAULT。"""
    safe = '''
def upgrade() -> None:
    for name in ("resume_name",):
        op.execute(f"ALTER TABLE interview ADD COLUMN {name} VARCHAR(120) NOT NULL DEFAULT ''")

def downgrade() -> None:
    op.execute("ALTER TABLE interview DROP COLUMN resume_name")
'''
    risky = '''
def upgrade() -> None:
    op.execute("ALTER TABLE interview ADD COLUMN resume_name VARCHAR(120) NOT NULL")
'''
    assert scan_migration(safe) == []
    problems = scan_migration(risky)
    assert len(problems) == 1
    assert "DEFAULT" in problems[0]
