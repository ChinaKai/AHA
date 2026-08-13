from __future__ import annotations

from pathlib import Path
import json
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import unquote
from urllib.request import Request, urlopen

from aha_cli.backends.claude import CLAUDE_ENV_GROUP_FIELDS
from aha_cli.backends.registry import agent_backend_names, agent_backend_or_default, normalize_reasoning_effort
from aha_cli.domain.models import default_config, normalize_integrations_config
from aha_cli.services.observe_proxy import observe_proxy_status, observe_proxy_usage_summary
from aha_cli.services.provider_config import (
    normalize_configured_models,
    normalize_providers,
    provider_by_id,
    sync_legacy_backend_env,
)
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
CLAUDE_ENV_GROUP_ALIASES = {
    "ANTHROPIC_BASE_URL": ("ANTHROPIC_BASE_URL", "base_url"),
    "ANTHROPIC_MODEL": ("ANTHROPIC_MODEL", "model"),
    "ANTHROPIC_API_KEY": ("ANTHROPIC_API_KEY", "api_key"),
    "ANTHROPIC_AUTH_TOKEN": ("ANTHROPIC_AUTH_TOKEN", "auth_token"),
    "ANTHROPIC_DEFAULT_FABLE_MODEL": ("ANTHROPIC_DEFAULT_FABLE_MODEL", "fable_model"),
    "ANTHROPIC_DEFAULT_OPUS_MODEL": ("ANTHROPIC_DEFAULT_OPUS_MODEL", "opus_model"),
    "ANTHROPIC_DEFAULT_SONNET_MODEL": ("ANTHROPIC_DEFAULT_SONNET_MODEL", "sonnet_model"),
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": (
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "haiku_model",
    ),
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": (
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "context_window",
    ),
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


def _extract_models(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        data = payload.get("models")
    if not isinstance(data, list):
        return []
    models: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in data:
        if isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or "").strip()
        elif isinstance(item, str):
            model_id = item.strip()
        else:
            model_id = ""
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        max_input = item.get("max_input_tokens") if isinstance(item, dict) else None
        max_output = item.get("max_output_tokens") if isinstance(item, dict) else None
        mode = item.get("mode") if isinstance(item, dict) else None
        entry: dict[str, object] = {"id": model_id}
        if isinstance(max_input, int) and max_input > 0:
            entry["max_input_tokens"] = max_input
        if isinstance(max_output, int) and max_output > 0:
            entry["max_output_tokens"] = max_output
        if isinstance(mode, str) and mode.strip():
            entry["mode"] = mode.strip()
        models.append(entry)
    return models


def _detect_gateway_models(
    base_url: str,
    api_key: str,
    auth_token: str,
    timeout: int = 15,
    auth_style: str = "auto",
) -> tuple[list[dict[str, object]], str]:
    """Query a gateway's /v1/models (OpenAI-style) or /models (Anthropic-style) endpoint.

    Tries several URL/auth combinations because gateways vary: some want
    ``Authorization: Bearer``, others want ``x-api-key`` (Anthropic-style).
    Returns the first non-empty model list found (deduplicated in order) plus
    the auth style that succeeded ("bearer", "x-api-key", or "none").
    """
    key = (auth_token or api_key or "").strip()
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("base_url is required")
    candidates: list[str] = []
    if base.endswith("/v1"):
        candidates.append(f"{base}/models")
    else:
        candidates.append(f"{base}/v1/models")
        candidates.append(f"{base}/models")
    normalized_auth_style = str(auth_style or "auto").strip().lower()
    auth_sets: list[tuple[str, dict[str, str]]] = []
    if normalized_auth_style in {"auto", "bearer"} and key:
        auth_sets.append(("bearer", {"Authorization": f"Bearer {key}"}))
    if normalized_auth_style in {"auto", "x-api-key"} and key:
        auth_sets.append(("x-api-key", {"x-api-key": key, "anthropic-version": "2023-06-01"}))
    if normalized_auth_style in {"auto", "none"}:
        auth_sets.append(("none", {}))
    if not auth_sets:
        raise ValueError("provider credential is not configured")
    last_error = ""
    for endpoint in candidates:
        for auth_style, auth_headers in auth_sets:
            try:
                request = Request(endpoint, headers=auth_headers, method="GET")
                with urlopen(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8") or "{}")
                models = _extract_models(payload)
                if models:
                    return models, auth_style
            except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
                last_error = f"{endpoint}: {exc}"
                continue
    if last_error:
        raise ValueError(f"failed to detect models: {last_error}")
    return [], "none"


def _test_gateway_model(
    base_url: str,
    api_key: str,
    auth_token: str,
    model: str,
    auth_style: str,
    *,
    backend: str = "claude",
    wire_api: str = "",
    timeout: int = 10,
) -> None:
    """Send a minimal probe request using the selected backend's runtime protocol.

    Codex providers default to the Responses API. Claude x-api-key gateways use
    Anthropic Messages, while Bearer gateways retain OpenAI chat compatibility.
    Raises ValueError on failure.
    """
    key = (auth_token or api_key or "").strip()
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("base_url is required")
    if not model:
        raise ValueError("model is required")
    if not key:
        raise ValueError("api_key or auth_token is required")
    normalized_backend = str(backend or "").strip().lower()
    normalized_wire_api = str(wire_api or "").strip().lower()
    body: dict[str, object]
    if normalized_backend == "codex" and normalized_wire_api != "chat":
        endpoint = f"{base}/v1/responses" if not base.endswith("/v1") else f"{base}/responses"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {"model": model, "input": "ping", "max_output_tokens": 1}
    elif auth_style == "x-api-key":
        endpoint = f"{base}/v1/messages" if not base.endswith("/v1") else f"{base}/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        body = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
    else:
        endpoint = f"{base}/v1/chat/completions" if not base.endswith("/v1") else f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    try:
        request = Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            if status >= 400:
                raise ValueError(f"gateway returned HTTP {status}")
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"model probe failed: {exc}") from exc


