from __future__ import annotations

from aha_cli.domain.models import normalize_task_hardware_debug
from aha_cli.services.prompt_templates import render_prompt_template


def hardware_debug_context_for_prompt(task: dict) -> str:
    config = normalize_task_hardware_debug(task.get("hardware_debug"))
    groups = [item for item in config.get("groups") or [] if isinstance(item, dict)]
    if not any(str(group.get("mode") or "off") != "off" for group in groups):
        return ""
    mode = "groups"
    lines: list[str] = []
    for group in groups:
        group_mode = str(group.get("mode") or "off")
        serial = group.get("serial") if isinstance(group.get("serial"), dict) else {}
        network = group.get("network") if isinstance(group.get("network"), dict) else {}
        credentials = group.get("credentials") if isinstance(group.get("credentials"), dict) else {}
        permissions = group.get("permissions") if isinstance(group.get("permissions"), dict) else {}
        lines.append(f"- hardware group: id={group.get('id') or '(missing)'}, mode={group_mode}")
        lines.append(f"  description: {group.get('description') or '(not provided)'}")
        lines.append(f"  access permission: {permissions.get('access') or 'read_only'}")
        if group_mode in {"serial", "both"}:
            lines.append(f"  serial: device={serial.get('device') or '(missing)'}, baudrate={int(serial.get('baudrate') or 115200)}")
        if group_mode in {"network", "both"}:
            lines.append(f"  network: device_ip={network.get('device_ip') or '(missing)'}, transport=discover (SSH 22 preferred, Telnet 23 fallback)")
        username = str(credentials.get("username") or "").strip()
        if username:
            lines.append(f"  login username: {username}")
        lines.append(f"  login password configured: {bool(credentials.get('password'))}")

    return render_prompt_template(
        "hardware_debug_context.md",
        mode=mode,
        terminals="\n".join(lines),
    )


__all__ = ["hardware_debug_context_for_prompt"]
