from __future__ import annotations

import importlib
import json
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import zipapp
import zipfile

AHA_WSL_AHA_BIN_ENV = "AHA_WSL_AHA_BIN"
AHA_RUNTIME_PYTHON_ENV = "AHA_RUNTIME_PYTHON"


def _ignore_build_artifacts(_path: str, names: list[str]) -> set[str]:
    ignored = {"__pycache__"}
    ignored.update(name for name in names if name.endswith((".pyc", ".pyo")))
    return ignored


def running_zipapp_path() -> Path | None:
    """Path to the zipapp (onebin) this process is running from, or ``None``."""
    raw_path = sys.argv[0] if sys.argv else ""
    if not raw_path:
        return None
    try:
        candidate = Path(raw_path).expanduser().resolve()
    except OSError:
        return None
    try:
        if candidate.is_file() and zipfile.is_zipfile(candidate):
            return candidate
    except OSError:
        return None
    return None


def authoritative_onebin_path() -> Path | None:
    """Return the onebin that owns this runtime, including a WSL handoff.

    A Windows Web service launches WSL backend watchers through the same onebin,
    but later agent-shell commands can enter through a WSL-side shim or
    ``python -m aha_cli``. The forwarded path remains the authority in that
    case, preventing a separate WSL AHA install from spawning stale hardware
    bridges. Source/editable runs have no forwarded path and keep the normal
    module fallback.
    """
    raw_path = str(os.environ.get(AHA_WSL_AHA_BIN_ENV) or "").strip()
    if raw_path:
        try:
            candidate = Path(raw_path).expanduser().resolve()
            if candidate.is_file() and zipfile.is_zipfile(candidate):
                return candidate
        except OSError:
            pass
    return running_zipapp_path()


def console_python_executable(executable: str | Path | None = None) -> str:
    """Return a console-capable Python executable when one is available."""

    current = Path(executable or sys.executable)
    if current.name.lower() != "pythonw.exe":
        return str(current)
    candidate = current.with_name("python.exe")
    return str(candidate if candidate.is_file() else current)


def _install_report_python(zipapp_path: Path | None) -> Path | None:
    if zipapp_path is None:
        return None
    report_path = zipapp_path.parent / "install-report.json"
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    candidate = Path(str(payload.get("python") or "")).expanduser()
    return candidate if candidate.is_file() else None


def _python_supports_module(executable: Path, module: str) -> bool:
    code = (
        "import importlib,sys;"
        f"importlib.import_module({module!r});sys.exit(0)"
    )
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    try:
        result = subprocess.run(
            [str(executable), "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def resolve_aha_python(required_module: str | None = None) -> str | None:
    """Resolve the interpreter AHA should use for a child CLI invocation.

    The running interpreter remains authoritative unless a required optional
    module is missing. On Windows, installed AHA metadata and the default AHA
    virtual environment provide conservative recovery candidates.
    """

    current = Path(sys.executable)
    if not required_module:
        return str(current)
    try:
        importlib.import_module(required_module)
        return str(current)
    except ImportError:
        pass

    candidates: list[Path] = []
    configured = str(os.environ.get(AHA_RUNTIME_PYTHON_ENV) or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    zipapp_path = authoritative_onebin_path()
    reported = _install_report_python(zipapp_path)
    if reported is not None:
        candidates.append(reported)
    if sys.platform == "win32":
        try:
            candidates.append(Path.home() / ".venvs" / "aha" / "Scripts" / "python.exe")
        except RuntimeError:
            pass

    current_key = os.path.normcase(os.path.abspath(str(current)))
    seen = {current_key}
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = os.path.normcase(os.path.abspath(str(resolved)))
        if key in seen or not resolved.is_file():
            continue
        seen.add(key)
        if _python_supports_module(resolved, required_module):
            return str(resolved)
    return None


def aha_cli_invocation(*, required_module: str | None = None) -> list[str]:
    """Command prefix to invoke this AHA runtime's CLI in a subprocess.

    Uses the running zipapp (onebin) when present so a packaged dashboard can
    spawn bridges/backends without an importable ``aha_cli`` module; otherwise
    ``python -m aha_cli`` for source checkouts.
    """
    python = resolve_aha_python(required_module) or sys.executable
    zipapp_path = authoritative_onebin_path()
    if zipapp_path:
        return [str(python), str(zipapp_path)]
    return [str(python), "-m", "aha_cli"]


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


def _bundle_optional_package(staging: Path, name: str) -> bool:
    """Copy a pure-Python third-party package into the onebin staging dir.

    Used to bundle pyserial (the ``serial`` package) so a packaged dashboard can
    do hardware serial debug without a separate ``pip install``. Silently skips
    when the package is not installed in the build environment.
    """
    import importlib.util

    spec = importlib.util.find_spec(name)
    if spec is None or spec.submodule_search_locations is None:
        return False
    for location in spec.submodule_search_locations:
        source = Path(location)
        if source.is_dir() and not (staging / name).exists():
            shutil.copytree(source, staging / name, ignore=_ignore_build_artifacts)
            return True
    return False


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
    try:
        tag = _git_output(source, ["describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"])
    except (OSError, subprocess.SubprocessError):
        tag = ""
    if tag and date and commit:
        return f"{tag}.{date}.{commit}"
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
        # Bundle pure-Python optional deps so the packaged artifact is self-contained.
        _bundle_optional_package(staging, "serial")  # pyserial: hardware serial debug
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
