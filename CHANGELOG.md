# Changelog

All notable changes to this project will be documented in this file.

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
