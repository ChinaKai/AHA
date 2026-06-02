from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from aha_cli.services.auto_context_compact import start_backend_after_auto_compact
from aha_cli.services.backend_runtime import PROCESS_AGENT_BACKENDS, backend_status
from aha_cli.services.run_diagnostics import diagnose_runs
from aha_cli.store.filesystem import append_event, append_message, run_exists, set_agent_status, set_task_status, task_snapshot
from aha_cli.web.status import consume_agent_recovery_context, recover_stale_running_agents

BackendStatusProvider = Callable[[Path, str, str, str | None], dict]
BackendStarter = Callable[..., dict]


class RunRecoveryError(Exception):
    def __init__(self, message: str, *, reason: str, status_code: str = "400 Bad Request") -> None:
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


def _validate_run_id(root: Path, run_id: str) -> str:
    selected_run_id = str(run_id or "").strip()
    if not selected_run_id or not run_exists(root, selected_run_id):
        raise RunRecoveryError(f"Run not found: {selected_run_id or '-'}", reason="run_not_found", status_code="404 Not Found")
    return selected_run_id


def _matches_filter(candidate: dict, *, run_id: str, task_id: str | None, agent_id: str | None) -> bool:
    return (
        str(candidate.get("run_id") or "") == run_id
        and (not task_id or str(candidate.get("task_id") or "") == task_id)
        and (not agent_id or str(candidate.get("agent_id") or "") == agent_id)
    )


def _recovery_candidates(
    root: Path,
    run_id: str,
    *,
    task_id: str | None,
    agent_id: str | None,
    backend_status_provider: BackendStatusProvider,
) -> list[dict]:
    diagnostic = diagnose_runs(
        root,
        command_runner=lambda _argv: "",
        backend_status_provider=backend_status_provider,
    )
    return [
        candidate
        for candidate in diagnostic.get("stale_running_agents") or []
        if _matches_filter(candidate, run_id=run_id, task_id=task_id, agent_id=agent_id)
    ]


def _restart_plan(root: Path, run_id: str, task_id: str, agent_id: str) -> dict:
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError as exc:
        raise RunRecoveryError(f"Task not found: {task_id}", reason="task_not_found", status_code="404 Not Found") from exc
    agent = next((item for item in task.get("agents", []) if str(item.get("id") or "") == agent_id), None)
    if agent is None:
        raise RunRecoveryError(f"Agent not found: {agent_id}", reason="agent_not_found", status_code="404 Not Found")
    backend = str(agent.get("backend") or task.get("preferred_backend") or "codex")
    restartable = backend in PROCESS_AGENT_BACKENDS
    return {
        "task_id": task_id,
        "agent_id": agent_id,
        "restartable": restartable,
        "backend": backend,
        "model": agent.get("model") or task.get("preferred_model"),
        "sandbox": agent.get("sandbox") or task.get("preferred_sandbox") or "workspace-write",
        "approval": agent.get("approval") or task.get("preferred_approval") or "never",
        "reason": "" if restartable else "backend_has_no_chat_process",
    }


def _restart_plans_for_candidates(root: Path, run_id: str, candidates: list[dict]) -> list[dict]:
    plans: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        task_id = str(candidate.get("task_id") or "")
        agent_id = str(candidate.get("agent_id") or "")
        if not task_id or not agent_id or (task_id, agent_id) in seen:
            continue
        seen.add((task_id, agent_id))
        try:
            plans.append(_restart_plan(root, run_id, task_id, agent_id))
        except RunRecoveryError as exc:
            plans.append(
                {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "restartable": False,
                    "backend": "",
                    "reason": exc.reason,
                }
            )
    return plans


def _recovery_restart_message(agent_id: str, recovery_context: str) -> str:
    lines = [
        f"AHA recovery restart requested for `{agent_id}`.",
        "The previous backend process stopped while this agent was still marked running.",
    ]
    if recovery_context.strip():
        lines.extend(["", recovery_context.strip()])
    lines.extend(
        [
            "",
            "Continue from the current workspace and task state. Inspect actual files, messages, and runtime state before deciding the next action.",
        ]
    )
    return "\n".join(lines)


