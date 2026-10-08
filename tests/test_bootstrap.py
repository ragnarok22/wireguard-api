"""Bootstrap contracts use a stateful, unprivileged kernel/command simulator."""

import base64
import json
import os
import subprocess
from ipaddress import IPv4Interface
from types import SimpleNamespace

import pytest

from bootstrap import bootstrap
from errors import ControlPlaneError

PRIVATE = base64.b64encode(bytes(range(32))).decode()
PUBLIC = base64.b64encode(bytes(range(32, 64))).decode()
OTHER = base64.b64encode(bytes(range(64, 96))).decode()


class Host:
    def __init__(self):
        self.exists = False
        self.kind = "wireguard"
        self.addresses = []
        self.public_key = "(none)"
        self.route = [{"dev": "ens5", "gateway": "172.22.0.1"}]
        self.forward = "0"
        self.rules = set()
        self.calls = []
        self.fail = None
        self.generated = PRIVATE
        self.egress_links = [{"ifname": "ens5"}, {"ifname": "wan0"}]

    def run(self, command, **kwargs):
        assert isinstance(command, list)
        assert kwargs["check"] is True
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 0.25
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        self.calls.append((command, kwargs.get("input")))
        if self.fail and self.fail(command):
            raise subprocess.CalledProcessError(2, command, stderr=PRIVATE)
        output = ""
        if command == ["ip", "-j", "-details", "link", "show"]:
            output = json.dumps(
                self.egress_links
                + (
                    [{"ifname": "wgtest", "linkinfo": {"info_kind": self.kind}}]
                    if self.exists
                    else []
                )
            )
        elif command[:4] == ["ip", "link", "add", "dev"]:
            self.exists = True
        elif command[:4] == ["ip", "-j", "address", "show"]:
            output = json.dumps([{"addr_info": self.addresses}])
        elif command[:3] == ["ip", "address", "add"]:
            self.addresses.append(
                {"family": "inet", "local": "10.77.0.1", "prefixlen": 24}
            )
        elif command == ["wg", "genkey"]:
            output = self.generated + "\n"
        elif command == ["wg", "pubkey"]:
            assert kwargs["input"] == PRIVATE + "\n"
            output = PUBLIC + "\n"
        elif command == ["wg", "show", "wgtest", "public-key"]:
            output = self.public_key + "\n"
        elif command[:2] == ["wg", "set"]:
            assert command == [
                "wg",
                "set",
                "wgtest",
                "listen-port",
                "51900",
                "private-key",
                "/dev/stdin",
            ]
            assert kwargs["input"] == PRIVATE + "\n"
            self.public_key = PUBLIC
        elif command == ["ip", "-j", "route", "get", "1.1.1.1"]:
            output = json.dumps(self.route)
        elif command == ["sysctl", "-n", "net.ipv4.ip_forward"]:
            output = self.forward + "\n"
        elif command == ["sysctl", "-w", "net.ipv4.ip_forward=1"]:
            self.forward = "1"
        elif command[0] == "iptables":
            rule = tuple(arg for arg in command if arg not in ("-C", "-A"))
            if "-C" in command and rule not in self.rules:
                raise subprocess.CalledProcessError(1, command)
            if "-A" in command:
                assert rule not in self.rules
                self.rules.add(rule)
        else:
            assert command == ["ip", "link", "set", "up", "dev", "wgtest"]
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        interface="wgtest",
        server_address=IPv4Interface("10.77.0.1/24"),
        listen_port=51900,
        data_dir=tmp_path,
        egress_interface=None,
        command_timeout=0.25,
    )
    host = Host()
    monkeypatch.setattr(subprocess, "run", host.run)
    return settings, host


