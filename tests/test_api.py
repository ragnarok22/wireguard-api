"""HTTP regression contracts over a real store and typed WireGuard simulator."""

import asyncio
import importlib
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from conftest import HTTPWireGuard, public_key
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

import api
from errors import StorageError, WireGuardError
from models import PeerPage
from service import PeerService
from settings import Settings
from version import VERSION
from wireguard import PeerStats, WireGuard

AUTH = {"X-API-Token": "test-secret"}
CREATE_HEADERS = AUTH | {"Idempotency-Key": "request-1"}


@pytest.fixture
def app(http_node: PeerService) -> FastAPI:
    return api.create_app(service=http_node)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def assert_error(response: Response, status: int, code: str) -> None:
    assert response.status_code == status
    assert set(response.json()) == {"code", "detail"}
    assert response.json()["code"] == code
    assert isinstance(response.json()["detail"], str)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/peers"),
        ("POST", "/v1/peers"),
        ("GET", f"/v1/peers/{uuid4()}"),
        ("DELETE", f"/v1/peers/{uuid4()}"),
        ("GET", f"/v1/peers/{uuid4()}/config-template"),
        ("GET", f"/v1/operations/{uuid4()}"),
        ("GET", "/v1/server"),
    ],
)
@pytest.mark.parametrize("headers,status", [({}, 401), ({"X-API-Token": "wrong"}, 403)])
def test_management_requires_token_without_mutation(
    client: TestClient,
    http_kernel: HTTPWireGuard,
    method: str,
    path: str,
    headers: dict[str, str],
    status: int,
) -> None:
    response = client.request(method, path, headers=headers)
    assert_error(response, status, "http_error")
    assert http_kernel.generated == 0 and http_kernel.events == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"key_mode": "external"},
        {"key_mode": "generated", "public_key": public_key(10)},
        {"key_mode": "external", "public_key": 123},
        {"key_mode": "external", "public_key": "submitted-secret"},
        {"key_mode": "generated", "allowed_ips": ["10.13.13.2/32"]},
        {"key_mode": "generated", "address": "10.13.13.1"},
        {"key_mode": "generated", "address": "10.13.13.255"},
        {"key_mode": "generated", "address": "10.13.14.2"},
        {"key_mode": "generated", "address": "2001:db8::1"},
        {"key_mode": "generated", "private_key": "submitted-secret"},
    ],
)
def test_invalid_payload_never_generates_or_mutates(
    client: TestClient,
    http_node: PeerService,
    http_kernel: HTTPWireGuard,
    payload: dict[str, object],
) -> None:
    response = client.post("/v1/peers", headers=CREATE_HEADERS, json=payload)
    assert_error(response, 422, "invalid_input")
    assert "submitted-secret" not in response.text
    assert "test-secret" not in response.text
    assert http_kernel.generated == 0 and http_kernel.events == []
    assert http_node.store.list_peers() == []


@pytest.mark.parametrize("query", ["format=config", "format=json", "unknown=secret"])
def test_obsolete_query_never_generates_keys_or_mutates(
    client: TestClient,
    http_kernel: HTTPWireGuard,
    query: str,
) -> None:
    response = client.post(
        f"/v1/peers?{query}", headers=CREATE_HEADERS, json={"key_mode": "generated"}
    )
    assert_error(response, 422, "invalid_input")
    assert http_kernel.generated == 0 and http_kernel.events == []


@pytest.mark.parametrize("request_key", [None, "submitted secret", "x" * 129])
def test_idempotency_header_is_required_and_validated_before_mutation(
    client: TestClient,
    http_kernel: HTTPWireGuard,
    request_key: str | None,
) -> None:
    headers = AUTH if request_key is None else AUTH | {"Idempotency-Key": request_key}
    response = client.post("/v1/peers", headers=headers, json={"key_mode": "generated"})
    assert_error(response, 422, "invalid_input")
    assert "submitted secret" not in response.text
    assert "x" * 129 not in response.text
    assert http_kernel.generated == 0 and http_kernel.events == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/peers"),
        ("POST", "/peers?format=config"),
        ("DELETE", "/peers/pub"),
        ("GET", "/peers/pub/config"),
        ("GET", "/health"),
        ("GET", "/v1/peers/pub/config"),
    ],
)
def test_legacy_routes_are_404_without_mutation(
    client: TestClient,
    http_kernel: HTTPWireGuard,
    method: str,
    path: str,
) -> None:
    response = client.request(method, path, headers=CREATE_HEADERS, json={})
    assert_error(response, 404, "http_error")
    assert http_kernel.generated == 0 and http_kernel.events == []


