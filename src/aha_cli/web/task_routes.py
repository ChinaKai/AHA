from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from aha_cli.backends.registry import agent_backend_names
from aha_cli.domain.models import TASK_COLLABORATION_MODES
from aha_cli.services.chat_supervision import apply_supervision_real_host
from aha_cli.services.steward import apply_steward_decision, steward_decision_snapshot
from aha_cli.services.session_compact import compact_reset_backend_session
from aha_cli.services.tasks import create_task_and_dispatch
from aha_cli.store.filesystem import (
    append_event,
    add_agent,
    delete_task,
    load_config,
    read_json,
    require_plan,
    resolve_workspace_path,
    run_dir,
    set_task_hidden,
    task_context_snapshot,
    task_final_snapshot,
    task_log_page,
    task_snapshot,
    update_agent_config,
    update_task_context_management_config,
    update_task_proxy_config,
    update_task_supervision_config,
)
from aha_cli.web.http_utils import parse_json_body, parse_optional_bool
from aha_cli.web.run_api import require_api_run_id
from aha_cli.web.task_actions import (
    handle_send_payload,
    parse_task_context_management_fields,
    parse_task_proxy_fields,
    parse_task_supervision_fields,
    request_task_finalization_with_backend,
    prepare_task_main_autostart,
    start_prepared_backend,
    start_dispatched_task_backend,
)

SANDBOX_OPTIONS = {"read-only", "workspace-write", "danger-full-access"}
APPROVAL_OPTIONS = {"untrusted", "on-failure", "on-request", "never"}


def optional_int_payload(payload: dict, key: str) -> int | None:
    if key not in payload or payload.get(key) in (None, ""):
        return None
    return int(payload.get(key))


def route_result(payload: dict, status: str = "200 OK") -> dict:
    return {"handled": True, "status": status, "payload": payload}


def route_not_handled() -> dict:
    return {"handled": False}


def task_final_view_snapshot(root: Path, run_id: str, task_id: str) -> dict:
    detail = task_final_snapshot(root, run_id, task_id)
    plan = require_plan(root, run_id)
    task = next((item for item in plan.get("tasks", []) if item.get("id") == task_id), None)
    output_name = str((task or {}).get("output_file") or "")
    if not output_name:
        return detail
    output_file = run_dir(root, run_id) / output_name
    output_meta_file = output_file.with_suffix(".meta.json")
    output_meta = read_json(output_meta_file) if output_meta_file.exists() else {}
    if output_file.exists() and output_meta.get("policy") in {"finalize", "journal", "overview"}:
        detail["result"] = output_file.read_text(encoding="utf-8")
        detail["result_meta"] = output_meta
    return detail


def task_detail_payload(root: Path, run_id: str, task_id: str, detail_name: str, query: dict[str, list[str]]) -> dict:
    if detail_name == "logs":
        limit = int(query.get("limit", ["200"])[0] or "200")
        source = query.get("source", ["auto"])[0] or "auto"
        before_values = query.get("before_offset", []) or query.get("before", [])
        try:
            before = int(before_values[0]) if before_values and before_values[0] else None
        except ValueError:
            before = None
        return task_log_page(root, run_id, task_id, limit=limit, before=before, source=source)
    if detail_name == "final":
        return task_final_view_snapshot(root, run_id, task_id)
    if detail_name == "context":
        return task_context_snapshot(root, run_id, task_id)
    if detail_name == "steward":
        return steward_decision_snapshot(root, run_id, task_id)
    if not detail_name:
        return task_snapshot(root, run_id, task_id)
    raise LookupError("task detail not found")


def handle_task_detail_route(root: Path, run_id: str, path: str, query: dict[str, list[str]]) -> dict:
    parts = unquote(path.removeprefix("/api/task/")).split("/", 1)
    task_id = parts[0]
    detail_name = parts[1] if len(parts) > 1 else ""
    try:
        return route_result(task_detail_payload(root, run_id, task_id, detail_name, query))
    except KeyError:
        return route_result({"error": "task not found"}, "404 Not Found")
    except LookupError:
        return route_result({"error": "task detail not found"}, "404 Not Found")


