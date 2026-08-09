.PHONY: install dev test eval lint typecheck compose-up compose-down migrate seed

install:
	cd backend && uv sync --extra s3 --extra dev

dev:
	cd backend && uv run uvicorn app.main:app --reload --port 8000

test:
	cd backend && uv run pytest -q

eval:
	cd backend && uv run python -m app.scripts.eval_gates --report eval/last-run.json

lint:
	cd backend && uv run ruff check app clients tests

typecheck:
	cd backend && uv run mypy app clients

compose-up:
	docker compose up -d --build

compose-down:
	docker compose down

migrate:
	cd backend && uv run alembic upgrade head

seed:
	cd backend && uv run python -m app.scripts.seed
