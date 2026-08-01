from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.service_upgrade import (
    check_user_service_upgrade,
    executable_version,
    release_asset_url,
    upgrade_user_service,
    version_update_available,
)


def write_fake_aha(path: Path, version: str) -> None:
    path.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                'if [ "$1" = "--version" ]; then',
                f'  echo "aha {version}"',
                "  exit 0",
                "fi",
                "exit 0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


class ServiceUpgradeTests(unittest.TestCase):
    def test_version_update_comparison_avoids_downgrades(self) -> None:
        self.assertTrue(version_update_available("v1.2.2.20260731.aaaaaaa", "v1.2.3.20260730.bbbbbbb"))
        self.assertFalse(version_update_available("v1.2.4.20260801.aaaaaaa", "v1.2.3.20260730.bbbbbbb"))
        self.assertFalse(version_update_available("v1.2.3.20260801.aaaaaaa", "v1.2.3.20260730.bbbbbbb"))
        self.assertIsNone(version_update_available("v1.2.3.20260801.aaaaaaa", "v1.2.3.20260801.bbbbbbb"))
        self.assertFalse(version_update_available("v1.2.3.20260801.aaaaaaa", "v1.2.3.20260801.aaaaaaa"))

    def test_executable_version_runs_extensionless_zipapp_through_python(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-upgrade-zipapp-") as tmp:
            artifact = Path(tmp) / "aha"
            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr(
                    "__main__.py",
                    "import sys\nprint('aha v9.8.7.test' if sys.argv[1:] == ['--version'] else '')\n",
                )

            self.assertEqual(executable_version(artifact), "v9.8.7.test")

    def test_executable_version_can_read_metadata_from_non_runnable_zipapp(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-upgrade-zipapp-metadata-") as tmp:
            artifact = Path(tmp) / "aha"
            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr("aha_cli/_build_version.py", 'BUILD_VERSION = "v2.3.4.20260801.abcdef0"\n')
                archive.writestr("__main__.py", "import definitely_missing_aha_dependency\n")

            self.assertEqual(executable_version(artifact), "v2.3.4.20260801.abcdef0")
            self.assertEqual(executable_version(artifact, require_runnable=True), "")

    def test_release_asset_url_supports_latest_and_tags(self) -> None:
        self.assertEqual(
            release_asset_url("ChinaKai/AHA", "latest", "aha"),
            "https://github.com/ChinaKai/AHA/releases/latest/download/aha",
        )
        self.assertEqual(
            release_asset_url("ChinaKai/AHA", "v1.2.3", "aha linux"),
            "https://github.com/ChinaKai/AHA/releases/download/v1.2.3/aha%20linux",
        )

    def test_upgrade_user_service_installs_local_artifact_without_restart(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-upgrade-test-") as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "bin" / "aha"
            artifact = tmp_path / "release" / "aha"
            target.parent.mkdir()
            artifact.parent.mkdir()
            write_fake_aha(target, "20260701.old")
            write_fake_aha(artifact, "20260711.new")

            result = upgrade_user_service(
                bin_path=target,
                service_name="aha-test",
                artifact=artifact,
                restart=False,
            )

            self.assertEqual(result["bin"], str(target))
            self.assertEqual(result["service"], "aha-test.service")
            self.assertEqual(result["source"], "artifact")
            self.assertEqual(result["previous_version"], "20260701.old")
            self.assertEqual(result["installed_version"], "20260711.new")
            self.assertFalse(result["restarted"])
            self.assertEqual(target.read_text(encoding="utf-8"), artifact.read_text(encoding="utf-8"))

    def test_check_user_service_upgrade_does_not_replace_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-upgrade-check-test-") as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "bin" / "aha"
            artifact = tmp_path / "release" / "aha"
            target.parent.mkdir()
            artifact.parent.mkdir()
            write_fake_aha(target, "20260701.old")
            write_fake_aha(artifact, "20260711.new")

            result = check_user_service_upgrade(bin_path=target, artifact=artifact)

            self.assertEqual(result["current_version"], "20260701.old")
            self.assertEqual(result["latest_version"], "20260711.new")
            self.assertTrue(result["update_available"])
            self.assertEqual(executable_version(target), "20260701.old")

    def test_service_upgrade_cli_installs_local_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-upgrade-cli-test-") as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "bin" / "aha"
            artifact = tmp_path / "release" / "aha"
            target.parent.mkdir()
            artifact.parent.mkdir()
            write_fake_aha(target, "20260701.old")
            write_fake_aha(artifact, "20260711.new")
            stdout = io.StringIO()

            with mock.patch("sys.stdout", stdout):
                code = main(
                    [
                        "service",
                        "upgrade-user",
                        "--bin",
                        str(target),
                        "--artifact",
                        str(artifact),
                        "--no-restart",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["bin"], str(target))
            self.assertEqual(payload["installed_version"], "20260711.new")

    def test_service_upgrade_cli_check_only_reports_versions_without_installing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-upgrade-cli-check-test-") as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "bin" / "aha"
            artifact = tmp_path / "release" / "aha"
            target.parent.mkdir()
            artifact.parent.mkdir()
            write_fake_aha(target, "20260701.old")
            write_fake_aha(artifact, "20260711.new")
            stdout = io.StringIO()

            with mock.patch("sys.stdout", stdout):
                code = main(
                    [
                        "service",
                        "upgrade-user",
                        "--bin",
                        str(target),
                        "--artifact",
                        str(artifact),
                        "--check-only",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["update_available"])
            self.assertEqual(payload["current_version"], "20260701.old")
            self.assertEqual(payload["latest_version"], "20260711.new")
            self.assertEqual(executable_version(target), "20260701.old")


if __name__ == "__main__":
    unittest.main()
