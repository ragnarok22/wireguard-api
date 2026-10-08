"""Pure VPN aggregation and comparable per-peer counter sampling."""

from dataclasses import dataclass

from models import (
    HandshakeSummary,
    PeerCounts,
    PoolSummary,
    SampleInfo,
    TrafficSummary,
    VpnStats,
)
from settings import Settings
from storage import PeerRecord
from version import VERSION
from wireguard import Snapshot


@dataclass(frozen=True)
class VpnSample:
    snapshot: Snapshot
    records: tuple[PeerRecord, ...]
    pending_operations: int
    sampled_at: float
    monotonic_at: float


@dataclass(frozen=True)
class StatsReading:
    sample: VpnSample
    interval_seconds: float | None
    rx_rate: float | None
    tx_rate: float | None


def sample_rates(current: VpnSample, previous: VpnSample | None) -> StatsReading:
    if previous is None or current.monotonic_at <= previous.monotonic_at:
        return StatsReading(current, None, None, None)
    interval = current.monotonic_at - previous.monotonic_at
    peers, old = current.snapshot.peers, previous.snapshot.peers
    identities = {peer.public_key: peer.id for peer in current.records}
    old_identities = {peer.public_key: peer.id for peer in previous.records}
    if (
        current.snapshot.public_key != previous.snapshot.public_key
        or set(peers) != set(old)
        or identities != old_identities
    ):
        return StatsReading(current, interval, None, None)
    rx = tx = 0
    for key, peer in peers.items():
        rx_delta = peer.transfer_rx - old[key].transfer_rx
        tx_delta = peer.transfer_tx - old[key].transfer_tx
        if rx_delta < 0 or tx_delta < 0:
            return StatsReading(current, interval, None, None)
        rx += rx_delta
        tx += tx_delta
    return StatsReading(current, interval, rx / interval, tx / interval)


def aggregate(
    reading: StatsReading,
    settings: Settings,
    started_at: float,
    now: float,
    window_seconds: int,
) -> VpnStats:
    sample = reading.sample
    observed = sample.snapshot.peers
    records = sample.records
    keys = {record.public_key for record in records}
    handshakes = [peer.latest_handshake for peer in observed.values()]
    capacity = settings.server_address.network.num_addresses - 3
    return VpnStats(
        version=VERSION,
        uptime_seconds=max(0, now - started_at),
        sample=SampleInfo(
            sampled_at=sample.sampled_at,
            age_seconds=max(0, now - sample.monotonic_at),
            interval_seconds=reading.interval_seconds,
        ),
        peers=PeerCounts(
            registered=len(records),
            active=sum(record.state == "active" for record in records),
            pending=sum(record.state == "pending" for record in records),
            deleting=sum(record.state == "deleting" for record in records),
            applied=sum(
                record.state == "active"
                and record.public_key in observed
                and observed[record.public_key].allowed_ips == (f"{record.address}/32",)
                for record in records
            ),
            observed=len(observed),
            unmanaged=len(observed.keys() - keys),
        ),
        handshakes=HandshakeSummary(
            recent=sum(
                handshake is not None
                and 0 <= sample.sampled_at - handshake <= window_seconds
                for handshake in handshakes
            ),
            never=sum(handshake is None for handshake in handshakes),
            latest_at=max(
                (value for value in handshakes if value is not None), default=None
            ),
            window_seconds=window_seconds,
        ),
        traffic=TrafficSummary(
            rx_bytes=sum(peer.transfer_rx for peer in observed.values()),
            tx_bytes=sum(peer.transfer_tx for peer in observed.values()),
            rx_bytes_per_second=reading.rx_rate,
            tx_bytes_per_second=reading.tx_rate,
        ),
        pool=PoolSummary(
            network=str(settings.server_address.network),
            capacity=capacity,
            reserved=len(records),
            available=capacity - len(records),
        ),
        pending_operations=sample.pending_operations,
    )
