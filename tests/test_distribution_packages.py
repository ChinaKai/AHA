from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from aha_cli.cli import main
from aha_cli.web.upgrade import web_upgrade_status


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_DEB = REPO_ROOT / "scripts" / "build_linux_deb.py"
WINDOWS_BOOTSTRAP = REPO_ROOT / "scripts" / "windows_installer_bootstrap.py"
BUILD_WINDOWS = REPO_ROOT / "scripts" / "build_windows_installer.ps1"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class DistributionPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.deb = load_script_module("aha_build_linux_deb", BUILD_DEB)
        cls.bootstrap = load_script_module("aha_windows_installer_bootstrap", WINDOWS_BOOTSTRAP)

    def test_windows_bootstrap_passes_bundled_artifact_and_hash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-windows-bootstrap-") as tmp:
            payload_root = Path(tmp)
            payload = payload_root / "payload"
            payload.mkdir()
            (payload / "aha").write_bytes(b"aha")
            (payload / "install_windows.ps1").write_text("", encoding="utf-8")
            args = argparse.Namespace(
                mode="Full",
                agent_backend="Codex",
                aha_dir="",
                aha_home="",
                bind="127.0.0.1",
                port=8788,
                repair=False,
                strict_modules=False,
                with_browser=True,
                skip_browser_download=False,
                enable_startup=False,
                allow_downgrade=False,
                uninstall=False,
                no_shortcut=False,
                no_start=False,
                no_auth=False,
                allow_unsafe_bind=False,
            )
            with mock.patch.object(self.bootstrap, "payload_root", return_value=payload_root), mock.patch.object(
                self.bootstrap, "powershell_executable", return_value="powershell.exe"
            ):
                command = self.bootstrap.build_installer_command(args)

        self.assertEqual(command[0], "powershell.exe")
        self.assertIn("-Artifact", command)
        self.assertIn("-Sha256", command)
        self.assertIn("-WithBrowser", command)
        self.assertIn("Codex", command)

    def test_windows_installer_builder_uses_pyinstaller_and_optional_signtool(self) -> None:
        script = BUILD_WINDOWS.read_text(encoding="utf-8")

        self.assertIn('"--onefile"', script)
        self.assertIn('"--windowed"', script)
        self.assertNotIn('"--console"', script)
        self.assertIn('"--icon"', script)
        self.assertIn("aha.ico", script)
        self.assertIn('$BundledArtifact = Join-Path $PayloadRoot "aha"', script)
        self.assertIn('"--add-data"', script)
        self.assertIn("AHA_WINDOWS_SIGN_CERT_SHA1", script)
        self.assertIn("signtool.exe", script)
        self.assertIn("/fd SHA256", script)
        self.assertIn("/tr $TimestampUrl", script)

    def test_windows_installer_bootstrap_has_bilingual_gui_logo_and_uac(self) -> None:
        source = WINDOWS_BOOTSTRAP.read_text(encoding="utf-8")

        self.assertIn("import tkinter as tk_module", source)
        self.assertIn('"title": "AHA Setup"', source)
        self.assertIn('"title": "AHA 安装向导"', source)
        self.assertIn('bundled_payload("aha.ico")', source)
        self.assertIn('info.lpVerb = "runas"', source)
        self.assertIn("InstallerWizard(args).run()", source)
        self.assertIn("--smoke-test", source)
        self.assertIn('"action_install": "下一步：安装 {version}"', source)
        self.assertIn('footer.grid(row=1, column=0, columnspan=2, sticky="ew")', source)
        self.assertIn('canvas.create_window((0, 0), window=outer, anchor="nw")', source)
        self.assertIn('canvas.bind_all("<MouseWheel>", scroll_content)', source)
        self.assertIn("mode=\"determinate\"", source)
        self.assertIn("registered_installation()", source)
        self.assertIn("confirm_downgrade", source)

    def test_windows_installer_version_and_stage_helpers(self) -> None:
        self.assertEqual(
            self.bootstrap.compare_build_versions(
                "v0.1.202.20260822.abcdef0",
                "v0.1.201.20260821.abcdef0",
            ),
            1,
        )
        self.assertEqual(
            self.bootstrap.compare_build_versions(
                "v0.1.200.20260822.abcdef0",
                "v0.1.201.20260821.abcdef0",
            ),
            -1,
        )
        self.assertEqual(
            self.bootstrap.parse_installer_stage(
                "AHA_INSTALL_STAGE|55|modules|Installing optional modules"
            ),
            (55, "modules", "Installing optional modules"),
        )

    def test_windows_installer_detects_install_upgrade_repair_and_downgrade(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-installer-action-") as tmp:
            root = Path(tmp)
            payload = root / "payload"
            payload.mkdir()
            onebin = payload / "aha"
            install_dir = root / "installed"
            install_dir.mkdir()
            report = install_dir / "install-report.json"

            def write_bundle(version: str) -> None:
                with zipfile.ZipFile(onebin, "w") as archive:
                    archive.writestr("aha_cli/_build_version.py", f'BUILD_VERSION = "{version}"\n')

            with mock.patch.object(self.bootstrap, "payload_root", return_value=root):
                write_bundle("v0.1.201.20260822.abcdef0")
                self.assertEqual(self.bootstrap.installation_action(root / "missing")["action"], "install")

                report.write_text(
                    json.dumps({"version": "aha v0.1.200.20260821.abcdef0"}),
                    encoding="utf-8",
                )
                self.assertEqual(self.bootstrap.installation_action(install_dir)["action"], "upgrade")

                report.write_text(
                    json.dumps({"version": "aha v0.1.201.20260822.1234567"}),
                    encoding="utf-8",
                )
                self.assertEqual(self.bootstrap.installation_action(install_dir)["action"], "repair")

                report.write_text(
                    json.dumps({"version": "aha v0.1.202.20260823.abcdef0"}),
                    encoding="utf-8",
                )
                self.assertEqual(self.bootstrap.installation_action(install_dir)["action"], "downgrade")

    @unittest.skipUnless(shutil.which("dpkg-deb") is not None, "dpkg-deb unavailable")
    def test_linux_deb_contains_cli_and_systemd_user_unit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-deb-test-") as tmp:
            root = Path(tmp)
            onebin = root / "aha"
            with zipfile.ZipFile(onebin, "w") as archive:
                archive.writestr("aha_cli/_build_version.py", 'BUILD_VERSION = "v1.2.3.20260822.abcdef0"\n')
                archive.writestr("__main__.py", "print('aha')\n")
            output = root / "aha.deb"
            built = self.deb.build_linux_deb(
                onebin=onebin,
                output=output,
                architecture="amd64",
            )
            extract = root / "extract"
            completed = subprocess.run(
                ["dpkg-deb", "-x", str(built), str(extract)],
                check=False,
                capture_output=True,
                text=True,
            )
            fields = subprocess.run(
                ["dpkg-deb", "-f", str(built), "Package", "Version", "Architecture"],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(fields.returncode, 0, fields.stderr)
            self.assertTrue((extract / "usr/bin/aha").is_file())
            self.assertTrue((extract / "usr/lib/aha/aha").is_file())
            unit = (extract / "usr/lib/systemd/user/aha.service").read_text(encoding="utf-8")
            self.assertIn("service prepare-user", unit)
            self.assertIn("AHA_PACKAGE_MANAGER=deb", unit)
            self.assertIn("Package: aha", fields.stdout)
            self.assertIn("Version: 1.2.3.20260822.abcdef0", fields.stdout)
            self.assertIn("Architecture: amd64", fields.stdout)

    @unittest.skipUnless(shutil.which("dpkg-deb") is not None, "dpkg-deb unavailable")
    def test_package_deb_cli_builds_requested_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aha-deb-cli-") as tmp:
            root = Path(tmp)
            onebin = root / "aha"
            with zipfile.ZipFile(onebin, "w") as archive:
                archive.writestr("aha_cli/_build_version.py", 'BUILD_VERSION = "v2.0.1.20260822.abcdef0"\n')
                archive.writestr("__main__.py", "print('aha')\n")
            output = root / "aha_amd64.deb"
            code = main(
                [
                    "package",
                    "deb",
                    "--onebin",
                    str(onebin),
                    "--architecture",
                    "amd64",
                    "--output",
                    str(output),
                ]
            )

            self.assertEqual(code, 0)
            self.assertTrue(output.is_file())

    def test_deb_managed_runtime_disables_onebin_self_upgrade(self) -> None:
        with mock.patch.dict("os.environ", {"AHA_PACKAGE_MANAGER": "deb"}, clear=True):
            status = web_upgrade_status()

        self.assertFalse(status["available"])
        self.assertEqual(status["mode"], "package-manager")
        self.assertEqual(status["package_manager"], "deb")

    def test_release_workflow_builds_windows_and_linux_packages(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("portable-linux:", workflow)
        self.assertIn("windows-installer:", workflow)
        self.assertIn("publish:", workflow)
        self.assertIn("build_linux_deb.py", workflow)
        self.assertIn("build_windows_installer.ps1", workflow)
        self.assertIn("AHA-Setup-x64.exe", workflow)
        self.assertIn("--smoke-test", workflow)
        self.assertIn("aha_*.deb", workflow)
        self.assertIn("SHA256SUMS", workflow)


if __name__ == "__main__":
    unittest.main()
