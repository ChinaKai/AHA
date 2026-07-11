from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.service_upgrade import release_asset_url, upgrade_user_service


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


if __name__ == "__main__":
    unittest.main()
