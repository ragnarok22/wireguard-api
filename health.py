import time

from pydantic import BaseModel

from wireguard import WireGuard

# Track application start time
_start_time = time.time()


class HealthStatus(BaseModel):
    status: str
    version: str
    uptime_seconds: float
    wireguard_interface: str
    wireguard_available: bool
    peer_count: int


def check_health(wg: WireGuard, version: str) -> tuple[HealthStatus, int]:
    """
    Check the health of the WireGuard API service.

    Returns a tuple of (HealthStatus, HTTP status code).
    Returns 200 if healthy, 503 if unhealthy.
    """
    uptime = time.time() - _start_time
    wireguard_available = False
    peer_count = 0

    try:
        peers = wg.list_peers()
        wireguard_available = True
        peer_count = len(peers)
    except Exception:
        wireguard_available = False

    status = "healthy" if wireguard_available else "unhealthy"
    http_code = 200 if wireguard_available else 503

    return (
        HealthStatus(
            status=status,
            version=version,
            uptime_seconds=round(uptime, 1),
            wireguard_interface=wg.interface,
            wireguard_available=wireguard_available,
            peer_count=peer_count,
        ),
        http_code,
    )
