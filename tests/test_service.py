"""Exercise durable service invariants with SQLite and a typed kernel simulator."""

import base64
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import service as service_module
from errors import (
    ConflictError,
    InputError,
    NotFoundError,
    StorageError,
    WireGuardError,
)
from models import CreateResult, PeerCreate
from service import PeerService
from settings import Settings
from storage import Store
from wireguard import PeerStats, Snapshot, WireGuard


def key(number: int) -> str:
    return base64.b64encode(number.to_bytes(32, "big")).decode("ascii")


def stats(public_key: str, address: str) -> PeerStats:
    return PeerStats(public_key, (address,), "192.0.2.1:4567", 123, 100, 200, 25)


class FakeWireGuard(WireGuard):
    def __init__(self) -> None:
        super().__init__()
        self.peers: dict[str, PeerStats] = {}
        self.server_key = key(1)
        self.generated = 0
        self.observations = 0
        self.events: list[tuple[str, str]] = []
        self.fail_observe = False
        self.fail_add = False
        self.fail_remove = False
        self.noop_add = False
        self.noop_remove = False
        self.before_add: Callable[[str, str], None] | None = None
        self.before_remove: Callable[[str], None] | None = None
        self.after_add: Callable[[], None] | None = None

    def snapshot(self) -> Snapshot:
        self.observations += 1
        if self.fail_observe:
            raise WireGuardError("Private subprocess detail must not be persisted")
        return Snapshot(self.server_key, dict(self.peers))

    def gen_keys(self) -> tuple[str, str]:
        self.generated += 1
        return key(1000 + self.generated), key(2000 + self.generated)

    def add_peer(self, public_key: str, address: str) -> None:
        self.events.append(("add", public_key))
        if self.before_add is not None:
            self.before_add(public_key, address)
        if self.fail_add:
            raise WireGuardError("secret-key-and-stderr")
        if not self.noop_add:
            self.peers[public_key] = stats(public_key, address)
        if self.after_add is not None:
            self.after_add()

    def remove_peer(self, public_key: str) -> None:
        self.events.append(("remove", public_key))
        if self.before_remove is not None:
            self.before_remove(public_key)
        if self.fail_remove:
            raise WireGuardError("secret-key-and-stderr")
        if not self.noop_remove:
            self.peers.pop(public_key, None)


@pytest.fixture
def node(tmp_path: Path) -> PeerService:
    settings = Settings(
        api_token="test-secret",
        server_endpoint="node.example.org:51820",
        data_dir=tmp_path,
    )
    store = Store(
        tmp_path / "peers.sqlite3", settings.interface, str(settings.server_address)
    )
    service = PeerService(settings, store, FakeWireGuard())
    service.initialize()
    return service


def backend(node: PeerService) -> FakeWireGuard:
    assert isinstance(node.backend, FakeWireGuard)
    return node.backend


def create(
    node: PeerService, request_key: str = "request", **values: object
) -> CreateResult:
    return node.create(
        PeerCreate.model_validate({"key_mode": "generated"} | values), request_key
    )


def test_reservation_precedes_kernel_and_credentials_are_creation_only(
    node: PeerService,
) -> None:
    kernel = backend(node)

    def verify_reserved(public_key: str, address: str) -> None:
        records = node.store.list_peers()
        assert len(records) == 1
        assert records[0].public_key == public_key
        assert records[0].address + "/32" == address
        assert records[0].state == "pending"
        assert node.store.pending_count() == 1

    kernel.before_add = verify_reserved
    result = create(node)
    assert result.operation.status == "complete"
    assert result.peer.state == "active" and result.peer.applied
    assert result.private_key == key(1001)
    assert result.client_config is not None
    assert f"PrivateKey = {result.private_key}" in result.client_config
    assert "::/0" not in result.client_config
    assert result.peer.observation == stats(
        result.peer.public_key, result.peer.address + "/32"
    )
    assert node.get(result.peer.id) == result.peer
    assert node.operation(result.operation.id).status == "complete"
    assert node.is_ready()
    template = node.template(result.peer.id)
    assert "PrivateKey = <YOUR_PRIVATE_KEY>" in template
    assert result.private_key not in template
    assert result.private_key not in node.get(result.peer.id).model_dump_json()
    assert result.private_key not in node.store.path.read_bytes().decode("latin1")


