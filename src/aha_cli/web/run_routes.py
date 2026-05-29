from __future__ import annotations

from pathlib import Path
import tempfile

from aha_cli.backends.registry import agent_backend_names, agent_backend_or_default
from aha_cli.domain.models import TASK_COLLABORATION_MODES, default_config
from aha_cli.services.orchestrator import dispatch_task_to_main
from aha_cli.services.run_archive import export_run_archive, import_run_archive
from aha_cli.store.filesystem import (
    add_workspace,
    config_path,
    create_plan,
    load_config,
    rename_run,
    resolve_workspace_path,
    run_summary,
)
from aha_cli.store.io import write_json
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
CONFIG_SANDBOX_OPTIONS = SANDBOX_OPTIONS | {"auto"}
APPROVAL_OPTIONS = {"untrusted", "on-failure", "on-request", "never"}
SESSION_POLICY_OPTIONS = {"sticky", "fresh"}
BOOTSTRAP_BACKEND_OPTIONS = {"codex", "claude"}
CLAUDE_ENV_GROUP_FIELDS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY")
CLAUDE_ENV_GROUP_ALIASES = {
    "ANTHROPIC_BASE_URL": ("ANTHROPIC_BASE_URL", "base_url"),
    "ANTHROPIC_MODEL": ("ANTHROPIC_MODEL", "model"),
    "ANTHROPIC_API_KEY": ("ANTHROPIC_API_KEY", "api_key"),
}


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


def _optional_string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _string_or_default(value: object, default: str) -> str:
    return str(value or "").strip() or default


