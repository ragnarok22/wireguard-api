# Repository Guidelines

## Project Structure & Modules
- `api.py`: FastAPI app exposing a single POST endpoint that executes shell commands after token validation; reads env vars via `.env`.
- `pyproject.toml` and `uv.lock`: Python 3.13 runtime with FastAPI/uvicorn, managed with `uv`.
- `Makefile`: common tasks (`install`, `format`, `lint`, `run`); `Dockerfile` multi-stage build (uv builder → `linuxserver/wireguard` runtime); `compose.yaml` for local container orchestration.
- No tests yet—add new suites under `tests/` mirroring module names.
- External configs: set `API_TOKEN`, `API_PORT`/`VPN_PORT`; ports 51820/udp and 8008/tcp must be reachable when containerized.

## Build, Test, and Development Commands
- `make install`: sync dependencies with uv (uses `pyproject.toml`/`uv.lock`).
- `make run`: start dev server via uvicorn on port 8008 with reload.
- `make lint`: Ruff static checks per repo config.
- `make format`: Ruff formatter plus auto-fix.
- Container flow: `API_TOKEN=changeme API_PORT=8008 VPN_PORT=51820 docker compose up --build`.
- When tests are added, prefer `uv run pytest`; keep fixtures lightweight to avoid privileged operations.

## Coding Style & Naming Conventions
- Python: Ruff enforces line length 88 and rulesets E,F,I,UP,B; target version `py313`.
- Naming: classes PascalCase, functions/vars snake_case; Pydantic models for request bodies.
- Type hints required for public functions; keep FastAPI response models explicit.
- Run `make format` before commits to ensure consistent import ordering and formatting.

## Testing Guidelines
- Add `tests/test_<area>.py` with function names `test_<behavior>`; focus on auth (403 on bad token) and command execution outcomes.
- Prefer dependency-free tests; mock subprocess where possible to avoid privileged calls.
- For integration checks, run `docker compose up -d` with a throwaway `API_TOKEN` and hit `http://localhost:8008/` using `curl --data 'token=...&command=echo ok'`.

## Commit & Pull Request Guidelines
- Follow Conventional Commits used in history (e.g., `build(docker): ...`, `feat: ...`, `chore(makefile): ...`).
- Include what changed, why, and validation (`make lint`, `make run` smoke checks) in PR descriptions.
- Link related issues; call out env var or port changes and any required host capabilities (NET_ADMIN, SYS_MODULE).
- For API changes, include sample request/response payloads; avoid committing secrets or `.env` files.

## Security & Configuration Tips
- `API_TOKEN` gates shell execution; use strong, unique values and avoid logging or sharing tokens.
- Container requires elevated caps; run only on trusted hosts, and restrict firewall to required ports.
- Commands run with `shell=True`—sanitize inputs and avoid passing user-controlled strings without validation.
