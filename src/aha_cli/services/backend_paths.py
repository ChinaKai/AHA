from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def _running_zipapp() -> Path | None:
    try:
        from aha_cli.services.onebin import running_zipapp_path
    except (ImportError, SystemExit):  # pragma: no cover - import fallback
        return None
    try:
        return running_zipapp_path()
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


def _ensure_windows_python3_shim(zipapp_path: Path | None) -> None:
    """On Windows, ensure a ``python3`` shim exists next to the onebin.

    The zipapp shebang is ``#!/usr/bin/env python3``; on Windows the bare
    ``python3`` commonly resolves to the Microsoft Store redirector stub
    (AppInstallerPythonRedirector.exe), which exits non-zero without a Store
    install. An extensionless POSIX shim lets ``env`` find a real interpreter in
    backend shells (Git Bash / MSYS2). Linux and macOS already provide a working
    ``python3``, so no shim is created there - shadowing the system command in a
    PATH-prepended directory would be harmful.
    """
    if sys.platform != "win32" or zipapp_path is None:
        return
    target = str(sys.executable).replace("\\", "/")
    shim = zipapp_path.parent / "python3"
    body = f"#!/bin/sh\nexec \"{target}\" \"$@\"\n"
    try:
        if not shim.exists() or shim.read_text(encoding="utf-8") != body:
            shim.write_text(body, encoding="utf-8")
            shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        # Best-effort: a missing/stale shim only affects `python3` resolution in
        # backend shells, never AHA itself.
        pass


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
    _ensure_windows_python3_shim(zipapp_path)
    # Prepend the running onebin's directory only when running as a packaged
    # zipapp: the onebin ships its own ``aha`` + python3 shim that child backend
    # processes must resolve. Under an editable/source install the ``aha``
    # console-script is already on PATH via pip, so prepending sys.executable's
    # directory here would shadow user bin dirs (.local/bin, nvm) with the
    # system interpreter dir.
    if zipapp_path is not None:
        aha_dir = _aha_cli_dir(zipapp_path)
        # A WSL-hosted watcher runs the Windows onebin from /mnt/<drive>/...;
        # that directory's ``python3`` shim targets Windows pythonw (CRLF,
        # unusable here) and would shadow /usr/bin/python3 for every backend
        # child shell. Prepend a dedicated ``aha``-only bin dir instead: the
        # orchestrating Windows instance stays first for ``aha`` lookups
        # (ahead of any separate WSL AHA the user keeps) without dragging the
        # Windows python3 shim onto PATH.
        if aha_dir is not None and not str(aha_dir).startswith("/mnt/"):
            candidates.insert(0, aha_dir)
        else:
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
