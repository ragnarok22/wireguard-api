import re
import time
from collections.abc import Callable

from prometheus_client import Counter, Gauge, Histogram
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from wireguard import WireGuard

# Request metrics
REQUEST_COUNT = Counter(
    "wireguard_api_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"],
)

REQUEST_DURATION = Histogram(
    "wireguard_api_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# WireGuard metrics
PEERS_TOTAL = Gauge(
    "wireguard_peers_total",
    "Current number of WireGuard peers",
)

PEER_TRANSFER_RX = Gauge(
    "wireguard_peer_transfer_rx_bytes",
    "Received bytes for WireGuard peer",
    ["public_key"],
)

PEER_TRANSFER_TX = Gauge(
    "wireguard_peer_transfer_tx_bytes",
    "Transmitted bytes for WireGuard peer",
    ["public_key"],
)

PEER_LAST_HANDSHAKE = Gauge(
    "wireguard_peer_last_handshake_seconds",
    "Seconds since last handshake for WireGuard peer",
    ["public_key"],
)


# Path patterns for normalization to avoid high-cardinality labels
PATH_PATTERNS = [
    (re.compile(r"^/peers/[^/]+/config$"), "/peers/{public_key}/config"),
    (re.compile(r"^/peers/[^/]+$"), "/peers/{public_key}"),
]


def normalize_path(path: str) -> str:
    """
    Normalize request paths to reduce label cardinality.
    Replaces dynamic segments like public keys with placeholders.
    """
    for pattern, replacement in PATH_PATTERNS:
        if pattern.match(path):
            return replacement
    return path


class MetricsMiddleware(BaseHTTPMiddleware):
    """
    Middleware to collect request metrics for Prometheus.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Response]
    ) -> Response:
        # Skip metrics for /metrics and /health endpoints to avoid recursion
        if request.url.path in ("/metrics", "/health"):
            return await call_next(request)

        method = request.method
        endpoint = normalize_path(request.url.path)

        start_time = time.time()
        response = await call_next(request)
        duration = time.time() - start_time

        status_code = str(response.status_code)

        REQUEST_COUNT.labels(
            method=method,
            endpoint=endpoint,
            status_code=status_code,
        ).inc()

        REQUEST_DURATION.labels(
            method=method,
            endpoint=endpoint,
        ).observe(duration)

        return response


def update_wireguard_metrics(wg: WireGuard) -> None:
    """
    Update WireGuard-specific Prometheus metrics.
    Call this before serving /metrics to get current stats.
    """
    try:
        peers = wg.list_peers()
        PEERS_TOTAL.set(len(peers))

        current_time = time.time()

        for public_key, data in peers.items():
            # Transfer RX (bytes)
            try:
                rx_bytes = int(data.get("transfer_rx", 0))
                PEER_TRANSFER_RX.labels(public_key=public_key).set(rx_bytes)
            except (ValueError, TypeError):
                PEER_TRANSFER_RX.labels(public_key=public_key).set(0)

            # Transfer TX (bytes)
            try:
                tx_bytes = int(data.get("transfer_tx", 0))
                PEER_TRANSFER_TX.labels(public_key=public_key).set(tx_bytes)
            except (ValueError, TypeError):
                PEER_TRANSFER_TX.labels(public_key=public_key).set(0)

            # Last handshake (convert epoch to seconds ago)
            try:
                last_handshake = int(data.get("latest_handshake", 0))
                if last_handshake > 0:
                    seconds_ago = current_time - last_handshake
                    PEER_LAST_HANDSHAKE.labels(public_key=public_key).set(seconds_ago)
                else:
                    # No handshake yet
                    PEER_LAST_HANDSHAKE.labels(public_key=public_key).set(-1)
            except (ValueError, TypeError):
                PEER_LAST_HANDSHAKE.labels(public_key=public_key).set(-1)

    except Exception:
        # If we can't get peer info, set peers to 0
        PEERS_TOTAL.set(0)
