"""The sole owner of container interface, server identity, and IPv4 forwarding."""

import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from errors import ControlPlaneError, WireGuardError
from keys import validate_key

if TYPE_CHECKING:
    from settings import Settings


class _Commands:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    def run(self, args: list[str], input_text: str | None = None) -> str:
        try:
            return subprocess.run(
                args,
                input=input_text,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
            ).stdout
        except subprocess.CalledProcessError as error:
            # Only iptables -C's documented "rule absent" exit is recoverable.
            if args[0] == "iptables" and "-C" in args and error.returncode == 1:
                return "absent"
            raise WireGuardError("Bootstrap command failed") from None
        except OSError, subprocess.TimeoutExpired:
            raise WireGuardError("Bootstrap command failed") from None

    def json(self, args: list[str]) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.run(args))
            if not isinstance(value, list) or any(
                not isinstance(item, dict) for item in value
            ):
                raise ValueError
            return value
        except ValueError, TypeError:
            raise WireGuardError("Invalid bootstrap command response") from None

    def rule(self, table: str, chain: str, args: list[str]) -> None:
        prefix = ["iptables", "-w", "5", "-t", table]
        if self.run([*prefix, "-C", chain, *args]) == "absent":
            self.run([*prefix, "-A", chain, *args])


def _read_private(path: Path) -> str:
    # Do not follow symlinks or permit device/FIFO reads. Repair old permissions
    # through the same descriptor used for reading, without a path-based race.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r", encoding="ascii") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 128:
            raise ValueError("Invalid key file")
        os.fchmod(stream.fileno(), 0o600)
        return validate_key(stream.read(128).strip())


def _publish(path: Path, content: str) -> None:
    # Publish a complete, fsynced 0600 file exclusively. A competing creator
    # wins without replacement; the caller always rereads the winning identity.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="ascii", dir=path.parent, prefix=".bootstrap-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink()


def _identity(settings: Settings, commands: _Commands) -> tuple[str, str]:
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        binding = {
            "interface": settings.interface,
            "server_address": str(settings.server_address),
        }
        identity_file = settings.data_dir / "bootstrap.json"
        if not identity_file.exists():
            _publish(identity_file, json.dumps(binding) + "\n")
        if json.loads(identity_file.read_text(encoding="ascii")) != binding:
            raise ValueError("Bootstrap identity mismatch")
        path = settings.data_dir / "server_private.key"
        try:
            private = _read_private(path)
        except FileNotFoundError:
            private = validate_key(commands.run(["wg", "genkey"]).strip())
            _publish(path, private + "\n")
            private = _read_private(path)
        public = validate_key(commands.run(["wg", "pubkey"], private + "\n").strip())
        return private, public
    except OSError, ValueError:
        raise ControlPlaneError("Invalid or inaccessible bootstrap identity") from None


def _interface_name(value: object, interface: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,14}", value) is None
        or value == interface
    ):
        raise ControlPlaneError("Invalid bootstrap egress interface")
    return value


def bootstrap(settings: Settings) -> None:
    """Ensure the configured identity and network, repairing partial startup.

    The volume pins interface/address in bootstrap.json. Intentional migrations
    require an explicit volume migration rather than silently rekeying or moving
    an existing control plane. No peer keys or subprocess output enter errors.
    """
    commands = _Commands(settings.command_timeout)
    private, public = _identity(settings, commands)
    interface = settings.interface
    links = commands.json(["ip", "-j", "-details", "link", "show"])
    selected = [link for link in links if link.get("ifname") == interface]
    if selected:
        info = selected[0].get("linkinfo")
        if (
            len(selected) != 1
            or not isinstance(info, dict)
            or info.get("info_kind") != "wireguard"
        ):
            raise ControlPlaneError("Bootstrap interface is not WireGuard")
    else:
        commands.run(["ip", "link", "add", "dev", interface, "type", "wireguard"])

    # Inspect every address, including IPv6: the interface is IPv4-exclusive.
    addresses = commands.json(["ip", "-j", "address", "show", "dev", interface])
    expected = settings.server_address
    if len(addresses) != 1 or not isinstance(addresses[0].get("addr_info"), list):
        raise ControlPlaneError("Invalid bootstrap interface addresses")
    actual = addresses[0]["addr_info"]
    if actual:
        if (
            len(actual) != 1
            or not isinstance(actual[0], dict)
            or actual[0].get("family") != "inet"
            or actual[0].get("local") != str(expected.ip)
            or actual[0].get("prefixlen") != expected.network.prefixlen
        ):
            raise ControlPlaneError("Bootstrap interface address mismatch")
    current = commands.run(["wg", "show", interface, "public-key"]).strip()
    if current not in ("(none)", public):
        raise ControlPlaneError("Bootstrap interface key mismatch")

    if settings.egress_interface is None:
        routes = commands.json(["ip", "-j", "route", "get", "1.1.1.1"])
        if len(routes) != 1:
            raise ControlPlaneError("No unambiguous bootstrap egress route")
        egress = _interface_name(routes[0].get("dev"), interface)
    else:
        egress = _interface_name(settings.egress_interface, interface)
    if not any(link.get("ifname") == egress for link in links):
        raise ControlPlaneError("Bootstrap egress interface does not exist")

    if not actual:
        commands.run(["ip", "address", "add", str(expected), "dev", interface])
    commands.run(
        [
            "wg",
            "set",
            interface,
            "listen-port",
            str(settings.listen_port),
            "private-key",
            "/dev/stdin",
        ],
        private + "\n",
    )
    commands.run(["ip", "link", "set", "up", "dev", interface])
    if commands.run(["sysctl", "-n", "net.ipv4.ip_forward"]).strip() != "1":
        commands.run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
    network = str(expected.network)
    commands.rule(
        "nat", "POSTROUTING", ["-s", network, "-o", egress, "-j", "MASQUERADE"]
    )
    commands.rule(
        "filter",
        "FORWARD",
        ["-i", interface, "-o", egress, "-s", network, "-j", "ACCEPT"],
    )
    commands.rule(
        "filter",
        "FORWARD",
        [
            "-i",
            egress,
            "-o",
            interface,
            "-d",
            network,
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED,RELATED",
            "-j",
            "ACCEPT",
        ],
    )