def test_failed_reservation_never_mutates_kernel(
    node: PeerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise StorageError("Storage unavailable")

    monkeypatch.setattr(node.store, "create_peer", unavailable)
    with pytest.raises(StorageError):
        create(node)
    assert backend(node).events == []
    assert node.store.list_peers() == []


@pytest.mark.parametrize("failure", ["fail_add", "noop_add"])
def test_failed_apply_retains_pending_and_credentials_then_recovers(
    node: PeerService, failure: str
) -> None:
    kernel = backend(node)
    setattr(kernel, failure, True)
    result = create(node)
    assert result.operation.status == "pending"
    assert result.operation.error == "wireguard_unavailable"
    assert result.peer.state == "pending" and not result.peer.applied
    assert result.private_key == key(1001)
    assert result.client_config is not None
    assert node.store.get_peer(result.peer.id).state == "pending"
    assert node.operation(result.operation.id).error == "wireguard_unavailable"
    assert "secret-key" not in result.model_dump_json()
    assert not node.is_ready()
    replay = create(node)
    assert replay.replayed and replay.operation.id == result.operation.id
    assert replay.private_key is None and replay.client_config is None
    assert kernel.generated == 1
    setattr(kernel, failure, False)
    node.reconcile()
    assert node.is_ready()
    assert node.get(result.peer.id).applied
    assert node.operation(result.operation.id).status == "complete"


def test_post_apply_observation_failure_still_returns_pending_credentials(
    node: PeerService,
) -> None:
    kernel = backend(node)
    kernel.after_add = lambda: setattr(kernel, "fail_observe", True)
    result = create(node)
    assert result.operation.status == "pending"
    assert not result.peer.applied
    assert result.private_key is not None and result.client_config is not None
    assert f"PublicKey = {kernel.server_key}" in result.client_config
    assert result.peer.observation is None
    kernel.after_add = None
    kernel.fail_observe = False
    node.reconcile()
    assert node.is_ready()


@pytest.mark.parametrize("failure", ["fail_remove", "noop_remove", "fail_observe"])
def test_delete_pending_survives_restart_and_never_resurrects(
    node: PeerService, failure: str
) -> None:
    result = create(node)
    kernel = backend(node)
    kernel.events.clear()

    def verify_intent(public_key: str) -> None:
        assert public_key == result.peer.public_key
        assert node.store.get_peer(result.peer.id).state == "deleting"

    kernel.before_remove = verify_intent
    setattr(kernel, failure, True)
    operation = node.delete(result.peer.id)
    assert operation.status == "pending"
    assert node.store.get_peer(result.peer.id).state == "deleting"
    assert node.delete(result.peer.id).id == operation.id
    with pytest.raises(ConflictError, match="revoked"):
        create(node)
    kernel.before_remove = None
    setattr(kernel, failure, False)
    restarted_store = Store(
        node.store.path, node.settings.interface, str(node.settings.server_address)
    )
    restarted = PeerService(node.settings, restarted_store, kernel)
    restarted.initialize()
    restarted.reconcile()
    assert restarted.store.get_peer(result.peer.id) is None
    assert restarted.operation(operation.id).status == "complete"
    assert result.peer.public_key not in kernel.peers
    assert not any(action == "add" for action, _ in kernel.events)
    assert restarted.is_ready()
    with pytest.raises(ConflictError, match="revoked"):
        create(restarted)


def test_exclusive_reconciliation_removes_unknown_and_repairs_mismatch(
    node: PeerService,
) -> None:
    result = create(node)
    kernel = backend(node)
    kernel.peers[key(999)] = stats(key(999), "10.13.13.99/32")
    kernel.peers[result.peer.public_key] = stats(result.peer.public_key, "0.0.0.0/0")
    assert not node.is_ready()
    node.reconcile()
    assert set(kernel.peers) == {result.peer.public_key}
    assert kernel.peers[result.peer.public_key].allowed_ips == (
        result.peer.address + "/32",
    )
    assert node.is_ready()
    kernel.peers.clear()
    assert not node.is_ready()
    node.reconcile()
    assert node.get(result.peer.id).applied


def test_reconcile_detects_nonconvergence(node: PeerService) -> None:
    kernel = backend(node)
    kernel.peers[key(999)] = stats(key(999), "10.13.13.99/32")
    kernel.noop_remove = True
    with pytest.raises(WireGuardError, match="converged"):
        node.reconcile()
    assert not node.is_ready()


def test_failed_observation_blocks_creation_and_readiness(node: PeerService) -> None:
    kernel = backend(node)
    kernel.fail_observe = True
    with pytest.raises(WireGuardError):
        create(node)
    with pytest.raises(WireGuardError):
        node.is_ready()
    assert kernel.generated == 0 and kernel.events == []
    assert node.store.list_peers() == []
    node.initialize()
    kernel.fail_observe = False
    node.reconcile()
    assert node.is_ready()


def test_pending_delete_does_not_release_address(
    node: PeerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = create(node)
    backend(node).fail_remove = True
    assert node.delete(first.peer.id).status == "pending"
    # Keep the pending delete durable while allowing an otherwise healthy inventory.
    monkeypatch.setattr(node, "reconcile", lambda: None)
    with pytest.raises(ConflictError, match="reserved"):
        create(node, "requested", address=first.peer.address)
    second = create(node, "automatic")
    assert second.peer.address != first.peer.address
    assert node.server().reserved == 2


def test_idempotency_replay_mismatch_and_revocation(node: PeerService) -> None:
    result = create(node)
    events = list(backend(node).events)
    replay = create(node)
    assert replay.replayed and replay.peer == result.peer
    assert replay.operation.id == result.operation.id
    assert replay.private_key is None and replay.client_config is None
    assert backend(node).generated == 1 and backend(node).events == events
    assert len(node.store.list_peers()) == 1
    with pytest.raises(ConflictError, match="different request") as exc:
        create(node, address="10.13.13.3")
    assert exc.value.status_code == 409
    assert node.delete(result.peer.id).status == "complete"
    with pytest.raises(ConflictError, match="revoked"):
        create(node)


def test_external_key_explicit_address_and_duplicate_key(node: PeerService) -> None:
    result = create(node, key_mode="external", public_key=key(10), address="10.13.13.5")
    assert result.peer.address == "10.13.13.5"
    assert result.peer.public_key == key(10)
    assert result.private_key is None and result.client_config is None
    assert backend(node).generated == 0
    events = list(backend(node).events)
    with pytest.raises(ConflictError) as exc:
        create(node, "other", key_mode="external", public_key=key(10))
    assert exc.value.status_code == 409
    assert backend(node).events == events


@pytest.mark.parametrize(
    "address", ["10.13.13.1", "10.13.13.0", "10.13.13.255", "10.13.14.1"]
)
def test_forbidden_addresses_are_422_without_keys_or_kernel_changes(
    node: PeerService, address: str
) -> None:
    with pytest.raises(InputError) as exc:
        create(node, address=address)
    assert exc.value.status_code == 422
    assert backend(node).generated == 0 and backend(node).events == []


def test_pool_exhaustion_is_409(tmp_path: Path) -> None:
    settings = Settings(
        api_token="test-secret",
        server_endpoint="node.example.org",
        server_address="10.0.0.1/30",
    )
    node = PeerService(
        settings,
        Store(tmp_path / "small.sqlite3", "wg0", "10.0.0.1/30"),
        FakeWireGuard(),
    )
    node.initialize()
    assert create(node).peer.address == "10.0.0.2"
    with pytest.raises(ConflictError, match="exhausted") as exc:
        create(node, "second")
    assert exc.value.status_code == 409
    server = node.server()
    assert (server.capacity, server.reserved, server.available) == (1, 1, 0)
    assert server.endpoint == "node.example.org:51820"
    assert server.address == "10.0.0.1/30" and server.pool == "10.0.0.0/30"
    assert server.public_key == backend(node).server_key and server.interface == "wg0"


def test_snapshot_ttl_force_failure_and_cache_invalidation(
    node: PeerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [100.0]
    monkeypatch.setattr(service_module.time, "monotonic", lambda: clock[0])
    kernel = backend(node)
    first = node.snapshot(force=True)
    count = kernel.observations
    clock[0] += node.settings.snapshot_ttl / 2
    assert node.snapshot() is first and kernel.observations == count
    clock[0] += node.settings.snapshot_ttl / 2
    assert node.snapshot() is not first and kernel.observations == count + 1
    kernel.fail_observe = True
    with pytest.raises(WireGuardError):
        node.snapshot(force=True)
    with pytest.raises(WireGuardError):
        node.snapshot()
    assert kernel.observations == count + 3
    kernel.fail_observe = False
    assert node.snapshot().public_key == kernel.server_key
    result = create(node)
    node.snapshot()
    assert node.delete(result.peer.id).status == "complete"
    assert result.peer.public_key not in node.snapshot().peers


def test_pagination_and_missing_resources(node: PeerService) -> None:
    assert node.list(2, None).items == []
    created = [create(node, str(index)) for index in range(3)]
    expected = sorted(result.peer.id for result in created)
    first = node.list(2, None)
    assert [peer.id for peer in first.items] == expected[:2]
    assert first.next_cursor == expected[1]
    last = node.list(2, first.next_cursor)
    assert [peer.id for peer in last.items] == expected[2:]
    assert last.next_cursor is None
    for lookup in (node.get, node.operation, node.delete, node.template):
        with pytest.raises(NotFoundError) as exc:
            lookup("missing")
        assert exc.value.status_code == 404


@pytest.mark.parametrize("kind", ["create", "delete"])
def test_storage_commit_failure_is_pending_and_retry_recovers(
    node: PeerService, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    original = node.store.complete_operation

    def failed_commit(operation_id: str) -> None:
        raise StorageError("commit-secret-stderr")

    initial = create(node) if kind == "delete" else None
    monkeypatch.setattr(node.store, "complete_operation", failed_commit)
    if initial is None:
        result = create(node)
        operation = result.operation
        peer_id = result.peer.id
        assert result.peer.state == "pending" and not result.peer.applied
        assert result.private_key is not None and result.client_config is not None
    else:
        operation = node.delete(initial.peer.id)
        peer_id = initial.peer.id
        assert node.store.get_peer(peer_id).state == "deleting"
    assert operation.status == "pending" and operation.error == "storage_unavailable"
    assert node.operation(operation.id).status == "pending"
    assert "commit-secret" not in str(node.operation(operation.id))
    assert not node.is_ready()
    monkeypatch.setattr(node.store, "complete_operation", original)
    node.reconcile()
    assert node.operation(operation.id).status == "complete"
    assert node.is_ready()
    if kind == "create":
        assert node.get(peer_id).applied
    else:
        with pytest.raises(NotFoundError):
            node.get(peer_id)


def test_failure_to_record_error_preserves_original_pending_result(
    node: PeerService, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    backend(node).fail_add = True

    def failed_error_write(operation_id: str, error: str) -> None:
        raise StorageError("secret from database")

    monkeypatch.setattr(node.store, "fail_operation", failed_error_write)
    result = create(node)
    assert result.operation.status == "pending"
    assert result.operation.error == "wireguard_unavailable"
    assert node.store.pending_count() == 1
    assert "Pending operation" in caplog.text
    assert "secret" not in caplog.text


def test_concurrent_creates_across_services_share_exclusive_allocation(
    node: PeerService,
) -> None:
    services = [node] + [
        PeerService(
            node.settings,
            Store(node.store.path, "wg0", str(node.settings.server_address)),
            node.backend,
        )
        for _ in range(3)
    ]

    def allocate(index: int) -> CreateResult:
        return create(services[index % len(services)], f"concurrent-{index}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(allocate, range(12)))
    assert len({result.peer.address for result in results}) == 12
    assert len({result.peer.public_key for result in results}) == 12
    assert all(result.operation.status == "complete" for result in results)
    assert len(node.store.list_peers()) == len(backend(node).peers) == 12
    assert node.is_ready()


def test_applied_view_requires_active_state_and_exact_allowed_ips(
    node: PeerService,
) -> None:
    result = create(node)
    record = node.store.get_peer(result.peer.id)
    assert record is not None
    snapshot = node.snapshot(force=True)
    assert not node._view(replace(record, state="pending"), snapshot).applied
    assert not node._view(record, Snapshot(snapshot.public_key, {})).applied
    wrong = replace(
        snapshot.peers[record.public_key],
        allowed_ips=(record.address + "/32", "0.0.0.0/0"),
    )
    assert not node._view(
        record, Snapshot(snapshot.public_key, {record.public_key: wrong})
    ).applied
