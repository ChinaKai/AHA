from __future__ import annotations

import os
import signal
import subprocess
import sys
import unittest
from unittest import mock

from aha_cli import process_control


class ProcessControlTests(unittest.TestCase):
    """Cross-platform process-control contract.

    Exercises the POSIX path on Linux/macOS. The Windows (ctypes) branch is
    structurally parallel and is verified on a Windows runner.
    """

    def test_process_exists_self_and_dead(self) -> None:
        self.assertTrue(process_control.process_exists(os.getpid()))
        self.assertFalse(process_control.process_exists(999999999))
        self.assertFalse(process_control.process_exists(None))
        self.assertFalse(process_control.process_exists(0))
        self.assertFalse(process_control.process_exists("not-a-pid"))

    def test_current_uid_matches_os_on_posix(self) -> None:
        self.assertEqual(process_control.current_uid(), os.geteuid())

    def test_process_group_id_matches_os_on_posix(self) -> None:
        self.assertEqual(
            process_control.process_group_id(os.getpid()), os.getpgid(os.getpid())
        )

    def test_send_signal_dead_pid_raises_process_lookup(self) -> None:
        with self.assertRaises(ProcessLookupError):
            process_control.send_signal(999999999, signal.SIGTERM)

    def test_terminate_process_stops_child(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.assertTrue(process_control.process_exists(proc.pid))
            exited = process_control.terminate_process(proc.pid, timeout=5.0)
            self.assertTrue(exited)
            self.assertFalse(process_control.process_exists(proc.pid))
        finally:
            if process_control.process_exists(proc.pid):
                proc.kill()
            proc.wait(timeout=5)

    def test_parent_death_preexec_contract(self) -> None:
        fn = process_control.parent_death_preexec()
        if sys.platform == "win32":
            self.assertIsNone(fn)
        else:
            self.assertTrue(callable(fn))

    def test_assign_parent_death_does_not_raise(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            process_control.assign_parent_death(proc)  # POSIX no-op; Windows job assign
        finally:
            if process_control.process_exists(proc.pid):
                proc.kill()
            proc.wait(timeout=5)

    def test_terminate_parent_death_children_routes_to_windows_job(self) -> None:
        with mock.patch.object(process_control, "_WIN", True), mock.patch.object(
            process_control,
            "_windows_terminate_kill_job",
        ) as terminate_job:
            process_control.terminate_parent_death_children()

        terminate_job.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
