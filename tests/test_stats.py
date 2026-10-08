"""Rates use comparable peer counters, never a misleading aggregate subtraction."""

from dataclasses import replace
from ipaddress import IPv4Interface

import pytest
from conftest import public_key

from settings import Settings
from stats import StatsReading, VpnSample, aggregate, sample_rates
from storage import PeerRecord
from wireguard import PeerStats, Snapshot


def peer(number: int, rx: int, tx: int, handshake: int | None = None) -> PeerStats:
    return PeerStats(
        public_key(number), (f"10.0.0.{number}/32",), None, handshake, rx, tx, 0
    )


def sample(
    *peers: PeerStats, monotonic: float = 10, records: tuple[PeerRecord, ...] = ()
) -> VpnSample:
    return VpnSample(
        Snapshot(public_key(99), {item.public_key: item for item in peers}),
        records,
        2,
        1000,
        monotonic,
    )


def test_first_sample_and_idle_interface() -> None:
    first = sample_rates(sample(), None)
    assert first.rx_rate is None and first.tx_rate is None
    assert first.interval_seconds is None
    idle = sample_rates(sample(monotonic=12), sample())
    assert (idle.rx_rate, idle.tx_rate, idle.interval_seconds) == (0, 0, 2)


def test_rates_sum_per_peer_deltas_and_use_monotonic_time() -> None:
    previous = sample(peer(2, 100, 1000), peer(3, 200, 2000))
    current = sample(peer(2, 150, 1040), peer(3, 250, 2060), monotonic=12.5)
    current = replace(current, sampled_at=1)  # Wall-clock adjustment is irrelevant.
    result = sample_rates(current, previous)
    assert result.rx_rate == 40
    assert result.tx_rate == 40
    assert result.interval_seconds == 2.5


@pytest.mark.parametrize("now", [9, 10])
def test_nonpositive_interval_cannot_produce_a_rate(now: float) -> None:
    result = sample_rates(
        sample(peer(2, 100, 200), monotonic=now), sample(peer(2, 1, 2))
    )
    assert result.rx_rate is None and result.interval_seconds is None


@pytest.mark.parametrize(
    "change", ["join", "leave", "server_key", "identity", "rx_reset", "tx_reset"]
)
def test_discontinuities_require_two_new_comparable_samples(change: str) -> None:
    record = PeerRecord("identity", public_key(2), "10.0.0.2", "active", 100)
    previous = sample(peer(2, 100, 200), records=(record,))
    current = sample(peer(2, 110, 220), monotonic=11, records=(record,))
    if change == "join":
        current = sample(
            peer(2, 110, 220), peer(3, 99999, 99999), monotonic=11, records=(record,)
        )
    elif change == "leave":
        current = sample(monotonic=11, records=(record,))
    elif change == "server_key":
        current = replace(
            current, snapshot=replace(current.snapshot, public_key=public_key(98))
        )
    elif change == "identity":
        current = replace(current, records=(replace(record, id="replacement"),))
    else:
        current = sample(
            peer(
                2,
                0 if change == "rx_reset" else 110,
                0 if change == "tx_reset" else 220,
            ),
            monotonic=11,
            records=(record,),
        )
    disrupted = sample_rates(current, previous)
    assert disrupted.rx_rate is None and disrupted.tx_rate is None
    recovered = sample_rates(replace(current, monotonic_at=12), current)
    assert recovered.rx_rate == 0 and recovered.tx_rate == 0


def test_summary_distinguishes_desired_applied_and_unmanaged_peers() -> None:
    records = (
        PeerRecord("a", public_key(2), "10.0.0.2", "active", 100),
        PeerRecord("b", public_key(3), "10.0.0.3", "pending", 100),
        PeerRecord("c", public_key(4), "10.0.0.4", "deleting", 100),
        PeerRecord("d", public_key(5), "10.0.0.5", "active", 100),
        PeerRecord("e", public_key(6), "10.0.0.6", "active", 100),
    )
    observed = sample(
        peer(2, 100, 200, 820),  # Inclusive 180-second boundary.
        peer(3, 50, 80),
        peer(4, 10, 20, 819),
        replace(peer(5, 1, 2, 1001), allowed_ips=("10.0.0.99/32",)),
        peer(7, 3, 4, 999),
        records=records,
    )
    settings = Settings.model_validate(
        {
            "api_token": "secret",
            "server_endpoint": "node.example.org",
            "server_address": IPv4Interface("10.0.0.1/24"),
        }
    )
    result = aggregate(StatsReading(observed, 1, 10, 20), settings, 1, 10.25, 180)
    assert result.peers.model_dump() == {
        "registered": 5,
        "active": 3,
        "pending": 1,
        "deleting": 1,
        "applied": 1,
        "observed": 5,
        "unmanaged": 1,
    }
    assert result.handshakes.recent == 2
    assert result.handshakes.never == 1
    assert result.handshakes.latest_at == 1001
    assert result.traffic.rx_bytes == 164 and result.traffic.tx_bytes == 306
    assert result.traffic.rx_bytes_per_second == 10
    assert result.traffic.scope == "current_interface"
    assert result.pool.available == 248 and result.pool.reserved == 5
    assert result.pending_operations == 2
    assert result.sample.age_seconds == 0.25
    assert result.uptime_seconds == 9.25
    assert (
        aggregate(
            StatsReading(observed, None, None, None), settings, 20, 9, 1
        ).handshakes.recent
        == 1
    )


def test_empty_summary_has_no_last_handshake(http_node) -> None:
    reading = sample_rates(sample(), None)
    result = aggregate(reading, http_node.settings, 10, 9, 180)
    assert result.handshakes.latest_at is None
    assert result.peers.registered == 0 and result.peers.observed == 0
    assert result.uptime_seconds == 0 and result.sample.age_seconds == 0


def test_service_sample_is_fresh_consistent_and_read_only(
    http_node, http_kernel
) -> None:
    before = http_node.snapshot()
    http_kernel.peers[public_key(10)] = peer(10, 1, 2)
    assert public_key(10) not in before.peers
    result = http_node.stats_sample()
    assert public_key(10) in result.snapshot.peers
    assert result.records == () and result.pending_operations == 0
    assert http_kernel.events == []
    assert result.sampled_at > 0 and result.monotonic_at > 0
