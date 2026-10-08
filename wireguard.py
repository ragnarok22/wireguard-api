"""Typed, bounded subprocess access to a single WireGuard interface."""

import ipaddress
import math
import re
import subprocess
from dataclasses import dataclass

from errors import WireGuardError
from keys import validate_key


@dataclass(frozen=True)
class PeerStats:
    public_key: str
    allowed_ips: tuple[str, ...]
    endpoint: str | None
    latest_handshake: int | None
    transfer_rx: int
    transfer_tx: int
    persistent_keepalive: int


@dataclass(frozen=True)
class Snapshot:
    public_key: str
    peers: dict[str, PeerStats]


def _unsigned(value: str) -> int:
    if re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError("Invalid unsigned integer")
    return int(value)


class WireGuard:
    def __init__(self, interface: str = "wg0", timeout: float = 5.0) -> None:
        if re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,14}", interface) is None:
            raise WireGuardError("Invalid WireGuard interface")
        if not math.isfinite(timeout) or timeout <= 0:
            raise WireGuardError("Invalid WireGuard timeout")
        self.interface = interface
        self.timeout = timeout

    def _run(self, command: list[str], input_text: str | None = None) -> str:
        try:
            result = subprocess.run(
                command,
                input=input_text,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
            )
        except subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired:
            # Subprocess exceptions can contain keys in command, input, or output.
            raise WireGuardError("WireGuard command failed") from None
        return result.stdout

    def snapshot(self) -> Snapshot:
        output = self._run(["wg", "show", self.interface, "dump"])
        try:
            lines = output.splitlines()
            if not lines:
                raise ValueError("Missing interface header")
            header = lines[0].split("\t")
            if len(header) != 4:
                raise ValueError("Invalid interface header")
            _, public_key, listen_port, fwmark = header
            validate_key(public_key)
            if _unsigned(listen_port) > 65535:
                raise ValueError("Invalid listen port")
            if fwmark != "off":
                if re.fullmatch(r"(?:[0-9]+|0x[0-9a-fA-F]+)", fwmark) is None:
                    raise ValueError("Invalid firewall mark")
                mark = int(fwmark, 16) if fwmark.startswith("0x") else int(fwmark)
                if mark > 0xFFFFFFFF:
                    raise ValueError("Invalid firewall mark")

            peers: dict[str, PeerStats] = {}
            for line in lines[1:]:
                fields = line.split("\t")
                if len(fields) != 8:
                    raise ValueError("Invalid peer row")
                key, _, endpoint, allowed, handshake, rx, tx, keepalive = fields
                validate_key(key)
                if key in peers or not endpoint:
                    raise ValueError("Invalid peer identity or endpoint")
                allowed_ips = () if allowed == "(none)" else tuple(allowed.split(","))
                for network in allowed_ips:
                    if str(ipaddress.ip_network(network, strict=True)) != network:
                        raise ValueError("Noncanonical allowed network")
                latest_handshake = _unsigned(handshake)
                persistent_keepalive = 0 if keepalive == "off" else _unsigned(keepalive)
                if persistent_keepalive > 65535:
                    raise ValueError("Invalid keepalive")
                peers[key] = PeerStats(
                    public_key=key,
                    allowed_ips=allowed_ips,
                    endpoint=None if endpoint == "(none)" else endpoint,
                    latest_handshake=latest_handshake or None,
                    transfer_rx=_unsigned(rx),
                    transfer_tx=_unsigned(tx),
                    persistent_keepalive=persistent_keepalive,
                )
            return Snapshot(public_key=public_key, peers=peers)
        except ValueError:
            raise WireGuardError("Invalid WireGuard dump") from None

    def list_peers(self) -> dict[str, PeerStats]:
        return self.snapshot().peers

    def gen_keys(self) -> tuple[str, str]:
        try:
            private_key = validate_key(self._run(["wg", "genkey"]).strip())
            public_key = validate_key(
                self._run(["wg", "pubkey"], input_text=private_key + "\n").strip()
            )
        except ValueError:
            raise WireGuardError("Invalid generated WireGuard key") from None
        return private_key, public_key

    def add_peer(self, public_key: str, address: str) -> None:
        try:
            validate_key(public_key)
            network = ipaddress.IPv4Network(address, strict=True)
            if network.prefixlen != 32 or str(network) != address:
                raise ValueError("Expected a canonical IPv4 host network")
        except ValueError:
            raise WireGuardError("Invalid WireGuard peer") from None
        self._run(
            ["wg", "set", self.interface, "peer", public_key, "allowed-ips", address]
        )

    def remove_peer(self, public_key: str) -> None:
        try:
            validate_key(public_key)
        except ValueError:
            raise WireGuardError("Invalid WireGuard peer key") from None
        self._run(["wg", "set", self.interface, "peer", public_key, "remove"])
