from __future__ import annotations

import re
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import zipapp


def _ignore_build_artifacts(_path: str, names: list[str]) -> set[str]:
    ignored = {"__pycache__"}
    ignored.update(name for name in names if name.endswith((".pyc", ".pyo")))
    return ignored


def default_source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git_output(root: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    return result.stdout.strip()


def _existing_build_version(package_dir: Path) -> str:
    path = package_dir / "_build_version.py"
    if not path.is_file():
        return ""
    match = re.search(r"BUILD_VERSION\s*=\s*[\"']([^\"']*)[\"']", path.read_text(encoding="utf-8"))
    return match.group(1).strip() if match else ""


def build_version_for_source(source: Path) -> str:
    package_dir = source / "aha_cli"
    try:
        date = _git_output(source, ["show", "-s", "--format=%cd", "--date=format:%Y%m%d", "HEAD"])
        commit = _git_output(source, ["rev-parse", "--short=7", "HEAD"])
    except (OSError, subprocess.SubprocessError):
        return _existing_build_version(package_dir)
    return f"{date}.{commit}" if date and commit else _existing_build_version(package_dir)


def build_onebin(
    output: Path,
    *,
    source_root: Path | None = None,
    interpreter: str = "/usr/bin/env python3",
    compressed: bool = True,
) -> Path:
    source = (source_root or default_source_root()).resolve()
    package_dir = source / "aha_cli"
    if not (package_dir / "cli.py").is_file():
        raise ValueError(f"AHA source root not found or invalid: {source}")

    target = output.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="aha-onebin-") as tmp:
        staging = Path(tmp) / "app"
        staging.mkdir()
        shutil.copytree(package_dir, staging / "aha_cli", ignore=_ignore_build_artifacts)
        version = build_version_for_source(source)
        (staging / "aha_cli" / "_build_version.py").write_text(
            f"from __future__ import annotations\n\nBUILD_VERSION = {version!r}\n",
            encoding="utf-8",
        )
        (staging / "__main__.py").write_text(
            "from aha_cli.cli import main\n\nraise SystemExit(main())\n",
            encoding="utf-8",
        )
        zipapp.create_archive(
            staging,
            target=target,
            interpreter=interpreter,
            compressed=compressed,
        )

    mode = target.stat().st_mode
    target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target
