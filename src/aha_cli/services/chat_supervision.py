"""Delegated browser control plane for user-facing stewardship decisions.

The host is not a third-party reviewer. It is the delegated ``browser -> main``
control plane for ambiguous semantic decisions after the rules-only steward
hands off with ``semantic_review``. If the host backend fails, AHA falls back
to user confirmation instead of letting the task appear as a generic backend
failure.
"""

from __future__ import annotations

import json
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.backend_runtime import PROCESS_AGENT_BACKENDS, start_backend
from aha_cli.services.chat_offsets import chat_offset_path, save_chat_offset
from aha_cli.services.orchestrator import (
    chat_offset_exists,
    execute_actions,
    extract_action_payload,
    task_has_incomplete_sub_agents,
)
from aha_cli.store.filesystem import (
    append_event,
    append_message,
    ensure_task_supervision_host_agent,
    event_path,
    inbox_path,
    iter_jsonl_reverse,
    mark_task_coordination,
    run_dir,
    set_agent_status,
    set_task_status,
    task_snapshot,
)


SUPERVISION_HOST_DECISIONS = {"ask_user", "continue", "stop", "wait", "route_to_agent", "spawn_sub", "record_task_update"}
SUPERVISION_FAILURE_FALLBACK_STATUS = "awaiting_user"
SUPERVISION_STATUS_CHANNELS = ("main_backend", "host_backend", "steward_decision")
DELEGATED_BROWSER_CONTROL_PLANE_CONTRACT = (
    "Delegated browser control plane contract:\n"
    "- You are the delegated browser->main control plane, not a third-party reviewer.\n"
    "- Steward handles deterministic low-risk continuation; you handle semantic handoff decisions.\n"
    "- Keep the task moving, correct direction, route or wait when needed, and escalate only when required.\n"
    "- Do not replace main's technical implementation judgment or make product decisions for the user.\n"
    "- Do not approve destructive operations, commits, spending, permission changes, or irreversible choices; use ask_user.\n"
    "- Do not expose host/supervision mechanics to main; response must read like the next browser instruction.\n"
)
SUPERVISION_EVENT_TYPES = {
    "main_reported_to_host",
    "host_decision",
    "main_applied_decision",
}
SUPERVISION_HOST_IDENTITY_MARKERS = (
    "host agent",
    "host role=host",
    "task supervision host",
    "supervision host",
    "托管 host",
    "托管host",
    "托管 agent",
    "托管agent",
)


def _mentions_supervision_host_identity(text: object) -> bool:
    lower = str(text or "").lower()
    return any(marker in lower for marker in SUPERVISION_HOST_IDENTITY_MARKERS)


def _target_should_not_see_supervision_host(task: dict | None, target: str) -> bool:
    return bool(task and task_supervision_host_id(task) and not is_task_supervision_host_agent(task, target))


def prompt_event_visible_to_target(event: dict, target: str, task: dict | None = None) -> bool:
    if target != "main" and not target.startswith("sub-"):
        return True
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if _target_should_not_see_supervision_host(task, target):
        if event.get("type") == "agent_message" and _mentions_supervision_host_identity(data.get("text")):
            return False
        if event.get("type") == "message" and _mentions_supervision_host_identity(data.get("message")):
            return False
    if event.get("type") in SUPERVISION_EVENT_TYPES:
        return False
    if event.get("type") == "message":
        if str(data.get("sender") or "").lower() == "aha" or str(data.get("from_agent") or "").lower() == "aha":
            return False
        if str(data.get("target") or "") not in {"main", "browser", "system"}:
            return False
    return True


def task_supervision_host_id(task: dict) -> str | None:
    supervision = task.get("supervision") if isinstance(task.get("supervision"), dict) else {}
    if (
        supervision.get("mode") != "assisted"
        or supervision.get("host_backend") == "stub"
        or not supervision.get("real_agent_enabled")
    ):
        return None
    return str(supervision.get("host_agent_id") or "host")


def is_task_supervision_host_agent(task: dict, agent_id: str | None) -> bool:
    host_agent_id = task_supervision_host_id(task)
    return bool(host_agent_id and agent_id == host_agent_id)


