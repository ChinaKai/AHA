from __future__ import annotations

from pathlib import Path

from aha_cli.services.chat_supervision import (
    agents_visible_to_prompt,
    is_task_supervision_host_agent,
    prompt_event_visible_to_target,
    supervision_host_context,
    supervision_host_delta_context,
    supervision_host_handoff_notes,
    supervision_host_notes,
    task_supervision_host_id,
)
from aha_cli.services.commit_policy import commit_message_policy_prompt
from aha_cli.services.hardware_debug import hardware_debug_context_for_prompt
from aha_cli.services.task_skills import task_skills_context_for_prompt
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.event_views import event_agent_refs
from aha_cli.store.filesystem import (
    event_path,
    iter_jsonl_reverse,
    require_plan,
    run_dir,
    status_snapshot,
    task_snapshot,
)


PROMPT_REDACTED_PROXY_FIELDS = {
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "preferred_http_proxy",
    "preferred_https_proxy",
    "preferred_no_proxy",
}
DELTA_PROMPT_SKIP_EVENT_TYPES = {
    "agent_command_finished",
    "agent_command_started",
    "agent_finished",
    "agent_message",
    "agent_prompt_metrics",
    "agent_started",
    "agent_status_changed",
    "agent_thread",
    "agent_usage",
    "task_journal_rendered",
    "task_status_changed",
}
PROMPT_CONVERSATION_CHAIN_LIMIT = 3
PROMPT_CONVERSATION_SCAN_LIMIT = 200
PROMPT_CONVERSATION_MESSAGE_CHAR_LIMIT = 700
PROMPT_CONVERSATION_MIN_MESSAGE_CHAR_LIMIT = 240
PROMPT_RECENT_CONVERSATION_CHAR_BUDGET = 1800
COMMIT_POLICY_INTENT_TERMS = (
    "commit",
    "git commit",
    "revert",
    "merge",
    "cherry-pick",
    "amend",
    "finalize",
    "提交",
    "提交代码",
    "提交改动",
    "回滚",
    "撤销提交",
    "合并",
    "收口",
)
COORDINATION_POLICY_INTENT_TERMS = (
    "spawn_sub",
    "route_to_agent",
    "record_task_update",
    "sub-agent",
    "sub agent",
    "sub-",
    "delegate",
    "delegation",
    "route",
    "routing",
    "parallel",
    "并行",
    "子 agent",
    "子agent",
    "分派",
    "委派",
    "路由",
    "协作",
    "拆分",
)


def model_family_for_guidance(backend: str | None, *model_values: object) -> str | None:
    if str(backend or "").strip().lower() != "claude":
        return None
    model_text = " ".join(str(value or "").strip().lower() for value in model_values if value is not None)
    if "minimax" in model_text or "mini-max" in model_text:
        return "minimax"
    if "kimi" in model_text:
        return "kimi"
    return None


def recent_run_events(root: Path, run_id: str, limit: int) -> list[dict]:
    events: list[dict] = []
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        events.append(event)
        if len(events) >= limit:
            break
    return list(reversed(events))


def _event_task_id(event: dict) -> str | None:
    data = event.get("data")
    if isinstance(data, dict):
        task_id = data.get("task_id")
        if task_id:
            return str(task_id)
    return None


def _prompt_visibility_task(root: Path, run_id: str, task_id: str | None, target: str | None) -> dict | None:
    if not task_id or not target:
        return None
    try:
        tasks = status_snapshot(root, run_id).get("tasks", [])
    except (OSError, ValueError):
        return None
    if not isinstance(tasks, list):
        return None
    return next((task for task in tasks if task.get("id") == task_id), None)


def recent_prompt_events(root: Path, run_id: str, limit: int, task_id: str | None, target: str | None = None) -> list[dict]:
    if not task_id:
        return recent_run_events(root, run_id, limit)
    events: list[dict] = []
    visibility_task = _prompt_visibility_task(root, run_id, task_id, target)
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        if _event_task_id(event) != task_id:
            continue
        if target and target not in event_agent_refs(event):
            continue
        if target and not prompt_event_visible_to_target(event, target, visibility_task):
            continue
        events.append(event)
        if len(events) >= limit:
            break
    return list(reversed(events))


def _is_current_message_event(event: dict, item: dict, target: str) -> bool:
    data = event.get("data")
    if event.get("type") != "message" or not isinstance(data, dict):
        return False
    return (
        data.get("target") == target
        and data.get("sender") == item.get("sender")
        and data.get("message") == item.get("message")
        and data.get("ts") == item.get("ts")
    )


def _message_endpoint(data: dict, *keys: str) -> str:
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def _conversation_message_endpoints(event: dict) -> tuple[str, str]:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    sender = _message_endpoint(data, "display_sender", "from_agent", "sender") or "-"
    target = _message_endpoint(data, "display_target", "to_agent", "target") or "-"
    return sender, target


