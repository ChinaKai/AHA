from __future__ import annotations

from urllib.parse import urlparse

from aha_cli.domain.models import (
    DEFAULT_TASK_CONTEXT_THRESHOLD_PERCENT,
    DEFAULT_TASK_SUPERVISION_MAX_ROUNDS,
    TASK_HARDWARE_DEBUG_ACCESS_MODES,
    TASK_HARDWARE_DEBUG_PERMISSION_KEYS,
    TASK_HARDWARE_DEBUG_MODES,
    TASK_BROWSER_ACCESS_MODES,
    TASK_BROWSER_CHANNEL_MODES,
    TASK_BROWSER_CONTROL_MODES,
    TASK_BROWSER_DEVICE_MODES,
    TASK_BROWSER_MODE_VALUES,
    TASK_BROWSER_DISPLAY_MODES,
    TASK_BROWSER_PROFILE_MODES,
    TASK_BROWSER_PROXY_MODES,
    TASK_BROWSER_RUNTIME_MODES,
    TASK_BROWSER_TRANSFER_MODES,
    TASK_SUPERVISION_ASK_USER_GATES,
    normalize_browser_profile_name,
)
from aha_cli.services.auto_context_compact import start_backend_after_auto_compact as start_backend
from aha_cli.services.backend_runtime import backend_status, stop_backend
from aha_cli.web.http_utils import parse_optional_bool
from aha_cli.web.task_commands import (
    compact_reset_selected_agent,
    complete_selected_task,
    finalization_prompt,
    format_agent_command,
    format_aha_command,
    format_task_journal_for_prompt,
    handle_slash_command,
    interrupt_selected_agent,
    record_task_checkpoint,
    reopen_selected_task,
    request_task_finalization,
)
from aha_cli.web.task_messaging import (
    ensure_chat_offset_before_message,
    handle_send_payload,
    is_supervision_host_message,
    is_task_supervision_host_target,
    message_backend_autostart_config,
    realtime_debug_log,
    save_chat_offset_after_message,
    task_locked_for_messages,
)
from aha_cli.web.task_runtime import (
    prepare_task_main_autostart,
    request_task_finalization_with_backend,
    start_dispatched_task_backend,
    start_prepared_backend,
)


def parse_task_proxy_fields(payload: dict) -> dict[str, object]:
    update: dict[str, object] = {}
    if "proxy_enabled" in payload:
        update["proxy_enabled"] = parse_optional_bool(payload.get("proxy_enabled"), "proxy_enabled")
    if "http_proxy" in payload:
        update["http_proxy"] = str(payload.get("http_proxy") or "") or None
    if "https_proxy" in payload:
        update["https_proxy"] = str(payload.get("https_proxy") or "") or None
    if "no_proxy" in payload:
        update["no_proxy"] = str(payload.get("no_proxy") or "") or None
    return update


def parse_task_supervision_fields(payload: dict) -> dict[str, object]:
    update: dict[str, object] = {}
    if "mode" in payload:
        mode = str(payload.get("mode") or "manual")
        if mode not in {"manual", "assisted"}:
            raise ValueError(f"unknown supervision mode: {mode}")
        update["mode"] = mode
    if "host_backend" in payload:
        update["host_backend"] = str(payload.get("host_backend") or "stub")
    if "host_model" in payload:
        update["host_model"] = str(payload.get("host_model") or "") or None
    if "host_proxy_enabled" in payload:
        update["host_proxy_enabled"] = parse_optional_bool(payload.get("host_proxy_enabled"), "host_proxy_enabled")
    if "host_agent_id" in payload:
        update["host_agent_id"] = str(payload.get("host_agent_id") or "host")
    if "real_agent_enabled" in payload:
        update["real_agent_enabled"] = parse_optional_bool(payload.get("real_agent_enabled"), "real_agent_enabled")
    if "channel" in payload:
        channel = str(payload.get("channel") or "main_only")
        if channel not in {"main_only", "host_visible"}:
            raise ValueError(f"unknown supervision channel: {channel}")
        update["channel"] = channel
    if "max_rounds" in payload:
        update["max_rounds"] = max(1, int(payload.get("max_rounds") or DEFAULT_TASK_SUPERVISION_MAX_ROUNDS))
    if "ask_user_gates" in payload:
        gates = payload.get("ask_user_gates")
        if not isinstance(gates, dict):
            raise ValueError("ask_user_gates must be an object")
        update["ask_user_gates"] = {
            key: parse_optional_bool(gates.get(key), key) if key in gates else False
            for key in TASK_SUPERVISION_ASK_USER_GATES
        }
    return update


