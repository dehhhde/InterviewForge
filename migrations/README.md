# migrations/

`alembic init` 生成后改造过，别当模板看：

- **`env.py`**：URL 从 `ALEMBIC_DATABASE_URL` 或 `get_settings().database_url` 取
  （`alembic.ini` 里 `sqlalchemy.url` 留空）；迁移走**同步**引擎
  （`app.utils.db.build_sync_engine` 把 `+aiosqlite` 摘成 `pysqlite`）；
  `target_metadata = app.models.Base.metadata`；SQLite 开 `render_as_batch=True`。
- **`script.py.mako`**：保持生成原样。
- **`alembic.ini` 只能是 ASCII**：alembic 用 locale(GBK) 读它，中文注释会在任何迁移执行之前
  抛 `UnicodeDecodeError`（实测 2026-10-16）。要写的说明放 `env.py` 或 `docs/adr/`。

跑法（没有 `make`，这两条就是 `make migrate` / `make new-migration` 的等价命令）：

```bash
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m alembic upgrade head
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 -m alembic check   # 模型与迁移有没有漂移
```

`env.py` 已经自己把项目根塞进 `sys.path`，`PYTHONPATH=.` 只是给 `check` / `alembic` 之外的
命令保持一致口径。

设计取舍见 `docs/adr/0007-sqlite-persistence-layer.md`。
