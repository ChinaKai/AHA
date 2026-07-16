from __future__ import annotations

from aha_cli.domain.models import normalize_task_hardware_debug
from aha_cli.services.prompt_templates import render_prompt_template


def hardware_debug_context_for_prompt(task: dict) -> str:
    config = normalize_task_hardware_debug(task.get("hardware_debug"))
    mode = str(config.get("mode") or "off")
    if mode == "off":
        return ""
    lines: list[str] = []
    serial = config.get("serial") if isinstance(config.get("serial"), dict) else {}
    network = config.get("network") if isinstance(config.get("network"), dict) else {}
    credentials = config.get("credentials") if isinstance(config.get("credentials"), dict) else {}
    permissions = config.get("permissions") if isinstance(config.get("permissions"), dict) else {}
    lines.append(f"- access permission: {permissions.get('access') or 'read_only'}")
    if mode in {"serial", "both"}:
        lines.append(
            f"- serial terminal: device={serial.get('device') or '(missing)'}, "
            f"baudrate={int(serial.get('baudrate') or 115200)}"
        )
    if mode in {"network", "both"}:
        lines.append(
            f"- network terminal: device_ip={network.get('device_ip') or '(missing)'}, "
            "transport=discover (SSH 22 preferred, Telnet 23 fallback)"
        )
    username = str(credentials.get("username") or "").strip()
    if username:
        lines.append(f"- login username: {username}")
    lines.append(f"- login password configured: {bool(credentials.get('password'))}")

    return render_prompt_template(
        "hardware_debug_context.md",
        mode=mode,
        terminals="\n".join(lines),
    )


__all__ = ["hardware_debug_context_for_prompt"]
