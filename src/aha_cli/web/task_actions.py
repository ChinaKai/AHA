from __future__ import annotations

from aha_cli.domain.models import (
    DEFAULT_TASK_CONTEXT_THRESHOLD_PERCENT,
    DEFAULT_TASK_SUPERVISION_MAX_ROUNDS,
    TASK_SUPERVISION_ASK_USER_GATES,
)
from aha_cli.services.auto_context_compact import start_backend_after_auto_compact as start_backend
from aha_cli.services.backend_runtime import backend_status, stop_backend
from aha_cli.web.http_utils import parse_optional_bool
from aha_cli.web.task_commands import (
    compact_reset_selected_agent,
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


__all__ = [
    "backend_status",
    "compact_reset_selected_agent",
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
    "parse_task_supervision_fields",
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
