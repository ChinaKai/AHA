from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from aha_cli.services.hardware_session import FdTransport
from aha_cli.services.serial_lock import (
    SerialDeviceBusyError,
    acquire_serial_lock,
    serial_lock_owner,
    serial_lock_path,
    takeover_serial_device,
)


class SerialLockTests(unittest.TestCase):
    def test_acquire_blocks_second_owner_and_release_only_removes_own_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp)
            device = "/dev/ttyUSB-test"
            lock = acquire_serial_lock(device, lock_directory=lock_dir)
            self.assertIsNotNone(lock)
            owner = serial_lock_owner(device, lock_directory=lock_dir)
            self.assertEqual(owner["pid"], os.getpid())
            self.assertTrue(owner["alive"])
            self.assertEqual(owner["uid"], os.geteuid())
            self.assertTrue(owner["can_terminate"])

            with self.assertRaises(SerialDeviceBusyError) as raised:
                acquire_serial_lock(device, pid=os.getpid() + 1, lock_directory=lock_dir)
            self.assertEqual(raised.exception.owner["pid"], os.getpid())

            path = serial_lock_path(device, lock_directory=lock_dir)
            path.unlink()
            path.write_text("2000000000\n", encoding="ascii")
            lock.release()
            self.assertTrue(path.exists())

    def test_stale_lock_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp)
            device = "/dev/ttyUSB-stale"
            path = serial_lock_path(device, lock_directory=lock_dir)
            path.write_text("2000000000\n", encoding="ascii")

            lock = acquire_serial_lock(device, lock_directory=lock_dir)
            try:
                self.assertEqual(serial_lock_owner(device, lock_directory=lock_dir)["pid"], os.getpid())
            finally:
                lock.release()
            self.assertFalse(path.exists())

    def test_takeover_sends_sigterm_and_removes_departed_owner_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp)
            device = "/dev/ttyUSB-takeover"
            process = subprocess.Popen(["sleep", "30"])
            path = serial_lock_path(device, lock_directory=lock_dir)
            path.write_text(f"{process.pid}\n", encoding="ascii")
            try:
                result = takeover_serial_device(device, lock_directory=lock_dir)
                process.wait(timeout=2)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=2)
            self.assertTrue(result["released"])
            self.assertEqual(result["owner"]["pid"], process.pid)
            self.assertFalse(path.exists())

    def test_foreign_uid_owner_cannot_be_taken_over(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp)
            device = "/dev/ttyUSB-foreign"
            process = subprocess.Popen(["sleep", "30"])
            path = serial_lock_path(device, lock_directory=lock_dir)
            path.write_text(f"{process.pid}\n", encoding="ascii")
            try:
                with (
                    mock.patch("aha_cli.services.serial_lock.os.geteuid", return_value=1000),
                    mock.patch("aha_cli.services.serial_lock._process_uid", return_value=0),
                    self.assertRaises(PermissionError) as raised,
                ):
                    takeover_serial_device(device, lock_directory=lock_dir)
                self.assertIn("AHA runs as UID 1000", str(raised.exception))
                self.assertIn("owned by UID 0", str(raised.exception))
                self.assertIn("close it manually", str(raised.exception))
                self.assertIsNone(process.poll())
            finally:
                process.terminate()
                process.wait(timeout=2)

    def test_pty_does_not_use_global_uucp_lock(self) -> None:
        self.assertIsNone(serial_lock_path("/dev/pts/42"))

    def test_transport_close_releases_its_serial_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp)
            device = "/dev/ttyUSB-close"
            lock = acquire_serial_lock(device, lock_directory=lock_dir)
            master, slave = os.openpty()
            try:
                FdTransport(slave, serial_lock=lock).close()
                self.assertIsNone(serial_lock_owner(device, lock_directory=lock_dir))
            finally:
                os.close(master)


if __name__ == "__main__":
    unittest.main()