def _is_task_supervision_host_agent_record(task: dict, agent: dict) -> bool:
    if is_task_supervision_host_agent(task, str(agent.get("id") or "")):
        return True
    return str(agent.get("role") or "") == "host" and str(agent.get("created_by") or "") == "supervision"


def agents_visible_to_prompt(task: dict, target: str) -> list[dict]:
    agents = task.get("agents") if isinstance(task.get("agents"), list) else []
    if is_task_supervision_host_agent(task, target):
        return agents
    return [agent for agent in agents if not _is_task_supervision_host_agent_record(task, agent)]


def supervision_host_handoff_notes(root: Path, run_id: str, task_id: str, limit: int = 5) -> list[str]:
    notes: list[str] = []
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        data = event.get("data") or {}
        if data.get("task_id") != task_id:
            continue
        if event.get("type") == "steward_semantic_review_requested":
            reason = str(data.get("reason") or "").strip()
            notes.append(f"steward -> host: semantic_review ({reason or 'no reason'})")
        if len(notes) >= limit:
            break
    return list(reversed(notes))


def supervision_host_context(task: dict, host_notes: list[str] | None = None, handoff_notes: list[str] | None = None) -> str:
    task_context = {
        "id": task.get("id"),
        "title": task.get("title"),
        "description": task.get("description"),
        "status": task.get("status"),
        "workspace_path": task.get("workspace_path"),
        "current_round_id": task.get("current_round_id"),
        "round_sequence": task.get("round_sequence"),
        "supervision": task.get("supervision"),
        "agents": [
            {
                "id": agent.get("id"),
                "role": agent.get("role"),
                "backend": agent.get("backend"),
                "status": agent.get("status"),
            }
            for agent in task.get("agents", [])
        ],
    }
    return (
        "AHA host instructions:\n"
        f"{DELEGATED_BROWSER_CONTROL_PLANE_CONTRACT}"
        "The current message from main is task-main's latest user-facing reply.\n"
        "Talk to task-main as the next browser control message: direct, natural, and focused on the next step.\n"
        "Your response field is inserted as the next user message to task-main.\n"
        "Do not mention host, agent, supervision, proxy, decision, JSON, or delegated control-plane mechanics.\n"
        "Do not restate or praise main's answer; ask for or direct the next concrete step.\n"
        "Use continue only when task-main should do more concrete work.\n"
        "Use wait when task-main or sub-agents are already working and your only next message would be an acknowledgement like OK, waiting, or report when ready.\n"
        "Use stop when task-main's latest reply already completes the user's request, or when your only next message would be to say done/finish.\n"
        "Use ask_user only when continuing would be destructive, commit code, spend money, or require information that is impossible to infer.\n"
        "Inspect context only. Do not modify files or execute state-changing commands.\n"
        "Decide what task-main should do next after its latest user-facing reply.\n\n"
        "Return only one JSON object with this shape:\n"
        '{"decision":"continue","reason":"short runtime reason","response":"natural message for main","actions":[]}\n\n'
        "Allowed decision values: ask_user, continue, wait, stop, route_to_agent, spawn_sub, record_task_update.\n"
        "For continue, the response field is what main sees in Chat. For ask_user or stop, the response field is what the user sees in Chat. For wait, response is recorded only in Runtime and is not routed.\n"
        "Do not include decision/reason labels in response.\n"
        "Use actions only when main should execute concrete AHA actions; otherwise return an empty list.\n\n"
        f"Task context:\n{json.dumps(task_context, ensure_ascii=False, indent=2)}\n\n"
        f"Recent steward handoffs:\n{chr(10).join(handoff_notes or ['(none)'])}\n\n"
        f"Recent browser-to-host notes:\n{chr(10).join(host_notes or ['(none)'])}"
    )


def supervision_host_prompt(
    task: dict,
    main_reply: str,
    host_notes: list[str] | None = None,
    handoff_notes: list[str] | None = None,
) -> str:
    return f"{supervision_host_context(task, host_notes, handoff_notes)}\n\nMain latest reply:\n{main_reply}\n"


