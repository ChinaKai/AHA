from __future__ import annotations

import os
import tempfile
import unittest

from aha_cli import locking


class LockingTests(unittest.TestCase):
    """Cross-platform advisory lock contract.

    These exercise the POSIX path on Linux/macOS. The Windows (msvcrt) branch is
    structurally identical and is verified on a Windows runner; it raises the
    same BlockingIOError for a contended non-blocking acquire.
    """

    def setUp(self) -> None:
        fd, path = tempfile.mkstemp(prefix="aha-lock-test-")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        self.path = path

    def _open(self) -> int:
        return os.open(self.path, os.O_RDWR)

    def test_acquire_then_release(self) -> None:
        fd = self._open()
        try:
            locking.acquire(fd)
            locking.release(fd)
        finally:
            os.close(fd)

    def test_nonblocking_raises_when_held(self) -> None:
        # Two independent open() calls create separate file descriptions, so an
        # exclusive lock on one is visible as contention to the other.
        fd1, fd2 = self._open(), self._open()
        try:
            locking.acquire(fd1)
            with self.assertRaises(BlockingIOError):
                locking.acquire(fd2, blocking=False)
            locking.release(fd1)
            # After release the second descriptor acquires without blocking.
            locking.acquire(fd2, blocking=False)
            locking.release(fd2)
        finally:
            os.close(fd1)
            os.close(fd2)

    def test_exclusive_lock_context_manager(self) -> None:
        fd1, fd2 = self._open(), self._open()
        try:
            with locking.exclusive_lock(fd1):
                with self.assertRaises(BlockingIOError):
                    locking.acquire(fd2, blocking=False)
            # Outside the context the lock is released.
            locking.acquire(fd2, blocking=False)
            locking.release(fd2)
        finally:
            os.close(fd1)
            os.close(fd2)

    def test_release_without_prior_acquire_is_safe(self) -> None:
        # Call sites release inside finally blocks; releasing an unlocked fd must
        # not raise even if acquire was never reached.
        fd = self._open()
        try:
            locking.release(fd)
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
