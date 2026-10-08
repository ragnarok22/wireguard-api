"""Portable cgroup filesystem fixtures; no host usage counters or privileges."""

from pathlib import Path

import pytest

from cgroups import CgroupReader


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def tree(tmp_path: Path, version: int = 2) -> tuple[CgroupReader, Path, Path]:
    proc, root = tmp_path / "proc", tmp_path / "cgroup"
    write(proc / "meminfo", "MemTotal: 524288 kB\n")
    if version == 2:
        write(proc / "self/cgroup", "0::/tenant/app\n")
        write(proc / "self/mountinfo", f"1 0 0:1 / {root} ro - cgroup2 cgroup rw\n")
        parent, group = root / "tenant", root / "tenant/app"
        write(parent / "cpu.max", "max 100000")
        write(parent / "memory.max", "max")
        write(group / "cpu.stat", "usage_usec 2000000\nuser_usec 1000000\n")
        write(group / "cpu.max", "200000 100000")
        write(group / "memory.current", "67108864")
        write(group / "memory.max", "268435456")
    else:
        write(proc / "self/cgroup", "2:cpu,cpuacct:/app\n3:memory:/app\n")
        write(
            proc / "self/mountinfo",
            f"1 0 0:1 / {root / 'cpu'} ro - cgroup cgroup rw,cpu,cpuacct\n"
            f"2 0 0:2 / {root / 'memory'} ro - cgroup cgroup rw,memory\n",
        )
        group = root / "cpu/app"
        write(group / "cpuacct.usage", "2000000000")
        write(group / "cpu.cfs_quota_us", "200000")
        write(group / "cpu.cfs_period_us", "100000")
        write(root / "memory/app/memory.usage_in_bytes", "67108864")
        write(root / "memory/app/memory.limit_in_bytes", "268435456")
    return CgroupReader(proc), root, group


@pytest.fixture(autouse=True)
def cpu_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cgroups.os.process_cpu_count", lambda: 4)


@pytest.mark.parametrize("version", [1, 2])
def test_reads_current_group_and_effective_limits(tmp_path: Path, version: int) -> None:
    reader, _, _ = tree(tmp_path, version)
    result = reader.read()
    assert result.source == f"cgroup_v{version}"
    assert result.cpu is not None and result.cpu.total_seconds == 2
    assert result.cpu.capacity_cores == 2
    assert result.memory is not None and result.memory.used_bytes == 67108864
    assert result.memory.limit_bytes == 268435456
    assert result.memory.capacity_bytes == 268435456


def test_visible_ancestors_and_affinity_bound_cpu_and_memory(
    tmp_path, monkeypatch
) -> None:
    reader, root, group = tree(tmp_path)
    write(group / "cpu.max", "300000 100000")
    write(root / "tenant/cpu.max", "50000 100000")
    write(root / "tenant/memory.max", "134217728")
    assert reader.read().cpu.capacity_cores == 0.5
    assert reader.read().memory.limit_bytes == 134217728
    write(root / "tenant/cpu.max", "max 100000")
    monkeypatch.setattr("cgroups.os.process_cpu_count", lambda: 1)
    assert reader.read().cpu.capacity_cores == 1
    write(group / "memory.max", "1073741824")
    write(root / "tenant/memory.max", "max")
    assert reader.read().memory.capacity_bytes == 536870912


@pytest.mark.parametrize("version", [1, 2])
def test_unlimited_groups_use_capacity_not_fake_host_usage(tmp_path, version) -> None:
    reader, root, group = tree(tmp_path, version)
    if version == 2:
        write(group / "cpu.max", "max 100000")
        write(group / "memory.max", "max")
    else:
        write(group / "cpu.cfs_quota_us", "-1")
        write(root / "memory/app/memory.limit_in_bytes", "9223372036854771712")
    result = reader.read()
    assert result.cpu.capacity_cores == 4
    assert result.memory.limit_bytes is None
    assert result.memory.capacity_bytes == 536870912
    assert result.memory.used_bytes == 67108864


@pytest.mark.parametrize("count", [None, 0])
def test_unknown_affinity_with_unlimited_quota_has_unknown_capacity(
    tmp_path, monkeypatch, count
) -> None:
    reader, _, group = tree(tmp_path)
    write(group / "cpu.max", "max 100000")
    monkeypatch.setattr("cgroups.os.process_cpu_count", lambda: count)
    assert reader.read().cpu.capacity_cores is None


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(
    "kind", ["usage", "quota", "zero_quota", "period", "zero_period", "missing"]
)
def test_invalid_cpu_data_does_not_invalidate_memory(tmp_path, version, kind) -> None:
    reader, _, group = tree(tmp_path, version)
    if version == 2:
        if kind == "usage":
            write(group / "cpu.stat", "usage_usec -1")
        elif kind == "missing":
            write(group / "cpu.stat", "user_usec 1")
        else:
            quota = {"quota": "invalid", "zero_quota": "0"}.get(kind, "200000")
            period = {"period": "invalid", "zero_period": "0"}.get(kind, "100000")
            write(group / "cpu.max", f"{quota} {period}")
    else:
        if kind in ("usage", "missing"):
            (group / "cpuacct.usage").unlink() if kind == "missing" else write(
                group / "cpuacct.usage", "-1"
            )
        else:
            filename = "cpu.cfs_quota_us" if "quota" in kind else "cpu.cfs_period_us"
            write(group / filename, "0" if kind.startswith("zero") else "bad")
    result = reader.read()
    assert result.cpu is None and result.memory is not None


