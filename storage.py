"""Durable desired state; kernel changes are replayed from pending operations.

The service holds ``lock()`` across its read/allocate/kernel/write sequence.
Repository writes themselves use short SQLite transactions. Operation history
deliberately has no peer foreign key: successful deletion retains that history.
"""

import ipaddress
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Literal
from uuid import uuid4

from errors import ConflictError, NotFoundError, StorageError
from keys import validate_key
from legacy import client_address, read_legacy
from storage_lock import StorageLock


@dataclass(frozen=True)
class PeerRecord:
    id: str
    public_key: str
    address: str
    state: Literal["pending", "active", "deleting"]
    created_at: float


@dataclass(frozen=True)
class OperationRecord:
    id: str
    peer_id: str
    kind: Literal["create", "delete"]
    status: Literal["pending", "complete"]
    public_key: str
    address: str
    error: str | None
    created_at: float
    request_key: str | None = None
    fingerprint: str | None = None


_SCHEMA = (
    """CREATE TABLE metadata (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        version INTEGER NOT NULL, interface TEXT NOT NULL,
        server_address TEXT NOT NULL, migrated INTEGER NOT NULL CHECK(migrated IN (0,1))
    )""",
    """CREATE TABLE peers (
        id TEXT PRIMARY KEY, public_key TEXT NOT NULL UNIQUE,
        address TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK(state IN ('pending','active','deleting')),
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE operations (
        id TEXT PRIMARY KEY, peer_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('create','delete')),
        status TEXT NOT NULL CHECK(status IN ('pending','complete')),
        public_key TEXT NOT NULL, address TEXT NOT NULL, error TEXT,
        created_at REAL NOT NULL, request_key TEXT UNIQUE, fingerprint TEXT
    )""",
    "CREATE INDEX operation_peer ON operations(peer_id, created_at)",
    """CREATE UNIQUE INDEX operation_pending ON operations(peer_id)
        WHERE status = 'pending'""",
)


