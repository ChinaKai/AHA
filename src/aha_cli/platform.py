"""Cross-platform platform helpers.

Centralizes OS detection, temporary-directory roots, and the default shell so the
rest of the codebase never hard-codes POSIX-only literals such as ``/tmp``,
``/var/tmp``, or ``/bin/sh``. Combined with the lazy POSIX imports in
``services/local_terminal.py``, this lets the package import and boot on Windows.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

WIN = sys.platform == "win32"


def is_windows() -> bool:
    """True on Windows."""
    return WIN


def temp_root() -> Path:
    """Canonical temp root for this OS (replaces hard-coded ``/tmp``)."""
    return Path(tempfile.gettempdir())


def candidate_temp_roots() -> list[Path]:
    """Temp roots worth scanning for nested ``.aha`` homes.

    On POSIX this includes the conventional ``/tmp`` and ``/var/tmp`` in addition
    to ``tempfile.gettempdir()``. On Windows only the OS temp dir is used.
    """
    seen: list[Path] = [Path(tempfile.gettempdir())]
    if not WIN:
        for candidate in ("/tmp", "/var/tmp"):
            path = Path(candidate)
            if path not in seen:
                seen.append(path)
    return seen


def default_shell() -> str:
    """User's preferred shell if set and present, else an OS-appropriate default."""
    if WIN:
        return str(os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe")
    shell = str(os.environ.get("SHELL") or "").strip()
    if shell and Path(shell).exists():
        return shell
    return "/bin/sh"


def expand_path(value: str) -> str:
    """Expand ``~`` and ``$VAR``/``${VAR}`` tokens in a path-like string.

    Cross-platform (``os.path.expanduser`` + ``os.path.expandvars``). Bare names
    such as ``codex`` or ``claude`` pass through unchanged so PATH lookup still
    works. Empty/falsy input is returned as-is.
    """
    if not value:
        return value
    return os.path.expanduser(os.path.expandvars(str(value)))


def spawn_command(argv: list[str]) -> list[str]:
    """Return ``argv`` adjusted for the host OS so backend CLIs spawn correctly.

    Windows ``CreateProcess`` cannot execute npm-style ``.cmd``/``.bat`` shims
    (e.g. ``claude.cmd``, ``codex.cmd``) directly, so a bare command that
    resolves to such a shim is rewritten to
    ``["cmd.exe", "/c", <resolved-path>, *args]``. A bare name that resolves to
    a real ``.exe`` uses the full path; anything unresolved passes through
    unchanged (so a missing CLI still surfaces a clear FileNotFoundError).
    POSIX returns ``argv`` unchanged.
    """
    if not WIN or not argv:
        return argv
    name = str(argv[0])
    resolved = shutil.which(name)
    target = resolved or name
    if target.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", target, *argv[1:]]
    if resolved:
        return [resolved, *argv[1:]]
    return argv
