from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from aha_cli.services import app_version
from aha_cli.services.app_version import aha_version


class AppVersionTests(unittest.TestCase):
    def run_git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if not shutil.which("git"):
            self.skipTest("git is not available")
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)

    def tearDown(self) -> None:
        app_version._source_tree_version.cache_clear()

    def test_aha_version_uses_builtin_value(self) -> None:
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("aha_cli.services.app_version.BUILD_VERSION", "v1.2.3.20260527.057e500"),
        ):
            self.assertEqual(aha_version(), "v1.2.3.20260527.057e500")

    def test_aha_version_allows_environment_override(self) -> None:
        with (
            mock.patch.dict("os.environ", {"AHA_VERSION": "20260528.override"}, clear=True),
            mock.patch("aha_cli.services.app_version.BUILD_VERSION", "20260527.057e500"),
        ):
            self.assertEqual(aha_version(), "20260528.override")

    def test_aha_version_falls_back_to_source_tree_version(self) -> None:
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("aha_cli.services.app_version.BUILD_VERSION", ""),
            mock.patch("aha_cli.services.app_version._source_tree_version", return_value="vsource.abcdef0.clean"),
        ):
            self.assertEqual(aha_version(), "vsource.abcdef0.clean")

    def test_source_tree_version_reports_dirty_ahead_and_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            local = root / "local"
            other = root / "other"
            self.run_git(root, "init", "--bare", "--initial-branch=main", str(remote))
            self.run_git(root, "clone", str(remote), str(local))
            (local / "README.md").write_text("# AHA\n", encoding="utf-8")
            self.run_git(local, "add", "README.md")
            self.run_git(local, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "init")
            self.run_git(local, "push", "-u", "origin", "main")
            self.run_git(root, "clone", str(remote), str(other))
            (other / "remote.txt").write_text("remote\n", encoding="utf-8")
            self.run_git(other, "add", "remote.txt")
            self.run_git(other, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "remote")
            self.run_git(other, "push", "origin", "main")
            (local / "local.txt").write_text("local\n", encoding="utf-8")
            self.run_git(local, "add", "local.txt")
            self.run_git(local, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "local")
            (local / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            self.run_git(local, "fetch", "origin")
            commit = self.run_git(local, "rev-parse", "--short=7", "HEAD").stdout.strip()
            app_version._source_tree_version.cache_clear()
            with mock.patch("pathlib.Path.cwd", return_value=local):
                version = app_version._source_tree_version()

        self.assertEqual(version, f"vsource.{commit}.dirty-ahead-behind")
