from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import normalize_task_context_management
from aha_cli.services.backend_runtime import backend_status, start_backend
from aha_cli.store.filesystem import append_message, task_snapshot
from aha_cli.store.io import read_json
from aha_cli.store.paths import session_path


def compact_reset_backend_session(*args: object, **kwargs: object) -> dict:
    from aha_cli.services.session_compact import compact_reset_backend_session as compact_reset

    return compact_reset(*args, **kwargs)


def backend_session_has_active_id(root: Path, run_id: str, task_id: str, agent_id: str) -> bool:
    path = session_path(root, run_id, task_id, agent_id)
    if not path.exists():
        return False
    try:
        session = read_json(path)
    except (OSError, ValueError):
        return False
    return bool(session.get("backend_session_id"))


def _auto_compact_agent_context(
    root: Path,
    run_id: str,
    task_id: str | None,
    agent_id: str,
    *,
    backend_state: dict | None = None,
    allowed_statuses: set[str],
    stop_backend_before_reset: bool,
    trigger: str,
) -> dict | None:
    task_id = str(task_id or "")
    agent_id = str(agent_id or "main")
    if not task_id or not agent_id:
        return None
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return None
    agent = next((item for item in task.get("agents", []) if str(item.get("id") or "") == agent_id), None)
    if not agent:
        return None
    policy = normalize_task_context_management(task.get("context_management"))
    if not policy.get("auto_compact_enabled"):
        return None
    state = backend_state if backend_state is not None else backend_status(root, run_id, agent_id, task_id=task_id)
    status = str(state.get("status") or "stopped").lower()
    if status not in allowed_statuses:
        return None
    pressure = state.get("context_pressure") if isinstance(state.get("context_pressure"), dict) else {}
    try:
        percent = float(pressure.get("percent"))
    except (TypeError, ValueError):
        return None
    threshold = int(policy.get("auto_compact_threshold_percent") or 75)
    if percent < threshold or not backend_session_has_active_id(root, run_id, task_id, agent_id):
        return None
    try:
        compact_result = compact_reset_backend_session(
            root,
            run_id,
            task_id,
            agent_id,
            reason="large",
            restart=False,
            stop_backend_before_reset=stop_backend_before_reset,
        )
    except (KeyError, ValueError):
        return None
    append_message(
        root,
        run_id,
        "browser",
        (
            f"AHA 已自动整理 `{agent_id}` 的 agent context："
            f"context {percent:.2f}% >= {threshold}%，已 compact/reset backend session。"
            f"Summary: `{compact_result.get('summary_path') or '-'}`"
        ),
        sender="aha",
        task_id=task_id,
        role=str(agent.get("role") or ""),
        from_agent="aha",
        to_agent=agent_id,
        coordination="auto_context_compact",
        display_sender="AHA",
        display_target=agent_id,
    )
    return {
        **compact_result,
        "auto_context_compact": True,
        "trigger": trigger,
        "backend_status": status,
        "context_percent": percent,
        "threshold_percent": threshold,
    }


def auto_compact_agent_context_before_backend_start(
    root: Path,
    run_id: str,
    task_id: str | None,
    agent_id: str,
    *,
    backend_state: dict | None = None,
) -> dict | None:
    return _auto_compact_agent_context(
        root,
        run_id,
        task_id,
        agent_id,
        backend_state=backend_state,
        allowed_statuses={"stopped"},
        stop_backend_before_reset=True,
        trigger="before_backend_start",
    )


def auto_compact_agent_context_after_turn(
    root: Path,
    run_id: str,
    task_id: str | None,
    agent_id: str,
    *,
    backend_state: dict | None = None,
) -> dict | None:
    return _auto_compact_agent_context(
        root,
        run_id,
        task_id,
        agent_id,
        backend_state=backend_state,
        allowed_statuses={"running", "stopped"},
        stop_backend_before_reset=False,
        trigger="turn_end",
    )


def start_backend_after_auto_compact(
    root: Path,
    run_id: str,
    target: str = "main",
    *,
    backend: str = "codex",
    codex_bin: str = "codex",
    claude_bin: str = "claude",
    model: str | None = None,
    sandbox: str = "workspace-write",
    approval: str = "never",
    interval: float = 1.0,
    from_start: bool = False,
    no_json: bool = False,
    extra_args: list[str] | None = None,
    prompt_prefix: str | None = None,
    task_id: str | None = None,
) -> dict:
    auto_compact_agent_context_before_backend_start(root, run_id, task_id, target)
    start_kwargs = {
        "backend": backend,
        "codex_bin": codex_bin,
        "claude_bin": claude_bin,
        "model": model,
        "sandbox": sandbox,
        "approval": approval,
        "interval": interval,
        "from_start": from_start,
        "no_json": no_json,
        "extra_args": extra_args,
        "task_id": task_id,
    }
    if prompt_prefix is not None:
        start_kwargs["prompt_prefix"] = prompt_prefix
    return start_backend(
        root,
        run_id,
        target,
        **start_kwargs,
    )


__all__ = [
    "auto_compact_agent_context_after_turn",
    "auto_compact_agent_context_before_backend_start",
    "backend_session_has_active_id",
    "start_backend_after_auto_compact",
]
