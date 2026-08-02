from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import threading
from typing import Any

from aha_cli.domain.models import is_service_assistant_run, is_service_assistant_task, normalize_feishu_integration_config, utc_now
from aha_cli.services.feishu import mark_confirmation_card_updated, pending_confirmation_card_updates
from aha_cli.services.feishu_audit import audit_feishu_channel
from aha_cli.store.config import load_config
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import aha_home_path, config_path
from aha_cli.store.runs import list_run_summaries, require_plan

_channels: dict[str, Any] = {}
_channels_lock = threading.Lock()
_config_lock = threading.Lock()
_runtime_lock = threading.Lock()


def feishu_config(root: Path) -> dict:
    integrations = load_config(root).get("integrations")
    raw = integrations.get("feishu") if isinstance(integrations, dict) else None
    return normalize_feishu_integration_config(raw)


def feishu_credentials(config: dict, environ: dict[str, str] | None = None) -> tuple[str, str]:
    env = environ if environ is not None else os.environ
    app_id_env = str(config.get("app_id_env") or "AHA_FEISHU_APP_ID")
    secret_env = str(config.get("app_secret_env") or "AHA_FEISHU_APP_SECRET")
    app_id = str(config.get("app_id") or env.get(app_id_env) or "").strip()
    app_secret = str(config.get("app_secret") or env.get(secret_env) or "").strip()
    return app_id, app_secret


def feishu_runtime_path(root: Path) -> Path:
    return aha_home_path(root) / "runtime" / "feishu.json"


def _write_runtime(root: Path, **changes: object) -> dict:
    path = feishu_runtime_path(root)
    with _runtime_lock:
        try:
            state = read_json(path)
        except (FileNotFoundError, OSError, ValueError):
            state = {}
        state.update(changes)
        state["updated_at"] = utc_now()
        write_json(path, state)
        return state


def feishu_sdk_available() -> bool:
    return importlib.util.find_spec("lark_channel") is not None


def _feishu_env_groups(config: dict) -> dict[str, list[dict[str, str]]]:
    """Return model selectors for configured backend env groups, without secrets."""
    result: dict[str, list[dict[str, str]]] = {}
    for backend, model_key in (("codex", "OPENAI_MODEL"), ("claude", "ANTHROPIC_MODEL")):
        backend_config = config.get(backend)
        raw_groups = backend_config.get("env") if isinstance(backend_config, dict) else []
        if isinstance(raw_groups, dict):
            raw_groups = [raw_groups]
        groups: list[dict[str, str]] = []
        for index, raw_group in enumerate(raw_groups if isinstance(raw_groups, list) else []):
            if not isinstance(raw_group, dict):
                continue
            name = str(raw_group.get("name") or f"env-{index + 1}").strip()
            if not name:
                continue
            groups.append({"name": name, "model": str(raw_group.get(model_key) or "").strip()})
        result[backend] = groups
    return result


def _feishu_backend_defaults(config: dict) -> dict[str, dict[str, object]]:
    """Expose non-secret backend defaults used by the Feishu Agent controls."""
    result: dict[str, dict[str, object]] = {}
    for backend in ("codex", "claude", "stub"):
        backend_config = config.get(backend) if isinstance(config.get(backend), dict) else {}
        proxy = backend_config.get("proxy") if isinstance(backend_config.get("proxy"), dict) else {}
        result[backend] = {
            "reasoning_effort": str(backend_config.get("reasoning_effort") or ""),
            "proxy_enabled": bool(proxy.get("enabled")),
        }
    return result


