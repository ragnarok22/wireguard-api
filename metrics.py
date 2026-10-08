"""Per-application Prometheus collectors and streaming-safe request metrics."""

from threading import Lock
from time import perf_counter

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from wireguard import Snapshot

_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "DELETE", "CONNECT", "OPTIONS", "TRACE", "PATCH"}
)
_SKIPPED_PATHS = frozenset({"/metrics", "/livez", "/readyz"})


class Metrics:
    """Own all collectors and serialize scrapes with request updates."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self._lock = Lock()
        self._peer_keys: set[str] = set()
        self._requests = Counter(
            "wireguard_api_requests_total",
            "Total HTTP requests",
            ["method", "endpoint", "status_code"],
            registry=self.registry,
        )
        self._duration = Histogram(
            "wireguard_api_request_duration_seconds",
            "HTTP request duration in seconds",
            ["method", "endpoint"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
            registry=self.registry,
        )
        self._available = Gauge(
            "wireguard_available",
            "Whether the WireGuard snapshot is available",
            registry=self.registry,
        )
        self._peers = Gauge(
            "wireguard_peers_total",
            "Current number of WireGuard peers; NaN when unavailable",
            registry=self.registry,
        )
        self._pending = Gauge(
            "wireguard_pending_operations",
            "Number of pending peer operations",
            registry=self.registry,
        )
        self._rx = Gauge(
            "wireguard_peer_transfer_rx_bytes",
            "Received bytes for WireGuard peer",
            ["public_key"],
            registry=self.registry,
        )
        self._tx = Gauge(
            "wireguard_peer_transfer_tx_bytes",
            "Transmitted bytes for WireGuard peer",
            ["public_key"],
            registry=self.registry,
        )
        self._handshake = Gauge(
            "wireguard_peer_last_handshake_timestamp_seconds",
            "Unix timestamp of the last handshake; zero when never established",
            ["public_key"],
            registry=self.registry,
        )

    def observe_request(
        self, method: str, endpoint: str, status_code: int, duration: float
    ) -> None:
        with self._lock:
            self._requests.labels(method, endpoint, str(status_code)).inc()
            self._duration.labels(method, endpoint).observe(duration)

    def render(self, snapshot: Snapshot | None, pending: int) -> bytes:
        """Publish a coherent snapshot, removing unavailable or vanished peers."""
        with self._lock:
            peers = snapshot.peers if snapshot is not None else {}
            current_keys = set(peers)
            for key in self._peer_keys - current_keys:
                self._rx.remove(key)
                self._tx.remove(key)
                self._handshake.remove(key)
            self._peer_keys = current_keys
            self._available.set(int(snapshot is not None))
            self._peers.set(len(peers) if snapshot is not None else float("nan"))
            self._pending.set(pending)
            for peer in peers.values():
                self._rx.labels(peer.public_key).set(peer.transfer_rx)
                self._tx.labels(peer.public_key).set(peer.transfer_tx)
                self._handshake.labels(peer.public_key).set(peer.latest_handshake or 0)
            return generate_latest(self.registry)


class MetricsMiddleware:
    """Measure the complete ASGI response without buffering streaming bodies."""

    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _SKIPPED_PATHS:
            await self.app(scope, receive, send)
            return

        method = scope["method"] if scope["method"] in _METHODS else "OTHER"
        status_code = 500
        started = perf_counter()

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        except BaseException:
            status_code = 500
            raise
        finally:
            path = getattr(scope.get("route"), "path", None)
            endpoint = path if isinstance(path, str) else "__unmatched__"
            self.metrics.observe_request(
                method, endpoint, status_code, perf_counter() - started
            )
