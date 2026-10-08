"""Real container startup regressions for authentication, keys and bootstrap."""

import json
import sys
import time
import uuid
from collections.abc import Callable

from scripts.smoke_container import DockerCommandError, docker

FAILURE_MARKER = "bootstrap-command-secret-marker"

# Test-only wrappers run in place of commands, with original executables copied
# before installation. Successful commands still operate on the real kernel.
PREPARE_WRAPPERS = r"""
import os
import shutil
from pathlib import Path

root = Path('/source')
for directory in ('bin', 'real'):
    (root / directory).mkdir()
wrapper = '''#!/app/.venv/bin/python
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
command = [name, *sys.argv[1:]]
prefix = json.loads(os.environ['WG_SMOKE_FAIL'])
if command[:len(prefix)] == prefix:
    sys.stderr.write('bootstrap-command-secret-marker\\n')
    if name == 'wg' and 'set' in command:
        sys.stderr.write(sys.stdin.read())
    sys.exit(2)
os.execv('/smoke/real/' + name, [name, *sys.argv[1:]])
'''
for name in ('wg', 'ip', 'sysctl', 'iptables'):
    shutil.copyfile(shutil.which(name), root / 'real' / name)
    (root / 'real' / name).chmod(0o700)
    path = root / 'bin' / name
    path.write_text(wrapper)
    path.chmod(0o700)
"""

KEY_CHECK = """
import os
import stat
from pathlib import Path

path = Path('/config/server_private.key')
if path.exists():
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode), 'Key must be a regular file'
    assert stat.S_IMODE(info.st_mode) == 0o600, 'Key permissions must be 0600'
    assert info.st_uid == os.geteuid(), 'Key owner differs from bootstrap owner'
    assert info.st_gid == os.getegid(), 'Key group differs from bootstrap group'
assert not list(Path('/config').glob('.bootstrap-*')), 'Leaked publication file'
assert not Path('/tmp/server.key').exists(), 'Leaked legacy temporary key'
"""