def _service_assistant_status(root: Path) -> dict:
    run_id = ""
    plan: dict = {}
    for summary in list_run_summaries(root):
        candidate = str(summary.get("id") or "")
        if not candidate:
            continue
        try:
            candidate_plan = require_plan(root, candidate)
        except SystemExit:
            continue
        if is_service_assistant_run(candidate_plan):
            run_id = candidate
            plan = candidate_plan
            break
    tasks = [
        task
        for task in plan.get("tasks", [])
        if isinstance(task, dict) and is_service_assistant_task(task) and not task.get("deleted_at")
    ]
    active = [task for task in tasks if str(task.get("status") or "") not in {"completed", "failed", "blocked"}]
    return {
        "identity": "aha_service_steward",
        "system_managed": True,
        "workspace_path": str(aha_home_path(root).resolve()),
        "sandbox": "read-only",
        "approval": "never",
        "run_id": run_id,
        "provisioned": bool(run_id),
        "conversation_count": len(tasks),
        "active_conversation_count": len(active),
        "prompt_templates": [
            "service_assistant_identity.md",
            "service_assistant_runtime.md",
            "service_assistant_action_contract.md",
        ],
    }


def _create_feishu_channel(app_id: str, app_secret: str, security_mode: str) -> Any:
    """Import and construct the SDK outside the web server's event-loop thread.

    lark-channel-sdk 1.2 initializes its WebSocket event loop at import time.
    Importing it from ``run_feishu_channel`` would therefore capture AHA's
    already-running asyncio loop, which the SDK later tries to drive with
    ``run_until_complete`` from its worker thread.
    """
    from lark_channel import FeishuChannel, SecurityConfig

    return FeishuChannel(
        app_id=app_id,
        app_secret=app_secret,
        transport="ws",
        security=SecurityConfig(mode=security_mode),
    )


def feishu_status(root: Path) -> dict:
    config = feishu_config(root)
    global_config = load_config(root)
    effective_backend = str(config.get("backend") or global_config.get("backend") or "codex")
    backend_config = global_config.get(effective_backend) if isinstance(global_config.get(effective_backend), dict) else {}
    effective_model = str(config.get("model") or backend_config.get("model") or "")
    effective_reasoning_effort = str(config.get("reasoning_effort") or backend_config.get("reasoning_effort") or "")
    backend_proxy = backend_config.get("proxy") if isinstance(backend_config.get("proxy"), dict) else {}
    configured_proxy_enabled = config.get("proxy_enabled")
    effective_proxy_enabled = (
        bool(configured_proxy_enabled)
        if isinstance(configured_proxy_enabled, bool)
        else bool(backend_proxy.get("enabled"))
    )
    app_id, app_secret = feishu_credentials(config)
    try:
        runtime = read_json(feishu_runtime_path(root))
    except (FileNotFoundError, OSError, ValueError):
        runtime = {}
    return {
        "enabled": bool(config.get("enabled")),
        "configured": bool(app_id and app_secret),
        "app_id": str(config.get("app_id") or ""),
        "effective_app_id": app_id,
        "app_secret_configured": bool(app_secret),
        "app_id_env": config.get("app_id_env"),
        "app_secret_env": config.get("app_secret_env"),
        "backend": str(config.get("backend") or ""),
        "model": str(config.get("model") or ""),
        "reasoning_effort": str(config.get("reasoning_effort") or ""),
        "proxy_enabled": configured_proxy_enabled if isinstance(configured_proxy_enabled, bool) else None,
        "effective_backend": effective_backend,
        "effective_model": effective_model,
        "effective_reasoning_effort": effective_reasoning_effort,
        "effective_proxy_enabled": effective_proxy_enabled,
        "backend_defaults": _feishu_backend_defaults(global_config),
        "env_groups": _feishu_env_groups(global_config),
        "allowed_open_ids": list(config.get("allowed_open_ids") or []),
        "allowed_open_id_count": len(config.get("allowed_open_ids") or []),
        "group_mentions_only": bool(config.get("group_mentions_only")),
        "notifications_enabled": bool(config.get("notifications_enabled")),
        "security_mode": config.get("security_mode"),
        "sdk_installed": feishu_sdk_available(),
        "runtime": runtime,
        "assistant": _service_assistant_status(root),
    }


