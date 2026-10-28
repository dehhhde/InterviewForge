"""add interview thread_id column

Revision ID: 4c8d1f7a2b93
Revises: 2fe4f8fcb3c6
Create Date: 2026-10-28 15:10:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4c8d1f7a2b93"
down_revision: str | None = "2fe4f8fcb3c6"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Upgrade schema.

    形状与 `2fe4f8fcb3c6` 那条完全一致，原因也完全一致（ADR-0013 / AGENTS.md §3）：
    **不走 `batch_alter_table`**——它重建表要 `DROP TABLE interview`，而
    `question.interview_id` 带 `ON DELETE CASCADE`，`migrations/env.py` 的连接又开了
    `PRAGMA foreign_keys=ON`，两者一撞就把题目删空过（实测发生过，`tests/test_migrations.py`
    的 `scan_migration` 现在把它钉成机器护栏）。
    所以这里发纯 `ALTER TABLE ADD COLUMN … NOT NULL DEFAULT ''`，模型侧同步声明
    `server_default`，否则 `alembic check` 长期报漂移。

    **不回填**：旧报告当时的 `make_thread_config()` 生成的是随机 uuid 且没记在任何地方，
    线程归属这件事在历史上就是丢失的；拿 `interview_id` 去猜一个 uuid 等于造一条假证据。
    空串就是"这份报告没有可寻址的图终态"的诚实形状（导出侧据此回落到"只有题目"的报告）。
    """
    op.execute("ALTER TABLE interview ADD COLUMN thread_id VARCHAR(32) NOT NULL DEFAULT ''")


def downgrade() -> None:
    """Downgrade schema：同样是纯 `ALTER TABLE DROP COLUMN`（本机 SQLite 3.50 支持），不重建表。"""
    op.execute("ALTER TABLE interview DROP COLUMN thread_id")