def parse_task_context_management_fields(payload: dict) -> dict[str, object]:
    update: dict[str, object] = {}
    if "auto_compact_enabled" in payload:
        update["auto_compact_enabled"] = parse_optional_bool(payload.get("auto_compact_enabled"), "auto_compact_enabled")
    elif "enabled" in payload:
        update["auto_compact_enabled"] = parse_optional_bool(payload.get("enabled"), "enabled")
    if "auto_compact_threshold_percent" in payload:
        update["auto_compact_threshold_percent"] = max(1, min(99, int(payload.get("auto_compact_threshold_percent") or DEFAULT_TASK_CONTEXT_THRESHOLD_PERCENT)))
    elif "threshold_percent" in payload:
        update["auto_compact_threshold_percent"] = max(1, min(99, int(payload.get("threshold_percent") or DEFAULT_TASK_CONTEXT_THRESHOLD_PERCENT)))
    return update


def parse_task_token_saving_fields(payload: dict) -> dict[str, object]:
    update: dict[str, object] = {}
    if "enabled" in payload:
        update["enabled"] = parse_optional_bool(payload.get("enabled"), "enabled")
    elif "token_saving_enabled" in payload:
        update["enabled"] = parse_optional_bool(payload.get("token_saving_enabled"), "token_saving_enabled")
    if "provider" in payload:
        update["provider"] = str(payload.get("provider") or "nav")
    return update


def parse_task_observe_proxy_fields(payload: dict) -> dict[str, object]:
    update: dict[str, object] = {}
    if "enabled" in payload:
        update["enabled"] = parse_optional_bool(payload.get("enabled"), "enabled")
    elif "observe_proxy_enabled" in payload:
        update["enabled"] = parse_optional_bool(payload.get("observe_proxy_enabled"), "observe_proxy_enabled")
    return update


def parse_task_hardware_debug_fields(payload: dict) -> dict[str, object]:
    update: dict[str, object] = {}
    if "mode" in payload:
        mode = str(payload.get("mode") or "off").strip().lower()
        if mode not in TASK_HARDWARE_DEBUG_MODES:
            raise ValueError(f"mode must be one of: {', '.join(TASK_HARDWARE_DEBUG_MODES)}")
        update["mode"] = mode
    for key in ("serial", "network", "credentials"):
        if key not in payload:
            continue
        value = payload.get(key)
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be an object")
        update[key] = value
    mode = update.get("mode")
    if mode in {"serial", "both"}:
        serial = update.get("serial") or {}
        if not str(serial.get("device") or serial.get("port") or "").strip():
            raise ValueError("serial.device is required for serial hardware debug")
    if mode in {"network", "both"}:
        network = update.get("network") or {}
        if not str(network.get("device_ip") or network.get("host") or "").strip():
            raise ValueError("network.device_ip is required for network hardware debug")
    if "channels" in payload:
        channels = payload.get("channels")
        if not isinstance(channels, (list, dict)):
            raise ValueError("channels must be a list or object")
        update["channels"] = channels
    if "enabled" in payload:
        update["enabled"] = parse_optional_bool(payload.get("enabled"), "enabled")
    elif "hardware_debug_enabled" in payload:
        update["enabled"] = parse_optional_bool(payload.get("hardware_debug_enabled"), "hardware_debug_enabled")
    if "devices" in payload:
        devices = payload.get("devices")
        if not isinstance(devices, (list, dict)):
            raise ValueError("devices must be a list or object")
        update["devices"] = devices
    if "permissions" in payload:
        permissions = payload.get("permissions")
        if not isinstance(permissions, dict):
            raise ValueError("permissions must be an object")
        update["permissions"] = {}
        if "access" in permissions:
            access = str(permissions.get("access") or "").strip().lower().replace("-", "_")
            if access not in TASK_HARDWARE_DEBUG_ACCESS_MODES:
                raise ValueError(f"permissions.access must be one of: {', '.join(TASK_HARDWARE_DEBUG_ACCESS_MODES)}")
            update["permissions"]["access"] = access
        update["permissions"].update({
            key: parse_optional_bool(permissions.get(key), key)
            for key in TASK_HARDWARE_DEBUG_PERMISSION_KEYS
            if key in permissions
        })
        for old_key, new_key in {"serial_read": "read", "serial_write": "write"}.items():
            if old_key in permissions:
                update["permissions"][new_key] = parse_optional_bool(permissions.get(old_key), old_key)
    return update