def _current_message_sender_label(item: dict) -> str:
    sender = _message_endpoint(item, "display_sender", "from_agent", "sender") or "browser"
    recipient = _message_endpoint(item, "display_target", "to_agent", "target")
    if (item.get("display_sender") or item.get("display_target")) and recipient:
        return f"{sender} -> {recipient}"
    return sender


def _intent_text_for_prompt(item: dict) -> str:
    values = [
        item.get("message"),
        item.get("original_command"),
        item.get("command_namespace"),
        item.get("result_policy"),
    ]
    return " ".join(str(value or "") for value in values).lower()


def _has_intent_term(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in text for term in terms)


def _prompt_needs_commit_policy(item: dict, *, is_finalization: bool) -> bool:
    del is_finalization
    return _has_intent_term(_intent_text_for_prompt(item), COMMIT_POLICY_INTENT_TERMS)


def _prompt_needs_coordination_policy(
    task: dict,
    target: str,
    item: dict,
    *,
    commit_policy_needed: bool,
    visible_agents: list[dict],
) -> bool:
    text = _intent_text_for_prompt(item)
    if _has_intent_term(text, COORDINATION_POLICY_INTENT_TERMS):
        return True
    target_is_main = target == "main" or str(item.get("role") or "").strip() == "main"
    visible_sub_agents = [agent for agent in visible_agents if str(agent.get("id") or "").startswith("sub-")]
    if target_is_main and visible_sub_agents and commit_policy_needed:
        return True
    coordination = str(task.get("coordination") or "").lower()
    return target_is_main and bool(visible_sub_agents) and coordination in {"waiting_for_subagents", "subagents_complete"}


def _coordination_policy_for_prompt(needed: bool) -> str:
    if not needed:
        return ""
    return render_prompt_template("backend_coordination_policy_full.md").rstrip()


def _action_contract_for_prompt(needed: bool) -> str:
    if not needed:
        return ""
    return render_prompt_template("backend_action_contract.md").rstrip()


def _agent_context_for_prompt(current_agent: dict, visible_agents: list[dict], needed: bool) -> str:
    if not needed:
        return ""
    return render_prompt_template(
        "backend_agent_context.md",
        current_agent=current_agent,
        visible_agents=visible_agents,
    ).rstrip()


def _commit_policy_for_prompt(
    needed: bool,
    task_id: str,
    target: str,
    backend: str | None,
    model: str | None,
) -> str:
    if not needed:
        return ""
    return render_prompt_template(
        "backend_commit_policy_full.md",
        commit_message_policy=commit_message_policy_prompt(task_id, target, backend=backend, model=model).rstrip(),
    ).rstrip()


def _recovery_context_for_prompt(item: dict) -> str:
    context = str(item.get("recovery_context") or "").strip()
    if not context:
        return ""
    return render_prompt_template("backend_recovery_context.md", context=context).rstrip()


def _host_notes_for_prompt(root: Path, run_id: str, task_id: str, target: str, item: dict) -> list[str]:
    if "- browser_to_host_notes:\n" in str(item.get("message") or ""):
        return []
    return supervision_host_notes(root, run_id, task_id, target)


def _current_message_inlines_browser_to_host_notes(item: dict) -> bool:
    return "- browser_to_host_notes:\n" in str(item.get("message") or "")


def _is_browser_to_target_message(event: dict, target: str) -> bool:
    if event.get("type") != "message":
        return False
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    sender = _message_endpoint(data, "display_sender", "from_agent", "sender")
    recipient = _message_endpoint(data, "display_target", "to_agent", "target")
    return sender == "browser" and recipient == target


def _conversation_message_visible_to_target(event: dict, target: str, task: dict | None) -> bool:
    if event.get("type") != "message":
        return False
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if str(data.get("sender") or "").lower() == "aha" or str(data.get("from_agent") or "").lower() == "aha":
        return False
    sender, recipient = _conversation_message_endpoints(event)
    endpoint_refs = {sender, recipient}
    endpoint_refs.update(str(data.get(key) or "").strip() for key in ("sender", "target", "from_agent", "to_agent", "display_sender", "display_target"))
    endpoint_refs = {ref for ref in endpoint_refs if ref}
    if target in endpoint_refs:
        return True
    return prompt_event_visible_to_target(event, target, task)


def _conversation_internal_evaluation_event(event: dict, task: dict | None) -> bool:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    sender, recipient = _conversation_message_endpoints(event)
    message = str(data.get("message") or "")
    if message.startswith("Supervision exchange to evaluate:\n"):
        return True
    host_agent_id = task_supervision_host_id(task) if task else None
    return bool(host_agent_id and sender == "main" and recipient == host_agent_id)


