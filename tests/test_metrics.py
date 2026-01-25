import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from metrics import (  # noqa: E402
    PEER_LAST_HANDSHAKE,
    PEER_TRANSFER_RX,
    PEER_TRANSFER_TX,
    PEERS_TOTAL,
    normalize_path,
    update_wireguard_metrics,
)
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


class TestNormalizePath:
    def test_peers_list(self):
        assert normalize_path("/peers") == "/peers"

    def test_peer_by_key(self):
        assert normalize_path("/peers/abc123pubkey=") == "/peers/{public_key}"

    def test_peer_config(self):
        assert (
            normalize_path("/peers/abc123pubkey=/config")
            == "/peers/{public_key}/config"
        )

    def test_health_unchanged(self):
        assert normalize_path("/health") == "/health"

    def test_metrics_unchanged(self):
        assert normalize_path("/metrics") == "/metrics"

    def test_unknown_path_unchanged(self):
        assert normalize_path("/unknown/path") == "/unknown/path"


class TestUpdateWireGuardMetrics:
    def test_updates_peer_count(self):
        wg = FakeWireGuard(
            peers={
                "peer1=": {
                    "transfer_rx": "100",
                    "transfer_tx": "200",
                    "latest_handshake": "0",
                },
                "peer2=": {
                    "transfer_rx": "300",
                    "transfer_tx": "400",
                    "latest_handshake": "0",
                },
            }
        )

        update_wireguard_metrics(wg)

        assert PEERS_TOTAL._value.get() == 2

    def test_updates_transfer_metrics(self):
        wg = FakeWireGuard(
            peers={
                "testpeer=": {
                    "transfer_rx": "12345",
                    "transfer_tx": "67890",
                    "latest_handshake": "0",
                },
            }
        )

        update_wireguard_metrics(wg)

        assert PEER_TRANSFER_RX.labels(public_key="testpeer=")._value.get() == 12345
        assert PEER_TRANSFER_TX.labels(public_key="testpeer=")._value.get() == 67890

    def test_handles_handshake_timestamp(self):
        # Use a timestamp from a few seconds ago
        recent_handshake = int(time.time()) - 60

        wg = FakeWireGuard(
            peers={
                "handshakepeer=": {
                    "transfer_rx": "0",
                    "transfer_tx": "0",
                    "latest_handshake": str(recent_handshake),
                },
            }
        )

        update_wireguard_metrics(wg)

        # Should be ~60 seconds (with tolerance for test execution)
        handshake_value = PEER_LAST_HANDSHAKE.labels(
            public_key="handshakepeer="
        )._value.get()
        assert 55 <= handshake_value <= 65

    def test_handles_no_handshake(self):
        wg = FakeWireGuard(
            peers={
                "nohandshake=": {
                    "transfer_rx": "0",
                    "transfer_tx": "0",
                    "latest_handshake": "0",
                },
            }
        )

        update_wireguard_metrics(wg)

        assert PEER_LAST_HANDSHAKE.labels(public_key="nohandshake=")._value.get() == -1

    def test_handles_wireguard_failure(self):
        wg = FakeWireGuard(should_fail=True)

        # Should not raise, just set peers to 0
        update_wireguard_metrics(wg)

        assert PEERS_TOTAL._value.get() == 0

    def test_handles_invalid_transfer_values(self):
        wg = FakeWireGuard(
            peers={
                "invalidpeer=": {
                    "transfer_rx": "not_a_number",
                    "transfer_tx": None,
                    "latest_handshake": "invalid",
                },
            }
        )

        # Should not raise
        update_wireguard_metrics(wg)

        assert PEER_TRANSFER_RX.labels(public_key="invalidpeer=")._value.get() == 0
        assert PEER_TRANSFER_TX.labels(public_key="invalidpeer=")._value.get() == 0
        assert PEER_LAST_HANDSHAKE.labels(public_key="invalidpeer=")._value.get() == -1

    def test_empty_peers(self):
        wg = FakeWireGuard(peers={})

        update_wireguard_metrics(wg)

        assert PEERS_TOTAL._value.get() == 0