def parse_supervision_host_decision(reply: str) -> dict:
    payload = extract_action_payload(reply)
    if not payload:
        return {
            "decision": "ask_user",
            "reason": "host did not return JSON; defaulting to user confirmation",
            "response": reply.strip(),
            "actions": [],
        }
    decision = str(payload.get("decision") or "").strip()
    actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
    if decision not in SUPERVISION_HOST_DECISIONS:
        first_action = next((str(action.get("type") or "") for action in actions if isinstance(action, dict)), "")
        decision = first_action if first_action in SUPERVISION_HOST_DECISIONS else "ask_user"
    return {
        "decision": decision,
        "reason": str(payload.get("reason") or payload.get("response") or "").strip(),
        "response": str(payload.get("response") or "").strip(),
        "actions": actions,
    }


def low_information_supervision_response(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return True
    compact = "".join(normalized.split())
    exact = {
        "ok",
        "okay",
        "好",
        "好的",
        "嗯",
        "嗯嗯",
        "收到",
        "行",
        "可以",
        "等着",
        "好。",
        "好的。",
        "收到。",
    }
    if compact in exact:
        return True
    wait_markers = (
        "等你的汇总",
        "等汇总",
        "等结果",
        "有结果",
        "不用回",
        "不用反复确认",
        "不用再确认",
        "先这样",
        "继续等",
        "waiting",
        "wait for",
        "report when",
    )
    if any(marker in compact for marker in wait_markers):
        return True
    return len(compact) <= 6 and any(marker in compact for marker in ("好", "嗯", "收", "等"))


def supervision_host_decision_count(root: Path, run_id: str, task_id: str, host_agent_id: str = "host") -> int:
    count = 0
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        data = event.get("data") or {}
        if data.get("task_id") != task_id:
            continue
        if event.get("type") == "message" and data.get("target") == "main" and data.get("sender") == "browser":
            display_sender = str(data.get("display_sender") or "").strip()
            from_agent = str(data.get("from_agent") or "").strip()
            if display_sender != host_agent_id and from_agent != host_agent_id:
                break
        if event.get("type") == "host_decision":
            count += 1
    return count


def supervision_host_notes(root: Path, run_id: str, task_id: str, host_agent_id: str, limit: int = 8) -> list[str]:
    path = run_dir(root, run_id) / "tasks" / task_id / "messages.jsonl"
    notes: list[str] = []
    for _offset, item in iter_jsonl_reverse(path) or ():
        if item.get("sender") == "browser" and (item.get("target") == host_agent_id or item.get("to_agent") == host_agent_id):
            message = str(item.get("message") or "").strip()
            if message:
                notes.append(f"browser -> {host_agent_id}: {message}")
                if len(notes) >= limit:
                    break
    return list(reversed(notes))


def apply_supervision_real_host(
    root: Path,
    run_id: str,
    task_id: str | None,
    *,
    source_agent: str,
    reply_text: str,
    cfg: dict,
    run: Path,
) -> dict | None:
    if not task_id or source_agent != "main":
        return None
    try:
        task = task_snapshot(root, run_id, task_id)["task"]
    except KeyError:
        return None
    supervision = task.get("supervision") if isinstance(task.get("supervision"), dict) else {}
    host_backend = str(supervision.get("host_backend") or "stub")
    if (
        supervision.get("mode") != "assisted"
        or host_backend == "stub"
        or host_backend not in PROCESS_AGENT_BACKENDS
        or not supervision.get("real_agent_enabled")
    ):
        return None
    host_agent_id = str(supervision.get("host_agent_id") or "host")
    host_agent = next((agent for agent in task.get("agents", []) if agent.get("id") == host_agent_id), None)
    if host_agent is None or str(host_agent.get("backend") or "") != host_backend:
        ensured = ensure_task_supervision_host_agent(root, run_id, task_id, backend=host_backend)
        task = ensured["task"]
        host_agent = ensured["agent"]
        supervision = task.get("supervision") if isinstance(task.get("supervision"), dict) else supervision
        host_agent_id = str(host_agent.get("id") or "host")
        host_backend = str(host_agent.get("backend") or supervision.get("host_backend") or host_backend)
    append_event(
        root,
        run_id,
        "main_reported_to_host",
        {
            "task_id": task_id,
            "host_backend": host_backend,
            "host_agent_id": host_agent_id,
            "channel": supervision.get("channel") or "main_only",
            "reply_chars": len(reply_text),
        },
    )
    offset_file = chat_offset_path(run, host_agent_id, task_id)
    host_inbox = inbox_path(root, run_id, host_agent_id)
    if not chat_offset_exists(root, run_id, host_agent_id, task_id):
        save_chat_offset(offset_file, host_inbox.stat().st_size if host_inbox.exists() else 0)
    append_message(
        root,
        run_id,
        host_agent_id,
        reply_text,
        sender="main",
        task_id=task_id,
        role="host",
        from_agent="main",
        to_agent=host_agent_id,
        agent_id=host_agent_id,
        display_sender="main",
        display_target=host_agent_id,
    )
    set_task_status(root, run_id, task_id, "running")
    set_agent_status(root, run_id, task_id, host_agent_id, "pending")
    host_backend = str(host_agent.get("backend") or supervision.get("host_backend") or host_backend)
    if host_backend in PROCESS_AGENT_BACKENDS:
        start_backend(
            root,
            run_id,
            host_agent_id,
            backend=host_backend,
            model=host_agent.get("model") or (cfg.get(host_backend, {}) or {}).get("model"),
            sandbox=host_agent.get("sandbox") or "read-only",
            approval=host_agent.get("approval") or "never",
            codex_bin=(cfg.get("codex", {}) or {}).get("bin") or "codex",
            claude_bin=(cfg.get("claude", {}) or {}).get("bin") or "claude",
            from_start=False,
            task_id=task_id,
        )
        return {"routed_to_host": True, "executed": []}
    append_event(
        root,
        run_id,
        "agent_error",
        {
            "source": "supervision-host",
            "target": host_agent_id,
            "task_id": task_id,
            "reason": f"backend {host_backend} does not have a chat process",
        },
    )
    return {"routed_to_host": False, "executed": []}


def apply_supervision_host_decision(
    root: Path,
    run_id: str,
    task_id: str,
    *,
    host_agent_id: str,
    host_reply: str,
    exit_code: int,
) -> dict:
    task = task_snapshot(root, run_id, task_id)["task"]
    supervision = task.get("supervision") if isinstance(task.get("supervision"), dict) else {}
    main_agent = next((agent for agent in task.get("agents", []) if agent.get("id") == "main"), {})
    host_agent = next((agent for agent in task.get("agents", []) if agent.get("id") == host_agent_id), {})
    host_backend = str(host_agent.get("backend") or supervision.get("host_backend") or "stub")
    backend_failed = exit_code != 0
    if backend_failed:
        decision = {
            "decision": "ask_user",
            "reason": "host backend failed; defaulting to user confirmation",
            "response": host_reply.strip(),
            "actions": [],
        }
        executed: list[dict] = []
        append_event(
            root,
            run_id,
            "supervision_host_backend_failed",
            {
                "task_id": task_id,
                "host_backend": host_backend,
                "host_agent_id": host_agent_id,
                "exit_code": exit_code,
                "fallback_status": SUPERVISION_FAILURE_FALLBACK_STATUS,
            },
        )
    else:
        decision = parse_supervision_host_decision(host_reply)
        executed = execute_actions(root, run_id, task_id, host_reply)
    host_chat_message = decision["response"] or decision["reason"] or host_reply.strip()
    if (
        decision["decision"] == "continue"
        and not executed
        and task_has_incomplete_sub_agents(task)
        and low_information_supervision_response(host_chat_message)
    ):
        decision = {
            **decision,
            "decision": "wait",
            "reason": decision["reason"] or "host response is wait-like while sub-agents are still running",
        }
    routed_to_main = False
    routed_to_browser = False
    waiting_for_subagents = False
    route_skipped_reason = ""
    try:
        max_rounds = max(1, int(supervision.get("max_rounds") or 5))
    except (TypeError, ValueError):
        max_rounds = 5
    previous_host_rounds = supervision_host_decision_count(root, run_id, task_id, host_agent_id)
    if decision["decision"] == "wait":
        waiting_for_subagents = task_has_incomplete_sub_agents(task)
        if waiting_for_subagents:
            set_task_status(root, run_id, task_id, "running")
            set_agent_status(root, run_id, task_id, "main", "waiting")
        else:
            route_skipped_reason = "wait requested without incomplete sub-agents"
    elif decision["decision"] == "continue" and host_chat_message and not executed:
        if previous_host_rounds < max_rounds:
            append_message(
                root,
                run_id,
                "main",
                host_chat_message,
                sender="browser",
                task_id=task_id,
                role="main",
                from_agent="browser",
                to_agent="main",
                display_sender=host_agent_id,
                display_target="main",
                agent_id=host_agent_id,
            )
            mark_task_coordination(
                root,
                run_id,
                task_id,
                final_summary_requested_at="",
                final_summary_completed_at="",
                round_summary_requested_at="",
                round_summary_completed_at="",
                followup_started_at=utc_now(),
            )
            set_task_status(root, run_id, task_id, "running")
            set_agent_status(root, run_id, task_id, "main", "pending")
            main_backend = str(main_agent.get("backend") or task.get("preferred_backend") or "codex")
            if main_backend in PROCESS_AGENT_BACKENDS:
                start_backend(
                    root,
                    run_id,
                    "main",
                    backend=main_backend,
                    model=main_agent.get("model") or task.get("preferred_model"),
                    sandbox=main_agent.get("sandbox") or task.get("preferred_sandbox") or "workspace-write",
                    approval=main_agent.get("approval") or task.get("preferred_approval") or "never",
                    from_start=not chat_offset_exists(root, run_id, "main", task_id),
                    task_id=task_id,
                )
            routed_to_main = True
        else:
            route_skipped_reason = "max_rounds reached"
    elif decision["decision"] in {"ask_user", "stop"} and host_chat_message:
        append_message(
            root,
            run_id,
            "browser",
            host_chat_message,
            sender=host_agent_id,
            task_id=task_id,
            role="host",
            from_agent=host_agent_id,
            to_agent="browser",
            agent_id="main",
            display_sender=host_agent_id,
            display_target="browser",
        )
        routed_to_browser = True
    event_payload = {
        "task_id": task_id,
        "host_backend": host_backend,
        "host_agent_id": host_agent_id,
        "decision": decision["decision"],
        "reason": decision["reason"],
        "response": decision["response"],
        "action_count": len(decision["actions"]),
        "executed_action_count": len(executed),
        "exit_code": exit_code,
        "backend_failed": backend_failed,
        "status_channels": list(SUPERVISION_STATUS_CHANNELS),
        "fallback_status": SUPERVISION_FAILURE_FALLBACK_STATUS if backend_failed else "",
        "routed_to_main": routed_to_main,
        "routed_to_browser": routed_to_browser,
        "waiting": waiting_for_subagents,
        "host_round": previous_host_rounds + 1,
        "max_rounds": max_rounds,
    }
    append_event(root, run_id, "host_decision", event_payload)
    effect = (
        "routed_to_main"
        if routed_to_main
        else (
            "actions_executed"
            if executed
            else (
                "await_user"
                if decision["decision"] == "ask_user"
                else (
                    "waiting"
                    if waiting_for_subagents
                    else ("stopped" if decision["decision"] == "stop" and routed_to_browser else (route_skipped_reason or "decision_recorded"))
                )
            )
        )
    )
    append_event(
        root,
        run_id,
        "main_applied_decision",
        {
            "task_id": task_id,
            "decision": decision["decision"],
            "applied": routed_to_main or routed_to_browser or bool(executed) or waiting_for_subagents or decision["decision"] == "ask_user",
            "effect": effect,
            "executed_action_count": len(executed),
            "routed_to_main": routed_to_main,
            "routed_to_browser": routed_to_browser,
            "waiting": waiting_for_subagents,
            "reason": decision["reason"],
        },
    )
    return event_payload | {"executed": executed, "routed_to_main": routed_to_main, "waiting": waiting_for_subagents}


__all__ = [
    "agents_visible_to_prompt",
    "apply_supervision_host_decision",
    "apply_supervision_real_host",
    "is_task_supervision_host_agent",
    "low_information_supervision_response",
    "parse_supervision_host_decision",
    "SUPERVISION_FAILURE_FALLBACK_STATUS",
    "prompt_event_visible_to_target",
    "supervision_host_context",
    "supervision_host_handoff_notes",
    "supervision_host_notes",
    "supervision_host_prompt",
    "task_supervision_host_id",
]
