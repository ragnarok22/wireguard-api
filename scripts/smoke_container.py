"""Exercise API configs, DNS, IPv4 NAT, persistence, and active revocation."""

import base64
import json
import subprocess
import sys
import time
import uuid

TARGET_SCRIPT = """
import socket
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class DNSHandler(socketserver.BaseRequestHandler):
    def handle(self):
        query, connection = self.request
        name = b'\\x06egress\\x05smoke\\x04test\\x00'
        question = query[12:12 + len(name) + 4]
        if question[:-4] != name or question[-2:] != b'\\x00\\x01':
            return
        address = socket.inet_aton(socket.gethostbyname(socket.gethostname()))
        is_ipv4 = question[-4:-2] == b'\\x00\\x01'
        header = query[:2] + struct.pack('!HHHHH', 0x8180, 1, int(is_ipv4), 0, 0)
        answer = b'\\xc0\\x0c' + struct.pack('!HHIH', 1, 1, 0, 4) + address
        if not is_ipv4:
            answer = b''
        connection.sendto(header + question + answer, self.client_address)

class HTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.client_address[0].encode())

dns = socketserver.UDPServer(('0.0.0.0', 53), DNSHandler)
threading.Thread(target=dns.serve_forever, daemon=True).start()
HTTPServer(('0.0.0.0', 8080), HTTPHandler).serve_forever()
"""

TRAFFIC_SCRIPT = """
import json
import os
import sys
import time
import urllib.request

state = {'attempts': 0, 'successes': 0, 'consecutive_failures': 0, 'source': None}
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
while True:
    try:
        with opener.open(sys.argv[1], timeout=2) as response:
            state['source'] = response.read().decode()
        state['successes'] += 1
        state['consecutive_failures'] = 0
    except (OSError, urllib.error.URLError):
        state['consecutive_failures'] += 1
    state['attempts'] += 1
    with open('/tmp/traffic.next', 'w') as output:
        json.dump(state, output)
    os.replace('/tmp/traffic.next', '/tmp/traffic.json')
    time.sleep(0.1)
"""


class DockerCommandError(RuntimeError):
    """Safe diagnostics without command arguments, credentials, or output."""


def docker(*args: str, input_text: str | None = None, timeout: float = 30) -> str:
    try:
        return subprocess.run(
            ["docker", *args],
            input=input_text,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        ).stdout.rstrip("\n")
    except subprocess.CalledProcessError as exc:
        raise DockerCommandError(
            f"Docker {args[0]} failed with exit code {exc.returncode}"
        ) from None
    except subprocess.TimeoutExpired:
        raise DockerCommandError(f"Docker {args[0]} timed out") from None