def test_generated_creation_defaults_replay_and_one_time_credentials(
    client: TestClient,
    http_node: PeerService,
    http_kernel: HTTPWireGuard,
) -> None:
    created = client.post(
        "/v1/peers", headers=CREATE_HEADERS, json={"key_mode": "generated"}
    )
    assert created.status_code == 201
    body = created.json()
    assert set(body) == {
        "peer",
        "operation",
        "private_key",
        "client_config",
        "replayed",
    }
    peer, operation = body["peer"], body["operation"]
    assert peer["address"] == "10.13.13.2"
    assert peer["state"] == "active" and peer["applied"] is True
    assert operation["status"] == "complete" and operation["peer_id"] == peer["id"]
    assert body["private_key"] == public_key(1001)
    config = body["client_config"]
    for line in [
        f"PrivateKey = {body['private_key']}",
        "Address = 10.13.13.2/32",
        "DNS = 1.1.1.1",
        "Endpoint = node.example.org:51820",
        "AllowedIPs = 0.0.0.0/0",
        "PersistentKeepalive = 25",
    ]:
        assert line in config.splitlines()
    assert "::/0" not in config
    assert created.headers["Cache-Control"] == "no-store"
    assert created.headers["Location"] == f"/v1/peers/{peer['id']}"
    events = list(http_kernel.events)
    replay = client.post(
        "/v1/peers", headers=CREATE_HEADERS, json={"key_mode": "generated"}
    )
    assert replay.status_code == 200
    assert replay.headers["Cache-Control"] == "no-store"
    assert replay.json() == body | {
        "replayed": True,
        "private_key": None,
        "client_config": None,
    }
    assert http_kernel.generated == 1 and http_kernel.events == events
    reads = [
        client.get(created.headers["Location"], headers=AUTH),
        client.get("/v1/peers", headers=AUTH),
        client.get(f"/v1/operations/{operation['id']}", headers=AUTH),
        client.get(f"/v1/peers/{peer['id']}/config-template", headers=AUTH),
    ]
    for response in reads:
        assert response.status_code == 200
        assert body["private_key"] not in response.text
        assert "preshared_key" not in response.text.lower()
        assert "PresharedKey" not in response.text
    assert "PrivateKey = <YOUR_PRIVATE_KEY>" in reads[-1].json()["config"]
    assert body["private_key"].encode() not in http_node.store.path.read_bytes()
    conflict = client.post(
        "/v1/peers",
        headers=CREATE_HEADERS,
        json={"key_mode": "generated", "address": "10.13.13.3"},
    )
    assert_error(conflict, 409, "conflict")
    assert http_kernel.generated == 1 and http_kernel.events == events


def test_external_slash_public_key_is_reachable_by_uuid_and_deletable(
    client: TestClient,
    http_kernel: HTTPWireGuard,
) -> None:
    slash_key = public_key(2**256 - 1)
    assert "/" in slash_key
    response = client.post(
        "/v1/peers",
        headers=CREATE_HEADERS,
        json={"key_mode": "external", "public_key": slash_key, "address": "10.13.13.8"},
    )
    assert response.status_code == 201
    assert response.json()["peer"]["public_key"] == slash_key
    assert response.json()["private_key"] is None
    assert response.json()["client_config"] is None
    location = response.headers["Location"]
    assert slash_key not in location
    assert client.get(location, headers=AUTH).json() == response.json()["peer"]
    assert http_kernel.generated == 0
    deleted = client.delete(location, headers=AUTH)
    assert deleted.status_code == 204 and deleted.content == b""
    assert http_kernel.events == [("add", slash_key), ("remove", slash_key)]
    assert_error(client.get(location, headers=AUTH), 404, "not_found")