def parse_task_browser_control_fields(payload: dict) -> dict[str, object]:
    update: dict[str, object] = {}
    if "mode" in payload:
        mode = str(payload.get("mode") or "off").strip().lower()
        if mode not in TASK_BROWSER_CONTROL_MODES:
            raise ValueError(f"mode must be one of: {', '.join(TASK_BROWSER_CONTROL_MODES)}")
        update["mode"] = mode
    if "start_url" in payload:
        start_url = str(payload.get("start_url") or "").strip()
        if start_url:
            parsed = urlparse(start_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("start_url must be an http or https URL")
        update["start_url"] = start_url
    if "agent_access" in payload:
        access = str(payload.get("agent_access") or "").strip().lower().replace("-", "_")
        if access not in TASK_BROWSER_ACCESS_MODES:
            raise ValueError(f"agent_access must be one of: {', '.join(TASK_BROWSER_ACCESS_MODES)}")
        update["agent_access"] = access
    if "runtime" in payload:
        runtime = str(payload.get("runtime") or "").strip().lower()
        if runtime not in TASK_BROWSER_RUNTIME_MODES:
            raise ValueError(f"runtime must be one of: {', '.join(TASK_BROWSER_RUNTIME_MODES)}")
        update["runtime"] = runtime
    if "profile" in payload:
        profile = str(payload.get("profile") or "").strip().lower()
        if profile not in TASK_BROWSER_PROFILE_MODES:
            raise ValueError(f"profile must be one of: {', '.join(TASK_BROWSER_PROFILE_MODES)}")
        update["profile"] = profile
        if profile == "named" and not normalize_browser_profile_name(payload.get("profile_name")):
            raise ValueError("profile_name is required when profile is named")
    if "profile_name" in payload:
        profile_name = normalize_browser_profile_name(payload.get("profile_name"))
        if str(payload.get("profile_name") or "").strip() and not profile_name:
            raise ValueError("profile_name must be 1-80 printable characters")
        update["profile_name"] = profile_name
    if "channel" in payload:
        channel = str(payload.get("channel") or "auto").strip().lower()
        if channel not in TASK_BROWSER_CHANNEL_MODES:
            raise ValueError(f"channel must be one of: {', '.join(TASK_BROWSER_CHANNEL_MODES)}")
        update["channel"] = channel
    if "browser_mode" in payload:
        browser_mode = str(payload.get("browser_mode") or "privacy").strip().lower()
        if browser_mode not in TASK_BROWSER_MODE_VALUES:
            raise ValueError(f"browser_mode must be one of: {', '.join(TASK_BROWSER_MODE_VALUES)}")
        update["browser_mode"] = browser_mode
    if "display" in payload:
        display = str(payload.get("display") or "").strip().lower()
        if display not in TASK_BROWSER_DISPLAY_MODES:
            raise ValueError(f"display must be one of: {', '.join(TASK_BROWSER_DISPLAY_MODES)}")
        update["display"] = display
    if "device_mode" in payload:
        device_mode = str(payload.get("device_mode") or "").strip().lower()
        if device_mode not in TASK_BROWSER_DEVICE_MODES:
            raise ValueError(f"device_mode must be one of: {', '.join(TASK_BROWSER_DEVICE_MODES)}")
        update["device_mode"] = device_mode
    if "allowed_hosts" in payload:
        allowed_hosts = payload.get("allowed_hosts")
        if not isinstance(allowed_hosts, (list, str)):
            raise ValueError("allowed_hosts must be a list or newline/comma-separated string")
        update["allowed_hosts"] = allowed_hosts
    for key in ("downloads", "uploads"):
        if key not in payload:
            continue
        mode = str(payload.get(key) or "deny").strip().lower()
        if mode not in TASK_BROWSER_TRANSFER_MODES:
            raise ValueError(f"{key} must be one of: {', '.join(TASK_BROWSER_TRANSFER_MODES)}")
        update[key] = mode
    if "proxy_mode" in payload:
        proxy_mode = str(payload.get("proxy_mode") or "direct").strip().lower()
        if proxy_mode not in TASK_BROWSER_PROXY_MODES:
            raise ValueError(f"proxy_mode must be one of: {', '.join(TASK_BROWSER_PROXY_MODES)}")
        update["proxy_mode"] = proxy_mode
    if "proxy_server" in payload:
        proxy_server = str(payload.get("proxy_server") or "").strip()
        if proxy_server:
            parsed = urlparse(proxy_server)
            if (
                parsed.scheme not in {"http", "https", "socks4", "socks5"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("proxy_server must be an HTTP(S) or SOCKS proxy URL without credentials")
        update["proxy_server"] = proxy_server
    for key in ("proxy_bypass", "proxy_username"):
        if key in payload:
            update[key] = str(payload.get(key) or "").strip()
    if "proxy_password" in payload:
        update["proxy_password"] = str(payload.get("proxy_password") or "")
    if "clear_proxy_password" in payload:
        update["clear_proxy_password"] = parse_optional_bool(
            payload.get("clear_proxy_password"),
            "clear_proxy_password",
        )
    return update


def parse_task_skills_fields(payload: dict) -> dict[str, object]:
    update: dict[str, object] = {}
    if "enabled_paths" in payload:
        enabled_paths = payload.get("enabled_paths")
    elif "skill_paths" in payload:
        enabled_paths = payload.get("skill_paths")
    elif "paths" in payload:
        enabled_paths = payload.get("paths")
    elif "skills" in payload:
        enabled_paths = payload.get("skills")
    else:
        return update
    if not isinstance(enabled_paths, (list, str)):
        raise ValueError("enabled_paths must be a list or newline/comma-separated string")
    update["enabled_paths"] = enabled_paths
    return update


__all__ = [
    "backend_status",
    "compact_reset_selected_agent",
    "complete_selected_task",
    "ensure_chat_offset_before_message",
    "finalization_prompt",
    "format_agent_command",
    "format_aha_command",
    "format_task_journal_for_prompt",
    "handle_send_payload",
    "handle_slash_command",
    "interrupt_selected_agent",
    "is_supervision_host_message",
    "is_task_supervision_host_target",
    "message_backend_autostart_config",
    "parse_task_proxy_fields",
    "parse_task_context_management_fields",
    "parse_task_token_saving_fields",
    "parse_task_observe_proxy_fields",
    "parse_task_browser_control_fields",
    "parse_task_hardware_debug_fields",
    "parse_task_supervision_fields",
    "parse_task_skills_fields",
    "prepare_task_main_autostart",
    "realtime_debug_log",
    "record_task_checkpoint",
    "reopen_selected_task",
    "request_task_finalization",
    "request_task_finalization_with_backend",
    "save_chat_offset_after_message",
    "start_backend",
    "start_dispatched_task_backend",
    "start_prepared_backend",
    "stop_backend",
    "task_locked_for_messages",
]
