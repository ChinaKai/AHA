from __future__ import annotations

import hashlib
from pathlib import Path
import re
import secrets
import threading
import time

from aha_cli.domain.models import utc_now
from aha_cli.locking import exclusive_lock
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import aha_home_path

MAX_GROUP_HANDOFFS = 1024
GROUP_HANDOFF_MERGE_WINDOW_SECONDS = 30 * 60
GROUP_HANDOFF_ACTIVE_THREAD_SECONDS = 2 * 60 * 60
GROUP_HANDOFF_MEMO_THREAD_SECONDS = 30 * 24 * 60 * 60
_state_lock = threading.RLock()
FOLLOWUP_RE = re.compile(
    r"(再\s*帮我|帮我\s*(问|催)|问一下|催(一下|一催|下)|跟进|进展|有结果|怎么样了|"
    r"刚才|上面|前面|这个(需求|问题|事情|事)|那(个|件)事|补充|顺便|对了|给我发|"
    r"发(一下|下|吧|张|个)|follow\s*up|ping)",
    re.IGNORECASE,
)


def feishu_group_handoffs_path(root: Path) -> Path:
    return aha_home_path(root) / "feishu" / "group_handoffs.json"


def _load(root: Path) -> dict[str, dict]:
    try:
        payload = read_json(feishu_group_handoffs_path(root))
    except (FileNotFoundError, OSError, ValueError):
        payload = {}
    handoffs = payload.get("handoffs") if isinstance(payload.get("handoffs"), dict) else {}
    return {str(key): value for key, value in handoffs.items() if isinstance(value, dict)}


def _save(root: Path, handoffs: dict[str, dict]) -> None:
    path = feishu_group_handoffs_path(root)
    write_json(path, {"version": 1, "handoffs": handoffs, "updated_at": utc_now()})
    try:
        path.chmod(0o600)
        path.parent.chmod(0o700)
    except OSError:
        pass


def _with_lock(root: Path):
    path = feishu_group_handoffs_path(root).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a+b")


def _preview(text: str, limit: int = 500) -> str:
    return " ".join(str(text or "").split())[:limit]


def _message_entry(message_id: str, text: str, *, kind: str, at: str, at_epoch: float) -> dict:
    return {
        "message_id": str(message_id or ""),
        "text": _preview(text),
        "kind": str(kind or ""),
        "at": at,
        "at_epoch": at_epoch,
    }


def _request_messages(record: dict) -> list[dict]:
    raw = record.get("request_messages")
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    preview = str(record.get("request_preview") or "").strip()
    if not preview:
        return []
    return [
        {
            "message_id": str(record.get("group_message_id") or ""),
            "text": preview,
            "kind": "original",
            "at": str(record.get("created_at") or ""),
            "at_epoch": float(record.get("created_at_epoch") or 0),
        }
    ]


def _combined_request_preview(messages: list[dict]) -> str:
    if not messages:
        return ""
    original = str(messages[0].get("text") or "").strip()
    followups = [str(item.get("text") or "").strip() for item in messages[1:] if str(item.get("text") or "").strip()]
    if not followups:
        return original[:500]
    lines = [original, "补充/追问：", *[f"- {item}" for item in followups]]
    return "\n".join(lines)[:500]


def _record_epoch(record: dict) -> float:
    return float(record.get("updated_at_epoch") or record.get("created_at_epoch") or 0)


def _scope_key(record: dict) -> tuple[str, str, str, str, str, str]:
    return (
        str(record.get("digital_run_id") or ""),
        str(record.get("digital_task_id") or ""),
        str(record.get("group_chat_id") or ""),
        str(record.get("open_id") or ""),
        str(record.get("steward_run_id") or ""),
        str(record.get("steward_task_id") or ""),
    )


def _active_thread_key(record: dict) -> tuple[str, str, str, str, str, str, str]:
    thread_id = str(record.get("thread_id") or "")
    return (*_scope_key(record), thread_id)


