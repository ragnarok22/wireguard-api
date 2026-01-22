# wireguard-api

[![Release](https://github.com/ragnarok22/wireguard-api/actions/workflows/release.yml/badge.svg)](https://github.com/ragnarok22/wireguard-api/actions/workflows/release.yml)
[![Publish Docker image](https://github.com/ragnarok22/wireguard-api/actions/workflows/public-docker.yml/badge.svg)](https://github.com/ragnarok22/wireguard-api/actions/workflows/public-docker.yml)
[![GitHub Package](https://github.com/ragnarok22/wireguard-api/actions/workflows/github-publish.yml/badge.svg)](https://github.com/ragnarok22/wireguard-api/actions/workflows/github-publish.yml)
![GitHub contributors](https://img.shields.io/github/contributors/ragnarok22/wireguard-api)

<!-- ALL-CONTRIBUTORS-BADGE:START - Do not remove or modify this section -->
[![All Contributors](https://img.shields.io/badge/all_contributors-2-orange.svg)](#contributors)
<!-- ALL-CONTRIBUTORS-BADGE:END -->

## What is this?
FastAPI service that lets an authenticated client execute WireGuard management commands on a host. Designed to run alongside a WireGuard node (ports 51820/udp for VPN traffic and 8008/tcp for the API). Use on trusted hosts; commands run with `shell=True`.

## Installation
### Containers
- GitHub Container Registry: `docker pull ghcr.io/ragnarok22/wireguard-api:main`
- Docker Hub: `docker pull ragnarok22/wireguard-api`

Run with the required capabilities and environment variables:
```bash
docker run -d \
  --name=wireguard_api \
  --cap-add=NET_ADMIN --cap-add=SYS_MODULE \
  -e API_TOKEN=your_token \
  -p 51820:51820/udp -p 8008:8008 \
  -v /wireguard-api:/config -v /lib/modules:/lib/modules \
  --sysctl="net.ipv4.conf.all.src_valid_mark=1" \
  ghcr.io/ragnarok22/wireguard-api:main
```

Compose example (uses `compose.yaml`): `API_TOKEN=changeme API_PORT=8008 VPN_PORT=51820 docker compose up --build`.

### Local development
Requires Python 3.13 and [uv](https://github.com/astral-sh/uv):
```bash
make install       # sync dependencies
make run           # uvicorn api:app --reload on :8008
make lint | make format
```

## Usage
Send POST requests to `/` with `token` and `command`:
```bash
curl -X POST http://localhost:8008/ \
  -d 'token=your_token&command=wg show'
```
Invalid tokens return 403. Responses include command output under `status`. Avoid passing untrusted strings to `command`.

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
