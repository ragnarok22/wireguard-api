# Repository Guidelines

## Project Structure & Modules
- `api.py`: FastAPI application factory, lifespan/background reconciliation, safe error handlers, and public `/livez`, `/readyz`, and `/metrics` routes. `routes.py`: authenticated `/v1` peer, server, and operation routes.
- `service.py`: Peer allocation, idempotency, desired/observed views, durable mutation, and exclusive interface reconciliation.
- `storage.py`: Transactional SQLite desired state and operation history at `WG_DATA_DIR/peers.sqlite3`; `storage_lock.py`: interprocess serialization; `legacy.py`: strict all-or-nothing legacy JSON validation without modifying the original.
- `wireguard.py`: Typed, timeout-bounded subprocess adapter and observations; `bootstrap.py`: sole container owner of interface, server identity, IPv4 forwarding, and NAT. `service_run`: bootstrap followed by the single-worker application factory.
- `settings.py`: Explicit validated environment/`.env` loading; `models.py`: request/response contracts; `errors.py`: safe stable errors; `keys.py`: canonical key validation; `configuration.py`: full IPv4 client configuration/template rendering.
- `health.py` and `metrics.py`: Storage/convergence readiness and per-application Prometheus collectors with bounded HTTP labels and explicit unavailable observations.
- `stats.py`: VPN summaries and comparable per-peer traffic rates; `cgroups.py`: current-cgroup CPU/RAM and effective limits; `system_info.py`: resource/runtime composition; `telemetry.py`: independent background sampling with freshness-bounded caches.
- `version.py`: Application `VERSION` read from `pyproject.toml`; `scripts/release_metadata.py`: exact SemVer validation and monotonic image-alias policy.
- `pyproject.toml` and `uv.lock`: Python 3.14+ dependencies managed with uv; `.python-version` pins the development interpreter.
- `Makefile`: Development, quality, coverage, and deployment-check targets. `Dockerfile` uses pinned uv/linuxserver images and Alpine's Python, disabling inherited wg-quick, module-loading, and CoreDNS owners.
- `tests/`: Unit suites for application modules and release metadata. `scripts/smoke_container.py`: disposable server/client/NAT-target integration with a real handshake and recreation/persistence checks.
- `.github/workflows/checks.yml`: Reusable Python and native amd64/arm64 container checks; `tests.yml`: PR/main entry point; `publish-docker.yml`: metadata, publication, published-digest verification, and eligible alias promotion; `release.yml`: release after verification.
- `renovate.json`: Weekly Python, lockfile, Actions, and Docker update configuration.
- Keep `AGENTS.md` and `CLAUDE.md` synchronized. Preserve the user's README introduction and all contributor blocks when updating documentation.

## Build, Test, and Development Commands
- Requires uv 0.12.23+ and make. uv downloads the pinned Python interpreter when needed.
- `make install`: Sync dependencies with `uv sync --locked`.
- `make run`: Start `uvicorn api:create_app --factory` at `http://127.0.0.1:8008` with reload. This does not bootstrap networking: require validated settings, writable storage, and an existing correctly configured WireGuard interface owned exclusively by this node.
- `make lint` / `make format-check`: Ruff lint and formatting validation.
- `make format`: Ruff formatting and automatic fixes.
- `make typecheck`: Strict mypy checks for application modules.
- `make test` / `make coverage`: Unit tests and branch coverage with XML output; all application statements and branches must meet the 100% coverage floor. Keep mypy/coverage module lists aligned with refactors and added helpers, including release metadata.
- `make check`: Lint, formatting, types, and tests.
- `make deployment-check`: Validate `service_run` shell syntax, required Compose interpolation, and `docker build --check .`.
- For an older installed uv binary, pass `UV="uv tool run --from uv==0.12.23 uv"` to make.
- Container flow: Set a strong `API_TOKEN` and real `SERVER_ENDPOINT`, then `docker compose up -d --build`. Example endpoint `node.example.org:51820` must be replaced with the deployment's reachable hostname/IP; `vpn.example.com` is intentionally rejected.
- Container verification: `make deployment-check`, `docker build -t wireguard-api:smoke .`, then `python3 scripts/smoke_container.py wireguard-api:smoke`. The smoke runner needs Python 3.10+ and Docker with host WireGuard support.

