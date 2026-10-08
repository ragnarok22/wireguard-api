# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Changed
- **Breaking API refactor**: Replace unversioned peer routes and public-key URLs with authenticated `/v1` routes and UUID peer/operation IDs. Creation requires `key_mode` and `Idempotency-Key`; lists return `items` and `next_cursor`. Replace `?format=config` and partial configuration responses with one-time generated JSON credentials and a full private-key-placeholder template.
- **Breaking node ownership**: Manage one exclusive IPv4 WireGuard interface and a `/16`–`/30` client pool, with one client `/32` per peer. Reconciliation removes unmanaged peers. Client configurations route only `0.0.0.0/0`; IPv6 and preshared keys are unsupported.
- **Breaking deployment**: Disable inherited linuxserver wg-quick configuration/activation, module-loading, and CoreDNS services. Application bootstrap owns the interface, server identity, IPv4 forwarding, and subnet-scoped NAT. Existing `/config/wg_confs` configurations are not imported and require explicit migration.
- **Breaking configuration**: Require a non-default `API_TOKEN` and a real `SERVER_ENDPOINT`; reject the old fallback token and `vpn.example.com`. Replace the server-public-key override with the observed interface key and add validated interface, address/pool, listener, data directory, DNS, egress, timeout, reconciliation, and snapshot-cache settings.
- **Breaking monitoring**: Replace `/health` with public `/livez` and `/readyz`. Readiness requires healthy storage and exact kernel convergence; runtime backend failures leave liveness available. Rename the handshake gauge to `wireguard_peer_last_handshake_timestamp_seconds` and distinguish unavailable observations from an empty inventory.
- **Python compatibility**: Require Python 3.14+ and pin development to Python 3.14.8.
- **Dependencies**: Refresh runtime and development dependencies, replace the deprecated HTTPX test-client dependency with httpx2, and regenerate the uv lockfile.
- **Docker**: Pin uv and WireGuard images, remove the redundant Debian builder, install dependencies once with Alpine's Python, and copy only application files.
- **Compose**: Bind the management API to loopback by default, map configurable host ports to the WireGuard listener and fixed `8008/tcp` API, require runtime credentials, persist the configured data directory, and probe readiness.
- **CI/CD**: Update and SHA-pin Actions, share quality checks between PR and tag workflows, and publish amd64/arm64 images to Docker Hub and GHCR. Verify the exact published digest natively before promoting eligible aliases and creating a GitHub release.
- **Release identity**: Read the application version from `pyproject.toml`. Require exact SemVer release tags, including prerelease/build metadata, to match project and API versions. Prereleases receive full tags only; stable aliases follow the highest known stable Git tag in each scope, including unpublished tags, so older releases cannot move aliases backward. Encode build metadata's `+` as `_` in Docker tags.
- **Documentation**: Document deployment migration, `/v1` examples, one-time credential handling, module responsibilities, and development/release checks while retaining the README introduction and contributor blocks.

### Added
- **Node statistics**: Authenticated `/v1/stats` summarizes peers, handshakes, traffic counters/rates, pool capacity, and pending operations; `/v1/system` reports cgroup-aware CPU/RAM, data-filesystem usage, and runtime information. Independent background sampling exposes sample freshness and unavailable data explicitly.
- **Durable storage and operations**: SQLite desired state, serialized allocation/kernel mutation, and persisted idempotency/operation history. Return `202` for accepted pending work, reconcile after interruptions, and retain operation history after revocation. Cancel a pending creation superseded by deletion rather than reporting successful creation.
- **Legacy migration**: Automatically import an entire valid `peers.json` transactionally on initial storage setup, preserving the original. Require canonical public keys and unique usable IPv4 `/32` allocations inside the node pool; invalid legacy data blocks startup. SQLite becomes the sole source of truth after migration.
- **Identity safeguards**: Preserve existing server keys with restrictive permissions and pin interface/address identity in `bootstrap.json` and storage metadata. Intentional interface/pool changes require explicit migration.
- **Safe contracts**: Stable `code`/`detail` errors without submitted secrets or subprocess output, `Cache-Control: no-store` on creation, server metadata through authenticated `/v1/server`, and pending-operation/availability metrics.
- **Quality checks**: Strict mypy checks, centralized pytest configuration, and branch coverage reporting with a 100% coverage floor.
- **Unit coverage**: Expand coverage across application composition/routes, service, storage/locking/migration, bootstrap/adapter, settings/contracts, monitoring, and release metadata. Isolate tests from local `.env` files and `/config`, using fake backends and temporary storage.
- **Container verification**: Deployment checks and smoke tests for authenticated `/v1` operations, monitoring, generated configurations/templates, a real WireGuard handshake, tunneled traffic through NAT, container recreation, deletion, and preserved server identity.
- **Dependency maintenance**: Renovate configuration for Python, lockfiles, GitHub Actions, and Docker images.
- **Build context**: `.dockerignore` excludes local environment files, configuration, and development artifacts.

### Fixed
- **Mutation reporting**: Verify kernel application before reporting success; retain durable pending intent and reserved addresses on backend failures. Idempotent replays return the original peer/operation without regenerating or replaying credentials; mismatched requests and revoked originals return `409`.
- **Credential recovery expectations**: Generated client private keys are never stored or recoverable through retries/templates. A lost creation response requires revoking the original allocation and creating a replacement with a new idempotency key.
- **Observability**: Avoid stale successful snapshots and false empty inventories on backend failure; remove stale per-peer series, bound request labels with route templates, and measure complete streaming responses.

## [0.4.2] - 2026-01-25

### Added
- **Health endpoint**: New `/health` endpoint for liveness/readiness probes. Returns 200 when WireGuard is available, 503 otherwise. No authentication required.
- **Prometheus metrics**: New `/metrics` endpoint exposing request metrics (`wireguard_api_requests_total`, `wireguard_api_request_duration_seconds`) and WireGuard stats (`wireguard_peers_total`, `wireguard_peer_transfer_rx_bytes`, `wireguard_peer_transfer_tx_bytes`, `wireguard_peer_last_handshake_seconds`).
- **Dependencies**: Added `prometheus-client>=0.21.0` for metrics collection.

## [0.4.1] - 2026-01-23

### Fixed
- **Connectivity**: Added missing `iptables` and `iproute2` dependencies to Dockerfile to fix NAT/Masquerade issues.
- **CI/CD**: Configuring Docker image tagging to strip 'v' prefix (e.g., `v0.4.1` -> `0.4.1`).

## [0.4.0] - 2026-01-23

### Added
- **Persistence**: Peers are now saved to `/config/peers.json` and automatically restored on container startup. This prevents data loss when the container is recreated.

### Fixed
- **Connectivity**: Enabled IP Forwarding and fixed NAT configuration to resolve "No Internet" issues for connected clients.
- **Linting**: Fixed various linting errors and standardized code style with `ruff`.
