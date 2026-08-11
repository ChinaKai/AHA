from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

from aha_cli.services import backend_paths


class TestBackendPaths:
    def _run_with_zipapp(self, zipapp: Path | None) -> dict[str, str]:
        env: dict[str, str] = {"PATH": "/usr/bin:/bin"}
        with mock.patch("aha_cli.services.onebin.running_zipapp_path", return_value=zipapp):
            backend_paths.add_user_backend_paths(env)
        return env

    def test_win32_onebin_creates_python3_shim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            zipapp = Path(tmp) / "aha"
            zipapp.write_bytes(b"PK\x03\x04")
            with mock.patch("aha_cli.services.backend_paths.sys.platform", "win32"):
                env = self._run_with_zipapp(zipapp)

            shim = zipapp.parent / "python3"
            assert shim.is_file()
            body = shim.read_text(encoding="utf-8")
            assert body.startswith("#!/bin/sh\n")
            assert sys.executable.replace("\\", "/") in body
            # The onebin directory is prepended so backend shells can resolve `aha`.
            assert str(zipapp.parent) in env["PATH"].split(os.pathsep)

    def test_non_win32_skips_python3_shim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            zipapp = Path(tmp) / "aha"
            zipapp.write_bytes(b"PK\x03\x04")
            with mock.patch("aha_cli.services.backend_paths.sys.platform", "linux"):
                env = self._run_with_zipapp(zipapp)

            # Shadowing a working system `python3` in a PATH-prepended dir would
            # be harmful, so Linux/macOS must not get a shim.
            assert not (zipapp.parent / "python3").exists()
            assert str(zipapp.parent) in env["PATH"].split(os.pathsep)

    def test_source_dev_skips_python3_shim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("aha_cli.services.backend_paths.sys.platform", "win32"):
                env = self._run_with_zipapp(None)

            # No onebin -> no install dir to place a shim in; only user bin dirs.
            assert all("python3" not in Path(item).name for item in env["PATH"].split(os.pathsep))

    def test_shim_rewrites_only_when_target_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            zipapp = Path(tmp) / "aha"
            zipapp.write_bytes(b"PK\x03\x04")
            with mock.patch("aha_cli.services.backend_paths.sys.platform", "win32"):
                env = self._run_with_zipapp(zipapp)
            shim = zipapp.parent / "python3"
            before = shim.stat().st_mtime

            # Same interpreter -> no rewrite.
            with mock.patch("aha_cli.services.backend_paths.sys.platform", "win32"):
                env = self._run_with_zipapp(zipapp)
            assert shim.stat().st_mtime == before

            # Different interpreter target -> rewrite.
            with (
                mock.patch("aha_cli.services.backend_paths.sys.platform", "win32"),
                mock.patch("aha_cli.services.backend_paths.sys.executable", r"C:\other\python.exe"),
            ):
                env = self._run_with_zipapp(zipapp)
            assert "C:/other/python.exe" in shim.read_text(encoding="utf-8")
