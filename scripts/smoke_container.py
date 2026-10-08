"""Exercise the API, persistence, real handshake, and IPv4 NAT with Docker."""

import base64
import json
import subprocess
import sys
import time
import uuid


def docker(*args: str, input_text: str | None = None, timeout: float = 30) -> str:
    return subprocess.run(
        ["docker", *args],
        input=input_text,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    ).stdout.rstrip("\n")


def smoke_test(image: str) -> None:
    name = f"wireguard-api-smoke-{uuid.uuid4().hex[:12]}"
    volume, network = f"{name}-config", f"{name}-net"
    client, target = f"{name}-client", f"{name}-target"
    token = uuid.uuid4().hex
    deadline = time.monotonic() + 300

    def command(*args: str, input_text: str | None = None) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Smoke test exceeded its 300-second deadline")
        return docker(*args, input_text=input_text, timeout=min(30, remaining))

    def request(
        path: str,
        method: str = "GET",
        body: dict | None = None,
        auth: str | None = token,
    ) -> tuple[int, str]:
        args = [
            "exec",
            name,
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "3",
            "--write-out",
            "\n%{http_code}",
            "--request",
            method,
        ]
        if auth is not None:
            args.extend(["--header", f"X-API-Token: {auth}"])
        if body is not None:
            args.extend(
                [
                    "--header",
                    f"Idempotency-Key: {uuid.uuid4().hex}",
                    "--header",
                    "Content-Type: application/json",
                    "--data",
                    json.dumps(body),
                ]
            )
        content, status = command(*args, f"http://127.0.0.1:8008{path}").rsplit("\n", 1)
        return int(status), content

    def wait_for(predicate, description: str, seconds: float = 60) -> None:
        end = min(deadline, time.monotonic() + seconds)
        while time.monotonic() < end:
            try:
                if predicate():
                    return
            except (subprocess.CalledProcessError, ValueError):
                pass
            time.sleep(0.25)
        raise RuntimeError(f"Timed out waiting for {description}")

    def wait_ready() -> None:
        wait_for(lambda: request("/readyz", auth=None)[0] == 200, "readiness")
        assert request("/livez", auth=None)[0] == 200
        command("exec", name, "wg", "show", "wgtest")

    def peer_keys() -> set[str]:
        return set(command("exec", name, "wg", "show", "wgtest", "peers").splitlines())

    def create(body: dict) -> dict:
        status, content = request("/v1/peers", "POST", body)
        assert status in (201, 202), f"Create returned HTTP {status}"
        result = json.loads(content)
        assert result["operation"]["id"] and result["operation"]["status"]
        wait_for(
            lambda: result["peer"]["public_key"] in peer_keys(), "peer application"
        )
        return result

    def container_ip(container: str) -> str:
        return command(
            "inspect",
            container,
            "--format",
            f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}',
        )

    def start_server() -> None:
        command(
            "run",
            "--detach",
            "--name",
            name,
            "--platform",
            platform,
            "--network",
            network,
            "--cap-add",
            "NET_ADMIN",
            "--sysctl",
            "net.ipv4.conf.all.src_valid_mark=1",
            "--sysctl",
            "net.ipv4.ip_forward=1",
            "--env",
            f"API_TOKEN={token}",
            "--env",
            "SERVER_ENDPOINT=node.example.org:51820",
            "--env",
            "WG_INTERFACE=wgtest",
            "--env",
            "WG_SERVER_ADDRESS=10.77.0.1/24",
            "--env",
            "WG_RECONCILE_INTERVAL=0.25",
            "--env",
            "WG_SNAPSHOT_TTL=0.1",
            "--mount",
            f"type=volume,source={volume},target=/config",
            image,
        )
        wait_ready()

    try:
        platform = command(
            "image", "inspect", image, "--format", "{{.Os}}/{{.Architecture}}"
        )
        command("volume", "create", volume)
        command("network", "create", network)
        start_server()
        command(
            "exec",
            name,
            "python",
            "-c",
            "import sys; assert sys.version_info >= (3, 14); "
            "import api, routes, health, metrics, wireguard, storage, settings, "
            "models, service, errors, keys, configuration, version, bootstrap",
        )
        assert request("/v1/peers", auth=None)[0] == 401
        assert request("/v1/peers", auth="invalid")[0] == 403
        assert json.loads(request("/v1/peers")[1]) == {"items": [], "next_cursor": None}
        server_key = json.loads(request("/v1/server")[1])["public_key"]
        assert server_key == command("exec", name, "wg", "show", "wgtest", "public-key")

        # Exercise generated slash-containing public keys too, with a bounded
        # search. Deleted candidates are fully reconciled before address reuse.
        for _ in range(32):
            generated = create({"key_mode": "generated"})
            candidate = generated["peer"]
            if "/" in candidate["public_key"]:
                break
            candidate_path = f"/v1/peers/{candidate['id']}"
            assert request(candidate_path, "DELETE")[0] in (202, 204)
            wait_for(
                lambda candidate=candidate, candidate_path=candidate_path: (
                    candidate["public_key"] not in peer_keys()
                    and request(candidate_path)[0] == 404
                ),
                "generated candidate deletion",
            )
        else:
            raise RuntimeError(
                "No generated slash-containing public key in 32 attempts"
            )
        peer = generated["peer"]
        public_key, private_key = peer["public_key"], generated["private_key"]
        for key in (public_key, private_key, server_key):
            assert (
                base64.b64encode(base64.b64decode(key, validate=True)).decode() == key
            )
            assert len(base64.b64decode(key)) == 32
        assert peer["address"] == "10.77.0.2"
        assert (
            generated["client_config"] and "[Interface]" in generated["client_config"]
        )
        path = f"/v1/peers/{peer['id']}"
        assert json.loads(request(path)[1])["public_key"] == public_key
        template = json.loads(request(f"{path}/config-template")[1])["config"]
        assert "[Interface]" in template and "[Peer]" in template

        # A canonical deterministic slash-containing key must work via stable IDs.
        slash_key = base64.b64encode(bytes([255]) * 32).decode()
        external = create(
            {"key_mode": "external", "public_key": slash_key, "address": "10.77.0.9"}
        )
        assert external["private_key"] is None and external["client_config"] is None
        external_path = f"/v1/peers/{external['peer']['id']}"
        assert json.loads(request(external_path)[1])["public_key"] == slash_key
        assert request(f"{external_path}/config-template")[0] == 200
        status, content = request("/v1/peers")
        assert status == 200 and len(json.loads(content)["items"]) == 2

        def metrics_ready() -> bool:
            status, content = request("/metrics", auth=None)
            return (
                status == 200
                and "wireguard_peers_total 2.0" in content
                and "wireguard_available 1.0" in content
            )

        wait_for(metrics_ready, "fresh available peer metrics")

        # Both helpers use the same tested image but bypass /init/bootstrap.
        command(
            "run",
            "--detach",
            "--name",
            target,
            "--platform",
            platform,
            "--network",
            network,
            "--entrypoint",
            "python",
            image,
            "-u",
            "-c",
            "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            " def do_GET(self):\n"
            "  self.send_response(200); self.end_headers(); "
            "self.wfile.write(self.client_address[0].encode())\n"
            "HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()",
        )
        command(
            "run",
            "--detach",
            "--name",
            client,
            "--platform",
            platform,
            "--network",
            network,
            "--cap-add",
            "NET_ADMIN",
            "--entrypoint",
            "sleep",
            image,
            "infinity",
        )
        server_ip, target_ip = container_ip(name), container_ip(target)
        command("exec", client, "ip", "link", "add", "dev", "wgc", "type", "wireguard")
        command("exec", client, "ip", "address", "add", "10.77.0.2/32", "dev", "wgc")
        command(
            "exec",
            "-i",
            client,
            "wg",
            "setconf",
            "wgc",
            "/dev/stdin",
            input_text=f"[Interface]\nPrivateKey = {private_key}\n"
            f"[Peer]\nPublicKey = {server_key}\nEndpoint = {server_ip}:51820\n"
            "AllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 1\n",
        )
        command("exec", client, "ip", "link", "set", "up", "dev", "wgc")
        command("exec", client, "ip", "route", "add", f"{target_ip}/32", "dev", "wgc")
        command("exec", client, "ip", "route", "add", "10.77.0.1/32", "dev", "wgc")
        # Require bootstrap's scoped FORWARD rules to permit both directions.
        command("exec", name, "iptables", "-P", "FORWARD", "DROP")

        def nat_traffic() -> bool:
            source = command(
                "exec",
                client,
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "3",
                f"http://{target_ip}:8080/",
            )
            return source == server_ip

        wait_for(nat_traffic, "tunnel HTTP traffic and subnet-scoped NAT", 30)
        handshakes = command("exec", name, "wg", "show", "wgtest", "latest-handshakes")
        assert any(
            line.split()[0] == public_key and int(line.split()[1]) > 0
            for line in handshakes.splitlines()
        )
        command("exec", client, "ping", "-c", "1", "-W", "3", "10.77.0.1")

        assert request(external_path, "DELETE")[0] in (202, 204)
        wait_for(lambda: slash_key not in peer_keys(), "peer removal")
        # Recreate, rather than merely restarting: namespace disappears, volume stays.
        command("rm", "--force", name)
        start_server()
        assert json.loads(request("/v1/server")[1])["public_key"] == server_key
        status, content = request("/v1/peers")
        restored = json.loads(content)["items"]
        assert status == 200 and {item["public_key"] for item in restored} == {
            public_key
        }
        wait_for(lambda: public_key in peer_keys(), "restored peer application")
        assert slash_key not in peer_keys()
        assert request(external_path)[0] == 404
        assert json.loads(request(path)[1])["public_key"] == public_key
        print(
            "API, recreate/persistence, handshake, tunnel traffic and NAT passed: "
            f"{image}"
        )
    except Exception:
        try:
            print(docker("logs", name, timeout=10), file=sys.stderr)
        except (subprocess.SubprocessError, OSError):
            pass
        raise
    finally:
        for args in (
            ("rm", "--force", "--volumes", name, client, target),
            ("network", "rm", network),
            ("volume", "rm", volume),
        ):
            try:
                docker(*args, timeout=15)
            except (subprocess.SubprocessError, OSError):
                pass


if __name__ == "__main__":
    smoke_test(sys.argv[1] if len(sys.argv) > 1 else "wireguard-api:smoke")
