# Changelog

All notable changes to this project will be documented in this file.

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
