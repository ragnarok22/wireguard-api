import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from health import HealthStatus, check_health  # noqa: E402
from wireguard import WireGuardError  # noqa: E402


class FakeWireGuard:
    def __init__(
        self,
        interface: str = "wg0",
        peers: dict | None = None,
        should_fail: bool = False,
    ):
        self.interface = interface
        self.peers = peers or {}
        self.should_fail = should_fail

    def list_peers(self):
        if self.should_fail:
            raise WireGuardError("WireGuard not available")
        return self.peers


def test_health_status_model():
    status = HealthStatus(
        status="healthy",
        version="1.0.0",
        uptime_seconds=100.5,
        wireguard_interface="wg0",
        wireguard_available=True,
        peer_count=5,
    )
    assert status.status == "healthy"
    assert status.version == "1.0.0"
    assert status.uptime_seconds == 100.5
    assert status.wireguard_interface == "wg0"
    assert status.wireguard_available is True
    assert status.peer_count == 5


def test_check_health_healthy():
    wg = FakeWireGuard(peers={"peer1": {}, "peer2": {}})

    health_status, http_code = check_health(wg, "0.4.1")

    assert http_code == 200
    assert health_status.status == "healthy"
    assert health_status.version == "0.4.1"
    assert health_status.wireguard_interface == "wg0"
    assert health_status.wireguard_available is True
    assert health_status.peer_count == 2
    assert health_status.uptime_seconds >= 0


def test_check_health_unhealthy():
    wg = FakeWireGuard(should_fail=True)

    health_status, http_code = check_health(wg, "0.4.1")

    assert http_code == 503
    assert health_status.status == "unhealthy"
    assert health_status.version == "0.4.1"
    assert health_status.wireguard_interface == "wg0"
    assert health_status.wireguard_available is False
    assert health_status.peer_count == 0


def test_check_health_no_peers():
    wg = FakeWireGuard(peers={})

    health_status, http_code = check_health(wg, "0.4.1")

    assert http_code == 200
    assert health_status.status == "healthy"
    assert health_status.wireguard_available is True
    assert health_status.peer_count == 0


def test_check_health_custom_interface():
    wg = FakeWireGuard(interface="wg1", peers={"peer1": {}})

    health_status, http_code = check_health(wg, "1.2.3")

    assert http_code == 200
    assert health_status.wireguard_interface == "wg1"
    assert health_status.version == "1.2.3"