def smoke_test(image: str) -> None:
    name = f"wireguard-api-smoke-{uuid.uuid4().hex[:12]}"
    volume, network = f"{name}-config", f"{name}-net"
    egress_network = f"{name}-egress"
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
            except (DockerCommandError, ValueError):
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

    def container_ip(container: str, attached_network: str = network) -> str:
        return command(
            "inspect",
            container,
            "--format",
            f'{{{{(index .NetworkSettings.Networks "{attached_network}").IPAddress}}}}',
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
            egress_network,
            "--cap-add",
            "NET_ADMIN",
            "--cpus",
            "0.5",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--sysctl",
            "net.ipv4.conf.all.src_valid_mark=1",
            "--sysctl",
            "net.ipv4.ip_forward=1",
            "--env",
            f"API_TOKEN={token}",
            "--env",
            "SERVER_ENDPOINT=node.smoke.test:51820",
            "--env",
            f"CLIENT_DNS={target_ip}",
            "--env",
            "WG_EGRESS_INTERFACE=eth0",
            "--env",
            "WG_INTERFACE=wgtest",
            "--env",
            "WG_SERVER_ADDRESS=10.77.0.1/24",
            "--env",
            "WG_RECONCILE_INTERVAL=0.25",
            "--env",
            "WG_SNAPSHOT_TTL=0.1",
            "--env",
            "WG_TELEMETRY_INTERVAL=0.25",
            "--mount",
            f"type=volume,source={volume},target=/config",
            image,
        )
        command("network", "connect", "--alias", "node.smoke.test", network, name)
        wait_ready()

    try:
        platform = command(
            "image", "inspect", image, "--format", "{{.Os}}/{{.Architecture}}"
        )
        command("volume", "create", volume)
        command("network", "create", network)
        command("network", "create", egress_network)
        # Controlled DNS/HTTP egress is reachable from the server's eth0 only.
        command(
            "run",
            "--detach",
            "--name",
            target,
            "--platform",
            platform,
            "--network",
            egress_network,
            "--entrypoint",
            "python",
            image,
            "-u",
            "-c",
            TARGET_SCRIPT,
        )
        target_ip = container_ip(target, egress_network)
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
        for path in ("/v1/stats", "/v1/system"):
            assert request(path, auth=None)[0] == 401
            assert request(path, auth="invalid")[0] == 403

        def resources_ready() -> bool:
            status, content = request("/v1/system")
            if status != 200:
                return False
            info = json.loads(content)
            return info["cpu"]["status"] == "available"

        wait_for(resources_ready, "container resource sampling")
        info = json.loads(request("/v1/system")[1])
        assert (
            info["status"] == "available" and info["resource_scope"] == "current_cgroup"
        )
        assert info["cpu"]["capacity_cores"] == 0.5
        assert info["cpu"]["total_usage_seconds"] > 0
        assert info["cpu"]["usage_percent"] >= 0
        assert info["memory"]["limit_bytes"] == 256 * 1024 * 1024
        assert info["memory"]["capacity_bytes"] == 256 * 1024 * 1024
        assert 0 < info["memory"]["used_bytes"] < info["memory"]["limit_bytes"]
        assert info["disk"]["scope"] == "data_filesystem"
        assert info["sample"]["age_seconds"] < 3
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

        def vpn_stats_ready() -> bool:
            status, content = request("/v1/stats")
            return status == 200 and json.loads(content)["peers"]["applied"] == 2

        wait_for(vpn_stats_ready, "VPN summary sampling")
        stats = json.loads(request("/v1/stats")[1])
        assert stats["peers"]["registered"] == 2
        assert stats["pool"]["reserved"] == 2
        assert stats["pending_operations"] == 0

        # Helpers use the tested image but bypass /init/bootstrap.
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
            "--sysctl",
            "net.ipv4.conf.all.src_valid_mark=1",
            "--entrypoint",
            "sleep",
            image,
            "infinity",
        )
        # Register Docker's resolver before wg-quick takes DNS ownership.
        docker_resolver = command("exec", client, "cat", "/etc/resolv.conf")
        command("exec", client, "resolvconf", "-u")
        command(
            "exec", "-i", client, "resolvconf", "-a", "eth0", input_text=docker_resolver
        )

        def configure_client() -> None:
            # Apply the exact API response, including address, DNS and full routing.
            command(
                "exec",
                "-i",
                client,
                "sh",
                "-c",
                "umask 077; cat > /tmp/wgc.conf",
                input_text=generated["client_config"],
            )
            try:
                command(
                    "exec",
                    client,
                    "sh",
                    "-c",
                    "wg-quick up /tmp/wgc.conf > /tmp/wg-quick.log 2>&1",
                )
            except DockerCommandError:
                details = command("exec", client, "cat", "/tmp/wg-quick.log")
                for secret in (private_key, public_key, server_key, token):
                    details = details.replace(secret, "<redacted>")
                print(f"Client setup failed:\n{details}", file=sys.stderr)
                raise
            route = command("exec", client, "ip", "route", "get", target_ip)
            assert "dev wgc" in route, "Egress must use the API's full-tunnel route"
            resolver = command("exec", client, "cat", "/etc/resolv.conf")
            assert f"nameserver {target_ip}" in resolver, "API DNS was not applied"

        configure_client()
        server_ip = container_ip(name, egress_network)
        # Require bootstrap's scoped FORWARD rules to permit both directions.
        command("exec", name, "iptables", "-P", "FORWARD", "DROP")

        def http_source(container: str, host: str) -> str:
            return command(
                "exec",
                container,
                "curl",
                "--noproxy",
                "*",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "3",
                f"http://{host}:8080/",
            )

        def nat_traffic() -> bool:
            return http_source(client, target_ip) == server_ip

        def dns_traffic() -> bool:
            resolved = command(
                "exec",
                client,
                "python",
                "-c",
                "import socket; print(socket.gethostbyname('egress.smoke.test'))",
            )
            return (
                resolved == target_ip
                and http_source(client, "egress.smoke.test") == server_ip
            )

        wait_for(nat_traffic, "tunnel HTTP traffic and subnet-scoped NAT", 30)
        wait_for(dns_traffic, "API-configured DNS and HTTP by resolved hostname", 30)

        command(
            "exec",
            "--detach",
            client,
            "python",
            "-u",
            "-c",
            TRAFFIC_SCRIPT,
            f"http://{target_ip}:8080/",
        )

        def traffic_state() -> dict:
            return json.loads(command("exec", client, "cat", "/tmp/traffic.json"))

        wait_for(
            lambda: (
                traffic_state()["successes"] >= 3
                and traffic_state()["source"] == server_ip
            ),
            "continuous NAT traffic",
            15,
        )

        def traffic_rates_ready() -> bool:
            assert nat_traffic()
            status, content = request("/v1/stats")
            if status != 200:
                return False
            info = json.loads(content)
            rates = info["traffic"]
            return (
                info["handshakes"]["recent"] >= 1
                and rates["rx_bytes_per_second"] is not None
                and rates["rx_bytes_per_second"] > 0
                and rates["tx_bytes_per_second"] > 0
            )

        wait_for(traffic_rates_ready, "measured tunnel traffic rates", 15)
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
        server_ip = container_ip(name, egress_network)
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

        # Resolve the recreated server's endpoint and reuse the original API config.
        command("exec", client, "wg-quick", "down", "/tmp/wgc.conf")
        configure_client()
        command("exec", name, "iptables", "-P", "FORWARD", "DROP")
        wait_for(nat_traffic, "restored peer tunnel HTTP/NAT", 30)
        wait_for(dns_traffic, "restored peer DNS", 30)
        before = traffic_state()
        wait_for(
            lambda: (
                traffic_state()["successes"] >= before["successes"] + 3
                and traffic_state()["consecutive_failures"] == 0
                and traffic_state()["source"] == server_ip
            ),
            "active traffic immediately before revocation",
            15,
        )
        assert request(path, "DELETE")[0] in (202, 204)
        wait_for(
            lambda: public_key not in peer_keys() and request(path)[0] == 404,
            "active peer revocation",
        )
        wait_for(
            lambda: traffic_state()["consecutive_failures"] >= 3,
            "revoked client's continuous HTTP traffic to fail",
            20,
        )
        revoked = traffic_state()
        wait_for(
            lambda: traffic_state()["attempts"] >= revoked["attempts"] + 3,
            "continued traffic attempts after revocation",
            20,
        )
        assert traffic_state()["successes"] == revoked["successes"], (
            "Revoked client regained HTTP access"
        )
        assert "dev wgc" in command("exec", client, "ip", "route", "get", target_ip)
        assert http_source(name, target_ip) == server_ip, "HTTP target must remain live"
        print(
            "API, limited-container resources, VPN rates, recreation, "
            "API client config, handshake, DNS, tunnel NAT and "
            "active revocation passed: "
            f"{image}"
        )
    except Exception:
        try:
            print(docker("logs", name, timeout=10), file=sys.stderr)
        except (DockerCommandError, OSError):
            pass
        raise
    finally:
        for args in (
            ("rm", "--force", "--volumes", name, client, target),
            ("network", "rm", network),
            ("network", "rm", egress_network),
            ("volume", "rm", volume),
        ):
            try:
                docker(*args, timeout=15)
            except (DockerCommandError, OSError):
                pass


if __name__ == "__main__":
    smoke_test(sys.argv[1] if len(sys.argv) > 1 else "wireguard-api:smoke")