def _conversation_starts_chain(event: dict) -> bool:
    sender, _recipient = _conversation_message_endpoints(event)
    return sender.lower() == "browser"


def _truncate_for_prompt(value: object, limit: int = PROMPT_CONVERSATION_MESSAGE_CHAR_LIMIT) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    suffix = " " + render_prompt_template("backend_truncated_message_suffix.md").strip()
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def recent_conversation_events(
    root: Path,
    run_id: str,
    chain_limit: int,
    task_id: str | None,
    target: str,
    item: dict,
) -> list[dict]:
    if not task_id:
        return []
    visibility_task = _prompt_visibility_task(root, run_id, task_id, target)
    events: list[dict] = []
    chain_starts = 0
    scanned = 0
    skip_inlined_browser_host_notes = is_task_supervision_host_agent(visibility_task or {}, target) and _current_message_inlines_browser_to_host_notes(item)
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        if _event_task_id(event) != task_id:
            continue
        if event.get("type") != "message":
            continue
        scanned += 1
        if scanned > PROMPT_CONVERSATION_SCAN_LIMIT:
            break
        if _is_current_message_event(event, item, target):
            continue
        if skip_inlined_browser_host_notes and _is_browser_to_target_message(event, target):
            continue
        if not _conversation_message_visible_to_target(event, target, visibility_task):
            continue
        if _conversation_internal_evaluation_event(event, visibility_task):
            continue
        events.append(event)
        if _conversation_starts_chain(event):
            chain_starts += 1
            if chain_starts >= chain_limit:
                break
    return list(reversed(events))


def _conversation_line(event: dict, message_limit: int) -> str:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    sender, recipient = _conversation_message_endpoints(event)
    ts = str(data.get("ts") or event.get("ts") or "-")
    return render_prompt_template(
        "backend_recent_conversation_line.md",
        ts=ts,
        sender=sender,
        recipient=recipient,
        message=_truncate_for_prompt(data.get("message"), message_limit),
    ).rstrip()


def _format_conversation_chains(chains: list[list[dict]], message_limit: int) -> str:
    chain_texts: list[str] = []
    for index, chain in enumerate(chains, 1):
        messages = "\n".join(_conversation_line(event, message_limit) for event in chain)
        chain_texts.append(
            render_prompt_template(
                "backend_recent_conversation_chain.md",
                index=index,
                messages=messages,
            ).rstrip()
        )
    return render_prompt_template(
        "backend_recent_conversation_chains.md",
        chain_count=len(chains),
        chains="\n".join(chain_texts),
    ).rstrip()


def _format_conversation_with_budget(chains: list[list[dict]], budget: int) -> str:
    selected = [list(chain) for chain in chains]
    for message_limit in (PROMPT_CONVERSATION_MESSAGE_CHAR_LIMIT, 480, PROMPT_CONVERSATION_MIN_MESSAGE_CHAR_LIMIT):
        candidate = [list(chain) for chain in selected]
        while candidate:
            text = _format_conversation_chains(candidate, message_limit)
            if len(text) <= budget:
                return text
            if len(candidate) > 1:
                candidate = candidate[1:]
                continue
            if len(candidate[0]) > 2:
                candidate[0] = candidate[0][1:]
                continue
            break
    text = _format_conversation_chains(selected[-1:], PROMPT_CONVERSATION_MIN_MESSAGE_CHAR_LIMIT)
    if len(text) <= budget:
        return text
    suffix = "\n" + render_prompt_template("backend_truncated_budget_suffix.md").strip()
    return text[: max(0, budget - len(suffix))].rstrip() + suffix


def format_recent_conversation(
    events: list[dict],
    chain_limit: int = PROMPT_CONVERSATION_CHAIN_LIMIT,
    budget: int = PROMPT_RECENT_CONVERSATION_CHAR_BUDGET,
) -> str:
    if not events:
        return render_prompt_template("backend_recent_conversation_empty.md").strip()
    chains: list[list[dict]] = []
    current: list[dict] = []
    for event in events:
        if _conversation_starts_chain(event) and current:
            chains.append(current)
            current = []
        current.append(event)
    if current:
        chains.append(current)
    chains = chains[-chain_limit:]
    return _format_conversation_with_budget(chains, budget)


def recent_delta_prompt_events(root: Path, run_id: str, limit: int, task_id: str | None, target: str, item: dict) -> list[dict]:
    events: list[dict] = []
    visibility_task = _prompt_visibility_task(root, run_id, task_id, target)
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)) or ():
        if task_id and _event_task_id(event) != task_id:
            continue
        data = event.get("data")
        if event.get("type") == "agent_finished" and isinstance(data, dict) and data.get("target") == target:
            break
        if target not in event_agent_refs(event):
            continue
        if not prompt_event_visible_to_target(event, target, visibility_task):
            continue
        if event.get("type") in DELTA_PROMPT_SKIP_EVENT_TYPES:
            continue
        if _is_current_message_event(event, item, target):
            continue
        events.append(event)
        if len(events) >= limit:
            break
    return list(reversed(events))


