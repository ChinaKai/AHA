from __future__ import annotations

from pathlib import Path
import tempfile
from urllib.parse import unquote

from aha_cli.backends.registry import agent_backend_names, agent_backend_or_default
from aha_cli.domain.models import default_config, normalize_integrations_config
from aha_cli.services.observe_proxy import observe_proxy_status, observe_proxy_usage_summary
from aha_cli.services.orchestrator import dispatch_task_to_main
from aha_cli.services.proxy import normalize_proxy_config, proxy_configured
from aha_cli.services.run_archive import export_run_archive, import_run_archive
from aha_cli.services.run_delete import RunDeleteError, delete_run
from aha_cli.services.run_lifecycle_actions import RunLifecycleActionError, set_run_lifecycle_status
from aha_cli.services.run_recovery import RunRecoveryError, run_stale_runtime_recovery
from aha_cli.services.run_retention import (
    RunRetentionError,
    apply_run_retention,
    inspect_run_retention_archive,
    list_retention_archives,
    restore_run_retention_archive,
    run_retention_report,
)
from aha_cli.services.run_retention_policy import enforce_run_retention_policy, retention_policy_schedule_config
from aha_cli.store.filesystem import (
    add_workspace,
    config_path,
    create_plan,
    load_config,
    rename_run,
    resolve_workspace_path,
    run_exists,
    run_summary,
    update_run_proxy_config,
)
from aha_cli.store.io import read_json, write_json
from aha_cli.web.execution_fields import parse_execution_fields
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
    default_api_run_id,
    require_api_run_id,
    request_run_id,
    run_export_headers,
    run_import_success_payload,
    runs_payload,
    safe_download_name,
    workspaces_payload,
)
from aha_cli.web.task_actions import parse_task_proxy_fields, start_dispatched_task_backend

SANDBOX_OPTIONS = {"read-only", "workspace-write", "danger-full-access"}
CONFIG_SANDBOX_OPTIONS = SANDBOX_OPTIONS | {"auto"}
APPROVAL_OPTIONS = {"untrusted", "on-failure", "on-request", "never"}
SESSION_POLICY_OPTIONS = {"sticky", "fresh"}
BOOTSTRAP_BACKEND_OPTIONS = {"codex", "claude"}
CODEX_ENV_GROUP_FIELDS = ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY", "CODEX_WIRE_API", "CODEX_ENV_KEY")
CODEX_ENV_GROUP_ALIASES = {
    "OPENAI_BASE_URL": ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "base_url", "api_url"),
    "OPENAI_MODEL": ("OPENAI_MODEL", "ANTHROPIC_MODEL", "model"),
    "OPENAI_API_KEY": ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "api_key", "auth_token"),
    "CODEX_WIRE_API": ("CODEX_WIRE_API", "wire_api"),
    "CODEX_ENV_KEY": ("CODEX_ENV_KEY", "env_key"),
}
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


def _query_text(query: dict[str, list[str]], key: str) -> str | None:
    value = str(query.get(key, [""])[0] or "").strip()
    return value or None


