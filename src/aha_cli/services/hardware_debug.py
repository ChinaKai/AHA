from __future__ import annotations

from aha_cli.domain.models import normalize_task_hardware_debug


def hardware_debug_context_for_prompt(task: dict) -> str:
    config = normalize_task_hardware_debug(task.get("hardware_debug"))
    channels = config.get("channels") or []
    if not channels:
        return ""

    lines = [
        "Hardware debug context:",
        f"- enabled channels: {len(channels)}",
    ]

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

    lines.extend(
        [
            "Hardware debug operating rules:",
            "- Read the channel operation skill before sending commands through that hardware channel.",
            "- Treat the permission flags as task-local policy; do not perform disabled hardware operations.",
            "- Prefer AHA hardware I/O logging helpers when available so users can monitor TX/RX in the Web Hardware tab.",
            "- Keep large channel logs and binary artifacts in files, then summarize paths instead of pasting full logs.",
        ]
    )
    return "\n".join(lines)


__all__ = ["hardware_debug_context_for_prompt"]
