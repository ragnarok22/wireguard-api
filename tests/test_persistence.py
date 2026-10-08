import base64
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest

from errors import ConflictError, NotFoundError, StorageError
from storage import Store


def key(number=1):
    return base64.b64encode(bytes([number]) * 32).decode()


def store_at(tmp_path, **kwargs):
    return Store(tmp_path / "peers.sqlite3", "wg0", "10.0.0.1/24", **kwargs)


def test_write_denied_reports_failure_without_losing_reservation(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    store.initialize()
    peer, operation = store.create_peer(key(), "10.0.0.2")
    real_connect = sqlite3.connect

    def readonly(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.execute("PRAGMA query_only = ON")
        return connection

    with monkeypatch.context() as patch:
        patch.setattr("storage.sqlite3.connect", readonly)
        with pytest.raises(StorageError):
            store.complete_operation(operation.id)
    assert store.get_peer(peer.id) == peer
    assert store.get_operation(operation.id) == operation


def test_database_is_private_before_sqlite_creates_schema(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    real_connect = sqlite3.connect
    observed = []

    def inspect_permissions(*args, **kwargs):
        # SQLite must never create a briefly world-readable database.
        assert store.path.exists()
        observed.append(store.path.stat().st_mode & 0o777)
        assert observed[-1] == 0o600
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("storage.sqlite3.connect", inspect_permissions)
    old_umask = os.umask(0)
    try:
        store.initialize()
    finally:
        os.umask(old_umask)
    assert observed == [0o600]


@pytest.mark.parametrize("interface", ["", "-wg0", "a" * 16, "wg/0", "wg 0", "wg0\n"])
def test_invalid_interface_rejected_before_creating_storage(tmp_path, interface):
    with pytest.raises(StorageError, match="interface"):
        Store(tmp_path / "peers.db", interface, "10.0.0.1/24")
    assert not (tmp_path / "peers.db").exists()


def test_corrupt_legacy_is_never_overwritten_or_partially_imported(tmp_path):
    legacy = tmp_path / "peers.json"
    original = b'{"broken"'
    legacy.write_bytes(original)
    store = store_at(tmp_path, legacy_path=legacy)
    with pytest.raises(StorageError):
        store.initialize()
    assert legacy.read_bytes() == original
    legacy.write_text(json.dumps({key(): {"allowed_ips": ["10.0.0.2/32"]}}))
    store.initialize()
    assert len(store.list_peers()) == 1


def test_lifecycle_durable_replay_history_and_pagination(tmp_path):
    store = store_at(tmp_path)
    store.initialize()
    store.health_check()
    assert store.pending_count() == 0
    assert store.get_peer("missing") is None
    assert store.get_operation("missing") is None
    assert store.latest_operation("missing") is None
    assert store.find_request("missing") is None
    peer, create = store.create_peer(key(), "10.0.0.2/32", "request", "fingerprint")
    other, other_create = store.create_peer(key(2), "10.0.0.3")
    assert create.request_key == "request"
    assert create.fingerprint == "fingerprint"
    assert create.address == peer.address == "10.0.0.2"
    assert store.find_request("request") == create
    assert store.latest_operation(peer.id) == create
    store.fail_operation(create.id, "kernel unavailable")
    failed = store.get_operation(create.id)
    assert failed.error == "kernel unavailable"
    assert failed.status == "pending"
    assert store.get_peer(peer.id) == peer
    assert store.pending_count() == 2
    ordered = sorted([peer, other], key=lambda item: item.id)
    assert store.list_peers() == ordered
    assert store.list_peers(limit=1) == ordered[:1]
    assert store.list_peers(limit=0) == []
    assert store.list_peers(after=ordered[0].id) == ordered[1:]
    assert store.list_peers(after=ordered[-1].id, limit=1) == []
    reopened = store_at(tmp_path)
    reopened.initialize()
    assert reopened.get_operation(create.id) == failed
    assert reopened.pending_operations() == [failed, other_create]
    reopened.complete_operation(create.id)
    assert reopened.get_peer(peer.id).state == "active"
    assert reopened.get_operation(create.id).error is None
    delete = reopened.request_delete(peer.id)
    assert delete.kind == "delete"
    assert reopened.request_delete(peer.id) == delete
    assert reopened.get_peer(peer.id).state == "deleting"
    reopened.fail_operation(delete.id, "remove failed")
    assert reopened.get_peer(peer.id).state == "deleting"
    with pytest.raises(ConflictError):
        reopened.create_peer(key(3), "10.0.0.2")
    reopened.complete_operation(delete.id)
    reopened.complete_operation(delete.id)
    reopened.complete_operation(create.id)
    reopened.fail_operation(delete.id, "stale failure")
    assert reopened.get_peer(peer.id) is None
    assert reopened.get_operation(delete.id).status == "complete"
    assert reopened.get_operation(delete.id).error is None
    assert reopened.latest_operation(peer.id).id == delete.id
    assert reopened.find_request("request").id == create.id
    replacement, _ = reopened.create_peer(key(), "10.0.0.2")
    assert replacement.id != peer.id
    reopened.complete_operation(delete.id)
    assert reopened.get_peer(replacement.id) == replacement


def test_delete_supersedes_pending_create_without_resurrection(tmp_path):
    store = store_at(tmp_path)
    store.initialize()
    peer, create = store.create_peer(key(), "10.0.0.2")
    delete = store.request_delete(peer.id)
    assert store.pending_operations() == [delete]
    assert store.get_operation(create.id).status == "complete"
    store.complete_operation(create.id)
    assert store.get_peer(peer.id).state == "deleting"
    store.complete_operation(delete.id)
    store.complete_operation(create.id)
    assert store.list_peers() == []


def test_latest_operation_follows_commit_order_after_clock_adjustment(
    tmp_path, monkeypatch
):
    store = store_at(tmp_path)
    store.initialize()
    monkeypatch.setattr("storage.time.time", Mock(side_effect=[1000.0, 900.0]))
    peer, create = store.create_peer(key(), "10.0.0.2")
    delete = store.request_delete(peer.id)
    assert delete.created_at < create.created_at
    assert store.latest_operation(peer.id) == delete


@pytest.mark.parametrize("duplicate", ["key", "address", "request"])
def test_unique_reservations_are_atomic(tmp_path, duplicate):
    store = store_at(tmp_path)
    store.initialize()
    peer, operation = store.create_peer(key(), "10.0.0.2", "request", "fp")
    with pytest.raises(ConflictError):
        store.create_peer(
            key() if duplicate == "key" else key(2),
            "10.0.0.2" if duplicate == "address" else "10.0.0.3",
            "request" if duplicate == "request" else "other",
        )
    assert store.list_peers() == [peer]
    assert store.pending_operations() == [operation]


@pytest.mark.parametrize("method", ["request_delete", "complete_operation"])
def test_missing_mutation_is_not_found(tmp_path, method):
    store = store_at(tmp_path)
    store.initialize()
    with pytest.raises(NotFoundError):
        getattr(store, method)("missing")
    with pytest.raises(NotFoundError):
        store.fail_operation("missing", "error")


def test_migration_is_once_only_and_original_preserved(tmp_path):
    legacy = tmp_path / "peers.json"
    original = json.dumps(
        {
            key(): {"allowed_ips": ["10.0.0.2/32"]},
            key(2): {"allowed_ips": ["10.0.0.3/32"]},
        }
    ).encode()
    legacy.write_bytes(original)
    store = store_at(tmp_path, legacy_path=legacy)
    store.initialize()
    peers = store.list_peers()
    assert len(peers) == 2
    assert all(peer.state == "pending" for peer in peers)
    assert {op.peer_id for op in store.pending_operations()} == {p.id for p in peers}
    assert all(op.kind == "create" for op in store.pending_operations())
    for peer in peers:
        store.complete_operation(store.latest_operation(peer.id).id)
        store.complete_operation(store.request_delete(peer.id).id)
    store_at(tmp_path, legacy_path=legacy).initialize()
    assert store.list_peers() == []
    assert all(store.latest_operation(p.id).kind == "delete" for p in peers)
    assert legacy.read_bytes() == original
    legacy.write_bytes(b"invalid, ignored after successful migration")
    store.initialize()
    assert store.list_peers() == []


@pytest.mark.parametrize("initial", [None, "{}"])
def test_missing_or_empty_legacy_is_permanently_marked(tmp_path, initial):
    legacy = tmp_path / "peers.json"
    if initial is not None:
        legacy.write_text(initial)
    store = store_at(tmp_path, legacy_path=legacy)
    store.initialize()
    legacy.write_text(json.dumps({key(): {"allowed_ips": ["10.0.0.2/32"]}}))
    store.initialize()
    assert store.list_peers() == []


@pytest.mark.parametrize(
    "invalid",
    [
        [],
        {"bad-key": {"allowed_ips": ["10.0.0.3/32"]}},
        {key(2): []},
        {key(2): {}},
        {key(2): {"allowed_ips": "10.0.0.3/32"}},
        {key(2): {"allowed_ips": []}},
        {key(2): {"allowed_ips": ["10.0.0.3/32", "10.0.0.4/32"]}},
        {key(2): {"allowed_ips": [42]}},
        *[
            {key(2): {"allowed_ips": [ip]}}
            for ip in [
                "10.0.0.3",
                "10.0.0.3/24",
                "10.1.0.3/32",
                "10.0.0.1/32",
                "10.0.0.0/32",
                "10.0.0.255/32",
                "::1/32",
                "nonsense/32",
                "10.0.0.2/32",
            ]
        ],
    ],
)
def test_invalid_migration_rejects_entire_inventory(tmp_path, invalid):
    legacy = tmp_path / "peers.json"
    inventory = {key(): {"allowed_ips": ["10.0.0.2/32"]}}
    if isinstance(invalid, dict):
        inventory.update(invalid)
    else:
        inventory = invalid
    original = json.dumps(inventory).encode()
    legacy.write_bytes(original)
    store = store_at(tmp_path, legacy_path=legacy)
    with pytest.raises(StorageError):
        store.initialize()
    assert legacy.read_bytes() == original
    legacy.write_text("{}")
    store.initialize()
    assert store.list_peers() == []
    assert store.pending_operations() == []


def test_duplicate_json_keys_rejected(tmp_path):
    legacy = tmp_path / "peers.json"
    legacy.write_text(f'{{"{key()}": {{}}, "{key()}": {{}}}}')
    original = legacy.read_bytes()
    with pytest.raises(StorageError):
        store_at(tmp_path, legacy_path=legacy).initialize()
    assert legacy.read_bytes() == original


@pytest.mark.parametrize(
    "error", [PermissionError("private path"), UnicodeError("bad")]
)
def test_legacy_read_error_is_safe_and_retryable(tmp_path, monkeypatch, error):
    legacy = tmp_path / "peers.json"
    legacy.write_text("{}")
    store = store_at(tmp_path, legacy_path=legacy)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", Mock(side_effect=error))
        with pytest.raises(StorageError, match="Cannot read legacy"):
            store.initialize()
    assert legacy.read_text() == "{}"
    store.initialize()


@pytest.mark.parametrize("identity", ["interface", "server", "version", "missing"])
def test_identity_and_schema_mismatches_rejected(tmp_path, identity):
    store = store_at(tmp_path)
    store.initialize()
    if identity == "interface":
        store = Store(store.path, "wg1", "10.0.0.1/24")
    elif identity == "server":
        store = Store(store.path, "wg0", "10.0.0.5/24")
    else:
        with closing(sqlite3.connect(store.path)) as connection, connection:
            connection.execute(
                "UPDATE metadata SET version = 99"
                if identity == "version"
                else "DELETE FROM metadata"
            )
    with pytest.raises(StorageError, match="identity mismatch"):
        store.initialize()
    with pytest.raises(StorageError):
        store.health_check()


def test_corrupt_db_and_untracked_schema_rejected(tmp_path):
    store = store_at(tmp_path)
    store.path.write_bytes(b"not a SQLite database")
    original = store.path.read_bytes()
    with pytest.raises(StorageError, match="Peer storage unavailable"):
        store.initialize()
    assert store.path.read_bytes() == original
    store.path.unlink()
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("CREATE TABLE unknown (id TEXT)")
    with pytest.raises(StorageError, match="Unrecognized"):
        store.initialize()


@pytest.mark.parametrize("table", ["peers", "operations"])
def test_migration_marker_missing_with_existing_inventory_is_ambiguous(tmp_path, table):
    store = store_at(tmp_path)
    store.initialize()
    peer, op = store.create_peer(key(), "10.0.0.2")
    if table == "operations":
        store.complete_operation(store.request_delete(peer.id).id)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE metadata SET migrated = 0")
    with pytest.raises(StorageError, match="Untracked inventory"):
        store.initialize()
    assert store.get_operation(op.id) is not None


@pytest.mark.parametrize("address", ["bad", "::1/64"])
def test_invalid_server_identity(tmp_path, address):
    with pytest.raises(StorageError):
        Store(tmp_path / "peers.db", "wg0", address)


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf")])
def test_invalid_timeout(tmp_path, timeout):
    with pytest.raises(StorageError):
        store_at(tmp_path, lock_timeout=timeout)


def test_absent_db_is_not_silently_recreated_on_read_or_write(tmp_path):
    store = store_at(tmp_path)
    with pytest.raises(StorageError):
        store.list_peers()
    with pytest.raises(StorageError):
        store.create_peer(key(), "10.0.0.2")
    assert not store.path.exists()


def test_stable_reentrant_lock_and_instance_thread_exclusion(tmp_path):
    store = store_at(tmp_path, lock_timeout=0.05)
    contender = store_at(tmp_path, lock_timeout=0.025)
    with ThreadPoolExecutor(max_workers=1) as executor, store.lock():
        store.initialize()
        lock_path = Path(str(store.path) + ".lock")
        inode = lock_path.stat().st_ino
        with store.lock():
            pass
        with pytest.raises(StorageError, match="timed out"):
            with contender.lock():
                pytest.fail("Must exclude another instance")

        def try_lock():
            with store.lock():
                return True

        with pytest.raises(StorageError, match="timed out"):
            executor.submit(try_lock).result(timeout=2)
    with contender.lock():
        assert lock_path.stat().st_ino == inode
    assert lock_path.stat().st_mode & 0o777 == 0o600
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_lock_waits_and_recovers_from_body_exception(tmp_path):
    store = store_at(tmp_path, lock_timeout=1)
    contender = store_at(tmp_path, lock_timeout=1)
    entered = threading.Event()

    def try_lock():
        entered.set()
        with contender.lock():
            return "acquired"

    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.lock():
            future = executor.submit(try_lock)
            assert entered.wait(timeout=2)
            threading.Event().wait(0.03)
            assert not future.done()
        assert future.result(timeout=2) == "acquired"
    with pytest.raises(RuntimeError):
        with store.lock(), store.lock():
            raise RuntimeError("service failed")
    with contender.lock():
        pass
    with store.lock():
        pass


def test_process_lock_exclusion(tmp_path):
    store = store_at(tmp_path)
    code = (
        "from pathlib import Path; from storage import Store; "
        "s=Store(Path(__import__('sys').argv[1]),'wg0','10.0.0.1/24',"
        "lock_timeout=0.02); "
        "s.initialize()"
    )
    with store.lock():
        result = subprocess.run(
            [sys.executable, "-c", code, str(store.path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode != 0
        assert "StorageError: Storage lock timed out" in result.stderr
    result = subprocess.run(
        [sys.executable, "-c", code, str(store.path)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_concurrent_reservation_has_one_winner(tmp_path):
    store = store_at(tmp_path)
    store.initialize()
    barrier = threading.Barrier(2)

    def reserve(number):
        instance = store_at(tmp_path)
        barrier.wait(timeout=2)
        try:
            return instance.create_peer(key(number), "10.0.0.2")
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, [1, 2]))
    assert sum(result is not None for result in results) == 1
    assert len(store.list_peers()) == store.pending_count() == 1


def test_lock_io_failure_is_reported(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(
            "storage_lock.os.open", Mock(side_effect=PermissionError("secret"))
        )
        with pytest.raises(StorageError, match="lock unavailable"):
            store.initialize()
    store.initialize()


@pytest.mark.parametrize("failure", ["open", "chmod"])
def test_database_permission_failure_prevents_initialization(
    tmp_path, monkeypatch, failure
):
    store = store_at(tmp_path)
    real_open = os.open
    real_fchmod = os.fchmod

    def deny_database_open(path, *args, **kwargs):
        if Path(path) == store.path:
            raise PermissionError("private diagnostic")
        return real_open(path, *args, **kwargs)

    def deny_database_chmod(fd, mode):
        if store.path.exists() and os.fstat(fd).st_ino == store.path.stat().st_ino:
            raise PermissionError("private diagnostic")
        return real_fchmod(fd, mode)

    with monkeypatch.context() as patch:
        if failure == "open":
            patch.setattr("storage.os.open", deny_database_open)
        else:
            patch.setattr("storage.os.fchmod", deny_database_chmod)
        connect = Mock()
        patch.setattr("storage.sqlite3.connect", connect)
        with pytest.raises(
            StorageError, match="^Cannot restrict peer storage permissions$"
        ):
            store.initialize()
        connect.assert_not_called()
    store.initialize()
    store.health_check()


def test_existing_database_permissions_restricted_before_reopen(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    store.initialize()
    peer, operation = store.create_peer(key(), "10.0.0.2")
    store.path.chmod(0o666)
    real_connect = sqlite3.connect

    def inspect_permissions(*args, **kwargs):
        assert store.path.stat().st_mode & 0o777 == 0o600
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("storage.sqlite3.connect", inspect_permissions)
    store.initialize()
    assert store.get_peer(peer.id) == peer
    assert store.get_operation(operation.id) == operation


@pytest.mark.parametrize("interface", ["wg0", "a", "w" * 15, "wg-test.1", "_wg0"])
def test_valid_interface_and_canonical_network_identity(tmp_path, interface):
    store = Store(tmp_path / "peers.db", interface, "10.0.0.1/255.255.255.0")
    store.initialize()
    assert store.server_address == "10.0.0.1/24"
    reopened = Store(store.path, interface, "10.0.0.1/24")
    reopened.initialize()
    reopened.health_check()


def test_deleting_record_without_intent_is_rejected(tmp_path):
    store = store_at(tmp_path)
    store.initialize()
    peer, create = store.create_peer(key(), "10.0.0.2")
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE peers SET state = 'deleting'")
        connection.execute("UPDATE operations SET status = 'complete'")
    with pytest.raises(StorageError, match="no pending operation"):
        store.request_delete(peer.id)
    assert store.get_operation(create.id).status == "complete"


def test_integrity_check_reports_non_ok_result(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    store.initialize()
    real_connect = sqlite3.connect

    class DamagedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "PRAGMA quick_check":
                return super().execute("SELECT 'damaged' AS integrity")
            return super().execute(sql, *args)

    def damaged(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=DamagedConnection)

    monkeypatch.setattr("storage.sqlite3.connect", damaged)
    with pytest.raises(StorageError, match="integrity check"):
        store.health_check()


@pytest.mark.parametrize("kind", ["create", "delete"])
def test_commit_failure_keeps_pending_replay_and_releases_connection(
    tmp_path, monkeypatch, kind
):
    store = store_at(tmp_path)
    store.initialize()
    peer, operation = store.create_peer(key(), "10.0.0.2")
    if kind == "delete":
        store.complete_operation(operation.id)
        operation = store.request_delete(peer.id)
        peer = store.get_peer(peer.id)
    real_connect = sqlite3.connect

    class FailedCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("private diagnostic")

    def failed(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=FailedCommit)

    with monkeypatch.context() as patch:
        patch.setattr("storage.sqlite3.connect", failed)
        with pytest.raises(StorageError, match="^Peer storage unavailable$"):
            store.complete_operation(operation.id)
    assert store.get_peer(peer.id) == peer
    assert store.pending_operations() == [operation]
    store.complete_operation(operation.id)
    if kind == "create":
        assert store.get_peer(peer.id).state == "active"
    else:
        assert store.get_peer(peer.id) is None


@pytest.mark.parametrize("table", ["peers", "operations"])
def test_unsupported_table_structure_rejected_before_use(tmp_path, table):
    store = store_at(tmp_path)
    store.initialize()
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN unsupported TEXT")
    with pytest.raises(StorageError, match="Unrecognized peer storage schema"):
        store.initialize()


def test_migration_sql_failure_rolls_back_every_import_and_marker(
    tmp_path, monkeypatch
):
    legacy = tmp_path / "peers.json"
    original = json.dumps(
        {
            key(): {"allowed_ips": ["10.0.0.2/32"]},
            key(2): {"allowed_ips": ["10.0.0.3/32"]},
        }
    ).encode()
    legacy.write_bytes(original)
    store = store_at(tmp_path, legacy_path=legacy)
    real_connect = sqlite3.connect

    class FailedImport(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO operations") and args[0][4] == key(2):
                raise sqlite3.OperationalError("disk full")
            return super().execute(sql, *args)

    def failed(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=FailedImport)

    with monkeypatch.context() as patch:
        patch.setattr("storage.sqlite3.connect", failed)
        with pytest.raises(StorageError):
            store.initialize()
    assert legacy.read_bytes() == original
    store.initialize()
    assert {peer.public_key for peer in store.list_peers()} == {key(), key(2)}
    assert len(store.pending_operations()) == 2
    store.initialize()
    assert len(store.list_peers()) == 2