def _is_active_thread_record(record: dict, *, now_epoch: float) -> bool:
    status = str(record.get("status") or "")
    if status == "pending":
        return True
    updated_at = _record_epoch(record)
    if status == "delivered":
        return bool(updated_at and now_epoch - updated_at <= GROUP_HANDOFF_ACTIVE_THREAD_SECONDS)
    if status == "memo_created" and str(record.get("memo_id") or ""):
        return bool(updated_at and now_epoch - updated_at <= GROUP_HANDOFF_MEMO_THREAD_SECONDS)
    return False


def _same_group_handoff_scope(
    record: dict,
    *,
    digital_run_id: str,
    digital_task_id: str,
    group_chat_id: str,
    open_id: str,
    steward_run_id: str,
    steward_task_id: str,
) -> bool:
    return _scope_key(record) == (
        str(digital_run_id or ""),
        str(digital_task_id or ""),
        str(group_chat_id or ""),
        str(open_id or ""),
        str(steward_run_id or ""),
        str(steward_task_id or ""),
    )


def _should_merge_group_handoff(record: dict, *, now_epoch: float, request_message: str) -> bool:
    updated_at = _record_epoch(record)
    if updated_at and now_epoch - updated_at > GROUP_HANDOFF_MERGE_WINDOW_SECONDS:
        return False
    preview = _preview(request_message)
    if not preview:
        return False
    if hashlib.sha256(str(request_message or "").encode("utf-8")).hexdigest() == str(record.get("request_fingerprint") or ""):
        return True
    return bool(FOLLOWUP_RE.search(preview))


def _compatible_group_handoff(
    record: dict,
    *,
    digital_run_id: str,
    digital_task_id: str,
    group_chat_id: str,
    open_id: str,
    steward_run_id: str,
    steward_task_id: str,
) -> bool:
    return str(record.get("status") or "") == "pending" and _same_group_handoff_scope(
        record,
        digital_run_id=digital_run_id,
        digital_task_id=digital_task_id,
        group_chat_id=group_chat_id,
        open_id=open_id,
        steward_run_id=steward_run_id,
        steward_task_id=steward_task_id,
    )


def _message_identity(message: dict) -> tuple[str, str]:
    return (str(message.get("message_id") or ""), str(message.get("text") or "").strip())


def _merged_ids(record: dict) -> list[str]:
    values = record.get("merged_from") if isinstance(record.get("merged_from"), list) else []
    return [str(item) for item in values if str(item or "").strip()]


def _append_request_messages(record: dict, messages: list[dict]) -> dict:
    existing = _request_messages(record)
    seen = {_message_identity(item) for item in existing}
    for message in messages:
        identity = _message_identity(message)
        if not identity[0] and not identity[1]:
            continue
        if identity in seen:
            continue
        existing.append(dict(message))
        seen.add(identity)
    existing.sort(key=lambda item: (float(item.get("at_epoch") or 0), str(item.get("message_id") or "")))
    record["request_messages"] = existing[-20:]
    record["request_preview"] = _combined_request_preview(record["request_messages"])
    record["merged_count"] = max(0, len(record["request_messages"]) - 1)
    group_message_ids = [
        str(item)
        for item in record.get("group_message_ids", [])
        if str(item or "").strip()
    ] if isinstance(record.get("group_message_ids"), list) else []
    for message in record["request_messages"]:
        message_id = str(message.get("message_id") or "")
        if message_id and message_id not in group_message_ids:
            group_message_ids.append(message_id)
    record["group_message_ids"] = group_message_ids[-20:]
    latest = next((item for item in reversed(record["request_messages"]) if str(item.get("message_id") or "")), None)
    if latest:
        record["latest_group_message_id"] = str(latest.get("message_id") or "")
    return record


def _merge_existing_record(
    target: dict,
    source: dict,
    *,
    source_id: str,
    now: str,
    now_epoch: float,
) -> dict:
    if not str(target.get("thread_id") or ""):
        target["thread_id"] = str(target.get("id") or "")
    target = _append_request_messages(target, _request_messages(source))
    if str(source.get("status") or "") == "pending" or str(target.get("status") or "") == "delivered":
        target["status"] = "pending"
    if str(source.get("status") or "") == "delivered" and not target.get("last_delivered_at"):
        target["last_delivered_at"] = str(source.get("delivered_at") or source.get("updated_at") or "")
    merged_from = _merged_ids(target)
    for item in [source_id, *_merged_ids(source)]:
        if item and item != str(target.get("id") or "") and item not in merged_from:
            merged_from.append(item)
    if merged_from:
        target["merged_from"] = merged_from[-20:]
    target["updated_at"] = now
    target["updated_at_epoch"] = now_epoch
    return target


