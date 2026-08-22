from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.service_install import (
    ServiceInstallError,
    UserServiceSpec,
    install_user_service,
    prepare_user_service,
    render_packaged_systemd_user_unit,
    render_systemd_user_unit,
    uninstall_user_service,
)


class ServiceInstallTests(unittest.TestCase):
    def test_prepare_user_service_creates_secure_token_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-prepare-") as tmp:
            home = Path(tmp) / ".aha"
            first = prepare_user_service(home)
            token = Path(first["auth_token_file"])
            original = token.read_text(encoding="utf-8")
            second = prepare_user_service(home)

            self.assertTrue(first["token_created"])
            self.assertFalse(second["token_created"])
            self.assertEqual(token.read_text(encoding="utf-8"), original)
            self.assertEqual(token.stat().st_mode & 0o777, 0o600)

    def test_render_systemd_user_unit_includes_auth_and_package_manager(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-render-") as tmp:
            root = Path(tmp)
            unit = render_systemd_user_unit(
                UserServiceSpec(
                    bin_path=root / "bin path" / "aha",
                    aha_home=root / "aha home",
                    service_name="aha-test",
                    bind="127.0.0.1",
                    port=18788,
                    run_id="run-123",
                    package_manager="deb",
                )
            )

        self.assertIn("Environment=\"AHA_PACKAGE_MANAGER=deb\"", unit)
        self.assertIn("Environment=\"AHA_SERVICE_NAME=aha-test.service\"", unit)
        self.assertIn("--auth-token-file", unit)
        self.assertIn('"run-123"', unit)
        self.assertIn("--port 18788", unit)

    def test_unauthenticated_network_bind_requires_explicit_override(self) -> None:
        with self.assertRaisesRegex(ServiceInstallError, "allow-unsafe-bind"):
            UserServiceSpec(
                bin_path=Path("/tmp/aha"),
                aha_home=Path("/tmp/home"),
                bind="0.0.0.0",
                auth_required=False,
            ).normalized()

    def test_install_user_service_dry_run_does_not_write_or_run_systemctl(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-dry-run-") as tmp:
            root = Path(tmp)
            unit_path = root / "config" / "aha.service"
            runner = mock.Mock()
            result = install_user_service(
                UserServiceSpec(bin_path=root / "missing-aha", aha_home=root / "home"),
                dry_run=True,
                unit_path=unit_path,
                command_runner=runner,
            )

            self.assertTrue(result["dry_run"])
            self.assertFalse(unit_path.exists())
            self.assertFalse((root / "home").exists())
            runner.assert_not_called()

    def test_install_and_uninstall_user_service_use_systemctl(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-install-") as tmp:
            root = Path(tmp)
            binary = root / "bin" / "aha"
            binary.parent.mkdir()
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            unit_path = root / "config" / "aha.service"
            commands: list[list[str]] = []

            def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
                commands.append(argv)
                return subprocess.CompletedProcess(argv, 0, "", "")

            installed = install_user_service(
                UserServiceSpec(bin_path=binary, aha_home=root / "home", service_name="aha-test"),
                unit_path=unit_path,
                command_runner=runner,
            )
            removed = uninstall_user_service(
                "aha-test",
                unit_path=unit_path,
                command_runner=runner,
            )

            self.assertTrue(installed["enabled"])
            self.assertTrue(installed["started"])
            self.assertTrue(removed["removed"])
            self.assertFalse(unit_path.exists())
            self.assertIn(["systemctl", "--user", "daemon-reload"], commands)
            self.assertIn(["systemctl", "--user", "enable", "aha-test.service"], commands)
            self.assertIn(["systemctl", "--user", "restart", "aha-test.service"], commands)
            self.assertIn(["systemctl", "--user", "disable", "--now", "aha-test.service"], commands)

    def test_packaged_unit_prepares_token_and_disables_onebin_upgrade(self) -> None:
        unit = render_packaged_systemd_user_unit()

        self.assertIn("ExecStartPre=\"/usr/bin/aha\" --home %h/.aha service prepare-user", unit)
        self.assertIn("ExecStart=\"/usr/bin/aha\" --home %h/.aha ui", unit)
        self.assertIn('Environment="AHA_PACKAGE_MANAGER=deb"', unit)
        self.assertIn('Environment="AHA_INSTALL_BIN=/usr/lib/aha/aha"', unit)

    def test_service_prepare_user_cli_outputs_json(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-cli-") as tmp:
            home = Path(tmp) / ".aha"
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = main(
                    [
                        "service",
                        "prepare-user",
                        "--aha-home",
                        str(home),
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertTrue(Path(payload["auth_token_file"]).is_file())

    def test_service_install_user_cli_dry_run_outputs_unit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-cli-unit-") as tmp:
            root = Path(tmp)
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = main(
                    [
                        "service",
                        "install-user",
                        "--bin",
                        str(root / "missing"),
                        "--aha-home",
                        str(root / "home"),
                        "--dry-run",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["dry_run"])
            self.assertIn("ExecStart=", payload["unit"])


if __name__ == "__main__":
    unittest.main()
