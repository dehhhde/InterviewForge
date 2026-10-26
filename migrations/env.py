"""迁移运行环境〔阶段 3 · M3〕

alembic 生成的模板改了三处，每处都有原因：

1. **URL 从配置取，不写在 `alembic.ini` 里**。`ini` 会进版本库，而 `database_url` 分环境
   （`configs/development.yaml` / `testing.yaml`），写死等于让 CI 和生产指向同一个库。
   优先级：环境变量 `ALEMBIC_DATABASE_URL` > `get_settings().database_url`。
   前者是给"临时对着另一个库跑迁移"留的口子，比如验收时指向一个新建的空文件。
2. **迁移走同步引擎**。`env.py` 的整个上下文是同步的，async 引擎要到处 `run_sync`；
   `app.utils.db.build_sync_engine` 负责把 `sqlite+aiosqlite` 降级成 `sqlite`，
   顺带把 WAL / busy_timeout / foreign_keys 三条 PRAGMA 一起发出去。
3. **`target_metadata = Base.metadata`，且 import `app.models` 而不是单个模型模块**。
   autogenerate 只看得见已登记的表，漏 import 会生成一份"没有表"的空迁移，
   而它一样能 upgrade 成功。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

# `python -m alembic` 时 CWD 已在 sys.path，但从别的目录调用就不一定了；
# 这段必须在 app.* 的 import 之前，否则 `alembic -c ... upgrade` 从别的目录跑就直接 ImportError。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alembic import context  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from app.models import Base  # noqa: E402  # 触发四个模型的注册，见 docstring 第 3 条
from app.utils.db import build_sync_engine  # noqa: E402

config = context.config

if config.config_file_name is not None:
    # 迁移脚本的日志沿用 alembic.ini 的配置，但不让 sqlalchemy 的 echo 盖过应用侧的日志级别。
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    override = os.getenv("ALEMBIC_DATABASE_URL")
    if override:
        return override
    # 延迟 import：`get_settings()` 会读 `.env`，脚本环境缺变量时不该连迁移都起不来。
    from app.config import get_settings

    return get_settings().database_url


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`：只把 SQL 打到标准输出，不碰数据库。

    验收和排障时很有用——SQLite 的 `ALTER TABLE` 能力有限，
    想看清一次迁移到底发了哪些语句，就用这个模式。
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=_database_url().startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine: Engine = build_sync_engine(_database_url())
    with engine.begin() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite 不支持 ALTER COLUMN，batch 模式把改列变成"建新表-搬数据-改名"。
            # 不开这个开关，将来任何一次改列都会生成不可执行的迁移。
            render_as_batch=engine.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
