from __future__ import annotations

from pathlib import Path
import tempfile

from aha_cli.backends.registry import agent_backend_names, agent_backend_or_default
from aha_cli.services.orchestrator import dispatch_task_to_main
from aha_cli.services.run_archive import export_run_archive, import_run_archive
from aha_cli.store.filesystem import (
    add_workspace,
    create_plan,
    load_config,
    resolve_workspace_path,
    run_summary,
)
from aha_cli.web.http_utils import (
    http_response,
    json_response,
    parse_json_body,
    parse_multipart_form,
    parse_optional_bool,
    parse_query_bool,
)
from aha_cli.web.run_api import (
    archive_upload_suffix,
    bootstrap_payload,
    require_api_run_id,
    run_export_headers,
    run_import_success_payload,
    runs_payload,
    safe_download_name,
    workspaces_payload,
)
from aha_cli.web.task_actions import start_dispatched_task_backend

SANDBOX_OPTIONS = {"read-only", "workspace-write", "danger-full-access"}
APPROVAL_OPTIONS = {"untrusted", "on-failure", "on-request", "never"}


def head_or_response(method: str, response: bytes, content_type: str = "application/json; charset=utf-8") -> bytes:
    return http_response("200 OK", b"", content_type) if method == "HEAD" else response


def handle_runs_index(root: Path, default_run_id: str, method: str) -> bytes:
    response = json_response(runs_payload(root, default_run_id))
    return head_or_response(method, response)


def handle_run_export(root: Path, default_run_id: str, method: str, query: dict[str, list[str]]) -> bytes:
    selected_run_id = require_api_run_id(root, default_run_id, query)
    no_logs = parse_query_bool(query, "no_logs", False)
    safe_run_id = safe_download_name(selected_run_id)
    with tempfile.TemporaryDirectory(prefix="aha-run-export-") as tmp:
        archive_path = export_run_archive(
            root,
            selected_run_id,
            Path(tmp) / f"aha-run-{safe_run_id}.tar.gz",
            include_logs=not no_logs,
        )
        payload = b"" if method == "HEAD" else archive_path.read_bytes()
    return http_response("200 OK", payload, "application/gzip", run_export_headers(selected_run_id))


def handle_run_import(root: Path, headers: dict[str, str], body: bytes) -> bytes:
    temp_archive_path: Path | None = None
    try:
        content_type = headers.get("content-type", "")
        if content_type.lower().startswith("multipart/form-data"):
            fields, files = parse_multipart_form(headers, body)
            upload = files.get("archive") or files.get("file")
            if not upload:
                return json_response({"error": "archive file is required"}, "400 Bad Request")
            upload_body = upload.get("body")
            if not isinstance(upload_body, bytes) or not upload_body:
                return json_response({"error": "archive file is empty"}, "400 Bad Request")
            suffix = archive_upload_suffix(str(upload.get("filename") or "archive.tar.gz"))
            with tempfile.NamedTemporaryFile(prefix="aha-run-import-", suffix=suffix, delete=False) as handle:
                handle.write(upload_body)
                temp_archive_path = Path(handle.name)
            payload = fields
            archive_path = temp_archive_path
        else:
            payload = parse_json_body(body)
            archive_path_text = str(payload.get("archive_path", "") or "").strip()
            if not archive_path_text:
                return json_response({"error": "archive_path is required"}, "400 Bad Request")
            archive_path = Path(archive_path_text)

        target_run_id = str(payload.get("target_run_id", "") or "").strip() or None
        preserve_id = parse_optional_bool(payload.get("preserve_id", False), "preserve_id")
        force = parse_optional_bool(payload.get("force", False), "force")
        source_run_id, imported_run_id = import_run_archive(
            root,
            archive_path,
            target_run_id=target_run_id,
            preserve_id=preserve_id,
            force=force,
        )
        return json_response(run_import_success_payload(root, source_run_id, imported_run_id), "201 Created")
    finally:
        if temp_archive_path is not None:
            temp_archive_path.unlink(missing_ok=True)


def handle_bootstrap(root: Path, default_run_id: str, method: str, request_headers: dict[str, str] | None = None) -> bytes:
    response = json_response(bootstrap_payload(root, default_run_id), request_headers=request_headers)
    return head_or_response(method, response)