def _query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = str(query.get(key, [""])[0] or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _query_groups(query: dict[str, list[str]]) -> list[str] | None:
    raw_values = query.get("group", []) + query.get("groups", [])
    groups: list[str] = []
    for raw_value in raw_values:
        for item in str(raw_value or "").split(","):
            value = item.strip()
            if value and value not in groups:
                groups.append(value)
    return groups or None


def _payload_text(payload: dict, key: str) -> str:
    return str(payload.get(key, "") or "").strip()


def _payload_int(payload: dict, key: str, default: int) -> int:
    raw = _payload_text(payload, key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _payload_groups(payload: dict) -> list[str] | None:
    value = payload.get("groups", payload.get("group"))
    if value is None:
        return None
    raw_values = value if isinstance(value, list) else [value]
    groups: list[str] = []
    for raw_value in raw_values:
        for item in str(raw_value or "").split(","):
            group = item.strip()
            if group and group not in groups:
                groups.append(group)
    return groups or None


def _retention_visibility_payload(root: Path, run_id: str, query: dict[str, list[str]]) -> dict:
    return run_retention_report(
        root,
        run_id,
        top=_query_int(query, "top", 10),
        groups=_query_groups(query),
        include_chat=parse_query_bool(query, "include_chat", False),
        min_age_seconds=_query_int(query, "min_age_seconds", 0),
        max_total_bytes=_query_int(query, "max_total_bytes", 0),
        max_candidate_bytes=_query_int(query, "max_candidate_bytes", 0),
        min_candidate_files=_query_int(query, "min_candidate_files", 0),
    )


def _retention_archive_visibility_payload(root: Path, run_id: str) -> dict:
    return list_retention_archives(root, run_id)


def _recovery_visibility_payload(root: Path, run_id: str, query: dict[str, list[str]]) -> dict:
    return run_stale_runtime_recovery(
        root,
        run_id,
        task_id=_query_text(query, "task_id"),
        agent_id=_query_text(query, "agent_id"),
        apply=False,
    )


def _run_visibility_error(exc: Exception) -> bytes | None:
    if isinstance(exc, FileNotFoundError):
        return json_response({"error": str(exc), "reason": "run_not_found"}, "404 Not Found")
    if isinstance(exc, RunRetentionError):
        return json_response({"error": str(exc), "reason": exc.reason}, exc.status_code)
    if isinstance(exc, RunRecoveryError):
        return json_response({"error": str(exc), "reason": exc.reason}, exc.status_code)
    if isinstance(exc, ValueError):
        return json_response({"error": str(exc)}, "400 Bad Request")
    return None


def _confirmation_error(expected: str) -> bytes:
    return json_response(
        {
            "error": f"confirmation required: {expected}",
            "reason": "confirm_required",
            "confirm": expected,
        },
        "400 Bad Request",
    )


def handle_run_retention_visibility(root: Path, method: str, run_id: str, query: dict[str, list[str]]) -> bytes:
    try:
        retention = _retention_visibility_payload(root, run_id, query)
    except (FileNotFoundError, ValueError) as exc:
        response = _run_visibility_error(exc)
        if response is not None:
            return response
        raise
    response = json_response(
        {"ok": True, "run_id": retention["run_id"], "retention": retention}
    )
    return head_or_response(method, response)


def handle_run_recovery_visibility(root: Path, method: str, run_id: str, query: dict[str, list[str]]) -> bytes:
    try:
        recovery = _recovery_visibility_payload(root, run_id, query)
    except (RunRecoveryError, ValueError) as exc:
        response = _run_visibility_error(exc)
        if response is not None:
            return response
        raise
    response = json_response(
        {"ok": True, "run_id": recovery["run_id"], "recovery": recovery}
    )
    return head_or_response(method, response)


def handle_run_retention_action(root: Path, default_run_id: str, run_id: str, body: bytes) -> bytes:
    payload = parse_json_body(body)
    action = _payload_text(payload, "action") or "archive"
    force = action == "compact" or parse_optional_bool(payload.get("force", False), "force")
    apply_if_over_limit = action == "policy" or parse_optional_bool(payload.get("apply_if_over_limit", False), "apply_if_over_limit")
    if action not in {"archive", "compact", "policy"}:
        return json_response({"error": f"unknown retention action: {action}"}, "400 Bad Request")
    expected_confirm = "delete archived originals" if force else "apply retention policy" if apply_if_over_limit else "archive"
    if _payload_text(payload, "confirm") != expected_confirm:
        return _confirmation_error(expected_confirm)
    current_run_id = str(payload.get("current_run_id", "") or "").strip() or default_run_id
    try:
        options = {
            "current_run_id": current_run_id,
            "active_heartbeat_seconds": _payload_int(payload, "active_heartbeat_seconds", 120),
            "force": force,
            "top": _payload_int(payload, "top", 10),
            "groups": _payload_groups(payload),
            "include_chat": parse_optional_bool(payload.get("include_chat", False), "include_chat"),
            "min_age_seconds": _payload_int(payload, "min_age_seconds", 0),
            "max_total_bytes": _payload_int(payload, "max_total_bytes", 0),
            "max_candidate_bytes": _payload_int(payload, "max_candidate_bytes", 0),
            "min_candidate_files": _payload_int(payload, "min_candidate_files", 0),
        }
        if apply_if_over_limit:
            retention = enforce_run_retention_policy(root, run_id, apply=True, **options)
        else:
            retention = apply_run_retention(root, run_id, **options)
        archives = _retention_archive_visibility_payload(root, retention["run_id"])
    except (FileNotFoundError, RunRetentionError, ValueError) as exc:
        response = _run_visibility_error(exc)
        if response is not None:
            return response
        raise
    return json_response(
        {
            "ok": True,
            "run_id": retention["run_id"],
            "retention": retention,
            "retention_archives": archives,
        }
    )


def handle_run_recovery_action(root: Path, run_id: str, body: bytes) -> bytes:
    payload = parse_json_body(body)
    if _payload_text(payload, "confirm") != "recover stale agent":
        return _confirmation_error("recover stale agent")
    task_id = _payload_text(payload, "task_id")
    agent_id = _payload_text(payload, "agent_id")
    restart_backend = parse_optional_bool(payload.get("restart_backend", False), "restart_backend")
    if not task_id or not agent_id:
        return json_response({"error": "task_id and agent_id are required"}, "400 Bad Request")
    try:
        recovery = run_stale_runtime_recovery(
            root,
            run_id,
            task_id=task_id,
            agent_id=agent_id,
            apply=True,
            restart_backend=restart_backend,
        )
    except (RunRecoveryError, ValueError) as exc:
        response = _run_visibility_error(exc)
        if response is not None:
            return response
        raise
    return json_response({"ok": True, "run_id": recovery["run_id"], "recovery": recovery})


def handle_run_retention_archive_list(root: Path, method: str, run_id: str) -> bytes:
    try:
        archives = _retention_archive_visibility_payload(root, run_id)
    except (FileNotFoundError, RunRetentionError, ValueError) as exc:
        response = _run_visibility_error(exc)
        if response is not None:
            return response
        raise
    response = json_response({"ok": True, "run_id": run_id, "retention_archives": archives})
    return head_or_response(method, response)


def handle_run_retention_archive_inspect(root: Path, method: str, run_id: str, archive_name: str) -> bytes:
    try:
        archive = inspect_run_retention_archive(root, run_id, unquote(archive_name))
    except (FileNotFoundError, RunRetentionError, ValueError) as exc:
        response = _run_visibility_error(exc)
        if response is not None:
            return response
        raise
    response = json_response({"ok": True, "run_id": archive["run_id"], "retention_archive": archive})
    return head_or_response(method, response)


def handle_run_retention_archive_restore(
    root: Path,
    default_run_id: str,
    run_id: str,
    body: bytes,
    archive_name: str | None = None,
) -> bytes:
    payload = parse_json_body(body)
    archive_path = _payload_text(payload, "archive")
    selected_archive_name = unquote(archive_name or Path(archive_path).name)
    if not selected_archive_name:
        return json_response({"error": "archive is required"}, "400 Bad Request")
    force = parse_optional_bool(payload.get("force", False), "force")
    expected_confirm = "overwrite restored files" if force else "restore archive"
    if _payload_text(payload, "confirm") != expected_confirm:
        return _confirmation_error(expected_confirm)
    current_run_id = str(payload.get("current_run_id", "") or "").strip() or default_run_id
    try:
        restore = restore_run_retention_archive(
            root,
            run_id,
            selected_archive_name,
            current_run_id=current_run_id,
            force=force,
            active_heartbeat_seconds=_payload_int(payload, "active_heartbeat_seconds", 120),
        )
        archives = _retention_archive_visibility_payload(root, restore["run_id"])
    except (FileNotFoundError, RunRetentionError, ValueError) as exc:
        response = _run_visibility_error(exc)
        if response is not None:
            return response
        raise
    return json_response(
        {
            "ok": True,
            "run_id": restore["run_id"],
            "restore": restore,
            "retention_archives": archives,
        }
    )


def handle_run_maintenance_visibility(root: Path, method: str, run_id: str, query: dict[str, list[str]]) -> bytes:
    try:
        retention = _retention_visibility_payload(root, run_id, query)
        recovery = _recovery_visibility_payload(root, run_id, query)
        archives = _retention_archive_visibility_payload(root, run_id)
    except (FileNotFoundError, RunRetentionError, RunRecoveryError, ValueError) as exc:
        response = _run_visibility_error(exc)
        if response is not None:
            return response
        raise
    response = json_response(
        {
            "ok": True,
            "run_id": retention["run_id"],
            "retention": retention,
            "recovery": recovery,
            "retention_archives": archives,
        }
    )
    return head_or_response(method, response)


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


def _codex_env_groups(value: object) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, dict):
        legacy = {"name": "default"}
        for key in CODEX_ENV_GROUP_FIELDS:
            legacy[key] = next((str(value.get(alias) or "").strip() for alias in CODEX_ENV_GROUP_ALIASES[key] if value.get(alias)), "")
        if not any(legacy.get(key) for key in CODEX_ENV_GROUP_FIELDS):
            return []
        return [legacy]
    if not isinstance(value, list):
        raise ValueError("codex.env must be a list")
    groups: list[dict] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError("codex.env entries must be objects")
        raw_name = str(item.get("name") or "").strip()
        group = {"name": raw_name or f"env-{index}"}
        for key in CODEX_ENV_GROUP_FIELDS:
            group[key] = next((str(item.get(alias) or "").strip() for alias in CODEX_ENV_GROUP_ALIASES[key] if item.get(alias)), "")
        if raw_name or any(group.get(key) for key in CODEX_ENV_GROUP_FIELDS):
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


def _proxy_config_from_payload(value: object, field_name: str, fallback: dict | None = None) -> dict:
    fallback = fallback or {}
    payload = _object_value(value, field_name)
    return normalize_proxy_config(
        payload.get("enabled", payload.get("proxy_enabled", fallback.get("enabled", False))),
        payload.get("http_proxy", fallback.get("http_proxy")),
        payload.get("https_proxy", fallback.get("https_proxy")),
        payload.get("no_proxy", fallback.get("no_proxy")),
    )


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
    codex_env = _codex_env_groups(codex_payload.get("env"))
    legacy_proxy = _proxy_config_from_payload(payload.get("proxy"), "proxy")
    proxy_fallback = legacy_proxy if proxy_configured(legacy_proxy) else None
    codex = {
        "bin": _string_or_default(codex_payload.get("bin"), str(codex_defaults["bin"])),
        "model": _optional_string(codex_payload.get("model")),
        "sandbox": _config_sandbox(codex_payload.get("sandbox"), str(codex_defaults["sandbox"])),
        "approval": _string_or_default(codex_payload.get("approval"), str(codex_defaults["approval"])),
        "json": parse_optional_bool(codex_payload.get("json", codex_defaults["json"]), "codex.json"),
        "session_policy": _session_policy(codex_payload.get("session_policy"), str(codex_defaults["session_policy"])),
        "env_active": _optional_string(codex_payload.get("env_active")),
        "env": codex_env,
        "proxy": _proxy_config_from_payload(codex_payload.get("proxy"), "codex.proxy", proxy_fallback),
    }
    if codex["approval"] not in APPROVAL_OPTIONS:
        raise ValueError(f"unknown approval: {codex['approval']}")

    claude_payload = _object_value(payload.get("claude"), "claude")
    claude_defaults = defaults["claude"]
    claude_env = _claude_env_groups(claude_payload.get("env"))
    claude = {
        "bin": _string_or_default(claude_payload.get("bin"), str(claude_defaults["bin"])),
        "model": _optional_string(claude_payload.get("model")),
        "sandbox": _config_sandbox(claude_payload.get("sandbox"), str(claude_defaults["sandbox"])),
        "permission_mode": _optional_string(claude_payload.get("permission_mode")),
        "session_policy": _session_policy(claude_payload.get("session_policy"), str(claude_defaults["session_policy"])),
        "env_active": _optional_string(claude_payload.get("env_active")),
        "env": claude_env,
        "proxy": _proxy_config_from_payload(claude_payload.get("proxy"), "claude.proxy", proxy_fallback),
    }
    integrations = normalize_integrations_config(_object_value(payload.get("integrations"), "integrations"))

    return {
        "backend": backend,
        "runner_command": _optional_string(payload.get("runner_command")),
        "default_parallel": default_parallel,
        "default_mode": mode,
        "workspace_roots": _string_list(payload.get("workspace_roots"), "workspace_roots"),
        "webgame_workspace": _optional_string(payload.get("webgame_workspace")),
        "proxy": legacy_proxy,
        "context_windows": _object_value(payload.get("context_windows"), "context_windows"),
        "retention_policy": retention_policy_schedule_config(payload.get("retention_policy")),
        "integrations": integrations,
        "codex": codex,
        "claude": claude,
    }


def _preserve_existing_bootstrap_sections(config_file: Path, cfg: dict) -> dict:
    if not config_file.exists():
        return cfg
    try:
        existing = read_json(config_file)
    except (OSError, ValueError):
        return cfg
    existing_knowledge = existing.get("knowledge") if isinstance(existing, dict) else None
    if isinstance(existing_knowledge, dict):
        cfg["knowledge"] = existing_knowledge
    return cfg


def handle_save_bootstrap(root: Path, default_run_id: str, body: bytes) -> bytes:
    payload = parse_json_body(body)
    path = config_path(root)
    if path.exists() and not parse_optional_bool(payload.get("force", False), "force"):
        return json_response({"error": "AHA is already initialized"}, "409 Conflict")
    cfg = _preserve_existing_bootstrap_sections(path, _bootstrap_config_from_payload(payload))
    write_json(path, cfg)
    return json_response(bootstrap_payload(root, default_run_id), "201 Created")


def handle_observe_proxy_status(root: Path, default_run_id: str, method: str, query: dict[str, list[str]]) -> bytes:
    requested_run_id = request_run_id(default_run_id, query)
    run_id = requested_run_id if requested_run_id and run_exists(root, requested_run_id) else default_api_run_id(root, default_run_id)
    task_id = str((query.get("task_id") or query.get("taskId") or [""])[0] or "").strip()
    recent_limit = _query_int(query, "recent_limit", 20) if task_id else 0
    preview_chars = _query_int(query, "preview_chars", 2000) if task_id else 0
    cfg = load_config(root)
    status = observe_proxy_status(root, cfg)
    usage = observe_proxy_usage_summary(
        root,
        run_id,
        event_limit=recent_limit,
        preview_chars=preview_chars,
        include_recent=bool(task_id),
        recent_task_id=task_id or None,
    )
    response = json_response({"observe_proxy": {**status, "usage": usage}})
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
    try:
        execution_fields = parse_execution_fields(payload, default_collaboration_mode="auto")
    except ValueError as exc:
        return json_response({"error": str(exc)}, "400 Bad Request")

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

    explicit_proxy_enabled = parse_optional_bool(payload["proxy_enabled"], "proxy_enabled") if "proxy_enabled" in payload else None
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
        proxy_enabled=explicit_proxy_enabled,
        http_proxy=str(payload.get("http_proxy", "") or "") or None,
        https_proxy=str(payload.get("https_proxy", "") or "") or None,
        no_proxy=str(payload.get("no_proxy", "") or "") or None,
        collaboration_mode=execution_fields["collaboration_mode"],
        workflow_template=execution_fields["workflow_template"],
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


def handle_update_run_lifecycle(root: Path, default_run_id: str, run_id: str, body: bytes) -> bytes:
    payload = parse_json_body(body)
    status = str(payload.get("status", payload.get("lifecycle_status", "")) or "").strip()
    if not status:
        return json_response({"error": "lifecycle status is required"}, "400 Bad Request")
    current_run_id = str(payload.get("current_run_id", "") or "").strip() or default_run_id
    try:
        run = set_run_lifecycle_status(root, run_id, status, current_run_id=current_run_id)
    except RunLifecycleActionError as exc:
        return json_response({"error": str(exc), "reason": exc.reason}, exc.status_code)
    except ValueError as exc:
        return json_response({"error": str(exc)}, "400 Bad Request")
    response = runs_payload(root, default_run_id)
    response.update({"ok": True, "run": run})
    return json_response(response)


def handle_update_run_proxy(root: Path, default_run_id: str, run_id: str, body: bytes) -> bytes:
    payload = parse_json_body(body)
    try:
        proxy = update_run_proxy_config(root, run_id, **parse_task_proxy_fields(payload))
    except SystemExit as exc:
        return json_response({"error": str(exc)}, "404 Not Found")
    except ValueError as exc:
        return json_response({"error": str(exc)}, "400 Bad Request")
    response = runs_payload(root, default_run_id)
    response.update({"ok": True, "run": run_summary(root, run_id), "proxy": proxy})
    return json_response(response)


def handle_delete_run(root: Path, default_run_id: str, run_id: str, query: dict[str, list[str]]) -> bytes:
    force = parse_query_bool(query, "force", False)
    current_run_id = str(query.get("current_run_id", [""])[0] or "").strip() or default_run_id
    try:
        deleted = delete_run(root, run_id, current_run_id=current_run_id, force=force)
    except RunDeleteError as exc:
        return json_response({"error": str(exc), "reason": exc.reason}, exc.status_code)
    except ValueError as exc:
        return json_response({"error": str(exc)}, "400 Bad Request")
    response = runs_payload(root, default_run_id)
    response.update({"ok": True, "deleted": deleted})
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
    if method in {"GET", "HEAD"} and path == "/api/integrations/observe-proxy":
        return handle_observe_proxy_status(root, default_run_id, method, query)
    if method == "POST" and path == "/api/runs":
        return handle_create_run(root, body)
    if method in {"GET", "HEAD"} and path.startswith("/api/runs/"):
        route = path.removeprefix("/api/runs/").strip("/")
        parts = route.split("/")
        if len(parts) == 2 and parts[1] == "retention-archives":
            return handle_run_retention_archive_list(root, method, parts[0])
        if len(parts) == 3 and parts[1] == "retention-archives":
            return handle_run_retention_archive_inspect(root, method, parts[0], parts[2])
        if len(parts) == 2 and parts[1] == "retention":
            return handle_run_retention_visibility(root, method, parts[0], query)
        if len(parts) == 2 and parts[1] == "recovery":
            return handle_run_recovery_visibility(root, method, parts[0], query)
        if len(parts) == 2 and parts[1] == "maintenance":
            return handle_run_maintenance_visibility(root, method, parts[0], query)
    if method == "POST" and path.startswith("/api/runs/"):
        route = path.removeprefix("/api/runs/").strip("/")
        parts = route.split("/")
        if len(parts) == 2 and parts[1] == "retention":
            return handle_run_retention_action(root, default_run_id, parts[0], body)
        if len(parts) == 2 and parts[1] == "recovery":
            return handle_run_recovery_action(root, parts[0], body)
        if len(parts) == 4 and parts[1] == "retention-archives" and parts[3] == "restore":
            return handle_run_retention_archive_restore(root, default_run_id, parts[0], body, parts[2])
        if len(parts) == 3 and parts[1] == "retention-archive" and parts[2] == "restore":
            return handle_run_retention_archive_restore(root, default_run_id, parts[0], body)
    if method in {"POST", "PATCH"} and path.startswith("/api/runs/"):
        route = path.removeprefix("/api/runs/").strip("/")
        parts = route.split("/")
        if len(parts) == 2 and parts[1] == "lifecycle":
            return handle_update_run_lifecycle(root, default_run_id, parts[0], body)
        if len(parts) == 2 and parts[1] == "proxy":
            return handle_update_run_proxy(root, default_run_id, parts[0], body)
    if method == "DELETE" and path.startswith("/api/runs/"):
        run_id = path.removeprefix("/api/runs/").strip("/")
        if not run_id or "/" in run_id:
            return json_response({"error": "run id is required"}, "400 Bad Request")
        return handle_delete_run(root, default_run_id, run_id, query)
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
    "handle_update_run_lifecycle",
    "handle_delete_run",
    "handle_save_bootstrap",
    "handle_run_export",
    "handle_run_import",
    "handle_run_retention_action",
    "handle_run_retention_archive_inspect",
    "handle_run_retention_archive_list",
    "handle_run_retention_archive_restore",
    "handle_run_maintenance_visibility",
    "handle_run_recovery_action",
    "handle_run_recovery_visibility",
    "handle_run_retention_visibility",
    "handle_run_workspace_route",
    "handle_runs_index",
    "handle_workspaces_index",
]
