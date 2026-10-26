dev:
	uvicorn app.main:app --reload --port 8000

lint:
	ruff check app tests && ruff format --check app tests

fmt:
	ruff check --fix app tests && ruff format app tests

type:
	mypy app

test:
	pytest -q

cov:
	pytest --cov=app --cov-report=term-missing -q

migrate:
	alembic upgrade head

new-migration:
	alembic revision --autogenerate -m

.PHONY: dev lint fmt type test cov migrate new-migration
