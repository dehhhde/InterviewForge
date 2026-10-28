# 命令速查表 —— **本机（Windows + Git Bash）跑不了它**：没有 `make`，也没有 `uv`
# （AGENTS.md §2 实测）。它的全部价值是把"该跑哪一条"写在一处，每行都已经钉好
# `.venv` 解释器、`PYTHONPATH=.` 与 `-X utf8`，可以**直接复制粘贴**到 Git Bash 里跑。
#
# 为什么每行都要带这三样：
# - 裸 `python` 在这台机器上以退出码 49 失败；裸 `ruff`/`pytest` 用的不是仓库的 venv。
# - `PYTHONPATH=.`：`app.scripts` 与 `alembic` 的 `env.py` 都按仓库根导入。
# - `-X utf8`：控制台是 GBK，不钉住编码，中文输出直接抛 `UnicodeDecodeError`。
#
# 起服务请用 `start.sh` / `start.bat`（它们比这里的 `dev` 更完整：同样钉了这三样，
# 还负责把 `.env` 读进来）。真正跑迁移的唯一入口是下面 `migrate` 那一行。

PY = .venv/Scripts/python.exe

dev:
	PYTHONPATH=. $(PY) -X utf8 -m uvicorn app.main:app --reload --port 8000

lint:
	$(PY) -m ruff check app tests migrations
	$(PY) -m ruff format --check app tests

fmt:
	$(PY) -m ruff check --fix app tests migrations
	$(PY) -m ruff format app tests migrations

type:
	$(PY) -m mypy app

test:
	$(PY) -m pytest -q

cov:
	$(PY) -m pytest --cov=app --cov-report=term-missing -q

migrate:
	PYTHONPATH=. $(PY) -X utf8 -m alembic upgrade head

check-migrations:
	PYTHONPATH=. $(PY) -X utf8 -m alembic check

seed:
	PYTHONPATH=. $(PY) -X utf8 -m app.scripts.seed

reap-tasks:
	PYTHONPATH=. $(PY) -X utf8 -m app.scripts.reap_tasks

new-migration:
	PYTHONPATH=. $(PY) -X utf8 -m alembic revision --autogenerate -m

.PHONY: dev lint fmt type test cov migrate check-migrations seed reap-tasks new-migration