def _string_list(value: object, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError(f"{field_name} must be a list")
    return [str(item).strip() for item in items if str(item or "").strip()]


def _object_value(value: object, field_name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return value


def _claude_env_groups(value: object) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, dict):
        legacy = {"name": "default"}
        for key in CLAUDE_ENV_GROUP_FIELDS:
            legacy[key] = next((str(value.get(alias) or "").strip() for alias in CLAUDE_ENV_GROUP_ALIASES[key] if value.get(alias)), "")
        if not any(legacy.get(key) for key in CLAUDE_ENV_GROUP_FIELDS):
            return []
        return [legacy]
    if not isinstance(value, list):
        raise ValueError("claude.env must be a list")
    groups: list[dict] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError("claude.env entries must be objects")
        raw_name = str(item.get("name") or "").strip()
        group = {"name": raw_name or f"env-{index}"}
        for key in CLAUDE_ENV_GROUP_FIELDS:
            group[key] = str(item.get(key) or "").strip()
        if raw_name or any(group.get(key) for key in CLAUDE_ENV_GROUP_FIELDS):
            groups.append(group)
    return groups


def _config_sandbox(value: object, default: str) -> str:
    sandbox = _string_or_default(value, default)
    if sandbox not in CONFIG_SANDBOX_OPTIONS:
        raise ValueError(f"unknown sandbox: {sandbox}")
    return sandbox


def _session_policy(value: object, default: str) -> str:
    policy = _string_or_default(value, default)
    if policy not in SESSION_POLICY_OPTIONS:
        raise ValueError(f"unknown session policy: {policy}")
    return policy


def _bootstrap_config_from_payload(payload: dict) -> dict:
    defaults = default_config()
    backend = _string_or_default(payload.get("backend"), "codex")
    if backend not in BOOTSTRAP_BACKEND_OPTIONS:
        raise ValueError(f"unknown backend: {backend}")
    mode = _string_or_default(payload.get("default_mode"), str(defaults["default_mode"]))
    if mode not in {"research", "implementation"}:
        raise ValueError(f"unknown default mode: {mode}")
    try:
        default_parallel = max(1, int(payload.get("default_parallel", defaults["default_parallel"]) or defaults["default_parallel"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("default_parallel must be an integer") from exc

    codex_payload = _object_value(payload.get("codex"), "codex")
    codex_defaults = defaults["codex"]
    codex = {
        "bin": _string_or_default(codex_payload.get("bin"), str(codex_defaults["bin"])),
        "model": _optional_string(codex_payload.get("model")),
        "sandbox": _config_sandbox(codex_payload.get("sandbox"), str(codex_defaults["sandbox"])),
        "approval": _string_or_default(codex_payload.get("approval"), str(codex_defaults["approval"])),
        "json": parse_optional_bool(codex_payload.get("json", codex_defaults["json"]), "codex.json"),
        "session_policy": _session_policy(codex_payload.get("session_policy"), str(codex_defaults["session_policy"])),
    }
    if codex["approval"] not in APPROVAL_OPTIONS:
        raise ValueError(f"unknown approval: {codex['approval']}")

    claude_payload = _object_value(payload.get("claude"), "claude")
    claude_defaults = defaults["claude"]
    claude_env = _claude_env_groups(claude_payload.get("env"))
    claude = {
        "bin": _string_or_default(claude_payload.get("bin"), str(claude_defaults["bin"])),
        "sandbox": _config_sandbox(claude_payload.get("sandbox"), str(claude_defaults["sandbox"])),
        "permission_mode": _optional_string(claude_payload.get("permission_mode")),
        "session_policy": _session_policy(claude_payload.get("session_policy"), str(claude_defaults["session_policy"])),
        "env_active": _optional_string(claude_payload.get("env_active")),
        "env": claude_env,
    }

    return {
        "backend": backend,
        "runner_command": _optional_string(payload.get("runner_command")),
        "default_parallel": default_parallel,
        "default_mode": mode,
        "workspace_roots": _string_list(payload.get("workspace_roots"), "workspace_roots"),
        "webgame_workspace": _optional_string(payload.get("webgame_workspace")),
        "context_windows": _object_value(payload.get("context_windows"), "context_windows"),
        "codex": codex,
        "claude": claude,
    }


def handle_save_bootstrap(root: Path, default_run_id: str, body: bytes) -> bytes:
    payload = parse_json_body(body)
    if config_path(root).exists() and not parse_optional_bool(payload.get("force", False), "force"):
        return json_response({"error": "AHA is already initialized"}, "409 Conflict")
    cfg = _bootstrap_config_from_payload(payload)
    write_json(config_path(root), cfg)
    return json_response(bootstrap_payload(root, default_run_id), "201 Created")


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
    collaboration_mode = str(payload.get("collaboration_mode", "auto") or "auto")
    if collaboration_mode not in TASK_COLLABORATION_MODES:
        return json_response({"error": f"unknown collaboration mode: {collaboration_mode}"}, "400 Bad Request")

    create_initial_task = parse_optional_bool(payload.get("create_initial_task", True), "create_initial_task")
    task_titles = payload.get("task_titles", payload.get("tasks", []))
    if isinstance(task_titles, str):
        task_titles = [task_titles]
    if not create_initial_task:
        task_titles = []
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
        collaboration_mode=collaboration_mode,
        create_default_tasks=create_initial_task,
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


def handle_update_run(root: Path, default_run_id: str, run_id: str, body: bytes) -> bytes:
    payload = parse_json_body(body)
    name = str(payload.get("name", payload.get("goal", "")) or "").strip()
    if not name:
        return json_response({"error": "run name cannot be empty"}, "400 Bad Request")
    try:
        run = rename_run(root, run_id, name)
    except SystemExit as exc:
        return json_response({"error": str(exc)}, "404 Not Found")
    except ValueError as exc:
        return json_response({"error": str(exc)}, "400 Bad Request")
    response = runs_payload(root, default_run_id)
    response.update({"ok": True, "run": run})
    return json_response(response)


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
    if method == "POST" and path == "/api/bootstrap":
        return handle_save_bootstrap(root, default_run_id, body)
    if method == "POST" and path == "/api/runs":
        return handle_create_run(root, body)
    if method == "PATCH" and path.startswith("/api/runs/"):
        run_id = path.removeprefix("/api/runs/").strip("/")
        if not run_id or "/" in run_id:
            return json_response({"error": "run id is required"}, "400 Bad Request")
        return handle_update_run(root, default_run_id, run_id, body)
    if method in {"GET", "HEAD"} and path == "/api/workspaces":
        return handle_workspaces_index(root, method)
    if method == "POST" and path == "/api/workspaces":
        return handle_create_workspace(root, body)
    return None


__all__ = [
    "handle_bootstrap",
    "handle_create_run",
    "handle_create_workspace",
    "handle_update_run",
    "handle_save_bootstrap",
    "handle_run_export",
    "handle_run_import",
    "handle_run_workspace_route",
    "handle_runs_index",
    "handle_workspaces_index",
]
