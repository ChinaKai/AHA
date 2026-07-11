from __future__ import annotations

from pathlib import Path
import os
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import quote
from urllib.request import Request, urlopen


DEFAULT_RELEASE_REPO = "ChinaKai/AHA"
DEFAULT_RELEASE_VERSION = "latest"
DEFAULT_RELEASE_ASSET = "aha"
DEFAULT_SERVICE_NAME = "aha.service"


class ServiceUpgradeError(RuntimeError):
    pass


def normalize_service_name(service_name: str | None) -> str:
    value = str(service_name or DEFAULT_SERVICE_NAME).strip() or DEFAULT_SERVICE_NAME
    return value if value.endswith(".service") else f"{value}.service"


def release_asset_url(repo: str, version: str, asset_name: str) -> str:
    repo_value = str(repo or DEFAULT_RELEASE_REPO).strip() or DEFAULT_RELEASE_REPO
    version_value = str(version or DEFAULT_RELEASE_VERSION).strip() or DEFAULT_RELEASE_VERSION
    asset_value = str(asset_name or DEFAULT_RELEASE_ASSET).strip() or DEFAULT_RELEASE_ASSET
    asset_path = quote(asset_value, safe="")
    if version_value == "latest":
        return f"https://github.com/{repo_value}/releases/latest/download/{asset_path}"
    return f"https://github.com/{repo_value}/releases/download/{quote(version_value, safe='')}/{asset_path}"


def executable_version(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        result = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    text = result.stdout.strip()
    if not text.startswith("aha "):
        return ""
    return text.split()[-1]


def _download_artifact(url: str, output: Path) -> None:
    request = Request(url, headers={"User-Agent": "aha-service-upgrade"})
    with urlopen(request, timeout=120) as response:
        with output.open("wb") as handle:
            shutil.copyfileobj(response, handle)


def _stage_artifact(
    tmp_dir: Path,
    *,
    artifact: Path | None,
    download_url: str,
) -> Path:
    staged = tmp_dir / "aha"
    if artifact is not None:
        if not artifact.is_file():
            raise ServiceUpgradeError(f"artifact does not exist: {artifact}")
        shutil.copy2(artifact, staged)
    else:
        _download_artifact(download_url, staged)
    mode = staged.stat().st_mode
    staged.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return staged


def _replace_executable(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_target = target.parent / f".{target.name}.upgrade-{os.getpid()}"
    try:
        shutil.copy2(source, temporary_target)
        mode = temporary_target.stat().st_mode
        temporary_target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(temporary_target, target)
    finally:
        try:
            temporary_target.unlink()
        except FileNotFoundError:
            pass


def _run_systemctl_restart(service_name: str) -> None:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", service_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceUpgradeError(f"failed to restart {service_name}: {exc}") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        suffix = f": {details}" if details else ""
        raise ServiceUpgradeError(f"failed to restart {service_name}{suffix}")


def upgrade_user_service(
    *,
    bin_path: Path,
    service_name: str = DEFAULT_SERVICE_NAME,
    repo: str = DEFAULT_RELEASE_REPO,
    version: str = DEFAULT_RELEASE_VERSION,
    asset_name: str = DEFAULT_RELEASE_ASSET,
    download_url: str | None = None,
    artifact: Path | None = None,
    restart: bool = True,
    validate: bool = True,
) -> dict:
    target = bin_path.expanduser().resolve()
    normalized_service_name = normalize_service_name(service_name)
    if artifact is not None and download_url:
        raise ServiceUpgradeError("choose only one of artifact or download_url")
    resolved_url = str(download_url or release_asset_url(repo, version, asset_name)).strip()
    if artifact is None and not resolved_url:
        raise ServiceUpgradeError("download URL is empty")

    previous_version = executable_version(target)
    with tempfile.TemporaryDirectory(prefix="aha-service-upgrade-") as tmp:
        staged = _stage_artifact(Path(tmp), artifact=artifact.expanduser().resolve() if artifact else None, download_url=resolved_url)
        installed_version = executable_version(staged) if validate else ""
        if validate and not installed_version:
            raise ServiceUpgradeError("downloaded AHA executable did not report a version with --version")
        _replace_executable(staged, target)

    if validate and not installed_version:
        installed_version = executable_version(target)
    if restart:
        _run_systemctl_restart(normalized_service_name)

    return {
        "bin": str(target),
        "service": normalized_service_name,
        "source": "artifact" if artifact is not None else "download",
        "download_url": "" if artifact is not None else resolved_url,
        "previous_version": previous_version,
        "installed_version": installed_version,
        "restarted": restart,
    }
