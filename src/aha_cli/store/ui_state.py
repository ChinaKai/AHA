from __future__ import annotations

from pathlib import Path

from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import aha_home_path, run_dir


def global_ui_state_path(root: Path) -> Path:
    return aha_home_path(root) / "ui_state.json"


def normalize_global_ui_state(data: dict | None = None) -> dict:
    source = data or {}
    return {
        "last_selected_run_id": str(source.get("last_selected_run_id") or "").strip(),
    }


def read_global_ui_state(root: Path) -> dict:
    path = global_ui_state_path(root)
    if not path.exists():
        return normalize_global_ui_state()
    return normalize_global_ui_state(read_json(path))


def update_global_ui_state(root: Path, fields: dict) -> dict:
    state = read_global_ui_state(root)
    if "last_selected_run_id" in fields:
        state["last_selected_run_id"] = str(fields.get("last_selected_run_id") or "").strip()
    write_json(global_ui_state_path(root), state)
    return state


def ui_state_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "ui_state.json"


def normalize_ui_state(data: dict | None = None) -> dict:
    source = data or {}
    return {
        "last_selected_task_id": str(source.get("last_selected_task_id") or "").strip(),
    }


def read_ui_state(root: Path, run_id: str) -> dict:
    path = ui_state_path(root, run_id)
    if not path.exists():
        return normalize_ui_state()
    return normalize_ui_state(read_json(path))


def update_ui_state(root: Path, run_id: str, fields: dict) -> dict:
    state = read_ui_state(root, run_id)
    if "last_selected_task_id" in fields:
        state["last_selected_task_id"] = str(fields.get("last_selected_task_id") or "").strip()
    write_json(ui_state_path(root, run_id), state)
    return state