def _mark_merged_alias(record: dict, *, target_id: str, now: str, now_epoch: float) -> dict:
    record["status"] = "merged"
    record["merged_into"] = str(target_id or "")
    record["updated_at"] = now
    record["updated_at_epoch"] = now_epoch
    return record


def _normalize_record(record: dict, *, identity: str) -> bool:
    changed = False
    if str(record.get("id") or "") != str(identity or ""):
        record["id"] = str(identity or "")
        changed = True
    messages = _request_messages(record)
    if messages and not isinstance(record.get("request_messages"), list):
        record["request_messages"] = messages
        changed = True
    group_message_ids = [
        str(item)
        for item in record.get("group_message_ids", [])
        if str(item or "").strip()
    ] if isinstance(record.get("group_message_ids"), list) else []
    for message in messages:
        message_id = str(message.get("message_id") or "")
        if message_id and message_id not in group_message_ids:
            group_message_ids.append(message_id)
    if group_message_ids and group_message_ids != record.get("group_message_ids"):
        record["group_message_ids"] = group_message_ids[-20:]
        changed = True
    merged_count = max(0, len(messages) - 1)
    if record.get("merged_count") != merged_count:
        record["merged_count"] = merged_count
        changed = True
    preview = _combined_request_preview(messages)
    if preview and str(record.get("request_preview") or "") != preview:
        record["request_preview"] = preview
        changed = True
    return changed


def _normalize_active_threads(handoffs: dict[str, dict], *, now: str, now_epoch: float) -> bool:
    changed = False
    for identity, record in list(handoffs.items()):
        changed = _normalize_record(record, identity=identity) or changed
    active_by_scope: dict[tuple[str, str, str, str, str, str, str], list[tuple[str, dict]]] = {}
    for identity, record in handoffs.items():
        if _is_active_thread_record(record, now_epoch=now_epoch):
            active_by_scope.setdefault(_active_thread_key(record), []).append((identity, record))
    for entries in active_by_scope.values():
        if len(entries) <= 1 or not any(str(record.get("status") or "") == "pending" for _identity, record in entries):
            continue
        entries.sort(key=lambda item: (float(item[1].get("created_at_epoch") or 0), item[0]))
        target_id, target = entries[0]
        if str(target.get("status") or "") == "delivered":
            target["status"] = "pending"
            target["reopened_at"] = now
            changed = True
        if not str(target.get("thread_id") or ""):
            target["thread_id"] = target_id
            changed = True
        for source_id, source in entries[1:]:
            previous_target = dict(target)
            previous_source = dict(source)
            target = _merge_existing_record(target, source, source_id=source_id, now=now, now_epoch=now_epoch)
            handoffs[target_id] = target
            handoffs[source_id] = _mark_merged_alias(source, target_id=target_id, now=now, now_epoch=now_epoch)
            changed = changed or previous_target != target or previous_source != handoffs[source_id]
    return changed


def _resolve_alias(handoffs: dict[str, dict], handoff_id: str) -> tuple[str, dict | None]:
    current_id = str(handoff_id or "")
    seen: set[str] = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        record = handoffs.get(current_id)
        if not isinstance(record, dict):
            return current_id, None
        if str(record.get("status") or "") != "merged":
            return current_id, record
        current_id = str(record.get("merged_into") or "")
    return current_id, None


