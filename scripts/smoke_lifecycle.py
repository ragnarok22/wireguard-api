"""Real v0.4.2 upgrade, crash recovery, offline backups, restore, and rollback.

Run as a module: python3 -m scripts.smoke_lifecycle CANDIDATE PREVIOUS_IMAGE.
Both images must already be present and use the same native architecture.
"""

import argparse
import base64
import json
import time
import uuid
from collections.abc import Callable

from scripts.smoke_container import TARGET_SCRIPT, DockerCommandError, docker

SNAPSHOT_SCRIPT = """
import hashlib
import json
import stat
from pathlib import Path

root = Path('/source')
snapshot = {}
for path in sorted(root.rglob('*')):
    info = path.lstat()
    entry = {'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid}
    if path.is_symlink():
        entry['link'] = str(path.readlink())
    elif path.is_file():
        entry['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    snapshot[str(path.relative_to(root))] = entry
print(json.dumps(snapshot))
"""


class LifecycleSmoke:
    def __init__(self, candidate: str, previous: str) -> None:
        self.candidate, self.previous = candidate, previous
        self.name = f"wireguard-api-lifecycle-{uuid.uuid4().hex[:12]}"
        self.server, self.client, self.target = (
            f"{self.name}-{role}" for role in ("server", "client", "target")
        )
        self.transport, self.egress = f"{self.name}-net", f"{self.name}-egress"
        self.original, self.restored, self.recovery, self.archives = (
            f"{self.name}-{role}"
            for role in ("config", "restored", "recovery", "backups")
        )
        self.token = uuid.uuid4().hex
        self.deadline = time.monotonic() + 600
        self.stage = "initialization"

    def command(self, *args: str, input_text: str | None = None) -> str:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Lifecycle test exceeded its 600-second deadline")
        return docker(*args, input_text=input_text, timeout=min(30, remaining))

    def wait_for(
        self, predicate: Callable[[], bool], description: str, seconds: float = 60
    ) -> None:
        end = min(self.deadline, time.monotonic() + seconds)
        while time.monotonic() < end:
            try:
                if predicate():
                    return
            except (DockerCommandError, ValueError):
                pass
            time.sleep(0.25)
        raise RuntimeError(f"Timed out waiting for {description}")

    def request(
        self,
        path: str,
        method: str = "GET",
        body: dict | None = None,
        request_key: str = "lifecycle",
    ) -> tuple[int, str]:
        args = [
            "exec",
            self.server,
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "3",
            "--write-out",
            "\n%{http_code}",
            "--request",
            method,
            "--header",
            f"X-API-Token: {self.token}",
        ]
        if body is not None:
            args.extend(
                [
                    "--header",
                    "Content-Type: application/json",
                    "--header",
                    f"Idempotency-Key: {request_key}",
                    "--data",
                    json.dumps(body),
                ]
            )
        content, status = self.command(*args, f"http://127.0.0.1:8008{path}").rsplit(
            "\n", 1
        )
        return int(status), content

    def ip(self, container: str, network: str) -> str:
        return self.command(
            "inspect",
            container,
            "--format",
            f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}',
        )

    def helper(self, volume: str, *args: str, readonly: bool = False) -> str:
        mount = f"type=volume,source={volume},target=/source"
        if readonly:
            mount += ",readonly"
        return self.command(
            "run",
            "--rm",
            "--name",
            f"{self.name}-helper",
            "--network",
            "none",
            "--platform",
            self.platform,
            "--mount",
            mount,
            "--mount",
            f"type=volume,source={self.archives},target=/backup",
            "--entrypoint",
            args[0],
            self.candidate,
            *args[1:],
        )

    def snapshot(self, volume: str) -> dict:
        return json.loads(
            self.helper(volume, "python", "-c", SNAPSHOT_SCRIPT, readonly=True)
        )

    def backup(self, volume: str, filename: str) -> dict:
        # The server is stopped/removed before every backup: copy ALL of /config.
        assert (
            self.command("ps", "--quiet", "--filter", f"name=^{self.server}$") == ""
        ), "Never archive a running server"
        snapshot = self.snapshot(volume)
        self.helper(
            volume,
            "sh",
            "-c",
            'umask 077; tar -cpf "/backup/$1" -C /source .',
            "backup",
            filename,
            readonly=True,
        )
        assert (
            self.helper(
                volume, "stat", "-c", "%a", f"/backup/{filename}", readonly=True
            )
            == "600"
        ), "Backup archive must be private"
        assert self.snapshot(volume) == snapshot, "Backup modified its source"
        return snapshot

    def restore(self, volume: str, filename: str, expected: dict) -> None:
        assert self.snapshot(volume) == {}, "Restore requires an empty volume"
        self.helper(
            volume,
            "sh",
            "-c",
            'tar -xpf "/backup/$1" -C /source',
            "restore",
            filename,
        )
        assert self.snapshot(volume) == expected, (
            "Restore must preserve all bytes, modes and ownership"
        )

    def stop(self, *, crash: bool = False) -> None:
        if crash:
            self.command("kill", "--signal", "KILL", self.server)
        else:
            self.command("stop", "--time", "10", self.server)
        self.command("rm", self.server)

    def start(
        self,
        image: str,
        volume: str,
        *,
        legacy: bool = False,
        readonly: bool = False,
        ready: bool = True,
    ) -> None:
        mount = f"type=volume,source={volume},target=/config"
        if readonly:
            mount += ",readonly"
        platform = self.previous_platform if legacy else self.platform
        self.command(
            "run",
            "--detach",
            "--name",
            self.server,
            "--platform",
            platform,
            "--network",
            self.egress,
            "--cap-add",
            "NET_ADMIN",
            "--sysctl",
            "net.ipv4.conf.all.src_valid_mark=1",
            "--sysctl",
            "net.ipv4.ip_forward=1",
            "--env",
            f"API_TOKEN={self.token}",
            "--env",
            "SERVER_ENDPOINT=node.smoke.test:51820",
            "--env",
            "WG_INTERFACE=wg0",
            "--env",
            "WG_SERVER_ADDRESS=10.13.13.1/24",
            "--env",
            f"CLIENT_DNS={self.target_ip}",
            "--env",
            "WG_EGRESS_INTERFACE=eth0",
            "--env",
            "WG_RECONCILE_INTERVAL=0.25",
            "--mount",
            mount,
            image,
        )
        self.command(
            "network",
            "connect",
            "--alias",
            "node.smoke.test",
            self.transport,
            self.server,
        )
        if ready:
            route = "/health" if legacy else "/readyz"
            self.wait_for(lambda: self.request(route)[0] == 200, f"{route} readiness")
            # Assert the running image, not merely a differing CLI tag.
            actual = self.command("inspect", self.server, "--format", "{{.Image}}")
            expected = self.command("image", "inspect", image, "--format", "{{.Id}}")
            assert actual == expected, "Unexpected server image"

    def configure_client(self, *, reconnect: bool = False) -> None:
        if reconnect:
            self.command("exec", self.client, "wg-quick", "down", "/tmp/wgc.conf")
        else:
            self.command(
                "exec",
                "-i",
                self.client,
                "sh",
                "-c",
                "umask 077; cat > /tmp/wgc.conf",
                input_text=self.client_config,
            )
        # Fixed commands; key-bearing configuration travels over stdin only.
        self.command("exec", self.client, "wg-quick", "up", "/tmp/wgc.conf")
        self.command("exec", self.server, "iptables", "-P", "FORWARD", "DROP")
        assert "dev wgc" in self.command(
            "exec", self.client, "ip", "route", "get", self.target_ip
        ), "Client egress must traverse the tunnel"
        server_ip = self.ip(self.server, self.egress)

        def traffic() -> bool:
            source = self.command(
                "exec",
                self.client,
                "curl",
                "--noproxy",
                "*",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "3",
                "http://egress.smoke.test:8080/",
            )
            handshakes = self.command(
                "exec", self.server, "wg", "show", "wg0", "latest-handshakes"
            )
            return source == server_ip and any(
                line.split()[0] == self.active_key and int(line.split()[1]) > 0
                for line in handshakes.splitlines()
            )

        self.wait_for(traffic, "client handshake, configured DNS and tunneled HTTP/NAT")

    def peers(self) -> list[dict]:
        status, content = self.request("/v1/peers")
        assert status == 200, "Peer list unavailable"
        return json.loads(content)["items"]

    def verify_candidate(self, expected: dict[str, tuple[str, str]]) -> None:
        status, content = self.request("/v1/server")
        assert status == 200 and json.loads(content)["public_key"] == self.server_key, (
            "Server identity changed"
        )
        actual = {
            peer["public_key"]: (peer["id"], peer["address"]) for peer in self.peers()
        }
        assert actual == expected, "Peer identities or allocations changed"
        kernel = set(
            self.command("exec", self.server, "wg", "show", "wg0", "peers").splitlines()
        )
        assert kernel == set(expected), "Kernel inventory differs from desired state"
        for filename in ("server_private.key", "bootstrap.json", "peers.sqlite3"):
            mode = self.command(
                "exec", self.server, "stat", "-c", "%a", f"/config/{filename}"
            )
            assert mode == "600", "Persisted identity/database must remain private"
        assert self.request(f"/v1/peers/{self.deleted_id}")[0] == 404
        status, content = self.request(f"/v1/operations/{self.delete_operation}")
        assert status == 200 and json.loads(content)["status"] == "complete"
        status, content = self.request(
            "/v1/peers", "POST", self.pending_body, "pending-create"
        )
        replay = json.loads(content)
        assert status == 200 and replay["peer"]["id"] == self.pending_id
        assert replay["private_key"] is None and replay["client_config"] is None

    def expect_blocked(self, reason: str) -> None:
        self.wait_for(
            lambda: reason in self.command("logs", self.server),
            f"startup to reject {reason}",
            30,
        )
        try:
            status, _ = self.request("/readyz")
        except DockerCommandError:
            return  # API never started: rejected before it can accept writes.
        assert status != 200, "Invalid storage must never be ready"

    def run(self) -> None:
        self.platform = self.command(
            "image", "inspect", self.candidate, "--format", "{{.Os}}/{{.Architecture}}"
        )
        self.previous_platform = self.command(
            "image", "inspect", self.previous, "--format", "{{.Os}}/{{.Architecture}}"
        )
        assert self.platform == self.previous_platform, (
            "Use a native v0.4.2 image rebuilt from its release source on arm64"
        )
        assert self.command(
            "image", "inspect", self.candidate, "--format", "{{.Id}}"
        ) != (self.command("image", "inspect", self.previous, "--format", "{{.Id}}")), (
            "Upgrade requires different image bytes"
        )
        for volume in (self.original, self.restored, self.recovery, self.archives):
            self.command("volume", "create", volume)
        for network in (self.transport, self.egress):
            self.command("network", "create", network)
        self.command(
            "run",
            "--detach",
            "--name",
            self.target,
            "--platform",
            self.platform,
            "--network",
            self.egress,
            "--entrypoint",
            "python",
            self.candidate,
            "-u",
            "-c",
            TARGET_SCRIPT,
        )
        self.target_ip = self.ip(self.target, self.egress)
        self.command(
            "run",
            "--detach",
            "--name",
            self.client,
            "--platform",
            self.platform,
            "--network",
            self.transport,
            "--cap-add",
            "NET_ADMIN",
            "--sysctl",
            "net.ipv4.conf.all.src_valid_mark=1",
            "--entrypoint",
            "sleep",
            self.candidate,
            "infinity",
        )
        resolver = self.command("exec", self.client, "cat", "/etc/resolv.conf")
        self.command("exec", self.client, "resolvconf", "-u")
        self.command(
            "exec", "-i", self.client, "resolvconf", "-a", "eth0", input_text=resolver
        )

        self.stage = "v0.4.2 live peers and pre-upgrade backup"
        self.start(self.previous, self.original, legacy=True)
        status, content = self.request("/openapi.json")
        assert status == 200 and json.loads(content)["info"]["version"] == "0.4.2"
        self.server_key = self.command(
            "exec", self.server, "wg", "show", "wg0", "public-key"
        )
        status, content = self.request("/peers", "POST", {})
        assert status == 201, "Legacy peer creation failed"
        active = json.loads(content)
        self.active_key = active["public_key"]
        self.client_config = (
            f"[Interface]\nPrivateKey = {active['private_key']}\n"
            f"Address = {active['allowed_ips'][0]}\nDNS = {self.target_ip}\n\n"
            f"[Peer]\nPublicKey = {self.server_key}\n"
            "Endpoint = node.smoke.test:51820\nAllowedIPs = 0.0.0.0/0\n"
            "PersistentKeepalive = 25\n"
        )
        self.configure_client()
        # A URL-safe legacy key exercises a deletion before the migration checkpoint.
        deleted_key = base64.b64encode(bytes([1]) * 32).decode()
        status, _ = self.request(
            "/peers",
            "POST",
            {"public_key": deleted_key, "allowed_ips": ["10.13.13.9/32"]},
        )
        assert status == 201
        assert self.request(f"/peers/{deleted_key}", "DELETE")[0] == 204
        self.stop()
        # Repair old permissive secret-file modes before creating the backup.
        self.helper(
            self.original,
            "chmod",
            "600",
            "/source/server_private.key",
            "/source/peers.json",
        )
        legacy_snapshot = self.backup(self.original, "pre-upgrade.tar")

        self.stage = "old-image to candidate-image upgrade"
        self.start(self.candidate, self.original)
        migrated = self.peers()
        assert len(migrated) == 1 and migrated[0]["public_key"] == self.active_key
        assert migrated[0]["address"] + "/32" == active["allowed_ips"][0]
        assert (
            json.loads(self.request("/v1/server")[1])["public_key"] == self.server_key
        )
        assert (
            self.snapshot(self.original)["peers.json"] == legacy_snapshot["peers.json"]
        )
        expected = {self.active_key: (migrated[0]["id"], migrated[0]["address"])}
        self.configure_client(reconnect=True)
        print(
            "v0.4.2 → candidate: identity, migrated peer, DNS and tunnel traffic passed"
        )

        self.stage = "durable pending create/delete and abrupt termination"
        doomed_key = base64.b64encode(bytes([2]) * 32).decode()
        status, content = self.request(
            "/v1/peers",
            "POST",
            {"key_mode": "external", "public_key": doomed_key, "address": "10.13.13.9"},
            "doomed-create",
        )
        assert status in (201, 202)
        self.deleted_id = json.loads(content)["peer"]["id"]
        # Fail only kernel mutations, keeping observations and key generation real.
        # This exercises the API's durable-202 boundary before abrupt termination.
        self.command(
            "exec",
            "-i",
            self.server,
            "python",
            "-c",
            "import os, sys; from pathlib import Path; "
            "p = Path('/app/.venv/bin/wg'); assert not p.exists(); "
            "p.write_text(sys.stdin.read()); os.chmod(p, 0o700)",
            input_text="#!/bin/sh\n"
            'if [ "$1" = set ] && [ "$3" = peer ]; then exit 1; fi\n'
            'exec /usr/bin/wg "$@"\n',
        )
        self.pending_body = {"key_mode": "generated", "address": "10.13.13.8"}
        status, content = self.request(
            "/v1/peers", "POST", self.pending_body, "pending-create"
        )
        assert status == 202, "Failed kernel mutation must leave a durable creation"
        pending = json.loads(content)
        self.pending_id = pending["peer"]["id"]
        expected[pending["peer"]["public_key"]] = (self.pending_id, "10.13.13.8")
        status, content = self.request(f"/v1/peers/{self.deleted_id}", "DELETE")
        assert status == 202, "Failed kernel mutation must leave a durable deletion"
        self.delete_operation = json.loads(content)["id"]
        assert self.request("/readyz")[0] == 503
        assert self.request("/livez")[0] == 200
        # Kill with a real SQLite transaction open: uncommitted damage must roll back.
        self.command(
            "exec",
            "--detach",
            self.server,
            "python",
            "-c",
            "import sqlite3, time; from pathlib import Path; "
            "db = sqlite3.connect('/config/peers.sqlite3'); "
            "db.execute('BEGIN IMMEDIATE'); db.execute('DELETE FROM peers'); "
            "Path('/tmp/uncommitted').touch(); time.sleep(600)",
        )
        self.wait_for(
            lambda: (
                self.command("exec", self.server, "test", "-f", "/tmp/uncommitted")
                == ""
            ),
            "an interrupted, uncommitted database write",
        )
        self.stop(crash=True)
        self.start(self.candidate, self.original)
        self.verify_candidate(expected)
        self.configure_client(reconnect=True)

        self.stage = "candidate recreation and offline backup"
        self.stop()
        self.start(self.candidate, self.original)
        self.verify_candidate(expected)
        self.configure_client(reconnect=True)
        self.stop()
        candidate_snapshot = self.backup(self.original, "candidate.tar")
        self.restore(self.restored, "candidate.tar", candidate_snapshot)
        self.start(self.candidate, self.restored)
        self.verify_candidate(expected)
        self.configure_client(reconnect=True)
        self.stop()
        print("Crash recovery, recreation and fresh-volume backup restoration passed")

        self.stage = "read-only storage rejection and recovery"
        before = self.snapshot(self.restored)
        self.start(self.candidate, self.restored, readonly=True, ready=False)
        self.expect_blocked("Invalid or inaccessible bootstrap identity")
        self.stop()
        assert self.snapshot(self.restored) == before, (
            "Read-only startup modified storage"
        )

        self.stage = "corrupted database rejection and restore"
        self.helper(
            self.restored,
            "python",
            "-c",
            "from pathlib import Path; "
            "Path('/source/peers.sqlite3').write_bytes(b'corrupt')",
        )
        self.start(self.candidate, self.restored, ready=False)
        self.expect_blocked("Peer storage unavailable")
        self.stop()
        assert (
            self.snapshot(self.restored)["peers.sqlite3"]["sha256"]
            != (candidate_snapshot["peers.sqlite3"]["sha256"])
        ), "Corrupted database must not be silently reset"
        self.restore(self.recovery, "candidate.tar", candidate_snapshot)
        self.start(self.candidate, self.recovery)
        self.verify_candidate(expected)
        self.configure_client(reconnect=True)
        self.stop()
        print(
            "Read-only/corrupt storage rejected; backup recovery preserved live peers"
        )

        self.stage = "rollback to v0.4.2 with its pre-upgrade checkpoint"
        # Reuse the damaged restore volume only after replacing it in its entirety.
        self.command("volume", "rm", self.restored)
        self.command("volume", "create", self.restored)
        self.restore(self.restored, "pre-upgrade.tar", legacy_snapshot)
        self.start(self.previous, self.restored, legacy=True)
        assert (
            self.command("exec", self.server, "wg", "show", "wg0", "public-key")
            == self.server_key
        ), "Rollback changed server identity"
        status, content = self.request("/peers")
        legacy_peers = json.loads(content)
        assert status == 200 and len(legacy_peers) == 1
        assert legacy_peers[0]["public_key"] == self.active_key
        assert legacy_peers[0]["allowed_ips"] == active["allowed_ips"]
        self.configure_client(reconnect=True)
        self.stop()
        # Re-upgrading that checkpoint is also a supported recovery operation.
        self.start(self.candidate, self.restored)
        remigrated = self.peers()
        assert len(remigrated) == 1 and remigrated[0]["public_key"] == self.active_key
        assert remigrated[0]["address"] + "/32" == active["allowed_ips"][0]
        assert (
            json.loads(self.request("/v1/server")[1])["public_key"] == self.server_key
        )
        self.configure_client(reconnect=True)
        print(
            "Pre-upgrade backup rollback and re-upgrade: "
            "identity and connectivity passed"
        )

    def cleanup(self) -> None:
        for args in (
            (
                "rm",
                "--force",
                "--volumes",
                self.server,
                self.client,
                self.target,
                f"{self.name}-helper",
            ),
            ("network", "rm", self.transport),
            ("network", "rm", self.egress),
            (
                "volume",
                "rm",
                self.original,
                self.restored,
                self.recovery,
                self.archives,
            ),
        ):
            try:
                docker(*args, timeout=15)
            except (DockerCommandError, OSError):
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "candidate", help="Candidate image already built/pulled locally"
    )
    parser.add_argument(
        "previous", help="Native v0.4.2 release image or source rebuild"
    )
    args = parser.parse_args()
    smoke = LifecycleSmoke(args.candidate, args.previous)
    try:
        smoke.run()
    except Exception:
        # Only the stage enters diagnostics; no legacy logs/commands may expose keys.
        print(f"Lifecycle smoke failed during: {smoke.stage}", flush=True)
        raise
    finally:
        smoke.cleanup()


if __name__ == "__main__":
    main()
