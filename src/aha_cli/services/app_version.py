from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import subprocess

from aha_cli._build_version import BUILD_VERSION


@lru_cache(maxsize=1)
def _source_tree_version() -> str:
    for path in (Path.cwd(), Path(__file__).resolve()):
        start = path if path.is_dir() else path.parent
        for directory in (start, *start.parents):
            if not (directory / ".git").exists():
                continue
            try:
                result = subprocess.run(
                    ["git", "-C", str(directory), "rev-parse", "--short=7", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
            except (OSError, subprocess.SubprocessError):
                return ""
            commit = result.stdout.strip()
            return f"source.{commit}" if commit else ""
    return ""


def aha_version(root: Path | None = None) -> str:
    del root
    return str(os.environ.get("AHA_VERSION") or BUILD_VERSION or _source_tree_version() or "").strip()
