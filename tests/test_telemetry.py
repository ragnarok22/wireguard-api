"""Cached endpoints are independent of request cadence and collector failures."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_system_info import ResourceReader

from api import create_app
from errors import StorageError, TelemetryError, WireGuardError
from models import PeerCreate
from system_info import SystemCollector
from telemetry import Telemetry

AUTH = {"X-API-Token": "test-secret"}


@pytest.fixture
def telemetry(http_node):
    http_node.settings = http_node.settings.model_copy(
        update={"telemetry_interval": 30}
    )
    return Telemetry(
        http_node,
        SystemCollector(
            http_node.settings.data_dir, http_node.started_at, ResourceReader()
        ),
    )


def test_warmup_freshness_and_read_caches(telemetry, monkeypatch) -> None:
    with pytest.raises(TelemetryError, match="warming up"):
        telemetry.stats()
    with pytest.raises(TelemetryError, match="warming up"):
        telemetry.system()
    telemetry.sample_vpn()
    telemetry.sample_system()
    assert telemetry.stats().peers.registered == 0
    assert telemetry.system().cpu.status == "warming_up"
    stats_time = telemetry._vpn.sample.monotonic_at
    system_time = telemetry._system[1]
    monkeypatch.setattr(
        "telemetry.time.monotonic",
        lambda: max(stats_time, system_time) + telemetry.max_age + 0.01,
    )
    with pytest.raises(TelemetryError, match="stale"):
        telemetry.stats()
    with pytest.raises(TelemetryError, match="stale"):
        telemetry.system()


@pytest.mark.parametrize(
    "error",
    [
        WireGuardError("private-output"),
        StorageError("private-output"),
        RuntimeError("private-output"),
    ],
)
def test_vpn_failure_invalidates_cache_and_rates_but_system_stays_available(
    telemetry, monkeypatch, caplog, error
) -> None:
    telemetry.sample_vpn()
    telemetry.sample_vpn()
    telemetry.sample_system()
    original = telemetry.service.stats_sample
    monkeypatch.setattr(telemetry.service, "stats_sample", Mock(side_effect=error))
    telemetry.sample_vpn()
    with pytest.raises(TelemetryError) as caught:
        telemetry.stats()
    assert caught.value.code == getattr(error, "code", "telemetry_unavailable")
    assert (
        "private-output" not in str(caught.value)
        and "private-output" not in caplog.text
    )
    assert telemetry.system().memory.used_bytes == 64
    monkeypatch.setattr(telemetry.service, "stats_sample", original)
    telemetry.sample_vpn()
    assert telemetry.stats().traffic.rx_bytes_per_second is None


def test_system_failure_invalidates_cache_and_baseline_but_vpn_stays_available(
    telemetry, monkeypatch, caplog
) -> None:
    telemetry.sample_vpn()
    telemetry.sample_system()
    original = telemetry.system_collector.sample
    monkeypatch.setattr(
        telemetry.system_collector, "sample", Mock(side_effect=OSError("secret path"))
    )
    telemetry.sample_system()
    with pytest.raises(TelemetryError, match="System statistics are unavailable"):
        telemetry.system()
    assert telemetry.stats().peers.registered == 0
    assert "secret path" not in caplog.text
    monkeypatch.setattr(telemetry.system_collector, "sample", original)
    telemetry.sample_system()
    assert telemetry.system().cpu.status == "warming_up"


def test_system_response_is_detached_and_age_and_uptime_advance(
    telemetry, monkeypatch
) -> None:
    telemetry.sample_system()
    observed = telemetry._system[1]
    monkeypatch.setattr("telemetry.time.monotonic", lambda: observed + 2)
    first = telemetry.system()
    assert first.sample.age_seconds == 2
    assert first.runtime.uptime_seconds == observed + 2 - telemetry.service.started_at
    first.cpu.capacity_cores = 999
    assert telemetry.system().cpu.capacity_cores == 0.5
    monkeypatch.setattr("telemetry.time.monotonic", lambda: observed - 1)
    assert telemetry.system().sample.age_seconds == 0


def test_endpoints_read_samples_without_running_collectors(
    telemetry, monkeypatch
) -> None:
    with TestClient(
        create_app(service=telemetry.service, telemetry=telemetry)
    ) as client:
        stats = Mock(side_effect=AssertionError("HTTP must not sample VPN"))
        system = Mock(side_effect=AssertionError("HTTP must not sample resources"))
        monkeypatch.setattr(telemetry.service, "stats_sample", stats)
        monkeypatch.setattr(telemetry.system_collector, "sample", system)
        for path in ("/v1/stats", "/v1/system"):
            response = client.get(path, headers=AUTH)
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
        assert not stats.called and not system.called
        schema = client.get("/openapi.json").json()
        assert "VpnStats" in schema["components"]["schemas"]
        assert "SystemInfo" in schema["components"]["schemas"]


@pytest.mark.parametrize("path", ["/v1/stats", "/v1/system"])
@pytest.mark.parametrize("headers,status", [({}, 401), ({"X-API-Token": "wrong"}, 403)])
def test_endpoints_require_authentication(telemetry, path, headers, status) -> None:
    with TestClient(
        create_app(service=telemetry.service, telemetry=telemetry)
    ) as client:
        assert client.get(path, headers=headers).status_code == status


@pytest.mark.parametrize("value", ["0", "3601", "not-an-integer"])
def test_handshake_window_is_bounded(telemetry, value) -> None:
    with TestClient(
        create_app(service=telemetry.service, telemetry=telemetry)
    ) as client:
        response = client.get(
            f"/v1/stats?handshake_window_seconds={value}", headers=AUTH
        )
        assert (
            response.status_code == 422 and response.json()["code"] == "invalid_input"
        )


def test_http_summary_reflects_pending_deletion_without_mutating_it(
    telemetry, http_kernel
) -> None:
    node = telemetry.service
    created = node.create(PeerCreate(key_mode="generated"), "test")
    http_kernel.fail_remove = True
    assert node.delete(created.peer.id).status == "pending"
    with TestClient(create_app(service=node, telemetry=telemetry)) as client:
        response = client.get("/v1/stats?handshake_window_seconds=60", headers=AUTH)
        result = response.json()
        assert result["peers"]["deleting"] == 1
        assert result["pool"]["reserved"] == 1
        assert result["pending_operations"] == 1
        assert result["handshakes"]["window_seconds"] == 60
        assert result["traffic"]["rx_bytes"] == 100
        assert node.store.get_peer(created.peer.id).state == "deleting"


def test_http_failure_is_safe_and_does_not_make_system_or_liveness_fail(
    telemetry, http_kernel
) -> None:
    with TestClient(
        create_app(service=telemetry.service, telemetry=telemetry)
    ) as client:
        http_kernel.fail_observe = True
        telemetry.sample_vpn()
        result = client.get("/v1/stats", headers=AUTH)
        assert result.status_code == 503
        assert result.json() == {
            "code": "wireguard_unavailable",
            "detail": "VPN statistics are unavailable",
        }
        assert client.get("/v1/system", headers=AUTH).status_code == 200
        assert client.get("/livez").status_code == 200


def test_background_loops_use_workers_and_stop_cleanly(telemetry, monkeypatch) -> None:
    telemetry.interval = 0.001
    calls = {"vpn": 0, "system": 0}

    async def exercise() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        event_thread = threading.get_ident()

        def sample(component):
            assert threading.get_ident() != event_thread
            calls[component] += 1
            if all(calls.values()):
                loop.call_soon_threadsafe(stop.set)

        monkeypatch.setattr(telemetry, "sample_vpn", lambda: sample("vpn"))
        monkeypatch.setattr(telemetry, "sample_system", lambda: sample("system"))
        await asyncio.wait_for(
            asyncio.gather(telemetry.loop("vpn", stop), telemetry.loop("system", stop)),
            timeout=2,
        )
        await telemetry.loop("vpn", stop)

    asyncio.run(exercise())
    assert calls["vpn"] and calls["system"]


def test_slow_vpn_sampling_does_not_block_cached_system_or_liveness(
    telemetry, monkeypatch
) -> None:
    started, release = threading.Event(), threading.Event()
    original = telemetry.service.stats_sample

    def blocked():
        started.set()
        assert release.wait(2)
        return original()

    with TestClient(
        create_app(service=telemetry.service, telemetry=telemetry)
    ) as client:
        monkeypatch.setattr(telemetry.service, "stats_sample", blocked)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(telemetry.sample_vpn)
            try:
                assert started.wait(2)
                assert client.get("/v1/system", headers=AUTH).status_code == 200
                assert client.get("/livez").status_code == 200
                assert not future.done()
            finally:
                release.set()
            future.result(timeout=2)


def test_successful_polling_recovers_vpn_without_http_sampling(
    http_node, http_kernel
) -> None:
    telemetry = Telemetry(http_node)
    telemetry.interval = 0.01
    http_kernel.fail_observe = True
    with TestClient(create_app(service=http_node, telemetry=telemetry)) as client:
        assert client.get("/v1/stats", headers=AUTH).status_code == 503
        http_kernel.fail_observe = False
        deadline = time.monotonic() + 2
        while client.get("/v1/stats", headers=AUTH).status_code != 200:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert telemetry.stats().sample.age_seconds < 1
