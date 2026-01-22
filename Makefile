.PHONY: install format lint run test

install:
	uv sync

format:
	uv run ruff format .
	uv run ruff check . --fix

lint:
	uv run ruff check .

run:
	uv run uvicorn api:app --reload

test:
	uv run pytest