def handle_task_action_route(root: Path, run_id: str, path: str, body: bytes) -> dict:
    parts = path.removeprefix("/api/task/").split("/", 1)
    if len(parts) != 2:
        return route_result({"error": "task action required"}, "400 Bad Request")
    task_id, action = unquote(parts[0]), parts[1]
    try:
        if action == "hide":
            task = set_task_hidden(root, run_id, task_id, True)
        elif action == "restore":
            task = set_task_hidden(root, run_id, task_id, False)
        elif action in {"final", "complete"}:
            final_payload = request_task_finalization_with_backend(root, run_id, task_id, f"/api/task/{task_id}/{action}")
            task = task_snapshot(root, run_id, task_id)["task"]
            return route_result({"ok": True, "task": task, **final_payload})
        elif action in {"reopen", "resume"}:
            from aha_cli.store.filesystem import reopen_task

            task = reopen_task(root, run_id, task_id)
        elif action == "delete":
            task = delete_task(root, run_id, task_id)
        elif action == "proxy":
            task = update_task_proxy_config(root, run_id, task_id, **parse_task_proxy_fields(parse_json_body(body)))
        elif action == "context-management":
            task = update_task_context_management_config(root, run_id, task_id, **parse_task_context_management_fields(parse_json_body(body)))
        elif action == "supervision":
            task = update_task_supervision_config(root, run_id, task_id, **parse_task_supervision_fields(parse_json_body(body)))
        elif action == "session/compact-reset":
            payload = parse_json_body(body)
            agent_id = str(payload.get("agent_id") or payload.get("target") or "main")
            compact_payload = compact_reset_backend_session(
                root,
                run_id,
                task_id,
                agent_id,
                reason=str(payload.get("reason") or "manual"),
                restart=bool(payload.get("restart", True)),
            )
            task = task_snapshot(root, run_id, task_id)["task"]
            return route_result({"ok": True, "task": task, "compact_reset": compact_payload})
        elif action == "steward/apply":
            payload = parse_json_body(body)
            autostart = prepare_task_main_autostart(root, run_id, task_id) if payload.get("autostart", True) else None
            steward_payload = apply_steward_decision(root, run_id, task_id)
            if steward_payload.get("semantic_review"):
                latest_main_reply = next(
                    (
                        str(item.get("message") or "")
                        for item in reversed(steward_payload["snapshot"].get("recent_messages") or [])
                        if item.get("from") == "main" and item.get("to") == "browser"
                    ),
                    "",
                )
                host_result = apply_supervision_real_host(
                    root,
                    run_id,
                    task_id,
                    source_agent="main",
                    reply_text=latest_main_reply,
                    cfg=load_config(root),
                    run=run_dir(root, run_id),
                )
                if host_result:
                    steward_payload = {**steward_payload, "applied": bool(host_result.get("routed_to_host")), "semantic_host": host_result}
                    append_event(root, run_id, "steward_semantic_review_routed", {"task_id": task_id, "routed_to_host": bool(host_result.get("routed_to_host"))})
                else:
                    append_event(root, run_id, "steward_semantic_review_skipped", {"task_id": task_id, "reason": "real supervision host is not configured"})
                    steward_payload = {**steward_payload, "semantic_host": {"routed_to_host": False, "reason": "real supervision host is not configured"}}
            task = task_snapshot(root, run_id, task_id)["task"]
            response = {"ok": True, "task": task, "steward": steward_payload}
            if steward_payload.get("applied") and not steward_payload.get("semantic_review"):
                backend = start_prepared_backend(root, run_id, autostart)
                if backend:
                    response["backend"] = backend
            return route_result(response)
        else:
            return route_result({"error": f"unknown task action: {action}"}, "400 Bad Request")
        return route_result({"ok": True, "task": task})
    except (KeyError, SystemExit, ValueError) as exc:
        return route_result({"error": str(exc)}, "404 Not Found")


def validate_backend_name(name: str) -> str | None:
    if name not in agent_backend_names():
        return f"unknown agent backend: {name}"
    return None


