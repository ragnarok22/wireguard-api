import builtins
import json
import logging
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from wireguard import WireGuard, WireGuardError


@pytest.fixture()
def wg(tmp_path):
    return WireGuard(interface="wg-test", storage_path=str(tmp_path / "peers.json"))


def test_save_and_load_peers(wg):
    wg.save_peer_to_storage("pubkey1=", ["10.0.0.2/32"])
    wg.save_peer_to_storage("pubkey2=", ["10.0.0.3/32"])
    stored = {
        "pubkey1=": {"allowed_ips": ["10.0.0.2/32"]},
        "pubkey2=": {"allowed_ips": ["10.0.0.3/32"]},
    }
    assert json.loads(Path(wg.storage_path).read_text()) == stored

    reloaded = WireGuard(storage_path=wg.storage_path)
    assert reloaded.load_peers_from_storage() == stored

    wg.remove_peer_from_storage("pubkey1=")
    assert reloaded.load_peers_from_storage() == {
        "pubkey2=": {"allowed_ips": ["10.0.0.3/32"]}
    }


def test_create_peer_adds_interface_peer_and_persists_it(wg, monkeypatch):
    wg.save_peer_to_storage("existing=", ["10.0.0.2/32"])
    run = Mock(return_value="")
    monkeypatch.setattr(wg, "_run", run)

    wg.create_peer("new=", ["10.0.0.3/32", "10.0.1.0/24"])

    run.assert_called_once_with(
        [
            "wg",
            "set",
            "wg-test",
            "peer",
            "new=",
            "allowed-ips",
            "10.0.0.3/32,10.0.1.0/24",
        ]
    )
    assert wg.load_peers_from_storage() == {
        "existing=": {"allowed_ips": ["10.0.0.2/32"]},
        "new=": {"allowed_ips": ["10.0.0.3/32", "10.0.1.0/24"]},
    }


def test_delete_peer_removes_interface_peer_and_persisted_record(wg, monkeypatch):
    wg.save_peer_to_storage("pubkey1=", ["10.0.0.2/32"])
    wg.save_peer_to_storage("pubkey2=", ["10.0.0.3/32"])
    run = Mock(return_value="")
    monkeypatch.setattr(wg, "_run", run)

    wg.delete_peer("pubkey1=")

    run.assert_called_once_with(["wg", "set", "wg-test", "peer", "pubkey1=", "remove"])
    assert wg.load_peers_from_storage() == {
        "pubkey2=": {"allowed_ips": ["10.0.0.3/32"]}
    }


@pytest.mark.parametrize("operation", ["create", "delete"])
def test_failed_interface_mutation_does_not_modify_storage(wg, monkeypatch, operation):
    wg.save_peer_to_storage("pubkey1=", ["10.0.0.2/32"])
    storage = Path(wg.storage_path)
    original = storage.read_bytes()
    monkeypatch.setattr(
        wg, "_run", Mock(side_effect=WireGuardError("permission denied"))
    )

    with pytest.raises(WireGuardError, match="permission denied"):
        if operation == "create":
            wg.create_peer("new=", ["10.0.0.3/32"])
        else:
            wg.delete_peer("pubkey1=")

    assert storage.read_bytes() == original


def test_constructor_creates_storage_directory(tmp_path):
    storage = tmp_path / "nested" / "config" / "peers.json"

    wg = WireGuard(storage_path=str(storage))

    assert storage.parent.is_dir()
    assert not storage.exists()
    assert wg.load_peers_from_storage() == {}


def test_constructor_accepts_storage_filename_without_parent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    makedirs = Mock()
    monkeypatch.setattr("wireguard.os.makedirs", makedirs)

    wg = WireGuard(storage_path="peers.json")

    assert wg.storage_path == "peers.json"
    assert wg.load_peers_from_storage() == {}
    makedirs.assert_not_called()


def test_constructor_logs_directory_creation_failure(tmp_path, monkeypatch, caplog):
    storage = tmp_path / "blocked" / "peers.json"
    monkeypatch.setattr(
        "wireguard.os.makedirs", Mock(side_effect=PermissionError("denied"))
    )

    wg = WireGuard(storage_path=str(storage))

    assert wg.storage_path == str(storage)
    assert not storage.parent.exists()
    assert "Could not create storage directory" in caplog.text
    assert "denied" in caplog.text


def test_missing_storage_is_an_empty_peer_set(wg):
    assert wg.load_peers_from_storage() == {}
    assert not Path(wg.storage_path).exists()


