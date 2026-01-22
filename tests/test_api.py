import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


class FakeWireGuard:
    def __init__(self, peers=None, gen_keys_return=("priv", "pub")):
        self.peers = peers or {}
        self.gen_keys_return = gen_keys_return
        self.created = []
        self.deleted = []

    def list_peers(self):
        return self.peers

    def create_peer(self, pub_key, allowed_ips):
        self.created.append((pub_key, allowed_ips))

    def delete_peer(self, public_key):
        self.deleted.append(public_key)

    def gen_keys(self):
        return self.gen_keys_return


@pytest.fixture()
def api_module(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret-token")
    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(repo_root))
    if "api" in sys.modules:
        del sys.modules["api"]
    return importlib.import_module("api")


@pytest.fixture()
def client(api_module):
    return TestClient(api_module.app)


@pytest.fixture()
def auth_headers():
    return {"X-API-Token": "secret-token"}


def test_rejects_invalid_token(client):
    response = client.get("/peers", headers={"X-API-Token": "wrong"})

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid authentication token"


def test_lists_peers(client, api_module, auth_headers):
    peer_data = {
        "preshared_key": "psk",
        "endpoint": "10.0.0.2:51820",
        "allowed_ips": ["10.0.0.2/32"],
        "latest_handshake": "100",
        "transfer_rx": "0",
        "transfer_tx": "0",
        "persistent_keepalive": "off",
    }
    api_module.wg = FakeWireGuard(peers={"pub1": peer_data.copy()})

    response = client.get("/peers", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == [{**peer_data, "public_key": "pub1"}]


def test_get_peer_returns_peer(client, api_module, auth_headers):
    peer_data = {
        "preshared_key": "psk",
        "endpoint": "10.0.0.2:51820",
        "allowed_ips": ["10.0.0.2/32"],
        "latest_handshake": "100",
        "transfer_rx": "0",
        "transfer_tx": "0",
        "persistent_keepalive": "off",
    }
    api_module.wg = FakeWireGuard(peers={"pub1": peer_data.copy()})

    response = client.get("/peers/pub1", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {**peer_data, "public_key": "pub1"}


def test_creates_peer_with_generated_keys(client, api_module, auth_headers):
    fake = FakeWireGuard(gen_keys_return=("privkey", "pubkey"))
    api_module.wg = fake

    response = client.post(
        "/peers", headers=auth_headers, json={"allowed_ips": ["10.0.0.3/32"]}
    )

    assert response.status_code == 201
    assert response.json() == {
        "public_key": "pubkey",
        "allowed_ips": ["10.0.0.3/32"],
        "private_key": "privkey",
    }
    assert fake.created == [("pubkey", ["10.0.0.3/32"])]


def test_delete_peer_not_found(client, api_module, auth_headers):
    api_module.wg = FakeWireGuard(peers={})

    response = client.delete("/peers/missing", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Peer not found"


def test_creates_peer_with_config_format(client, api_module, auth_headers):
    fake = FakeWireGuard(gen_keys_return=("privkey", "pubkey"))
    api_module.wg = fake

    response = client.post(
        "/peers",
        params={"format": "config"},
        headers=auth_headers,
        json={"allowed_ips": ["10.0.0.3/32"]},
    )

    assert response.status_code == 201
    assert response.headers["content-type"].startswith("text/plain")
    content = response.text
    assert "[Interface]" in content
    assert "PrivateKey = privkey" in content
    assert "Address = 10.0.0.3/32" in content
    assert "[Peer]" in content
    assert "PublicKey = SERVER_PUB_KEY_PLACEHOLDER" in content
    assert fake.created == [("pubkey", ["10.0.0.3/32"])]


def test_creates_peer_auto_allocation(client, api_module, auth_headers):
    fake = FakeWireGuard(gen_keys_return=("privkey", "pubkey"))

    # Mock subnet and used IPs
    # Mock subnet and used IPs on the instance mock

    fake.get_interface_subnet = lambda: "10.0.0.1/24"
    fake.allocate_next_ip = lambda subnet, used: "10.0.0.2"

    api_module.wg = fake

    # Create without allowed_ips
    response = client.post("/peers", headers=auth_headers, json={})

    assert response.status_code == 201
    data = response.json()
    assert data["public_key"] == "pubkey"
    assert data["allowed_ips"] == ["10.0.0.2/32"]

    assert fake.created == [("pubkey", ["10.0.0.2/32"])]