class BootstrapSmoke:
    def __init__(self, image: str) -> None:
        self.image = image
        self.name = f"wireguard-api-bootstrap-{uuid.uuid4().hex[:12]}"
        self.server = f"{self.name}-server"
        self.helper_name = f"{self.name}-helper"
        self.wrappers = f"{self.name}-wrappers"
        self.volumes: list[str] = []
        self.token = uuid.uuid4().hex
        self.deadline = time.monotonic() + 300

    def command(self, *args: str, input_text: str | None = None) -> str:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Bootstrap smoke exceeded its 300-second deadline")
        return docker(*args, input_text=input_text, timeout=min(30, remaining))

    def wait_for(self, predicate: Callable[[], bool], description: str) -> None:
        end = min(self.deadline, time.monotonic() + 30)
        while time.monotonic() < end:
            try:
                if predicate():
                    return
            except (DockerCommandError, ValueError):
                pass
            time.sleep(0.25)
        raise RuntimeError(f"Timed out waiting for {description}")

    def volume(self, suffix: str) -> str:
        name = f"{self.name}-{suffix}"
        self.command("volume", "create", name)
        self.volumes.append(name)
        return name

    def helper(self, volume: str, script: str) -> str:
        return self.command(
            "run",
            "--rm",
            "--name",
            self.helper_name,
            "--platform",
            self.platform,
            "--network",
            "none",
            "--mount",
            f"type=volume,source={volume},target=/source",
            "--entrypoint",
            "python",
            self.image,
            "-c",
            script,
        )

    def start(
        self,
        volume: str,
        *,
        token: str | None,
        failure: list[str] | None = None,
        endpoint: str | None = "node.smoke.test:51820",
    ) -> None:
        args = [
            "run",
            "--detach",
            "--name",
            self.server,
            "--platform",
            self.platform,
            "--cap-add",
            "NET_ADMIN",
            "--sysctl",
            "net.ipv4.ip_forward=" + ("0" if failure == ["sysctl", "-w"] else "1"),
            "--mount",
            f"type=volume,source={volume},target=/config",
        ]
        if token is not None:
            args.extend(["--env", f"API_TOKEN={token}"])
        if endpoint is not None:
            args.extend(["--env", f"SERVER_ENDPOINT={endpoint}"])
        if failure is not None:
            args.extend(
                [
                    "--mount",
                    f"type=volume,source={self.wrappers},target=/smoke,readonly",
                    "--env",
                    "PATH=/smoke/bin:/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "--env",
                    f"WG_SMOKE_FAIL={json.dumps(failure)}",
                ]
            )
        self.command(*args, self.image)

    def request(self, path: str, token: str | None = None) -> int:
        args = [
            "exec",
            self.server,
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "2",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
        ]
        if token is not None:
            args.extend(["--header", f"X-API-Token: {token}"])
        return int(self.command(*args, f"http://127.0.0.1:8008{path}"))

    def ready(self) -> None:
        self.wait_for(lambda: self.request("/readyz") == 200, "default readiness")
        for path in ("/livez", "/metrics"):
            assert self.request(path) == 200, f"Public probe failed: {path}"
        assert self.request("/v1/peers") == 401
        assert self.request("/v1/peers", "invalid") == 403
        assert self.request("/v1/peers", self.token) == 200
        self.command("exec", self.server, "python", "-c", KEY_CHECK)

    def remove(self) -> None:
        self.command("rm", "--force", self.server)

    def blocked(self, reason: str) -> None:
        self.wait_for(
            lambda: reason in self.command("logs", self.server), "bootstrap rejection"
        )
        logs = self.command("logs", self.server)
        assert self.token not in logs, "Startup logs disclosed authentication token"
        assert "default_token_change_me" not in logs, "Rejected token was logged"
        assert FAILURE_MARKER not in logs, "Bootstrap exposed subprocess stderr"
        # s6 may retry the service while the container remains running. Require
        # no HTTP listener, rather than accepting merely a non-ready response.
        for _ in range(2):
            for path in ("/livez", "/readyz", "/v1/peers"):
                try:
                    self.request(path, self.token)
                except DockerCommandError:
                    continue
                raise AssertionError("API accepted HTTP after failed bootstrap")
            time.sleep(0.5)
        self.command("exec", self.server, "python", "-c", KEY_CHECK)
        # Compare private material internally; never return it to host diagnostics.
        self.command(
            "exec",
            "-i",
            self.server,
            "python",
            "-c",
            "import sys; from pathlib import Path; "
            "p=Path('/config/server_private.key'); logs=sys.stdin.read(); "
            "assert not p.exists() or p.read_text().strip() not in logs",
            input_text=logs,
        )

    def run(self) -> None:
        try:
            self.platform = self.command(
                "image", "inspect", self.image, "--format", "{{.Os}}/{{.Architecture}}"
            )
            wrappers = self.volume("wrappers")
            self.helper(wrappers, PREPARE_WRAPPERS)
            for index, token in enumerate((None, "", " \t", "default_token_change_me")):
                volume = self.volume(f"auth-{index}")
                self.start(volume, token=token)
                self.blocked("api_token")
                self.helper(
                    volume,
                    "from pathlib import Path; "
                    "assert not (Path('/source') / 'server_private.key').exists()",
                )
                self.remove()
            volume = self.volume("missing-endpoint")
            self.start(volume, token=self.token, endpoint=None)
            self.blocked("server_endpoint")
            self.remove()

            volume = self.volume("default")
            self.start(volume, token=self.token)
            self.ready()
            public = self.command(
                "exec", self.server, "wg", "show", "wg0", "public-key"
            )
            address = json.loads(
                self.command(
                    "exec", self.server, "ip", "-j", "address", "show", "dev", "wg0"
                )
            )
            assert address[0]["addr_info"][0]["local"] == "10.13.13.1"
            assert address[0]["addr_info"][0]["prefixlen"] == 24
            # Repeat on an existing interface; lost rules are repaired exactly once.
            self.command(
                "exec", self.server, "iptables", "-t", "nat", "-F", "POSTROUTING"
            )
            self.command("exec", self.server, "iptables", "-F", "FORWARD")
            bootstrap = (
                "from bootstrap import bootstrap; from settings import Settings; "
                "bootstrap(Settings.load())"
            )
            for _ in range(2):
                self.command("exec", self.server, "python", "-c", bootstrap)
            nat = self.command(
                "exec", self.server, "iptables", "-t", "nat", "-S", "POSTROUTING"
            )
            forward = self.command("exec", self.server, "iptables", "-S", "FORWARD")
            assert nat.count("-A POSTROUTING") == 1
            assert "-s 10.13.13.0/24 -o eth0 -j MASQUERADE" in nat
            assert forward.count("-A FORWARD") == 2
            self.ready()
            self.command("restart", self.server)
            self.ready()
            assert (
                self.command("exec", self.server, "wg", "show", "wg0", "public-key")
                == public
            )
            self.remove()
            self.helper(
                volume,
                "from pathlib import Path; "
                "(Path('/source') / 'server_private.key').chmod(0o644)",
            )
            self.start(volume, token=self.token)
            self.ready()
            assert (
                self.command("exec", self.server, "wg", "show", "wg0", "public-key")
                == public
            )
            self.remove()

            failures = [
                ["wg", "genkey"],
                ["wg", "pubkey"],
                ["ip", "link", "add"],
                ["ip", "address", "add"],
                ["wg", "set"],
                ["ip", "link", "set"],
                ["sysctl", "-n"],
                ["sysctl", "-w"],
                ["iptables", "-w", "5", "-t", "nat", "-C"],
                ["iptables", "-w", "5", "-t", "nat", "-A"],
                ["iptables", "-w", "5", "-t", "filter", "-A"],
            ]
            for index, failure in enumerate(failures):
                volume = self.volume(f"failure-{index}")
                self.start(volume, token=self.token, failure=failure)
                self.blocked("Bootstrap command failed")
                self.remove()
                self.start(volume, token=self.token)
                self.ready()
                self.remove()
            print(
                "Authentication, default bootstrap, key permissions, restart, "
                f"repair and command failures passed: {self.image}"
            )
        finally:
            for args in [
                ("rm", "--force", self.server, self.helper_name),
                *(("volume", "rm", volume) for volume in reversed(self.volumes)),
            ]:
                try:
                    docker(*args, timeout=15)
                except (DockerCommandError, OSError):
                    pass


if __name__ == "__main__":
    BootstrapSmoke(sys.argv[1] if len(sys.argv) > 1 else "wireguard-api:smoke").run()