def _merge_group_handoff(
    record: dict,
    *,
    group_message_id: str,
    request_message: str,
    request_summary: str = "",
    request_detail: str = "",
    handoff_reason: str = "",
    now: str,
    now_epoch: float,
) -> dict:
    preview = _preview(request_message)
    if not str(record.get("thread_id") or ""):
        record["thread_id"] = str(record.get("id") or "")
    messages = _request_messages(record)
    if not any(
        str(item.get("message_id") or "") == str(group_message_id or "")
        or str(item.get("text") or "").strip() == preview
        for item in messages
    ):
        messages.append(_message_entry(group_message_id, preview, kind="followup", at=now, at_epoch=now_epoch))
    record["request_messages"] = messages[-20:]
    record["request_preview"] = _combined_request_preview(record["request_messages"])
    record["last_request_preview"] = preview
    if request_summary:
        record["request_summary"] = _preview(request_summary)
    if request_detail:
        record["request_detail"] = _preview(request_detail, limit=1200)
    if handoff_reason:
        record["handoff_reason"] = _preview(handoff_reason)
    record["latest_group_message_id"] = str(group_message_id or "")
    group_message_ids = [
        str(item)
        for item in record.get("group_message_ids", [])
        if str(item or "").strip()
    ] if isinstance(record.get("group_message_ids"), list) else []
    for identity in (str(record.get("group_message_id") or ""), str(group_message_id or "")):
        if identity and identity not in group_message_ids:
            group_message_ids.append(identity)
    record["group_message_ids"] = group_message_ids[-20:]
    record["merged_count"] = max(0, len(record["request_messages"]) - 1)
    if str(record.get("status") or "") == "delivered":
        record["status"] = "pending"
        record["reopened_at"] = now
    record["updated_at"] = now
    record["updated_at_epoch"] = now_epoch
    return record


