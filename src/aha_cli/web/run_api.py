from __future__ import annotations

from pathlib import Path

from aha_cli.backends.registry import agent_backends
from aha_cli.domain.workflow_templates import workflow_template_metadata
from aha_cli.services.app_version import aha_version
from aha_cli.services.task_skills import discover_task_skill_options
from aha_cli.store.config import load_config
from aha_cli.store.filesystem import (
    config_path,
    list_run_summaries,
    list_workspaces,
    run_exists,
    run_summary,
)
from aha_cli.store.task_memos import (
    read_task_memo_summary_cache,
    read_task_memos,
    task_memo_counts,
    write_task_memo_summary_cache,
)
from aha_cli.store.ui_state import read_global_ui_state, read_ui_state
from aha_cli.web.upgrade import web_upgrade_status


class ApiRunNotFound(Exception):
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(run_id)


def safe_download_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)


def archive_upload_suffix(filename: str) -> str:
    name = filename.lower()
    for suffix in (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar"):
        if name.endswith(suffix):
            return suffix
    return ".tar.gz"


def request_run_id(default_run_id: str, query: dict[str, list[str]], payload: dict | None = None) -> str:
    payload_run_id = str((payload or {}).get("run_id", "") or "").strip()
    query_run_id = str(query.get("run_id", [""])[0] or "").strip()
    return payload_run_id or query_run_id or default_run_id


def default_api_run_id(root: Path, default_run_id: str, runs: list[dict] | None = None) -> str:
    if default_run_id and run_exists(root, default_run_id):
        return default_run_id
    summaries = runs if runs is not None else list_run_summaries(root)
    return str(summaries[0]["id"]) if summaries else ""


def require_api_run_id(root: Path, default_run_id: str, query: dict[str, list[str]], payload: dict | None = None) -> str:
    selected_run_id = request_run_id(default_run_id, query, payload)
    if not selected_run_id:
        selected_run_id = default_api_run_id(root, default_run_id)
    if not run_exists(root, selected_run_id):
        raise ApiRunNotFound(selected_run_id)
    return selected_run_id


def configured_workspace_roots(aha_home: Path | None = None, roots: list[Path] | None = None) -> list[Path]:
    if roots is not None:
        return roots
    if aha_home is None:
        return []
    configured = load_config(aha_home).get("workspace_roots", [])
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, list):
        return []
    workspace_roots: list[Path] = []
    for item in configured:
        value = str(item or "").strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = aha_home.parent / path
        workspace_roots.append(path)
    return workspace_roots


def workspace_options(roots: list[Path] | None = None, aha_home: Path | None = None) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    if aha_home is not None:
        for workspace in list_workspaces(aha_home):
            workspace_path = str(workspace["path"])
            seen.add(workspace_path)
            options.append(
                {
                    "id": str(workspace["id"]),
                    "name": str(workspace.get("name") or workspace["id"]),
                    "label": str(workspace.get("name") or workspace["path"]),
                    "path": workspace_path,
                    "root": str(Path(workspace_path).parent),
                    "source": "registry",
                }
            )
    workspace_roots = configured_workspace_roots(aha_home, roots)
    for root in workspace_roots:
        if not root.is_dir():
            continue
        for path in sorted(item for item in root.iterdir() if item.is_dir()):
            if str(path) in seen:
                continue
            seen.add(str(path))
            options.append(
                {
                    "name": path.name,
                    "label": f"{root.name}/{path.name}",
                    "path": str(path),
                    "root": str(root),
                }
            )
    return options


def runs_payload(root: Path, default_run_id: str) -> dict:
    runs = list_run_summaries(root)
    return {
        "default_run_id": default_api_run_id(root, default_run_id, runs),
        "ui_state": read_global_ui_state(root),
        "runs": runs,
    }


def task_memo_summary(root: Path, run_id: str) -> dict:
    base_counts = task_memo_counts([])
    if not run_id or not run_exists(root, run_id):
        return {
            "available": False,
            "run_id": "",
            "last_selected_memo_id": "",
            "counts": base_counts,
        }

    cached_counts = read_task_memo_summary_cache(root, run_id)
    if cached_counts is not None:
        counts = {**base_counts, **cached_counts}
    else:
        memos = read_task_memos(root, run_id)
        counts = task_memo_counts(memos)
        try:
            write_task_memo_summary_cache(root, run_id, memos, source_updated_at="bootstrap")
        except OSError:
            pass

    return {
        "available": True,
        "run_id": run_id,
        "last_selected_memo_id": read_ui_state(root, run_id).get("last_selected_memo_id", ""),
        "counts": counts,
    }


def bootstrap_payload(root: Path, default_run_id: str, cwd: Path | None = None) -> dict:
    cfg = load_config(root)
    runs = list_run_summaries(root)
    selected_run_id = default_api_run_id(root, default_run_id, runs)
    return {
        "aha_home": str(root),
        "aha_version": aha_version(root),
        "web_upgrade": web_upgrade_status(),
        "initialized": config_path(root).exists(),
        "config": cfg,
        "config_backend_options": ["codex", "claude"],
        "default_workspace_path": str(cwd or Path.cwd()),
        "default_run_id": selected_run_id,
        "ui_state": read_global_ui_state(root),
        "runs": runs,
        "memo_summary": task_memo_summary(root, selected_run_id),
        "workspaces": workspace_options(aha_home=root),
        "backends": agent_backends(cfg),
        "workflow_templates": workflow_template_metadata(),
        "skill_options": discover_task_skill_options(root, cwd or Path.cwd()),
    }


def workspaces_payload(root: Path, cwd: Path | None = None, roots: list[Path] | None = None) -> dict:
    workspace_roots = configured_workspace_roots(root, roots)
    return {
        "aha_home": str(root),
        "default_workspace_path": str(cwd or Path.cwd()),
        "root": str(workspace_roots[0]) if workspace_roots else "",
        "roots": [str(root) for root in workspace_roots if root.is_dir()],
        "workspaces": workspace_options(roots=roots, aha_home=root),
    }


def run_export_headers(run_id: str) -> dict[str, str]:
    safe_run_id = safe_download_name(run_id)
    return {"Content-Disposition": f'attachment; filename="aha-run-{safe_run_id}.tar.gz"'}


def run_import_success_payload(root: Path, source_run_id: str, imported_run_id: str) -> dict:
    return {
        "ok": True,
        "source_run_id": source_run_id,
        "imported_run_id": imported_run_id,
        "run": run_summary(root, imported_run_id),
        "runs": list_run_summaries(root),
    }
