from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import is_feishu_group_task, resolve_group_digital_human_permissions
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.config import load_config
from aha_cli.store.event_views import event_agent_refs
from aha_cli.store.filesystem import iter_jsonl_reverse
from aha_cli.store.knowledge import knowledge_root
from aha_cli.store.paths import aha_home_path, event_path
from aha_cli.store.workspaces import list_workspaces

MAX_RECENT_GROUP_MESSAGES = 8
MAX_LINE_CHARS = 260


def _clip(value: object, limit: int = MAX_LINE_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _default_read_paths(root: Path, config: dict) -> list[str]:
    """Default knowledge sources for the digital human.

    When no explicit read_paths are configured, the digital human's knowledge
    comes from: the AHA Knowledge Base root, configured workspace roots /
    registered workspaces, and the digital-human workspace directory. These are
    declared as a path list (not enumerated content) — the digital human may
    read everything under each path.
    """
    paths: list[str] = []
    try:
        kb_root = knowledge_root(root, config)
        paths.append(str(kb_root.resolve()))
    except (Exception, SystemExit):
        pass
    configured_roots = [str(item).strip() for item in (config.get("workspace_roots") or []) if str(item).strip()]
    registered = []
    try:
        registered = list_workspaces(root)
    except (Exception, SystemExit):
        registered = []
    registered_paths = [str(item.get("path") or "").strip() for item in registered if str(item.get("path") or "").strip()]
    from aha_cli.store.ws_target import host_native_path

    for value in [*configured_roots, *registered_paths]:
        if value and value not in paths:
            # Config/workspace paths are stored in the AHA platform's view
            # (Windows); convert to the current host's native view (e.g. /mnt/c
            # inside WSL) so they resolve on the running process.
            try:
                resolved = Path(host_native_path(value, aha_home=root)).resolve()
                paths.append(str(resolved))
            except OSError:
                continue
    dh_dir = aha_home_path(root) / "feishu_group_state"
    paths.append(str(dh_dir.resolve()))
    seen: list[str] = []
    for item in paths:
        if item and item not in seen:
            seen.append(item)
    return seen


def _recent_group_context_lines(root: Path, run_id: str, task_id: str) -> list[str]:
    messages: list[dict] = []
    try:
        iterator = iter_jsonl_reverse(event_path(root, run_id))
    except (Exception, SystemExit):
        iterator = []
    for _offset, event in iterator or ():
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event.get("type") != "message":
            continue
        if str(data.get("task_id") or "") != str(task_id or ""):
            continue
        if "main" not in event_agent_refs(event):
            continue
        if str(data.get("feishu_channel") or "") != "group_digital_human":
            continue
        text = str(data.get("feishu_original_text") or data.get("message") or "").strip()
        if not text:
            continue
        messages.append(
            {
                "ts": str(data.get("ts") or event.get("ts") or ""),
                "chat_type": str(data.get("feishu_chat_type") or ""),
                "text": _clip(text, 180),
                "attachments": data.get("feishu_attachments") if isinstance(data.get("feishu_attachments"), list) else [],
            }
        )
        if len(messages) >= MAX_RECENT_GROUP_MESSAGES:
            break
    if not messages:
        return ["Recent group @ context: none available"]
    lines = [
        "Recent group @ context for this digital-human user:",
        "- Only messages delivered to AHA are listed. Non-@ group messages are not available unless Feishu history fetching is added.",
    ]
    for index, message in enumerate(reversed(messages), start=1):
        attachment_count = len(message["attachments"])
        suffix = f" | attachments={attachment_count}" if attachment_count else ""
        lines.append(f"- {index}. {message['ts'] or '-'} {message['chat_type'] or '-'}: {message['text']}{suffix}")
    return lines


def feishu_group_source_index_context(root: Path, run_id: str, task: dict | None) -> str:
    if not is_feishu_group_task(task):
        return ""
    try:
        config = load_config(root)
        task_id = str((task or {}).get("id") or "")
        permissions = resolve_group_digital_human_permissions(config)
        allowed_topics = ", ".join(str(item) for item in permissions.get("allowed_topics") or []) or "none"
        # An explicitly empty handoff_always list means "no additional topics
        # beyond the baseline" — the baseline handoff triggers (execution,
        # commitment, permissions, private/secrets) live in the identity
        # template and still apply. Do not re-inject them here, otherwise an
        # empty list could not express "turn off extra forced handoffs".
        handoff_always = ", ".join(str(item) for item in permissions.get("handoff_always") or []) or "none (baseline handoff rules from the identity template still apply)"
        allow_common = bool(permissions.get("allow_common_knowledge"))
        permission_context = render_prompt_template(
            "feishu_group_digital_human_permission.md",
            allow_common_knowledge="enabled" if allow_common else "disabled",
            allowed_topics=allowed_topics,
            handoff_always=handoff_always,
        ).rstrip()
        read_paths = [str(item) for item in permissions.get("read_paths") or [] if str(item or "").strip()]
        if not read_paths:
            read_paths = _default_read_paths(root, config)
        path_lines = [f"- {item}" for item in read_paths]
        if allow_common:
            common_line = (
                "- Common knowledge is allowed for casual small talk and generic facts; "
                "project/domain answers must be based on the readable paths."
            )
        else:
            common_line = (
                "- Common knowledge is NOT used for answers: answer only from the readable paths; "
                "when the answer is not covered by the readable paths, hand off."
            )
        lines = [
            "Digital-human information source index:",
            "- Readable paths: you may read all files and subdirectories under these paths.",
            *path_lines,
            common_line,
            "- Never reveal secrets, credentials, private config, or raw absolute paths in public replies.",
            "- If the answer depends on content not covered by the readable paths, or requires execution, commitment, permission, dispute resolution, or private/secrets access, hand off to the owner.",
            "",
            permission_context,
            "",
            *_recent_group_context_lines(root, run_id, task_id),
        ]
        return "\n".join(lines).strip()
    except (Exception, SystemExit):
        return "Digital-human information source index: unavailable"


__all__ = ["feishu_group_source_index_context"]
