"""CPU deltas are normalized to cgroup capacity, not host-wide CPU usage."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from cgroups import CgroupReader, CpuReading, MemoryReading, Resources
from system_info import SystemCollector


class ResourceReader(CgroupReader):
    def __init__(self) -> None:
        self.resources = Resources(
            "cgroup_v2", CpuReading("group", 2, 0.5), MemoryReading(64, 256, 256)
        )

    def read(self) -> Resources:
        return self.resources


@pytest.fixture
def collector(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "system_info.platform.freedesktop_os_release", lambda: {"PRETTY_NAME": "Alpine"}
    )
    monkeypatch.setattr(
        "system_info.shutil.disk_usage",
        lambda path: SimpleNamespace(total=1000, used=250, free=750),
    )
    return SystemCollector(tmp_path, 1, ResourceReader())


def test_resources_runtime_and_fractional_cpu_limit(collector, monkeypatch) -> None:
    clock = iter((10.0, 12.0))
    monkeypatch.setattr("system_info.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("system_info.time.time", lambda: 1000)
    first = collector.sample()
    assert first.status == "available"
    assert first.cpu.status == "warming_up" and first.cpu.usage_percent is None
    assert first.cpu.capacity_cores == 0.5 and first.cpu.total_usage_seconds == 2
    assert first.sample.sampled_at == 1000 and first.sample.interval_seconds is None
    assert first.memory.usage_percent == 25
    assert first.disk.usage_percent == 25 and first.disk.scope == "data_filesystem"
    assert first.runtime.os == "Alpine" and first.runtime.uptime_seconds == 9
    assert first.runtime.python_version and first.runtime.architecture
    collector.reader.resources = replace(
        collector.reader.resources, cpu=CpuReading("group", 2.5, 0.5)
    )
    second = collector.sample()
    assert second.cpu.status == "available"
    assert second.cpu.used_cores == 0.25 and second.cpu.usage_percent == 50
    assert second.sample.interval_seconds == 2


@pytest.mark.parametrize(
    "kind", ["counter_reset", "group_changed", "zero_elapsed", "negative_elapsed"]
)
def test_discontinuities_warm_up_without_negative_or_spurious_usage(
    collector, monkeypatch, kind
) -> None:
    clock = iter(
        (
            10,
            10 if kind == "zero_elapsed" else 9 if kind == "negative_elapsed" else 11,
            12,
        )
    )
    monkeypatch.setattr("system_info.time.monotonic", lambda: next(clock))
    collector.sample()
    current = CpuReading(
        "new" if kind == "group_changed" else "group",
        1 if kind == "counter_reset" else 2.5,
        0.5,
    )
    collector.reader.resources = replace(collector.reader.resources, cpu=current)
    assert collector.sample().cpu.usage_percent is None
    collector.reader.resources = replace(
        collector.reader.resources,
        cpu=replace(current, total_seconds=current.total_seconds + 0.1),
    )
    assert collector.sample().cpu.usage_percent > 0


def test_missing_data_is_partial_and_does_not_fabricate_zero(collector) -> None:
    collector.reader.resources = Resources("unavailable", None, None)
    result = collector.sample()
    assert result.status == "partial"
    assert result.cpu.status == "unavailable"
    assert result.cpu.total_usage_seconds is None and result.cpu.capacity_cores is None
    assert result.memory.used_bytes is None and result.memory.usage_percent is None
    assert result.disk is not None and result.runtime.os == "Alpine"


def test_failure_breaks_cpu_continuity_and_recovery_starts_with_a_baseline(
    collector, monkeypatch
) -> None:
    clock = iter((10, 11, 12, 13))
    monkeypatch.setattr("system_info.time.monotonic", lambda: next(clock))
    original = collector.reader.resources
    collector.sample()
    collector.reader.resources = replace(original, cpu=None)
    collector.sample()
    collector.reader.resources = original
    assert collector.sample().cpu.status == "warming_up"
    collector.reset()
    reset = collector.sample()
    assert reset.sample.interval_seconds is None
    assert reset.cpu.status == "warming_up"


def test_unknown_capacity_and_disk_unavailability(collector, monkeypatch) -> None:
    collector.reader.resources = Resources(
        "cgroup_v2", CpuReading("group", 2, None), MemoryReading(64, None, None)
    )
    monkeypatch.setattr(
        "system_info.shutil.disk_usage",
        lambda path: (_ for _ in ()).throw(PermissionError("sensitive path")),
    )
    result = collector.sample()
    assert result.status == "partial" and result.disk is None
    assert result.cpu.status == "unavailable"
    assert result.cpu.usage_percent is None
    assert result.memory.capacity_bytes is None and result.memory.usage_percent is None
    assert "sensitive path" not in result.model_dump_json()


def test_zero_size_filesystem_and_nonpositive_uptime(collector, monkeypatch) -> None:
    monkeypatch.setattr(
        "system_info.shutil.disk_usage",
        lambda path: SimpleNamespace(total=0, used=0, free=0),
    )
    monkeypatch.setattr("system_info.time.monotonic", lambda: 0)
    result = collector.sample()
    assert result.disk.usage_percent == 0 and result.runtime.uptime_seconds == 0


@pytest.mark.parametrize("metadata", [{}, None])
def test_os_metadata_fallback_and_default_reader(
    tmp_path, monkeypatch, metadata
) -> None:
    def release():
        if metadata is None:
            raise OSError("No release metadata")
        return metadata

    monkeypatch.setattr("system_info.platform.freedesktop_os_release", release)
    monkeypatch.setattr("system_info.platform.system", lambda: "Darwin")
    result = SystemCollector(tmp_path, 0)
    assert isinstance(result.reader, CgroupReader)
    assert result.os_name == ("Darwin" if metadata is None else "Linux")