def test_corrupt_storage_is_logged_and_left_intact(wg, caplog):
    storage = Path(wg.storage_path)
    storage.write_text("{invalid json")

    assert wg.load_peers_from_storage() == {}
    assert "Failed to load peers from storage" in caplog.text
    assert storage.read_text() == "{invalid json"


def test_unreadable_storage_is_logged(wg, monkeypatch, caplog):
    wg.save_peer_to_storage("existing=", ["10.0.0.2/32"])
    monkeypatch.setattr(
        "builtins.open", Mock(side_effect=PermissionError("read denied"))
    )

    assert wg.load_peers_from_storage() == {}
    assert "Failed to load peers from storage: read denied" in caplog.text


def test_storage_write_failure_is_logged_and_existing_data_remains(
    wg, monkeypatch, caplog
):
    wg.save_peer_to_storage("existing=", ["10.0.0.2/32"])
    original = Path(wg.storage_path).read_bytes()
    real_open = builtins.open

    def deny_storage_write(file, mode="r", *args, **kwargs):
        if file == wg.storage_path and mode == "w":
            raise PermissionError("write denied")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", deny_storage_write)

    wg.save_peer_to_storage("new=", ["10.0.0.3/32"])

    assert "Failed to write peers to storage: write denied" in caplog.text
    assert Path(wg.storage_path).read_bytes() == original


@pytest.mark.parametrize("existing_storage", [True, False])
def test_removing_unknown_peer_does_not_write_storage(wg, existing_storage):
    storage = Path(wg.storage_path)
    if existing_storage:
        wg.save_peer_to_storage("existing=", ["10.0.0.2/32"])
        original = storage.read_bytes()

    wg.remove_peer_from_storage("missing=")

    if existing_storage:
        assert storage.read_bytes() == original
    else:
        assert not storage.exists()


def test_restore_peers_replays_commands_without_rewriting_storage(
    wg, monkeypatch, caplog
):
    wg.save_peer_to_storage("pubkey1=", ["10.0.0.2/32"])
    wg.save_peer_to_storage("pubkey2=", ["10.0.0.3/32", "10.0.1.0/24"])
    original = Path(wg.storage_path).read_bytes()
    run = Mock(return_value="")
    monkeypatch.setattr(wg, "_run", run)

    with caplog.at_level(logging.INFO, logger="wireguard"):
        wg.restore_peers()

    assert run.call_args_list == [
        call(
            ["wg", "set", "wg-test", "peer", "pubkey1=", "allowed-ips", "10.0.0.2/32"]
        ),
        call(
            [
                "wg",
                "set",
                "wg-test",
                "peer",
                "pubkey2=",
                "allowed-ips",
                "10.0.0.3/32,10.0.1.0/24",
            ]
        ),
    ]
    assert "Restored 2 peers." in caplog.text
    assert Path(wg.storage_path).read_bytes() == original


def test_restore_continues_after_a_peer_fails(wg, monkeypatch, caplog):
    storage = Path(wg.storage_path)
    storage.write_text(
        json.dumps(
            {
                "broken=": {"allowed_ips": ["invalid"]},
                "working=": {"allowed_ips": ["10.0.0.3/32"]},
                "empty=": {},
            }
        )
    )
    original = storage.read_bytes()
    run = Mock(side_effect=[WireGuardError("invalid allowed IPs"), "", ""])
    monkeypatch.setattr(wg, "_run", run)

    with caplog.at_level(logging.INFO, logger="wireguard"):
        wg.restore_peers()

    assert run.call_args_list == [
        call(["wg", "set", "wg-test", "peer", "broken=", "allowed-ips", "invalid"]),
        call(
            ["wg", "set", "wg-test", "peer", "working=", "allowed-ips", "10.0.0.3/32"]
        ),
        call(["wg", "set", "wg-test", "peer", "empty=", "allowed-ips", ""]),
    ]
    assert "Failed to restore peer broken=: invalid allowed IPs" in caplog.text
    assert "Restored 2 peers." in caplog.text
    assert storage.read_bytes() == original


def test_restore_with_no_storage_does_not_run_commands(wg, monkeypatch, caplog):
    run = Mock()
    monkeypatch.setattr(wg, "_run", run)

    with caplog.at_level(logging.INFO, logger="wireguard"):
        wg.restore_peers()

    run.assert_not_called()
    assert "Restored 0 peers." in caplog.text
    assert not Path(wg.storage_path).exists()
