# wireguard-api

**Your own WireGuard VPN, minus the config-file gymnastics.**
Run it in Docker. Use the REST API to add, view, or remove clients and generate
ready-to-import WireGuard configs.

[![Tests](https://github.com/ragnarok22/wireguard-api/actions/workflows/tests.yml/badge.svg)](https://github.com/ragnarok22/wireguard-api/actions/workflows/tests.yml)
[![Publish images and release](https://github.com/ragnarok22/wireguard-api/actions/workflows/publish-docker.yml/badge.svg)](https://github.com/ragnarok22/wireguard-api/actions/workflows/publish-docker.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
<!-- ALL-CONTRIBUTORS-BADGE:START - Do not remove or modify this section -->
[![All Contributors](https://img.shields.io/badge/all_contributors-2-orange.svg?style=flat-square)](#contributors)
<!-- ALL-CONTRIBUTORS-BADGE:END -->

## Contents

- [Features](#features)
- [Deployment](#deployment)
- [Configuration](#configuration)
- [Upgrading an existing deployment](#upgrading-an-existing-deployment)
- [Usage](#usage)
- [Health and monitoring](#health-and-monitoring)
- [Development](#development)
- [Contributors](#contributors)

## Features

- **Versioned peer management:** create, list, inspect, and revoke clients through
  the authenticated `/v1` API, using UUID peer IDs rather than public keys in URLs.
- **IPv4 client allocation:** one address per peer from the node's `/16`–`/30`
  pool, with generated keys or a client-supplied public key.
- **Client configuration:** generated credentials and a complete configuration
  are returned once; existing peers have a configuration template.
- **Durable operations:** SQLite records desired state before kernel changes;
  reconciliation retries pending work and restores peers after recreation.
- **Monitoring:** separate public liveness and readiness probes, plus Prometheus
  request, availability, pending-operation, traffic, and handshake metrics.

On a fresh deployment, the container creates `wg0` at `10.13.13.1/24`, listens
on UDP `51820`, and configures IPv4 forwarding and subnet-scoped NAT. The API
listens separately on TCP `8008`. This is an **exclusive, IPv4-only VPN node**:
the database owns the interface's entire peer inventory. Reconciliation removes
unmanaged peers and repairs managed peers; do not share the interface with
another peer manager. Client routes use only `0.0.0.0/0`, not IPv6. Preshared keys
are not supported or exposed.

## Deployment

### Requirements

- A Linux host with WireGuard kernel support, Docker, and Docker Compose.
- A real public hostname or IPv4 address reachable by VPN clients.
- Your VPN UDP port allowed through the host/cloud firewall.
- `NET_ADMIN` for the container. Hosts needing explicit kernel module loading
  can additionally grant `SYS_MODULE` and mount `/lib/modules`.

The included Compose file binds the management API to **`127.0.0.1` by default**.
Use an SSH tunnel or a restricted HTTPS reverse proxy for remote management.
Protect API traffic: a generated creation response contains a client private key.

### Docker Compose

```bash
git clone https://github.com/ragnarok22/wireguard-api.git
cd wireguard-api
```

Create a private `.env` file in the project directory:

```dotenv
API_TOKEN=replace-with-a-strong-unique-secret
SERVER_ENDPOINT=vpn.your-domain.tld:51820
API_BIND=127.0.0.1
API_PORT=8008
VPN_PORT=51820
```

**Replace `vpn.your-domain.tld` with your actual reachable hostname or IPv4
address.** `node.example.org`, used by deployment validation, is also a
placeholder to replace for deployment. `API_TOKEN` and `SERVER_ENDPOINT` are
required, with no fallback token or endpoint. The old token
`default_token_change_me` and the hostname `vpn.example.com` are intentionally
rejected. Keep `.env` private and outside Git.

```bash
docker compose up -d --build
docker compose logs app
curl --fail-with-body http://127.0.0.1:8008/readyz
```

Readiness returns HTTP `200` with `"status": "ready"` when storage and the
WireGuard peer inventory converge. Compose mounts `./config` at `WG_DATA_DIR`
and uses `/readyz` for its health check.

### Published images

Images support **`linux/amd64` and `linux/arm64`**:

- `ghcr.io/ragnarok22/wireguard-api`
- `docker.io/ragnarok22/wireguard-api`

With the `.env` file above and an existing `config` directory:

```bash
docker run -d \
  --name wireguard_api \
  --cap-add NET_ADMIN \
  --env-file .env \
  -p 51820:51820/udp \
  -p 127.0.0.1:8008:8008/tcp \
  -v "$(pwd)/config:/config" \
  --sysctl net.ipv4.conf.all.src_valid_mark=1 \
  --sysctl net.ipv4.ip_forward=1 \
  --restart unless-stopped \
  ghcr.io/ragnarok22/wireguard-api:latest
```

Pin a full version tag or digest for repeatable deployments. With `docker run`,
change the `-p` mappings and volume destination yourself: `API_BIND`, `API_PORT`,
and `VPN_PORT` are Compose-only settings. Match the exposed UDP port in
`SERVER_ENDPOINT` and the container UDP port in `WG_LISTEN_PORT`.

### AWS EC2 networking

Allow inbound UDP on the VPN port from VPN clients in the security group. Keep
management access restricted; the default loopback API binding is not directly
reachable through an EC2 security group rule.

If the instance also routes traffic for other networks without translating it
to its own address, disable **Source/destination check** under **EC2 → Instances
→ Actions → Networking → Change source/destination check**. That requirement
depends on the ENI routing topology; the default deployment here masquerades
VPN client traffic to the server's address.

## Configuration

`Settings.load()` reads environment variables and `.env`; existing environment
values take precedence. Compose passes the application settings below.

| Variable | Default | Description |
| --- | --- | --- |
| `API_TOKEN` | Required | Nonempty shared secret for `X-API-Token`; the old default token is rejected. |
| `SERVER_ENDPOINT` | Required | Reachable IPv4 address or hostname with optional UDP port. A missing port becomes `51820`, independently of `WG_LISTEN_PORT`. `vpn.example.com` is rejected; IPv6 endpoint literals are unsupported. |
| `WG_INTERFACE` | `wg0` | Exclusive WireGuard interface; container bootstrap creates or validates it. Linux interface name, at most 15 characters. |
| `WG_SERVER_ADDRESS` | `10.13.13.1/24` | Usable IPv4 server address and pool prefix, `/16`–`/30`. Network, broadcast, and server addresses are unavailable to clients. |
| `WG_LISTEN_PORT` | `51820` | Container WireGuard UDP listener, integer `1`–`65535`. |
| `WG_DATA_DIR` | `/config` | Persistent data directory; Compose mounts `./config` here. |
| `CLIENT_DNS` | `1.1.1.1` | Single IPv4 DNS address included in client configurations. |
| `WG_EGRESS_INTERFACE` | Unset | Optional existing egress interface; otherwise discover it from the route to `1.1.1.1`. Must differ from `WG_INTERFACE`. |
| `WG_COMMAND_TIMEOUT` | `5` | Subprocess timeout in seconds, greater than `0` and at most `60`. |
| `WG_RECONCILE_INTERVAL` | `5` | Background reconciliation interval in seconds, greater than `0` and at most `300`. |
| `WG_SNAPSHOT_TTL` | `1` | Observation cache lifetime in seconds, `0`–`30`; readiness forces a fresh observation. |
| `WG_TELEMETRY_INTERVAL` | `1` | Independent VPN/system sampling interval in seconds, `0.1`–`30`. Samples older than `max(3, 3 × interval)` seconds are unavailable. |
| `API_BIND` | `127.0.0.1` | Compose-only host address for the API port binding. |
| `API_PORT` | `8008` | Compose-only host TCP port mapped to container `8008`. |
| `VPN_PORT` | `51820` | Compose-only host UDP port mapped to `WG_LISTEN_PORT`. Use this host port in `SERVER_ENDPOINT`. |

`SERVER_PUBLIC_KEY` is no longer an override: configurations use the public key
observed on the interface. Changing the interface or server address/pool on an
existing volume requires an explicit migration, not just an environment change.

### Persistent data

Keep the entire data directory across container recreation:

| File | Contents |
| --- | --- |
| `peers.sqlite3` | Sole source of truth for UUID peers, reserved addresses, operation history, and idempotency records. |
| `peers.sqlite3.lock` | Interprocess lock serializing allocation, persistence, and kernel mutation. |
| `bootstrap.json` | Pins the interface name and server address/pool to this volume. |
| `server_private.key` | Server identity; an existing valid key is preserved rather than regenerated. |
| `peers.json` | Optional legacy import input; preserved unchanged after migration, never used as a live inventory afterward. |

The database and bootstrap identity files are created with restrictive `0600`
permissions; existing server-key permissions are repaired to `0600`. Client
private keys are never persisted. Back up the data directory with the service
stopped so the database and server identity stay consistent.

## Upgrading an existing deployment

This refactor is breaking: old unversioned `/peers`, public-key URLs,
`?format=config`, `/peers/{public_key}/config`, and `/health` are replaced by the
contracts below. Update management clients before switching deployments.

Although the image inherits `linuxserver/wireguard`, its native wg-quick
configuration generation/activation, module-loading, and CoreDNS services are
disabled. `bootstrap.py` is the sole container interface owner. Files in
`/config/wg_confs` **do not control this node and are not imported**. Existing
user-managed wg-quick configurations need an explicit migration of their peer
inventory, server identity, address/pool, and routing policy before startup.
There is no automatic import of arbitrary routes, IPv6, or preshared keys.

For a deployment using the old API's `peers.json`:

1. Stop the old service and back up its entire data directory.
2. Preserve `server_private.key` from the old bootstrap. If the server key only
   exists in a user configuration, explicitly migrate that identity to this
   file before starting; otherwise bootstrap creates a new server identity.
3. Set the required token and real endpoint. Match `WG_INTERFACE` and
   `WG_SERVER_ADDRESS` to the intended existing node identity and client pool.
4. Validate the entire legacy inventory. Each entry must have a canonical
   WireGuard public key and exactly `{"allowed_ips": ["10.13.13.2/32"]}` with
   one IPv4 `/32` inside the pool. Duplicate JSON keys, duplicate client
   addresses, server/network/broadcast addresses, extra fields, multiple
   addresses, and invalid keys are rejected.
5. Start the new service with the same persistent directory. On initial database
   initialization, migration imports all valid records transactionally, assigns
   UUID IDs, and records pending create operations for reconciliation.
6. Check `/readyz`, list `/v1/peers`, and update clients to use their UUID IDs.

Migration is **all-or-nothing**. Invalid legacy data clearly blocks application
startup; the original JSON remains untouched and no partial inventory is
accepted. Correct the migration input deliberately from your backup and retry.
Once migration is recorded, SQLite exclusively owns desired state: editing
`peers.json` does not update peers, and the application never overwrites it.

Both bootstrap and storage guard the interface/address identity. An intentional
interface or pool change needs a coordinated migration of persisted identity,
peer allocations, and client configurations. Do not casually delete
`bootstrap.json`, the database, or the server key to bypass a mismatch.

## Usage

Examples use `curl` and `jq`, running on the server or through a management
tunnel:

```bash
export API_URL="http://127.0.0.1:8008"
export API_TOKEN="replace-with-your-deployment-token"
```

Every `/v1` endpoint requires `X-API-Token`: missing headers return `401`, invalid
tokens return `403`. `/livez`, `/readyz`, and `/metrics` are public. Generated
OpenAPI documentation is available at `/docs` and `/redoc`, with the schema at
`/openapi.json`; use **Authorize** in `/docs` to supply the token.

### Endpoint reference

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/v1/server` | Server public key, endpoint, interface, pool, and capacity/reservation counts. |
| `GET` | `/v1/stats` | VPN peer counts, handshakes, current traffic counters/rates, pool capacity, and pending operations. |
| `GET` | `/v1/system` | Current-cgroup CPU/RAM, data-filesystem usage, and runtime information. |
| `GET` | `/v1/peers?limit=50&after=<UUID>` | Page through desired peers and kernel observations. |
| `POST` | `/v1/peers` | Create a generated-key or external-key peer with `Idempotency-Key`. |
| `GET` | `/v1/peers/{peer_id}` | Inspect a UUID peer, its state, and whether it is applied. |
| `GET` | `/v1/peers/{peer_id}/config-template` | Full client template with `<YOUR_PRIVATE_KEY>` placeholder. |
| `DELETE` | `/v1/peers/{peer_id}` | Revoke a UUID peer; `204` when complete or `202` while pending. |
| `GET` | `/v1/operations/{operation_id}` | Read durable operation progress by UUID. |
| `GET` | `/livez` | Process liveness. |
| `GET` | `/readyz` | Storage health and kernel convergence. |
| `GET` | `/metrics` | Prometheus exposition. |

### Generate and save a client configuration

`POST /v1/peers` requires a JSON body with `key_mode` and an `Idempotency-Key`
header of **1–128 characters from `[A-Za-z0-9_.:-]`**. Creation rejects query
parameters and unknown body fields. Use a distinct request key for each new
peer and preserve it for retries.

This command writes the complete response and extracted client configuration
to restrictive files. The output paths are `./client-create.json` and
`./client.conf`; use new filenames for each client:

```bash
umask 077
export REQUEST_KEY="client-$(uuidgen)"
curl --fail-with-body -X POST "$API_URL/v1/peers" \
  -H "X-API-Token: $API_TOKEN" \
  -H "Idempotency-Key: $REQUEST_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key_mode":"generated"}' -o ./client-create.json &&
jq -er '.client_config // error("No credentials in this response")' \
  ./client-create.json > ./client.conf
export PEER_ID="$(jq -r '.peer.id' ./client-create.json)"
export OPERATION_ID="$(jq -r '.operation.id' ./client-create.json)"
```

Only import `./client.conf` and activate the tunnel after the operation is
`complete`. A new generated response has `peer`, `operation`, `private_key`,
`client_config`, and `replayed: false`, with `Cache-Control: no-store`. The peer
includes `id`, `public_key`, bare IPv4 `address`, `state`, `created_at`, `applied`,
and nullable `observation`. Operation records include UUID `id` and `peer_id`,
`kind`, `status`, public key/address, nullable safe `error`, `created_at`, and
request key/fingerprint. Timestamps are Unix seconds.

Example `201` response excerpt (keys/configuration abbreviated; timestamps,
observations, and operation request metadata omitted):

```json
{
  "peer": {
    "id": "79a94a53-c94a-46c7-ab91-bc52196c1355",
    "public_key": "<client-public-key>",
    "address": "10.13.13.2",
    "state": "active",
    "applied": true
  },
  "operation": {
    "id": "e5df978a-5c11-4d17-b425-4aeb2c349cb8",
    "peer_id": "79a94a53-c94a-46c7-ab91-bc52196c1355",
    "kind": "create",
    "status": "complete",
    "error": null
  },
  "private_key": "<client-private-key>",
  "client_config": "<complete WireGuard client configuration>",
  "replayed": false
}
```

- **`201`**: creation is verified in WireGuard; the peer is active.
- **`202`**: intent is durable but application is pending. Save the initial
  credentials now, then poll the returned operation ID. `Retry-After: 5`
  advises when to check again; this is not a completed tunnel setup.
- The initial generated response returns credentials **once**, including when
  accepted as pending. They are not stored for subsequent retrieval.

The rendered configuration has this shape (keys are placeholders):

```ini
[Interface]
PrivateKey = <generated-client-private-key>
Address = 10.13.13.2/32
DNS = 1.1.1.1

[Peer]
PublicKey = <server-public-key>
Endpoint = vpn.your-domain.tld:51820
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
```

Replace the example endpoint with your real server address in deployment
settings. Keep both saved files private; both contain the client's private key.

### External keys and requested addresses

To keep private-key generation on the client, use `key_mode: "external"` and a
canonical base64 WireGuard public key encoding 32 bytes. Generated mode forbids
a supplied public key. External mode requires one and returns null
`private_key` and `client_config` fields.

```bash
export CLIENT_PUBLIC_KEY="replace-with-your-canonical-WireGuard-public-key"
curl --fail-with-body -X POST "$API_URL/v1/peers" \
  -H "X-API-Token: $API_TOKEN" \
  -H "Idempotency-Key: external-client-$(uuidgen)" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg key "$CLIENT_PUBLIC_KEY" \
    '{key_mode:"external", public_key:$key, address:"10.13.13.10"}')"
```

`address` is optional in either mode. Supply a **bare IPv4 address**, not CIDR
and not an `allowed_ips` list. It must be unused and inside this node's pool,
excluding the server, network, and broadcast addresses. Omit it for automatic
allocation. The server installs exactly that client address as `/32`.

### Retry semantics and lost responses

Retry creation with the **same key and same body**. A replay returns `200` if
complete or `202` if still pending, with the original peer/operation and
`replayed: true`; `private_key` and `client_config` are null. Reusing the key
with a different request returns `409`. Replaying after the original peer has
been revoked or entered deletion also returns `409`.

```bash
curl --fail-with-body -X POST "$API_URL/v1/peers" \
  -H "X-API-Token: $API_TOKEN" \
  -H "Idempotency-Key: $REQUEST_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key_mode":"generated"}'
```

This retries the generated example above; keep any original credential-bearing
files instead of replacing them with the replay response.

There is **no private-key recovery endpoint**. If a generated initial response
is lost, its idempotency key lets you recover the peer and operation identity,
but not its credentials. Revoke that peer, wait for completed revocation, then
create a replacement with a new idempotency key. If you already know the
operation ID, poll it directly. Generating another peer without revoking the
old one leaves an unwanted allocation behind.

### List, inspect, and build a template

```bash
curl --fail-with-body "$API_URL/v1/server" -H "X-API-Token: $API_TOKEN"
curl --fail-with-body "$API_URL/v1/peers?limit=50" \
  -H "X-API-Token: $API_TOKEN"
curl --fail-with-body "$API_URL/v1/peers/$PEER_ID" \
  -H "X-API-Token: $API_TOKEN"
```

The list is `{"items": [...], "next_cursor": "<UUID>"}`; `next_cursor` is null
on the last page. Pass it as `after` on the next request. `limit` defaults to
`50` and accepts `1`–`100`. Peer states are `pending`, `active`, and `deleting`.
`applied` reports whether an active peer's kernel address matches desired state;
`observation` contains live endpoint, traffic, handshake, and keepalive data.

For a non-null cursor returned by the previous page:

```bash
export CURSOR="replace-with-the-returned-next_cursor-UUID"
curl --fail-with-body "$API_URL/v1/peers?limit=50&after=$CURSOR" \
  -H "X-API-Token: $API_TOKEN"
```

```bash
umask 077
curl --fail-with-body "$API_URL/v1/peers/$PEER_ID/config-template" \
  -H "X-API-Token: $API_TOKEN" -o ./client-template.json &&
jq -er '.config' ./client-template.json > ./client-template.conf
```

This returns JSON with a `config` string containing both `[Interface]` and
`[Peer]`, including `PrivateKey = <YOUR_PRIVATE_KEY>`. Replace that placeholder
locally with your retained client private key before import. A template never
retrieves a generated private key and contains no preshared key.

### Revoke a peer and poll an operation

Use `PEER_ID` from creation or the list, not the peer's public key:

```bash
curl --fail-with-body -i -X DELETE "$API_URL/v1/peers/$PEER_ID" \
  -H "X-API-Token: $API_TOKEN"
```

Completed deletion returns `204` with no body. A pending deletion returns
`202`, an operation record, `Location: /v1/operations/<UUID>`, and
`Retry-After: 5`. The address stays reserved until kernel revocation is verified.
Set `OPERATION_ID` to the returned deletion operation's ID when polling it:

```bash
curl --fail-with-body "$API_URL/v1/operations/$OPERATION_ID" \
  -H "X-API-Token: $API_TOKEN"
```

Operations report `pending` while reconciliation retries and `complete` when
the kernel mutation is verified. `cancelled` is terminal for a pending create
superseded by deletion; it does not mean successful creation. Operation history
remains available after peer deletion.
Inspecting or deleting a missing peer returns `404`.

### Errors

API errors have a stable JSON shape:

```json
{"code":"conflict","detail":"Idempotency key was used for a different request"}
```

Codes include `http_error` (`401`/`403` and routing errors), `invalid_input`
(`422`), `not_found` (`404`), `conflict` (`409`), `wireguard_unavailable`,
`storage_unavailable`, or `unavailable` (`503`), and `internal_error` (`500`).
Details are safe: validation errors identify fields without echoing submitted
values, and backend failures do not expose subprocess output or credentials.
A failure before durable acceptance returns an error; a durable mutation that
needs retry returns `202` with its operation instead of claiming success.

## Health and monitoring

### VPN statistics and container resources

Both JSON endpoints require the management token and return `Cache-Control: no-store`:

```bash
curl --fail-with-body "$API_URL/v1/stats" -H "X-API-Token: $API_TOKEN"
curl --fail-with-body "$API_URL/v1/system" -H "X-API-Token: $API_TOKEN"
```

`GET /v1/stats` returns:

- `peers`: registered counts by `active`/`pending`/`deleting`, verified-applied,
  observed, and unmanaged counts. A deleting peer still reserves its address.
- `handshakes`: `recent`, `never`, `latest_at`, and `window_seconds`. The recent
  window defaults to 180 seconds; set `?handshake_window_seconds=60` to change it
  within `1`–`3600`. Recent handshakes indicate activity, not guaranteed connectivity.
- `traffic`: `rx_bytes`, `tx_bytes`, `rx_bytes_per_second`, and
  `tx_bytes_per_second`. RX is received by the server (client upload); TX is sent
  by the server (client download). Scope is `current_interface`, including any
  unmanaged peer awaiting reconciliation. Counters belong to currently installed
  peers, can reset, and are not persisted historical totals.
- `pool`: network, capacity, reserved addresses, and available addresses;
  `pending_operations`, application version, and process uptime.

Rates sum per-peer counter deltas over the measured monotonic interval. They are
`null` in the first sample or after peer membership/identity changes, server-key
changes, or detected counter resets. Two new comparable samples restore rates;
an idle, unchanged interface reports zero. Polling the endpoint does not trigger
sampling or alter the interval.

`GET /v1/system` returns `resource_scope: "current_cgroup"`, plus:

- `cpu`: cgroup v1/v2 source, effective `capacity_cores`, cumulative
  `total_usage_seconds`, measured `used_cores`, and `usage_percent` relative to
  that capacity. A Docker quota of 0.5 CPU means 0.5 fully consumed cores is 100%.
  The first sample has status `warming_up` and a null percentage. Short quota
  bursts can exceed 100% over a small sampling window.
- `memory`: `used_bytes` (cgroup usage, including charged cache), configured
  `limit_bytes`, effective `capacity_bytes`, and `usage_percent`. No configured
  limit is represented by `null`; physical memory capacity can bound the effective
  denominator without substituting host-wide usage.
- `disk`: `scope: "data_filesystem"`, total/used/free bytes, and usage percentage
  for the filesystem containing `WG_DATA_DIR`. This is filesystem capacity, not
  the directory's size or a container disk quota.
- `runtime`: OS, shared kernel, architecture, Python/API versions, and process uptime.

CPU capacity respects process affinity and visible ancestor quotas. Memory respects
visible ancestor limits. Limits hidden above a private cgroup namespace cannot be
inspected. In Docker these counters normally describe the container and its
processes; outside Docker they describe the application's current cgroup, not a
host-wide fallback. Missing/unsupported resource data is `null` with system status
`partial`; macOS development does not fabricate Linux container counters.

Both responses include `sample.sampled_at` (Unix seconds), `age_seconds`, and
`interval_seconds`. Sampling runs independently for VPN and system resources,
outside the event loop, every `WG_TELEMETRY_INTERVAL` seconds. A failed VPN/storage
observation invalidates the VPN sample and returns a safe `503`; stale samples and
whole-system collector failures use `telemetry_unavailable`. System data and
liveness stay available when the VPN collector fails. No sample is presented as
an empty success or silently reused past its freshness limit.

### Probes and Prometheus

```bash
curl --fail-with-body "$API_URL/livez"
curl --fail-with-body "$API_URL/readyz"
curl --fail-with-body "$API_URL/metrics"
```

`/livez` returns `200` with `status: "alive"` and the application version.
`/readyz` verifies store integrity/identity, fresh kernel inventory, no pending
operations, and exact desired/observed peer convergence. It returns `200` for
`ready`, or `503` for `not_ready`, with `version`, `uptime_seconds`, `interface`,
and a nullable safe `reason`. Runtime backend failure keeps liveness available
while readiness is `503` and reconciliation retries. Invalid settings, bootstrap
identity, or legacy storage can block startup before probes are available.

Scrape `/metrics` every 15–30 seconds. It exposes:

- `wireguard_api_requests_total`
- `wireguard_api_request_duration_seconds`
- `wireguard_available`
- `wireguard_peers_total`
- `wireguard_pending_operations`
- `wireguard_peer_transfer_rx_bytes`
- `wireguard_peer_transfer_tx_bytes`
- `wireguard_peer_last_handshake_timestamp_seconds`

HTTP labels use route templates, not individual UUIDs; monitoring requests are
excluded. Per-peer labels are public keys. Unavailable observations are not
reported as an empty healthy inventory: availability is `0`, peer count is
`NaN`, and stale peer series are removed. Pending count is `-1` if unavailable.

## Development

This project uses [uv](https://github.com/astral-sh/uv) for dependency management
and requires Python 3.14+, uv 0.12.23+, and make. uv can download the interpreter
pinned in `.python-version`. For an older uv binary, pass
`UV="uv tool run --from uv==0.12.23 uv"` to make.

```bash
make install       # Sync the committed lockfile
make run           # Run api:create_app with --factory and reload on 127.0.0.1:8008
make lint          # Check Python with Ruff
make format        # Format Python and apply Ruff fixes
make format-check  # Check formatting without editing files
make typecheck     # Strict mypy checks on application modules
make test          # Run pytest
make coverage      # Run branch coverage; also writes coverage.xml
make check         # Lint, formatting, types, and tests
make deployment-check  # Check service shell, Compose, and Dockerfile
```

`make run` starts only the application factory, **not container bootstrap**.
Supply the required settings, writable storage, and an existing configured
WireGuard interface whose identity/address matches this node, with permission
to manage it. Its inventory is exclusively managed by the database.

The coverage floor is **100% of statements and branches** across the application
modules listed in `pyproject.toml`: composition/routes, service, storage/locking/
migration, adapter/bootstrap, settings/models/errors/keys/configuration/version,
health, and metrics. Release-metadata behavior is also exercised by unit tests;
keep quality configuration aligned when adding helpers. Unit tests use fake
WireGuard backends and temporary storage, without privileged networking.

### Module responsibilities

- `api.py`: application factory, lifespan/reconciliation loop, safe error handlers,
  and public monitoring routes; `routes.py`: authenticated `/v1` contracts.
- `service.py`: allocation, idempotency, desired/observed views, mutation, and
  exclusive reconciliation; `storage.py`: transactional SQLite inventory and
  operation history; `storage_lock.py`: interprocess coordination;
  `legacy.py`: strict, non-destructive JSON migration validation.
- `wireguard.py`: bounded typed subprocess adapter; `bootstrap.py`: container
  identity, interface, IPv4 forwarding, and NAT; `service_run`: bootstrap followed
  by the single-worker application factory.
- `settings.py`, `models.py`, `errors.py`, `keys.py`, and `configuration.py`:
  validated settings/contracts, safe errors, canonical keys, and client rendering.
- `health.py` / `metrics.py`: readiness and per-application Prometheus collectors.
- `stats.py`: VPN aggregation and counter rates; `cgroups.py`: current-cgroup CPU
  and memory; `system_info.py`: resource/runtime composition; `telemetry.py`:
  independent background samplers and freshness-bounded read caches.
- `version.py`: `VERSION` read from `pyproject.toml`; `scripts/release_metadata.py`:
  release-tag validation and monotonic alias selection.

### Container checks

```bash
make deployment-check
docker build --check .  # Included in deployment-check; useful standalone
docker build -t wireguard-api:smoke .
python3 scripts/smoke_container.py wireguard-api:smoke
```

The smoke runner requires Python 3.10+ and Docker on a WireGuard-capable Linux
host. It creates disposable server/client/target containers, networking, and a
volume; checks authentication, `/v1` contracts, configuration templates, probes,
metrics, and applies the API-returned client configuration with `wg-quick`;
checks a real WireGuard handshake, full-tunnel routing, configured DNS, and
tunneled HTTP traffic/NAT using a controlled DNS/HTTP target on a separate network;
checks stats/rates and CPU/RAM inside a server limited to 0.5 CPU and 256 MiB;
then recreates the server and verifies persistence, deletion, and preserved
server identity. Finally, it revokes the connected client while continuous HTTP
requests are running, requires repeated failures with no recovered access, and
checks that the target remains available. It has a 300-second overall deadline
and cleans up its containers, both networks, and volume on success or failure.
Application dependencies stay inside the image; no public Internet/DNS service
is needed. This exercises the supported IPv4-only, `0.0.0.0/0` client policy.

### CI, publishing, and dependency updates

- Pull requests and pushes to `main` run Python checks and native amd64/arm64
  builds and smoke tests. Coverage XML is uploaded as an artifact.
- Release tags must be exact SemVer `vMAJOR.MINOR.PATCH`, optionally with
  prerelease/build metadata, and match `project.version` in `pyproject.toml`
  and `version.VERSION` exactly. `pyproject.toml` is the single version source.
- Full version image tags omit the leading `v`; build metadata encodes `+` as
  `_` for Docker tags. Every prerelease receives only its full version tag.
- Stable `MAJOR`, `MAJOR.MINOR`, and `latest` aliases are monotonic: only the
  highest known stable Git version in each respective scope may advance them.
  Newer known stable tags suppress older aliases even if not yet published;
  build metadata does not affect SemVer precedence.
- Publication runs quality/container checks, pushes the multi-platform image
  to Docker Hub and GHCR, and smoke-tests the exact published digest natively
  on amd64 and arm64 before promoting eligible moving aliases and creating the
  GitHub release. Docker Hub uses `DOCKER_USERNAME` / `DOCKER_TOKEN`; GHCR and
  releases use `GITHUB_TOKEN`.
- Actions and Docker base images are pinned. Enable the
  [Renovate GitHub app](https://github.com/apps/renovate) to activate weekly
  dependency/lockfile maintenance in `renovate.json`.

To refresh dependencies deliberately, update the ranges in `pyproject.toml`,
run `uv lock --upgrade`, then run `make check` and `make coverage` before
deployment checks, building, and smoke-testing the image.

## Contributors

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

Licensed under the [MIT License](LICENSE). See [CHANGELOG.md](CHANGELOG.md) for
release history.
