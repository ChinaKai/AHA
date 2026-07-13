from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile


SOURCE_UPGRADE_UNAVAILABLE_REASON = "Web upgrade is unavailable for source-started AHA services"
SOURCE_PUBLISH_UNAVAILABLE_REASON = "Web publish is unavailable because AHA_SOURCE_ROOT is not a source checkout"
GIT_COMMAND_TIMEOUT_SECONDS = 120
PUBLISH_REQUEST_TIMEOUT_SECONDS = 300
SEMVER_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def _path_text(path: Path) -> str:
    return str(path.expanduser().resolve())


def _is_zipapp(path: Path) -> bool:
    try:
        return path.is_file() and zipfile.is_zipfile(path)
    except OSError:
        return False


def web_upgrade_status() -> dict:
    source_root = os.environ.get("AHA_SOURCE_ROOT", "").strip()
    if source_root:
        return web_publish_status()

    installed_bin = os.environ.get("AHA_INSTALL_BIN", "").strip()
    if installed_bin:
        installed_path = Path(installed_bin).expanduser()
        if installed_path.is_file():
            return {
                "available": True,
                "action": "upgrade",
                "mode": "installed-onebin",
                "bin": _path_text(installed_path),
            }
        return {
            "available": False,
            "action": "upgrade",
            "mode": "installed-onebin",
            "reason": f"installed AHA executable not found: {installed_path}",
        }

    executable_path = Path(sys.argv[0]).expanduser()
    if _is_zipapp(executable_path):
        return {
            "available": True,
            "action": "upgrade",
            "mode": "current-onebin",
            "bin": _path_text(executable_path),
        }

    return {
        "available": False,
        "action": "upgrade",
        "mode": "source",
        "reason": SOURCE_UPGRADE_UNAVAILABLE_REASON,
    }


def web_upgrade_command() -> list[str]:
    status = web_upgrade_status()
    if not status.get("available") or status.get("action") != "upgrade":
        raise FileNotFoundError(str(status.get("reason") or SOURCE_UPGRADE_UNAVAILABLE_REASON))

    executable = str(status.get("bin") or "").strip()
    installed_bin = os.environ.get("AHA_INSTALL_BIN", "").strip()
    target_bin = _path_text(Path(installed_bin)) if installed_bin else executable
    command = [executable, "service", "upgrade-user", "--bin", target_bin, "--no-health-check", "--json"]
    service_name = os.environ.get("AHA_SERVICE_NAME", "").strip()
    if service_name:
        command.extend(["--service-name", service_name])
    release_url = os.environ.get("AHA_RELEASE_URL", "").strip()
    if release_url:
        command.extend(["--download-url", release_url])
    else:
        for env_name, flag in (
            ("AHA_RELEASE_REPO", "--repo"),
            ("AHA_RELEASE_VERSION", "--version"),
            ("AHA_RELEASE_ASSET", "--asset-name"),
        ):
            value = os.environ.get(env_name, "").strip()
            if value:
                command.extend([flag, value])
    return command


def web_publish_status() -> dict:
    source_root = os.environ.get("AHA_SOURCE_ROOT", "").strip()
    if not source_root:
        return {
            "available": False,
            "action": "publish",
            "mode": "source",
            "reason": SOURCE_PUBLISH_UNAVAILABLE_REASON,
        }
    source_path = Path(source_root).expanduser()
    if not source_path.is_dir():
        return {
            "available": False,
            "action": "publish",
            "mode": "source",
            "reason": f"source checkout not found: {source_path}",
        }
    if not (source_path / ".git").exists():
        return {
            "available": False,
            "action": "publish",
            "mode": "source",
            "reason": f"source checkout is not a git repository: {source_path}",
        }
    return {
        "available": True,
        "action": "publish",
        "mode": "source",
        "source_root": _path_text(source_path),
    }


def web_publish_source_root() -> Path:
    status = web_publish_status()
    if not status.get("available"):
        raise FileNotFoundError(str(status.get("reason") or SOURCE_PUBLISH_UNAVAILABLE_REASON))
    return Path(str(status.get("source_root") or "")).expanduser()


def _run_git(source_root: Path, args: list[str], *, timeout: int = GIT_COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(source_root), *args]
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        output = (exc.stderr or exc.stdout or "").strip()
        detail = f": {output}" if output else ""
        raise RuntimeError(f"git {' '.join(args)} failed{detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout}s") from exc


def _git_output(source_root: Path, args: list[str]) -> str:
    return _run_git(source_root, args).stdout.strip()


def _git_output_or_empty(source_root: Path, args: list[str]) -> str:
    try:
        return _git_output(source_root, args)
    except RuntimeError:
        return ""