def test_pending_creation_replay_and_recovery(
    client: TestClient,
    http_node: PeerService,
    http_kernel: HTTPWireGuard,
) -> None:
    http_kernel.fail_add = True
    response = client.post(
        "/v1/peers", headers=CREATE_HEADERS, json={"key_mode": "generated"}
    )
    assert response.status_code == 202
    assert response.headers["Retry-After"] == "5"
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["peer"]["state"] == "pending" and not body["peer"]["applied"]
    assert body["operation"]["error"] == "wireguard_unavailable"
    assert body["private_key"] and body["client_config"]
    assert "Private subprocess" not in response.text
    replay = client.post(
        "/v1/peers", headers=CREATE_HEADERS, json={"key_mode": "generated"}
    )
    assert replay.status_code == 202
    assert replay.json()["replayed"] is True
    assert (
        replay.json()["private_key"] is None and replay.json()["client_config"] is None
    )
    assert http_kernel.generated == 1
    assert client.get("/readyz").status_code == 503
    http_kernel.fail_add = False
    http_node.reconcile()
    operation = client.get(f"/v1/operations/{body['operation']['id']}", headers=AUTH)
    assert operation.json()["status"] == "complete"
    assert client.get(response.headers["Location"], headers=AUTH).json()["applied"]
    assert client.get("/readyz").status_code == 200


def test_pending_delete_operation_location_and_recovery(
    client: TestClient,
    http_node: PeerService,
    http_kernel: HTTPWireGuard,
) -> None:
    created = client.post(
        "/v1/peers", headers=CREATE_HEADERS, json={"key_mode": "generated"}
    )
    location = created.headers["Location"]
    http_kernel.fail_remove = True
    response = client.delete(location, headers=AUTH)
    assert response.status_code == 202
    assert response.headers["Retry-After"] == "5"
    assert response.headers["Location"] == f"/v1/operations/{response.json()['id']}"
    assert (
        client.get(response.headers["Location"], headers=AUTH).json() == response.json()
    )
    assert response.json()["kind"] == "delete"
    assert response.json()["status"] == "pending"
    assert response.json()["error"] == "wireguard_unavailable"
    assert "Private subprocess" not in response.text
    assert client.get(location, headers=AUTH).json()["state"] == "deleting"
    assert client.delete(location, headers=AUTH).json()["id"] == response.json()["id"]
    http_kernel.fail_remove = False
    http_node.reconcile()
    assert (
        client.get(response.headers["Location"], headers=AUTH).json()["status"]
        == "complete"
    )
    assert_error(client.get(location, headers=AUTH), 404, "not_found")


def test_pagination_uses_uuid_cursor_and_bounded_limit(client: TestClient) -> None:
    ids = sorted(
        client.post(
            "/v1/peers",
            headers=AUTH | {"Idempotency-Key": str(index)},
            json={"key_mode": "generated"},
        ).json()["peer"]["id"]
        for index in range(3)
    )
    first = client.get("/v1/peers?limit=2", headers=AUTH).json()
    assert [peer["id"] for peer in first["items"]] == ids[:2]
    assert first["next_cursor"] == ids[1]
    last = client.get(f"/v1/peers?limit=2&after={ids[1]}", headers=AUTH).json()
    assert [peer["id"] for peer in last["items"]] == ids[2:]
    assert last["next_cursor"] is None
    for query in ("limit=0", "limit=101", "limit=no", "after=not-a-uuid"):
        assert_error(
            client.get(f"/v1/peers?{query}", headers=AUTH), 422, "invalid_input"
        )
    assert client.get("/v1/peers", headers=AUTH).json()["next_cursor"] is None


