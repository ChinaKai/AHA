from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from aha_cli.backends.registry import agent_backend_names
from aha_cli.services.agent_backend_switch import restart_agent_backend, switch_agent_backend
from aha_cli.services.chat_supervision import apply_supervision_real_host
from aha_cli.services.hardware_io import append_hardware_io_record
from aha_cli.services.hardware_bridge import (
    append_bridge_control,
    bridge_status,
    device_stream_page,
    ensure_bridge,
    task_devices,
)

_TERMINAL_TASK_STATUSES = {"completed", "failed", "blocked"}


def _resolve_task_device(task: dict) -> tuple[str | None, int]:
    devices = task_devices(task)
    return (devices[0] if devices else (None, 115200))


def _task_is_terminal(task: dict) -> bool:
    return bool(task.get("deleted_at")) or str(task.get("status")) in _TERMINAL_TASK_STATUSES


def _hardware_stream_payload(root: Path, run_id: str, task_id: str, *, after: int | None, limit: int) -> dict:
    task = task_snapshot(root, run_id, task_id)["task"]
    device, baudrate = _resolve_task_device(task)
    read_only = _task_is_terminal(task)
    if not device:
        return {"events": [], "after_offset": after or 0, "has_more": False, "device": None, "read_only": read_only, "bridge": None}
    # Opening the live console on an active task lazily brings the device bridge up.
    if not read_only:
        try:
            ensure_bridge(root, device, baudrate)
        except Exception:
            pass
    page = device_stream_page(root, device, after=after, limit=limit)
    page["device"] = device
    page["read_only"] = read_only
    page["bridge"] = bridge_status(root, device)
    return page
from aha_cli.services.proxy import backend_proxy_config
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
    set_agent_status,
    set_task_hidden,
    task_context_snapshot,
    task_final_snapshot,
    task_log_page,
    task_snapshot,
    update_agent_config,
    update_task_context_management_config,
    update_task_hardware_debug_config,
    update_task_proxy_config,
    update_task_skills_config,
    update_task_supervision_config,
)
from aha_cli.store.task_memos import (
    create_task_memo,
    delete_task_memo,
    read_task_memos,
    update_task_memo,
)
from aha_cli.store.task_memo_assets import (
    TASK_MEMO_ASSET_DIR,
    create_task_memo_asset,
    create_task_memo_asset_from_bytes,
    read_task_memo_asset,
    task_memo_assets_dir,
)
from aha_cli.web.execution_fields import parse_execution_fields
from aha_cli.web.http_utils import parse_json_body, parse_multipart_form, parse_optional_bool
from aha_cli.web.run_api import require_api_run_id
from aha_cli.web.status import recover_stale_running_agents
from aha_cli.web.task_actions import (
    complete_selected_task,
    handle_send_payload,
    parse_task_context_management_fields,
    parse_task_hardware_debug_fields,
    parse_task_proxy_fields,
    parse_task_skills_fields,
    parse_task_supervision_fields,
    request_task_finalization_with_backend,
    prepare_task_main_autostart,
    start_prepared_backend,
    start_dispatched_task_backend,
)
from aha_cli.web.task_runtime import request_memo_completion_report_with_backend

SANDBOX_OPTIONS = {"read-only", "workspace-write", "danger-full-access"}
APPROVAL_OPTIONS = {"untrusted", "on-failure", "on-request", "never"}


def route_result(payload: dict, status: str = "200 OK") -> dict:
    return {"handled": True, "status": status, "payload": payload}


def binary_route_result(body: bytes, content_type: str, status: str = "200 OK", headers: dict[str, str] | None = None) -> dict:
    return {"handled": True, "status": status, "body": body, "content_type": content_type, "headers": headers or {}}


def route_not_handled() -> dict:
    return {"handled": False}


