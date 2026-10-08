"""Exercise a built image with real WireGuard and disposable Docker storage."""

import base64
import json
import subprocess
import sys
import time
import uuid
from urllib.parse import quote


def docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True).rstrip("\n")


def smoke_test(image: str) -> None:
    name = f"wireguard-api-smoke-{uuid.uuid4().hex[:12]}"
    volume = f"{name}-config"
    token = uuid.uuid4().hex

    def request(
        path: str,
        method: str = "GET",
        body: str | None = None,
        auth: str | None = token,
    ) -> tuple[int, str]:
        args = [
            "exec",
            name,
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "5",
            "--write-out",
            "\n%{http_code}",
            "--request",
            method,
        ]
        if auth is not None:
            args.extend(["--header", f"X-API-Token: {auth}"])
        if body is not None:
            args.extend(["--header", "Content-Type: application/json", "--data", body])
        args.append(f"http://127.0.0.1:8008{path}")
        content, status = docker(*args).rsplit("\n", 1)
        return int(status), content

    def wait_ready() -> None:
        for _ in range(60):
            try:
                status, content = request("/health", auth=None)
                if status == 200 and json.loads(content)["wireguard_available"]:
                    # Verify the interface independently of the health endpoint.
                    docker("exec", name, "wg", "show", "wg0")
                    return
            except (subprocess.CalledProcessError, ValueError):
                pass
            time.sleep(1)
        raise RuntimeError("Container did not become ready within 60 seconds")

    try:
        platform = docker(
            "image", "inspect", image, "--format", "{{.Os}}/{{.Architecture}}"
        )
        docker("volume", "create", volume)
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--platform",
            platform,
            "--cap-add",
            "NET_ADMIN",
            "--sysctl",
            "net.ipv4.conf.all.src_valid_mark=1",
            "--sysctl",
            "net.ipv4.ip_forward=1",
            "--env",
            f"API_TOKEN={token}",
            "--env",
            "SERVER_ENDPOINT=vpn.example.com:51820",
            "--mount",
            f"type=volume,source={volume},target=/config",
            image,
        )
        wait_ready()
        docker(
            "exec",
            name,
            "python",
            "-c",
            "import sys; assert sys.version_info >= (3, 14); "
            "import api, health, metrics, wireguard; print(sys.version)",
        )
        assert request("/peers", auth=None)[0] == 401
        assert request("/peers", auth="invalid")[0] == 403
        assert request("/peers") == (200, "[]")

        status, content = request("/peers", "POST", "{}")
        assert status == 201, content
        peer = json.loads(content)
        public_key = peer["public_key"]
        assert peer["private_key"]
        assert peer["allowed_ips"] == ["10.13.13.2/32"]
        # Avoid path-separator ambiguity for base64 public keys.
        status, content = request("/peers")
        assert status == 200 and json.loads(content)[0]["public_key"] == public_key
        assert public_key in docker("exec", name, "wg", "show", "wg0", "peers")

        status, content = request("/peers?format=config", "POST", "{}")
        assert status == 201 and "[Interface]" in content and "[Peer]" in content
        assert "Address = 10.13.13.3/32" in content

        status, content = request("/metrics", auth=None)
        assert status == 200 and "wireguard_peers_total 2.0" in content
        assert json.loads(request("/health", auth=None)[1])["peer_count"] == 2

        docker("restart", name)
        wait_ready()
        status, content = request("/peers")
        restored = json.loads(content)
        assert status == 200 and len(restored) == 2
        assert public_key in {item["public_key"] for item in restored}
        assert public_key in docker("exec", name, "wg", "show", "wg0", "peers")

        # Use a deterministic, URL-safe public key for path-based operations.
        path_key = base64.b64encode(bytes(range(32))).decode()
        status, content = request(
            "/peers", "POST", json.dumps({"public_key": path_key})
        )
        assert status == 201, content
        path = f"/peers/{quote(path_key, safe='')}"
        assert request(path)[0] == 200
        assert request(f"{path}/config")[0] == 200
        assert request(path, "DELETE")[0] == 204
        assert request(path)[0] == 404
        assert (
            path_key
            not in docker("exec", name, "wg", "show", "wg0", "peers").splitlines()
        )
        print(f"Container smoke checks passed: {image}")
    except Exception:
        subprocess.run(["docker", "logs", name], check=False)
        raise
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=False)
        subprocess.run(["docker", "volume", "rm", volume], check=False)


if __name__ == "__main__":
    smoke_test(sys.argv[1] if len(sys.argv) > 1 else "wireguard-api:smoke")
