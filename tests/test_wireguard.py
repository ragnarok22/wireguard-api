import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from wireguard import WireGuard, WireGuardError  # noqa: E402


def test_allocate_next_ip_skips_used_and_server_ip():
    wg = WireGuard()

    next_ip = wg.allocate_next_ip("10.0.0.1/24", {"10.0.0.2", "10.0.0.3"})

    assert next_ip == "10.0.0.4"


def test_allocate_next_ip_raises_when_full():
    wg = WireGuard()

    with pytest.raises(WireGuardError, match="No available IPs"):
        wg.allocate_next_ip("10.0.0.1/30", {"10.0.0.2"})


def test_allocate_next_ip_invalid_cidr():
    wg = WireGuard()

    with pytest.raises(WireGuardError, match="Invalid subnet CIDR"):
        wg.allocate_next_ip("not-a-cidr", set())


def test_list_peers_parses_wg_dump(monkeypatch):
    wg = WireGuard()

    dump = "pubkey= preshared 1.2.3.4:51820 10.0.0.2/32,10.0.0.3/32 100 200 300 off\n"
    monkeypatch.setattr(wg, "_run", lambda cmd: dump)

    peers = wg.list_peers()

    assert "pubkey=" in peers
    assert peers["pubkey="]["allowed_ips"] == ["10.0.0.2/32", "10.0.0.3/32"]


def test_list_peers_returns_empty_on_error(monkeypatch):
    wg = WireGuard()

    def raise_error(cmd):
        raise WireGuardError("boom")

    monkeypatch.setattr(wg, "_run", raise_error)

    assert wg.list_peers() == {}
