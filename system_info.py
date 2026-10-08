"""Container resources and data-filesystem information without blocking waits."""

import platform
import shutil
import time
from pathlib import Path

from cgroups import CgroupReader, CpuReading
from models import CpuInfo, DiskInfo, MemoryInfo, RuntimeInfo, SampleInfo, SystemInfo
from version import VERSION


class SystemCollector:
    def __init__(
        self, data_dir: Path, started_at: float, reader: CgroupReader | None = None
    ) -> None:
        self.data_dir = data_dir
        self.started_at = started_at
        self.reader = reader or CgroupReader()
        self._previous: tuple[CpuReading, float] | None = None
        self._last_sample: float | None = None
        try:
            self.os_name = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
        except OSError:
            self.os_name = platform.system()

    def sample(self) -> SystemInfo:
        resources = self.reader.read()
        now = time.monotonic()
        interval = (
            None if self._last_sample is None else max(0, now - self._last_sample)
        )
        self._last_sample = now
        cpu = resources.cpu
        used_cores = None
        if cpu is not None and self._previous is not None:
            previous, previous_at = self._previous
            elapsed = now - previous_at
            if (
                cpu.identity == previous.identity
                and elapsed > 0
                and cpu.total_seconds >= previous.total_seconds
            ):
                used_cores = (cpu.total_seconds - previous.total_seconds) / elapsed
        self._previous = None if cpu is None else (cpu, now)
        capacity = None if cpu is None else cpu.capacity_cores
        cpu_percent = (
            None
            if used_cores is None or capacity is None
            else 100 * used_cores / capacity
        )
        memory = resources.memory
        disk = None
        try:
            usage = shutil.disk_usage(self.data_dir)
            disk = DiskInfo(
                total_bytes=usage.total,
                used_bytes=usage.used,
                free_bytes=usage.free,
                usage_percent=0 if usage.total == 0 else 100 * usage.used / usage.total,
            )
        except OSError:
            pass
        available = (
            cpu is not None
            and capacity is not None
            and memory is not None
            and memory.capacity_bytes is not None
            and disk is not None
        )
        return SystemInfo(
            status="available" if available else "partial",
            sample=SampleInfo(
                sampled_at=time.time(), age_seconds=0, interval_seconds=interval
            ),
            cpu=CpuInfo(
                source=resources.source,
                status="unavailable"
                if cpu is None or capacity is None
                else "warming_up"
                if cpu_percent is None
                else "available",
                capacity_cores=capacity,
                total_usage_seconds=None if cpu is None else cpu.total_seconds,
                used_cores=used_cores,
                usage_percent=cpu_percent,
            ),
            memory=MemoryInfo(
                source=resources.source,
                used_bytes=None if memory is None else memory.used_bytes,
                limit_bytes=None if memory is None else memory.limit_bytes,
                capacity_bytes=None if memory is None else memory.capacity_bytes,
                usage_percent=None
                if memory is None or memory.capacity_bytes is None
                else 100 * memory.used_bytes / memory.capacity_bytes,
            ),
            disk=disk,
            runtime=RuntimeInfo(
                os=self.os_name,
                kernel=platform.release(),
                architecture=platform.machine(),
                python_version=platform.python_version(),
                api_version=VERSION,
                uptime_seconds=max(0, now - self.started_at),
            ),
        )
