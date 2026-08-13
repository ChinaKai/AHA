from __future__ import annotations

from collections.abc import Iterable

from aha_cli.services.prompt_templates import render_prompt_template


FEISHU_GROUP_CHANNEL = "group_digital_human"
MAX_MERGED_GROUP_MESSAGES = 20
MAX_MERGED_GROUP_CHARS = 8000


def _group_message_key(item: dict) -> tuple[str, str, str] | None:
    if str(item.get("sender") or "").strip().lower() != "feishu":
        return None
    if str(item.get("feishu_channel") or "").strip() != FEISHU_GROUP_CHANNEL:
        return None
    if item.get("command_namespace") or item.get("result_policy"):
        return None
    task_id = str(item.get("task_id") or "").strip()
    chat_id = str(item.get("feishu_chat_id") or "").strip()
    user_key = str(item.get("feishu_session_key") or item.get("feishu_mention_open_id") or "").strip()
    if not task_id or not chat_id or not user_key:
        return None
    return task_id, chat_id, user_key


def _is_backend_switch_handoff(item: dict) -> bool:
    """Match the message written by ``switch_agent_backend`` after an agent
    configuration change. It carries no user instruction; when a real message
    follows it should be merged so the agent resumes in one turn."""
    if str(item.get("sender") or "").strip().lower() != "aha":
        return False
    if str(item.get("from_agent") or "").strip().lower() != "aha":
        return False
    if str(item.get("coordination") or "").strip().lower() != "backend_switch":
        return False
    if item.get("command_namespace") or item.get("result_policy"):
        return False
    return True


def _is_consumable_agent_message(item: dict) -> bool:
    """A message the backend worker should actually process: has body text, is
    not a backend-switch handoff (those merge into the next message), and is not
    a feishu group message (which has its own coalescing path)."""
    if not str(item.get("message") or "").strip():
        return False
    if _is_backend_switch_handoff(item):
        return False
    if str(item.get("sender") or "").strip().lower() == "feishu":
        return False
    return True


def _group_message_text(item: dict) -> str:
    return str(item.get("feishu_original_text") or item.get("message") or "").strip()


def _retained_group_messages(items: list[dict]) -> tuple[list[tuple[dict, str]], int, bool]:
    retained_reversed: list[tuple[dict, str]] = []
    used_chars = 0
    latest_truncated = False
    for item in reversed(items[-MAX_MERGED_GROUP_MESSAGES:]):
        text = _group_message_text(item)
        remaining = MAX_MERGED_GROUP_CHARS - used_chars
        if remaining <= 0:
            break
        if len(text) > remaining:
            if not retained_reversed:
                text = text[-remaining:]
                latest_truncated = True
            else:
                break
        retained_reversed.append((item, text))
        used_chars += len(text)
    retained = list(reversed(retained_reversed))
    return retained, len(items) - len(retained), latest_truncated


def _merged_group_item(items: list[dict]) -> tuple[dict, dict]:
    retained, omitted_count, latest_truncated = _retained_group_messages(items)
    latest = dict(items[-1])
    lines: list[str] = []
    if omitted_count:
        lines.append(f"（前 {omitted_count} 条较早消息因刷屏保护未纳入本轮）")
    for index, (_item, text) in enumerate(retained, start=1):
        suffix = "（内容过长，仅保留末尾）" if latest_truncated and index == len(retained) else ""
        lines.append(f"{index}. {text}{suffix}")
    combined_text = "\n".join(lines).strip() or "-"
    latest["message"] = render_prompt_template(
        "feishu_group_digital_human_coalesced.md",
        count=len(items),
        messages=combined_text,
    ).rstrip("\n")
    latest["feishu_original_text"] = combined_text
    latest["feishu_merged_count"] = len(items)
    latest["feishu_merged_omitted_count"] = omitted_count
    latest["feishu_merged_latest_truncated"] = latest_truncated
    attachments = [
        attachment
        for item, _text in retained
        for attachment in (item.get("feishu_attachments") if isinstance(item.get("feishu_attachments"), list) else [])
        if isinstance(attachment, dict)
    ]
    if attachments:
        latest["feishu_attachments"] = attachments[-8:]
    stats = {
        "merged_count": len(items),
        "omitted_count": omitted_count,
        "latest_truncated": latest_truncated,
        "first_ts": str(items[0].get("ts") or ""),
        "last_ts": str(items[-1].get("ts") or ""),
    }
    return latest, stats


def next_task_message_batch(
    records: Iterable[tuple[dict, int]],
    task_id: str,
) -> tuple[dict, int, dict] | None:
    relevant = [
        (item, item_offset)
        for item, item_offset in records
        if str(item.get("task_id") or "") == str(task_id or "")
    ]
    if not relevant:
        return None
    first_item, first_offset = relevant[0]
    key = _group_message_key(first_item)
    if key is None:
        # Coalesce a backend-switch handoff with the next consumable message so
        # the agent resumes in a single turn instead of spinning one empty round.
        if _is_backend_switch_handoff(first_item):
            merged = _merge_backend_switch_with_next(relevant)
            if merged is not None:
                return merged
        return dict(first_item), first_offset, {"merged_count": 1, "omitted_count": 0}
    grouped: list[tuple[dict, int]] = [(first_item, first_offset)]
    for item, item_offset in relevant[1:]:
        if _group_message_key(item) != key:
            break
        grouped.append((item, item_offset))
    if len(grouped) == 1:
        return dict(first_item), first_offset, {"merged_count": 1, "omitted_count": 0}
    merged, stats = _merged_group_item([item for item, _item_offset in grouped])
    return merged, grouped[-1][1], stats


def _merge_backend_switch_with_next(relevant: list[tuple[dict, int]]) -> tuple[dict, int, dict] | None:
    """Merge a backend-switch handoff into the next non-handoff message.

    Returns ``(merged_item, last_offset, stats)`` where ``last_offset`` is the
    offset of the final consumed message so the worker does not re-read it. The
    handoff text is prefixed onto the following message so the agent reads the
    handoff summary before the new instruction in the same turn.
    """
    handoff_item, _handoff_offset = relevant[0]
    handoff_text = str(handoff_item.get("message") or "").strip()
    next_item: dict | None = None
    next_offset = _handoff_offset
    for item, item_offset in relevant[1:]:
        if _is_consumable_agent_message(item):
            next_item = dict(item)
            next_offset = item_offset
            break
        # Skip over intermediate handoffs (e.g. repeated config changes) but keep
        # scanning for a real message; do not consume non-message records yet.
        if not _is_backend_switch_handoff(item):
            break
    if next_item is None:
        return None
    next_text = str(next_item.get("message") or "").strip()
    prefix = handoff_text
    if next_text:
        prefix = f"{prefix}\n\n{next_text}"
    next_item["message"] = prefix
    next_item["merged_count"] = 2
    next_item["feishu_merged_count"] = 2
    stats = {
        "merged_count": 2,
        "omitted_count": 0,
        "latest_truncated": False,
        "first_ts": str(handoff_item.get("ts") or ""),
        "last_ts": str(next_item.get("ts") or ""),
        "handoff_merged": True,
    }
    return next_item, next_offset, stats


__all__ = [
    "FEISHU_GROUP_CHANNEL",
    "MAX_MERGED_GROUP_CHARS",
    "MAX_MERGED_GROUP_MESSAGES",
    "next_task_message_batch",
]