def _saved_provider(root: Path, payload: dict) -> dict:
    provider_id = str(payload.get("provider_id") or "").strip()
    if not provider_id:
        raise ValueError("provider_id is required")
    provider = provider_by_id(load_config(root), provider_id)
    if not provider:
        raise ValueError("provider not found")
    return provider


def _provider_auth_headers(provider: dict, wire_api: str) -> dict[str, str]:
    credential = str(provider.get("credential") or "").strip()
    auth_style = str(provider.get("auth_style") or "auto").strip()
    if auth_style == "auto":
        auth_style = "x-api-key" if wire_api == "anthropic_messages" else "bearer"
    headers = {"Content-Type": "application/json"}
    if auth_style == "bearer" and credential:
        headers["Authorization"] = f"Bearer {credential}"
    elif auth_style == "x-api-key" and credential:
        headers["x-api-key"] = credential
    if wire_api == "anthropic_messages":
        headers["anthropic-version"] = "2023-06-01"
    return headers


def _anthropic_base_candidates(base_url: str) -> list[str]:
    """Candidate bases whose ``/v1/messages`` may serve the Anthropic protocol.

    Some gateways (DeepSeek, MiniMax, Kimi) expose their OpenAI-compatible API
    at the provider base while serving Anthropic Messages under an
    ``/anthropic`` suffix. Return the primary base first, then the ``/anthropic``
    variant when it differs.
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return []
    if base.endswith("/anthropic"):
        return [base]
    root = base[:-3] if base.endswith("/v1") else base
    candidates = [base]
    anthropic = f"{root}/anthropic"
    if anthropic != base:
        candidates.append(anthropic)
    return candidates


def _probe_anthropic_messages(provider: dict, model: str, timeout: int = 10) -> tuple[dict[str, object], str]:
    """Probe the Anthropic Messages interface, falling back to ``/anthropic``.

    Returns ``(status, anthropic_base_url)``. The ``/anthropic`` base is only
    tried when the primary base reports the endpoint as missing
    (``unsupported``), so auth/rate-limit results on the primary base are
    reported as-is without extra requests.
    """
    candidates = _anthropic_base_candidates(str(provider.get("base_url") or ""))
    if not candidates:
        return _probe_status(provider, model, "anthropic_messages", timeout=timeout), ""
    primary = _probe_status({**provider, "base_url": candidates[0]}, model, "anthropic_messages", timeout=timeout)
    if primary.get("status") != "unsupported" or len(candidates) == 1:
        return primary, ""
    for candidate in candidates[1:]:
        status = _probe_status({**provider, "base_url": candidate}, model, "anthropic_messages", timeout=timeout)
        if status.get("status") == "supported":
            return status, candidate
    return primary, ""


def _protocol_probe_request(provider: dict, model: str, wire_api: str) -> Request:
    base = str(provider.get("base_url") or "").strip().rstrip("/")
    prefix = base if base.endswith("/v1") else f"{base}/v1"
    if wire_api == "responses":
        endpoint = f"{prefix}/responses"
        body: dict[str, object] = {"model": model, "input": "ping", "max_output_tokens": 1}
    elif wire_api == "chat_completions":
        endpoint = f"{prefix}/chat/completions"
        body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    else:
        endpoint = f"{prefix}/messages"
        body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    return Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers=_provider_auth_headers(provider, wire_api),
        method="POST",
    )


def _probe_status(provider: dict, model: str, wire_api: str, timeout: int = 10) -> dict[str, object]:
    try:
        with urlopen(_protocol_probe_request(provider, model, wire_api), timeout=timeout) as response:
            status_code = int(getattr(response, "status", 200) or 200)
        if 200 <= status_code < 300:
            return {"status": "supported", "http_status": status_code}
        return {"status": "inconclusive", "http_status": status_code}
    except HTTPError as exc:
        status_code = int(exc.code or 0)
        if status_code in {401, 403}:
            status = "auth_error"
        elif status_code == 429:
            status = "rate_limited"
        elif status_code in {404, 405, 501}:
            status = "unsupported"
        elif status_code >= 500:
            status = "unavailable"
        else:
            status = "inconclusive"
        return {"status": status, "http_status": status_code}
    except (URLError, TimeoutError, OSError):
        return {"status": "unavailable"}
    except (ValueError, json.JSONDecodeError):
        return {"status": "inconclusive"}


def handle_detect_models(root: Path, body: bytes) -> bytes:
    payload = parse_json_body(body)
    try:
        provider = _saved_provider(root, payload)
    except ValueError as exc:
        return json_response({"error": str(exc)}, "404 Not Found" if "not found" in str(exc) else "400 Bad Request")
    credential = str(provider.get("credential") or "").strip()
    configured_auth_style = str(provider.get("auth_style") or "auto").strip()
    if configured_auth_style not in {"auto", "none"} and not credential:
        return json_response({"error": "provider credential is not configured"}, "400 Bad Request")
    try:
        models, detected_auth_style = _detect_gateway_models(
            str(provider.get("base_url") or ""),
            credential,
            credential,
            auth_style=configured_auth_style,
        )
    except ValueError:
        return json_response({"error": "failed to detect provider models"}, "502 Bad Gateway")
    if not models:
        return json_response({"error": "no models found"}, "502 Bad Gateway")
    return json_response({
        "provider_id": provider["id"],
        "auth_style": detected_auth_style,
        "models": models,
    })


def handle_detect_model_test(root: Path, body: bytes) -> bytes:
    payload = parse_json_body(body)
    try:
        provider = _saved_provider(root, payload)
    except ValueError as exc:
        return json_response({"error": str(exc)}, "404 Not Found" if "not found" in str(exc) else "400 Bad Request")
    detected_auth_style = str(payload.get("auth_style") or "").strip().lower()
    if provider.get("auth_style") == "auto" and detected_auth_style in {"bearer", "x-api-key", "none"}:
        provider = {**provider, "auth_style": detected_auth_style}
    raw_models = payload.get("models", payload.get("model_ids", payload.get("model")))
    if isinstance(raw_models, str):
        selected_models = [raw_models.strip()] if raw_models.strip() else []
    elif isinstance(raw_models, list):
        selected_models = [str(item).strip() for item in raw_models if str(item or "").strip()]
    else:
        selected_models = []
    if not selected_models:
        return json_response({"error": "at least one model is required"}, "400 Bad Request")
    def _probe_one_model(model: str) -> dict[str, object]:
        capabilities = {
            wire_api: _probe_status(provider, model, wire_api)
            for wire_api in ("responses", "chat_completions")
        }
        messages_status, anthropic_base_url = _probe_anthropic_messages(provider, model)
        capabilities["anthropic_messages"] = messages_status
        result: dict[str, object] = {"model_id": model, "capabilities": capabilities}
        if anthropic_base_url:
            result["anthropic_base_url"] = anthropic_base_url
        return result

    unique_models = list(dict.fromkeys(selected_models))
    if len(unique_models) > 1:
        # Probe models concurrently so a slow/failing model does not block the
        # others; each probe already catches transport/auth errors per status.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(8, len(unique_models))) as executor:
            results = list(executor.map(_probe_one_model, unique_models))
    else:
        results = [_probe_one_model(unique_models[0])] if unique_models else []
    return json_response({"provider_id": provider["id"], "results": results})


def _optional_string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _string_or_default(value: object, default: str) -> str:
    return str(value or "").strip() or default


MODEL_SOURCE_OPTIONS = {"both", "official", "env"}


def _model_source(value: object, default: str = "both") -> str:
    text = str(value or "").strip().lower()
    return text if text in MODEL_SOURCE_OPTIONS else default


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


def _validate_claude_env_group(group: dict) -> None:
    if group.get("ANTHROPIC_API_KEY") and group.get("ANTHROPIC_AUTH_TOKEN"):
        raise ValueError("claude.env group cannot set both ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN")
    context = str(group.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") or "").strip()
    if context and (not context.isdigit() or int(context) <= 0):
        raise ValueError("claude.env context window must be a positive integer")


def _claude_env_groups(value: object) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, dict):
        legacy = {"name": "default"}
        for key in CLAUDE_ENV_GROUP_FIELDS:
            field_value = next((str(value.get(alias) or "").strip() for alias in CLAUDE_ENV_GROUP_ALIASES[key] if value.get(alias)), "")
            if field_value:
                legacy[key] = field_value
        if len(legacy) == 1:
            return []
        _validate_claude_env_group(legacy)
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
            field_value = next(
                (str(item.get(alias) or "").strip() for alias in CLAUDE_ENV_GROUP_ALIASES[key] if item.get(alias)),
                "",
            )
            if field_value:
                group[key] = field_value
        _validate_claude_env_group(group)
        if raw_name or len(group) > 1:
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


def _reasoning_effort(value: object, backend: str) -> str | None:
    try:
        return normalize_reasoning_effort(value, backend)
    except ValueError as exc:
        raise ValueError(f"unknown {backend} reasoning_effort: {value}") from exc


def _proxy_config_from_payload(value: object, field_name: str, fallback: dict | None = None) -> dict:
    fallback = fallback or {}
    payload = _object_value(value, field_name)
    return normalize_proxy_config(
        payload.get("enabled", payload.get("proxy_enabled", fallback.get("enabled", False))),
        payload.get("http_proxy", fallback.get("http_proxy")),
        payload.get("https_proxy", fallback.get("https_proxy")),
        payload.get("no_proxy", fallback.get("no_proxy")),
    )


def _backend_proxy_switch_from_payload(value: object, field_name: str, fallback: bool = False) -> dict:
    payload = _object_value(value, field_name)
    return {
        "enabled": parse_optional_bool(
            payload.get("enabled", payload.get("proxy_enabled", fallback)),
            f"{field_name}.enabled",
        )
    }


def _bootstrap_config_from_payload(payload: dict, existing_config: dict | None = None) -> dict:
    defaults = default_config()
    existing_config = existing_config if isinstance(existing_config, dict) else {}
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
    shared_proxy = _proxy_config_from_payload(payload.get("proxy"), "proxy")
    codex_input_proxy = _proxy_config_from_payload(codex_payload.get("proxy"), "codex.proxy")
    claude_payload = _object_value(payload.get("claude"), "claude")
    claude_input_proxy = _proxy_config_from_payload(claude_payload.get("proxy"), "claude.proxy")
    if not proxy_configured(shared_proxy):
        candidates = [codex_input_proxy, claude_input_proxy]
        if backend == "claude":
            candidates.reverse()
        shared_proxy = next((candidate for candidate in candidates if proxy_configured(candidate)), shared_proxy)
    codex = {
        "bin": _string_or_default(codex_payload.get("bin"), str(codex_defaults["bin"])),
        "model": _optional_string(codex_payload.get("model")),
        "reasoning_effort": _reasoning_effort(codex_payload.get("reasoning_effort"), "codex"),
        "sandbox": _config_sandbox(codex_payload.get("sandbox"), str(codex_defaults["sandbox"])),
        "approval": _string_or_default(codex_payload.get("approval"), str(codex_defaults["approval"])),
        "json": parse_optional_bool(codex_payload.get("json", codex_defaults["json"]), "codex.json"),
        "session_policy": _session_policy(codex_payload.get("session_policy"), str(codex_defaults["session_policy"])),
        "env_active": _optional_string(codex_payload.get("env_active")),
        "model_source": _model_source(codex_payload.get("model_source"), str(codex_defaults.get("model_source", "both"))),
        "env": codex_env,
        "proxy": _backend_proxy_switch_from_payload(
            codex_payload.get("proxy"),
            "codex.proxy",
            bool(shared_proxy.get("enabled")),
        ),
    }
    if codex["approval"] not in APPROVAL_OPTIONS:
        raise ValueError(f"unknown approval: {codex['approval']}")

    claude_defaults = defaults["claude"]
    claude_env = _claude_env_groups(claude_payload.get("env"))
    claude = {
        "bin": _string_or_default(claude_payload.get("bin"), str(claude_defaults["bin"])),
        "model": _optional_string(claude_payload.get("model")),
        "reasoning_effort": _reasoning_effort(claude_payload.get("reasoning_effort"), "claude"),
        "sandbox": _config_sandbox(claude_payload.get("sandbox"), str(claude_defaults["sandbox"])),
        "permission_mode": _optional_string(claude_payload.get("permission_mode")),
        "session_policy": _session_policy(claude_payload.get("session_policy"), str(claude_defaults["session_policy"])),
        "env_active": _optional_string(claude_payload.get("env_active")),
        "model_source": _model_source(claude_payload.get("model_source"), str(claude_defaults.get("model_source", "both"))),
        "env": claude_env,
        "proxy": _backend_proxy_switch_from_payload(
            claude_payload.get("proxy"),
            "claude.proxy",
            bool(shared_proxy.get("enabled")),
        ),
    }
    integrations = normalize_integrations_config(_object_value(payload.get("integrations"), "integrations"))
    provider_input = payload.get("providers") if "providers" in payload else existing_config.get("providers", [])
    providers = normalize_providers(provider_input, existing_config.get("providers", []))
    configured_model_input = (
        payload.get("configured_models")
        if "configured_models" in payload
        else existing_config.get("configured_models", [])
    )
    configured_models = normalize_configured_models(
        configured_model_input,
        (str(item.get("id") or "") for item in providers),
    )

    return {
        "backend": backend,
        "runner_command": _optional_string(payload.get("runner_command")),
        "default_parallel": default_parallel,
        "default_mode": mode,
        "workspace_roots": _string_list(payload.get("workspace_roots"), "workspace_roots"),
        "webgame_workspace": _optional_string(payload.get("webgame_workspace")),
        "proxy": shared_proxy,
        "context_windows": _object_value(payload.get("context_windows"), "context_windows"),
        "retention_policy": retention_policy_schedule_config(payload.get("retention_policy")),
        "integrations": integrations,
        "providers": providers,
        "configured_models": configured_models,
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
    existing = load_config(root) if path.exists() else {}
    cfg = _preserve_existing_bootstrap_sections(path, _bootstrap_config_from_payload(payload, existing))
    sync_legacy_backend_env(cfg)
    write_json(path, cfg)
    return json_response(bootstrap_payload(root, default_run_id), "201 Created")


def handle_observe_proxy_status(root: Path, default_run_id: str, method: str, query: dict[str, list[str]]) -> bytes:
    requested_run_id = request_run_id(default_run_id, query)
    run_id = requested_run_id if requested_run_id and run_exists(root, requested_run_id) else default_api_run_id(root, default_run_id)
    task_id = str((query.get("task_id") or query.get("taskId") or [""])[0] or "").strip()
    request_id = str((query.get("request_id") or query.get("requestId") or [""])[0] or "").strip()
    full_body = parse_optional_bool(str((query.get("full") or ["false"])[0] or "false"), "full")
    recent_limit = 1 if request_id else (_query_int(query, "recent_limit", 20) if task_id else 0)
    preview_chars = None if full_body and task_id else (_query_int(query, "preview_chars", 2000) if task_id else 0)
    cfg = load_config(root)
    status = observe_proxy_status(root, cfg)
    usage = observe_proxy_usage_summary(
        root,
        run_id,
        event_limit=recent_limit,
        preview_chars=preview_chars,
        include_recent=bool(task_id),
        recent_task_id=task_id or None,
        recent_request_id=request_id or None,
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
    if method == "POST" and path == "/api/detect-models":
        return handle_detect_models(root, body)
    if method == "POST" and path == "/api/detect-models/test":
        return handle_detect_model_test(root, body)
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
    "handle_detect_models",
    "handle_detect_model_test",
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
