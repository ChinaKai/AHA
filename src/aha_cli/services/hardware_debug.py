from __future__ import annotations

from aha_cli.domain.models import normalize_task_hardware_debug
from aha_cli.services.prompt_templates import render_prompt_template


def hardware_debug_context_for_prompt(task: dict) -> str:
    config = normalize_task_hardware_debug(task.get("hardware_debug"))
    if not config.get("enabled"):
        return ""
    channels = config.get("channels") or []
    if not channels:
        return ""

    lines: list[str] = []

    for index, channel in enumerate(channels, start=1):
        channel_type = str(channel.get("type") or "").strip()
        lines.append(f"- channel {index}: type={channel_type}")
        settings = channel.get("settings") if isinstance(channel.get("settings"), dict) else {}
        if settings:
            setting_text = ", ".join(
                f"{key}={value}"
                for key, value in settings.items()
                if value not in (None, "")
            )
            if setting_text:
                lines.append(f"  settings: {setting_text}")
        operation_skill_path = str(channel.get("operation_skill_path") or "").strip()
        if operation_skill_path:
            lines.append(f"  operation skill path: {operation_skill_path}")
        permissions = channel.get("permissions") or {}
        if permissions:
            permission_text = ", ".join(f"{key}={bool(value)}" for key, value in permissions.items())
            lines.append(f"  permissions: {permission_text}")

    return render_prompt_template(
        "hardware_debug_context.md",
        enabled_channel_count=len(channels),
        channels="\n".join(lines),
    )


__all__ = ["hardware_debug_context_for_prompt"]
