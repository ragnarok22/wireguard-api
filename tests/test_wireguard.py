import base64
import subprocess
import traceback
from dataclasses import FrozenInstanceError, asdict
from unittest.mock import Mock, call

import pytest

from errors import ControlPlaneError, WireGuardError
from wireguard import PeerStats, Snapshot, WireGuard

SERVER_KEY = base64.b64encode(bytes(range(32))).decode("ascii")
PEER_KEY = base64.b64encode(b"\xff" * 32).decode("ascii")
OTHER_KEY = base64.b64encode(b"\xfb" * 32).decode("ascii")
PRIVATE_KEY = base64.b64encode(b"\x01" * 32).decode("ascii")
PSK = base64.b64encode(b"\x02" * 32).decode("ascii")
HEADER = f"{PRIVATE_KEY}\t{SERVER_KEY}\t51820\toff\n"
ROW = (
    f"{PEER_KEY}\t{PSK}\t[2001:db8::1]:51820\t10.0.0.2/32,2001:db8::/64"
    "\t1700000000\t200\t300\t25\n"
)


@pytest.fixture()
def wg():
    return WireGuard(interface="wg-test", timeout=1.25)


@pytest.fixture()
def run(monkeypatch):
    mock = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=HEADER))
    monkeypatch.setattr("wireguard.subprocess.run", mock)
    return mock


def test_defaults_and_valid_interfaces():
    adapter = WireGuard()
    assert adapter.interface == "wg0"
    assert adapter.timeout == 5.0
    for interface in ["a", "wg.test_0-1", "a" * 15, ".wg", "_wg"]:
        assert WireGuard(interface).interface == interface


@pytest.mark.parametrize("interface", ["", "-wg", "a" * 16, "wg/0", "wg 0", "wg\n"])
def test_invalid_interface(interface):
    with pytest.raises(WireGuardError, match="Invalid WireGuard interface"):
        WireGuard(interface)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout(timeout):
    with pytest.raises(WireGuardError, match="Invalid WireGuard timeout"):
        WireGuard(timeout=timeout)


def test_run_uses_bounded_argument_list_and_preserves_output(wg, run):
    command = ["wg", "show", "wg-test", "dump"]
    assert wg._run(command) == HEADER
    run.assert_called_once_with(
        command,
        input=None,
        check=True,
        capture_output=True,
        text=True,
        timeout=1.25,
        shell=False,
    )


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, [PRIVATE_KEY], output=PSK, stderr=PRIVATE_KEY),
        FileNotFoundError(PRIVATE_KEY),
        PermissionError(PRIVATE_KEY),
        OSError(PRIVATE_KEY),
        subprocess.TimeoutExpired([PRIVATE_KEY], 1.25, output=PSK, stderr=PRIVATE_KEY),
    ],
)
@pytest.mark.parametrize("operation", ["snapshot", "list_peers", "gen_keys"])
def test_failed_observation_is_not_a_successful_empty_inventory(
    wg, run, caplog, error, operation
):
    run.side_effect = error
    with pytest.raises(WireGuardError, match="^WireGuard command failed$") as caught:
        getattr(wg, operation)()
    assert isinstance(caught.value, ControlPlaneError)
    assert caught.value.__cause__ is None
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert PRIVATE_KEY not in rendered
    assert PSK not in rendered


def test_snapshot_is_typed_and_never_exports_private_or_preshared_keys(wg, run):
    run.return_value.stdout = HEADER + ROW
    snapshot = wg.snapshot()
    assert snapshot == Snapshot(
        SERVER_KEY,
        {
            PEER_KEY: PeerStats(
                PEER_KEY,
                ("10.0.0.2/32", "2001:db8::/64"),
                "[2001:db8::1]:51820",
                1700000000,
                200,
                300,
                25,
            )
        },
    )
    assert PRIVATE_KEY not in repr(asdict(snapshot))
    assert PSK not in repr(asdict(snapshot))
    assert wg.list_peers() == snapshot.peers
    with pytest.raises(FrozenInstanceError):
        snapshot.public_key = OTHER_KEY
    with pytest.raises(FrozenInstanceError):
        snapshot.peers[PEER_KEY].transfer_rx = 0


@pytest.mark.parametrize("fwmark", ["off", "0", "123", "0xca6c", "0xFFFFFFFF"])
def test_header_only_is_a_successful_empty_snapshot(wg, run, fwmark):
    run.return_value.stdout = f"(none)\t{SERVER_KEY}\t0\t{fwmark}\n"
    assert wg.snapshot() == Snapshot(SERVER_KEY, {})


@pytest.mark.parametrize("keepalive", ["off", "0", "65535"])
def test_unset_peer_values_and_multiple_keys(wg, run, keepalive):
    run.return_value.stdout = (
        HEADER + ROW + f"{OTHER_KEY}\t(none)\t(none)\t(none)\t0\t0\t0\t{keepalive}\n"
    )
    peer = wg.snapshot().peers[OTHER_KEY]
    assert peer == PeerStats(
        OTHER_KEY, (), None, None, 0, 0, 0 if keepalive == "off" else int(keepalive)
    )
    assert "/" in PEER_KEY and "+" in OTHER_KEY and OTHER_KEY.endswith("=")


