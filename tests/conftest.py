"""Typed kernel simulator with real durable storage for HTTP/readiness tests."""

import base64
from pathlib import Path

import pytest
from pydantic import SecretStr

from errors import WireGuardError
from service import PeerService
from settings import Settings
from storage import Store
from wireguard import PeerStats, Snapshot, WireGuard


def public_key(number: int) -> str:
    return base64.b64encode(number.to_bytes(32, "big")).decode("ascii")


class HTTPWireGuard(WireGuard):
    def __init__(self) -> None:
        super().__init__()
        self.peers: dict[str, PeerStats] = {}
        self.generated = 0
        self.events: list[tuple[str, str]] = []
        self.fail_observe = False
        self.fail_add = False
        self.fail_remove = False

    def snapshot(self) -> Snapshot:
        if self.fail_observe:
            raise WireGuardError("WireGuard observation unavailable")
        return Snapshot(public_key(1), dict(self.peers))

    def gen_keys(self) -> tuple[str, str]:
        self.generated += 1
        return public_key(1000 + self.generated), public_key(2000 + self.generated)

    def add_peer(self, public_key: str, address: str) -> None:
        self.events.append(("add", public_key))
        if self.fail_add:
            raise WireGuardError("Private subprocess diagnostic")
        self.peers[public_key] = PeerStats(
            public_key, (address,), "192.0.2.1:4567", 123, 100, 200, 25
        )

    def remove_peer(self, public_key: str) -> None:
        self.events.append(("remove", public_key))
        if self.fail_remove:
            raise WireGuardError("Private subprocess diagnostic")
        self.peers.pop(public_key, None)


@pytest.fixture
def http_kernel() -> HTTPWireGuard:
    return HTTPWireGuard()


@pytest.fixture
def http_node(tmp_path: Path, http_kernel: HTTPWireGuard) -> PeerService:
    settings = Settings(
        api_token=SecretStr("test-secret"),
        server_endpoint="node.example.org",
        data_dir=tmp_path,
        snapshot_ttl=0,
    )
    node = PeerService(
        settings,
        Store(tmp_path / "peers.sqlite3", "wg0", str(settings.server_address)),
        http_kernel,
    )
    node.initialize()
    return node
