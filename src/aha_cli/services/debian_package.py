from __future__ import annotations

import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import tempfile

from aha_cli.services.service_install import render_packaged_systemd_user_unit
from aha_cli.services.service_upgrade import zipapp_build_version


def debian_architecture(value: str) -> str:
    requested = str(value or "auto").strip().lower()
    if requested and requested != "auto":
        return requested
    try:
        completed = subprocess.run(
            ["dpkg", "--print-architecture"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    detected = str(completed.stdout if completed and completed.returncode == 0 else "").strip()
    if detected:
        return detected
    machine = platform.machine().lower()
    return {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, machine or "all")


def debian_version(value: str) -> str:
    version = str(value or "").strip()
    if version.startswith("v"):
        version = version[1:]
    version = re.sub(r"[^A-Za-z0-9.+:~\-]", ".", version)
    if not version:
        raise ValueError("package version is empty")
    if not version[0].isdigit():
        version = f"0~{version}"
    return version


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def build_linux_deb(
    *,
    onebin: Path,
    output: Path,
    architecture: str = "auto",
    version: str = "",
) -> Path:
    artifact = onebin.expanduser().resolve()
    if not artifact.is_file():
        raise ValueError(f"AHA onebin does not exist: {artifact}")
    if not shutil.which("dpkg-deb"):
        raise RuntimeError("dpkg-deb is required to build the Linux package")
    package_version = debian_version(version or zipapp_build_version(artifact))
    package_arch = debian_architecture(architecture)
    target = output.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="aha-deb-") as tmp:
        root = Path(tmp) / "aha"
        debian = root / "DEBIAN"
        debian.mkdir(parents=True)
        (root / "usr/lib/aha").mkdir(parents=True)
        shutil.copy2(artifact, root / "usr/lib/aha/aha")
        (root / "usr/lib/aha/aha").chmod(0o755)
        _write_executable(
            root / "usr/bin/aha",
            "#!/bin/sh\nexec python3 /usr/lib/aha/aha \"$@\"\n",
        )
        unit_path = root / "usr/lib/systemd/user/aha.service"
        unit_path.parent.mkdir(parents=True)
        unit_path.write_text(render_packaged_systemd_user_unit(), encoding="utf-8")
        docs = root / "usr/share/doc/aha"
        docs.mkdir(parents=True)
        (docs / "README.Debian").write_text(
            "\n".join(
                [
                    "AHA is installed as a user-scoped local Web service.",
                    "",
                    "Enable it for the current user:",
                    "  systemctl --user enable --now aha.service",
                    "",
                    "Open http://127.0.0.1:8788 after startup.",
                    "AHA data remains in ~/.aha and is not removed with the package.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (debian / "control").write_text(
            "\n".join(
                [
                    "Package: aha",
                    f"Version: {package_version}",
                    "Section: devel",
                    "Priority: optional",
                    f"Architecture: {package_arch}",
                    "Maintainer: AHA contributors",
                    "Depends: python3 (>= 3.10)",
                    "Recommends: git, nodejs, npm",
                    "Description: local-first AI agent workbench",
                    " AHA organizes Codex, Claude, OpenCode, and other backends",
                    " as Run, Task, and Agent workflows through a local Web UI.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        _write_executable(
            debian / "postinst",
            "\n".join(
                [
                    "#!/bin/sh",
                    "set -e",
                    'if [ "${1:-}" = "configure" ]; then',
                    '  echo "AHA installed. Enable it with: systemctl --user enable --now aha.service"',
                    "fi",
                    "exit 0",
                    "",
                ]
            ),
        )
        env = os.environ.copy()
        env.setdefault("SOURCE_DATE_EPOCH", "0")
        completed = subprocess.run(
            ["dpkg-deb", "--root-owner-group", "--build", str(root), str(target)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if completed.returncode != 0:
            details = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"dpkg-deb failed: {details}")
    return target


__all__ = ["build_linux_deb", "debian_architecture", "debian_version"]
