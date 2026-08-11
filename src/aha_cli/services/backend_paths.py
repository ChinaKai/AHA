from __future__ import annotations

import os
import sys
from pathlib import Path


def _aha_cli_dir() -> Path | None:
    """Directory that exposes the ``aha`` CLI for backend subprocesses.

    Prefers the running zipapp (onebin) so a packaged dashboard can hand its own
    ``aha`` command to child processes; otherwise falls back to the current
    Python executable directory (pip console-script / editable installs).
    """
    try:
        from aha_cli.services.onebin import running_zipapp_path
    except (ImportError, SystemExit):  # pragma: no cover - import fallback
        running_zipapp_path = None
    zipapp_path = running_zipapp_path() if running_zipapp_path else None
    if zipapp_path is not None:
        return zipapp_path.parent
    executable_dir = Path(sys.executable).parent
    return executable_dir if executable_dir.is_dir() else None


def add_user_backend_paths(env: dict[str, str], *, home: Path | None = None) -> None:
    home = home or Path.home()
    candidates = [
        home / ".local" / "bin",
        home / ".npm-global" / "bin",
    ]
    nvm_root = home / ".nvm" / "versions" / "node"
    if nvm_root.is_dir():
        candidates.extend(sorted(nvm_root.glob("*/bin"), reverse=True))

    aha_dir = _aha_cli_dir()
    if aha_dir is not None:
        candidates.insert(0, aha_dir)

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