def task_description_with_memo_attachment_context(root: Path, run_id: str, description: str, source_memo_id: str) -> str:
    if not source_memo_id or f"{TASK_MEMO_ASSET_DIR}/" not in description:
        return description
    if "AHA memo attachment resolution:" in description:
        return description
    attachment_dir = task_memo_assets_dir(root, run_id).resolve()
    note = "\n".join(
        [
            "AHA memo attachment resolution:",
            f"- Markdown links beginning with `{TASK_MEMO_ASSET_DIR}/` refer to files under this run attachment directory:",
            f"  `{attachment_dir}`",
            f"- Example: `{TASK_MEMO_ASSET_DIR}/ab/file.png` should be opened as `{attachment_dir}/ab/file.png`.",
            "- These files are outside the task workspace; do not search for them relative to the workspace.",
        ]
    )
    return f"{description.rstrip()}\n\n{note}"


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
    if detail_name == "hardware-io":
        limit = int(query.get("limit", ["500"])[0] or "500")
        after_values = query.get("after_offset", []) or query.get("after", [])
        try:
            after = int(after_values[0]) if after_values and after_values[0] else None
        except ValueError:
            after = None
        return _hardware_stream_payload(root, run_id, task_id, after=after, limit=limit)
    if detail_name == "hardware-session":
        task = task_snapshot(root, run_id, task_id)["task"]
        device, _baudrate = _resolve_task_device(task)
        status = bridge_status(root, device) if device else None
        return {
            "device": device,
            "bridge": status,
            "read_only": _task_is_terminal(task),
            "attached": bool(status and status.get("alive") and not status.get("paused")),
        }
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
        elif action == "final":
            final_payload = request_task_finalization_with_backend(root, run_id, task_id, f"/api/task/{task_id}/{action}")
            task = task_snapshot(root, run_id, task_id)["task"]
            return route_result({"ok": True, "task": task, **final_payload})
        elif action == "complete":
            _message, completion_payload = complete_selected_task(root, run_id, task_id)
            status = "200 OK" if completion_payload.get("ok") else "404 Not Found"
            return route_result(completion_payload, status)
        elif action in {"reopen", "resume"}:
            from aha_cli.store.filesystem import reopen_task

            task = reopen_task(root, run_id, task_id)
            recovery = recover_stale_running_agents(root, run_id, task_id=task_id)
            task = task_snapshot(root, run_id, task_id)["task"]
            return route_result({"ok": True, "task": task, "recovery": recovery})
        elif action == "delete":
            task = delete_task(root, run_id, task_id)
        elif action == "proxy":
            task = update_task_proxy_config(root, run_id, task_id, **parse_task_proxy_fields(parse_json_body(body)))
            return route_result({"ok": True, "task": task, "proxy": backend_proxy_config(load_config(root), task.get("preferred_backend"), require_plan(root, run_id), task)})
        elif action == "context-management":
            task = update_task_context_management_config(root, run_id, task_id, **parse_task_context_management_fields(parse_json_body(body)))
        elif action == "skills":
            task = update_task_skills_config(root, run_id, task_id, **parse_task_skills_fields(parse_json_body(body)))
        elif action == "hardware-debug":
            task = update_task_hardware_debug_config(root, run_id, task_id, **parse_task_hardware_debug_fields(parse_json_body(body)))
        elif action == "hardware-io":
            result = append_hardware_io_record(root, run_id, task_id, parse_json_body(body))
            return route_result({"ok": True, **result})
        elif action == "hardware-send":
            payload = parse_json_body(body)
            data = str(payload.get("data") or "")
            if not data:
                return route_result({"ok": False, "error": "data is required"}, "400 Bad Request")
            task = task_snapshot(root, run_id, task_id)["task"]
            device, baudrate = _resolve_task_device(task)
            if not device:
                return route_result({"ok": False, "error": "Task has no UART device configured."}, "400 Bad Request")
            if _task_is_terminal(task):
                return route_result({"ok": False, "error": "Task is terminal; hardware console is read-only."}, "409 Conflict")
            status = ensure_bridge(root, device, baudrate)
            if status.get("paused"):
                return route_result({"ok": False, "error": "Bridge is paused; resume it to send.", "bridge": status}, "409 Conflict")
            record = append_bridge_control(root, device, {"cmd": "send", "data": data, "source": "web"})
            return route_result({"ok": True, "device": device, "record": record})
        elif action in {"hardware-pause", "hardware-resume"}:
            task = task_snapshot(root, run_id, task_id)["task"]
            device, _baudrate = _resolve_task_device(task)
            if not device:
                return route_result({"ok": False, "error": "Task has no UART device configured."}, "400 Bad Request")
            cmd = "pause" if action == "hardware-pause" else "resume"
            append_bridge_control(root, device, {"cmd": cmd})
            return route_result({"ok": True, "device": device, "command": cmd})
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
                    if host_result.get("routed_to_host"):
                        set_agent_status(root, run_id, task_id, "main", "waiting", waiting_reason="host")
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
    source_memo_id = str(payload.get("source_memo_id") or "").strip()
    description = task_description_with_memo_attachment_context(root, run_id, description, source_memo_id)
    if not title:
        return route_result({"error": "title cannot be empty"}, "400 Bad Request")
    backend = str(payload.get("backend", "codex") or "codex")
    preferred_sub_backend = str(payload.get("preferred_sub_backend", "") or "") or None
    try:
        execution_fields = parse_execution_fields(payload, include_legacy_controls=True)
    except ValueError as exc:
        return route_result({"error": str(exc)}, "400 Bad Request")
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
        context_management = None
        if "context_management" in payload:
            if not isinstance(payload.get("context_management"), dict):
                return route_result({"error": "context_management must be an object"}, "400 Bad Request")
            context_management = parse_task_context_management_fields(payload["context_management"])
        task_skills = None
        if "task_skills" in payload:
            if not isinstance(payload.get("task_skills"), dict):
                return route_result({"error": "task_skills must be an object"}, "400 Bad Request")
            task_skills = parse_task_skills_fields(payload["task_skills"])
        hardware_debug = None
        if "hardware_debug" in payload:
            if not isinstance(payload.get("hardware_debug"), dict):
                return route_result({"error": "hardware_debug must be an object"}, "400 Bad Request")
            hardware_debug = parse_task_hardware_debug_fields(payload["hardware_debug"])
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
            proxy_enabled=parse_optional_bool(payload["proxy_enabled"], "proxy_enabled") if "proxy_enabled" in payload else None,
            http_proxy=str(payload.get("http_proxy", "") or "") or None,
            https_proxy=str(payload.get("https_proxy", "") or "") or None,
            no_proxy=str(payload.get("no_proxy", "") or "") or None,
            collaboration_mode=execution_fields["collaboration_mode"],
            workflow_template=execution_fields["workflow_template"],
            delegation_policy=execution_fields["delegation_policy"],
            max_sub_agents=execution_fields["max_sub_agents"],
            preferred_sub_backend=preferred_sub_backend,
            preferred_sub_model=str(payload.get("preferred_sub_model", "") or "") or None,
            description=description,
            supervision=supervision,
            context_management=context_management,
            task_skills=task_skills,
            hardware_debug=hardware_debug,
            dispatch=dispatch,
        )
    except ValueError as exc:
        return route_result({"error": str(exc)}, "400 Bad Request")
    backend_state = start_dispatched_task_backend(root, run_id, task, dispatch)
    memo = None
    if source_memo_id:
        try:
            memo = enrich_task_memo(root, run_id, update_task_memo(root, run_id, source_memo_id, {"created_task_id": task.get("id")}))
        except (KeyError, SystemExit, ValueError):
            memo = None
    response = {"ok": True, "task": task}
    if memo:
        response["memo"] = memo
    if backend_state:
        response["backend"] = backend_state
    return route_result(response)


