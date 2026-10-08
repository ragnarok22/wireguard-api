"""Bounded, reentrant exclusion for synchronous control-plane mutations."""

import fcntl
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from errors import StorageError


class StorageLock:
    def __init__(self, path: Path, timeout: float) -> None:
        if not 0 <= timeout < float("inf"):
            raise StorageError("Invalid storage lock timeout")
        self.path = path
        self.timeout = timeout
        self._thread_lock = threading.RLock()
        self._depth = 0

    @contextmanager
    def acquire(self) -> Iterator[None]:
        """Keep the flock inode stable; closing the descriptor releases the lock."""
        deadline = time.monotonic() + self.timeout
        if not self._thread_lock.acquire(timeout=self.timeout):
            raise StorageError("Storage lock timed out")
        try:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "r+b") as lock_file:
                    os.fchmod(lock_file.fileno(), 0o600)
                    while True:
                        try:
                            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise StorageError("Storage lock timed out") from None
                            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
                    self._depth = 1
                    try:
                        yield
                    finally:
                        self._depth = 0
            except OSError as exc:
                raise StorageError("Storage lock unavailable") from exc
        finally:
            self._thread_lock.release()
