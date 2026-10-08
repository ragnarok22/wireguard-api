# Repository Guidelines

## Project Structure & Modules
- `api.py`: FastAPI peer-management endpoints, `X-API-Token` authentication, and public `/health` and `/metrics` endpoints; reads configuration through environment variables and `.env`.
- `wireguard.py`: WireGuard subprocess commands, IP allocation, and peer persistence at `/config/peers.json`.
- `health.py` and `metrics.py`: Health reporting and Prometheus request/peer metrics.
- `pyproject.toml` and `uv.lock`: Python 3.14+ dependencies managed with uv; `.python-version` pins the development interpreter.
- `Makefile`: Development and quality-check targets. `Dockerfile` uses pinned uv and linuxserver/wireguard images, installing dependencies with Alpine's Python.
- `tests/`: Unit suites for API, health, metrics, persistence, and WireGuard helpers. `scripts/smoke_container.py` exercises a built image with real WireGuard and disposable storage.
- `.github/workflows/checks.yml`: Reusable Python and native amd64/arm64 container checks. `tests.yml` runs these for PRs/main; `publish-docker.yml` gates publication and calls `release.yml` afterward.
- `renovate.json`: Weekly Python, lockfile, Actions, and Docker update configuration.

## Build, Test, and Development Commands
- Requires uv 0.12.23+ and make. uv downloads the pinned Python interpreter when needed.
- `make install`: Sync dependencies with `uv sync --locked`.
- `make run`: Start uvicorn at `http://127.0.0.1:8008` with reload.
- `make lint` / `make format-check`: Ruff lint and formatting validation.
- `make format`: Ruff formatting and automatic fixes.
- `make typecheck`: Strict mypy checks for application modules.
- `make test` / `make coverage`: Unit tests and branch coverage with XML output; coverage must meet the measured 81.9% baseline floor.
- `make check`: Lint, formatting, types, and tests.
- For an older installed uv binary, pass `UV="uv tool run --from uv==0.12.23 uv"` to make.
- Container flow: `API_TOKEN=changeme API_PORT=8008 VPN_PORT=51820 docker compose up --build`.
- Container verification: `docker build --check .`, `docker build -t wireguard-api:smoke .`, then `python3 scripts/smoke_container.py wireguard-api:smoke`.

## Coding Style & Naming Conventions
- Ruff enforces line length 88, rulesets E,F,I,UP,B, and target version `py314`.
- Classes use PascalCase; functions and variables use snake_case.
- Type hints are required for application functions; annotate asynchronous callbacks with awaitable signatures.
- Keep response models explicit and use Pydantic request models.
- Run `make format` before commits; fix diagnostics instead of suppressing them.

## Testing Guidelines
- Keep new suites under `tests/test_<area>.py`; pytest config provides the root import path, so do not modify `sys.path` in test modules.
- Mock subprocess and WireGuard interactions in unit tests; use temporary storage and avoid privileged operations.
- Cover authentication, peer operations, persistence, health, and metrics outcomes.
- Use the smoke script for container integration checks; it creates and cleans up its own container/volume and requires Docker with WireGuard kernel support.
- Run tests and coverage through make where possible. When a bug is reported, first reproduce it in a failing test, then delegate the fix and verify the test passes.

## Commit & Pull Request Guidelines
- Follow Conventional Commits used in history (e.g., `build(docker): ...`, `feat: ...`, `chore(makefile): ...`).
- Include what changed, why, and validation (`make check`, `make coverage`, container checks).
- Link related issues and describe Python, environment-variable, port, or host capability changes.
- For API changes, include sample request/response payloads. Avoid committing `.env` or other secrets.

## Security & Configuration Tips
- `API_TOKEN` gates peer management through `X-API-Token`; use strong values and avoid logging tokens or private keys.
- WireGuard subprocess calls use argument lists with `shell=False`; preserve this pattern.
- Containers need `NET_ADMIN`; `SYS_MODULE` and `/lib/modules` support hosts that need kernel modules loaded.
- Restrict API access to management clients. Public `/health` and `/metrics` remain unauthenticated.
- Supply runtime secrets through environment configuration; explicit Docker copies and `.dockerignore` keep local secrets and `/config` data outside the image.
- `API_PORT` and `VPN_PORT` select host ports; the container listens on `8008/tcp` and `51820/udp`. Reflect custom VPN ports in `SERVER_ENDPOINT`.