class Store:
    def __init__(
        self,
        path: Path,
        interface: str,
        server_address: str,
        legacy_path: Path | None = None,
        lock_timeout: float = 5.0,
    ) -> None:
        self.path = path.absolute()
        # Match Settings without coupling persistence to its environment loader.
        if re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,14}", interface) is None:
            raise StorageError("Invalid storage interface name")
        self.interface = interface
        try:
            self._server = ipaddress.IPv4Interface(server_address)
        except ValueError as exc:
            raise StorageError("Invalid storage network identity") from exc
        self.server_address = str(self._server)
        self.legacy_path = legacy_path
        self.lock_timeout = lock_timeout
        self._lock = StorageLock(Path(str(self.path) + ".lock"), lock_timeout)

    def lock(self) -> AbstractContextManager[None]:
        return self._lock.acquire()

    def _secure_database(self) -> None:
        """Create without truncation; restrict permissions before SQLite writes."""
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "r+b") as database_file:
                os.fchmod(database_file.fileno(), 0o600)
        except OSError as exc:
            raise StorageError("Cannot restrict peer storage permissions") from exc

    @contextmanager
    def _connection(
        self, *, write: bool = False, initialize: bool = False
    ) -> Iterator[sqlite3.Connection]:
        try:
            if initialize:
                self._secure_database()
            connection = sqlite3.connect(
                self.path.as_uri() + "?mode=rw",
                uri=True,
                timeout=self.lock_timeout,
                isolation_level=None,
            )
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA foreign_keys = ON")
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
                if write:
                    connection.commit()
            finally:
                # Closing also rolls back an unfinished transaction, including when
                # COMMIT itself fails. Never claim kernel/SQLite atomicity.
                connection.close()
        except sqlite3.Error as exc:
            raise StorageError("Peer storage unavailable") from exc

    def initialize(self) -> None:
        with self.lock(), self._connection(write=True, initialize=True) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not tables:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO metadata VALUES (1, 1, ?, ?, 0)",
                    (self.interface, self.server_address),
                )
            elif tables != {"metadata", "peers", "operations"}:
                raise StorageError("Unrecognized peer storage schema")
            self._check_identity(connection)
            # Check structural corruption before trusting the migration marker.
            self._quick_check(connection)
            for table, record in (
                ("peers", PeerRecord),
                ("operations", OperationRecord),
            ):
                columns = [
                    row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                ]
                if columns != [field.name for field in fields(record)]:
                    raise StorageError("Unrecognized peer storage schema")
            migrated = connection.execute("SELECT migrated FROM metadata").fetchone()[0]
            if not migrated:
                if (
                    connection.execute("SELECT count(*) FROM peers").fetchone()[0]
                    or (
                        connection.execute(
                            "SELECT count(*) FROM operations"
                        ).fetchone()[0]
                    )
                ):
                    raise StorageError("Untracked inventory cannot be migrated safely")
                for public_key, address in read_legacy(self.legacy_path, self._server):
                    self._insert_peer(connection, public_key, address, None, None)
                connection.execute("UPDATE metadata SET migrated = 1")

    def _check_identity(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT version, interface, server_address FROM metadata"
        ).fetchall()
        if len(rows) != 1 or tuple(rows[0]) != (
            1,
            self.interface,
            self.server_address,
        ):
            raise StorageError("Peer storage schema or network identity mismatch")

    @staticmethod
    def _quick_check(connection: sqlite3.Connection) -> None:
        if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
            raise StorageError("Peer storage integrity check failed")

    @staticmethod
    def _insert_peer(
        connection: sqlite3.Connection,
        public_key: str,
        address: str,
        request_key: str | None,
        fingerprint: str | None,
    ) -> tuple[PeerRecord, OperationRecord]:
        now = time.time()
        peer = PeerRecord(str(uuid4()), public_key, address, "pending", now)
        operation = OperationRecord(
            str(uuid4()),
            peer.id,
            "create",
            "pending",
            public_key,
            address,
            None,
            now,
            request_key,
            fingerprint,
        )
        connection.execute(
            "INSERT INTO peers VALUES (?, ?, ?, ?, ?)",
            (peer.id, public_key, address, peer.state, now),
        )
        connection.execute(
            "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation.id,
                peer.id,
                operation.kind,
                operation.status,
                public_key,
                address,
                None,
                now,
                request_key,
                fingerprint,
            ),
        )
        return peer, operation

    def list_peers(
        self, limit: int | None = None, after: str | None = None
    ) -> list[PeerRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM peers WHERE (? IS NULL OR id > ?) ORDER BY id LIMIT ?",
                (after, after, -1 if limit is None else max(0, limit)),
            )
            return [PeerRecord(**dict(row)) for row in rows]

    def get_peer(self, id: str) -> PeerRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM peers WHERE id = ?", (id,)
            ).fetchone()
            return None if row is None else PeerRecord(**dict(row))

    def get_operation(self, id: str) -> OperationRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (id,)
            ).fetchone()
            return None if row is None else OperationRecord(**dict(row))

    def pending_operations(self) -> list[OperationRecord]:
        with self._connection() as connection:
            return [
                OperationRecord(**dict(row))
                for row in connection.execute(
                    "SELECT * FROM operations WHERE status = 'pending' "
                    "ORDER BY created_at, id"
                )
            ]

    def find_request(self, key: str) -> OperationRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE request_key = ?", (key,)
            ).fetchone()
            return None if row is None else OperationRecord(**dict(row))

    def latest_operation(self, peer_id: str) -> OperationRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE peer_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (peer_id,),
            ).fetchone()
            return None if row is None else OperationRecord(**dict(row))

    def create_peer(
        self,
        public_key: str,
        address: str,
        request_key: str | None = None,
        fingerprint: str | None = None,
    ) -> tuple[PeerRecord, OperationRecord]:
        validate_key(public_key)
        address = client_address(address, self._server)
        try:
            with self._connection(write=True) as connection:
                return self._insert_peer(
                    connection, public_key, address, request_key, fingerprint
                )
        except StorageError as exc:
            if isinstance(exc.__cause__, sqlite3.IntegrityError):
                raise ConflictError(
                    "Peer key, address or request key already reserved"
                ) from exc
            raise

    def request_delete(self, peer_id: str) -> OperationRecord:
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM peers WHERE id = ?", (peer_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("Peer not found")
            peer = PeerRecord(**dict(row))
            if peer.state == "deleting":
                row = connection.execute(
                    "SELECT * FROM operations WHERE peer_id = ? "
                    "AND kind = 'delete' AND status = 'pending'",
                    (peer_id,),
                ).fetchone()
                if row is None:
                    raise StorageError("Deleting peer has no pending operation")
                return OperationRecord(**dict(row))
            connection.execute(
                "UPDATE operations SET status = 'complete' "
                "WHERE peer_id = ? AND status = 'pending'",
                (peer_id,),
            )
            operation = OperationRecord(
                str(uuid4()),
                peer_id,
                "delete",
                "pending",
                peer.public_key,
                peer.address,
                None,
                time.time(),
            )
            connection.execute(
                "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    operation.id,
                    peer_id,
                    operation.kind,
                    operation.status,
                    peer.public_key,
                    peer.address,
                    None,
                    operation.created_at,
                ),
            )
            connection.execute(
                "UPDATE peers SET state = 'deleting' WHERE id = ?", (peer_id,)
            )
            return operation

    def complete_operation(self, id: str) -> None:
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("Operation not found")
            operation = OperationRecord(**dict(row))
            if operation.status == "complete":
                return
            if operation.kind == "create":
                connection.execute(
                    "UPDATE peers SET state = 'active' "
                    "WHERE id = ? AND state = 'pending'",
                    (operation.peer_id,),
                )
            else:
                connection.execute(
                    "DELETE FROM peers WHERE id = ? AND state = 'deleting'",
                    (operation.peer_id,),
                )
            connection.execute(
                "UPDATE operations SET status = 'complete', error = NULL WHERE id = ?",
                (id,),
            )

    def fail_operation(self, id: str, message: str) -> None:
        with self._connection(write=True) as connection:
            if (
                connection.execute(
                    "SELECT id FROM operations WHERE id = ?", (id,)
                ).fetchone()
                is None
            ):
                raise NotFoundError("Operation not found")
            connection.execute(
                "UPDATE operations SET error = ? WHERE id = ? AND status = 'pending'",
                (message, id),
            )

    def health_check(self) -> None:
        with self._connection() as connection:
            self._check_identity(connection)
            self._quick_check(connection)
            connection.execute("SELECT count(*) FROM peers").fetchone()

    def pending_count(self) -> int:
        with self._connection() as connection:
            return int(
                connection.execute(
                    "SELECT count(*) FROM operations WHERE status = 'pending'"
                ).fetchone()[0]
            )
