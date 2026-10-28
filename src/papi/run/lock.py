"""One ``papi run`` per home: ``flock`` on ``$PAPI_HOME/locks/run.lock`` (the kernel releases it on exit)."""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
from typing import NamedTuple

from papi.core.config import Settings, get_settings


class AlreadyRunning(RuntimeError):
    def __init__(self, path: Path, pid: int | None) -> None:
        self.path, self.pid = path, pid
        who = f"pid {pid}" if pid else "başka bir süreç"
        super().__init__(f"papi run zaten çalışıyor ({who}; kilit {path})")


def lock_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).paths.home / "locks" / "run.lock"


def _read_pid(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


class InstanceLock:
    """Exclusive, non-blocking; ``acquire()`` raises ``AlreadyRunning``. The holder's pid is in the file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or lock_path()
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> InstanceLock:
        if self._fd is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunning(self.path, _read_pid(self.path)) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


class LockStatus(NamedTuple):
    held: bool
    pid: int | None


def holder(path: Path | None = None) -> LockStatus:
    """Is the lock held by a live process (possibly this one, through another descriptor)?"""
    path = path or lock_path()
    if not path.exists():
        return LockStatus(False, None)
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return LockStatus(True, _read_pid(path))
        fcntl.flock(fd, fcntl.LOCK_UN)
        return LockStatus(False, None)
    finally:
        os.close(fd)
