"""Per-application sampling and independent, freshness-bounded read caches."""

import asyncio
import logging
import threading
import time
from typing import Literal

from starlette.concurrency import run_in_threadpool

from errors import ControlPlaneError, TelemetryError
from models import SystemInfo, VpnStats
from service import PeerService
from stats import StatsReading, VpnSample, aggregate, sample_rates
from system_info import SystemCollector

logger = logging.getLogger(__name__)


def _failure(component: str, exception: Exception) -> TelemetryError:
    error = TelemetryError(f"{component} statistics are unavailable")
    if isinstance(exception, ControlPlaneError):
        error.code = exception.code
    logger.warning("%s sampling failed (%s)", component, type(exception).__name__)
    return error


class Telemetry:
    def __init__(
        self, service: PeerService, system_collector: SystemCollector | None = None
    ) -> None:
        self.service = service
        self.system_collector = system_collector or SystemCollector(
            service.settings.data_dir, service.started_at
        )
        self.interval = service.settings.telemetry_interval
        self.max_age = max(3, self.interval * 3)
        self._lock = threading.Lock()
        self._vpn_sampling = threading.Lock()
        self._system_sampling = threading.Lock()
        self._previous: VpnSample | None = None
        self._vpn: StatsReading | None = None
        self._system: tuple[SystemInfo, float] | None = None
        self._vpn_error: TelemetryError | None = None
        self._system_error: TelemetryError | None = None

    def sample_vpn(self) -> None:
        with self._vpn_sampling:
            try:
                sample = self.service.stats_sample()
            except Exception as exc:
                with self._lock:
                    self._previous = None
                    self._vpn = None
                    self._vpn_error = _failure("VPN", exc)
                return
            with self._lock:
                self._vpn = sample_rates(sample, self._previous)
                self._previous = sample
                self._vpn_error = None

    def sample_system(self) -> None:
        with self._system_sampling:
            try:
                sample = self.system_collector.sample()
            except Exception as exc:
                self.system_collector.reset()
                with self._lock:
                    self._system = None
                    self._system_error = _failure("System", exc)
                return
            with self._lock:
                self._system = (sample, time.monotonic())
                self._system_error = None

    def stats(self, window_seconds: int = 180) -> VpnStats:
        with self._lock:
            if self._vpn is None:
                raise self._vpn_error or TelemetryError("VPN statistics are warming up")
            now = time.monotonic()
            if now - self._vpn.sample.monotonic_at > self.max_age:
                raise TelemetryError("VPN statistics sample is stale")
            return aggregate(
                self._vpn,
                self.service.settings,
                self.service.started_at,
                now,
                window_seconds,
            )

    def system(self) -> SystemInfo:
        with self._lock:
            if self._system is None:
                raise self._system_error or TelemetryError(
                    "System statistics are warming up"
                )
            sample, observed_at = self._system
            now = time.monotonic()
            age = max(0, now - observed_at)
            if age > self.max_age:
                raise TelemetryError("System statistics sample is stale")
            result = sample.model_copy(deep=True)
            result.sample.age_seconds = age
            result.runtime.uptime_seconds = max(0, now - self.service.started_at)
            return result

    async def loop(
        self, component: Literal["vpn", "system"], stop: asyncio.Event
    ) -> None:
        sample = self.sample_vpn if component == "vpn" else self.sample_system
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
            except TimeoutError:
                await run_in_threadpool(sample)
