"""add interview resume/job snapshot columns

Revision ID: 2fe4f8fcb3c6
Revises: f73e2c144737
Create Date: 2026-10-21 10:35:16.352105

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2fe4f8fcb3c6"
down_revision: str | Sequence[str] | None = "f73e2c144737"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (列名, SQL 类型)。四列同构，循环发语句，别让四份复制粘贴各跑各的。
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("resume_name", "VARCHAR(120)"),
    ("resume_file_name", "VARCHAR(255)"),
    ("job_title", "VARCHAR(200)"),
    ("job_company", "VARCHAR(200)"),
)


def upgrade() -> None:
    """Upgrade schema.

    **不用 `batch_alter_table`，这里是踩过的坑**：batch 模式重建表要 `DROP TABLE interview`，
    而 `question.interview_id` 带 `ON DELETE CASCADE`，`migrations/env.py` 走的
    `build_sync_engine` 又给每个连接发了 `PRAGMA foreign_keys=ON`（应用侧本来就需要它来保证
    级联删除真的生效）。两者一撞，"给报告加四列"就把整张题目表删空了——实测在开发库上发生过。
    所以这里发的是纯 `ALTER TABLE ADD COLUMN`：SQLite 允许给已有表加 NOT NULL 列，只要带
    `DEFAULT`，不需要重建表，也就碰不到任何外键。模型侧同步声明 `server_default`，
    否则 `alembic check` 会长期报漂移。
    """
    for name, sql_type in _COLUMNS:
        op.execute(f"ALTER TABLE interview ADD COLUMN {name} {sql_type} NOT NULL DEFAULT ''")

    # 历史报告回填：库里那 10 份卷子当时没人记录"给谁、按哪个岗位出的"，
    # 趁 `resume` / `job` 行还在，按外键把名字抄进快照列。
    # `NULLIF(name,'')` 兜的是 `run_e2e_live.py` 那条链路——它不回写 `resume.name`，
    # 空名会让快照列一片空白，退回文件名比空白有用。
    op.execute(
        """
        UPDATE interview SET
            resume_name = COALESCE((
                SELECT NULLIF(name, '') FROM resume WHERE resume.id = interview.resume_id
            ), ''),
            resume_file_name = COALESCE((
                SELECT file_name FROM resume WHERE resume.id = interview.resume_id
            ), ''),
            job_title = COALESCE((
                SELECT title FROM job WHERE job.id = interview.job_id
            ), ''),
            job_company = COALESCE((
                SELECT company FROM job WHERE job.id = interview.job_id
            ), '')
        """
    )


def downgrade() -> None:
    """Downgrade schema.

    同样是纯 `ALTER TABLE DROP COLUMN`（SQLite 3.35+ 支持，本机 3.50），不重建表。
    """
    for name, _sql_type in _COLUMNS:
        op.execute(f"ALTER TABLE interview DROP COLUMN {name}")
