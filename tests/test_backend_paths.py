from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from aha_cli.services import backend_paths, onebin


class TestBackendPaths:
    def _run_with_zipapp(self, zipapp: Path | None) -> dict[str, str]:
        env: dict[str, str] = {"PATH": "/usr/bin:/bin"}
        with mock.patch("aha_cli.services.onebin.authoritative_onebin_path", return_value=zipapp):
            backend_paths.add_user_backend_paths(env)
        return env

    def test_win32_onebin_uses_command_shims_in_a_dedicated_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            zipapp = Path(tmp) / "aha"
            zipapp.write_bytes(b"PK\x03\x04")
            with mock.patch("aha_cli.services.backend_paths.sys.platform", "win32"):
                env = self._run_with_zipapp(zipapp)

            backend_bin = zipapp.parent / "backend-bin"
            aha_shim = backend_bin / "aha.cmd"
            python_shim = backend_bin / "python3.cmd"
            assert aha_shim.is_file()
            assert python_shim.is_file()
            assert str(zipapp) in aha_shim.read_text(encoding="utf-8")
            assert sys.executable in python_shim.read_text(encoding="utf-8")
            parts = env["PATH"].split(os.pathsep)
            assert parts[0] == str(backend_bin)
            assert str(zipapp.parent) not in parts
            assert env["AHA_RUNTIME_PYTHON"]

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

            # No onebin -> no install dir to place command shims in; only user bin dirs.
            assert all("python3" not in Path(item).name for item in env["PATH"].split(os.pathsep))

    def test_wsl_forwarded_onebin_repairs_aha_path_from_source_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            onebin = root / "windows" / "aha"
            onebin.parent.mkdir()
            with zipfile.ZipFile(onebin, "w") as archive:
                archive.writestr("__main__.py", "")
            home = root / "home"
            home.mkdir()
            env = {"PATH": "/usr/bin:/bin"}

            with (
                mock.patch.dict(
                    os.environ,
                    {"AHA_WSL_AHA_BIN": str(onebin), "XDG_DATA_HOME": str(root / "data")},
                    clear=True,
                ),
                mock.patch("aha_cli.services.onebin.running_zipapp_path", return_value=None),
            ):
                backend_paths.add_user_backend_paths(env, home=home)

            bridge_bin = root / "data" / "aha" / "backend-bin"
            assert env["PATH"].split(os.pathsep)[0] == str(bridge_bin)
            assert Path(os.readlink(bridge_bin / "aha")) == onebin.resolve()

    def test_wsl_forwarded_home_path_is_not_rewritten_as_windows_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            forwarded = Path("/home/kaikai/aha-windows-owner")
            env = {"PATH": "/usr/bin:/bin"}

            with (
                mock.patch.dict(
                    os.environ,
                    {"AHA_WSL_AHA_BIN": str(forwarded), "XDG_DATA_HOME": str(root / "data")},
                    clear=True,
                ),
                mock.patch(
                    "aha_cli.services.onebin.authoritative_onebin_path",
                    return_value=forwarded,
                ),
            ):
                backend_paths.add_user_backend_paths(env, home=home)

            bridge_bin = root / "data" / "aha" / "backend-bin"
            assert Path(os.readlink(bridge_bin / "aha")) == forwarded

    def test_windows_command_shim_rewrites_only_when_target_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            zipapp = Path(tmp) / "aha"
            zipapp.write_bytes(b"PK\x03\x04")
            with mock.patch("aha_cli.services.backend_paths.sys.platform", "win32"):
                env = self._run_with_zipapp(zipapp)
            shim = zipapp.parent / "backend-bin" / "aha.cmd"
            before = shim.stat().st_mtime

            # Same interpreter -> no rewrite.
            with mock.patch("aha_cli.services.backend_paths.sys.platform", "win32"):
                env = self._run_with_zipapp(zipapp)
            assert shim.stat().st_mtime == before

            # Different interpreter target -> rewrite.
            with (
                mock.patch("aha_cli.services.backend_paths.sys.platform", "win32"),
                mock.patch("aha_cli.services.onebin.sys.executable", r"C:\other\python.exe"),
            ):
                env = self._run_with_zipapp(zipapp)
            assert r"C:\other\python.exe" in shim.read_text(encoding="utf-8")

    def test_resolve_aha_python_uses_configured_runtime_for_missing_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "python.exe"
            candidate.write_bytes(b"python")
            with (
                mock.patch.object(onebin.importlib, "import_module", side_effect=ImportError),
                mock.patch.object(onebin, "authoritative_onebin_path", return_value=None),
                mock.patch.object(onebin, "_python_supports_module", return_value=True) as supports,
                mock.patch.dict(
                    os.environ,
                    {onebin.AHA_RUNTIME_PYTHON_ENV: str(candidate)},
                    clear=False,
                ),
            ):
                resolved = onebin.resolve_aha_python("playwright")

            assert resolved == str(candidate.resolve())
            supports.assert_called_once_with(candidate.resolve(), "playwright")
