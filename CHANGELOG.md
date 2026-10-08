# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Changed
- **Python compatibility**: Require Python 3.14+ and pin development to Python 3.14.8.
- **Dependencies**: Refresh runtime and development dependencies, replace the deprecated HTTPX test-client dependency with httpx2, and regenerate the uv lockfile.
- **Docker**: Pin uv and WireGuard images, remove the redundant Debian builder, install dependencies once with Alpine's Python, and copy only application files.
- **Compose**: Map configurable host ports to the fixed container ports (`51820/udp` and `8008/tcp`).
- **CI/CD**: Update and SHA-pin Actions, share quality checks between PR and tag workflows, and publish amd64/arm64 images to Docker Hub and GHCR from one workflow. Create the GitHub release only after successful image publication.
- **Documentation**: Update development commands and repository guidelines for the current peer-management API.

### Added
- **Quality checks**: Strict mypy checks, centralized pytest configuration, and branch coverage reporting with an 81.9% baseline floor.
- **Container verification**: Smoke tests for real WireGuard operations, authentication, monitoring, configuration generation, and persistence after restart.
- **Dependency maintenance**: Renovate configuration for Python, lockfiles, GitHub Actions, and Docker images.
- **Build context**: `.dockerignore` excludes local environment files, configuration, and development artifacts.

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
