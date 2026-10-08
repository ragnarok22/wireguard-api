"""Readiness requires durable storage and convergence, not an empty inventory."""

import time
from typing import Literal

from pydantic import BaseModel

from errors import ControlPlaneError
from service import PeerService
from version import VERSION


class ReadinessStatus(BaseModel):
    status: Literal["ready", "not_ready"]
    version: str
    uptime_seconds: float
    interface: str
    reason: str | None


def readiness(service: PeerService) -> ReadinessStatus:
    reason = None
    try:
        ready = service.is_ready()
        if not ready:
            reason = "state_not_converged"
    except ControlPlaneError as exc:
        ready = False
        reason = exc.code
    return ReadinessStatus(
        status="ready" if ready else "not_ready",
        version=VERSION,
        uptime_seconds=round(time.monotonic() - service.started_at, 1),
        interface=service.settings.interface,
        reason=reason,
    )
