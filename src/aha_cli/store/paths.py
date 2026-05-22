from __future__ import annotations

import os
from pathlib import Path

from aha_cli.constants import CONFIG_DIR, CONFIG_FILE, EVENTS_FILE, PLAN_FILE, RUNS_DIR, WORKSPACES_DIR

AHA_HOME_ENV = "AHA_HOME"
_EXPLICIT_AHA_HOMES: set[str] = set()


def _normalized_path(path: Path) -> Path:
    return path.expanduser().resolve()


def default_aha_home() -> Path:
    return _normalized_path(Path.home() / CONFIG_DIR)


def mark_aha_home(path: Path) -> Path:
    home = _normalized_path(path)
    _EXPLICIT_AHA_HOMES.add(str(home))
    return home


def aha_home_path(root: Path) -> Path:
    root = _normalized_path(root)
    env_home = os.environ.get(AHA_HOME_ENV)
    if env_home and _normalized_path(Path(env_home)) == root:
        return root
    if str(root) in _EXPLICIT_AHA_HOMES:
        return root
    if root.name == CONFIG_DIR:
        return root
    if (root / CONFIG_FILE).exists() or (root / RUNS_DIR).is_dir():
        return root
    return root / CONFIG_DIR


def find_aha_home(start: Path | None = None, explicit: str | Path | None = None) -> Path:
    if explicit:
        return mark_aha_home(Path(explicit))
    env_home = os.environ.get(AHA_HOME_ENV)
    if env_home:
        return mark_aha_home(Path(env_home))
    current = (start or Path.cwd()).resolve()
    for path in [current, *current.parents]:
        if (path / CONFIG_DIR).is_dir():
            return path / CONFIG_DIR
    return mark_aha_home(default_aha_home())


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for path in [current, *current.parents]:
        if (path / CONFIG_DIR).is_dir():
            return path
    return current


def config_path(root: Path) -> Path:
    return aha_home_path(root) / CONFIG_FILE


def run_dir(root: Path, run_id: str) -> Path:
    return aha_home_path(root) / RUNS_DIR / run_id


def workspaces_dir(root: Path) -> Path:
    return aha_home_path(root) / WORKSPACES_DIR


def plan_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / PLAN_FILE


def event_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / EVENTS_FILE


def inbox_path(root: Path, run_id: str, target: str) -> Path:
    safe_target = target.replace("/", "_")
    return run_dir(root, run_id) / "inbox" / f"{safe_target}.jsonl"


def session_path(root: Path, run_id: str, task_id: str | None, agent_id: str) -> Path:
    if task_id:
        return run_dir(root, run_id) / "tasks" / task_id / "sessions" / f"{agent_id}.json"
    return run_dir(root, run_id) / "sessions" / f"{agent_id}.json"
