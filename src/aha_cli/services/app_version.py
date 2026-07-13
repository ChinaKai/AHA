from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import subprocess

from aha_cli._build_version import BUILD_VERSION


def _git_output(root: Path, args: list[str], *, timeout: int = 1) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def _git_output_or_empty(root: Path, args: list[str]) -> str:
    try:
        return _git_output(root, args)
    except (OSError, subprocess.SubprocessError):
        return ""


def _source_git_status(root: Path) -> str:
    parts: list[str] = []
    if _git_output_or_empty(root, ["status", "--porcelain=v1", "--untracked-files=all"]):
        parts.append("dirty")
    upstream = _git_output_or_empty(root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    if upstream:
        ahead = _git_output_or_empty(root, ["rev-list", "--count", f"{upstream}..HEAD"])
        behind = _git_output_or_empty(root, ["rev-list", "--count", f"HEAD..{upstream}"])
        if ahead.isdigit() and int(ahead) > 0:
            parts.append("ahead")
        if behind.isdigit() and int(behind) > 0:
            parts.append("behind")
    return "-".join(parts) if parts else "clean"


@lru_cache(maxsize=1)
def _source_tree_version() -> str:
    for path in (Path.cwd(), Path(__file__).resolve()):
        start = path if path.is_dir() else path.parent
        for directory in (start, *start.parents):
            if not (directory / ".git").exists():
                continue
            try:
                commit = _git_output(directory, ["rev-parse", "--short=7", "HEAD"])
            except (OSError, subprocess.SubprocessError):
                return ""
            return f"vsource.{commit}.{_source_git_status(directory)}" if commit else ""
    return ""


def aha_version(root: Path | None = None) -> str:
    del root
    return str(os.environ.get("AHA_VERSION") or BUILD_VERSION or _source_tree_version() or "").strip()