def _restart_recovered_backend(
    root: Path,
    run_id: str,
    task_id: str,
    agent_id: str,
    *,
    backend_starter: BackendStarter = start_backend_after_auto_compact,
) -> dict:
    plan = _restart_plan(root, run_id, task_id, agent_id)
    if not plan["restartable"]:
        raise RunRecoveryError(
            f"Backend {plan['backend']} cannot be restarted as a chat process",
            reason="backend_not_restartable",
            status_code="409 Conflict",
        )
    recovery_context = consume_agent_recovery_context(root, run_id, task_id, agent_id)
    set_task_status(root, run_id, task_id, "running")
    set_agent_status(root, run_id, task_id, agent_id, "pending")
    task = task_snapshot(root, run_id, task_id)["task"]
    agent = next((item for item in task.get("agents", []) if str(item.get("id") or "") == agent_id), {})
    append_message(
        root,
        run_id,
        agent_id,
        _recovery_restart_message(agent_id, recovery_context),
        sender="aha",
        task_id=task_id,
        role=str(agent.get("role") or ""),
        from_agent="aha",
        to_agent=agent_id,
        reply_target="browser",
        coordination="stale_runtime_recovery_restart",
    )
    append_event(
        root,
        run_id,
        "agent_recovery_restart_requested",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "backend": plan["backend"],
            "model": plan.get("model"),
        },
    )
    try:
        backend = backend_starter(
            root,
            run_id,
            agent_id,
            backend=plan["backend"],
            model=plan.get("model"),
            sandbox=plan["sandbox"],
            approval=plan["approval"],
            from_start=False,
            task_id=task_id,
        )
    except (OSError, ValueError) as exc:
        raise RunRecoveryError(f"Failed to restart recovered backend: {exc}", reason="restart_failed", status_code="500 Internal Server Error") from exc
    return {"plan": plan, "backend": backend}


def run_stale_runtime_recovery(
    root: Path,
    run_id: str,
    *,
    task_id: str | None = None,
    agent_id: str | None = None,
    apply: bool = False,
    restart_backend: bool = False,
    backend_status_provider: BackendStatusProvider | None = None,
    backend_starter: BackendStarter = start_backend_after_auto_compact,
) -> dict:
    selected_run_id = _validate_run_id(root, run_id)
    task_id = str(task_id or "").strip() or None
    agent_id = str(agent_id or "").strip() or None
    backend_status_provider = backend_status_provider or backend_status
    candidates = _recovery_candidates(
        root,
        selected_run_id,
        task_id=task_id,
        agent_id=agent_id,
        backend_status_provider=backend_status_provider,
    )
    result = {
        "run_id": selected_run_id,
        "task_id": task_id or "",
        "agent_id": agent_id or "",
        "dry_run": not apply,
        "apply": apply,
        "candidates": candidates,
        "restart_backend": restart_backend,
        "restart_plans": _restart_plans_for_candidates(root, selected_run_id, candidates),
        "restarted": [],
        "restart_count": 0,
        "recovered": [],
        "recovered_count": 0,
    }
    if not apply:
        return result
    if not task_id or not agent_id:
        raise RunRecoveryError(
            "Apply requires exact --task-id and --agent-id",
            reason="target_required",
            status_code="400 Bad Request",
        )
    if not candidates:
        raise RunRecoveryError(
            "No stopped-backend running agent matched the requested target",
            reason="candidate_not_found",
            status_code="404 Not Found",
        )

    recovery = recover_stale_running_agents(root, selected_run_id, task_id=task_id, target=agent_id)
    if int(recovery.get("recovered_count") or 0) < 1:
        raise RunRecoveryError(
            "Stale runtime candidate changed before recovery",
            reason="candidate_changed",
            status_code="409 Conflict",
        )
    result["recovered"] = recovery.get("recovered") or []
    result["recovered_count"] = recovery.get("recovered_count") or 0
    if restart_backend:
        restarted = [
            _restart_recovered_backend(
                root,
                selected_run_id,
                task_id,
                agent_id,
                backend_starter=backend_starter,
            )
        ]
        result["restarted"] = restarted
        result["restart_count"] = len(restarted)
    return result


def format_stale_runtime_recovery(result: dict) -> str:
    mode = "apply" if result.get("apply") else "dry-run"
    lines = [
        f"AHA stale runtime recovery ({mode})",
        f"run_id: {result['run_id']}",
        f"target: {result.get('task_id') or '-'} / {result.get('agent_id') or '-'}",
        f"candidates: {len(result.get('candidates') or [])}",
    ]
    for candidate in result.get("candidates") or []:
        backend = f" backend={candidate['backend']}" if candidate.get("backend") else ""
        lines.append(
            f"- {candidate['task_id']}/{candidate['agent_id']}: "
            f"{candidate['backend_status']} ({candidate['reason']}){backend}"
        )
    if result.get("apply"):
        lines.append(f"recovered: {result.get('recovered_count') or 0}")
        for item in result.get("recovered") or []:
            lines.append(f"- {item['task_id']}/{item['agent_id']}")
        if result.get("restart_backend"):
            lines.append(f"restarted: {result.get('restart_count') or 0}")
            for item in result.get("restarted") or []:
                plan = item.get("plan") or {}
                backend = item.get("backend") or {}
                lines.append(f"- {plan.get('task_id')}/{plan.get('agent_id')}: {backend.get('status') or '-'}")
    return "\n".join(lines) + "\n"


__all__ = ["RunRecoveryError", "format_stale_runtime_recovery", "run_stale_runtime_recovery"]
