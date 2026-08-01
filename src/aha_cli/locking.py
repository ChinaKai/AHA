"""Cross-platform advisory file locking.

Centralizes the advisory-lock primitives the store and managed-runtime modules
use to coordinate exclusive access to JSONL append targets, plan files, backend
locks, and bridge/profile lock files. Replaces scattered ``fcntl.flock`` calls so
the package no longer hard-depends on the POSIX-only ``fcntl`` module for locking.

POSIX uses ``fcntl.flock`` (whole-file BSD advisory lock). Windows uses
``msvcrt.locking`` over byte 0 of the file, which is a sufficient cross-process
mutex for the low-contention, local single-user workloads AHA targets.

Non-blocking acquisition that finds the lock held raises ``BlockingIOError`` on
both platforms, so callers can express a try-lock with one ``except
BlockingIOError`` regardless of host OS.

Note: ``msvcrt.locking`` has no indefinite blocking mode, so the Windows blocking
path polls ``LK_NBLCK`` with a short sleep. This is functionally equivalent to
POSIX ``flock`` blocking for AHA's brief critical sections.
"""
from __future__ import annotations

import errno
import os
import sys
import time
from contextlib import contextmanager
from typing import IO, Iterator, Union

_WIN = sys.platform == "win32"

FileDescriptorLike = Union[int, IO]


def _fd(handle: FileDescriptorLike) -> int:
    return handle.fileno() if hasattr(handle, "fileno") else int(handle)


def acquire(handle: FileDescriptorLike, *, exclusive: bool = True, blocking: bool = True) -> bool:
    """Acquire an advisory lock on an open file descriptor or file object.

    With ``blocking=False`` the call raises :class:`BlockingIOError` when the
    lock is already held (matching POSIX ``flock`` semantics), so a try-lock is a
    single ``try``/``except BlockingIOError``. Returns ``True`` once acquired.
    """
    fd = _fd(handle)
    if _WIN:
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        if blocking:
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return True
                except OSError:
                    time.sleep(0.05)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(exc.errno or errno.EAGAIN, str(exc)) from exc
        return True
    import fcntl

    flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        flag |= fcntl.LOCK_NB
    fcntl.flock(fd, flag)
    return True


def release(handle: FileDescriptorLike) -> None:
    """Release an advisory lock previously acquired with :func:`acquire`."""
    fd = _fd(handle)
    if _WIN:
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def exclusive_lock(handle: FileDescriptorLike) -> Iterator[None]:
    """Block until an exclusive lock is acquired, then release on exit."""
    acquire(handle, exclusive=True, blocking=True)
    try:
        yield
    finally:
        release(handle)
