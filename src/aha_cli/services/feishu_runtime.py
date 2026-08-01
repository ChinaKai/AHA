from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import threading
from typing import Any

from aha_cli.domain.models import normalize_feishu_integration_config, utc_now
from aha_cli.store.config import load_config
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import aha_home_path, config_path

FEISHU_INSTALL_COMMAND = 'python3 -m pip install -e ".[feishu]"'

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
    app_id, app_secret = feishu_credentials(config)
    try:
        runtime = read_json(feishu_runtime_path(root))
    except (FileNotFoundError, OSError, ValueError):
        runtime = {}
    return {
        "enabled": bool(config.get("enabled")),
        "configured": bool(app_id and app_secret),
        "app_id": app_id,
        "app_secret_configured": bool(app_secret),
        "app_id_env": config.get("app_id_env"),
        "app_secret_env": config.get("app_secret_env"),
        "allowed_open_id_count": len(config.get("allowed_open_ids") or []),
        "group_mentions_only": bool(config.get("group_mentions_only")),
        "notifications_enabled": bool(config.get("notifications_enabled")),
        "security_mode": config.get("security_mode"),
        "sdk_installed": feishu_sdk_available(),
        "install_command": FEISHU_INSTALL_COMMAND,
        "runtime": runtime,
    }


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
    future = channel.schedule(channel.send(target, message, opts))
    result = future.result(timeout=timeout)
    if hasattr(result, "success") and not result.success:
        raise RuntimeError(str(getattr(result, "error", None) or "Feishu send failed"))
    return {
        "ok": True,
        "sent": True,
        "message_id": getattr(result, "message_id", None),
        "target": target,
    }


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
        _write_runtime(root, status="sdk_missing", connected=False, error=f"Install with: {FEISHU_INSTALL_COMMAND}")
        return

    from aha_cli.services.feishu_assistant import enqueue_message

    try:
        channel = await asyncio.to_thread(
            _create_feishu_channel,
            app_id,
            app_secret,
            str(config.get("security_mode") or "audit"),
        )
    except ImportError:
        _write_runtime(root, status="sdk_missing", connected=False, error=f"Install with: {FEISHU_INSTALL_COMMAND}")
        return
    channel.on("message", lambda message: enqueue_message(root, default_run_id, channel, message))
    channel.on("error", lambda error: _write_runtime(root, status="error", connected=False, error=str(error)))
    key = str(aha_home_path(root).resolve())
    try:
        _write_runtime(root, status="connecting", connected=False, error="", started_at=utc_now())
        await channel.start_background(timeout=30.0)
        with _channels_lock:
            _channels[key] = channel
        _write_runtime(root, status="connected", connected=True, error="", connected_at=utc_now())
        while True:
            await asyncio.sleep(30)
            _write_runtime(root, status="connected", connected=bool(channel.is_ready), error="")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - runtime status must survive optional SDK failures.
        _write_runtime(root, status="error", connected=False, error=str(exc))
    finally:
        with _channels_lock:
            _channels.pop(key, None)
        try:
            await channel.disconnect()
        except Exception:  # noqa: BLE001 - shutdown is best effort.
            pass
        _write_runtime(root, status="stopped", connected=False, stopped_at=utc_now())


__all__ = [
    "FEISHU_INSTALL_COMMAND",
    "active_feishu_channel",
    "feishu_config",
    "feishu_credentials",
    "feishu_runtime_path",
    "feishu_sdk_available",
    "feishu_status",
    "run_feishu_channel",
    "send_via_active_channel",
    "update_feishu_notifications_enabled",
]
