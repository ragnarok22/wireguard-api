UV ?= uv

.PHONY: install format format-check lint typecheck run test coverage check

install:
	$(UV) sync --locked

format:
	$(UV) run --locked ruff format .
	$(UV) run --locked ruff check . --fix

format-check:
	$(UV) run --locked ruff format --check .

lint:
	$(UV) run --locked ruff check .

typecheck:
	$(UV) run --locked mypy

run:
	$(UV) run --locked uvicorn api:app --host 127.0.0.1 --port 8008 --reload

test:
	$(UV) run --locked pytest

coverage:
	$(UV) run --locked pytest --cov --cov-report=term-missing --cov-report=xml

check: lint format-check typecheck test