## API & State Invariants
- Management routes are `/v1` only, authenticated with `X-API-Token` (`401` missing, `403` invalid). Use UUID peer/operation IDs. List responses have `items` and `next_cursor`; pass the cursor as `after`.
- Peer creation requires `Idempotency-Key` matching `[A-Za-z0-9_.:-]{1,128}` and `key_mode` (`generated` or `external`). External mode requires a canonical public key; generated mode forbids one. Optional `address` is a bare IPv4 address inside the node pool, not CIDR or an arbitrary route list.
- Persist intent before kernel mutation. Return `201` only for verified creation, `204` for verified deletion, and `202` for durable pending work. Operation status is `pending`, `complete`, or terminal `cancelled` for a pending create superseded by deletion; never describe cancellation as successful creation.
- Replays return `200` or `202` with the original peer/operation and no credentials. Mismatched idempotency requests or revoked/deleting originals return `409`. Keep idempotency/operation history after peer deletion and reserve addresses until revocation is verified.
- Generated private keys/client configs are returned once with `Cache-Control: no-store`, never persisted or recovered. Templates contain both client sections with `<YOUR_PRIVATE_KEY>`. After a lost generated response, recover identity by replay, revoke the original, then create a replacement with a new request key.
- SQLite is the sole source of desired state. Initial `peers.json` migration is all-or-nothing, requires canonical keys and unique usable single IPv4 `/32` allocations, preserves the source, and blocks startup on invalid data.
- The interface is exclusive and IPv4-only with a `/16`–`/30` pool. Remove unmanaged peers during reconciliation; do not share ownership with wg-quick or another manager. Client routing is only `0.0.0.0/0`; preshared keys are unsupported.
- Preserve existing `server_private.key`; pin interface/address in `bootstrap.json` and database metadata. Interface/pool changes require explicit migration, not deletion of identity files. `/config/wg_confs` is neither a runtime controller nor an import source.
- Readiness checks storage and fresh kernel convergence; runtime backend failures produce `503` readiness while liveness stays alive. Stable error bodies are `code`/`detail`, without submitted secrets or subprocess output.
- `/v1/stats` and `/v1/system` require authentication and use independent sampled caches. Rates require two comparable monotonic samples; membership/identity changes and counter resets invalidate them. CPU/RAM use current-cgroup counters, respect visible limits, and never substitute host usage. Missing fields are null; stale/failed samples are unavailable. Include the new collector modules in typing/coverage and the limited-container smoke check.

## Coding Style & Naming Conventions
- Ruff enforces line length 88, rulesets E,F,I,UP,B, and target version `py314`.
- Classes use PascalCase; functions and variables use snake_case.
- Type hints are required for application functions; annotate asynchronous callbacks with awaitable signatures.
- Keep response models explicit and use Pydantic request models.
- Run `make format` before commits; fix diagnostics instead of suppressing them.

## Testing Guidelines
- Keep new suites under `tests/test_<area>.py`; pytest config provides the root import path, so do not modify `sys.path` in test modules.
- Mock subprocess and WireGuard interactions in unit tests; use temporary storage and avoid privileged operations.
- Cover authentication/contracts, idempotency, interrupted operations/cancellation, concurrent allocation, migration/identity guards, bootstrap/adapter failure, readiness/metrics, and release-metadata outcomes.
- Use the smoke script for container integration: real handshake and tunneled HTTP/NAT traffic, recreation, deletion, and preserved server identity. It creates and cleans up its containers, network, and volume.
- Run tests and coverage through make where possible. When a bug is reported, first reproduce it in a failing test, then delegate the fix and verify the test passes.

## Release Guidelines
- `pyproject.toml` is the single version source. Release tags must be exact SemVer with a leading `v` and match `version.VERSION`/project version, including prerelease/build metadata.
- Full image tags omit `v` and encode build metadata's `+` as `_`. Prereleases get no moving aliases.
- Stable `MAJOR`, `MAJOR.MINOR`, and `latest` aliases follow the highest known stable Git version in each scope, even if newer tags are unpublished. Build metadata does not change precedence; older releases must not move aliases backward.
- Verify the exact published multi-platform digest natively on amd64 and arm64 before promoting eligible aliases or creating the GitHub release. Keep release runs serialized.
- Bump versions, commit, tag, push, and create releases only when explicitly requested.

## Commit & Pull Request Guidelines
- Follow Conventional Commits used in history (e.g., `build(docker): ...`, `feat: ...`, `chore(makefile): ...`).
- Include what changed, why, and validation (`make check`, `make coverage`, container checks).
- Link related issues and describe Python, environment-variable, port, or host capability changes.
- For API changes, include sample request/response payloads. Avoid committing `.env` or other secrets.

## Security & Configuration Tips
- `API_TOKEN` and `SERVER_ENDPOINT` are required. Use strong tokens, replace example endpoints with real reachable hosts/IPs, and never log tokens, client/server private keys, or credential-bearing responses.
- WireGuard/bootstrap subprocess calls use argument lists with `shell=False`, bounded timeouts, and sanitized failures; preserve this pattern.
- Containers need `NET_ADMIN`; `SYS_MODULE` and `/lib/modules` support hosts that need kernel modules loaded.
- Restrict API access to management clients; Compose binds to loopback by default. Public `/livez`, `/readyz`, and `/metrics` remain unauthenticated. Readiness uses its documented status/reason schema rather than the API error envelope.
- Supply runtime secrets through environment configuration; explicit Docker copies and `.dockerignore` keep local secrets and `/config` data outside the image.
- Keep persisted identity/database files and saved client credentials restrictive (`0600`); do not commit `.env`, data, private keys, or generated configs.
- `API_BIND`, `API_PORT`, and `VPN_PORT` are Compose-only host bindings; container API is `8008/tcp`, and UDP uses `WG_LISTEN_PORT` (default `51820`). Reflect the host UDP port in `SERVER_ENDPOINT`; a missing endpoint port defaults to `51820` independently of the listener setting.
