import importlib
import sys
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from wireguard import WireGuard, WireGuardError


class FakeWireGuard:
    def __init__(
        self,
        peers=None,
        gen_keys_return=("priv", "pub"),
        interface_subnet: str | Exception = "10.0.0.1/24",
        next_ip: str | Exception = "10.0.0.2",
        run_result: str | Exception = "",
        create_error: Exception | None = None,
        delete_error: Exception | None = None,
        interface: str = "wg0",
        list_peers_error: Exception | None = None,
        restore_error: Exception | None = None,
    ):
        self.peers = peers or {}
        self.gen_keys_return = gen_keys_return
        self.interface_subnet = interface_subnet
        self.next_ip = next_ip
        self.run_result = run_result
        self.create_error = create_error
        self.delete_error = delete_error
        self.interface = interface
        self.list_peers_error = list_peers_error
        self.restore_error = restore_error
        self.restore_calls = 0
        self.created = []
        self.deleted = []

    def restore_peers(self):
        self.restore_calls += 1
        if self.restore_error:
            raise self.restore_error

    def list_peers(self):
        if self.list_peers_error:
            raise self.list_peers_error
        return self.peers

    def create_peer(self, pub_key, allowed_ips):
        if self.create_error:
            raise self.create_error
        self.created.append((pub_key, allowed_ips))

    def delete_peer(self, public_key):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(public_key)

    def gen_keys(self):
        return self.gen_keys_return

    def get_interface_subnet(self):
        if isinstance(self.interface_subnet, Exception):
            raise self.interface_subnet
        return self.interface_subnet

    def allocate_next_ip(self, subnet, used_ips):
        if isinstance(self.next_ip, Exception):
            raise self.next_ip
        return self.next_ip

    def _run(self, cmd):
        if isinstance(self.run_result, Exception):
            raise self.run_result
        return self.run_result


