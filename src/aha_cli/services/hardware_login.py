"""Auto-login for hardware debug.

When a task configures ``credentials`` (username/password), arm rules on the
serial or network bridge that send the credentials the moment the device prints
a login or password prompt — so opening the terminal logs in automatically
without the agent having to drive it.

Reuses the bridge armed-rules control inbox (``arm`` commands). Fixed rule ids
make re-arming idempotent (the engine replaces a rule with the same id instead
of stacking duplicates).
"""
from __future__ import annotations

AUTO_LOGIN_USER_ID = "auto-login-user"
AUTO_LOGIN_PASS_ID = "auto-login-pass"

# Match common *nix/embedded prompts at end of a line. Case-insensitive so both
# "login:" and "Login:" land; trailing whitespace tolerated.
_USER_PATTERN = r"(?i)(login|user(name)?|account)\s*:\s*$"
_PASS_PATTERN = r"(?i)password\s*:\s*$"


def login_arm_commands(credentials: dict | None) -> list[dict]:
    """Build the ``arm`` control commands for the configured credentials."""
    creds = credentials if isinstance(credentials, dict) else {}
    username = str(creds.get("username") or "").strip()
    password = str(creds.get("password") or "")
    commands: list[dict] = []
    if username:
        commands.append(
            {
                "cmd": "arm",
                "id": AUTO_LOGIN_USER_ID,
                "trigger": "match",
                "regex": True,
                "pattern": _USER_PATTERN,
                "send": username + "\r",
                "max_fires": 5,
            }
        )
    if password:
        commands.append(
            {
                "cmd": "arm",
                "id": AUTO_LOGIN_PASS_ID,
                "trigger": "match",
                "regex": True,
                "pattern": _PASS_PATTERN,
                "send": password + "\r",
                "max_fires": 5,
            }
        )
    return commands


def arm_auto_login(root, credentials, *, device: str | None = None, host: str | None = None, port: int | None = None) -> int:
    """Arm login rules on the serial (device) or network (host:port) bridge.

    Returns the number of rules armed. Best-effort: callers should treat failure
    (e.g. no bridge yet) as non-fatal.
    """
    commands = login_arm_commands(credentials)
    if not commands:
        return 0
    if device:
        from aha_cli.services.hardware_bridge import append_bridge_control

        for command in commands:
            append_bridge_control(root, device, command)
    elif host:
        from aha_cli.services.network_terminal import append_network_control

        for command in commands:
            append_network_control(root, host, int(port or 23), command)
    else:
        return 0
    return len(commands)
