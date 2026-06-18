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
            "- A UART/serial port is a continuous stream, not request/response: the board keeps logging",
            "  after power-up even when you send nothing. Watch the stream to judge state (U-Boot countdown,",
            "  kernel panic, a waiting `login:` prompt) and only send (TX) when an action is actually needed.",
            "- Drive serial through the persistent session helpers so users can watch it in the Web Hardware tab:",
            "    aha hardware-attach <run> <task> --channel uart --device /dev/ttyUSB0 --baudrate 115200   (run in the background; holds the port and streams RX)",
            "    aha hardware-send  <run> <task> --channel uart --data 'printenv\\r'                          (interactive TX; \\r etc. are honored)",
            "    aha hardware-rules <run> <task> --channel uart                                              (inspect armed rules + status)",
            "    aha hardware-stop  <run> <task> --channel uart                                              (detach)",
            "- Time-critical windows (e.g. U-Boot 'Hit any key to stop autoboot') are too short for an agent",
            "  round-trip. Pre-arm a LOCAL auto-reaction that fires at native speed BEFORE you reset the board:",
            "    aha hardware-arm <run> <task> --channel uart --pattern 'stop autoboot' --send '\\r' --max-fires 1",
            "    aha hardware-arm <run> <task> --channel uart --interval 0.1 --duration 3 --send '\\r' --max-fires 0   (spam \\r right after reset)",
            "  Only arm an interrupt when the task actually needs it (e.g. entering U-Boot); otherwise let the board boot normally.",
            "- Keep large channel logs and binary artifacts in files, then summarize paths instead of pasting full logs.",
        ]
    )
    return "\n".join(lines)


__all__ = ["hardware_debug_context_for_prompt"]
