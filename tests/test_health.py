"""Readiness proves durable, observed convergence and preserves failure codes."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from conftest import HTTPWireGuard, public_key

import health
from errors import StorageError, WireGuardError
from models import PeerCreate
from service import PeerService
from storage import Store
from version import VERSION
from wireguard import PeerStats, WireGuard


def test_empty_observed_inventory_is_ready(
    http_node: PeerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health.time, "monotonic", lambda: http_node.started_at + 12.34)
    result = health.readiness(http_node)
    assert result.model_dump() == {
        "status": "ready",
        "version": VERSION,
        "uptime_seconds": 12.3,
        "interface": "wg0",
        "reason": None,
    }


@pytest.mark.parametrize("failure", ["pending", "unknown", "mismatched"])
def test_unconverged_inventory_is_not_ready(
    http_node: PeerService, http_kernel: HTTPWireGuard, failure: str
) -> None:
    http_kernel.fail_add = failure == "pending"
    created = http_node.create(PeerCreate(key_mode="generated"), "health-peer")
    if failure == "unknown":
        http_kernel.peers[public_key(999)] = PeerStats(
            public_key(999), ("10.13.13.99/32",), None, None, 0, 0, 0
        )
    elif failure == "mismatched":
        http_kernel.peers[created.peer.public_key] = PeerStats(
            created.peer.public_key, ("0.0.0.0/0",), None, None, 0, 0, 0
        )
    result = health.readiness(http_node)
    assert result.status == "not_ready"
    assert result.reason == "state_not_converged"
    http_kernel.fail_add = False
    http_node.reconcile()
    assert health.readiness(http_node).status == "ready"


def test_real_wireguard_observation_failure_is_not_an_empty_healthy_inventory(
    http_node: PeerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = WireGuard()
    command = Mock(side_effect=WireGuardError("WireGuard unavailable"))
    monkeypatch.setattr(backend, "_run", command)
    node = PeerService(
        http_node.settings,
        Store(tmp_path / "real.sqlite3", "wg0", str(http_node.settings.server_address)),
        backend,
    )
    node.initialize()
    result = health.readiness(node)
    assert result.status == "not_ready"
    assert result.reason == "wireguard_unavailable"
    command.assert_called()


def test_failed_storage_readiness_preserves_safe_code(
    http_node: PeerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        http_node.store,
        "health_check",
        Mock(side_effect=StorageError("private database diagnostic")),
    )
    result = health.readiness(http_node)
    assert result.status == "not_ready"
    assert result.reason == "storage_unavailable"
    assert "private" not in result.model_dump_json()