def test_bootstrap_repairs_without_duplicates_and_preserves_key(setup):
    settings, host = setup
    bootstrap(settings)
    key_file = settings.data_dir / "server_private.key"
    assert key_file.read_text().strip() == PRIVATE
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert host.public_key == PUBLIC
    assert len(host.rules) == 3
    assert any(
        "-s 10.77.0.0/24 -o ens5 -j MASQUERADE" in " ".join(rule) for rule in host.rules
    )
    assert any(
        "-i wgtest -o ens5 -s 10.77.0.0/24 -j ACCEPT" in " ".join(rule)
        for rule in host.rules
    )
    assert any(
        "-i ens5 -o wgtest -d 10.77.0.0/24 -m conntrack "
        "--ctstate ESTABLISHED,RELATED -j ACCEPT" in " ".join(rule)
        for rule in host.rules
    )
    first_calls = len(host.calls)
    bootstrap(settings)
    assert len(host.rules) == 3
    assert ["wg", "genkey"] not in [call[0] for call in host.calls[first_calls:]]
    assert key_file.read_text().strip() == PRIVATE
    host.rules.clear()
    bootstrap(settings)
    assert len(host.rules) == 3


def test_existing_key_permission_repair_and_explicit_egress(setup):
    settings, host = setup
    key_file = settings.data_dir / "server_private.key"
    key_file.write_text(PRIVATE + "\n")
    key_file.chmod(0o644)
    settings.egress_interface = "wan0"
    bootstrap(settings)
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert not any(command[:3] == ["ip", "-j", "route"] for command, _ in host.calls)
    assert all("wan0" in rule for rule in host.rules)


@pytest.mark.parametrize(
    "addresses",
    [
        [{"family": "inet", "local": "10.77.0.9", "prefixlen": 24}],
        [{"family": "inet", "local": "10.77.0.1", "prefixlen": 32}],
        [
            {"family": "inet", "local": "10.77.0.1", "prefixlen": 24},
            {"family": "inet", "local": "192.168.3.1", "prefixlen": 24},
        ],
        [{"family": "inet6", "local": "fe80::1", "prefixlen": 64}],
    ],
)
def test_rejects_all_conflicting_interface_addresses(setup, addresses):
    settings, host = setup
    host.exists = True
    host.addresses = addresses
    with pytest.raises(ControlPlaneError):
        bootstrap(settings)
    assert not host.rules


@pytest.mark.parametrize("kind,key", [("bridge", "(none)"), ("wireguard", OTHER)])
def test_never_rekeys_or_takes_over_foreign_interface(setup, kind, key):
    settings, host = setup
    host.exists, host.kind, host.public_key = True, kind, key
    with pytest.raises(ControlPlaneError):
        bootstrap(settings)
    assert not any(command[:2] == ["wg", "set"] for command, _ in host.calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("interface", "wgother"),
        ("server_address", IPv4Interface("10.78.0.1/24")),
    ],
)
def test_persistent_identity_cannot_accidentally_migrate(setup, field, value):
    settings, host = setup
    bootstrap(settings)
    host.calls.clear()
    setattr(settings, field, value)
    with pytest.raises(ControlPlaneError):
        bootstrap(settings)
    assert not host.calls


@pytest.mark.parametrize(
    "route",
    [
        [],
        [{}],
        [{"dev": "wgtest"}],
        [{"dev": "bad name"}],
        [{"dev": "a"}, {"dev": "b"}],
    ],
)
def test_route_must_identify_one_real_egress_without_guessing(setup, route):
    settings, host = setup
    host.route = route
    with pytest.raises(ControlPlaneError):
        bootstrap(settings)
    assert not host.rules


@pytest.mark.parametrize(
    "prefix",
    [
        ["ip", "link", "add"],
        ["ip", "address", "add"],
        ["wg", "set"],
        ["ip", "link", "set"],
        ["sysctl", "-w"],
        ["iptables", "-w"],
    ],
)
def test_stage_failure_is_safe_and_recoverable(setup, prefix):
    settings, host = setup
    host.fail = lambda command: command[: len(prefix)] == prefix
    with pytest.raises(ControlPlaneError) as error:
        bootstrap(settings)
    assert PRIVATE not in str(error.value)
    assert error.value.__cause__ is None
    host.fail = None
    bootstrap(settings)
    assert host.public_key == PUBLIC and len(host.rules) == 3