def test_incomplete_v1_quota_hierarchy_is_unavailable(tmp_path) -> None:
    reader, root, _ = tree(tmp_path, 1)
    write(root / "cpu/cpu.cfs_quota_us", "50000")
    assert reader.read().cpu is None


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("kind", ["usage", "limit", "zero", "missing"])
def test_invalid_memory_does_not_invalidate_cpu(tmp_path, version, kind) -> None:
    reader, root, group = tree(tmp_path, version)
    memory = group if version == 2 else root / "memory/app"
    used = "memory.current" if version == 2 else "memory.usage_in_bytes"
    limit = "memory.max" if version == 2 else "memory.limit_in_bytes"
    if kind == "missing":
        (memory / used).unlink()
    else:
        write(
            memory / (used if kind == "usage" else limit),
            "0" if kind == "zero" else "-1",
        )
    result = reader.read()
    assert result.memory is None and result.cpu is not None


def test_missing_intermediate_limit_is_not_silently_ignored(tmp_path) -> None:
    reader, root, _ = tree(tmp_path)
    (root / "tenant/cpu.max").unlink()
    (root / "tenant/memory.max").unlink()
    result = reader.read()
    assert result.cpu is None and result.memory is None


@pytest.mark.parametrize(
    "text",
    [
        None,
        "MemAvailable: 1 kB",
        "MemTotal: 1 MB",
        "MemTotal: bad kB",
        "MemTotal: 0 kB",
    ],
)
def test_unavailable_host_capacity_is_not_fabricated(tmp_path, text) -> None:
    reader, _, group = tree(tmp_path)
    write(group / "memory.max", "max")
    path = reader.proc_dir / "meminfo"
    if text is None:
        path.unlink()
    else:
        write(path, text)
    memory = reader.read().memory
    assert memory.used_bytes == 67108864
    assert memory.limit_bytes is None and memory.capacity_bytes is None


def test_finite_limit_survives_unavailable_host_capacity(tmp_path) -> None:
    reader, _, _ = tree(tmp_path)
    (reader.proc_dir / "meminfo").unlink()
    assert reader.read().memory.capacity_bytes == 268435456


@pytest.mark.parametrize("group", ["relative/path", "/../escape", "/elsewhere"])
def test_invalid_or_unmounted_paths_are_unavailable(tmp_path, group) -> None:
    reader, _, _ = tree(tmp_path)
    write(reader.proc_dir / "self/cgroup", f"0::{group}")
    if group == "/elsewhere":
        text = (
            (reader.proc_dir / "self/mountinfo").read_text().replace(" / ", " /tenant ")
        )
        write(reader.proc_dir / "self/mountinfo", text)
    assert reader.read().source == "unavailable"


@pytest.mark.parametrize(
    "kind", ["absent", "bad_group", "bad_mount", "unsupported", "no_controller"]
)
def test_unsupported_layouts_return_explicit_unavailability(tmp_path, kind) -> None:
    reader, _, _ = tree(tmp_path)
    if kind == "absent":
        (reader.proc_dir / "self/mountinfo").unlink()
    elif kind == "bad_group":
        write(reader.proc_dir / "self/cgroup", "malformed")
    elif kind == "bad_mount":
        write(reader.proc_dir / "self/mountinfo", "1 - cgroup2 cgroup rw")
    elif kind == "unsupported":
        write(
            reader.proc_dir / "self/mountinfo",
            "invalid\n1 0 0:1 / /proc rw - proc proc rw\n",
        )
    else:
        write(reader.proc_dir / "self/cgroup", "2:unused:/tenant/app")
    result = reader.read()
    assert (
        result.source == "unavailable" and result.cpu is None and result.memory is None
    )


def test_private_namespace_mount_root_and_escaped_mountpoint(tmp_path) -> None:
    reader, root, group = tree(tmp_path / "with space")
    write(reader.proc_dir / "self/cgroup", "0::/")
    escaped = str(group).replace(" ", r"\040")
    write(
        reader.proc_dir / "self/mountinfo",
        f"1 0 0:1 /docker/container {escaped} ro - cgroup2 cgroup rw\n",
    )
    result = reader.read()
    assert result.cpu.capacity_cores == 2
    assert result.memory.limit_bytes == 268435456


def test_v1_missing_controller_is_partial(tmp_path) -> None:
    reader, _, _ = tree(tmp_path, 1)
    write(reader.proc_dir / "self/cgroup", "3:memory:/app")
    result = reader.read()
    assert result.source == "cgroup_v1" and result.cpu is None
    assert result.memory is not None
