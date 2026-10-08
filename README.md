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
- [Compatibility policy](#compatibility-policy)
- [Usage](#usage)
- [Health and monitoring](#health-and-monitoring)
- [Troubleshooting](#troubleshooting)
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

Run one container/process owner per node and persistent directory. `service_run`
starts bootstrap followed by Uvicorn with **one worker**; use the same model for
direct application deployments. The interprocess storage lock serializes mutations,
but does not make multiple reconcilers, wg-quick, or another manager supported
owners of the same interface.

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

Generate a unique token with `openssl rand -hex 32`, put it in `API_TOKEN`, and
restrict the environment file with `chmod 600 .env`. Missing, empty,
whitespace-only, and old default tokens block application startup. Validation
errors identify the invalid setting without printing its supplied value.

```bash
docker compose up -d --build
docker compose logs app
curl --fail-with-body http://127.0.0.1:8008/readyz
```

Readiness returns HTTP `200` with `"status": "ready"` when storage and the
WireGuard peer inventory converge. Compose mounts `./config` at `WG_DATA_DIR`
and uses `/readyz` for its health check.

### Rotating the API token

The token is loaded at process startup. To rotate it, replace `API_TOKEN` in
your private `.env` (or deployment secret), then recreate the service so the new
environment is applied. Update the effective source: an exported `API_TOKEN`
overrides the Compose `.env` value. Keep the file restricted to `0600`.

```bash
docker compose up -d --no-build --force-recreate app
curl --fail-with-body http://127.0.0.1:8008/readyz
```

`docker compose restart` alone does not apply changed environment variables.
For a `docker run` deployment, remove and recreate the container with the updated
`--env-file` and the same persistent data mount. Update management clients to
send the new token: it must return `200` on `GET /v1/peers`; the previous token
must return `403`, and an omitted header still returns `401`. There is no
overlap period for old and new tokens. Recreation briefly interrupts VPN
traffic while the interface and peers are restored; server and peer identities
remain unchanged with the same data directory. `/livez`, `/readyz`, and
`/metrics` remain public.

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

Container bootstrap supports an exclusive IPv4 WireGuard interface with one
usable server address in a `/16`–`/30` pool. Choose a pool that does not overlap
your container/egress networks. An existing interface must have the expected
type, address and server key; foreign or conflicting interfaces block startup.
The host must provide WireGuard support, and the container must have `NET_ADMIN`
and IPv4 forwarding enabled (as in the included Compose file).

The egress interface must exist in the container's network namespace. Automatic
discovery uses the route to `1.1.1.1` without sending traffic; set
`WG_EGRESS_INTERFACE` explicitly for a topology requiring a different exit.
Bootstrap checks forwarding and installs pool-scoped masquerade plus outbound
and established-return forwarding rules on every run. Missing rules are repaired
without duplicates. Essential command failures prevent the API from starting;
s6 can keep the container running while retrying the failed service, so use
readiness and service logs to assess startup.

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

### Supported paths and storage compatibility

The integration-tested legacy upgrade path is **v0.4.2 → this implementation**,
using the old release's real `peers.json` and `server_private.key`. v0.4.2's
default node is `wg0` with `10.13.13.1/24`; preserve those values when upgrading
that deployment. Other releases/configurations need an inventory and identity
review first; arbitrary linuxserver wg-quick configurations are not an automatic
upgrade path.

The current SQLite format has `metadata.version = 1`. Startup requires the
recognized tables/columns, matching interface/address identity, and a successful
integrity check. Unknown schemas and corrupt databases block startup rather than
being reset. A matching version number alone is not a downgrade guarantee:
only use releases whose data/API compatibility has been explicitly verified.
The tested downgrade to v0.4.2 uses its **pre-upgrade backup**, not the new SQLite
directory. v0.4.2 does not read SQLite, and the preserved legacy JSON is not
updated after migration.

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
   Compare server public key, peer public keys, and allocations with the old
   deployment. Establish a fresh handshake and test both DNS and traffic from
   an existing client. Existing credentials remain valid for the supported
   IPv4 policy; remove obsolete `::/0` routing from old generated configurations.

Migration is **all-or-nothing**. Invalid legacy data clearly blocks application
startup; the original JSON remains untouched and no partial inventory is
accepted. Correct the migration input deliberately from your backup and retry.
Once migration is recorded, SQLite exclusively owns desired state: editing
`peers.json` does not update peers, and the application never overwrites it.

Both bootstrap and storage guard the interface/address identity. An intentional
interface or pool change needs a coordinated migration of persisted identity,
peer allocations, and client configurations. Do not casually delete
`bootstrap.json`, the database, or the server key to bypass a mismatch.

### Consistent offline backups

Stop the sole owner of the node before copying **the entire data directory**.
This includes the server key, bootstrap binding, SQLite database and any journal
or WAL files, legacy input, and operation/idempotency history. A live copy of
`peers.sqlite3` alone is not the supported backup procedure. Save the deployment
settings and exact image reference/digest alongside each checkpoint; `.env` and
saved client credentials are separate secrets and are not inside `/config`.

For the repository's Compose bind mount (`./config`), run from the project
directory on the Linux deployment host:

```bash
umask 077
mkdir -p backups
chmod 700 backups
CHECKPOINT="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="backups/config-$CHECKPOINT.tar"
CONTAINER="$(docker compose ps -q app)"
docker inspect --format '{{.Image}}' "$CONTAINER" > "$BACKUP.image-id"
# Also record the registry reference/digest used to deploy this container.
# Retain the old image locally, or save it if that digest cannot be pulled again.
docker compose stop app
for file in server_private.key bootstrap.json peers.sqlite3 peers.json; do
  if [ -f "config/$file" ]; then sudo chmod 600 "config/$file"; fi
done
sudo tar --numeric-owner -cpf - -C ./config . > "$BACKUP"
sha256sum "$BACKUP" > "$BACKUP.sha256"
docker compose start app
```

Check each command succeeds before continuing. Keep the backup/archive,
deployment secrets, and client credentials restricted to their owner (`0600`),
with private backup directories (`0700`). Encrypt copies kept off-host: the
archive contains the server's private key. For a custom mount or named volume,
use the same stopped-service procedure, mounting the source read-only into a
helper container and archiving it in full. Do not print keys or credential-bearing
API responses into backup logs.

### Restore and verify a checkpoint

1. Stop the service and retain the failed directory as a separate recovery copy.
2. Verify the archive checksum. Extract into an **empty** directory/volume;
   never overlay an old archive onto a newer database or leave newer journal
   files behind. Preserve file permissions and numeric ownership.
3. Restore the settings and image associated with that checkpoint. Preserve
   `WG_INTERFACE`, `WG_SERVER_ADDRESS`, the endpoint, listener and data mount.
4. Start only one owner of this restored node. Verify readiness, unchanged server
   public key, exact peer inventory/addresses, deletion history, and an existing
   client's fresh handshake, DNS and tunneled traffic.

For the Compose bind mount, with `BACKUP` still pointing at the selected archive:

```bash
sha256sum -c "$BACKUP.sha256"
docker compose stop app
sudo mv ./config "./config-before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -d -m 700 ./config
sudo tar --numeric-owner -xpf "$BACKUP" -C ./config
# Start with the checkpoint's image/configuration, not an accidental local rebuild.
docker compose up -d --no-build --force-recreate app
```

The checkpoint defines the recovery point: later creations, deletions and
idempotency records are not in that backup. A peer deleted **before** the
checkpoint must stay deleted. Before reopening management/UDP access, reconcile
any revocations made **after** it; restoring an older checkpoint can otherwise
bring those credentials back. Client private keys cannot be recovered from a
server backup. Lost generated client credentials require revocation and a new
creation with a new idempotency key.

### Rollback procedure

Before upgrading, take and verify an offline checkpoint and retain the exact
previous image plus its settings. If the upgrade fails:

1. Stop the candidate and checkpoint its failed directory separately for diagnosis.
2. Restore the **complete pre-upgrade checkpoint into an empty mount** using the
   procedure above. Do not point v0.4.2 at the candidate's migrated directory:
   it would use stale `peers.json`, ignoring newer peers and revocations in SQLite.
3. Select the previous image explicitly and restore its original deployment
   definition/environment. For example, an image-only Compose override can set
   `services.app.image` to the retained image ID or immutable registry digest;
   start with `--no-build --force-recreate`, including that override file.
   v0.4.2 uses `/health` and the unversioned API, so also restore its management
   client contracts and healthcheck. Its inherited networking services differ
   from the candidate; use the old deployment definition rather than assuming
   the candidate's Compose environment is equivalent.
4. Compare identity/inventory with the checkpoint and verify existing client
   connectivity. Apply post-checkpoint revocations before exposing the node.

This rollback intentionally returns to the pre-upgrade recovery point; automatic
reverse migration/merging of SQLite into legacy JSON is unsupported. A subsequent
upgrade imports that checkpoint again and assigns new UUIDs to legacy peers,
while preserving their public keys, addresses and server identity. For storage
failures, keep the damaged data, restore a verified checkpoint, and retry with
the correct writable mount/schema; do not recover by deleting the key/database
or replacing an unreadable inventory with an empty one.

## Compatibility policy

Starting with the stable **1.0.0** release, existing clients using the documented
`/v1` contract can upgrade through stable 1.x releases without changing their
requests or interpretation of existing responses. The current pre-1.0 refactor
has the [breaking migration](#supported-paths-and-storage-compatibility) described
above; publishing this policy does not change the project version or declare
1.0.0 released. Prereleases are evaluation builds and can change before the final
stable contract is published.

### Compatible and incompatible changes

| Release | Permitted changes |
| --- | --- |
| Patch (`1.x.y`) | Bug/security fixes restoring documented behavior, performance improvements, and documentation corrections that preserve the public contract. |
| Minor (`1.y.0`) | Backward-compatible features: new endpoints, optional request parameters with defaults preserving existing behavior, and additional response fields or metrics. Deprecations may be announced without removing behavior. |
| Major (`2.0.0` onward) | Incompatible API, configuration, deployment, or persistence changes, accompanied by migration instructions. Incompatible management API semantics use a new prefix such as `/v2`. |

The protected contract includes:

- Endpoint methods/paths, authentication via `X-API-Token`, documented HTTP
  outcomes, JSON media types, and the `code`/`detail` error envelope. Existing
  error codes retain their meaning; `detail` is human-readable and its exact
  wording is not a machine interface.
- Existing field names, types, nullability, timestamp/counter units, UUID peer
  and operation identifiers, and `items`/`next_cursor` pagination using `after`.
- Creation validation, duplicate/conflict handling, and persisted idempotency:
  matching retries return the original identity without credentials; different
  requests and revoked originals conflict. A `202` is durable pending work, not
  success; completion is verified, and cancellation is not successful creation.
- One-time generated credentials and `Cache-Control: no-store`, retained
  operation history, and address reservation until verified revocation.
- The documented public probe schemas/readiness semantics, metric names/labels/
  units and unavailable-value conventions, and client configuration routing.

Removing or renaming an existing endpoint/field/metric, changing a field's type
or nullability, changing units or identifier representation, requiring a new
request field/header, or tightening validation to reject previously documented
valid requests is incompatible. Adding values to an existing closed enum (for
example peer states or operation statuses) is also incompatible. Changing
idempotency, credential delivery, success/error semantics, existing defaults,
or the node's ownership/routing policy requires a major release. Rejecting
undocumented invalid inputs or correcting behavior that contradicts the contract
is a bug fix; release notes must describe any operational impact.

Clients must ignore unknown response fields, avoid depending on JSON field order,
and handle documented null/unavailable values. Requests must still use only
documented fields; creation rejects unknown fields. For example, adding an
optional server response field is compatible, while replacing a UUID with a
public key or treating a pending creation as complete is not.

### Deprecation and breaking-change announcements

Announce a deprecation in a minor release's `CHANGELOG.md` entry and GitHub release
notes, and update this README and the affected OpenAPI documentation (`deprecated`
where supported). Each notice must identify the affected feature, first deprecated
version, replacement, migration examples, and earliest major release planned for
removal. If no replacement exists, explicitly explain the migration constraint.

Deprecated behavior remains supported throughout 1.x; deprecation alone does not
change its response, validation, or side effects. Removal cannot occur in a patch
or minor release. A breaking major release must enumerate changes and provide
before/after requests, deployment/storage migration steps, supported upgrade
paths, and backup/rollback constraints. It must explain whether the previous API
prefix remains available and for how long; coexistence is not implied by `/v2`.

### Product, API, and storage versions

`pyproject.toml` is the authoritative product version, exposed in OpenAPI,
health and runtime responses and validated against the release tag. `/v1` is the
management contract generation, not the full product version. SQLite's
`metadata.version` is a storage-format identifier, not either API or product
SemVer.

Stable 1.x upgrades must preserve existing desired state and server identity.
Any storage migration needs explicit release documentation and verified forward
upgrade/recovery coverage; internal schema changes do not by themselves create
a new API generation. A compatible API does **not** guarantee an older image can
read a newer database. Use the documented
[storage compatibility](#supported-paths-and-storage-compatibility),
[offline backup](#consistent-offline-backups), and [rollback](#rollback-procedure)
procedures, and pin an exact image version/digest when deploying.

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

## Troubleshooting

Run these commands from the Compose project directory on the Linux deployment
host. Set `API_URL` to your management URL (default `http://127.0.0.1:8008`) and
`API_TOKEN` to the current token as in [Usage](#usage); account for a custom
`API_PORT` or management tunnel. Container-side commands use the configured
`WG_INTERFACE` and `WG_DATA_DIR` from the container environment.

### Identify the failure stage

```bash
docker compose ps -a app
docker compose logs --tail=100 app
curl --fail-with-body "$API_URL/livez"
curl --fail-with-body "$API_URL/readyz"
```

An expected `503` makes `curl --fail-with-body` exit nonzero but still prints the
diagnostic JSON. No HTTP listener suggests settings, bootstrap, or storage
initialization failure; a running container alone does not prove startup succeeded.
`/livez` returning `200` with `/readyz` returning `503` means the process is alive
but not operationally ready:

| Readiness `reason` / symptom | Next procedure |
| --- | --- |
| `wireguard_unavailable` | [WireGuard unavailable or clients cannot connect](#wireguard-unavailable-or-clients-cannot-connect) |
| No listener; bootstrap error in logs | [Bootstrap does not finish](#bootstrap-does-not-finish) |
| `state_not_converged`; operations remain pending | [Reconciliation or peer restoration is pending](#reconciliation-or-peer-restoration-is-pending) |
| `storage_unavailable`; storage initialization error | [Storage failure or corruption](#storage-failure-or-corruption) |
| Creation returns `409` with pool exhaustion | [Client address pool is exhausted](#client-address-pool-is-exhausted) |

API errors use `code`/`detail`; readiness uses `status`/`reason`. A `409` can also
mean a duplicate public key, reserved requested address, or idempotency conflict;
use its detail and request context to distinguish it from exhaustion. A public
probe succeeding does not verify your token; follow
[token rotation](#rotating-the-api-token) for authentication failures.

### WireGuard unavailable or clients cannot connect

1. Check that the deployment grants `NET_ADMIN`, the Linux host supports
   WireGuard, and the configured interface exists in the **container** namespace:

   ```bash
   docker compose exec -T app sh -eu -c '
     ip -details link show dev "$WG_INTERFACE"
     ip -4 address show dev "$WG_INTERFACE"
     wg show "$WG_INTERFACE" public-key
     wg show "$WG_INTERFACE" listen-port
     wg show "$WG_INTERFACE" allowed-ips
     wg show "$WG_INTERFACE" latest-handshakes
     wg show "$WG_INTERFACE" transfer
   '
   ```

   These selectors expose public identity/counters, not private keys. Do not
   collect `wg show ... dump`, `wg showconf`, `.env`, private key files, or
   credential-bearing creation responses in support logs.
2. If the interface is missing or commands fail, correct host kernel support,
   capabilities, or the container configuration, then recreate with
   `docker compose up -d --no-build --force-recreate app` using the same data mount
   and intended image. Bootstrap recreates networking and reconciliation restores
   peers. Raising `WG_COMMAND_TIMEOUT` is only appropriate after confirming a
   slow command; it does not fix permissions or missing kernel support.
3. If readiness is `200` but there is no fresh handshake, verify the client has
   the matching server public key and usable credentials, the peer operation is
   `complete`, the tunnel is active and sending traffic, and the endpoint resolves
   to the reachable server. Match `SERVER_ENDPOINT`'s UDP port to `VPN_PORT`,
   and the mapped container port to `WG_LISTEN_PORT`. Check host/cloud firewalls
   and upstream port forwarding. Readiness checks inventory, not UDP reachability
   or client connectivity.
4. If handshakes work but traffic fails, inspect container forwarding and NAT:

   ```bash
   docker compose exec -T app sh -eu -c '
     ip -4 route get 1.1.1.1
     sysctl -n net.ipv4.ip_forward
     iptables -t nat -S POSTROUTING
     iptables -S FORWARD
   '
   ```

   Forwarding must be `1`; masquerade and forwarding rules must match the pool
   and actual egress interface. Check for earlier firewall rules blocking traffic,
   overlapping networks, and an incorrect `WG_EGRESS_INTERFACE`. Bootstrap repairs
   missing owned rules on restart/recreation; it does not continuously reconcile
   firewall rules. Verify the client's IPv4 full-tunnel route (`0.0.0.0/0`), then
   test an IPv4 destination and the configured `CLIENT_DNS` from the connected
   client to distinguish routing from DNS failure.

Recovery is complete when readiness is `200`, server identity and client `/32`
allocations match the saved inventory, and an existing client obtains a fresh
handshake plus working DNS and tunneled HTTP traffic.

### Bootstrap does not finish

1. Read the service logs. Missing/empty/default `API_TOKEN` or an invalid endpoint
   must be corrected in the effective deployment environment before recreating
   the container. Use the [configuration requirements](#configuration), rather
   than bypassing validation. s6 may retry the service inside a running container.
2. For `Invalid or inaccessible bootstrap identity`, check the mount and file
   metadata using [storage diagnostics](#storage-failure-or-corruption). Confirm
   the original `server_private.key` is a valid regular file and that
   `bootstrap.json` matches the intended `WG_INTERFACE`/`WG_SERVER_ADDRESS`.
   Restore accidentally changed settings; recover damaged identity files from
   a complete checkpoint. Do not delete them to generate a replacement identity.
3. Interface type/address/key mismatch means bootstrap found a conflicting node.
   Confirm which process owns it, remove competing wg-quick/manager ownership,
   and restore the correct deployment settings. An intentional pool/interface
   change needs [explicit migration](#supported-paths-and-storage-compatibility).
4. For an essential command failure, use the WireGuard and forwarding diagnostics
   above. Ensure the egress interface exists in the container, route discovery
   has an unambiguous exit, and `ip`, `wg`, `sysctl`, and `iptables` can perform
   the required operations. Correct the cause and recreate the same deployment.
   Partial bootstrap is retryable: existing keys are preserved and missing
   interface/address/firewall setup is repaired without duplicate owned rules.

Check `/readyz`, compare the server public key with the pre-failure value, and
test an existing client's tunnel. If bootstrap succeeds but peers are pending,
continue with reconciliation below; bootstrap itself does not restore inventory.

### Reconciliation or peer restoration is pending

1. Inspect the operation returned by the mutation and the desired peer state:

   ```bash
   curl --fail-with-body "$API_URL/v1/operations/$OPERATION_ID" \
     -H "X-API-Token: $API_TOKEN"
   curl --fail-with-body "$API_URL/v1/peers?limit=100" \
     -H "X-API-Token: $API_TOKEN"
   curl --fail-with-body "$API_URL/metrics"
   ```

   Follow `next_cursor` using `after` for larger inventories. Check the operation's
   safe `error`, peer `state`/`applied`, `wireguard_available`, and
   `wireguard_pending_operations`. There is no bulk operation-list endpoint;
   retain operation IDs returned by mutations. A `202` already records intent.
2. Fix WireGuard/storage failures using the corresponding procedure. Allow the
   background loop to retry every `WG_RECONCILE_INTERVAL` seconds (default `5`),
   with additional time for bounded commands and a larger inventory. Generic
   reconciliation-retry logs do not mean intent was discarded. After a crash or
   recreation, start with the same complete data mount; SQLite recovers committed
   intent and reconciliation retries pending work.
3. Stop competing owners if peers keep changing. SQLite owns the whole interface;
   unmanaged peers are removed and managed `/32` assignments repaired. Editing
   `peers.json`, `/config/wg_confs`, or manually running `wg set` does not change
   desired state. There is no manual reconciliation HTTP endpoint.
4. Poll until the operation is `complete` and readiness is `200`. For deletion,
   verify the peer is absent and its connected client loses access. A superseded
   create can finish as `cancelled`; inspect the deletion operation to establish
   revocation. Addresses remain reserved during pending deletion.

For uncertain creation results, use the same idempotency key/body to recover
identity. Replays do not return credentials. Follow
[lost-response recovery](#retry-semantics-and-lost-responses) before creating a
replacement; repeatedly using new request keys can consume the pool.

### Storage failure or corruption

1. Inspect free space/inodes and mount/file metadata, without displaying contents:

   ```bash
   docker compose exec -T app sh -eu -c '
     df -h "$WG_DATA_DIR"
     df -i "$WG_DATA_DIR"
     stat -c "%a %u:%g %n" "$WG_DATA_DIR"
     for file in peers.sqlite3 peers.sqlite3.lock bootstrap.json server_private.key; do
       if [ -e "$WG_DATA_DIR/$file" ]; then
         stat -c "%a %u:%g %n" "$WG_DATA_DIR/$file"
       fi
     done
   '
   CONTAINER="$(docker compose ps -a -q app)"
   docker inspect --format '{{json .Mounts}}' "$CONTAINER"
   ```

   If the container has exited, inspect the corresponding host mount instead.
   Confirm the intended data directory is mounted read-write, has room for SQLite
   transactions/journals, and permits the service to create files, lock, and apply
   `0600` permissions. Correct ownership/access for the actual service user;
   widening secret files to world-readable is not a recovery step.
2. For lock timeouts, find and stop the competing process/container using the
   directory. A crash releases the OS lock; the remaining `.lock` file is normal.
   Do not unlink it to bypass contention, since owners must lock the same inode.
3. Schema/network-identity errors require the image and settings compatible with
   that data. Restore accidentally changed interface/address settings. Do not
   edit `metadata.version` to force acceptance; it is not a compatibility switch.
   Invalid initial legacy input requires deliberate correction from the original
   backup; import is all-or-nothing and preserves its source.
4. For corruption or unrecoverable identity damage, stop the service and retain
   the failed directory, then [restore a verified checkpoint](#restore-and-verify-a-checkpoint)
   into an empty mount with its matching image/settings. Follow the full
   [offline backup procedure](#consistent-offline-backups) for recovery copies.
   Never replace an unreadable inventory with an empty database, discard journals,
   or delete the server key. If a candidate upgrade failed, use the
   [rollback procedure](#rollback-procedure); v0.4.2 needs its complete pre-upgrade
   checkpoint, not the migrated directory or its stale legacy JSON.

After recovery, verify readiness, server identity, exact peer addresses, completed
deletions and an existing client's handshake/DNS/tunneled traffic. Restore returns
to the checkpoint's recovery point: apply post-checkpoint revocations before
reopening access, and retain credential-free idempotency/operation history.
These restore/rollback and read-only/corrupt/incompatible-schema recovery paths
are exercised by the [lifecycle container checks](#container-checks).

### Client address pool is exhausted

Automatic allocation returns `409` with `code: "conflict"` and detail
`"The client address pool is exhausted"` when no usable address remains. Readiness
can still be `200`; a full pool is not a backend failure.

```bash
curl --fail-with-body "$API_URL/v1/server" -H "X-API-Token: $API_TOKEN"
curl --fail-with-body "$API_URL/v1/peers?limit=100" \
  -H "X-API-Token: $API_TOKEN"
```

Capacity is the subnet's address count minus network, broadcast, and server
addresses: `/24` allows `253` clients; `/30` allows `1`. `reserved` includes
pending, active, and deleting peers; `available` must increase only after verified
revocation. Paginate through all peers and identify unwanted allocations,
including creations whose generated response was lost.

Revoke a selected unwanted peer through the API using its UUID:

```bash
curl --fail-with-body -i -X DELETE "$API_URL/v1/peers/$PEER_ID" \
  -H "X-API-Token: $API_TOKEN"
```

A `204` verifies deletion. For `202`, use the returned **deletion** operation ID
and [poll it](#revoke-a-peer-and-poll-an-operation); fix any pending backend failure
before expecting the address to be released. Confirm `available` has increased
before retrying creation with its original key/body. Existing deleted peers'
request keys stay revoked and cannot be recycled for a new peer.

A requested address already reserved also returns `409`; select a usable free
address or omit `address` for automatic allocation. Changing the pool prefix on
an existing volume is not an exhaustion workaround: interface and database
identity guards require coordinated migration. For additional capacity, provision
a separate exclusive node with its own persistent directory and non-overlapping
pool, or plan an explicit allocation/client-configuration migration.

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
health/metrics, stats/cgroups/system_info/telemetry, and release metadata. Keep
typing and coverage module lists aligned when adding helpers. Unit tests use fake
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
make bootstrap-check
python3 scripts/smoke_container.py wireguard-api:smoke
```

The smoke runner requires Python 3.10+ and Docker on a WireGuard-capable Linux
host. `make bootstrap-check` runs `scripts/smoke_bootstrap.py` against `IMAGE`
(default `wireguard-api:smoke`). It checks Compose's required token, negative
application startup with missing/empty/whitespace/default tokens, secret-free
diagnostics, default `wg0` readiness with automatic egress discovery, token
rotation, server-key permissions/ownership and temporary-file cleanup, permission
repair with stable identity, restart/recreation and repeatable firewall setup.
Test-only command wrappers inject failures into key generation/derivation,
interface creation/address/key/link setup, forwarding and firewall checks/writes;
each failure must block the HTTP listener and permit recovery with the same
volume. The suite has a 300-second deadline and removes its containers and volumes.

The traffic smoke runner creates disposable server/client/target containers,
networking, and a volume; checks authentication, `/v1` contracts, configuration
templates, probes and metrics; applies the API-returned client configuration with
`wg-quick`;
checks a real WireGuard handshake, full-tunnel routing, configured DNS, and
tunneled HTTP traffic/NAT using a controlled DNS/HTTP target on a separate network;
checks stats/rates and CPU/RAM inside a server limited to 0.5 CPU and 256 MiB;
then recreates the server and verifies persistence, deletion, and preserved
server identity, repaired `0600` key permissions and no leaked temporary key files.
Finally, it revokes the connected client while continuous HTTP
requests are running, requires repeated failures with no recovered access, and
checks that the target remains available. It has a 300-second overall deadline
and cleans up its containers, both networks, and volume on success or failure.
Application dependencies stay inside the image; no public Internet/DNS service
is needed. This exercises the supported IPv4-only, `0.0.0.0/0` client policy.

For version-to-version lifecycle checks, prepare a **different** v0.4.2 baseline
image on the candidate's architecture, then run:

```bash
# Native source rebuild of the exact v0.4.2 commit, using its original Dockerfile.
git archive --format=tar d4d5bd8a162b8ee57c3dc3bcaeeb57fab5147aae |
  docker build -t wireguard-api:baseline-0.4.2 -
make lifecycle-check
# Override either image when testing a published candidate/different local tag:
# make lifecycle-check IMAGE=registry/image@sha256:... PREVIOUS_IMAGE=old-image
```

On amd64, CI uses the actual published v0.4.2 image pinned to
`ragnarok22/wireguard-api@sha256:58e3deafcc1592a67061a3a1aa86f7c87074a33977e8a4d7e3eddc30baeb9503`.
That release has no arm64 image, so arm64 CI rebuilds its pinned release commit
and original Dockerfile natively. The original Dockerfile's base/uv references
are floating; that source rebuild validates the legacy application/storage
path, not byte-for-byte identity with the published amd64 artifact.

`scripts/smoke_lifecycle.py` checks working legacy-client DNS/HTTP/NAT before and
after upgrade, server identity, migration, durable pending create/delete recovery
after SIGKILL with an uncommitted SQLite transaction, same-image recreation,
offline tar backup/restore into a new volume (bytes, modes and ownership),
read-only/corrupt/incompatible-schema rejection and recovery, and rollback plus
re-upgrade from the pre-upgrade checkpoint. Peer UUIDs/allocations, completed
deletion operations and credential-free idempotency replays survive candidate
restoration. Deleted peers remain absent at each checkpoint. It has a 600-second
deadline and removes all test containers, networks, archives and volumes on
success/failure. The legacy bootstrap may make its historical outbound Internet
check; the actual VPN/DNS traffic checks use controlled local targets.

### CI, publishing, and dependency updates

- Pull requests and pushes to `main` run Python checks and native amd64/arm64
  builds, bootstrap rejection/repair checks, smoke tests, and
  upgrade/backup/rollback lifecycle checks. Coverage XML
  is uploaded as an artifact.
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
  to Docker Hub and GHCR, and runs bootstrap, smoke and lifecycle tests against
  the exact published digest natively on amd64 and arm64 before promoting eligible
  moving aliases and creating the GitHub release. Docker Hub uses `DOCKER_USERNAME` /
  `DOCKER_TOKEN`; GHCR and releases use `GITHUB_TOKEN`.
- Actions and Docker base images are pinned. Enable the
  [Renovate GitHub app](https://github.com/apps/renovate) to activate weekly
  dependency/lockfile maintenance in `renovate.json`.

### Publishing prerequisites and release verification

Configure repository Actions secrets `DOCKER_USERNAME` and `DOCKER_TOKEN` for
an account with push access to `ragnarok22/wireguard-api` on Docker Hub. GHCR uses
the workflow's `GITHUB_TOKEN`; the repository must permit package publication,
and an existing GHCR package must grant the repository access. GitHub release
creation requires the workflow's `contents: write` permission. Native amd64 and
arm64 GitHub-hosted runners must be available. The workflow logs in to each
registry; no developer-local registry credentials are required for publication.

Before tagging, update `pyproject.toml`, run `uv lock` without dependency
upgrades, and add a nonempty entry for that exact version in `CHANGELOG.md`.
Review the changelog's breaking changes and migration instructions, then run
`make check`, `make coverage`, deployment and container checks. Push an annotated
`vMAJOR.MINOR.PATCH` tag only after the intended release commit is ready. Tag
publication invokes the workflow automatically; do not create a GitHub release
manually ahead of image verification.

For example, `v0.9.0-rc.1` publishes only `0.9.0-rc.1` in both registries and
creates a GitHub prerelease after successful verification. It leaves `latest`,
`0`, `0.9` and older stable aliases untouched. Eligible final `v0.9.0` publishes
`0.9.0`, then promotes `0`, `0.9` and `latest` to its verified digest. `0.4`
remains on the newest known stable 0.4 release. Pre-1.0 releases can contain
breaking changes; use an exact version/digest and review migration instructions.

Publication asserts that both registry indexes contain linux/amd64 and
linux/arm64. Four native verification jobs pull the exact digest independently
from Docker Hub and GHCR on each architecture and run bootstrap, peer/persistence,
real handshake/DNS/HTTP/NAT and upgrade/restore/rollback checks. The smoke runner
checks product, OpenAPI, health and runtime versions; published-image checks also
require OCI version/revision labels and the expected architecture. GitHub release
notes include the exact changelog entry before automatically generated notes.
Record workflow URLs, tested digests and pre/post alias comparisons in the release
validation issue. Promotion and release creation require all four jobs to pass.

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
