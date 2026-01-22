# wireguard-api

VPN node based in Wireguard with a RESTful API exposed to manage peers.

[![Release](https://github.com/ragnarok22/wireguard-api/actions/workflows/release.yml/badge.svg)](https://github.com/ragnarok22/wireguard-api/actions/workflows/release.yml)
[![Publish Docker image](https://github.com/ragnarok22/wireguard-api/actions/workflows/publish-docker.yml/badge.svg)](https://github.com/ragnarok22/wireguard-api/actions/workflows/publish-docker.yml)
[![GitHub Package](https://github.com/ragnarok22/wireguard-api/actions/workflows/publish-github.yml/badge.svg)](https://github.com/ragnarok22/wireguard-api/actions/workflows/publish-github.yml)
<!-- ALL-CONTRIBUTORS-BADGE:START - Do not remove or modify this section -->
[![All Contributors](https://img.shields.io/badge/all_contributors-2-orange.svg?style=flat-square)](#contributors)
<!-- ALL-CONTRIBUTORS-BADGE:END -->

## Installation

### Run with Docker Compose (Recommended)

This project uses a modern `compose.yaml` configuration.

1. Create a `compose.yaml` (or clone the repo):
   ```yaml
   # See compose.yaml in the repo
   ```

2. Run the stack:
   ```bash
   API_TOKEN=your_token docker compose up --build
   ```

### Run with Docker

```bash
docker run -d \
    --name=wireguard_api \
    --cap-add=NET_ADMIN \
    --cap-add=SYS_MODULE \
    -e API_TOKEN=your_secret_token \
    -e SERVERURL=vpn.yourdomain.com \
    -p 51820:51820/udp \
    -p 8008:8008 \
    -v /lib/modules:/lib/modules \
    --sysctl="net.ipv4.conf.all.src_valid_mark=1" \
    --restart unless-stopped \
    ghcr.io/lugodev/wireguard-api:main
```

Environment Variables:
* `API_TOKEN`: Secret token for authentication (Header `X-API-Token`).
* `SERVERURL`: Your VPN hostname (used for client config generation).
* `SERVER_PUBLIC_KEY`: Public key of the server interface (used for client config generation).
* `SERVER_ENDPOINT`: (Optional) Full endpoint `host:port` for client config. Defaults to `vpn.example.com:51820`. overrides `SERVERURL`.
* `WG_INTERFACE`: (Optional) WireGuard interface to manage. Defaults to `wg0`.
* `API_PORT`: (Optional) Port where the API listens inside the container. Defaults to `8008`.
* `VPN_PORT`: (Optional) Port where WireGuard listens. Defaults to `51820`.

## Usage

The API is RESTful and served on port `8008`.
Authentication is done via the `X-API-Token` header.

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
*If `public_key` is omitted, one will be generated and returned (along with the private key).*

### Create Peer (One-Liner Config)
To generate a ready-to-use WireGuard configuration file directly:
```bash
curl -X POST "http://localhost:8008/peers?format=config" \
  -H "X-API-Token: your_secret_token" \
  -H "Content-Type: application/json" \
  -d '{"allowed_ips": ["10.13.13.2/32"]}' > client.conf
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

## Development

This project uses [uv](https://github.com/astral-sh/uv) for dependency management and Python 3.13.

### Prerequisites
- Python 3.13+
- `uv` installed
- `make`

### Commands
```bash
make install       # Sync dependencies
make run           # Run dev server (uvicorn)
make lint          # Run ruff check
make format        # Run ruff format
```

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
