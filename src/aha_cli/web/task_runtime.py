from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.auto_context_compact import start_backend_after_auto_compact as start_backend
from aha_cli.services.backend_runtime import PROCESS_AGENT_BACKENDS, backend_status
from aha_cli.services.chat import chat_offset_path, save_chat_offset
from aha_cli.store.filesystem import (
    append_event,
    append_message,
    inbox_path,
    list_task_rounds,
    mark_task_coordination,
    run_dir,
    set_task_status,
    task_snapshot,
)
from aha_cli.store.config import load_config
from aha_cli.store.knowledge import NAVIGATION_SLUG, entry_path_for, knowledge_config, project_key
from aha_cli.store.runs import require_plan
from aha_cli.web.status import TERMINAL_TASK_STATUSES, invalidate_backend_status_cache
from aha_cli.web.task_command_format import (
    finalization_prompt,
    format_knowledge_feedback_context_for_prompt,
    format_task_journal_for_prompt,
)


def is_task_supervision_host_target(task: dict, target_id: str | None) -> bool:
    supervision = task.get("supervision") if isinstance(task.get("supervision"), dict) else {}
    host_agent_id = str(supervision.get("host_agent_id") or "host")
    return bool(
        target_id
        and target_id == host_agent_id
        and supervision.get("mode") == "assisted"
        and supervision.get("real_agent_enabled")
        and supervision.get("host_backend") != "stub"
    )


def save_chat_offset_after_message(root: Path, run_id: str, task_id: str, target_id: str) -> None:
    inbox = inbox_path(root, run_id, target_id)
    offset_file = chat_offset_path(run_dir(root, run_id), target_id, task_id)
    save_chat_offset(offset_file, inbox.stat().st_size if inbox.exists() else 0)


def ensure_chat_offset_before_message(root: Path, run_id: str, task_id: str, target_id: str) -> None:
    offset_file = chat_offset_path(run_dir(root, run_id), target_id, task_id)
    if offset_file.exists():
        return
    inbox = inbox_path(root, run_id, target_id)
    save_chat_offset(offset_file, inbox.stat().st_size if inbox.exists() else 0)


def message_backend_autostart_config(root: Path, run_id: str, task_id: str | None, target_id: str) -> dict | None:
    if not task_id or not target_id:
        return None
    try:
        detail = task_snapshot(root, run_id, task_id)
    except KeyError:
        return None
    task = detail["task"]
    if is_task_supervision_host_target(task, target_id):
        return None
    agent = next((item for item in task.get("agents", []) if item.get("id") == target_id), None)
    if not agent:
        return None
    backend = str(agent.get("backend") or task.get("preferred_backend") or "codex")
    if backend not in PROCESS_AGENT_BACKENDS:
        return None
    state = backend_status(root, run_id, target_id, task_id=task_id)
    if state.get("status") != "stopped":
        return None
    return {
        "backend": backend,
        "target": target_id,
        "task_id": task_id,
        "model": agent.get("model") or task.get("preferred_model"),
        "sandbox": agent.get("sandbox") or task.get("preferred_sandbox") or "workspace-write",
        "approval": agent.get("approval") or task.get("preferred_approval") or "never",
    }


def prepare_task_main_autostart(root: Path, run_id: str, task_id: str | None) -> dict | None:
    if not task_id:
        return None
    autostart = message_backend_autostart_config(root, run_id, task_id, "main")
    if autostart:
        ensure_chat_offset_before_message(root, run_id, task_id, "main")
    return autostart


def start_prepared_backend(root: Path, run_id: str, autostart: dict | None) -> dict | None:
    if not autostart:
        return None
    backend = start_backend(
        root,
        run_id,
        autostart["target"],
        backend=autostart["backend"],
        model=autostart["model"],
        sandbox=autostart["sandbox"],
        approval=autostart["approval"],
        from_start=False,
        task_id=autostart["task_id"],
    )
    invalidate_backend_status_cache(root, run_id, autostart["target"], autostart["task_id"])
    return backend