def handle_create_run(root: Path, body: bytes) -> bytes:
    payload = parse_json_body(body)
    goal = str(payload.get("goal", "") or "").strip()
    if not goal:
        return json_response({"error": "goal cannot be empty"}, "400 Bad Request")

    cfg = load_config(root)
    mode = str(payload.get("mode", cfg.get("default_mode", "research")) or "research")
    if mode not in {"research", "implementation"}:
        return json_response({"error": f"unknown mode: {mode}"}, "400 Bad Request")

    backend = str(payload.get("backend", "") or "") or agent_backend_or_default(cfg.get("backend"), "stub")
    if backend not in agent_backend_names():
        return json_response({"error": f"unknown agent backend: {backend}"}, "400 Bad Request")

    sandbox = str(payload.get("sandbox", "") or "") or None
    approval = str(payload.get("approval", "") or "") or None
    if sandbox is not None and sandbox not in SANDBOX_OPTIONS:
        return json_response({"error": f"unknown sandbox: {sandbox}"}, "400 Bad Request")
    if approval is not None and approval not in APPROVAL_OPTIONS:
        return json_response({"error": f"unknown approval: {approval}"}, "400 Bad Request")

    task_titles = payload.get("task_titles", payload.get("tasks", []))
    if isinstance(task_titles, str):
        task_titles = [task_titles]
    write_scopes = payload.get("write_scopes", [])
    if isinstance(write_scopes, str):
        write_scopes = [write_scopes]
    try:
        agents = max(1, int(payload.get("agents", 1) or 1))
    except (TypeError, ValueError):
        return json_response({"error": "agents must be an integer"}, "400 Bad Request")

    try:
        workspace_path, workspace_id = resolve_workspace_path(
            root,
            workspace_id=str(payload.get("workspace_id", payload.get("workspace", "")) or "") or None,
            workspace_path=str(payload.get("workspace_path", "") or "") or None,
            default=Path.cwd(),
        )
    except ValueError as exc:
        return json_response({"error": str(exc)}, "400 Bad Request")

    plan = create_plan(
        root=root,
        goal=goal,
        agents=agents,
        mode=mode,
        task_titles=[str(item) for item in (task_titles or []) if str(item).strip()],
        write_scopes=[str(item) for item in (write_scopes or []) if str(item).strip()],
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
    )
    backend_states = []
    if bool(payload.get("dispatch", False)):
        for task in plan.get("tasks", []):
            dispatch_task_to_main(root, plan["id"], task)
            backend_state = start_dispatched_task_backend(root, plan["id"], task, True)
            if backend_state:
                backend_states.append(backend_state)
    response = {"ok": True, "run": run_summary(root, plan["id"])}
    if backend_states:
        response["backends"] = backend_states
    return json_response(response, "201 Created")


def handle_workspaces_index(root: Path, method: str) -> bytes:
    response = json_response(workspaces_payload(root))
    return head_or_response(method, response)


def handle_create_workspace(root: Path, body: bytes) -> bytes:
    payload = parse_json_body(body)
    workspace_path = str(payload.get("path", payload.get("workspace_path", "")) or "").strip()
    if not workspace_path:
        return json_response({"error": "workspace path is required"}, "400 Bad Request")
    try:
        workspace = add_workspace(root, workspace_path, name=str(payload.get("name", "") or "") or None)
    except ValueError as exc:
        return json_response({"error": str(exc)}, "400 Bad Request")
    return json_response({"ok": True, "workspace": workspace}, "201 Created")


def handle_run_workspace_route(
    root: Path,
    default_run_id: str,
    method: str,
    path: str,
    query: dict[str, list[str]],
    headers: dict[str, str],
    body: bytes,
) -> bytes | None:
    if method in {"GET", "HEAD"} and path == "/api/runs":
        return handle_runs_index(root, default_run_id, method)
    if method in {"GET", "HEAD"} and path == "/api/run/export":
        return handle_run_export(root, default_run_id, method, query)
    if method == "POST" and path == "/api/run/import":
        return handle_run_import(root, headers, body)
    if method in {"GET", "HEAD"} and path == "/api/bootstrap":
        return handle_bootstrap(root, default_run_id, method, headers)
    if method == "POST" and path == "/api/runs":
        return handle_create_run(root, body)
    if method in {"GET", "HEAD"} and path == "/api/workspaces":
        return handle_workspaces_index(root, method)
    if method == "POST" and path == "/api/workspaces":
        return handle_create_workspace(root, body)
    return None


__all__ = [
    "handle_bootstrap",
    "handle_create_run",
    "handle_create_workspace",
    "handle_run_export",
    "handle_run_import",
    "handle_run_workspace_route",
    "handle_runs_index",
    "handle_workspaces_index",
]
