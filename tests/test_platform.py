from __future__ import annotations

import sys
import tempfile
import unittest
import os
from pathlib import Path

from aha_cli import platform


class PlatformTests(unittest.TestCase):
    def test_is_windows_matches_sys_platform(self) -> None:
        self.assertEqual(platform.is_windows(), sys.platform == "win32")

    def test_temp_root_is_real_temp_dir(self) -> None:
        self.assertEqual(platform.temp_root(), Path(tempfile.gettempdir()))

    def test_candidate_temp_roots_non_empty_and_contains_temp_dir(self) -> None:
        roots = platform.candidate_temp_roots()
        self.assertTrue(roots)
        self.assertIn(Path(tempfile.gettempdir()), roots)
        # POSIX adds the conventional /tmp; Windows does not.
        if sys.platform != "win32":
            self.assertIn(Path("/tmp"), roots)

    def test_default_shell_non_empty(self) -> None:
        self.assertTrue(platform.default_shell())

    def test_expand_path_passthrough_and_tokens(self) -> None:
        self.assertEqual(platform.expand_path(""), "")
        self.assertEqual(platform.expand_path("codex"), "codex")  # bare name passthrough
        os.environ["AHA_TEST_VAR"] = "/opt/x"
        try:
            self.assertEqual(platform.expand_path("$AHA_TEST_VAR/proj"), "/opt/x/proj")
            self.assertEqual(platform.expand_path("${AHA_TEST_VAR}"), "/opt/x")
        finally:
            del os.environ["AHA_TEST_VAR"]
        # ~ expands to the home directory.
        self.assertTrue(platform.expand_path("~").endswith("") and platform.expand_path("~"))

    def test_spawn_command_posix_passthrough(self) -> None:
        self.assertEqual(platform.spawn_command(["claude", "-p"]), ["claude", "-p"])

    def test_spawn_command_windows_cmd_shim(self) -> None:
        import unittest.mock as mock
        with mock.patch.object(platform, "WIN", True), mock.patch("shutil.which", return_value=r"C:\npm\claude.cmd"):
            result = platform.spawn_command(["claude", "-p", "--verbose"])
        self.assertEqual(result, ["cmd.exe", "/c", r"C:\npm\claude.cmd", "-p", "--verbose"])

    def test_spawn_command_windows_exe_resolves(self) -> None:
        import unittest.mock as mock
        with mock.patch.object(platform, "WIN", True), mock.patch("shutil.which", return_value=r"C:\tools\codex.exe"):
            result = platform.spawn_command(["codex", "exec"])
        self.assertEqual(result, [r"C:\tools\codex.exe", "exec"])

    def test_spawn_command_windows_not_found_passthrough(self) -> None:
        import unittest.mock as mock
        with mock.patch.object(platform, "WIN", True), mock.patch("shutil.which", return_value=None):
            result = platform.spawn_command(["ghost", "-x"])
        self.assertEqual(result, ["ghost", "-x"])


if __name__ == "__main__":
    unittest.main()
