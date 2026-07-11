from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_ONEBIN = REPO_ROOT / "scripts" / "install_user_service.sh"
INSTALL_SOURCE = REPO_ROOT / "scripts" / "install_source_user_service.sh"
SMOKE_SERVICE_INSTALLERS = REPO_ROOT / "scripts" / "smoke_service_installers.py"
PREFLIGHT_SERVICE_UPGRADE = REPO_ROOT / "scripts" / "preflight_service_upgrade.py"


class ServiceInstallerTests(unittest.TestCase):
    def run_script(
        self,
        args: list[str],
        *,
        tmp_path: Path,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("AHA_HOME", None)
        env.pop("AHA_RUN_ID", None)
        env["HOME"] = str(tmp_path / "home")
        env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
        env["USER"] = "aha-smoke"
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            args,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )

    def test_service_installers_have_valid_shell_syntax(self) -> None:
        for script in (INSTALL_ONEBIN, INSTALL_SOURCE):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    ["bash", "-n", str(script)],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_onebin_installer_dry_run_prints_unit_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-onebin-") as tmp:
            tmp_path = Path(tmp)
            bin_path = tmp_path / "bin" / "aha"
            aha_home = tmp_path / "onebin-home"
            token_file = aha_home / "web-token"
            service_path = tmp_path / "config" / "systemd" / "user" / "aha-test.service"
            result = self.run_script(
                [
                    "bash",
                    str(INSTALL_ONEBIN),
                    "--dry-run",
                    "--bin",
                    str(bin_path),
                    "--aha-home",
                    str(aha_home),
                    "--port",
                    "18788",
                    "--run-id",
                    "run-123",
                    "--service-name",
                    "aha-test",
                    "--no-start",
                    "--no-linger",
                ],
                tmp_path=tmp_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Dry-run: no files written, no executable downloaded or built, no services changed", result.stdout)
            self.assertIn("Install source: release-download", result.stdout)
            self.assertIn("Release repo: ChinaKai/AHA", result.stdout)
            self.assertIn("Release version: latest", result.stdout)
            self.assertIn("Release asset: aha", result.stdout)
            self.assertIn("Download URL: https://github.com/ChinaKai/AHA/releases/latest/download/aha", result.stdout)
            self.assertIn(f"Service path: {service_path}", result.stdout)
            self.assertIn(f'Environment="AHA_HOME={aha_home}"', result.stdout)
            self.assertIn(f'Environment="AHA_INSTALL_BIN={bin_path}"', result.stdout)
            self.assertIn('Environment="AHA_SERVICE_NAME=aha-test.service"', result.stdout)
            self.assertIn('Environment="AHA_RELEASE_REPO=ChinaKai/AHA"', result.stdout)
            self.assertIn('Environment="AHA_RELEASE_VERSION=latest"', result.stdout)
            self.assertIn('Environment="AHA_RELEASE_ASSET=aha"', result.stdout)
            self.assertIn("Health URL: http://127.0.0.1:18788/api/health", result.stdout)
            self.assertIn("Upgrade validation: 1", result.stdout)
            self.assertIn("Auth required: 1", result.stdout)
            self.assertIn(f"Auth token file: {token_file}", result.stdout)
            self.assertIn(
                f'ExecStart="{bin_path}" --home "{aha_home}" ui "run-123" --host "127.0.0.1" --port 18788 --auth-token-file "{token_file}"',
                result.stdout,
            )
            self.assertFalse(bin_path.exists())
            self.assertFalse(aha_home.exists())
            self.assertFalse((tmp_path / "home" / ".aha").exists())
            self.assertFalse(service_path.exists())
            self.assertFalse(token_file.exists())

    def test_onebin_installer_accepts_local_release_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-onebin-artifact-") as tmp:
            tmp_path = Path(tmp)
            artifact = tmp_path / "release" / "aha"
            artifact.parent.mkdir()
            artifact.write_text("#!/bin/sh\n", encoding="utf-8")
            result = self.run_script(
                [
                    "bash",
                    str(INSTALL_ONEBIN),
                    "--dry-run",
                    "--artifact",
                    str(artifact),
                    "--no-start",
                    "--no-linger",
                ],
                tmp_path=tmp_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Install source: artifact", result.stdout)
            self.assertIn(f"Artifact path: {artifact}", result.stdout)
            self.assertNotIn("Download URL:", result.stdout)

    def test_onebin_installer_uses_aha_config_proxy_for_downloads(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-onebin-proxy-") as tmp:
            tmp_path = Path(tmp)
            aha_home = tmp_path / "aha-home"
            aha_home.mkdir()
            (aha_home / "config.json").write_text(
                json.dumps(
                    {
                        "proxy": {
                            "enabled": True,
                            "http_proxy": "http://127.0.0.1:7897",
                            "https_proxy": "http://127.0.0.1:7897",
                            "no_proxy": "localhost,127.0.0.1,::1",
                        }
                    }
                ),
                encoding="utf-8",
            )
            result = self.run_script(
                [
                    "bash",
                    str(INSTALL_ONEBIN),
                    "--dry-run",
                    "--aha-home",
                    str(aha_home),
                    "--no-start",
                    "--no-linger",
                ],
                tmp_path=tmp_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Download proxy: aha-config", result.stdout)
            self.assertIn('Environment="HTTP_PROXY=http://127.0.0.1:7897"', result.stdout)
            self.assertIn('Environment="HTTPS_PROXY=http://127.0.0.1:7897"', result.stdout)
            self.assertIn('Environment="NO_PROXY=localhost,127.0.0.1,::1"', result.stdout)
            self.assertIn('Environment="http_proxy=http://127.0.0.1:7897"', result.stdout)
            self.assertIn('Environment="https_proxy=http://127.0.0.1:7897"', result.stdout)
            self.assertIn('Environment="no_proxy=localhost,127.0.0.1,::1"', result.stdout)

    def test_onebin_installer_default_home_ignores_ambient_aha_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-onebin-default-") as tmp:
            tmp_path = Path(tmp)
            default_home = tmp_path / "home" / ".aha"
            leaked_home = tmp_path / "leaked-home"
            result = self.run_script(
                [
                    "bash",
                    str(INSTALL_ONEBIN),
                    "--dry-run",
                    "--no-start",
                    "--no-linger",
                ],
                tmp_path=tmp_path,
                extra_env={"AHA_HOME": str(leaked_home)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"AHA home: {default_home}", result.stdout)
            self.assertIn(f'Environment="AHA_HOME={default_home}"', result.stdout)
            self.assertIn(f'--home "{default_home}"', result.stdout)
            self.assertNotIn(str(leaked_home), result.stdout)

    def test_source_installer_dry_run_prints_unit_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-source-") as tmp:
            tmp_path = Path(tmp)
            aha_home = tmp_path / "repo-home"
            token_file = aha_home / "web-token"
            service_path = tmp_path / "config" / "systemd" / "user" / "aha-src-test.service"
            result = self.run_script(
                [
                    "bash",
                    str(INSTALL_SOURCE),
                    "--dry-run",
                    "--aha-home",
                    str(aha_home),
                    "--port",
                    "18766",
                    "--run-id",
                    "source-run",
                    "--python",
                    "/usr/bin/python3",
                    "--service-name",
                    "aha-src-test",
                    "--no-start",
                    "--no-enable",
                ],
                tmp_path=tmp_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Dry-run: no files written, no services changed", result.stdout)
            self.assertIn(f"Service path: {service_path}", result.stdout)
            self.assertIn(f"WorkingDirectory={REPO_ROOT}", result.stdout)
            self.assertIn(f'Environment="PYTHONPATH={REPO_ROOT}/src"', result.stdout)
            self.assertIn(f'Environment="AHA_HOME={aha_home}"', result.stdout)
            self.assertIn(f'Environment="AHA_SOURCE_ROOT={REPO_ROOT}"', result.stdout)
            self.assertIn("Health URL: http://127.0.0.1:18766/api/health", result.stdout)
            self.assertIn("Version validation: 1", result.stdout)
            self.assertIn("Auth required: 1", result.stdout)
            self.assertIn(f"Auth token file: {token_file}", result.stdout)
            self.assertIn(
                f'ExecStart="/usr/bin/python3" -m aha_cli --home "{aha_home}" ui "source-run" --host "127.0.0.1" --port 18766 --auth-token-file "{token_file}"',
                result.stdout,
            )
            self.assertFalse(aha_home.exists())
            self.assertFalse((tmp_path / "home" / ".aha").exists())
            self.assertFalse(service_path.exists())
            self.assertFalse(token_file.exists())

    def test_onebin_installer_requires_override_for_unauthenticated_network_bind(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-unsafe-") as tmp:
            tmp_path = Path(tmp)
            result = self.run_script(
                [
                    "bash",
                    str(INSTALL_ONEBIN),
                    "--dry-run",
                    "--host",
                    "0.0.0.0",
                    "--no-auth",
                    "--no-start",
                    "--no-linger",
                ],
                tmp_path=tmp_path,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("--no-auth with 0.0.0.0:8788 requires --allow-unsafe-bind", result.stderr)

    def test_service_installer_smoke_script(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SMOKE_SERVICE_INSTALLERS), "--json"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("onebin release dry-run unit no-write", payload["checks"])
        self.assertIn("source dry-run unit no-write", payload["checks"])
        self.assertIn("installer dry-run AHA home no-write", payload["checks"])
        self.assertFalse(payload["onebin"]["built_executable"])
        self.assertFalse(payload["onebin"]["created_aha_home"])
        self.assertFalse(payload["onebin"]["created_home_aha"])
        self.assertFalse(payload["onebin"]["wrote_service"])
        self.assertFalse(payload["onebin"]["wrote_token"])
        self.assertFalse(payload["source"]["created_aha_home"])
        self.assertFalse(payload["source"]["created_home_aha"])
        self.assertFalse(payload["source"]["wrote_service"])
        self.assertFalse(payload["source"]["wrote_token"])

    def test_service_upgrade_preflight_runs_no_write_checks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-service-preflight-test-") as tmp:
            tmp_path = Path(tmp)
            env = os.environ.copy()
            env.pop("AHA_HOME", None)
            env.pop("AHA_RUN_ID", None)
            env["HOME"] = str(tmp_path / "home")
            env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
            env["USER"] = "aha-smoke"
            result = subprocess.run(
                [sys.executable, str(PREFLIGHT_SERVICE_UPGRADE), "--skip-onebin-build", "--json"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "passed")
            self.assertIn("source entrypoint --version", payload["checks"])
            self.assertIn("service installer dry-run no-write", payload["checks"])
            self.assertIn("temporary onebin build skipped", payload["checks"])
            self.assertTrue(payload["real_home_runs"]["unchanged"])
            self.assertFalse((tmp_path / "home" / ".aha" / "runs").exists())
            self.assertEqual(payload["onebin"]["built"], False)


if __name__ == "__main__":
    unittest.main()