def validate_runtime_options(sandbox: str | None, approval: str | None) -> str | None:
    if sandbox is not None and sandbox not in SANDBOX_OPTIONS:
        return f"unknown sandbox: {sandbox}"
    if approval is not None and approval not in APPROVAL_OPTIONS:
        return f"unknown approval: {approval}"
    return None


def handle_create_task_route(root: Path, run_id: str, payload: dict) -> dict:
    title = str(payload.get("title", "")).strip()
    description = str(payload.get("description", "") or "").strip()
    if not title:
        return route_result({"error": "title cannot be empty"}, "400 Bad Request")
    backend = str(payload.get("backend", "codex") or "codex")
    preferred_sub_backend = str(payload.get("preferred_sub_backend", "") or "") or None
    collaboration_mode = str(payload.get("collaboration_mode", "") or "") or None
    if collaboration_mode and collaboration_mode not in TASK_COLLABORATION_MODES:
        return route_result({"error": f"unknown collaboration mode: {collaboration_mode}"}, "400 Bad Request")
    error = validate_backend_name(backend)
    if error:
        return route_result({"error": error}, "400 Bad Request")
    if preferred_sub_backend is not None:
        error = validate_backend_name(preferred_sub_backend)
        if error:
            return route_result({"error": error}, "400 Bad Request")
    sandbox = str(payload.get("sandbox", "") or "") or None
    approval = str(payload.get("approval", "") or "") or None
    error = validate_runtime_options(sandbox, approval)
    if error:
        return route_result({"error": error}, "400 Bad Request")
    try:
        workspace_path, workspace_id = resolve_workspace_path(
            root,
            workspace_id=str(payload.get("workspace_id", payload.get("workspace", "")) or "") or None,
            workspace_path=str(payload.get("workspace_path", "") or "") or None,
            default=Path.cwd(),
        )
        supervision = None
        if "supervision" in payload:
            if not isinstance(payload.get("supervision"), dict):
                return route_result({"error": "supervision must be an object"}, "400 Bad Request")
            supervision = parse_task_supervision_fields(payload["supervision"])
        dispatch = bool(payload.get("dispatch", True))
        task = create_task_and_dispatch(
            root,
            run_id,
            title,
            backend=backend,
            model=str(payload.get("model", "") or "") or None,
            workspace_path=workspace_path,
            workspace_id=workspace_id,
            sandbox=sandbox,
            approval=approval,
            proxy_enabled=parse_optional_bool(payload.get("proxy_enabled", False), "proxy_enabled"),
            http_proxy=str(payload.get("http_proxy", "") or "") or None,
            https_proxy=str(payload.get("https_proxy", "") or "") or None,
            no_proxy=str(payload.get("no_proxy", "") or "") or None,
            collaboration_mode=collaboration_mode,
            delegation_policy=str(payload.get("delegation_policy", "") or "") or None,
            max_sub_agents=optional_int_payload(payload, "max_sub_agents"),
            preferred_sub_backend=preferred_sub_backend,
            preferred_sub_model=str(payload.get("preferred_sub_model", "") or "") or None,
            description=description,
            supervision=supervision,
            dispatch=dispatch,
        )
    except ValueError as exc:
        return route_result({"error": str(exc)}, "400 Bad Request")
    backend_state = start_dispatched_task_backend(root, run_id, task, dispatch)
    response = {"ok": True, "task": task}
    if backend_state:
        response["backend"] = backend_state
    return route_result(response)


def handle_create_agent_route(root: Path, run_id: str, payload: dict) -> dict:
    task_id = str(payload.get("task_id", "")).strip()
    if not task_id:
        return route_result({"error": "task_id cannot be empty"}, "400 Bad Request")
    backend = str(payload.get("backend", "codex") or "codex")
    error = validate_backend_name(backend)
    if error:
        return route_result({"error": error}, "400 Bad Request")
    sandbox = str(payload.get("sandbox", "") or "") or None
    approval = str(payload.get("approval", "") or "") or None
    error = validate_runtime_options(sandbox, approval)
    if error:
        return route_result({"error": error}, "400 Bad Request")
    proxy_enabled = parse_optional_bool(payload["proxy_enabled"], "proxy_enabled") if "proxy_enabled" in payload else None
    agent = add_agent(
        root,
        run_id,
        task_id,
        backend=backend,
        role=str(payload.get("role", "sub") or "sub"),
        sandbox=sandbox,
        approval=approval,
        proxy_enabled=proxy_enabled,
    )
    return route_result({"ok": True, "agent": agent})