@pytest.mark.parametrize(
    "dump",
    [
        "",
        "\n",
        HEADER.replace("\t", " "),
        HEADER + "\n",
        HEADER.rstrip("\n") + "\textra\n",
        HEADER.replace(SERVER_KEY, "(none)"),
        HEADER.replace("51820", "65536"),
        HEADER.replace("51820", "-1"),
        HEADER.replace("off", "-1"),
        HEADER.replace("off", "0x"),
        HEADER.replace("off", "4294967296"),
        HEADER + ROW.replace("\t", " "),
        HEADER + ROW + ROW,
        HEADER + ROW.rstrip("\n") + "\textra\n",
        HEADER + "\t".join(ROW.split("\t")[:-1]) + "\n",
    ],
)
def test_malformed_dump_is_rejected_without_leaking_secrets(wg, run, caplog, dump):
    run.return_value.stdout = dump
    with pytest.raises(WireGuardError, match="^Invalid WireGuard dump$") as caught:
        wg.snapshot()
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert PRIVATE_KEY not in rendered
    assert PSK not in rendered


@pytest.mark.parametrize(
    ("column", "value"),
    [
        (0, ""),
        (0, "bad-key="),
        (0, base64.b64encode(b"short").decode("ascii")),
        (0, PEER_KEY[:-2] + "9="),  # Noncanonical Base64 padding bits.
        (2, ""),
        (3, ""),
        (3, "not-a-network"),
        (3, "10.0.0.2/24"),
        (3, "10.0.0.2"),
        (3, "2001:0db8::/64"),
        (3, "10.0.0.2/32,"),
        (4, "-1"),
        (4, "1.2"),
        (4, ""),
        (4, "１"),
        (5, "-1"),
        (5, "+1"),
        (6, "-2"),
        (6, " 2"),
        (7, "-3"),
        (7, "no"),
        (7, "65536"),
    ],
)
def test_invalid_peer_field_rejects_entire_inventory(wg, run, column, value):
    fields = ROW.rstrip("\n").split("\t")
    fields[0] = OTHER_KEY
    fields[column] = value
    run.return_value.stdout = HEADER + ROW + "\t".join(fields) + "\n"
    with pytest.raises(WireGuardError, match="Invalid WireGuard dump"):
        wg.snapshot()


def test_gen_keys_sends_private_key_only_on_bounded_stdin(wg, run):
    run.side_effect = [
        subprocess.CompletedProcess([], 0, stdout=PRIVATE_KEY + "\n"),
        subprocess.CompletedProcess([], 0, stdout=OTHER_KEY + "\n"),
    ]
    assert wg.gen_keys() == (PRIVATE_KEY, OTHER_KEY)
    kwargs = dict(check=True, capture_output=True, text=True, timeout=1.25, shell=False)
    assert run.call_args_list == [
        call(["wg", "genkey"], input=None, **kwargs),
        call(["wg", "pubkey"], input=PRIVATE_KEY + "\n", **kwargs),
    ]


@pytest.mark.parametrize("value", ["", "invalid", PRIVATE_KEY[:-2] + "F="])
@pytest.mark.parametrize("stage", ["private", "public"])
def test_malformed_generated_keys_are_rejected(wg, run, value, stage):
    outputs = [value] if stage == "private" else [PRIVATE_KEY, value]
    run.side_effect = [subprocess.CompletedProcess([], 0, stdout=s) for s in outputs]
    with pytest.raises(WireGuardError, match="Invalid generated WireGuard key"):
        wg.gen_keys()
    assert run.call_count == len(outputs)


def test_public_key_process_failure_is_safe(wg, run, caplog):
    run.side_effect = [
        subprocess.CompletedProcess([], 0, stdout=PRIVATE_KEY),
        subprocess.TimeoutExpired(["wg", "pubkey"], 1.25, stderr=PRIVATE_KEY),
    ]
    with pytest.raises(WireGuardError, match="WireGuard command failed") as caught:
        wg.gen_keys()
    assert PRIVATE_KEY not in "".join(traceback.format_exception(caught.value))
    assert PRIVATE_KEY not in caplog.text


def test_add_and_remove_peer_use_validated_arguments(wg, run):
    assert wg.add_peer(OTHER_KEY, "10.0.0.2/32") is None
    assert wg.remove_peer(PEER_KEY) is None
    assert [c.args[0] for c in run.call_args_list] == [
        ["wg", "set", "wg-test", "peer", OTHER_KEY, "allowed-ips", "10.0.0.2/32"],
        ["wg", "set", "wg-test", "peer", PEER_KEY, "remove"],
    ]


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.2",
        "10.0.0.0/24",
        "10.0.0.2/24",
        "::1/128",
        "bad",
        "10.0.0.2/255.255.255.255",
    ],
)
def test_add_rejects_invalid_or_noncanonical_ipv4_host_network(wg, run, address):
    with pytest.raises(WireGuardError, match="Invalid WireGuard peer"):
        wg.add_peer(PEER_KEY, address)
    run.assert_not_called()


@pytest.mark.parametrize("key", ["", "--help", PEER_KEY[:-2] + "9="])
def test_mutations_reject_invalid_keys_before_subprocess(wg, run, key):
    with pytest.raises(WireGuardError, match="Invalid WireGuard peer"):
        wg.add_peer(key, "10.0.0.2/32")
    with pytest.raises(WireGuardError, match="Invalid WireGuard peer key"):
        wg.remove_peer(key)
    run.assert_not_called()


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_mutation_failures_propagate(wg, run, operation):
    run.side_effect = PermissionError("denied")
    with pytest.raises(WireGuardError, match="WireGuard command failed"):
        if operation == "add":
            wg.add_peer(PEER_KEY, "10.0.0.2/32")
        else:
            wg.remove_peer(PEER_KEY)
