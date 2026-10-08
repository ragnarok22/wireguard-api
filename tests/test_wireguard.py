import subprocess
from unittest.mock import Mock

import pytest

from wireguard import WireGuard, WireGuardError


@pytest.fixture()
def wg(tmp_path):
    return WireGuard(interface="wg-test", storage_path=str(tmp_path / "peers.json"))


def test_allocate_next_ip_skips_used_and_server_ip(wg):
    assert wg.allocate_next_ip("10.0.0.1/24", {"10.0.0.2", "10.0.0.3"}) == "10.0.0.4"


def test_allocate_next_ip_raises_when_full(wg):
    with pytest.raises(WireGuardError, match="No available IPs"):
        wg.allocate_next_ip("10.0.0.1/30", {"10.0.0.2"})


def test_allocate_next_ip_invalid_cidr(wg):
    with pytest.raises(WireGuardError, match="Invalid subnet CIDR"):
        wg.allocate_next_ip("not-a-cidr", set())


def test_list_peers_parses_wg_dump_and_skips_invalid_rows(wg, monkeypatch):
    dump = (
        "private public 51820 off\n"
        "\n"
        "truncated= preshared endpoint\n"
        "invalid preshared endpoint 10.0.0.9/32 0 0 0 off\n"
        "pubkey= preshared 1.2.3.4:51820 10.0.0.2/32,10.0.0.3/32 100 200 300 off\n"
    )
    run = Mock(return_value=dump)
    monkeypatch.setattr(wg, "_run", run)

    assert wg.list_peers() == {
        "pubkey=": {
            "preshared_key": "preshared",
            "endpoint": "1.2.3.4:51820",
            "allowed_ips": ["10.0.0.2/32", "10.0.0.3/32"],
            "latest_handshake": "100",
            "transfer_rx": "200",
            "transfer_tx": "300",
            "persistent_keepalive": "off",
        }
    }
    run.assert_called_once_with(["wg", "show", "wg-test", "dump"])


def test_list_peers_returns_empty_on_error(wg, monkeypatch):
    monkeypatch.setattr(wg, "_run", Mock(side_effect=WireGuardError("boom")))
    assert wg.list_peers() == {}


def test_run_uses_argument_list_and_strips_output(wg, monkeypatch):
    command = ["wg", "show", "wg-test", "dump"]
    check_output = Mock(return_value=" \npeer data\t\n")
    monkeypatch.setattr("wireguard.subprocess.check_output", check_output)

    assert wg._run(command) == "peer data"
    check_output.assert_called_once_with(command, stderr=subprocess.STDOUT, text=True)


def test_run_wraps_command_failure_and_logs_output(wg, monkeypatch, caplog):
    command = ["wg", "set", "wg-test", "peer", "pub", "remove"]
    error = subprocess.CalledProcessError(1, command, output="Operation not permitted")
    monkeypatch.setattr("wireguard.subprocess.check_output", Mock(side_effect=error))

    with pytest.raises(WireGuardError, match="Operation not permitted") as caught:
        wg._run(command)

    assert caught.value.__cause__ is error
    assert "Command failed" in caplog.text
    assert "Operation not permitted" in caplog.text


def test_run_missing_wg_show_returns_empty_output(wg, monkeypatch, caplog):
    monkeypatch.setattr(
        "wireguard.subprocess.check_output", Mock(side_effect=FileNotFoundError("wg"))
    )

    assert wg._run(["wg", "show", "wg-test", "dump"]) == ""
    assert "wg command not found" in caplog.text


@pytest.mark.parametrize(
    "command", [["wg", "genkey"], ["ip", "addr", "show", "wg-test"]]
)
def test_run_missing_mutation_or_ip_command_raises(wg, monkeypatch, command):
    error = FileNotFoundError(command[0])
    monkeypatch.setattr("wireguard.subprocess.check_output", Mock(side_effect=error))

    with pytest.raises(WireGuardError, match="WireGuard command not found") as caught:
        wg._run(command)

    assert caught.value.__cause__ is error


def test_gen_keys_pipes_private_key_to_public_key_process(wg, monkeypatch):
    check_output = Mock(return_value="private-key\n")
    process = Mock(returncode=0)
    process.communicate.return_value = ("public-key\n", "")
    popen = Mock(return_value=process)
    monkeypatch.setattr("wireguard.subprocess.check_output", check_output)
    monkeypatch.setattr("wireguard.subprocess.Popen", popen)

    assert wg.gen_keys() == ("private-key", "public-key")
    check_output.assert_called_once_with(
        ["wg", "genkey"], stderr=subprocess.STDOUT, text=True
    )
    popen.assert_called_once_with(
        ["wg", "pubkey"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    process.communicate.assert_called_once_with(input="private-key")


def test_gen_keys_reports_public_key_process_failure(wg, monkeypatch):
    monkeypatch.setattr(wg, "_run", Mock(return_value="private-key"))
    process = Mock(returncode=1)
    process.communicate.return_value = ("", "invalid private key")
    monkeypatch.setattr("wireguard.subprocess.Popen", Mock(return_value=process))

    with pytest.raises(
        WireGuardError, match="Failed to generate pubkey: invalid private key"
    ):
        wg.gen_keys()


def test_gen_keys_does_not_start_public_key_process_when_genkey_fails(wg, monkeypatch):
    monkeypatch.setattr(wg, "_run", Mock(side_effect=WireGuardError("genkey failed")))
    popen = Mock()
    monkeypatch.setattr("wireguard.subprocess.Popen", popen)

    with pytest.raises(WireGuardError, match="genkey failed"):
        wg.gen_keys()

    popen.assert_not_called()


def test_get_interface_subnet_parses_ip_output(wg, monkeypatch):
    run = Mock(return_value="7: wg-test inet 10.13.13.1/24 scope global wg-test")
    monkeypatch.setattr(wg, "_run", run)

    assert wg.get_interface_subnet() == "10.13.13.1/24"
    run.assert_called_once_with(["ip", "-o", "-f", "inet", "addr", "show", "wg-test"])


@pytest.mark.parametrize("output", ["", "7: wg-test scope global"])
def test_get_interface_subnet_reports_missing_cidr(wg, monkeypatch, output):
    monkeypatch.setattr(wg, "_run", Mock(return_value=output))

    with pytest.raises(WireGuardError, match="Could not find CIDR") as caught:
        wg.get_interface_subnet()

    assert isinstance(caught.value.__cause__, WireGuardError)
    assert "Failed to get subnet for wg-test" in str(caught.value)


def test_get_interface_subnet_preserves_command_failure_cause(wg, monkeypatch):
    error = WireGuardError("device not found")
    monkeypatch.setattr(wg, "_run", Mock(side_effect=error))

    with pytest.raises(
        WireGuardError, match="Failed to get subnet for wg-test"
    ) as caught:
        wg.get_interface_subnet()

    assert caught.value.__cause__ is error
