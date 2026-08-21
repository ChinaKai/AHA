from __future__ import annotations

import os
import sys
from pathlib import Path

from aha_cli.services.onebin import (
    AHA_RUNTIME_PYTHON_ENV,
    AHA_WSL_AHA_BIN_ENV,
    console_python_executable,
)


def _running_zipapp() -> Path | None:
    try:
        from aha_cli.services.onebin import authoritative_onebin_path
    except (ImportError, SystemExit):  # pragma: no cover - import fallback
        return None
    try:
        return authoritative_onebin_path()
    except Exception:  # pragma: no cover - defensive, never break PATH setup
        return None


def _aha_cli_dir(zipapp_path: Path | None = None) -> Path | None:
    """Directory that exposes the ``aha`` CLI for backend subprocesses.

    Prefers the running zipapp (onebin) so a packaged dashboard can hand its own
    ``aha`` command to child processes; otherwise falls back to the current
    Python executable directory (pip console-script / editable installs).
    """
    if zipapp_path is not None:
        return zipapp_path.parent
    executable_dir = Path(sys.executable).parent
    return executable_dir if executable_dir.is_dir() else None


def _ensure_windows_backend_bin(zipapp_path: Path) -> Path | None:
    """Create Windows command shims without exposing the raw extensionless zipapp."""

    bin_dir = zipapp_path.parent / "backend-bin"
    python = console_python_executable()
    wrappers = {
        "aha.cmd": f'@echo off\r\n"{python}" "{zipapp_path}" %*\r\n',
        "python3.cmd": f'@echo off\r\n"{python}" %*\r\n',
    }
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name, body in wrappers.items():
            shim = bin_dir / name
            encoded = body.encode("utf-8")
            if not shim.exists() or shim.read_bytes() != encoded:
                shim.write_text(body, encoding="utf-8", newline="")
    except OSError:
        return None
    return bin_dir


def _ensure_wsl_backend_bin(zipapp_path: Path, home: Path) -> Path:
    """Materialize an ``aha``-only PATH dir for WSL backend children.

    The Windows onebin directory cannot be prepended as-is in WSL (its
    ``python3`` shim targets Windows pythonw and would hijack
    /usr/bin/python3), yet backend shells must still resolve ``aha`` to the
    orchestrating Windows instance first — a separate WSL AHA install the
    user keeps (e.g. ~/.local/bin/aha) must never shadow it inside backend
    processes. Emit a dedicated dir holding only an ``aha`` symlink to the
    running onebin: first on PATH, no interpreter shadowing, no duplicate
    copy to drift. The user's own bin dirs are never touched.
    """
    data_home = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    bin_dir = data_home / "aha" / "backend-bin"
    link = bin_dir / "aha"
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() and Path(os.readlink(link)) != zipapp_path:
            link.unlink()
        if link.is_symlink() and Path(os.readlink(link)) == zipapp_path:
            return bin_dir
        if link.exists():
            link.unlink()
        link.symlink_to(zipapp_path)
    except OSError:
        # Best-effort: `aha` falls back to whatever the user's PATH provides.
        pass
    return bin_dir


def add_user_backend_paths(env: dict[str, str], *, home: Path | None = None) -> None:
    home = home or Path.home()
    candidates = [
        home / ".local" / "bin",
        home / ".npm-global" / "bin",
    ]
    nvm_root = home / ".nvm" / "versions" / "node"
    if nvm_root.is_dir():
        candidates.extend(sorted(nvm_root.glob("*/bin"), reverse=True))

    zipapp_path = _running_zipapp()
    # Prepend an authoritative AHA command only for packaged runtimes. Windows
    # uses a dedicated .cmd-only directory so the extensionless zipapp is never
    # offered to ShellExecute; POSIX can execute the zipapp directly. Under an
    # editable/source install the ``aha`` console-script is already on PATH via
    # pip, so prepending sys.executable's directory would shadow user bin dirs.
    if zipapp_path is not None:
        aha_dir = _aha_cli_dir(zipapp_path)
        forwarded_wsl_onebin = bool(str(os.environ.get(AHA_WSL_AHA_BIN_ENV) or "").strip())
        if sys.platform == "win32" and not forwarded_wsl_onebin:
            backend_bin = _ensure_windows_backend_bin(zipapp_path)
            if backend_bin is not None:
                candidates.insert(0, backend_bin)
                env[AHA_RUNTIME_PYTHON_ENV] = console_python_executable()
            aha_dir = None
        # A WSL-hosted watcher runs the Windows onebin from /mnt/<drive>/...;
        # that directory's ``python3`` shim targets Windows pythonw (CRLF,
        # unusable here) and would shadow /usr/bin/python3 for every backend
        # child shell. Prepend a dedicated ``aha``-only bin dir instead: the
        # orchestrating Windows instance stays first for ``aha`` lookups
        # (ahead of any separate WSL AHA the user keeps) without dragging the
        # Windows python3 shim onto PATH.
        if aha_dir is not None and not forwarded_wsl_onebin and not str(aha_dir).startswith("/mnt/"):
            candidates.insert(0, aha_dir)
        elif forwarded_wsl_onebin or sys.platform != "win32":
            candidates.insert(0, _ensure_wsl_backend_bin(zipapp_path, home))

    existing = [item for item in env.get("PATH", "").split(os.pathsep) if item]
    merged: list[str] = []
    seen: set[str] = set()
    for path in [str(candidate) for candidate in candidates if candidate.is_dir()] + existing:
        if path in seen:
            continue
        seen.add(path)
        merged.append(path)
    if merged:
        env["PATH"] = os.pathsep.join(merged)