def _git_ref_exists(source_root: Path, ref: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--quiet", "--verify", ref],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git rev-parse --quiet --verify {ref} timed out after {GIT_COMMAND_TIMEOUT_SECONDS}s") from exc
    return proc.returncode == 0


def _repo_root(source_root: Path) -> Path:
    output = _git_output(source_root, ["rev-parse", "--show-toplevel"])
    if not output:
        raise RuntimeError(f"source checkout is not a git repository: {source_root}")
    return Path(output).expanduser().resolve()


def _semver_tags(tags_text: str) -> list[tuple[int, int, int, str]]:
    tags: list[tuple[int, int, int, str]] = []
    for line in tags_text.splitlines():
        tag = line.strip()
        match = SEMVER_TAG_RE.fullmatch(tag)
        if match:
            major, minor, patch = (int(part) for part in match.groups())
            tags.append((major, minor, patch, tag))
    return sorted(tags, reverse=True)


def next_release_tag(source_root: Path) -> tuple[str, str]:
    tags_text = _git_output(source_root, ["tag", "--list", "v[0-9]*", "--sort=-v:refname"])
    tags = _semver_tags(tags_text)
    if not tags:
        return "v0.1.0", ""
    major, minor, patch, previous = tags[0]
    return f"v{major}.{minor}.{patch + 1}", previous


def source_publish_preview(*, refresh: bool = True) -> dict:
    source_root = _repo_root(web_publish_source_root())
    fetch_error = ""
    if refresh:
        try:
            _run_git(source_root, ["fetch", "--tags", "origin"], timeout=PUBLISH_REQUEST_TIMEOUT_SECONDS)
        except RuntimeError as exc:
            fetch_error = str(exc)
    next_tag, latest_tag = next_release_tag(source_root)
    status_text = _git_output(source_root, ["status", "--porcelain=v1", "--untracked-files=all"])
    changed_paths = [line[3:].strip() for line in status_text.splitlines() if len(line) > 3]
    branch = _git_output_or_empty(source_root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    upstream = _git_output_or_empty(source_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    ahead = 0
    behind = 0
    if upstream:
        ahead_text = _git_output_or_empty(source_root, ["rev-list", "--count", f"{upstream}..HEAD"])
        behind_text = _git_output_or_empty(source_root, ["rev-list", "--count", f"HEAD..{upstream}"])
        ahead = int(ahead_text) if ahead_text.isdigit() else 0
        behind = int(behind_text) if behind_text.isdigit() else 0
    return {
        "publish": "source-release-preview",
        "source_root": str(source_root),
        "branch": branch,
        "upstream": upstream,
        "dirty": bool(status_text),
        "dirty_count": len(changed_paths),
        "changed_paths": changed_paths[:20],
        "ahead": ahead,
        "behind": behind,
        "latest_tag": latest_tag,
        "next_tag": next_tag,
        "fetch_error": fetch_error,
    }


def publish_source_release(tag: str | None = None) -> dict:
    source_root = _repo_root(web_publish_source_root())
    _run_git(source_root, ["fetch", "--tags", "origin"], timeout=PUBLISH_REQUEST_TIMEOUT_SECONDS)
    default_tag, previous_tag = next_release_tag(source_root)
    tag = str(tag or default_tag).strip()
    if not SEMVER_TAG_RE.fullmatch(tag):
        raise ValueError("release tag must use vX.Y.Z format")
    if _git_ref_exists(source_root, f"refs/tags/{tag}"):
        raise ValueError(f"release tag already exists: {tag}")
    status_text = _git_output(source_root, ["status", "--porcelain=v1", "--untracked-files=all"])
    branch = ""
    try:
        branch = _git_output(source_root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    except RuntimeError:
        branch = ""

    committed = False
    if status_text:
        _run_git(source_root, ["add", "-A"])
        _run_git(
            source_root,
            [
                "-c",
                "user.name=AHA Release",
                "-c",
                "user.email=aha-release@local",
                "commit",
                "-m",
                f"chore(release): publish {tag}",
                "-m",
                "Automated source publish from AHA Web.",
            ],
        )
        committed = True

    commit = _git_output(source_root, ["rev-parse", "HEAD"])
    _run_git(source_root, ["tag", tag])
    pushed: list[str] = []
    if branch:
        _run_git(source_root, ["push", "origin", f"HEAD:{branch}"], timeout=PUBLISH_REQUEST_TIMEOUT_SECONDS)
        pushed.append(branch)
    _run_git(source_root, ["push", "origin", tag], timeout=PUBLISH_REQUEST_TIMEOUT_SECONDS)
    pushed.append(tag)
    return {
        "publish": "source-release",
        "source_root": str(source_root),
        "previous_tag": previous_tag,
        "tag": tag,
        "commit": commit,
        "committed": committed,
        "branch": branch,
        "pushed": pushed,
    }
