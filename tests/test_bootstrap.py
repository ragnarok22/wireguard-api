"""Bootstrap contracts use a stateful, unprivileged kernel/command simulator."""

import base64
import json
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
        if command == ["ip", "-j", "link", "show"]:
            output = json.dumps(
                [{"ifname": "wgtest", "linkinfo": {"info_kind": self.kind}}]
                if self.exists
                else []
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
                "wg", "set", "wgtest", "listen-port", "51900",
                "private-key", "/dev/stdin",
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
        interface="wgtest", server_address=IPv4Interface("10.77.0.1/24"),
        listen_port=51900, data_dir=tmp_path, egress_interface=None,
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
        "-s 10.77.0.0/24 -o ens5 -j MASQUERADE" in " ".join(rule)
        for rule in host.rules
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


@pytest.mark.parametrize("addresses", [
    [{"family": "inet", "local": "10.77.0.9", "prefixlen": 24}],
    [{"family": "inet", "local": "10.77.0.1", "prefixlen": 32}],
    [{"family": "inet", "local": "10.77.0.1", "prefixlen": 24},
     {"family": "inet", "local": "192.168.3.1", "prefixlen": 24}],
    [{"family": "inet6", "local": "fe80::1", "prefixlen": 64}],
])
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


@pytest.mark.parametrize("field,value", [
    ("interface", "wgother"),
    ("server_address", IPv4Interface("10.78.0.1/24")),
])
def test_persistent_identity_cannot_accidentally_migrate(setup, field, value):
    settings, host = setup
    bootstrap(settings)
    host.calls.clear()
    setattr(settings, field, value)
    with pytest.raises(ControlPlaneError):
        bootstrap(settings)
    assert not host.calls


@pytest.mark.parametrize("route", [[], [{}], [{"dev": "wgtest"}],
                                    [{"dev": "bad name"}], [{"dev": "a"}, {"dev": "b"}]])
def test_route_must_identify_one_real_egress_without_guessing(setup, route):
    settings, host = setup
    host.route = route
    with pytest.raises(ControlPlaneError):
        bootstrap(settings)
    assert not host.rules


@pytest.mark.parametrize("prefix", [
    ["ip", "link", "add"], ["ip", "address", "add"], ["wg", "set"],
    ["ip", "link", "set"], ["sysctl", "-w"], ["iptables", "-w"],
])
def test_stage_failure_is_safe_and_recoverable(setup, prefix):
    settings, host = setup
    host.fail = lambda command: command[:len(prefix)] == prefix
    with pytest.raises(ControlPlaneError) as error:
        bootstrap(settings)
    assert PRIVATE not in str(error.value)
    assert error.value.__cause__ is None
    host.fail = None
    bootstrap(settings)
    assert host.public_key == PUBLIC and len(host.rules) == 3


@pytest.mark.parametrize("key", ["invalid", "", PRIVATE + "\n" + OTHER])
def test_invalid_persistent_keys_fail_closed(setup, key):
    settings, host = setup
    (settings.data_dir / "server_private.key").write_text(key)
    with pytest.raises(ControlPlaneError):
        bootstrap(settings)
    assert not host.calls