def update_feishu_settings(root: Path, payload: dict) -> dict:
    """Persist the Feishu integration without exposing or clearing its secret."""
    if not isinstance(payload, dict):
        raise ValueError("Feishu settings must be a JSON object")
    path = config_path(root)
    with _config_lock:
        try:
            config = read_json(path)
        except FileNotFoundError:
            config = {}
        if not isinstance(config, dict):
            raise ValueError("AHA config must be a JSON object")
        integrations = config.get("integrations")
        integrations = dict(integrations) if isinstance(integrations, dict) else {}
        current = integrations.get("feishu")
        current = dict(current) if isinstance(current, dict) else {}
        accepted = {
            "enabled",
            "app_id",
            "app_secret",
            "app_id_env",
            "app_secret_env",
            "backend",
            "model",
            "reasoning_effort",
            "proxy_enabled",
            "allowed_open_ids",
            "group_mentions_only",
            "notifications_enabled",
            "security_mode",
        }
        updated = {**current, **{key: value for key, value in payload.items() if key in accepted}}
        if not str(payload.get("app_secret") or "").strip():
            updated["app_secret"] = str(current.get("app_secret") or "")
        integrations["feishu"] = normalize_feishu_integration_config(updated)
        config["integrations"] = integrations
        write_json(path, config)
    return feishu_status(root)


def update_feishu_notifications_enabled(root: Path, enabled: bool) -> dict:
    """Persist only the optional task-status push setting.

    The Feishu console uses this narrow update instead of resubmitting the full
    bootstrap form, so concurrent or future integration settings are preserved.
    Notification workers reload config for every event, making this effective
    without restarting the Web service.
    """
    path = config_path(root)
    with _config_lock:
        try:
            config = read_json(path)
        except FileNotFoundError:
            config = {}
        if not isinstance(config, dict):
            raise ValueError("AHA config must be a JSON object")
        integrations = config.get("integrations")
        integrations = dict(integrations) if isinstance(integrations, dict) else {}
        feishu = integrations.get("feishu")
        feishu = dict(feishu) if isinstance(feishu, dict) else {}
        feishu["notifications_enabled"] = bool(enabled)
        integrations["feishu"] = feishu
        config["integrations"] = integrations
        write_json(path, config)
    return feishu_status(root)


def active_feishu_channel(root: Path) -> Any | None:
    with _channels_lock:
        return _channels.get(str(aha_home_path(root).resolve()))


def send_via_active_channel(root: Path, target: str, message: object, opts: dict | None = None, *, timeout: float = 20.0) -> dict:
    channel = active_feishu_channel(root)
    if channel is None:
        raise RuntimeError("Feishu channel is not connected in this process")
    kind = "card" if isinstance(message, dict) and isinstance(message.get("card"), dict) else "message"
    try:
        future = channel.schedule(channel.send(target, message, opts))
        result = future.result(timeout=timeout)
        if hasattr(result, "success") and not result.success:
            raise RuntimeError(str(getattr(result, "error", None) or "Feishu send failed"))
    except Exception as exc:  # noqa: BLE001 - SDK futures may raise transport-specific exceptions.
        audit_feishu_channel(
            root,
            direction="outbound",
            kind=kind,
            status="failed",
            transport="channel_ws",
            chat_id=target,
            content=message,
            error=exc,
        )
        raise
    message_id = getattr(result, "message_id", None)
    audit_feishu_channel(
        root,
        direction="outbound",
        kind=kind,
        status="sent",
        transport="channel_ws",
        message_id=str(message_id or ""),
        chat_id=target,
        content=message,
    )
    return {
        "ok": True,
        "sent": True,
        "message_id": message_id,
        "target": target,
    }


def update_card_via_active_channel(root: Path, message_id: str, card: dict, *, timeout: float = 20.0) -> dict:
    channel = active_feishu_channel(root)
    if channel is None:
        raise RuntimeError("Feishu channel is not connected in this process")
    try:
        future = channel.schedule(channel.update_card(str(message_id), card))
        result = future.result(timeout=timeout)
        if hasattr(result, "success") and not result.success:
            raise RuntimeError(str(getattr(result, "error", None) or "Feishu card update failed"))
    except Exception as exc:  # noqa: BLE001 - SDK futures may raise transport-specific exceptions.
        audit_feishu_channel(
            root,
            direction="outbound",
            kind="card_update",
            status="failed",
            transport="channel_ws",
            message_id=str(message_id),
            content={"card": card},
            error=exc,
        )
        raise
    audit_feishu_channel(
        root,
        direction="outbound",
        kind="card_update",
        status="updated",
        transport="channel_ws",
        message_id=str(message_id),
        content={"card": card},
    )
    return {"ok": True, "updated": True, "message_id": str(message_id)}