def enrich_task_memo(root: Path, run_id: str, memo: dict) -> dict:
    enriched = dict(memo)
    task_id = str(enriched.get("created_task_id") or "").strip()
    if not task_id:
        enriched["created_task_status"] = ""
        enriched["created_task_title"] = ""
        return enriched
    plan = require_plan(root, run_id)
    task = next((item for item in plan.get("tasks", []) if item.get("id") == task_id), None)
    enriched["created_task_status"] = str((task or {}).get("status") or "missing")
    enriched["created_task_title"] = str((task or {}).get("title") or "")
    return enriched


def enrich_task_memos(root: Path, run_id: str, memos: list[dict]) -> list[dict]:
    return [enrich_task_memo(root, run_id, memo) for memo in memos]


def task_memo_query_value(query: dict[str, list[str]], key: str) -> str:
    return str(query.get(key, [""])[0] or "").strip()


def task_memo_query_limit(query: dict[str, list[str]]) -> int:
    raw = task_memo_query_value(query, "limit")
    try:
        return max(1, min(int(raw or "50"), 200))
    except ValueError:
        return 50


def task_memo_matches_query(memo: dict, query_text: str) -> bool:
    if not query_text:
        return True
    haystack = " ".join(
        str(memo.get(key) or "")
        for key in ("id", "title", "description", "scheduled_date", "end_date", "created_task_id", "created_task_title")
    ).lower()
    return query_text.lower() in haystack


