from __future__ import annotations

import os
from pathlib import Path
import sys
import zipfile


SOURCE_UPGRADE_UNAVAILABLE_REASON = "Web upgrade is unavailable for source-started AHA services"


def _path_text(path: Path) -> str:
    return str(path.expanduser().resolve())


def _is_zipapp(path: Path) -> bool:
    try:
        return path.is_file() and zipfile.is_zipfile(path)
    except OSError:
        return False


def web_upgrade_status() -> dict:
    source_root = os.environ.get("AHA_SOURCE_ROOT", "").strip()
    if source_root:
        return {
            "available": False,
            "mode": "source",
            "reason": SOURCE_UPGRADE_UNAVAILABLE_REASON,
        }

    installed_bin = os.environ.get("AHA_INSTALL_BIN", "").strip()
    if installed_bin:
        installed_path = Path(installed_bin).expanduser()
        if installed_path.is_file():
            return {
                "available": True,
                "mode": "installed-onebin",
                "bin": _path_text(installed_path),
            }
        return {
            "available": False,
            "mode": "installed-onebin",
            "reason": f"installed AHA executable not found: {installed_path}",
        }

    executable_path = Path(sys.argv[0]).expanduser()
    if _is_zipapp(executable_path):
        return {
            "available": True,
            "mode": "current-onebin",
            "bin": _path_text(executable_path),
        }

    return {
        "available": False,
        "mode": "source",
        "reason": SOURCE_UPGRADE_UNAVAILABLE_REASON,
    }


def web_upgrade_command() -> list[str]:
    status = web_upgrade_status()
    if not status.get("available"):
        raise FileNotFoundError(str(status.get("reason") or SOURCE_UPGRADE_UNAVAILABLE_REASON))

    executable = str(status.get("bin") or "").strip()
    installed_bin = os.environ.get("AHA_INSTALL_BIN", "").strip()
    target_bin = _path_text(Path(installed_bin)) if installed_bin else executable
    command = [executable, "service", "upgrade-user", "--bin", target_bin, "--no-health-check", "--json"]
    service_name = os.environ.get("AHA_SERVICE_NAME", "").strip()
    if service_name:
        command.extend(["--service-name", service_name])
    release_url = os.environ.get("AHA_RELEASE_URL", "").strip()
    if release_url:
        command.extend(["--download-url", release_url])
    else:
        for env_name, flag in (
            ("AHA_RELEASE_REPO", "--repo"),
            ("AHA_RELEASE_VERSION", "--version"),
            ("AHA_RELEASE_ASSET", "--asset-name"),
        ):
            value = os.environ.get(env_name, "").strip()
            if value:
                command.extend([flag, value])
    return command