@pytest.fixture()
def api_module(monkeypatch):
    # Do not load local secrets or initialize /config during unit tests.
    monkeypatch.setattr("dotenv.load_dotenv", lambda: False)
    monkeypatch.setattr("wireguard.WireGuard", Mock(return_value=FakeWireGuard()))
    monkeypatch.setenv("API_TOKEN", "secret-token")
    monkeypatch.setenv("WG_INTERFACE", "wg0")
    monkeypatch.delenv("SERVER_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("SERVER_ENDPOINT", raising=False)
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


def test_delete_peer_success(client, api_module, auth_headers):
    fake = FakeWireGuard(peers={"pub": {}})
    api_module.wg = fake

    response = client.delete("/peers/pub", headers=auth_headers)

    assert response.status_code == 204
    assert fake.deleted == ["pub"]


def test_creates_peer_with_config_format(client, api_module, auth_headers):
    fake = FakeWireGuard(gen_keys_return=("privkey", "pubkey"), run_result="serverpub")
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
    assert "PublicKey = serverpub" in content
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


def test_create_config_with_public_key_returns_400(client, api_module, auth_headers):
    fake = FakeWireGuard(peers={}, gen_keys_return=("privkey", "pubkey"))
    api_module.wg = fake

    response = client.post(
        "/peers",
        params={"format": "config"},
        headers=auth_headers,
        json={"public_key": "existing", "allowed_ips": ["10.0.0.3/32"]},
    )

    assert response.status_code == 400
    assert "Cannot generate config" in response.json()["detail"]


def test_rejected_config_request_does_not_create_a_peer(
    client, api_module, auth_headers
):
    fake = FakeWireGuard()
    api_module.wg = fake
    response = client.post(
        "/peers?format=config",
        headers=auth_headers,
        json={"public_key": "existing", "allowed_ips": ["10.0.0.3/32"]},
    )

    assert response.status_code == 400
    assert fake.created == []


def test_health_does_not_hide_a_real_wireguard_query_failure(
    client, api_module, tmp_path, monkeypatch
):
    backend = WireGuard(storage_path=str(tmp_path / "peers.json"))
    monkeypatch.setattr(
        backend, "_run", Mock(side_effect=WireGuardError("interface unavailable"))
    )
    api_module.wg = backend

    assert client.get("/health").status_code == 503


def test_get_peer_not_found_returns_404(client, api_module, auth_headers):
    api_module.wg = FakeWireGuard(peers={})

    response = client.get("/peers/missing", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Peer not found"


def test_auto_allocation_failure_bubbles_http_error(client, api_module, auth_headers):
    fake = FakeWireGuard(interface_subnet=WireGuardError("no subnet"))
    api_module.wg = fake

    response = client.post("/peers", headers=auth_headers, json={})

    assert response.status_code == 500
    assert response.json()["detail"].startswith("IP Allocation failed: no subnet")


def test_create_peer_failure_returns_500(client, api_module, auth_headers):
    fake = FakeWireGuard(create_error=WireGuardError("boom"))
    api_module.wg = fake

    response = client.post(
        "/peers", headers=auth_headers, json={"allowed_ips": ["10.0.0.3/32"]}
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "boom"


def test_delete_peer_failure_returns_500(client, api_module, auth_headers):
    fake = FakeWireGuard(delete_error=WireGuardError("explode"), peers={"pub": {}})
    api_module.wg = fake

    response = client.delete("/peers/pub", headers=auth_headers)

    assert response.status_code == 500
    assert response.json()["detail"] == "explode"


def test_get_peer_config_returns_config(client, api_module, auth_headers):
    fake = FakeWireGuard(peers={"pub": {"allowed_ips": ["10.0.0.2/32"]}})
    api_module.wg = fake

    response = client.get("/peers/pub/config", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert "config" in body
    assert "PublicKey" in body["config"]


def test_get_peer_config_not_found_returns_404(client, api_module, auth_headers):
    api_module.wg = FakeWireGuard(peers={})

    response = client.get("/peers/missing/config", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Peer not found"


# --- Health Endpoint Tests ---


def test_health_returns_200_when_healthy(client, api_module):
    peer_data = {"transfer_rx": "100", "transfer_tx": "200", "latest_handshake": "0"}
    api_module.wg = FakeWireGuard(peers={"peer1": peer_data, "peer2": peer_data})

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["wireguard_available"] is True
    assert body["peer_count"] == 2
    assert body["wireguard_interface"] == "wg0"
    assert "version" in body
    assert "uptime_seconds" in body


def test_health_returns_503_when_unhealthy(client, api_module):
    api_module.wg = FakeWireGuard(list_peers_error=WireGuardError("unavailable"))

    response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unhealthy"
    assert body["wireguard_available"] is False
    assert body["peer_count"] == 0


def test_health_does_not_require_auth(client, api_module):
    api_module.wg = FakeWireGuard(peers={})

    # No auth headers provided
    response = client.get("/health")

    assert response.status_code == 200


# --- Metrics Endpoint Tests ---


def test_metrics_returns_prometheus_format(client, api_module):
    peer_data = {
        "transfer_rx": "1000",
        "transfer_tx": "2000",
        "latest_handshake": "0",
    }
    api_module.wg = FakeWireGuard(peers={"testpeer=": peer_data})

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]

    content = response.text
    assert "wireguard_peers_total" in content
    assert "wireguard_api_requests_total" in content


def test_metrics_does_not_require_auth(client, api_module):
    api_module.wg = FakeWireGuard(peers={})

    # No auth headers provided
    response = client.get("/metrics")

    assert response.status_code == 200


def test_metrics_includes_peer_transfer_metrics(client, api_module):
    peer_data = {
        "transfer_rx": "12345",
        "transfer_tx": "67890",
        "latest_handshake": "0",
    }
    api_module.wg = FakeWireGuard(peers={"metricspeer=": peer_data})

    response = client.get("/metrics")

    content = response.text
    assert "wireguard_peer_transfer_rx_bytes" in content
    assert "wireguard_peer_transfer_tx_bytes" in content


def test_startup_restores_peers_before_serving_requests(api_module):
    fake = FakeWireGuard(peers={"existing": {}})
    api_module.wg = fake

    with TestClient(api_module.app) as client:
        assert fake.restore_calls == 1
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["peer_count"] == 1

    assert fake.restore_calls == 1


def test_startup_restore_failure_is_logged_and_service_still_starts(api_module, caplog):
    fake = FakeWireGuard(restore_error=WireGuardError("storage unavailable"))
    api_module.wg = fake

    with TestClient(api_module.app) as client:
        assert client.get("/health").status_code == 200

    assert fake.restore_calls == 1
    assert "Failed to restore peers on startup" in caplog.text
    assert "storage unavailable" in caplog.text


def test_unhandled_exception_returns_generic_error_and_logs_cause(
    api_module, auth_headers, caplog
):
    api_module.wg = FakeWireGuard(list_peers_error=RuntimeError("internal diagnostic"))

    with TestClient(api_module.app, raise_server_exceptions=False) as client:
        response = client.get("/peers", headers=auth_headers)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    assert "internal diagnostic" not in response.text
    assert "Unhandled exception: internal diagnostic" in caplog.text
    error = next(record for record in caplog.records if record.levelname == "ERROR")
    assert isinstance(error.exc_info[1], RuntimeError)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/peers"),
        ("POST", "/peers"),
        ("GET", "/peers/pub"),
        ("DELETE", "/peers/pub"),
        ("GET", "/peers/pub/config"),
    ],
)
def test_peer_endpoints_require_authentication(client, api_module, method, path):
    backend = Mock(spec=WireGuard)
    api_module.wg = backend

    response = client.request(method, path, json={} if method == "POST" else None)

    assert response.status_code == 401
    assert backend.mock_calls == []


@pytest.mark.parametrize(
    "payload",
    [{"public_key": 123}, {"allowed_ips": "10.0.0.2/32"}, {"allowed_ips": [123]}],
)
def test_invalid_create_payload_does_not_change_wireguard(
    client, api_module, auth_headers, payload
):
    backend = Mock(spec=WireGuard)
    api_module.wg = backend

    response = client.post("/peers", headers=auth_headers, json=payload)

    assert response.status_code == 422
    assert backend.mock_calls == []


def test_create_with_existing_public_key_does_not_generate_keys(
    client, api_module, auth_headers
):
    fake = FakeWireGuard()
    fake.gen_keys = Mock(side_effect=AssertionError("Must not generate keys"))
    api_module.wg = fake

    response = client.post(
        "/peers",
        headers=auth_headers,
        json={"public_key": "existing", "allowed_ips": ["10.0.0.5/32"]},
    )

    assert response.status_code == 201
    assert response.json() == {
        "public_key": "existing",
        "allowed_ips": ["10.0.0.5/32"],
        "private_key": None,
    }
    fake.gen_keys.assert_not_called()
    assert fake.created == [("existing", ["10.0.0.5/32"])]


def test_auto_allocation_uses_existing_peer_addresses(
    client, api_module, auth_headers, tmp_path
):
    fake = FakeWireGuard(
        peers={
            "peer1": {"allowed_ips": ["10.0.0.2/32", "10.0.0.3/32"]},
            "peer2": {"allowed_ips": ["10.0.0.2/32"]},
            "peer3": {},
        }
    )
    allocator = WireGuard(storage_path=str(tmp_path / "peers.json"))
    fake.allocate_next_ip = Mock(wraps=allocator.allocate_next_ip)
    api_module.wg = fake

    response = client.post("/peers", headers=auth_headers, json={})

    assert response.status_code == 201
    assert response.json()["allowed_ips"] == ["10.0.0.4/32"]
    fake.allocate_next_ip.assert_called_once_with(
        "10.0.0.1/24", {"10.0.0.2", "10.0.0.3"}
    )
    assert fake.created == [("pub", ["10.0.0.4/32"])]


def test_auto_allocation_exhaustion_does_not_create_peer(
    client, api_module, auth_headers
):
    fake = FakeWireGuard(next_ip=WireGuardError("No available IPs in subnet"))
    api_module.wg = fake

    response = client.post("/peers", headers=auth_headers, json={})

    assert response.status_code == 500
    assert response.json() == {
        "detail": "IP Allocation failed: No available IPs in subnet"
    }
    assert fake.created == []


@pytest.mark.parametrize(
    "create", [True, False], ids=["created-config", "partial-config"]
)
@pytest.mark.parametrize(
    "configured_key", [True, False], ids=["env-key", "interface-key"]
)
def test_config_uses_configured_or_detected_server_key(
    client, api_module, auth_headers, monkeypatch, create, configured_key
):
    fake = FakeWireGuard(peers={"pub": {}}, gen_keys_return=("private", "pub"))
    fake._run = Mock(return_value="detected-key")
    api_module.wg = fake
    if configured_key:
        monkeypatch.setenv("SERVER_PUBLIC_KEY", "configured-key")

    if create:
        response = client.post(
            "/peers?format=config",
            headers=auth_headers,
            json={"allowed_ips": ["10.0.0.2/32"]},
        )
        assert response.status_code == 201
        config = response.text
    else:
        response = client.get("/peers/pub/config", headers=auth_headers)
        assert response.status_code == 200
        config = response.json()["config"]

    expected_key = "configured-key" if configured_key else "detected-key"
    assert f"PublicKey = {expected_key}" in config
    if configured_key:
        fake._run.assert_not_called()
    else:
        fake._run.assert_called_once_with(["wg", "show", "wg0", "public-key"])


@pytest.mark.parametrize(
    "create", [True, False], ids=["created-config", "partial-config"]
)
def test_server_key_lookup_failure_is_logged_and_config_is_returned(
    client, api_module, auth_headers, caplog, create
):
    api_module.wg = FakeWireGuard(
        peers={"pub": {}}, run_result=WireGuardError("interface unavailable")
    )

    if create:
        response = client.post(
            "/peers?format=config",
            headers=auth_headers,
            json={"allowed_ips": ["10.0.0.2/32"]},
        )
        assert response.status_code == 201
        config = response.text
    else:
        response = client.get("/peers/pub/config", headers=auth_headers)
        assert response.status_code == 200
        config = response.json()["config"]

    assert "PublicKey = SERVER_PUB_KEY_PLACEHOLDER" in config
    assert "Could not fetch server public key: interface unavailable" in caplog.text


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        (None, "vpn.example.com:51820"),
        ("vpn.example.com", "vpn.example.com:51820"),
        ("203.0.113.10", "203.0.113.10:51820"),
        ("vpn.example.com:15182", "vpn.example.com:15182"),
        ("[2001:db8::1]:15182", "[2001:db8::1]:15182"),
        # The helper only appends ports to domain/IPv4 hosts; IPv6 is passed through.
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]", "[2001:db8::1]"),
    ],
)
def test_config_endpoint_defaults_and_preserves_explicit_values(
    client, api_module, auth_headers, monkeypatch, endpoint, expected
):
    api_module.wg = FakeWireGuard(peers={"pub": {}}, run_result="server-key")
    if endpoint is not None:
        monkeypatch.setenv("SERVER_ENDPOINT", endpoint)

    response = client.get("/peers/pub/config", headers=auth_headers)

    assert response.status_code == 200
    assert f"Endpoint = {expected}" in response.json()["config"].splitlines()