def handle_agent_config_route(root: Path, run_id: str, payload: dict) -> dict:
    task_id = str(payload.get("task_id", "")).strip()
    agent_id = str(payload.get("agent_id", "")).strip()
    sandbox = str(payload.get("sandbox", "") or "") or None
    approval = str(payload.get("approval", "") or "") or None
    proxy_enabled = parse_optional_bool(payload["proxy_enabled"], "proxy_enabled") if "proxy_enabled" in payload else None
    if not task_id or not agent_id:
        return route_result({"error": "task_id and agent_id are required"}, "400 Bad Request")
    error = validate_runtime_options(sandbox, approval)
    if error:
        return route_result({"error": error}, "400 Bad Request")
    try:
        agent = update_agent_config(root, run_id, task_id, agent_id, sandbox=sandbox, approval=approval, proxy_enabled=proxy_enabled)
        return route_result({"ok": True, "agent": agent})
    except SystemExit as exc:
        return route_result({"error": str(exc)}, "404 Not Found")


def handle_task_config_route(root: Path, run_id: str, payload: dict) -> dict:
    task_id = str(payload.get("task_id", "")).strip()
    if not task_id:
        return route_result({"error": "task_id is required"}, "400 Bad Request")
    try:
        if "context_management" in payload and isinstance(payload.get("context_management"), dict):
            task = update_task_context_management_config(root, run_id, task_id, **parse_task_context_management_fields(payload["context_management"]))
        elif "supervision" in payload and isinstance(payload.get("supervision"), dict):
            task = update_task_supervision_config(root, run_id, task_id, **parse_task_supervision_fields(payload["supervision"]))
        else:
            task = update_task_proxy_config(root, run_id, task_id, **parse_task_proxy_fields(payload))
        return route_result({"ok": True, "task": task})
    except (SystemExit, ValueError) as exc:
        return route_result({"error": str(exc)}, "404 Not Found")


def handle_send_route(root: Path, run_id: str, payload: dict) -> dict:
    try:
        return route_result(handle_send_payload(root, run_id, payload))
    except ValueError as exc:
        return route_result({"error": str(exc)}, "400 Bad Request")


def route_task_agent_request(
    root: Path,
    default_run_id: str,
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: bytes,
) -> dict:
    if method in {"GET", "HEAD"} and path.startswith("/api/task/"):
        return handle_task_detail_route(root, require_api_run_id(root, default_run_id, query), path, query)
    if method == "POST" and path.startswith("/api/task/"):
        return handle_task_action_route(root, require_api_run_id(root, default_run_id, query), path, body)
    if method == "POST" and path == "/api/tasks":
        payload = parse_json_body(body)
        return handle_create_task_route(root, require_api_run_id(root, default_run_id, query, payload), payload)
    if method == "POST" and path == "/api/agents":
        payload = parse_json_body(body)
        return handle_create_agent_route(root, require_api_run_id(root, default_run_id, query, payload), payload)
    if method == "POST" and path == "/api/agent-config":
        payload = parse_json_body(body)
        return handle_agent_config_route(root, require_api_run_id(root, default_run_id, query, payload), payload)
    if method == "POST" and path == "/api/task-config":
        payload = parse_json_body(body)
        return handle_task_config_route(root, require_api_run_id(root, default_run_id, query, payload), payload)
    if method == "POST" and path == "/api/send":
        payload = parse_json_body(body)
        return handle_send_route(root, require_api_run_id(root, default_run_id, query, payload), payload)
    return route_not_handled()


__all__ = [
    "handle_agent_config_route",
    "handle_create_agent_route",
    "handle_create_task_route",
    "handle_send_route",
    "handle_task_action_route",
    "handle_task_config_route",
    "handle_task_detail_route",
    "route_task_agent_request",
    "task_detail_payload",
    "task_final_view_snapshot",
]
