"""Process-lifetime lock for one active production synchronization context."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from types import TracebackType


class RunLockError(Exception):
    """Raised when another production synchronization owns the context lock."""


class ProductionRunLock:
    """Hold a kernel lock; an unlocked stale file is deliberately harmless."""

    def __init__(self, state_directory: Path) -> None:
        self._directory = state_directory
        self._path = state_directory / ".nexus-jar-sync.lock"
        self._stream: object | None = None

    def __enter__(self) -> ProductionRunLock:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            stream = self._path.open("a+b")
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            _try_lock(stream)
            self._stream = stream
            return self
        except BlockingIOError:
            try:
                stream.close()
            except (OSError, UnboundLocalError):
                pass
            raise RunLockError("Another synchronization is already running for this state context") from None
        except OSError as error:
            try:
                stream.close()
            except (OSError, UnboundLocalError):
                pass
            if error.errno in {errno.EACCES, errno.EAGAIN} or getattr(error, "winerror", None) in {32, 33, 36}:
                raise RunLockError("Another synchronization is already running for this state context") from None
            raise RunLockError("Could not acquire the synchronization lock") from None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            try:
                _unlock(self._stream)
            finally:
                self._stream.close()  # type: ignore[union-attr]
                self._stream = None


def _try_lock(stream: object) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]


def _unlock(stream: object) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)  # type: ignore[attr-defined]
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
