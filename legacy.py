"""Validate an entire legacy inventory without modifying its original file."""

import json
from ipaddress import IPv4Interface
from pathlib import Path

from errors import StorageError
from keys import validate_key


def client_address(address: str, server: IPv4Interface) -> str:
    """Normalize one usable IPv4 /32 in the server's client pool."""
    client = IPv4Interface(address)
    network = server.network
    if (
        client.network.prefixlen != 32
        or client.ip not in network
        or client.ip in (server.ip, network.network_address, network.broadcast_address)
    ):
        raise ValueError("Expected an available IPv4 client address")
    return str(client.ip)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate legacy JSON key")
        result[name] = value
    return result


def read_legacy(path: Path | None, server: IPv4Interface) -> list[tuple[str, str]]:
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise StorageError("Cannot read legacy inventory") from exc
    try:
        inventory = json.loads(text, object_pairs_hook=_unique_object)
        if not isinstance(inventory, dict):
            raise ValueError("Expected a legacy inventory object")
        peers: list[tuple[str, str]] = []
        addresses: set[str] = set()
        for public_key, data in inventory.items():
            validate_key(public_key)
            if not isinstance(data, dict) or set(data) != {"allowed_ips"}:
                raise ValueError("Invalid legacy peer record")
            ips = data["allowed_ips"]
            if not isinstance(ips, list) or len(ips) != 1:
                raise ValueError("Expected one legacy client address")
            if not isinstance(ips[0], str) or not ips[0].endswith("/32"):
                raise ValueError("Expected an IPv4 /32")
            address = client_address(ips[0], server)
            if address in addresses:
                raise ValueError("Duplicate legacy client address")
            addresses.add(address)
            peers.append((public_key, address))
        return peers
    except ValueError as exc:
        raise StorageError("Invalid legacy inventory; original preserved") from exc