def redact_proxy_fields_for_prompt(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key in PROMPT_REDACTED_PROXY_FIELDS:
                redacted[key] = "<set>" if item else None
            else:
                redacted[key] = redact_proxy_fields_for_prompt(item)
        return redacted
    if isinstance(value, list):
        return [redact_proxy_fields_for_prompt(item) for item in value]
    return value


def _task_counts_for_prompt(tasks: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _agent_summary_for_prompt(agent: dict) -> dict:
    return {
        "id": agent.get("id"),
        "role": agent.get("role"),
        "backend": agent.get("backend"),
        "model": agent.get("model"),
        "sandbox": agent.get("sandbox"),
        "approval": agent.get("approval"),
        "status": agent.get("status"),
        "session_policy": agent.get("session_policy"),
        "session_id": agent.get("session_id"),
        "backend_session_id": agent.get("backend_session_id"),
        "session_status": agent.get("session_status"),
    }


def _agent_constraints_for_prompt(agent: dict | None) -> dict:
    if not isinstance(agent, dict):
        return {}
    keys = (
        "id",
        "role",
        "backend",
        "model",
        "sandbox",
        "approval",
        "status",
        "session_policy",
        "assignment",
        "scope_id",
        "generation",
        "created_reason",
    )
    return {key: agent.get(key) for key in keys if agent.get(key) not in (None, "", [])}


def _task_summary_for_prompt(task: dict, target: str) -> dict:
    agents = task.get("agents") if isinstance(task.get("agents"), list) else []
    visible_agents = agents_visible_to_prompt(task, target)
    current_agent = next((agent for agent in agents if agent.get("id") == target), None)
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "workspace_path": task.get("workspace_path"),
        "preferred_backend": task.get("preferred_backend"),
        "preferred_model": task.get("preferred_model"),
        "preferred_sandbox": task.get("preferred_sandbox"),
        "preferred_approval": task.get("preferred_approval"),
        "preferred_proxy_enabled": bool(task.get("preferred_proxy_enabled")),
        "preferred_http_proxy": task.get("preferred_http_proxy"),
        "preferred_https_proxy": task.get("preferred_https_proxy"),
        "preferred_no_proxy": task.get("preferred_no_proxy"),
        "collaboration_mode": task.get("collaboration_mode"),
        "delegation_policy": task.get("delegation_policy"),
        "max_sub_agents": task.get("max_sub_agents"),
        "status": task.get("status"),
        "exit_code": task.get("exit_code"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "current_round_id": task.get("current_round_id"),
        "round_sequence": task.get("round_sequence"),
        "last_final_round_id": task.get("last_final_round_id"),
        "last_final_at": task.get("last_final_at"),
        "coordination": task.get("coordination"),
        "hidden": bool(task.get("hidden")),
        "current_agent": _agent_summary_for_prompt(current_agent) if current_agent else None,
        "agents_summary": [_agent_summary_for_prompt(agent) for agent in visible_agents],
    }


def prompt_status_snapshot(root: Path, run_id: str, task_id: str | None, target: str) -> dict:
    snapshot = status_snapshot(root, run_id)
    tasks = snapshot.get("tasks") if isinstance(snapshot.get("tasks"), list) else []
    current_task = next((task for task in tasks if task.get("id") == task_id), None) if task_id else None
    compact = {
        "run_id": snapshot.get("run_id"),
        "goal": snapshot.get("goal"),
        "mode": snapshot.get("mode"),
        "updated_at": snapshot.get("updated_at"),
        "aha_root": snapshot.get("aha_root"),
        "main_agent": snapshot.get("main_agent"),
        "proxy": snapshot.get("proxy"),
        "task_counts": _task_counts_for_prompt(tasks),
        "task_total": len(tasks),
        "hidden_task_count": sum(1 for task in tasks if task.get("hidden")),
        "current_task": _task_summary_for_prompt(current_task, target) if current_task else None,
    }
    return redact_proxy_fields_for_prompt(compact)


def prompt_delta_status_snapshot(root: Path, run_id: str, task_id: str | None, target: str) -> dict:
    snapshot = status_snapshot(root, run_id)
    tasks = snapshot.get("tasks") if isinstance(snapshot.get("tasks"), list) else []
    current_task = next((task for task in tasks if task.get("id") == task_id), None) if task_id else None
    current_agent = None
    if current_task:
        agents = current_task.get("agents") if isinstance(current_task.get("agents"), list) else []
        current_agent = next((agent for agent in agents if agent.get("id") == target), None)
    compact_task = None
    if current_task:
        compact_task = {
            "id": current_task.get("id"),
            "title": current_task.get("title"),
            "status": current_task.get("status"),
            "current_round_id": current_task.get("current_round_id"),
            "round_sequence": current_task.get("round_sequence"),
            "last_final_round_id": current_task.get("last_final_round_id"),
            "last_final_at": current_task.get("last_final_at"),
            "hidden": bool(current_task.get("hidden")),
            "current_agent": _agent_summary_for_prompt(current_agent) if current_agent else None,
        }
    compact = {
        "run_id": snapshot.get("run_id"),
        "mode": snapshot.get("mode"),
        "updated_at": snapshot.get("updated_at"),
        "task_counts": _task_counts_for_prompt(tasks),
        "task_total": len(tasks),
        "hidden_task_count": sum(1 for task in tasks if task.get("hidden")),
        "current_task": compact_task,
    }
    return redact_proxy_fields_for_prompt(compact)


def _text_metrics(value) -> dict:
    text = "" if value is None else str(value)
    chars = len(text)
    return {
        "chars": chars,
        "bytes": len(text.encode("utf-8")),
        "lines": text.count("\n") + 1 if text else 0,
        "tokens": max(1, chars // 4),
    }


def _compact_rendered_prompt(text: str) -> str:
    lines: list[str] = []
    blank_count = 0
    for line in text.splitlines():
        if line.strip():
            blank_count = 0
            lines.append(line.rstrip())
            continue
        blank_count += 1
        if blank_count <= 1:
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def _fill_prompt_metrics(
    metrics: dict | None,
    prompt: str,
    *,
    target: str,
    item: dict,
    components: dict,
    is_finalization: bool,
    is_agent_command: bool,
    event_limit: int | None = None,
    prompt_mode: str = "full",
) -> None:
    if metrics is None:
        return
    metrics.clear()
    metrics.update(
        {
            "target": target,
            "task_id": item.get("task_id"),
            "sender": item.get("sender"),
            "is_finalization": is_finalization,
            "is_agent_command": is_agent_command,
            "prompt_mode": prompt_mode,
            "total": _text_metrics(prompt),
            "components": {name: _text_metrics(value) for name, value in components.items()},
        }
    )
    if event_limit is not None:
        metrics["event_limit"] = event_limit


def chat_prompt_with_metrics(
    root: Path,
    run_id: str,
    target: str,
    item: dict,
    prefix: str,
    *,
    backend: str | None = None,
    requested_model: str | None = None,
    resolved_model: str | None = None,
) -> tuple[str, dict]:
    metrics: dict = {}
    prompt = chat_prompt(
        root,
        run_id,
        target,
        item,
        prefix,
        metrics=metrics,
        backend=backend,
        requested_model=requested_model,
        resolved_model=resolved_model,
    )
    return prompt, metrics


def _limit_compact_summary_text(text: str, limit: int = 4800) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    suffix = "\n\n" + render_prompt_template("backend_compact_summary_truncated_suffix.md").strip()
    return stripped[: max(0, limit - len(suffix))].rstrip() + suffix


def compact_summary_context(root: Path, run_id: str, session: dict | None, *, limit_chars: int = 4800) -> str:
    summary_meta = session.get("compact_summary") if isinstance(session, dict) else None
    if not isinstance(summary_meta, dict):
        return ""
    relpath = str(summary_meta.get("path") or "").strip()
    if not relpath:
        return ""
    path = run_dir(root, run_id) / relpath
    if not path.exists():
        return render_prompt_template("backend_compact_summary_missing.md", relpath=relpath)
    text = _limit_compact_summary_text(path.read_text(encoding="utf-8", errors="replace"), limit_chars)
    if not text:
        return ""
    return render_prompt_template("backend_compact_summary_context.md", summary=text)


def chat_prompt(
    root: Path,
    run_id: str,
    target: str,
    item: dict,
    prefix: str,
    *,
    metrics: dict | None = None,
    backend: str | None = None,
    requested_model: str | None = None,
    resolved_model: str | None = None,
) -> str:
    plan = require_plan(root, run_id)
    task_id = item.get("task_id")
    result_policy = item.get("result_policy")
    is_finalization = result_policy == "finalize"
    is_memo_report = result_policy == "memo_report"
    is_result_request = is_finalization or is_memo_report
    is_agent_command = item.get("command_namespace") == "agent"
    task = None
    agent = None
    session = None
    sticky_delta = False
    components: dict = {
        "prefix": prefix,
        "run_goal": plan.get("goal", ""),
        "user_message": item.get("message", ""),
        "recovery_context": _recovery_context_for_prompt(item),
    }
    task_context = ""
    if task_id:
        try:
            detail = task_snapshot(root, run_id, str(task_id))
            task = detail["task"]
            agent = next((entry for entry in task.get("agents", []) if entry.get("id") == target), None)
            session = next((entry for entry in detail.get("sessions", []) if entry.get("agent_id") == target), None)
            if session:
                merged_agent = dict(agent or {})
                merged_agent["session_id"] = session.get("id")
                merged_agent["backend_session_id"] = session.get("backend_session_id")
                merged_agent["session_status"] = session.get("status")
                agent = merged_agent
            sticky_delta = bool(
                not is_result_request
                and (agent or {}).get("session_policy") == "sticky"
                and (agent or {}).get("backend_session_id")
            )
            if is_agent_command:
                command = str(item.get("message", "") or "")
                original_command = str(item.get("original_command", "") or "")
                agent_metadata = render_prompt_template(
                    "backend_agent_metadata.md",
                    task_id=task_id,
                    task_title=task.get("title", ""),
                    agent_id=target,
                    role=(agent or {}).get("role") or item.get("role", ""),
                    backend=(agent or {}).get("backend") or task.get("preferred_backend") or "codex",
                    model=(agent or {}).get("model") or task.get("preferred_model") or "default",
                    workspace=(agent or {}).get("workspace_path") or task.get("workspace_path") or "-",
                    sandbox=(agent or {}).get("sandbox") or task.get("preferred_sandbox") or "-",
                    approval=(agent or {}).get("approval") or task.get("preferred_approval") or "-",
                ).rstrip()
                components.update(
                    {
                        "agent_command": command,
                        "original_agent_command": original_command,
                        "agent_metadata": agent_metadata,
                    }
                )
                prompt = render_prompt_template(
                    "backend_agent_command.md",
                    prefix=prefix,
                    target=target,
                    original_command=original_command or command,
                    command=command,
                    agent_metadata=agent_metadata,
                )
                prompt = _compact_rendered_prompt(prompt)
                _fill_prompt_metrics(
                    metrics,
                    prompt,
                    target=target,
                    item=item,
                    components=components,
                    is_finalization=is_finalization,
                    is_agent_command=is_agent_command,
                )
                return prompt
            final_context = ""
            should_resume_from_compact = bool(
                not is_finalization
                and not is_memo_report
                and session
                and isinstance(session.get("compact_summary"), dict)
                and not session.get("backend_session_id")
            )
            compact_context = (
                compact_summary_context(root, run_id, session)
                if should_resume_from_compact
                else ""
            )
            journal_context = ""
            visible_agents = agents_visible_to_prompt(detail["task"], target)
            policy_backend = backend or (session or {}).get("backend") or (agent or {}).get("backend") or task.get("preferred_backend")
            policy_model = (
                resolved_model
                or requested_model
                or (session or {}).get("resolved_model")
                or (session or {}).get("model")
                or (agent or {}).get("model")
                or task.get("preferred_model")
            )
            commit_policy_needed = False if is_result_request else _prompt_needs_commit_policy(item, is_finalization=is_finalization)
            coordination_policy_needed = (
                False
                if is_result_request
                else _prompt_needs_coordination_policy(
                    detail["task"],
                    target,
                    item,
                    commit_policy_needed=commit_policy_needed,
                    visible_agents=visible_agents,
                )
            )
            commit_policy = _commit_policy_for_prompt(
                commit_policy_needed,
                str(task_id),
                target,
                policy_backend,
                policy_model,
            )
            coordination_policy = _coordination_policy_for_prompt(coordination_policy_needed)
            action_contract = _action_contract_for_prompt(coordination_policy_needed)
            visible_agent_constraints = [_agent_constraints_for_prompt(entry) for entry in visible_agents]
            current_agent_constraints = _agent_constraints_for_prompt(agent)
            agent_context = _agent_context_for_prompt(
                current_agent_constraints,
                visible_agent_constraints,
                coordination_policy_needed or commit_policy_needed,
            )
            components.update(
                {
                    "agent_context": agent_context,
                    "task_journal": journal_context,
                    "action_contract": action_contract,
                    "commit_policy": commit_policy,
                    "coordination_policy": coordination_policy,
                    "compact_summary": compact_context,
                }
            )
            if is_result_request:
                task_context = render_prompt_template(
                    "backend_task_context_minimal.md",
                    task_id=task_id,
                    title=detail["task"].get("title", ""),
                    status=detail["task"].get("status", ""),
                    role=item.get("role", ""),
                    workspace=detail["task"].get("workspace_path", ""),
                )
            else:
                task_context = render_prompt_template(
                    "backend_task_context.md",
                    task_id=task_id,
                    title=detail["task"].get("title", ""),
                    description=detail["task"].get("description", ""),
                    status=detail["task"].get("status", ""),
                    role=item.get("role", ""),
                    workspace=detail["task"].get("workspace_path", ""),
                    collaboration_mode=detail["task"].get("collaboration_mode", "auto"),
                    workflow_template=detail["task"].get("workflow_template", "auto"),
                    delegation_policy=detail["task"].get("delegation_policy", "auto"),
                    max_sub_agents=detail["task"].get("max_sub_agents", 0),
                    preferred_sub_backend=(
                        detail["task"].get("preferred_sub_backend")
                        or detail["task"].get("preferred_backend")
                        or "codex"
                    ),
                    preferred_sub_model=(
                        detail["task"].get("preferred_sub_model")
                        or detail["task"].get("preferred_model")
                        or "default"
                    ),
                    current_agent=current_agent_constraints,
                    agents=visible_agent_constraints,
                    agent_context=agent_context,
                    task_skills_context=task_skills_context_for_prompt(detail["task"]).rstrip(),
                    hardware_debug_context=hardware_debug_context_for_prompt(detail["task"]).rstrip(),
                    final_context=final_context.rstrip(),
                    task_journal=journal_context,
                    compact_summary=compact_context.rstrip(),
                    action_contract=action_contract,
                    coordination_policy=coordination_policy,
                    commit_policy=commit_policy,
                )
            if not sticky_delta and is_task_supervision_host_agent(detail["task"], target):
                host_notes = _host_notes_for_prompt(root, run_id, str(task_id), target, item)
                supervision_context = supervision_host_context(
                    detail["task"],
                    host_notes,
                    supervision_host_handoff_notes(root, run_id, str(task_id)),
                )
                task_context = f"{task_context.rstrip()}\n\n{supervision_context}\n"
                components["supervision_host_context"] = supervision_context
            components["task_context"] = task_context
        except KeyError:
            task_context = render_prompt_template("backend_task_context_missing.md", task_id=task_id)
            components["task_context"] = task_context
    prompt_backend = (
        backend
        or (session or {}).get("backend")
        or (agent or {}).get("backend")
        or (task or {}).get("preferred_backend")
    )
    prompt_requested_model = (
        requested_model
        or (session or {}).get("requested_model")
        or (agent or {}).get("model")
        or (task or {}).get("preferred_model")
    )
    prompt_resolved_model = (
        resolved_model
        or (session or {}).get("resolved_model")
        or (session or {}).get("model")
        or (agent or {}).get("model")
        or (task or {}).get("preferred_model")
    )
    if is_finalization:
        mode_template = "mode_instruction_final.md"
    elif is_memo_report:
        mode_template = "mode_instruction_memo_report.md"
    else:
        mode_template = "mode_instruction_default.md"
    mode_instruction = render_prompt_template(mode_template).strip()
    event_limit = 0 if is_result_request else PROMPT_CONVERSATION_CHAIN_LIMIT
    conversation_chain_limit = event_limit
    if sticky_delta:
        is_supervision_host = bool(task and is_task_supervision_host_agent(task, target))
        needs_conversation_events = is_supervision_host
        has_special_sticky_context = False
        event_limit = conversation_chain_limit if needs_conversation_events else 0
        conversation_events = (
            recent_conversation_events(
                root,
                run_id,
                conversation_chain_limit,
                str(task_id) if task_id else None,
                target,
                item,
            )
            if needs_conversation_events
            else []
        )
        recent_conversation = (
            format_recent_conversation(conversation_events, conversation_chain_limit)
            if is_supervision_host
            else ""
        )
        sticky_context = render_prompt_template(
            "backend_sticky_context.md",
            task_id=task_id,
            title=(task or {}).get("title", ""),
            status=(task or {}).get("status", ""),
            agent_id=target,
            role=item.get("role", ""),
            backend=(agent or {}).get("backend") or (task or {}).get("preferred_backend") or "codex",
            workspace=(agent or {}).get("workspace_path") or (task or {}).get("workspace_path") or "-",
            collaboration_mode=(task or {}).get("collaboration_mode") or "auto",
            workflow_template=(task or {}).get("workflow_template") or "auto",
            delegation_policy=(task or {}).get("delegation_policy") or "auto",
            max_sub_agents=(task or {}).get("max_sub_agents") if task else "-",
            sandbox=(agent or {}).get("sandbox") or (task or {}).get("preferred_sandbox") or "-",
            approval=(agent or {}).get("approval") or (task or {}).get("preferred_approval") or "-",
            session_policy=(agent or {}).get("session_policy") or "-",
            backend_session_id=(agent or {}).get("backend_session_id") or "-",
            task_skills_context=task_skills_context_for_prompt(task or {}).rstrip(),
            hardware_debug_context=hardware_debug_context_for_prompt(task or {}).rstrip(),
        )
        if is_supervision_host and task:
            host_notes = _host_notes_for_prompt(root, run_id, str(task_id), target, item)
            supervision_context = supervision_host_delta_context(
                task,
                host_notes,
                supervision_host_handoff_notes(root, run_id, str(task_id)),
            )
            sticky_context = f"{sticky_context.rstrip()}\n\n{supervision_context}\n"
            components["supervision_host_delta_context"] = supervision_context
            has_special_sticky_context = True
            if recent_conversation:
                supervision_conversation = render_prompt_template(
                    "backend_recent_supervision_conversation.md",
                    recent_conversation=recent_conversation,
                ).rstrip()
                sticky_context = f"{sticky_context.rstrip()}\n\n{supervision_conversation}\n"
                components["recent_conversation"] = recent_conversation
        sticky_agent_context = str(components.get("agent_context") or "").strip()
        sticky_commit_policy = str(components.get("commit_policy") or "").strip()
        sticky_coordination_policy = str(components.get("coordination_policy") or "").strip()
        for stale_component in (
            "task_context",
            "task_journal",
            "supervision_host_context",
            "run_goal",
        ):
            components.pop(stale_component, None)
        if sticky_agent_context:
            sticky_context = f"{sticky_context.rstrip()}\n\n{sticky_agent_context}\n"
            components["agent_context"] = sticky_agent_context
            has_special_sticky_context = True
        else:
            components.pop("agent_context", None)
        if sticky_coordination_policy:
            sticky_context = f"{sticky_context.rstrip()}\n\n{sticky_coordination_policy}\n"
            components["coordination_policy"] = sticky_coordination_policy
            has_special_sticky_context = True
        else:
            components.pop("coordination_policy", None)
        if sticky_commit_policy:
            sticky_context = f"{sticky_context.rstrip()}\n\n{sticky_commit_policy}\n"
            components["commit_policy"] = sticky_commit_policy
            has_special_sticky_context = True
        else:
            components.pop("commit_policy", None)
        components.update(
            {
                "sticky_context": sticky_context,
            }
        )
        prompt = render_prompt_template(
            "backend_chat_delta.md",
            prefix=prefix,
            target=target,
            mode_instruction=mode_instruction,
            run_goal=plan["goal"],
            sticky_context=sticky_context.rstrip(),
            recent_conversation=recent_conversation,
            recovery_context=components["recovery_context"],
            sender=_current_message_sender_label(item),
            ts=item.get("ts", ""),
            message=item.get("message", ""),
        )
        prompt = _compact_rendered_prompt(prompt)
        _fill_prompt_metrics(
            metrics,
            prompt,
            target=target,
            item=item,
            components=components,
            is_finalization=is_finalization,
            is_agent_command=is_agent_command,
            event_limit=event_limit,
            prompt_mode="sticky_delta",
        )
        return prompt
    if is_result_request:
        recent_conversation = render_prompt_template("backend_result_conversation_omitted.md").strip()
    else:
        conversation_events = recent_conversation_events(
            root,
            run_id,
            conversation_chain_limit,
            str(task_id) if task_id else None,
            target,
            item,
        )
        recent_conversation = format_recent_conversation(conversation_events, conversation_chain_limit)
    empty_task_context = render_prompt_template("backend_task_context_none.md").strip()
    components.update(
        {
            "mode_instruction": mode_instruction,
            "recent_conversation": recent_conversation,
            "task_context": task_context or empty_task_context,
        }
    )
    prompt = render_prompt_template(
        "backend_chat_full.md",
        prefix=prefix,
        target=target,
        mode_instruction=mode_instruction,
        run_goal=plan["goal"],
        task_context=task_context or empty_task_context,
        recent_conversation=recent_conversation,
        recovery_context=components["recovery_context"],
        sender=_current_message_sender_label(item),
        ts=item.get("ts", ""),
        message=item.get("message", ""),
    )
    prompt = _compact_rendered_prompt(prompt)
    _fill_prompt_metrics(
        metrics,
        prompt,
        target=target,
        item=item,
        components=components,
        is_finalization=is_finalization,
        is_agent_command=is_agent_command,
        event_limit=event_limit,
    )
    return prompt


__all__ = [
    "chat_prompt",
    "chat_prompt_with_metrics",
    "compact_summary_context",
    "format_recent_conversation",
    "prompt_delta_status_snapshot",
    "prompt_status_snapshot",
    "recent_conversation_events",
    "recent_delta_prompt_events",
    "recent_prompt_events",
    "recent_run_events",
    "redact_proxy_fields_for_prompt",
]