def filter_task_memos_for_query(root: Path, run_id: str, query: dict[str, list[str]]) -> tuple[list[dict], int]:
    memos = enrich_task_memos(root, run_id, read_task_memos(root, run_id))
    query_text = task_memo_query_value(query, "q")
    status = task_memo_query_value(query, "status").lower()
    linked = task_memo_query_value(query, "linked").lower()
    include_id = task_memo_query_value(query, "include_id")
    limit = task_memo_query_limit(query)

    def matches(memo: dict) -> bool:
        if include_id and memo.get("id") == include_id:
            return True
        memo_status = str(memo.get("status") or "")
        if status and status != "all":
            if status in {"active", "open"}:
                if memo_status in {"done", "closed"}:
                    return False
            elif memo_status != status:
                return False
        has_task = bool(str(memo.get("created_task_id") or "").strip())
        if linked in {"linked", "true", "1"} and not has_task:
            return False
        if linked in {"unlinked", "false", "0"} and has_task:
            return False
        return task_memo_matches_query(memo, query_text)

    filtered = [memo for memo in memos if matches(memo)]
    return filtered[:limit], len(filtered)


def handle_task_memos_route(root: Path, run_id: str, method: str, path: str, query: dict[str, list[str]], body: bytes) -> dict:
    try:
        if path == "/api/task-memos":
            if method in {"GET", "HEAD"}:
                memos, total = filter_task_memos_for_query(root, run_id, query)
                return route_result({"ok": True, "memos": memos, "total": total})
            if method == "POST":
                return route_result({"ok": True, "memo": enrich_task_memo(root, run_id, create_task_memo(root, run_id, parse_json_body(body)))})
        if path.startswith("/api/task-memos/"):
            suffix = unquote(path.removeprefix("/api/task-memos/"))
            memo_id, _, action = suffix.partition("/")
            if not memo_id:
                return route_result({"error": "memo id is required"}, "400 Bad Request")
            if action == "completion-report" and method == "POST":
                try:
                    result = request_memo_completion_report_with_backend(root, run_id, memo_id)
                except KeyError as exc:
                    return route_result({"error": str(exc)}, "404 Not Found")
                except (SystemExit, ValueError) as exc:
                    return route_result({"error": str(exc)}, "400 Bad Request")
                payload = {"ok": True, "memo": enrich_task_memo(root, run_id, result["memo"])}
                if result.get("backend"):
                    payload["backend"] = result["backend"]
                return route_result(payload)
            if action:
                return route_not_handled()
            if method in {"PATCH", "POST"}:
                return route_result({"ok": True, "memo": enrich_task_memo(root, run_id, update_task_memo(root, run_id, memo_id, parse_json_body(body)))})
            if method == "DELETE":
                return route_result({"ok": True, "memo": delete_task_memo(root, run_id, memo_id)})
        return route_not_handled()
    except ValueError as exc:
        return route_result({"error": str(exc)}, "400 Bad Request")
    except (KeyError, SystemExit) as exc:
        return route_result({"error": str(exc)}, "404 Not Found")


def create_task_memo_asset_from_request(root: Path, run_id: str, headers: dict[str, str] | None, body: bytes) -> dict:
    content_type = str((headers or {}).get("content-type") or "").lower()
    if content_type.startswith("multipart/form-data"):
        fields, files = parse_multipart_form(headers or {}, body)
        upload = files.get("image") or files.get("file") or files.get("asset")
        if not upload:
            raise ValueError("memo attachment file is required")
        upload_body = upload.get("body")
        if not isinstance(upload_body, bytes):
            raise ValueError("memo attachment file is required")
        filename = fields.get("filename") or upload.get("filename") or "image"
        upload_content_type = fields.get("content_type") or fields.get("type") or upload.get("content_type") or ""
        return create_task_memo_asset_from_bytes(
            root,
            run_id,
            filename=filename,
            content_type=upload_content_type,
            data=upload_body,
        )
    return create_task_memo_asset(root, run_id, parse_json_body(body))


