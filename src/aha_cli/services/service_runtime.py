from __future__ import annotations

import os
from pathlib import Path
import platform as stdlib_platform
import sys
import zipfile

from aha_cli.domain.models import utc_now
from aha_cli.services.app_version import aha_version
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import aha_home_path


def service_runtime_path(root: Path) -> Path:
    return aha_home_path(root) / "runtime" / "service.json"


def _install_mode() -> str:
    if str(os.environ.get("AHA_SOURCE_ROOT") or "").strip():
        return "source"
    executable = Path(str(sys.argv[0] or ""))
    try:
        if executable.is_file() and zipfile.is_zipfile(executable):
            return "onebin"
    except OSError:
        pass
    return "python"


def build_service_runtime(
    root: Path,
    *,
    host: str = "",
    port: int | str | None = None,
    auth_required: bool = False,
    status: str = "running",
) -> dict:
    home = aha_home_path(root).resolve()
    source_root = str(os.environ.get("AHA_SOURCE_ROOT") or "").strip()
    return {
        "schema_version": 1,
        "service": "aha-web",
        "status": str(status or "unknown"),
        "aha_version": aha_version(root),
        "platform": stdlib_platform.system() or sys.platform,
        "platform_release": stdlib_platform.release(),
        "architecture": stdlib_platform.machine(),
        "install_mode": _install_mode(),
        "aha_home": str(home),
        "service_working_directory": str(Path.cwd().resolve()),
        "source_root": source_root,
        "bind_host": str(host or ""),
        "bind_port": str(port or ""),
        "auth_required": bool(auth_required),
        "pid": os.getpid(),
        "updated_at": utc_now(),
    }


def write_service_runtime(
    root: Path,
    *,
    host: str = "",
    port: int | str | None = None,
    auth_required: bool = False,
    status: str = "running",
) -> dict:
    runtime = build_service_runtime(
        root,
        host=host,
        port=port,
        auth_required=auth_required,
        status=status,
    )
    path = service_runtime_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, runtime)
    return runtime


def read_service_runtime(root: Path) -> dict:
    try:
        runtime = read_json(service_runtime_path(root))
    except (FileNotFoundError, OSError, ValueError):
        runtime = build_service_runtime(root, status="unknown")
    return runtime if isinstance(runtime, dict) else build_service_runtime(root, status="unknown")


def service_runtime_prompt_payload(root: Path) -> dict:
    runtime = read_service_runtime(root)
    allowed = {
        "schema_version",
        "service",
        "status",
        "aha_version",
        "platform",
        "platform_release",
        "architecture",
        "install_mode",
        "aha_home",
        "service_working_directory",
        "source_root",
        "bind_host",
        "bind_port",
        "auth_required",
    }
    return {key: runtime.get(key) for key in allowed}


__all__ = [
    "build_service_runtime",
    "read_service_runtime",
    "service_runtime_path",
    "service_runtime_prompt_payload",
    "write_service_runtime",
]