def refresh_confirmation_cards(root: Path) -> dict:
    updated = 0
    failed = 0
    for record in pending_confirmation_card_updates(root):
        try:
            update_card_via_active_channel(
                root,
                str(record.get("message_id") or ""),
                record.get("terminal_card") if isinstance(record.get("terminal_card"), dict) else {},
            )
        except (RuntimeError, TimeoutError):
            failed += 1
            continue
        mark_confirmation_card_updated(root, str(record.get("confirmation_id") or ""))
        updated += 1
    return {"updated": updated, "failed": failed}


async def run_feishu_channel(root: Path, default_run_id: str = "") -> None:
    config = feishu_config(root)
    if not config.get("enabled"):
        _write_runtime(root, status="disabled", connected=False, error="")
        return
    app_id, app_secret = feishu_credentials(config)
    if not app_id or not app_secret:
        _write_runtime(root, status="not_configured", connected=False, error="missing app id or app secret")
        return
    if not feishu_sdk_available():
        _write_runtime(root, status="sdk_missing", connected=False, error="Feishu Channel SDK is unavailable")
        return

    from aha_cli.services.feishu_assistant import enqueue_card_action, enqueue_message

    try:
        channel = await asyncio.to_thread(
            _create_feishu_channel,
            app_id,
            app_secret,
            str(config.get("security_mode") or "audit"),
        )
    except ImportError:
        _write_runtime(root, status="sdk_missing", connected=False, error="Feishu Channel SDK is unavailable")
        return
    channel.on("message", lambda message: enqueue_message(root, default_run_id, channel, message))
    channel.on("cardAction", lambda event: enqueue_card_action(root, default_run_id, channel, event))
    def channel_error(error: object) -> None:
        audit_feishu_channel(
            root,
            direction="system",
            kind="connection",
            status="error",
            transport="channel_ws",
            error=error,
        )
        _write_runtime(root, status="error", connected=False, error=str(error))

    channel.on("error", channel_error)
    key = str(aha_home_path(root).resolve())
    try:
        _write_runtime(root, status="connecting", connected=False, error="", started_at=utc_now())
        audit_feishu_channel(
            root,
            direction="system",
            kind="connection",
            status="connecting",
            transport="channel_ws",
        )
        await channel.start_background(timeout=30.0)
        with _channels_lock:
            _channels[key] = channel
        _write_runtime(root, status="connected", connected=True, error="", connected_at=utc_now())
        audit_feishu_channel(
            root,
            direction="system",
            kind="connection",
            status="connected",
            transport="channel_ws",
        )
        await asyncio.to_thread(refresh_confirmation_cards, root)
        while True:
            await asyncio.sleep(30)
            await asyncio.to_thread(refresh_confirmation_cards, root)
            _write_runtime(root, status="connected", connected=bool(channel.is_ready), error="")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - runtime status must survive optional SDK failures.
        audit_feishu_channel(
            root,
            direction="system",
            kind="connection",
            status="failed",
            transport="channel_ws",
            error=exc,
        )
        _write_runtime(root, status="error", connected=False, error=str(exc))
    finally:
        with _channels_lock:
            _channels.pop(key, None)
        try:
            await channel.disconnect()
        except Exception:  # noqa: BLE001 - shutdown is best effort.
            pass
        _write_runtime(root, status="stopped", connected=False, stopped_at=utc_now())
        audit_feishu_channel(
            root,
            direction="system",
            kind="connection",
            status="stopped",
            transport="channel_ws",
        )


__all__ = [
    "active_feishu_channel",
    "feishu_config",
    "feishu_credentials",
    "feishu_runtime_path",
    "feishu_sdk_available",
    "feishu_status",
    "run_feishu_channel",
    "refresh_confirmation_cards",
    "send_via_active_channel",
    "update_card_via_active_channel",
    "update_feishu_settings",
    "update_feishu_notifications_enabled",
]