def handle_task_memo_assets_route(root: Path, run_id: str, method: str, path: str, body: bytes, headers: dict[str, str] | None = None) -> dict:
    try:
        if path == "/api/task-memo-assets" and method == "POST":
            asset = create_task_memo_asset_from_request(root, run_id, headers, body)
            return route_result({"ok": True, "asset": asset}, "201 Created")
        if path.startswith("/api/task-memo-assets/") and method in {"GET", "HEAD"}:
            filename = unquote(path.removeprefix("/api/task-memo-assets/")).strip("/")
            data, content_type, safe_name = read_task_memo_asset(root, run_id, filename)
            disposition = "inline" if content_type.startswith("image/") else "attachment"
            download_name = Path(safe_name).name
            return binary_route_result(
                data,
                content_type,
                headers={
                    "Content-Disposition": f'{disposition}; filename="{download_name}"',
                    "X-Content-Type-Options": "nosniff",
                },
            )
        return route_not_handled()
    except FileNotFoundError as exc:
        return route_result({"error": f"memo image asset not found: {exc}"}, "404 Not Found")
    except ValueError as exc:
        print(f"task memo asset upload failed: {exc}", flush=True)
        return route_result({"error": str(exc)}, "400 Bad Request")
    except SystemExit as exc:
        return route_result({"error": str(exc)}, "400 Bad Request")


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
    backend = str(payload.get("backend", "") or "").strip() or None
    model = str(payload.get("model", "") or "").strip() if "model" in payload else None
    sandbox = str(payload.get("sandbox", "") or "") or None
    approval = str(payload.get("approval", "") or "") or None
    proxy_enabled = parse_optional_bool(payload["proxy_enabled"], "proxy_enabled") if "proxy_enabled" in payload else None
    restart_backend = parse_optional_bool(payload["restart_backend"], "restart_backend") if "restart_backend" in payload else False
    if not task_id or not agent_id:
        return route_result({"error": "task_id and agent_id are required"}, "400 Bad Request")
    if backend is not None:
        error = validate_backend_name(backend)
        if error:
            return route_result({"error": error}, "400 Bad Request")
    error = validate_runtime_options(sandbox, approval)
    if error:
        return route_result({"error": error}, "400 Bad Request")
    try:
        agent = None
        if sandbox is not None or approval is not None or proxy_enabled is not None:
            agent = update_agent_config(root, run_id, task_id, agent_id, sandbox=sandbox, approval=approval, proxy_enabled=proxy_enabled)
        backend_switch = None
        if backend is not None:
            switch_kwargs = {"model": model} if "model" in payload else {}
            backend_switch = switch_agent_backend(root, run_id, task_id, agent_id, backend=backend, **switch_kwargs)
            agent = backend_switch["agent"]
        backend_restart = None
        if restart_backend and backend is None:
            backend_restart = restart_agent_backend(root, run_id, task_id, agent_id)
        if agent is None:
            agent = update_agent_config(root, run_id, task_id, agent_id)
        task = task_snapshot(root, run_id, task_id)["task"]
        return route_result({"ok": True, "agent": agent, "task": task, "backend_switch": backend_switch, "backend_restart": backend_restart})
    except (SystemExit, ValueError) as exc:
        return route_result({"error": str(exc)}, "404 Not Found")
    except OSError as exc:
        return route_result({"error": str(exc)}, "500 Internal Server Error")


def handle_task_config_route(root: Path, run_id: str, payload: dict) -> dict:
    task_id = str(payload.get("task_id", "")).strip()
    if not task_id:
        return route_result({"error": "task_id is required"}, "400 Bad Request")
    try:
        if "context_management" in payload and isinstance(payload.get("context_management"), dict):
            task = update_task_context_management_config(root, run_id, task_id, **parse_task_context_management_fields(payload["context_management"]))
        elif "task_skills" in payload and isinstance(payload.get("task_skills"), dict):
            task = update_task_skills_config(root, run_id, task_id, **parse_task_skills_fields(payload["task_skills"]))
        elif "hardware_debug" in payload and isinstance(payload.get("hardware_debug"), dict):
            task = update_task_hardware_debug_config(root, run_id, task_id, **parse_task_hardware_debug_fields(payload["hardware_debug"]))
        elif "supervision" in payload and isinstance(payload.get("supervision"), dict):
            task = update_task_supervision_config(root, run_id, task_id, **parse_task_supervision_fields(payload["supervision"]))
        else:
            task = update_task_proxy_config(root, run_id, task_id, **parse_task_proxy_fields(payload))
            return route_result({"ok": True, "task": task, "proxy": backend_proxy_config(load_config(root), task.get("preferred_backend"), require_plan(root, run_id), task)})
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
    headers: dict[str, str] | None = None,
) -> dict:
    if path == "/api/task-memo-assets" or path.startswith("/api/task-memo-assets/"):
        return handle_task_memo_assets_route(root, require_api_run_id(root, default_run_id, query), method, path, body, headers)
    if path == "/api/task-memos" or path.startswith("/api/task-memos/"):
        return handle_task_memos_route(root, require_api_run_id(root, default_run_id, query), method, path, query, body)
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
    "handle_task_memo_assets_route",
    "handle_task_memos_route",
    "handle_task_action_route",
    "handle_task_config_route",
    "handle_task_detail_route",
    "route_task_agent_request",
    "task_detail_payload",
    "task_final_view_snapshot",
]
