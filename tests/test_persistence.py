import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from wireguard import WireGuard  # noqa: E402


def test_save_and_load_peers():
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        storage_path = tmp.name

    try:
        wg = WireGuard(storage_path=storage_path)

        # Test Save
        wg.save_peer_to_storage("pubkey1=", ["10.0.0.2/32"])

        with open(storage_path) as f:
            data = json.load(f)

        assert "pubkey1=" in data
        assert data["pubkey1="]["allowed_ips"] == ["10.0.0.2/32"]

        # Test Load
        wg2 = WireGuard(storage_path=storage_path)
        peers = wg2.load_peers_from_storage()
        assert "pubkey1=" in peers

        # Test Remove
        wg.remove_peer_from_storage("pubkey1=")
        peers = wg.load_peers_from_storage()
        assert "pubkey1=" not in peers

    finally:
        if os.path.exists(storage_path):
            os.remove(storage_path)


def test_restore_peers(monkeypatch):
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        storage_path = tmp.name

    # Pre-populate storage
    initial_data = {
        "pubkey1=": {"allowed_ips": ["10.0.0.2/32"]},
        "pubkey2=": {"allowed_ips": ["10.0.0.3/32"]},
    }
    with open(storage_path, "w") as f:
        json.dump(initial_data, f)

    try:
        wg = WireGuard(storage_path=storage_path)

        added_peers = []

        def mock_add_peer(public_key, allowed_ips):
            added_peers.append((public_key, allowed_ips))

        monkeypatch.setattr(wg, "_add_peer_to_interface", mock_add_peer)

        wg.restore_peers()

        assert len(added_peers) == 2
        # Order is not guaranteed in dict, so check presence
        assert ("pubkey1=", ["10.0.0.2/32"]) in added_peers
        assert ("pubkey2=", ["10.0.0.3/32"]) in added_peers

    finally:
        if os.path.exists(storage_path):
            os.remove(storage_path)
