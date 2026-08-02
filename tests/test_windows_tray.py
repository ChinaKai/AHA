from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import struct
import tempfile
import unittest
from unittest import mock

from aha_cli import cli
from aha_cli.cli import main
from aha_cli.services import windows_tray


class _RegistryKey:
    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False


class _Registry:
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.value = ""

    def OpenKey(self, *_args):
        if not self.value:
            raise FileNotFoundError
        return _RegistryKey()

    def CreateKeyEx(self, *_args):
        return _RegistryKey()

    def QueryValueEx(self, _key, _name):
        return self.value, self.REG_SZ

    def SetValueEx(self, _key, _name, _reserved, _kind, value):
        self.value = value

    def DeleteValue(self, _key, _name):
        if not self.value:
            raise FileNotFoundError
        self.value = ""


class WindowsTrayTests(unittest.TestCase):
    def test_tray_settings_round_trip_keeps_token_out_of_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "local" / "tray.json"
            settings = windows_tray.TraySettings(root / "aha-home", "0.0.0.0", 18788, "secret-token")

            windows_tray.save_tray_settings(settings, config_path)
            loaded = windows_tray.load_tray_settings(config_path)
            config_text = config_path.read_text(encoding="utf-8")

            self.assertEqual(loaded, settings.normalized())
            self.assertNotIn("secret-token", config_text)
            self.assertEqual(json.loads(config_text)["web_token_file"], str(root / "aha-home" / "web-token"))
            self.assertEqual((root / "aha-home" / "web-token").read_text(encoding="utf-8"), "secret-token")

    def test_tray_settings_validate_home_bind_and_port(self) -> None:
        with self.assertRaisesRegex(windows_tray.WindowsTrayError, "AHA_HOME"):
            windows_tray.TraySettings("", "127.0.0.1", 8766).normalized()
        with self.assertRaisesRegex(windows_tray.WindowsTrayError, "Bind"):
            windows_tray.TraySettings(Path("home"), "", 8766).normalized()
        with self.assertRaisesRegex(windows_tray.WindowsTrayError, "1 到 65535"):
            windows_tray.TraySettings(Path("home"), "127.0.0.1", 70000).normalized()

    def test_packaged_windows_icon_has_expected_multisize_entries(self) -> None:
        icon_path = Path(__file__).resolve().parents[1] / "src" / "aha_cli" / "assets" / "aha.ico"
        payload = icon_path.read_bytes()
        reserved, kind, count = struct.unpack_from("<HHH", payload)
        sizes = []
        for index in range(count):
            width, height = struct.unpack_from("<BB", payload, 6 + index * 16)
            sizes.append((256 if width == 0 else width, 256 if height == 0 else height))
        self.assertEqual((reserved, kind), (0, 1))
        self.assertEqual(sizes, [(size, size) for size in (16, 20, 24, 32, 40, 48, 64, 128, 256)])

    def test_materialize_tray_icon_copies_packaged_logo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "AHA" / "tray.json"
            icon_path = windows_tray.materialize_tray_icon(config_path)
            self.assertEqual(icon_path, config_path.with_name("aha.ico"))
            self.assertTrue(icon_path.read_bytes().startswith(b"\x00\x00\x01\x00"))

    def test_pythonw_executable_uses_sibling_without_console(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / "python.exe"
            pythonw = root / "pythonw.exe"
            python.write_bytes(b"")
            pythonw.write_bytes(b"")
            self.assertEqual(windows_tray.pythonw_executable(python), str(pythonw))

    def test_tray_invocation_for_zipapp_preserves_runtime_configuration(self) -> None:
        with mock.patch.object(windows_tray, "running_zipapp_path", return_value=Path(r"C:\Program Files\AHA\aha")), mock.patch.object(
            windows_tray, "pythonw_executable", return_value=r"C:\Python\pythonw.exe"
        ):
            command = windows_tray.tray_invocation(
                Path(r"C:\Users\me\.aha"),
                "run-001",
                "127.0.0.1",
                8788,
                1000,
                auth_token_file=r"C:\Users\me\.aha\web-token",
            )

        self.assertEqual(command[:2], [r"C:\Python\pythonw.exe", r"C:\Program Files\AHA\aha"])
        self.assertEqual(command[2:6], ["--home", r"C:\Users\me\.aha", "tray", "run-001"])
        self.assertIn("--auth-token-file", command)
        self.assertNotIn("--open-browser", command)

    def test_web_ui_command_uses_current_aha_invocation(self) -> None:
        with mock.patch.object(windows_tray, "aha_cli_invocation", return_value=["python", "aha"]):
            command = windows_tray.web_ui_command(Path("home"), "", "127.0.0.1", 8766, 500)
        self.assertEqual(
            command,
            ["python", "aha", "--home", "home", "ui", "--host", "127.0.0.1", "--port", "8766", "--poll-interval", "500"],
        )

    def test_startup_registry_round_trip_and_exact_command_check(self) -> None:
        registry = _Registry()
        with mock.patch.object(windows_tray, "_winreg", return_value=registry):
            self.assertFalse(windows_tray.startup_enabled())
            windows_tray.set_startup_enabled(True, '"pythonw.exe" aha tray')
            self.assertTrue(windows_tray.startup_enabled())
            self.assertTrue(windows_tray.startup_enabled('"PYTHONW.EXE" AHA TRAY'))
            self.assertFalse(windows_tray.startup_enabled('"pythonw.exe" other tray'))
            windows_tray.set_startup_enabled(False, "ignored")
            self.assertFalse(windows_tray.startup_enabled())

    def test_dashboard_url_reads_token_file_and_normalizes_wildcard_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("a token\n", encoding="utf-8")
            self.assertEqual(
                windows_tray.dashboard_url("0.0.0.0", 8788, auth_token_file=str(token_file)),
                "http://127.0.0.1:8788/?token=a%20token",
            )

    def test_web_process_restart_stops_old_process_before_starting_new_one(self) -> None:
        first = mock.Mock()
        first.poll.return_value = None
        second = mock.Mock()
        second.poll.return_value = None
        with mock.patch.object(subprocess, "Popen", side_effect=[first, second]) as popen:
            process = windows_tray.WebUiProcess(["python", "aha", "ui"])
            process.start()
            process.restart()
            process.stop()
        self.assertEqual(popen.call_count, 2)
        first.terminate.assert_called_once_with()
        second.terminate.assert_called_once_with()

    def test_duplicate_tray_opens_existing_dashboard_without_second_web_process(self) -> None:
        mutex = mock.Mock()
        mutex.acquire.return_value = False
        web_process = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(windows_tray.platform, "WIN", True), mock.patch.object(
            windows_tray, "WindowsTrayMutex", return_value=mutex
        ), mock.patch.object(windows_tray, "WebUiProcess", return_value=web_process), mock.patch.object(
            windows_tray.webbrowser, "open"
        ) as open_browser:
            root = Path(tmp)
            windows_tray.run_windows_tray(
                root / "home",
                "",
                "127.0.0.1",
                8788,
                1000,
                config_path=root / "local" / "tray.json",
            )
        mutex.close.assert_called_once_with()
        web_process.start.assert_not_called()
        open_browser.assert_called_once_with("http://127.0.0.1:8788/")

    def test_apply_settings_restarts_web_and_updates_enabled_startup(self) -> None:
        web_process = mock.Mock()
        web_process.process = None
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            windows_tray, "WebUiProcess", return_value=web_process
        ), mock.patch.object(windows_tray, "startup_enabled", return_value=True), mock.patch.object(
            windows_tray, "set_startup_enabled"
        ) as set_startup, mock.patch.object(windows_tray, "wait_for_dashboard", return_value=True):
            root = Path(tmp)
            runtime = windows_tray.TrayRuntime(
                windows_tray.TraySettings(root / "old-home", "127.0.0.1", 8766, "old-token"),
                "run-001",
                1000,
                config_path=root / "local" / "tray.json",
            )
            runtime.apply_settings(windows_tray.TraySettings(root / "new-home", "0.0.0.0", 18788, "new-token"))

        self.assertEqual(runtime.settings.bind, "0.0.0.0")
        self.assertEqual(runtime.settings.port, 18788)
        self.assertEqual(runtime.run_id, "")
        web_process.stop.assert_called_once_with()
        web_process.start.assert_called_once_with()
        set_startup.assert_called_once_with(True, runtime.startup_command())

    def test_cli_tray_reports_non_windows_platform(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stderr", stderr), mock.patch.object(
            windows_tray.platform, "WIN", False
        ):
            code = main(["--home", tmp, "tray"])
        self.assertEqual(code, 2)
        self.assertIn("Windows only", stderr.getvalue())

    def test_cli_tray_uses_saved_settings_when_arguments_are_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "local" / "tray.json"
            saved = windows_tray.TraySettings(root / "aha-home", "0.0.0.0", 18788, "saved-token")
            windows_tray.save_tray_settings(saved, config_path)
            with mock.patch.object(cli, "default_tray_config_path", return_value=config_path), mock.patch.object(
                cli, "run_windows_tray"
            ) as run_tray, mock.patch.dict(
                os.environ,
                {"AHA_HOME": "", "AHA_WEB_TOKEN": "", "AHA_WEB_TOKEN_FILE": ""},
            ):
                code = main(["tray"])

        self.assertEqual(code, 0)
        self.assertEqual(run_tray.call_args.args[:5], (saved.aha_home.resolve(), "", "0.0.0.0", 18788, 1000))
        self.assertEqual(run_tray.call_args.kwargs["auth_token"], "saved-token")
        self.assertEqual(run_tray.call_args.kwargs["config_path"], config_path)

    def test_release_workflow_and_installer_include_windows_tray(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        installer = (repo / "scripts" / "install_windows.ps1").read_text(encoding="utf-8")
        workflow = (repo / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn('"tray"', installer)
        self.assertIn('"--enable-startup"', installer)
        self.assertIn("[ValidateNotNullOrEmpty()][string]$Bind", installer)
        self.assertIn('"--host"', installer)
        self.assertIn("-AllowUnsafeBind", installer)
        self.assertIn("[switch]$NoShortcut", installer)
        self.assertIn("Install-AhaStartMenuShortcut", installer)
        self.assertIn("[Environment+SpecialFolder]::Programs", installer)
        self.assertIn('Join-Path $shortcutDirectory "AHA.lnk"', installer)
        self.assertIn('" tray --open-browser"', installer)
        self.assertIn("System.Text.UTF8Encoding($false)", installer)
        self.assertIn("pythonw.exe", installer)
        self.assertIn("install_windows.ps1", workflow)


if __name__ == "__main__":
    unittest.main()
