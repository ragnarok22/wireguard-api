"""Serialize durable intent and kernel changes; reconcile after interruptions."""

import hashlib
import logging
import threading
import time
from dataclasses import replace
from ipaddress import IPv4Address

from configuration import client_config
from errors import ConflictError, ControlPlaneError, InputError, NotFoundError
from models import CreateResult, PeerCreate, PeerPage, PeerView, ServerView
from settings import Settings
from storage import OperationRecord, PeerRecord, Store
from wireguard import Snapshot, WireGuard, WireGuardError

logger = logging.getLogger(__name__)


class PeerService:
    def __init__(self, settings: Settings, store: Store, backend: WireGuard) -> None:
        self.settings = settings
        self.store = store
        self.backend = backend
        self.started_at = time.monotonic()
        self._mutex = threading.RLock()
        self._snapshot: Snapshot | None = None
        self._observed_at = 0.0

    def snapshot(self, *, force: bool = False) -> Snapshot:
        with self._mutex:
            if (
                not force
                and self._snapshot is not None
                and time.monotonic() - self._observed_at < self.settings.snapshot_ttl
            ):
                return self._snapshot
            self._snapshot = None
            snapshot = self.backend.snapshot()
            self._snapshot = snapshot
            self._observed_at = time.monotonic()
            return snapshot

    def initialize(self) -> None:
        self.store.initialize()
        try:
            self.reconcile()
        except ControlPlaneError:
            # Liveness stays available; readiness reports failure and the loop retries.
            logger.warning("Initial WireGuard reconciliation is pending")

    @staticmethod
    def _matches(record: PeerRecord, snapshot: Snapshot) -> bool:
        stats = snapshot.peers.get(record.public_key)
        return stats is not None and stats.allowed_ips == (f"{record.address}/32",)

    def _view(self, record: PeerRecord, snapshot: Snapshot) -> PeerView:
        return PeerView(
            id=record.id,
            public_key=record.public_key,
            address=record.address,
            state=record.state,
            created_at=record.created_at,
            applied=record.state == "active" and self._matches(record, snapshot),
            observation=snapshot.peers.get(record.public_key),
        )

    def _apply(self, operation: OperationRecord) -> OperationRecord:
        try:
            if operation.kind == "create":
                self.backend.add_peer(operation.public_key, f"{operation.address}/32")
            else:
                self.backend.remove_peer(operation.public_key)
            observed = self.snapshot(force=True).peers.get(operation.public_key)
            if operation.kind == "create":
                if observed is None or observed.allowed_ips != (
                    f"{operation.address}/32",
                ):
                    raise WireGuardError("Peer application could not be verified")
            elif observed is not None:
                raise WireGuardError("Peer revocation could not be verified")
            self.store.complete_operation(operation.id)
            return replace(operation, status="complete", error=None)
        except ControlPlaneError as exc:
            try:
                self.store.fail_operation(operation.id, exc.code)
            except ControlPlaneError:
                logger.warning("Pending operation could not record its last error")
            return replace(operation, error=exc.code)
        finally:
            self._snapshot = None

    def reconcile(self) -> None:
        with self._mutex, self.store.lock():
            # Observe before applying anything: an unknown inventory is never empty.
            self.snapshot(force=True)
            for operation in self.store.pending_operations():
                self._apply(operation)
            records = self.store.list_peers()
            desired = {
                peer.public_key: peer for peer in records if peer.state != "deleting"
            }
            snapshot = self.snapshot(force=True)
            for key in snapshot.peers.keys() - desired.keys():
                self.backend.remove_peer(key)
            for record in desired.values():
                if record.state == "active" and not self._matches(record, snapshot):
                    self.backend.add_peer(record.public_key, f"{record.address}/32")
            final = self.snapshot(force=True)
            if set(final.peers) != set(desired) or any(
                not self._matches(record, final) for record in desired.values()
            ):
                raise WireGuardError("WireGuard state has not converged")

    def is_ready(self) -> bool:
        with self._mutex, self.store.lock():
            self.store.health_check()
            snapshot = self.snapshot(force=True)
            records = self.store.list_peers()
            return (
                self.store.pending_count() == 0
                and set(snapshot.peers) == {record.public_key for record in records}
                and all(self._matches(record, snapshot) for record in records)
            )

    def _address(self, requested: IPv4Address | None) -> str:
        reserved = {record.address for record in self.store.list_peers()}
        server = self.settings.server_address
        if requested is not None:
            if requested not in server.network or requested in (
                server.ip,
                server.network.network_address,
                server.network.broadcast_address,
            ):
                raise InputError("Address must be a usable client IP inside the pool")
            if str(requested) in reserved:
                raise ConflictError("Client address is already reserved")
            return str(requested)
        for address in server.network.hosts():
            if address != server.ip and str(address) not in reserved:
                return str(address)
        raise ConflictError("The client address pool is exhausted")

    def create(self, request: PeerCreate, request_key: str) -> CreateResult:
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        with self._mutex, self.store.lock():
            previous = self.store.find_request(request_key)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise ConflictError(
                        "Idempotency key was used for a different request"
                    )
                record = self.store.get_peer(previous.peer_id)
                if record is None or record.state == "deleting":
                    raise ConflictError("The original peer has been revoked")
                return CreateResult(
                    peer=self._view(record, self.snapshot(force=True)),
                    operation=previous,
                    replayed=True,
                )
            # Reconcile exclusivity before allocation; failures cannot free addresses.
            self.reconcile()
            snapshot = self.snapshot()
            address = self._address(request.address)
            private_key = None
            public_key = request.public_key
            if public_key is None:
                private_key, public_key = self.backend.gen_keys()
            peer, operation = self.store.create_peer(
                public_key, address, request_key, fingerprint
            )
            operation = self._apply(operation)
            peer = replace(
                peer, state="active" if operation.status == "complete" else "pending"
            )
            # A failed post-write observation must not lose the durable operation ID
            # or newly generated credentials. Return accepted, not false success.
            try:
                snapshot = self.snapshot(force=True)
            except WireGuardError:
                snapshot = Snapshot(snapshot.public_key, {})
            return CreateResult(
                peer=self._view(peer, snapshot),
                operation=operation,
                private_key=private_key,
                client_config=None
                if private_key is None
                else client_config(
                    self.settings, address, snapshot.public_key, private_key
                ),
            )

    def delete(self, peer_id: str) -> OperationRecord:
        with self._mutex, self.store.lock():
            # Intent is stored even if the interface is currently unavailable.
            return self._apply(self.store.request_delete(peer_id))

    def get(self, peer_id: str) -> PeerView:
        record = self.store.get_peer(peer_id)
        if record is None:
            raise NotFoundError("Peer not found")
        return self._view(record, self.snapshot())

    def list(self, limit: int, after: str | None) -> PeerPage:
        records = self.store.list_peers(limit=limit + 1, after=after)
        snapshot = self.snapshot()
        return PeerPage(
            items=[self._view(record, snapshot) for record in records[:limit]],
            next_cursor=records[limit - 1].id if len(records) > limit else None,
        )

    def operation(self, operation_id: str) -> OperationRecord:
        operation = self.store.get_operation(operation_id)
        if operation is None:
            raise NotFoundError("Operation not found")
        return operation

    def template(self, peer_id: str) -> str:
        peer = self.get(peer_id)
        return client_config(
            self.settings,
            peer.address,
            self.snapshot().public_key,
            "<YOUR_PRIVATE_KEY>",
        )

    def server(self) -> ServerView:
        reserved = len(self.store.list_peers())
        capacity = self.settings.server_address.network.num_addresses - 3
        return ServerView(
            public_key=self.snapshot().public_key,
            endpoint=self.settings.server_endpoint,
            interface=self.settings.interface,
            address=str(self.settings.server_address),
            pool=str(self.settings.server_address.network),
            capacity=capacity,
            reserved=reserved,
            available=capacity - reserved,
        )
