from __future__ import annotations

from aha_cli.domain.models import normalize_task_browser_control
from aha_cli.services.prompt_templates import render_prompt_template


def browser_control_context_for_prompt(task: dict) -> str:
    config = normalize_task_browser_control(task.get("browser_control"))
    if config.get("mode") != "managed":
        return ""
    allowed_hosts = config.get("allowed_hosts") or []
    return render_prompt_template(
        "browser_control_context.md",
        agent_access=config.get("agent_access") or "read_only",
        runtime=config.get("runtime") or "playwright",
        profile=(
            f'named ({config.get("profile_name")})'
            if config.get("profile") == "named"
            else config.get("profile") or "ephemeral"
        ),
        display=config.get("display") or "native",
        proxy_mode=config.get("proxy_mode") or "direct",
        allowed_hosts=", ".join(str(item) for item in allowed_hosts) if allowed_hosts else "(unrestricted)",
        downloads=config.get("downloads") or "deny",
        uploads=config.get("uploads") or "deny",
    )


__all__ = ["browser_control_context_for_prompt"]
