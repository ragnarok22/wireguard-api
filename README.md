# wireguard-api

VPN node based in Wireguard with a RESTful API exposed to manage peers.

**What you get**
- FastAPI service that manages WireGuard peers over HTTP.
- Auto IP allocation when `allowed_ips` are omitted.
- Peers persisted to `/config/peers.json` and restored on startup.
- Public `/health` and `/metrics` endpoints for probes and Prometheus.
- Docker/Compose flow that bootstraps `wg0`, NAT, and IP forwarding for you.

[![Tests](https://github.com/ragnarok22/wireguard-api/actions/workflows/tests.yml/badge.svg)](https://github.com/ragnarok22/wireguard-api/actions/workflows/tests.yml)
[![Publish images and release](https://github.com/ragnarok22/wireguard-api/actions/workflows/publish-docker.yml/badge.svg)](https://github.com/ragnarok22/wireguard-api/actions/workflows/publish-docker.yml)
<!-- ALL-CONTRIBUTORS-BADGE:START - Do not remove or modify this section -->
[![All Contributors](https://img.shields.io/badge/all_contributors-2-orange.svg?style=flat-square)](#contributors)
<!-- ALL-CONTRIBUTORS-BADGE:END -->

## Deployment on AWS (Critical)
> [!IMPORTANT]
> Disable **Source/destination check** on the EC2 instance or routing will fail:
> 1. AWS Console → EC2 → Instances → select instance.
> 2. Actions → Networking → Change source/destination check.
> 3. Uncheck the box and save.

**Security Groups**
- UDP `51820`: inbound from `0.0.0.0/0` (WireGuard).
- TCP `8008`: inbound only from your management IP (API access).

### Run with Docker Compose (recommended)

```bash
git clone https://github.com/ragnarok22/wireguard-api.git
cd wireguard-api
API_TOKEN=your_token \
SERVER_ENDPOINT=vpn.example.com:51820 \
docker compose up --build
```

What this does
- Builds the app image with pinned `uv` and linuxserver/wireguard images, using Alpine's Python 3.14 runtime.
- Boots `wg0` at `10.13.13.1/24`, enables NAT + IP forwarding, and restores peers from `/config/peers.json`.
- Exposes UDP `51820` (WireGuard) and TCP `8008` (API).

### Run with Docker

```bash
docker run -d \
    --name=wireguard_api \
    --cap-add=NET_ADMIN \
    --cap-add=SYS_MODULE \
    -e API_TOKEN=your_secret_token \
    -e SERVER_ENDPOINT=vpn.yourdomain.com:51820 \
    -e SERVER_PUBLIC_KEY="server_public_key" \
    -e WG_INTERFACE=wg0 \
    -p 51820:51820/udp \
    -p 8008:8008 \
    -v /lib/modules:/lib/modules \
    -v $(pwd)/config:/config \
    --sysctl="net.ipv4.conf.all.src_valid_mark=1" \
    --sysctl="net.ipv4.ip_forward=1" \
    --restart unless-stopped \
    ghcr.io/ragnarok22/wireguard-api:latest
```

Release images are published to both `ghcr.io/ragnarok22/wireguard-api` and
`docker.io/ragnarok22/wireguard-api` for `linux/amd64` and `linux/arm64`. Stable
releases provide full version tags (such as `0.5.0`), major/minor tags (such as
`0.5`), and `latest`. Prereleases receive their full version tag only. Use a full
version tag to select a specific release.

## Configuration

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `API_TOKEN` | Yes | – | Shared secret for `X-API-Token` auth on peer endpoints. |
| `SERVER_ENDPOINT` | No | `vpn.example.com:51820` | Host:port shown in generated client configs. Missing port is auto-filled to `51820`. |
| `SERVER_PUBLIC_KEY` | No | Fetched from interface | Server pubkey used in configs; if unset we call `wg show <interface> public-key`. |
| `WG_INTERFACE` | No | `wg0` | WireGuard interface the API manages. |
| `VPN_PORT` | No | `51820` | Host-mapped WireGuard UDP port. The container listens on `51820`; set `SERVER_ENDPOINT` to the externally reachable port. |
| `API_PORT` | No | `8008` | Host-mapped API port. The app still listens on `8008` in-container. |

**Persistence**
- Peers are stored in `/config/peers.json`; mount `/config` to persist across restarts.
- `service_run` also keeps the server private key at `/config/server_private.key`.

## Usage

Base URL defaults to `http://localhost:8008` (inside container it always listens on `8008`).
Peer operations require `X-API-Token: <API_TOKEN>`.

### List Peers
```bash
curl -X GET http://localhost:8008/peers \
  -H "X-API-Token: your_secret_token"
```

### Create Peer
```bash
curl -X POST http://localhost:8008/peers \
  -H "X-API-Token: your_secret_token" \
  -H "Content-Type: application/json" \
  -d '{"allowed_ips": ["10.13.13.2/32"]}'
```
*If `public_key` is omitted, one will be generated.*
*If `allowed_ips` is omitted, the next available IP in the subnet will be automatically allocated.*

### Create Peer (One-Liner Config)
To generate a ready-to-use WireGuard configuration file directly:
```bash
curl -X POST "http://localhost:8008/peers?format=config" \
  -H "X-API-Token: your_secret_token" \
  -H "Content-Type: application/json" \
  -d '{}' > client.conf
```

### Get Peer Details
```bash
curl -X GET http://localhost:8008/peers/<PUBLIC_KEY> \
  -H "X-API-Token: your_secret_token"
```

### Get Peer Config
Returns a partial config block for the client.
```bash
curl -X GET http://localhost:8008/peers/<PUBLIC_KEY>/config \
  -H "X-API-Token: your_secret_token"
```

### Delete Peer
```bash
curl -X DELETE http://localhost:8008/peers/<PUBLIC_KEY> \
  -H "X-API-Token: your_secret_token"
```

### Health (no auth)
```bash
curl http://localhost:8008/health | jq
```
Sample response:
```json
{
  "status": "healthy",
  "version": "0.4.2",
  "uptime_seconds": 12.3,
  "wireguard_interface": "wg0",
  "wireguard_available": true,
  "peer_count": 0
}
```

### Prometheus Metrics (no auth)
```
curl http://localhost:8008/metrics
```
Exposes request metrics (`wireguard_api_requests_total`, `wireguard_api_request_duration_seconds`) and WireGuard stats (`wireguard_peers_total`, `wireguard_peer_transfer_rx_bytes`, `wireguard_peer_transfer_tx_bytes`, `wireguard_peer_last_handshake_seconds`). Scrape interval of 15–30s is typical.

## Development

This project uses [uv](https://github.com/astral-sh/uv) for dependency management
and requires Python 3.14+. Development and CI use the stable interpreter pinned
in `.python-version`; uv can download it automatically.

### Prerequisites
- `uv` 0.12.23 or newer
- `make`
- Docker with a WireGuard-capable Linux kernel for container smoke tests

For an existing uv installation, update it with `uv self update` when installed
using uv's standalone installer, or use your original package manager. To use
the project's uv version without changing your installed binary, pass
`UV="uv tool run --from uv==0.12.23 uv"` to make.

### Commands
```bash
make install       # Sync the committed lockfile
make run           # Run uvicorn at http://127.0.0.1:8008 with reload
make lint          # Check Python with Ruff
make format        # Format Python and apply Ruff fixes
make format-check  # Check formatting without editing files
make typecheck     # Run strict mypy checks on application modules
make test          # Run pytest
make coverage      # Run branch coverage; also writes coverage.xml
make check         # Lint, formatting, types, and tests
```

The 100-test unit suite covers **100% of statements and branches** in `api.py`,
`health.py`, `metrics.py`, and `wireguard.py`. The coverage floor is **100%**.
Tests use fake WireGuard instances and temporary storage, so the unit suite
does not need privileged networking.

### Container checks

```bash
docker build --check .
docker build -t wireguard-api:smoke .
python3 scripts/smoke_container.py wireguard-api:smoke
```

The smoke test creates an isolated container and Docker volume, checks real
WireGuard peer operations, public health/metrics, authentication, generated
configs, and persistence after restart, then removes its temporary resources.
The smoke runner itself needs only Python 3.10+ and Docker; application
dependencies stay inside the image.

### CI, publishing, and dependency updates

- Pull requests and pushes to `main` run Python checks and native amd64/arm64
  image builds with container smoke tests. Coverage XML is uploaded as an artifact.
- Tags matching `v*.*.*` run the same checks, build and publish both architectures
  to Docker Hub and GHCR, then create a GitHub release with generated notes.
  Docker Hub publishing uses the existing `DOCKER_USERNAME` and `DOCKER_TOKEN`
  repository secrets; GHCR and releases use `GITHUB_TOKEN`.
- GitHub Actions are pinned to commit SHAs, and Docker base images are pinned
  to digests. Runtime credentials are supplied when starting the container.
- Enable the [Renovate GitHub app](https://github.com/apps/renovate) for this
  repository to activate `renovate.json`. It schedules weekly dependency and
  lockfile maintenance, groups routine Python/Action/image updates, and keeps
  major upgrades in separate PRs. Python development updates stay on 3.14.

To refresh dependencies deliberately, update the ranges in `pyproject.toml`,
run `uv lock --upgrade`, then run `make check` and `make coverage` before
building and smoke-testing the image.

## Contributors ✨

Thanks goes to these wonderful people ([emoji key](https://allcontributors.org/docs/en/emoji-key)):

<!-- ALL-CONTRIBUTORS-LIST:START - Do not remove or modify this section -->
<!-- prettier-ignore-start -->
<!-- markdownlint-disable -->
<table>
  <tr>
    <td align="center"><a href="https://reinierhernandez.com"><img src="https://avatars.githubusercontent.com/u/8838803?v=4" width="100px;" alt=""/><br /><sub><b>Reinier Hernández</b></sub></a></td>
    <td align="center"><a href="http://lugodev.com"><img src="https://avatars.githubusercontent.com/u/18733370?v=4" width="100px;" alt=""/><br /><sub><b>Carlos Lugones</b></sub></a></td>
  </tr>
</table>
<!-- markdownlint-restore -->
<!-- prettier-ignore-end -->

<!-- ALL-CONTRIBUTORS-LIST:END -->

This project follows the [all-contributors](https://github.com/all-contributors/all-contributors) specification. Contributions of any kind welcome!
