from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from aha_cli import platform
from aha_cli.web.auth import normalize_auth_token


def _token_file(root: Path) -> Path:
    return Path(root) / "web-token"


def _read_token(root: Path) -> str:
    try:
        return _token_file(root).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def service_settings_status(root: Path) -> dict:
    root = Path(root).expanduser().resolve()
    settings = None
    tray_configured = False
    if platform.is_windows():
        try:
            from aha_cli.services.windows_tray import (
                default_tray_config_path,
                load_tray_settings,
            )

            tray_configured = default_tray_config_path().is_file()
            settings = load_tray_settings()
        except Exception:
            settings = None
    configured_home = Path(settings.aha_home) if settings is not None else root
    token = settings.web_token if settings is not None else _read_token(root)
    return {
        "aha_home": str(root),
        "configured_aha_home": str(configured_home),
        "aha_home_editable": bool(settings is not None),
        "web_token_configured": bool(token),
        "startup_supported": bool(platform.is_windows() and settings is not None),
        "startup_enabled": bool(
            settings is not None and str(settings.startup_task_name or "").strip()
        ),
        "tray_configured": tray_configured,
    }


def update_service_settings(root: Path, payload: dict) -> dict:
    root = Path(root).expanduser().resolve()
    current_status = service_settings_status(root)
    requested_home = str(
        payload.get("aha_home")
        or current_status.get("configured_aha_home")
        or root
    ).strip()
    target_home = Path(requested_home).expanduser().resolve()
    token_input = payload.get("web_token")
    token = _read_token(root)
    token_changed = False
    if token_input not in (None, ""):
        token = normalize_auth_token(token_input)
        token_changed = True
    requested_startup = payload.get("startup_enabled")
    home_changed = target_home != Path(
        str(current_status.get("configured_aha_home") or root)
    )

    if platform.is_windows() and current_status.get("tray_configured"):
        from aha_cli.services.windows_tray import (
            WINDOWS_STARTUP_TASK_NAME,
            TraySettings,
            configure_prelogin_startup_task,
            default_tray_config_path,
            load_tray_settings,
            save_tray_settings,
        )

        current = load_tray_settings()
        if current is None:
            raise RuntimeError("Windows tray settings are unavailable")
        updated = TraySettings(
            target_home,
            current.bind,
            current.port,
            token or current.web_token,
            current.startup_task_name,
        ).normalized()
        if requested_startup is not None:
            enabled = bool(requested_startup)
            if enabled != bool(current.startup_task_name):
                configure_prelogin_startup_task(
                    updated,
                    default_tray_config_path(),
                    enabled,
                )
                updated = replace(
                    updated,
                    startup_task_name=WINDOWS_STARTUP_TASK_NAME if enabled else "",
                )
        save_tray_settings(updated)
    else:
        if home_changed:
            raise ValueError(
                "AHA_HOME can only be changed by a Windows tray-managed installation"
            )
        if requested_startup not in (None, False):
            raise ValueError("pre-login startup is unavailable on this installation")
        if token_changed:
            token_file = _token_file(root)
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(token, encoding="utf-8")

    return {
        "ok": True,
        "restart_required": bool(home_changed or token_changed),
        "service_settings": service_settings_status(root),
    }


__all__ = ["service_settings_status", "update_service_settings"]