def register_group_handoff(
    root: Path,
    *,
    digital_run_id: str,
    digital_task_id: str,
    digital_session_key: str,
    group_chat_id: str,
    group_message_id: str,
    open_id: str,
    steward_run_id: str,
    steward_task_id: str,
    request_message: str,
    request_summary: str = "",
    request_detail: str = "",
    handoff_reason: str = "",
    owner_open_id: str = "",
    owner_chat_id: str = "",
    merge_handoff_id: str = "",
    force_new: bool = False,
) -> dict:
    now = utc_now()
    now_epoch = time.time()
    request_preview = _preview(request_message)
    handoff_id = secrets.token_urlsafe(18)
    record = {
        "id": handoff_id,
        "thread_id": handoff_id,
        "digital_run_id": str(digital_run_id or ""),
        "digital_task_id": str(digital_task_id or ""),
        "digital_session_key": str(digital_session_key or ""),
        "group_chat_id": str(group_chat_id or ""),
        "group_message_id": str(group_message_id or ""),
        "open_id": str(open_id or ""),
        "owner_open_id": str(owner_open_id or ""),
        "owner_chat_id": str(owner_chat_id or ""),
        "steward_run_id": str(steward_run_id or ""),
        "steward_task_id": str(steward_task_id or ""),
        "request_fingerprint": hashlib.sha256(str(request_message or "").encode("utf-8")).hexdigest(),
        "request_preview": request_preview,
        "request_summary": _preview(request_summary),
        "request_detail": _preview(request_detail, limit=1200),
        "handoff_reason": _preview(handoff_reason),
        "request_messages": [
            _message_entry(group_message_id, request_preview, kind="original", at=now, at_epoch=now_epoch)
        ],
        "group_message_ids": [str(group_message_id or "")] if str(group_message_id or "") else [],
        "merged_count": 0,
        "status": "pending",
        "created_at": now,
        "created_at_epoch": now_epoch,
        "updated_at": now,
        "updated_at_epoch": now_epoch,
    }
    with _state_lock, _with_lock(root) as handle, exclusive_lock(handle):
        handoffs = _load(root)
        if _normalize_active_threads(handoffs, now=now, now_epoch=now_epoch):
            _save(root, handoffs)
        requested_merge_id = str(merge_handoff_id or "").strip()
        if requested_merge_id:
            resolved_id, existing = _resolve_alias(handoffs, requested_merge_id)
            if isinstance(existing, dict) and _same_group_handoff_scope(
                existing,
                digital_run_id=digital_run_id,
                digital_task_id=digital_task_id,
                group_chat_id=group_chat_id,
                open_id=open_id,
                steward_run_id=steward_run_id,
                steward_task_id=steward_task_id,
            ) and _is_active_thread_record(existing, now_epoch=now_epoch):
                handoffs[resolved_id] = _merge_group_handoff(
                    existing,
                    group_message_id=group_message_id,
                    request_message=request_message,
                    request_summary=request_summary,
                    request_detail=request_detail,
                    handoff_reason=handoff_reason,
                    now=now,
                    now_epoch=now_epoch,
                )
                _save(root, handoffs)
                merged = dict(handoffs[resolved_id])
                merged["merged_existing"] = True
                merged["merge_source"] = "model"
                return merged
        if not force_new:
            active_memo_thread_ids = [
                existing_id
                for existing_id, existing in handoffs.items()
                if str(existing.get("status") or "") == "memo_created"
                and _same_group_handoff_scope(
                    existing,
                    digital_run_id=digital_run_id,
                    digital_task_id=digital_task_id,
                    group_chat_id=group_chat_id,
                    open_id=open_id,
                    steward_run_id=steward_run_id,
                    steward_task_id=steward_task_id,
                )
                and _is_active_thread_record(existing, now_epoch=now_epoch)
            ]
            for existing_id, existing in sorted(
                handoffs.items(),
                key=lambda item: (_record_epoch(item[1]), item[0]),
                reverse=True,
            ):
                existing_status = str(existing.get("status") or "")
                should_auto_merge = existing_status == "pending" or (
                    existing_status == "delivered"
                    and _should_merge_group_handoff(existing, now_epoch=now_epoch, request_message=request_message)
                ) or (
                    existing_status == "memo_created"
                    and len(active_memo_thread_ids) == 1
                    and _should_merge_group_handoff(existing, now_epoch=now_epoch, request_message=request_message)
                )
                if not (
                    _same_group_handoff_scope(
                        existing,
                        digital_run_id=digital_run_id,
                        digital_task_id=digital_task_id,
                        group_chat_id=group_chat_id,
                        open_id=open_id,
                        steward_run_id=steward_run_id,
                        steward_task_id=steward_task_id,
                    )
                    and _is_active_thread_record(existing, now_epoch=now_epoch)
                    and should_auto_merge
                ):
                    continue
                handoffs[existing_id] = _merge_group_handoff(
                    existing,
                    group_message_id=group_message_id,
                    request_message=request_message,
                    request_summary=request_summary,
                    request_detail=request_detail,
                    handoff_reason=handoff_reason,
                    now=now,
                    now_epoch=now_epoch,
                )
                _save(root, handoffs)
                merged = dict(handoffs[existing_id])
                merged["merged_existing"] = True
                merged["merge_source"] = "active_thread"
                return merged
        handoffs[handoff_id] = record
        if len(handoffs) > MAX_GROUP_HANDOFFS:
            handoffs = dict(
                sorted(handoffs.items(), key=lambda item: (float(item[1].get("created_at_epoch") or 0), item[0]))[
                    -MAX_GROUP_HANDOFFS:
                ]
            )
        _save(root, handoffs)
    return dict(record)


def pending_group_handoff_for_steward_reply(root: Path, run_id: str, task_id: str) -> dict | None:
    matches = pending_group_handoffs_for_steward_reply(root, run_id, task_id)
    if not matches:
        return None
    return dict(matches[0])


def pending_group_handoffs_for_steward_reply(root: Path, run_id: str, task_id: str) -> list[dict]:
    with _state_lock, _with_lock(root) as handle, exclusive_lock(handle):
        now = utc_now()
        now_epoch = time.time()
        handoffs = _load(root)
        if _normalize_active_threads(handoffs, now=now, now_epoch=now_epoch):
            _save(root, handoffs)
        matches = [
            dict(record)
            for record in handoffs.values()
            if str(record.get("status") or "") == "pending"
            and str(record.get("steward_run_id") or "") == str(run_id or "")
            and str(record.get("steward_task_id") or "") == str(task_id or "")
        ]
    return sorted(matches, key=lambda item: (float(item.get("created_at_epoch") or 0), str(item.get("id") or "")))


