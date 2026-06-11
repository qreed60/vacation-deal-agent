"""Phase 5B: file-based lock manager for scheduled searches.

Provides global and per-vacation locks that work inside Docker with a
mounted data volume.  Stale locks are cleaned up based on TTL.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from typing import Optional


class LockError(Exception):
    """Raised when a lock cannot be acquired."""


# In-process lock tracking (fcntl is per-process, so we need this for same-process tests)
_lock_registry: dict[str, int] = {}  # name -> pid of holder


def _lock_dir() -> Path:
    """Return the configured lock directory, creating it if needed."""
    path_str = os.environ.get("SCHEDULER_LOCK_DIR", "data/locks")
    p = Path(path_str)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _lock_ttl() -> int:
    """Return lock TTL in seconds (default 7200 = 2 hours)."""
    try:
        return int(os.environ.get("SCHEDULER_LOCK_TTL_SECONDS", "7200"))
    except (TypeError, ValueError):
        return 7200


class FileLock:
    """Context-manager lock backed by fcntl.flock with stale-lock TTL."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._path = _lock_dir() / f"{name}.lock"
        self._fd = open(self._path, "w")  # noqa: SIM115 — keep handle alive
        self._locked = False

    def acquire(self) -> None:
        """Acquire the lock. Raises LockError if stale-lock TTL expired."""
        ttl = _lock_ttl()
        now = time.time()
        pid = os.getpid()

        # Check in-process registry first (fcntl is per-process on Linux)
        holder_pid = _lock_registry.get(self._name)
        if holder_pid is not None and holder_pid != pid:
            raise LockError(f"Lock {self._name} already held by PID {holder_pid}")

        # Check for stale lock file
        if self._path.exists():
            mtime = os.path.getmtime(str(self._path))
            if (now - mtime) > ttl:
                # Stale — remove and proceed to acquire
                self._path.unlink(missing_ok=True)

        try:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd.write(str(pid))
            self._fd.flush()
            _lock_registry[self._name] = pid
            self._locked = True
        except (IOError, OSError):
            raise LockError(f"Could not acquire lock: {self._name}")

    def release(self) -> None:
        """Release the lock."""
        if self._locked:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            except (IOError, OSError):
                pass
            _lock_registry.pop(self._name, None)
            self._fd.close()
            self._path.unlink(missing_ok=True)
            self._locked = False

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[override]
        self.release()


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def acquire_global_lock() -> FileLock:
    """Acquire the global scheduler lock."""
    return FileLock("global")


def acquire_vacation_lock(vacation_id: int) -> FileLock:
    """Acquire a per-vacation lock."""
    return FileLock(f"vacation_{vacation_id}")