@pytest.mark.parametrize(
    "key", ["invalid", "", PRIVATE + "\n" + OTHER, PRIVATE + " " * 128 + OTHER]
)
def test_invalid_persistent_keys_fail_closed(setup, key):
    settings, host = setup
    (settings.data_dir / "server_private.key").write_text(key)
    with pytest.raises(ControlPlaneError):
        bootstrap(settings)
    assert not host.calls


@pytest.mark.parametrize(
    "failure", [OSError("secret"), subprocess.TimeoutExpired("wg", 0.25, PRIVATE)]
)
def test_spawn_and_timeout_errors_never_expose_output(setup, monkeypatch, failure):
    settings, _ = setup

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ControlPlaneError) as error:
        bootstrap(settings)
    assert "secret" not in str(error.value) and PRIVATE not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("response", ["not-json", "null", "{}", "[null]"])
def test_malformed_command_json_fails_closed(setup, monkeypatch, response):
    settings, host = setup

    def run(command, **kwargs):
        if command[:3] == ["ip", "-j", "-details"]:
            return subprocess.CompletedProcess(command, 0, stdout=response)
        return host.run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ControlPlaneError):
        bootstrap(settings)
    assert not host.rules


@pytest.mark.parametrize("response", [[], [{}], [{"addr_info": None}]])
def test_malformed_address_query_fails_closed(setup, monkeypatch, response):
    settings, host = setup

    def run(command, **kwargs):
        if command[:4] == ["ip", "-j", "address", "show"]:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(response))
        return host.run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ControlPlaneError):
        bootstrap(settings)
    assert not host.rules


def test_key_creation_race_preserves_winning_identity(setup, monkeypatch):
    settings, host = setup
    host.generated = OTHER
    link = os.link

    def race(source, destination):
        if destination.name == "server_private.key":
            destination.write_text(PRIVATE + "\n")
        link(source, destination)

    monkeypatch.setattr(os, "link", race)
    bootstrap(settings)
    assert (settings.data_dir / "server_private.key").read_text().strip() == PRIVATE
    assert host.public_key == PUBLIC
    assert not list(settings.data_dir.glob(".bootstrap-*"))


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_key_file_must_be_regular_and_not_a_symlink(setup, kind):
    settings, host = setup
    path = settings.data_dir / "server_private.key"
    if kind == "fifo":
        os.mkfifo(path)
    else:
        target = settings.data_dir / "target"
        target.write_text(PRIVATE)
        path.symlink_to(target)
    with pytest.raises(ControlPlaneError):
        bootstrap(settings)
    assert not host.calls


@pytest.mark.parametrize("stage", ["file", "directory"])
def test_fsync_failure_is_safe_cleans_temp_and_allows_retry(setup, monkeypatch, stage):
    settings, host = setup
    fsync = os.fsync

    def fail(descriptor):
        is_directory = os.fstat(descriptor).st_mode & 0o170000 == 0o040000
        if is_directory == (stage == "directory"):
            raise OSError(PRIVATE)
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(ControlPlaneError) as error:
        bootstrap(settings)
    assert PRIVATE not in str(error.value)
    assert not host.calls
    assert not list(settings.data_dir.glob(".bootstrap-*"))
    monkeypatch.setattr(os, "fsync", fsync)
    bootstrap(settings)
    assert host.public_key == PUBLIC


def test_invalid_generated_key_is_never_persisted(setup):
    settings, host = setup
    host.generated = "not-a-key"
    with pytest.raises(ControlPlaneError):
        bootstrap(settings)
    assert not (settings.data_dir / "server_private.key").exists()


def test_corrupt_identity_manifest_prevents_mutation(setup):
    settings, host = setup
    (settings.data_dir / "bootstrap.json").write_text("corrupt")
    with pytest.raises(ControlPlaneError):
        bootstrap(settings)
    assert not host.calls


def test_missing_explicit_egress_cannot_report_success(setup):
    settings, host = setup
    settings.egress_interface = "absent"
    with pytest.raises(ControlPlaneError):
        bootstrap(settings)
    assert not host.rules
    assert not host.addresses