def pending_group_handoffs_for_digital_task(root: Path, run_id: str, task_id: str, *, limit: int = 8) -> list[dict]:
    with _state_lock, _with_lock(root) as handle, exclusive_lock(handle):
        now = utc_now()
        now_epoch = time.time()
        handoffs = _load(root)
        if _normalize_active_threads(handoffs, now=now, now_epoch=now_epoch):
            _save(root, handoffs)
        matches = [
            dict(record)
            for record in handoffs.values()
            if str(record.get("status") or "") == "pending"
            and str(record.get("digital_run_id") or "") == str(run_id or "")
            and str(record.get("digital_task_id") or "") == str(task_id or "")
        ]
    matches.sort(
        key=lambda item: (float(item.get("updated_at_epoch") or item.get("created_at_epoch") or 0), str(item.get("id") or "")),
        reverse=True,
    )
    return matches[: max(1, int(limit or 8))]


def active_group_handoffs_for_digital_task(root: Path, run_id: str, task_id: str, *, limit: int = 8) -> list[dict]:
    now_epoch = time.time()
    with _state_lock, _with_lock(root) as handle, exclusive_lock(handle):
        now = utc_now()
        handoffs = _load(root)
        if _normalize_active_threads(handoffs, now=now, now_epoch=now_epoch):
            _save(root, handoffs)
        matches = [
            dict(record)
            for record in handoffs.values()
            if _is_active_thread_record(record, now_epoch=now_epoch)
            and str(record.get("digital_run_id") or "") == str(run_id or "")
            and str(record.get("digital_task_id") or "") == str(task_id or "")
        ]
    matches.sort(
        key=lambda item: (_record_epoch(item), str(item.get("id") or "")),
        reverse=True,
    )
    return matches[: max(1, int(limit or 8))]


def get_group_handoff(root: Path, handoff_id: str) -> dict | None:
    with _state_lock, _with_lock(root) as handle, exclusive_lock(handle):
        now = utc_now()
        now_epoch = time.time()
        handoffs = _load(root)
        if _normalize_active_threads(handoffs, now=now, now_epoch=now_epoch):
            _save(root, handoffs)
        _resolved_id, record = _resolve_alias(handoffs, str(handoff_id or ""))
    return dict(record) if isinstance(record, dict) else None


def mark_group_handoff(
    root: Path,
    handoff_id: str,
    status: str,
    *,
    error: str = "",
    reason: str = "",
    memo_id: str = "",
    memo_run_id: str = "",
) -> dict | None:
    with _state_lock, _with_lock(root) as handle, exclusive_lock(handle):
        handoffs = _load(root)
        now = utc_now()
        now_epoch = time.time()
        _normalize_active_threads(handoffs, now=now, now_epoch=now_epoch)
        _resolved_id, record = _resolve_alias(handoffs, str(handoff_id or ""))
        if not isinstance(record, dict):
            return None
        record["status"] = str(status or "")
        record["updated_at"] = now
        record["updated_at_epoch"] = now_epoch
        if status == "delivered":
            record["delivered_at"] = record["updated_at"]
        if status == "answered":
            record["delivered_at"] = record.get("delivered_at") or record["updated_at"]
        if status in {"answered", "rejected", "owner_handled", "dismissed", "memo_created", "task_created"}:
            record["closed_at"] = record["updated_at"]
            record[f"{status}_at"] = record["updated_at"]
        if error:
            record["error"] = str(error)[:1000]
        if reason:
            record["terminal_reason"] = str(reason)[:1000]
        if memo_id:
            record["memo_id"] = str(memo_id)
        if memo_run_id:
            record["memo_run_id"] = str(memo_run_id)
        _save(root, handoffs)
    return dict(record)


__all__ = [
    "active_group_handoffs_for_digital_task",
    "feishu_group_handoffs_path",
    "get_group_handoff",
    "GROUP_HANDOFF_ACTIVE_THREAD_SECONDS",
    "GROUP_HANDOFF_MERGE_WINDOW_SECONDS",
    "GROUP_HANDOFF_MEMO_THREAD_SECONDS",
    "mark_group_handoff",
    "pending_group_handoffs_for_digital_task",
    "pending_group_handoff_for_steward_reply",
    "pending_group_handoffs_for_steward_reply",
    "register_group_handoff",
]
