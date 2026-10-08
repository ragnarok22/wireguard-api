# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [1.0.0-rc.1] - 2026-10-08

Evaluation candidate for the first stable 1.0.0 release. The application contract
and storage format are unchanged from verified 0.9.0. This candidate is published
under its exact version only; `latest` and stable major/minor aliases stay on
their previously verified releases. Final 1.0.0 requires a passing representative
48-hour soak and an explicit go/no-go review in issue #20.

### Supported scope
- One exclusively owned IPv4 WireGuard interface, one `/16`–`/30` pool, one `/32` per peer and full-tunnel `0.0.0.0/0` client routing. IPv6, preshared keys and shared interface ownership are unsupported.
- Authenticated `/v1` UUID peer/operation resources, durable idempotency and verified reconciliation. Generated credentials are returned once; matching retries return identity without credentials. Public `/livez`, `/readyz` and `/metrics` retain the documented schemas and availability semantics.
- Explicit required token/endpoint, single worker, persistent server identity and SQLite desired state. Native amd64/arm64 artifacts are verified by published digest in both registries before GitHub prerelease creation.
- The documented stable 1.x compatibility/deprecation policy becomes a release guarantee with final 1.0.0; this is still a prerelease evaluation build.

### Upgrade and recovery
- From 0.9.0, stop the service and back up the entire data directory plus deployment settings; keep the same token, endpoint, server key, interface and pool. No API or storage migration is introduced by this candidate.
- From v0.4.2, follow the breaking `/v1` client migration and all-or-nothing legacy inventory import. Preserve the original server key and full pre-upgrade checkpoint. Old unversioned routes and `/health` are unsupported.
- Rollback/restoration uses the checkpoint and matching previous image, never stale legacy JSON or an overlaid database directory. See the [tested upgrade/backup/rollback procedures](https://github.com/ragnarok22/wireguard-api/blob/v1.0.0-rc.1/README.md#upgrading-an-existing-deployment).

### Validation
- Contract/compatibility, recovery and secure operations acceptance reviews are complete (#9, #18 and #16), including a fresh default Compose deployment.
- The candidate release workflow runs strict quality checks, 100% statement/branch coverage, native bootstrap/tunnel tests and upgrade/backup/restoration/rollback verification on both architectures and registries.
- The 48-hour operational soak is tracked separately in [#20](https://github.com/ragnarok22/wireguard-api/issues/20); publication of this candidate does not itself declare the soak complete or authorize a final go decision.

## [0.9.0] - 2026-10-08

Promotes the verified 0.9.0-rc.1 application to a non-prerelease 0.9.0 release.
This remains pre-1.0 software: the documented stable 1.x compatibility guarantee
starts with 1.0.0. Review the breaking upgrade from 0.4.2 before deploying.

### Breaking changes since 0.4.2
- **Management API**: Use authenticated `/v1` routes and UUID peer/operation IDs. Creation requires `key_mode` and `Idempotency-Key`; lists return `items` and `next_cursor`. Generated credentials are returned once; templates contain a private-key placeholder. Old unversioned/public-key routes are removed.
- **Node ownership and routing**: One exclusive IPv4 WireGuard interface and `/16`–`/30` pool, with one `/32` per peer. Unmanaged peers are removed. Clients use `0.0.0.0/0`; IPv6 and preshared keys are unsupported. Inherited wg-quick/CoreDNS/module-loading owners are disabled; `/config/wg_confs` is not imported.
- **Configuration and probes**: Explicit non-default `API_TOKEN` and real `SERVER_ENDPOINT` are required. Use public `/livez` and `/readyz` instead of `/health`. The handshake metric is now `wireguard_peer_last_handshake_timestamp_seconds`; unavailable data is distinguished from an empty inventory.
- **Storage and runtime**: SQLite becomes the desired-state source, with strict all-or-nothing initial legacy JSON migration and preserved source files. Python 3.14+ is required.

### Upgrade and recovery
- Stop the old service and back up its **entire data directory**. Preserve `server_private.key`, match its interface and server address/pool, provision the required token/endpoint and update management clients to `/v1`.
- Save one-time generated client credentials securely. After a lost response, replay only recovers identity: revoke that allocation and create a replacement with a new idempotency key.
- The tested legacy upgrade is **v0.4.2 → v0.9.0**. Rollback requires the **pre-upgrade full-directory checkpoint and previous image**, never the migrated directory or stale JSON. Follow the [upgrade, backup and rollback procedures](https://github.com/ragnarok22/wireguard-api/blob/v0.9.0/README.md#upgrading-an-existing-deployment).

### Added
- Durable pending operations, idempotency history, verified reconciliation, cancellation and address reservation through revocation; preserved server identity with restrictive key permissions.
- Authenticated `/v1/stats` and `/v1/system`, comparable VPN traffic rates, cgroup-aware CPU/RAM, sample freshness and explicit unavailable observations.
- Native amd64/arm64 images in Docker Hub and GHCR; published-digest, version/OCI-label, bootstrap, real tunnel, persistence and upgrade/restore/rollback verification before alias promotion and release creation.
- Secure deployment, token rotation, compatibility policy, operational troubleshooting and backup/restore/rollback documentation.

### Fixed
- Configuration validation no longer prints supplied tokens; command failures remain secret-free and block incomplete bootstrap.
- Default/custom interface setup, key-permission repair and repeatable forwarding/NAT preserve identity across restart/recreation.
- Kernel mutations report success only after verification. Replays never disclose generated credentials; conflicting/revoked requests return `409`. Stale/failed observations cannot masquerade as healthy empty inventories.

## [0.9.0-rc.1] - 2026-10-08

This is an evaluation release candidate for 0.9.0, with breaking changes from
0.4.2. Pin `0.9.0-rc.1` or its published digest; prereleases do not update moving
image aliases. The stable 1.x compatibility policy starts with 1.0.0.

### Upgrade and recovery
- Stop the old service and back up its **entire data directory** before upgrading. Preserve `server_private.key`, match the existing interface and server address/pool, and provide a strong `API_TOKEN` plus the real reachable `SERVER_ENDPOINT`.
- Update management clients to authenticated `/v1` routes, UUID IDs, `key_mode` and `Idempotency-Key`. Generated client credentials are returned once and cannot be recovered by retrying; save them securely.
- The tested legacy upgrade path is v0.4.2. Roll back with the **pre-upgrade full-directory checkpoint and old image**, not the migrated directory or stale legacy JSON. See the [upgrade, backup and rollback procedures](https://github.com/ragnarok22/wireguard-api/blob/v0.9.0-rc.1/README.md#upgrading-an-existing-deployment).

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
- **Compatibility policy**: Define stable 1.x patch/minor guarantees, major-only incompatible changes, deprecation announcements and migration requirements, and the distinction between product, API and storage versions.
- **Operational troubleshooting**: Add diagnostic and recovery procedures for WireGuard connectivity, incomplete bootstrap, pending reconciliation, storage failures/corruption and IP exhaustion, linked to tested offline backup, restoration and rollback procedures. Clarify the single-owner/worker model and effective token source during rotation.
- **Node statistics**: Authenticated `/v1/stats` summarizes peers, handshakes, traffic counters/rates, pool capacity, and pending operations; `/v1/system` reports cgroup-aware CPU/RAM, data-filesystem usage, and runtime information. Independent background sampling exposes sample freshness and unavailable data explicitly.
- **Durable storage and operations**: SQLite desired state, serialized allocation/kernel mutation, and persisted idempotency/operation history. Return `202` for accepted pending work, reconcile after interruptions, and retain operation history after revocation. Cancel a pending creation superseded by deletion rather than reporting successful creation.
- **Legacy migration**: Automatically import an entire valid `peers.json` transactionally on initial storage setup, preserving the original. Require canonical public keys and unique usable IPv4 `/32` allocations inside the node pool; invalid legacy data blocks startup. SQLite becomes the sole source of truth after migration.
- **Identity safeguards**: Preserve existing server keys with restrictive permissions and pin interface/address identity in `bootstrap.json` and storage metadata. Intentional interface/pool changes require explicit migration.
- **Safe contracts**: Stable `code`/`detail` errors without submitted secrets or subprocess output, `Cache-Control: no-store` on creation, server metadata through authenticated `/v1/server`, and pending-operation/availability metrics.
- **Quality checks**: Strict mypy checks, centralized pytest configuration, and branch coverage reporting with a 100% coverage floor.
- **Unit coverage**: Expand coverage across application composition/routes, service, storage/locking/migration, bootstrap/adapter, settings/contracts, monitoring, and release metadata. Isolate tests from local `.env` files and `/config`, using fake backends and temporary storage.
- **Container verification**: Deployment checks and smoke tests for authenticated `/v1` operations, monitoring, generated configurations/templates, a real WireGuard handshake, tunneled traffic through NAT, container recreation, deletion, and preserved server identity.
- **Startup and recovery regressions**: Verify default and custom WireGuard interfaces, invalid authentication rejection, token rotation, restrictive server-key permissions/ownership, temporary-file cleanup, repeatable firewall repair and essential-command failure recovery. Verify crash recovery, full-directory backup restoration and legacy rollback with real client traffic.
- **Published-artifact verification**: Independently pull and test Docker Hub and GHCR digests on native amd64/arm64; require both architectures in each index and matching product/OpenAPI/health/runtime versions plus OCI version/revision labels. Release notes include the versioned changelog before automatic notes.
- **Dependency maintenance**: Renovate configuration for Python, lockfiles, GitHub Actions, and Docker images.
- **Build context**: `.dockerignore` excludes local environment files, configuration, and development artifacts.

### Fixed
- **Secret-free startup diagnostics**: Hide submitted authentication values from configuration validation errors, including when another required setting is absent.
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
