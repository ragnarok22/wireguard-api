"""Exercise exported samples and the full ASGI middleware lifecycle."""

import asyncio
import math
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from prometheus_client import REGISTRY
from prometheus_client.parser import text_string_to_metric_families
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from metrics import Metrics, MetricsMiddleware
from wireguard import PeerStats, Snapshot

_TRANSFER_RX = "wireguard_peer_transfer_rx_bytes"
_TRANSFER_TX = "wireguard_peer_transfer_tx_bytes"
_HANDSHAKE = "wireguard_peer_last_handshake_timestamp_seconds"
_COUNT = "wireguard_api_requests_total"
_DURATION = "wireguard_api_request_duration_seconds"
_PEER = PeerStats(
    public_key="peer-one=",
    allowed_ips=("10.0.0.2/32",),
    endpoint="secret-endpoint:51820",
    latest_handshake=1_700_000_000,
    transfer_rx=123,
    transfer_tx=456,
    persistent_keepalive=25,
)


def snapshot(*peers: PeerStats) -> Snapshot:
    return Snapshot("server-public-key", {peer.public_key: peer for peer in peers})


def samples(payload: bytes, name: str) -> list[tuple[dict[str, str], float]]:
    return [
        (sample.labels, sample.value)
        for family in text_string_to_metric_families(payload.decode())
        for sample in family.samples
        if sample.name == name
    ]


def scope(path: str = "/peers", method: str = "GET") -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("localhost", 8008),
    }


async def invoke(app: ASGIApp, request_scope: Scope) -> list[Message]:
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    await app(request_scope, receive, send)
    return messages


async def peer_response(request: Request) -> Response:
    return Response(request.path_params["public_key"])


def make_app(metrics: Metrics) -> MetricsMiddleware:
    app = FastAPI()
    app.add_api_route("/peers/{public_key}", peer_response, methods=["GET"])
    return MetricsMiddleware(app, metrics)


def test_render_exports_typed_snapshot_and_pending_without_sensitive_fields() -> None:
    metrics = Metrics()
    payload = metrics.render(snapshot(_PEER), pending=3)
    assert samples(payload, "wireguard_available") == [({}, 1)]
    assert samples(payload, "wireguard_peers_total") == [({}, 1)]
    assert samples(payload, "wireguard_pending_operations") == [({}, 3)]
    assert samples(payload, _TRANSFER_RX) == [({"public_key": "peer-one="}, 123)]
    assert samples(payload, _TRANSFER_TX) == [({"public_key": "peer-one="}, 456)]
    assert samples(payload, _HANDSHAKE) == [
        ({"public_key": "peer-one="}, 1_700_000_000)
    ]
    for forbidden in (
        b"secret-endpoint",
        b"server-public-key",
        b"10.0.0.2",
        b"keepalive",
    ):
        assert forbidden not in payload


@pytest.mark.parametrize("handshake", [None, 0])
def test_never_handshaken_peer_uses_zero(handshake: int | None) -> None:
    payload = Metrics().render(snapshot(replace(_PEER, latest_handshake=handshake)), 0)
    assert samples(payload, _HANDSHAKE) == [({"public_key": "peer-one="}, 0)]


def test_removed_peers_disappear_and_remaining_values_refresh() -> None:
    metrics = Metrics()
    other = replace(_PEER, public_key="peer-two=", transfer_rx=9)
    metrics.render(snapshot(_PEER, other), 2)
    payload = metrics.render(snapshot(replace(other, transfer_rx=10)), 1)
    assert samples(payload, "wireguard_peers_total") == [({}, 1)]
    assert samples(payload, _TRANSFER_RX) == [({"public_key": "peer-two="}, 10)]
    for name in (_TRANSFER_TX, _HANDSHAKE):
        assert [labels for labels, _ in samples(payload, name)] == [
            {"public_key": "peer-two="}
        ]
    payload = metrics.render(snapshot(), 0)
    assert samples(payload, "wireguard_peers_total") == [({}, 0)]
    for name in (_TRANSFER_RX, _TRANSFER_TX, _HANDSHAKE):
        assert samples(payload, name) == []


def test_failed_snapshot_clears_labels_and_is_distinct_from_empty_success() -> None:
    metrics = Metrics()
    metrics.render(snapshot(_PEER), 0)
    payload = metrics.render(None, 4)
    assert samples(payload, "wireguard_available") == [({}, 0)]
    assert math.isnan(samples(payload, "wireguard_peers_total")[0][1])
    assert samples(payload, "wireguard_pending_operations") == [({}, 4)]
    for name in (_TRANSFER_RX, _TRANSFER_TX, _HANDSHAKE):
        assert samples(payload, name) == []
    recovered = metrics.render(snapshot(_PEER), 0)
    assert samples(recovered, "wireguard_available") == [({}, 1)]
    assert samples(recovered, _TRANSFER_RX) == [({"public_key": "peer-one="}, 123)]


def test_multiple_apps_isolate_requests_peers_and_default_registry() -> None:
    before = {metric.name for metric in REGISTRY.collect()}
    first, second = Metrics(), Metrics()
    asyncio.run(invoke(make_app(first), scope("/peers/one")))
    asyncio.run(invoke(make_app(first), scope("/peers/two")))
    asyncio.run(invoke(make_app(second), scope("/peers/three")))
    first_payload = first.render(snapshot(_PEER), 1)
    second_payload = second.render(snapshot(), 0)
    assert samples(first_payload, _COUNT)[0][1] == 2
    assert samples(second_payload, _COUNT)[0][1] == 1
    assert samples(second_payload, _TRANSFER_RX) == []
    assert first.registry is not second.registry
    assert {metric.name for metric in REGISTRY.collect()} == before


