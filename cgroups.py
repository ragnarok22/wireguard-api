"""Read the current process's cgroup, never global host usage counters.

Visible ancestor limits and process CPU affinity bound effective capacity. Limits
above a private cgroup namespace's mount root cannot be inspected from within it.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

Source = Literal["cgroup_v1", "cgroup_v2", "unavailable"]


@dataclass(frozen=True)
class CpuReading:
    identity: str
    total_seconds: float
    capacity_cores: float | None


@dataclass(frozen=True)
class MemoryReading:
    used_bytes: int
    limit_bytes: int | None
    capacity_bytes: int | None


@dataclass(frozen=True)
class Resources:
    source: Source
    cpu: CpuReading | None
    memory: MemoryReading | None


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _unsigned(value: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise ValueError("Invalid resource counter")
    return int(value)


def _path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Invalid cgroup path")
    return path


class CgroupReader:
    def __init__(self, proc_dir: Path = Path("/proc")) -> None:
        self.proc_dir = proc_dir

    def _locations(self) -> tuple[Source, dict[str, tuple[Path, Path]]]:
        groups: dict[str, PurePosixPath] = {}
        for line in (self.proc_dir / "self/cgroup").read_text().splitlines():
            hierarchy, controllers, value = line.split(":", 2)
            names = controllers.split(",") if controllers else ["unified"]
            for controller in names:
                groups[controller] = _path(value)
        locations: dict[str, tuple[Path, Path]] = {}
        source: Source = "cgroup_v1"
        for line in (self.proc_dir / "self/mountinfo").read_text().splitlines():
            before, separator, after = line.partition(" - ")
            fields = after.split()
            if not separator or not fields or fields[0] not in ("cgroup", "cgroup2"):
                continue
            mount = before.split()
            root = _path(_unescape(mount[3]))
            directory = Path(_path(_unescape(mount[4])))
            names = ["unified"] if fields[0] == "cgroup2" else fields[2].split(",")
            for controller in names:
                if controller not in groups:
                    continue
                group = groups[controller]
                # A private namespace exposes its current cgroup as '/'.
                relative = (
                    PurePosixPath(".") if str(group) == "/" else group.relative_to(root)
                )
                locations[controller] = (directory / relative, directory)
                if controller == "unified":
                    source = "cgroup_v2"
        if not locations:
            raise ValueError("Current cgroup is not mounted")
        return source, locations

    @staticmethod
    def _ancestors(location: tuple[Path, Path], filename: str) -> list[str]:
        directory, root = location
        values: list[str] = []
        while True:
            try:
                values.append((directory / filename).read_text().strip())
            except FileNotFoundError:
                if directory != root:
                    raise
            if directory == root:
                return values
            directory = directory.parent

    def _cpu(
        self, source: Source, locations: dict[str, tuple[Path, Path]]
    ) -> CpuReading:
        capacities: list[float] = []
        affinity = os.process_cpu_count()
        if affinity is not None and affinity > 0:
            capacities.append(float(affinity))
        if source == "cgroup_v2":
            location = locations["unified"]
            counters = dict(
                line.split()
                for line in (location[0] / "cpu.stat").read_text().splitlines()
            )
            total = _unsigned(counters["usage_usec"]) / 1_000_000
            for value in self._ancestors(location, "cpu.max"):
                quota, period = value.split()
                period_value = _unsigned(period)
                if period_value == 0:
                    raise ValueError("Invalid CPU period")
                if quota != "max":
                    capacity = _unsigned(quota) / period_value
                    if capacity == 0:
                        raise ValueError("Invalid CPU quota")
                    capacities.append(capacity)
        else:
            location = locations["cpuacct"]
            total = _unsigned((location[0] / "cpuacct.usage").read_text().strip()) / 1e9
            quotas = self._ancestors(locations["cpu"], "cpu.cfs_quota_us")
            periods = self._ancestors(locations["cpu"], "cpu.cfs_period_us")
            if len(quotas) != len(periods):
                raise ValueError("Incomplete CPU hierarchy")
            for quota, period in zip(quotas, periods, strict=True):
                period_value = _unsigned(period)
                if period_value == 0:
                    raise ValueError("Invalid CPU period")
                if quota != "-1":
                    capacity = _unsigned(quota) / period_value
                    if capacity == 0:
                        raise ValueError("Invalid CPU quota")
                    capacities.append(capacity)
        return CpuReading(
            f"{source}:{location[0]}", total, min(capacities, default=None)
        )

    def _host_memory_capacity(self) -> int | None:
        try:
            for line in (self.proc_dir / "meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    _, count, unit = line.split()
                    if unit != "kB":
                        raise ValueError("Invalid memory unit")
                    return _unsigned(count) * 1024 or None
        except OSError, ValueError:
            pass
        return None

    def _memory(
        self, source: Source, locations: dict[str, tuple[Path, Path]]
    ) -> MemoryReading:
        if source == "cgroup_v2":
            location = locations["unified"]
            used_file, limit_file = "memory.current", "memory.max"
        else:
            location = locations["memory"]
            used_file, limit_file = "memory.usage_in_bytes", "memory.limit_in_bytes"
        used = _unsigned((location[0] / used_file).read_text().strip())
        limits = []
        for value in self._ancestors(location, limit_file):
            if value == "max" and source == "cgroup_v2":
                continue
            limit = _unsigned(value)
            # v1 represents 'unlimited' as a page-aligned LONG_MAX sentinel.
            if source == "cgroup_v1" and limit >= 1 << 60:
                continue
            if limit == 0:
                raise ValueError("Invalid memory limit")
            limits.append(limit)
        limit_bytes = min(limits, default=None)
        host = self._host_memory_capacity()
        capacities = [value for value in (limit_bytes, host) if value is not None]
        return MemoryReading(used, limit_bytes, min(capacities, default=None))

    def read(self) -> Resources:
        try:
            source, locations = self._locations()
        except OSError, ValueError, IndexError:
            return Resources("unavailable", None, None)
        try:
            cpu = self._cpu(source, locations)
        except OSError, ValueError, KeyError:
            cpu = None
        try:
            memory = self._memory(source, locations)
        except OSError, ValueError, KeyError:
            memory = None
        return Resources(source, cpu, memory)