@pytest.mark.parametrize(
    "path", ["/v1/peers/{id}", "/v1/peers/{id}/config-template", "/v1/operations/{id}"]
)
def test_missing_uuid_resources_have_typed_errors(
    client: TestClient, path: str
) -> None:
    assert_error(client.get(path.format(id=uuid4()), headers=AUTH), 404, "not_found")
    assert_error(
        client.get(path.format(id="secret-invalid-id"), headers=AUTH),
        422,
        "invalid_input",
    )


def test_server_defaults_and_capacity(client: TestClient) -> None:
    response = client.get("/v1/server", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "public_key": public_key(1),
        "endpoint": "node.example.org:51820",
        "interface": "wg0",
        "address": "10.13.13.1/24",
        "pool": "10.13.13.0/24",
        "capacity": 253,
        "reserved": 0,
        "available": 253,
    }


def test_real_failed_observation_returns_ready_503_but_live_200(
    http_node: PeerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = WireGuard()
    command = Mock(side_effect=WireGuardError("Interface unavailable"))
    monkeypatch.setattr(backend, "_run", command)
    http_node.backend = backend
    with TestClient(api.create_app(service=http_node)) as client:
        assert client.get("/livez").json() == {"status": "alive", "version": VERSION}
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["reason"] == "wireguard_unavailable"
        assert_error(
            client.get("/v1/peers", headers=AUTH), 503, "wireguard_unavailable"
        )
    command.assert_called()


def test_metrics_distinguish_observed_zero_from_failed_observation(
    client: TestClient,
    http_kernel: HTTPWireGuard,
) -> None:
    response = client.get("/metrics")
    assert (
        response.status_code == 200 and "text/plain" in response.headers["Content-Type"]
    )
    assert "wireguard_available 1.0\n" in response.text
    assert "wireguard_peers_total 0.0\n" in response.text
    client.post("/v1/peers", headers=CREATE_HEADERS, json={"key_mode": "generated"})
    observed = client.get("/metrics").text
    assert "wireguard_peers_total 1.0\n" in observed
    assert (
        f'wireguard_peer_transfer_rx_bytes{{public_key="{public_key(2001)}"}} 100.0'
        in observed
    )
    http_kernel.fail_observe = True
    failed = client.get("/metrics")
    assert failed.status_code == 200
    assert "wireguard_available 0.0\n" in failed.text
    assert "wireguard_peers_total NaN\n" in failed.text
    assert "wireguard_pending_operations -1.0\n" in failed.text
    assert "wireguard_peer_transfer_rx_bytes{" not in failed.text


def test_unhandled_500_logs_only_type_and_middleware_counts_failure(
    app: FastAPI,
    http_node: PeerService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        http_node, "list", Mock(side_effect=RuntimeError("secret-token-and-key"))
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/peers", headers=AUTH)
        assert_error(response, 500, "internal_error")
        assert response.json()["detail"] == "Internal Server Error"
        assert "secret-token-and-key" not in response.text + caplog.text
        assert "Unhandled control-plane error (RuntimeError)" in caplog.text
        assert (
            'wireguard_api_requests_total{endpoint="/v1/peers",'
            'method="GET",status_code="500"} 1.0' in client.get("/metrics").text
        )
    assert all(record.exc_info is None for record in caplog.records)


def test_openapi_exact_response_models_and_error_schema(app: FastAPI) -> None:
    schema = app.openapi()
    paths = schema["paths"]
    expected = {
        ("/v1/peers", "get"): {"200": "PeerPage"},
        ("/v1/peers", "post"): {
            "201": "CreateResult",
            "200": "CreateResult",
            "202": "CreateResult",
        },
        ("/v1/peers/{peer_id}", "get"): {"200": "PeerView"},
        ("/v1/peers/{peer_id}", "delete"): {"202": "OperationRecord"},
        ("/v1/peers/{peer_id}/config-template", "get"): {"200": "ConfigTemplate"},
        ("/v1/operations/{operation_id}", "get"): {"200": "OperationRecord"},
        ("/v1/server", "get"): {"200": "ServerView"},
        ("/readyz", "get"): {"200": "ReadinessStatus", "503": "ReadinessStatus"},
        ("/metrics", "get"): {"503": "ErrorResponse"},
    }
    for (path, method), responses in expected.items():
        operation = paths[path][method]
        for status, model in responses.items():
            assert operation["responses"][status]["content"]["application/json"][
                "schema"
            ] == {"$ref": f"#/components/schemas/{model}"}
        if path.startswith("/v1"):
            assert operation["security"] == [{"APIKeyHeader": []}]
            for status in ("401", "403", "404", "409", "422", "503"):
                assert operation["responses"][status]["content"]["application/json"][
                    "schema"
                ] == {"$ref": "#/components/schemas/ErrorResponse"}
    assert "content" not in paths["/v1/peers/{peer_id}"]["delete"]["responses"]["204"]
    assert schema["components"]["schemas"]["ErrorResponse"]["properties"] == {
        "code": {"type": "string", "title": "Code"},
        "detail": {"type": "string", "title": "Detail"},
    }
    header = next(
        item
        for item in paths["/v1/peers"]["post"]["parameters"]
        if item["name"] == "Idempotency-Key"
    )
    assert header["required"] and header["in"] == "header"
    assert not any(path.startswith("/peers") for path in paths)


def test_import_does_not_load_settings_or_construct_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        load = Mock(side_effect=AssertionError("Import loaded settings"))
        store = Mock(side_effect=AssertionError("Import constructed storage"))
        backend = Mock(side_effect=AssertionError("Import constructed WireGuard"))
        patch.setattr(Settings, "load", load)
        patch.setattr("storage.Store", store)
        patch.setattr("wireguard.WireGuard", backend)
        importlib.reload(api)
        load.assert_not_called()
        store.assert_not_called()
        backend.assert_not_called()
    importlib.reload(api)


@pytest.mark.parametrize("explicit_settings", [False, True])
def test_factory_loads_settings_and_initializes_only_during_lifespan(
    http_node: PeerService,
    http_kernel: HTTPWireGuard,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_settings: bool,
) -> None:
    settings = http_node.settings.model_copy(update={"data_dir": tmp_path / "factory"})
    load = Mock(return_value=settings)
    backend = Mock(return_value=http_kernel)
    monkeypatch.setattr(Settings, "load", load)
    monkeypatch.setattr(api, "WireGuard", backend)
    app = api.create_app(settings=settings if explicit_settings else None)
    assert not settings.data_dir.exists()
    assert load.call_count == int(not explicit_settings)
    backend.assert_called_once_with(settings.interface, settings.command_timeout)
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
        assert app.state.service.store.path == settings.data_dir / "peers.sqlite3"
        assert app.state.service.store.path.exists()


def test_bad_storage_fails_startup(
    http_node: PeerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        http_node.store,
        "initialize",
        Mock(side_effect=StorageError("Storage unavailable")),
    )
    with (
        pytest.raises(StorageError, match="Storage unavailable"),
        TestClient(api.create_app(service=http_node)),
    ):
        pytest.fail("Startup must not serve with unusable storage")


def test_unavailable_startup_retries_in_worker_and_recovers(
    http_node: PeerService,
    http_kernel: HTTPWireGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_node.settings = http_node.settings.model_copy(
        update={"reconcile_interval": 0.01}
    )
    http_kernel.fail_observe = True
    with TestClient(api.create_app(service=http_node)) as client:
        assert client.get("/livez").status_code == 200
        assert client.get("/readyz").status_code == 503
        # Reconciliation also repairs an unexpected kernel peer after recovery.
        http_kernel.peers[public_key(999)] = PeerStats(
            public_key(999), ("10.13.13.99/32",), None, None, 0, 0, 0
        )
        recovered = threading.Event()
        original = http_kernel.remove_peer

        def remove(key: str) -> None:
            original(key)
            recovered.set()

        monkeypatch.setattr(http_kernel, "remove_peer", remove)
        http_kernel.fail_observe = False
        assert recovered.wait(2)
        assert client.get("/readyz").status_code == 200


def test_reconciliation_loop_retries_safe_failure_and_stops(
    http_node: PeerService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    threads: list[int] = []

    async def exercise() -> None:
        stop = asyncio.Event()
        event_thread = threading.get_ident()
        calls = 0

        def reconcile() -> None:
            nonlocal calls
            calls += 1
            threads.append(threading.get_ident())
            if calls == 1:
                raise WireGuardError("secret-reconciliation-diagnostic")
            loop.call_soon_threadsafe(stop.set)

        monkeypatch.setattr(http_node, "reconcile", reconcile)
        http_node.settings = http_node.settings.model_copy(
            update={"reconcile_interval": 0.001}
        )
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(api.reconciliation_loop(http_node, stop), timeout=2)
        assert calls == 2 and all(thread != event_thread for thread in threads)
        await api.reconciliation_loop(http_node, stop)

    asyncio.run(exercise())
    assert "WireGuard reconciliation will be retried" in caplog.text
    assert "secret-reconciliation-diagnostic" not in caplog.text


def test_shutdown_waits_for_pending_worker(
    http_node: PeerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release, completed = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    calls = 0

    def reconcile() -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            started.set()
            assert release.wait(2)
            completed.set()

    monkeypatch.setattr(http_node, "reconcile", reconcile)
    http_node.settings = http_node.settings.model_copy(
        update={"reconcile_interval": 0.001}
    )
    client = TestClient(api.create_app(service=http_node))
    client.__enter__()
    try:
        assert started.wait(2)
        with ThreadPoolExecutor(max_workers=1) as executor:
            closing = executor.submit(client.__exit__, None, None, None)
            try:
                assert not closing.done()
            finally:
                release.set()
            closing.result(timeout=2)
        assert completed.is_set()
    finally:
        release.set()


def test_blocking_lifecycle_and_http_service_calls_run_in_workers(
    http_node: PeerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, list[int]] = {}

    def track(name: str, function: Callable[..., object]) -> Callable[..., object]:
        def invoke(*args: object, **kwargs: object) -> object:
            calls.setdefault(name, []).append(threading.get_ident())
            return function(*args, **kwargs)

        return invoke

    names = (
        "initialize",
        "is_ready",
        "snapshot",
        "list",
        "create",
        "get",
        "template",
        "server",
        "operation",
        "delete",
    )
    for name in names:
        monkeypatch.setattr(http_node, name, track(name, getattr(http_node, name)))
    app = api.create_app(service=http_node)

    @app.get("/execution-thread")
    async def execution_thread() -> dict[str, int]:
        return {"id": threading.get_ident()}

    with TestClient(app) as client:
        event_thread = client.get("/execution-thread").json()["id"]
        created = client.post(
            "/v1/peers", headers=CREATE_HEADERS, json={"key_mode": "generated"}
        )
        assert created.status_code == 201
        location = created.headers["Location"]
        for path in (
            "/v1/peers",
            location,
            f"{location}/config-template",
            "/v1/server",
            f"/v1/operations/{created.json()['operation']['id']}",
            "/readyz",
            "/metrics",
        ):
            assert client.get(path, headers=AUTH).status_code == 200
        assert client.delete(location, headers=AUTH).status_code == 204
    assert set(calls) == set(names)
    assert all(
        thread != event_thread for threads in calls.values() for thread in threads
    )


def test_blocked_management_request_does_not_block_liveness(
    app: FastAPI,
    http_node: PeerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()
    original = http_node.list

    def blocked_list(limit: int, after: str | None) -> PeerPage:
        started.set()
        assert release.wait(2)
        return original(limit, after)

    monkeypatch.setattr(http_node, "list", blocked_list)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as executor:
        request = executor.submit(client.get, "/v1/peers", headers=AUTH)
        try:
            assert started.wait(2)
            assert client.get("/livez").status_code == 200
            assert not request.done()
        finally:
            release.set()
        assert request.result(timeout=2).status_code == 200
