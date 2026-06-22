from __future__ import annotations

import os
from pathlib import Path


def add_user_backend_paths(env: dict[str, str], *, home: Path | None = None) -> None:
    home = home or Path.home()
    candidates = [
        home / ".local" / "bin",
        home / ".npm-global" / "bin",
    ]
    nvm_root = home / ".nvm" / "versions" / "node"
    if nvm_root.is_dir():
        candidates.extend(sorted(nvm_root.glob("*/bin"), reverse=True))

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