def finalization_context_for_task(task: dict, rounds: list[dict], requested_at: str) -> dict:
    journal_ids = [str(item.get("journal_id")) for item in rounds if item.get("journal_id")]
    round_ids: list[str] = []
    for item in rounds:
        round_id = str(item.get("round_id") or "")
        if round_id and round_id not in round_ids:
            round_ids.append(round_id)
    return {
        "source": "task_journal",
        "from_at": task.get("created_at") or task.get("started_at") or "",
        "to_at": requested_at,
        "journal_count": len(rounds),
        "journal_ids": journal_ids,
        "round_ids": round_ids,
    }


def knowledge_feedback_context_for_task(root: Path, run_id: str, task: dict) -> str:
    cfg = load_config(root)
    kb_cfg = knowledge_config(cfg)
    nav_cfg = kb_cfg.get("project_nav") if isinstance(kb_cfg.get("project_nav"), dict) else {}
    workspace_path = str(task.get("workspace_path") or "")
    goal = ""
    try:
        goal = str(require_plan(root, run_id).get("goal") or "")
    except SystemExit:
        goal = ""
    project_key_value = project_key(Path(workspace_path), goal=goal) if workspace_path else ""
    return format_knowledge_feedback_context_for_prompt(
        {
            "knowledge_enabled": bool(kb_cfg.get("enabled")),
            "project_nav_enabled": bool(nav_cfg.get("enabled", True)),
            "project_nav_index_exists": bool(
                project_key_value
                and entry_path_for(root, cfg, "project", "navigation", project_key_value, NAVIGATION_SLUG)
            ),
            "project_key": project_key_value,
            "workspace_path": workspace_path,
        }
    )


def request_task_finalization(root: Path, run_id: str, task_id: str | None, command: str) -> str:
    if not task_id:
        return "No task is selected."
    try:
        detail = task_snapshot(root, run_id, task_id)
    except KeyError:
        return f"Task not found: {task_id}"
    task = detail["task"]
    rounds = list_task_rounds(root, run_id, task_id)
    requested_at = utc_now()
    final_context = finalization_context_for_task(task, rounds, requested_at)
    mark_task_coordination(root, run_id, task_id, final_summary_requested_at=requested_at, final_summary_completed_at="")
    if task.get("status") not in TERMINAL_TASK_STATUSES:
        set_task_status(root, run_id, task_id, "running")
    append_message(
        root,
        run_id,
        "main",
        finalization_prompt(
            task_id,
            str(task.get("title", "")),
            rounds,
            final_context,
            knowledge_feedback_context_for_task(root, run_id, task),
        ),
        sender="aha",
        task_id=task_id,
        role="main",
        from_agent="aha",
        to_agent="main",
        command_namespace="aha",
        original_command=command,
        result_policy="finalize",
        final_context=final_context,
    )
    append_event(
        root,
        run_id,
        "task_final_requested",
        {"task_id": task_id, "target": "main", "policy": "finalize", "journal_count": len(rounds), "requested_at": requested_at},
    )
    return f"Finalization requested for {task_id}. Task-main will write the Final when it finishes."


def request_task_finalization_with_backend(
    root: Path,
    run_id: str,
    task_id: str | None,
    command: str,
    *,
    autostart_backend: bool = True,
) -> dict:
    autostart = prepare_task_main_autostart(root, run_id, task_id) if autostart_backend else None
    message = request_task_finalization(root, run_id, task_id, command)
    payload: dict = {"message": message}
    backend = start_prepared_backend(root, run_id, autostart)
    if backend:
        payload["backend"] = backend
    return payload


def start_dispatched_task_backend(root: Path, run_id: str, task: dict, dispatch: bool) -> dict | None:
    if not dispatch:
        return None
    task_id = str(task.get("id") or "")
    autostart = message_backend_autostart_config(root, run_id, task_id, "main")
    if not autostart:
        return None
    backend = start_backend(
        root,
        run_id,
        "main",
        backend=autostart["backend"],
        model=autostart["model"],
        sandbox=autostart["sandbox"],
        approval=autostart["approval"],
        from_start=True,
        task_id=task_id,
    )
    invalidate_backend_status_cache(root, run_id, "main", task_id)
    return backend