def test_unknown_routes_and_methods_have_bounded_labels() -> None:
    metrics = Metrics()
    app = make_app(metrics)
    for index in range(30):
        messages = asyncio.run(
            invoke(app, scope(f"/unknown/{index}", f"CUSTOM{index}"))
        )
        assert messages[0]["status"] == 404
    assert samples(metrics.render(snapshot(), 0), _COUNT) == [
        ({"method": "OTHER", "endpoint": "__unmatched__", "status_code": "404"}, 30)
    ]


def test_route_template_is_read_after_routing_and_requests_share_labels() -> None:
    metrics = Metrics()
    app = make_app(metrics)
    for key in ("one", "two"):
        request_scope = scope(f"/peers/{key}")
        assert "route" not in request_scope
        asyncio.run(invoke(app, request_scope))
    payload = metrics.render(snapshot(), 0)
    assert samples(payload, _COUNT) == [
        ({"method": "GET", "endpoint": "/peers/{public_key}", "status_code": "200"}, 2)
    ]
    assert samples(payload, _DURATION + "_count") == [
        ({"method": "GET", "endpoint": "/peers/{public_key}"}, 2)
    ]


@pytest.mark.parametrize("start_response", [False, True])
def test_exceptions_are_counted_as_500_and_propagated(start_response: bool) -> None:
    metrics = Metrics()
    error = RuntimeError("secret exception details")

    async def failing_app(request_scope: Scope, receive: Receive, send: Send) -> None:
        if start_response:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        raise error

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(invoke(MetricsMiddleware(failing_app, metrics), scope()))
    assert raised.value is error
    payload = metrics.render(None, 0)
    assert samples(payload, _COUNT) == [
        ({"method": "GET", "endpoint": "__unmatched__", "status_code": "500"}, 1)
    ]
    assert samples(payload, _DURATION + "_count")[0][1] == 1
    assert b"secret exception details" not in payload


def test_streaming_duration_includes_last_body_and_forwards_messages() -> None:
    metrics = Metrics()
    clock = iter((10.0, 15.0))
    expected: list[Message] = [
        {"type": "http.response.start", "status": 201, "headers": []},
        {"type": "http.response.body", "body": b"first", "more_body": True},
        {"type": "http.response.body", "body": b"last", "more_body": False},
    ]

    async def streaming_app(request_scope: Scope, receive: Receive, send: Send) -> None:
        for message in expected:
            assert samples(metrics.render(None, 0), _COUNT) == []
            await send(message)
            await asyncio.sleep(0)

    with patch("metrics.perf_counter", side_effect=lambda: next(clock)):
        messages = asyncio.run(
            invoke(MetricsMiddleware(streaming_app, metrics), scope())
        )
    assert messages == expected
    payload = metrics.render(None, 0)
    assert samples(payload, _DURATION + "_sum") == [
        ({"method": "GET", "endpoint": "__unmatched__"}, 5)
    ]
    assert samples(payload, _COUNT)[0][0]["status_code"] == "201"


@pytest.mark.parametrize("path", ["/metrics", "/livez", "/readyz"])
def test_operational_endpoints_are_passed_through_without_observation(
    path: str,
) -> None:
    metrics = Metrics()
    messages = asyncio.run(invoke(make_app(metrics), scope(path)))
    assert messages[0]["status"] == 404
    assert samples(metrics.render(None, 0), _COUNT) == []


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
def test_non_http_scopes_are_passed_through(scope_type: str) -> None:
    metrics = Metrics()
    request_scope: Scope = {"type": scope_type}
    observed: list[Scope] = []

    async def app(inner_scope: Scope, receive: Receive, send: Send) -> None:
        observed.append(inner_scope)
        await send({"type": "lifespan.startup.complete"})

    messages = asyncio.run(invoke(MetricsMiddleware(app, metrics), request_scope))
    assert observed == [request_scope]
    assert messages == [{"type": "lifespan.startup.complete"}]
    assert samples(metrics.render(None, 0), _COUNT) == []


def test_invalid_route_metadata_uses_unmatched_label() -> None:
    metrics = Metrics()
    request_scope = scope("/untrusted-url")
    request_scope["route"] = object()
    asyncio.run(invoke(make_app(metrics), request_scope))
    assert samples(metrics.render(None, 0), _COUNT)[0][0]["endpoint"] == "__unmatched__"


def test_concurrent_renders_and_requests_keep_scrapes_coherent() -> None:
    metrics = Metrics()

    def render_and_observe(index: int) -> bytes:
        metrics.observe_request("GET", "/peers", 200, 0.01)
        peer = replace(_PEER, public_key=f"peer-{index}")
        return metrics.render(snapshot(peer), index)

    with ThreadPoolExecutor(max_workers=8) as executor:
        payloads: Iterator[bytes] = executor.map(render_and_observe, range(40))
        for index, payload in enumerate(payloads):
            assert samples(payload, _TRANSFER_RX) == [
                ({"public_key": f"peer-{index}"}, 123)
            ]
            assert samples(payload, "wireguard_pending_operations") == [({}, index)]
    assert samples(metrics.render(snapshot(), 0), _COUNT)[0][1] == 40
