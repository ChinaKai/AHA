from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

from aha_cli.domain.models import utc_now
from aha_cli.locking import exclusive_lock
from aha_cli.services.feishu_audit import audit_feishu_channel
from aha_cli.store.paths import aha_home_path


DEFAULT_BASE_URL = "https://open.feishu.cn"
API_TIMEOUT_SECONDS = 15
TOKEN_REFRESH_SKEW_SECONDS = 60
INBOUND_DEDUPE_TTL_SECONDS = 24 * 60 * 60
INBOUND_DEDUPE_MAX_ENTRIES = 4096
ACTION_TOKEN_TTL_SECONDS = 24 * 60 * 60
ACTION_TOKEN_MAX_ENTRIES = 1024
RECENT_GROUP_MAX_ENTRIES = 100
RECENT_CHAT_MAX_ENTRIES = 100
IDENTITY_PROFILE_MAX_ENTRIES = 300
IDENTITY_PROFILE_LOOKUP_MAX_ITEMS = 8
IDENTITY_PROFILE_LOOKUP_TIMEOUT_SECONDS = 4
IDENTITY_PROFILE_LOOKUP_RETRY_SECONDS = 60
UNSUPPORTED_CARD_PROPERTIES = {"input_value", "initial_value", "default_value"}

UrlOpener = Callable[..., Any]
_state_lock = threading.RLock()


class FeishuError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | str | None = None,
        status: int | None = None,
        response: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.response = response


def feishu_dir(root: Path) -> Path:
    return aha_home_path(root) / "feishu"


def session_bindings_path(root: Path) -> Path:
    return feishu_dir(root) / "session_bindings.json"


def inbound_dedupe_path(root: Path) -> Path:
    return feishu_dir(root) / "inbound_dedupe.json"


def action_tokens_path(root: Path) -> Path:
    return feishu_dir(root) / "action_tokens.json"


def confirmation_cards_path(root: Path) -> Path:
    return feishu_dir(root) / "confirmation_cards.json"


def recent_groups_path(root: Path) -> Path:
    return feishu_dir(root) / "recent_groups.json"


def recent_chats_path(root: Path) -> Path:
    return feishu_dir(root) / "recent_chats.json"


def identity_profiles_path(root: Path) -> Path:
    return feishu_dir(root) / "identity_profiles.json"


def feishu_state_lock_path(root: Path) -> Path:
    return feishu_dir(root) / ".state.lock"


def token_cache_path(root: Path) -> Path:
    return feishu_dir(root) / "tenant_tokens.json"


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_secret_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(8)}.tmp")
    raw = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _locked_state(root: Path):
    """Serialize Feishu security-state mutations across Web/backend processes."""
    lock_path = feishu_state_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _state_lock, lock_path.open("a+b") as handle, exclusive_lock(handle):
        yield


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _first_string(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value not in (None, "") and not isinstance(value, (dict, list, tuple)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _nested(mapping: object, *keys: str) -> object:
    current = mapping
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
        if current is None:
            return None
    return current


def sanitize_card_payload(card: dict) -> dict:
    """Remove card fields rejected by the Feishu parser from new or persisted cards."""

    def clean(value: object) -> object:
        if isinstance(value, dict):
            return {
                str(key): clean(item)
                for key, item in value.items()
                if str(key) not in UNSUPPORTED_CARD_PROPERTIES
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(card) if isinstance(card, dict) else {}


def _content_object(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}
    return parsed if isinstance(parsed, dict) else {"text": raw}


def _post_text(content: dict) -> str:
    localized = content
    for locale in ("zh_cn", "en_us", "ja_jp"):
        if isinstance(content.get(locale), dict):
            localized = content[locale]
            break
    parts: list[str] = []
    title = localized.get("title")
    if isinstance(title, str) and title.strip():
        parts.append(title.strip())
    for paragraph in _list(localized.get("content")):
        line: list[str] = []
        for element in _list(paragraph):
            if not isinstance(element, dict):
                continue
            value = element.get("text")
            if not isinstance(value, str):
                value = element.get("user_name") if element.get("tag") == "at" else ""
            if isinstance(value, str) and value:
                line.append(value)
        if line:
            parts.append("".join(line).strip())
    return "\n".join(part for part in parts if part)


def _message_text(message_type: str, content: dict) -> str:
    if message_type == "post":
        return _post_text(content)
    value = content.get("text")
    if isinstance(value, str):
        return value.strip()
    return ""


def message_resource_attachments(message_type: str, content: dict, *, message_id: str = "") -> list[dict]:
    kind = str(message_type or "").strip()
    if not isinstance(content, dict):
        return []
    attachments: list[dict] = []

    def add(resource_type: str, payload: dict) -> None:
        item = {
            "message_id": str(message_id or ""),
            "type": resource_type,
        }
        for key in (
            "image_key",
            "file_key",
            "media_key",
            "file_name",
            "name",
            "mime_type",
            "file_size",
            "size",
            "duration",
        ):
            value = payload.get(key)
            if value not in (None, ""):
                item[key] = value
        if any(key.endswith("_key") for key in item):
            attachments.append(item)

    if kind in {"image", "img"}:
        add("image", content)
    elif kind in {"file", "folder"}:
        add("file", content)
    elif kind in {"media", "video"}:
        add("media", content)
    elif kind == "audio":
        add("audio", content)
    elif kind == "post":
        for paragraph in _list(content.get("content")):
            for element in _list(paragraph):
                if isinstance(element, dict) and str(element.get("tag") or "") in {"img", "media", "file"}:
                    add(str(element.get("tag") or "attachment"), element)
    return attachments


def message_attachment_summary(attachments: list[dict]) -> str:
    clean = [item for item in attachments if isinstance(item, dict)]
    if not clean:
        return ""
    lines = ["飞书附件："]
    for index, item in enumerate(clean[:8], start=1):
        resource_type = str(item.get("type") or "attachment")
        name = str(item.get("file_name") or item.get("name") or "").strip()
        key = str(item.get("image_key") or item.get("file_key") or item.get("media_key") or "").strip()
        label = f"{resource_type}"
        if name:
            label += f" {name}"
        if key:
            label += f" key={key[:6]}...{key[-4:]}" if len(key) > 12 else f" key={key}"
        lines.append(f"{index}. {label}")
    if len(clean) > 8:
        lines.append(f"... 另有 {len(clean) - 8} 个附件")
    return "\n".join(lines)


def _mention_open_id(mention: dict) -> str:
    identity = _object(mention.get("id"))
    return str(identity.get("open_id") or mention.get("open_id") or "").strip()


def normalize_message_event(payload: dict, *, bot_open_id: str | None = None) -> dict:
    """Normalize a Feishu ``im.message.receive_v1`` event payload.

    Passing ``bot_open_id`` makes bot mention detection exact. Without it, any
    group mention is treated as the bot mention because the least-privilege
    subscription only delivers group messages that mention the application bot.
    """

    if not isinstance(payload, dict):
        raise FeishuError("飞书消息事件必须是 JSON 对象")
    header = _object(payload.get("header"))
    event_type = str(header.get("event_type") or payload.get("event_type") or "").strip()
    if event_type and event_type != "im.message.receive_v1":
        raise FeishuError(f"不支持的飞书事件类型: {event_type}")
    event = _object(payload.get("event"))
    sender = _object(event.get("sender"))
    sender_id = _object(sender.get("sender_id"))
    message = _object(event.get("message"))
    chat = _object(message.get("chat"))
    mentions = [item for item in _list(message.get("mentions")) if isinstance(item, dict)]
    mentioned_open_ids = [value for value in (_mention_open_id(item) for item in mentions) if value]
    expected_bot = str(bot_open_id or "").strip()
    is_at_bot = expected_bot in mentioned_open_ids if expected_bot else bool(mentions)

    message_type = str(message.get("message_type") or "").strip()
    content = _content_object(message.get("content"))
    text = _message_text(message_type, content)
    attachments = message_resource_attachments(message_type, content, message_id=str(message.get("message_id") or "").strip())
    if is_at_bot:
        for mention in mentions:
            mention_id = _mention_open_id(mention)
            if expected_bot and mention_id != expected_bot:
                continue
            key = str(mention.get("key") or "").strip()
            if key:
                text = text.replace(key, " ")
        text = " ".join(text.split())

    root_id = str(message.get("root_id") or "").strip()
    thread_id = str(message.get("thread_id") or root_id).strip()
    tenant_key = str(sender.get("tenant_key") or header.get("tenant_key") or "").strip()
    return {
        "event_type": event_type or "im.message.receive_v1",
        "tenant_key": tenant_key,
        "open_id": str(sender_id.get("open_id") or "").strip(),
        "user_id": str(sender_id.get("user_id") or sender.get("user_id") or "").strip(),
        "union_id": str(sender_id.get("union_id") or sender.get("union_id") or "").strip(),
        "sender_name": _first_string(
            sender.get("sender_name"),
            sender.get("name"),
            sender.get("display_name"),
            sender.get("user_name"),
            _nested(sender, "sender", "name"),
            _nested(sender, "user", "name"),
        ),
        "chat_id": str(message.get("chat_id") or "").strip(),
        "chat_name": _first_string(message.get("chat_name"), chat.get("name"), chat.get("chat_name")),
        "chat_type": str(message.get("chat_type") or "").strip(),
        "message_id": str(message.get("message_id") or "").strip(),
        "root_id": root_id,
        "thread_id": thread_id,
        "parent_id": str(message.get("parent_id") or "").strip(),
        "message_type": message_type,
        "text": text,
        "attachments": attachments,
        "is_at_bot": is_at_bot,
        "mentioned_open_ids": mentioned_open_ids,
        "sender_type": str(sender.get("sender_type") or "").strip(),
    }


def make_session_key(*, tenant_key: str, open_id: str, chat_id: str, chat_type: str) -> str:
    tenant = str(tenant_key or "").strip()
    kind = str(chat_type or "").strip().lower()
    if not tenant:
        raise FeishuError("tenant_key 不能为空")
    if kind == "p2p":
        identity = str(open_id or "").strip()
        if not identity:
            raise FeishuError("单聊会话缺少 open_id")
        return f"{tenant}:p2p:{identity}"
    if kind != "group":
        raise FeishuError(f"不支持的飞书会话类型: {kind or '-'}")
    identity = str(chat_id or "").strip()
    if not identity:
        raise FeishuError("群聊会话缺少 chat_id")
    return f"{tenant}:group:{identity}"


def session_key_for_message(message: dict) -> str:
    return make_session_key(
        tenant_key=str(message.get("tenant_key") or ""),
        open_id=str(message.get("open_id") or ""),
        chat_id=str(message.get("chat_id") or ""),
        chat_type=str(message.get("chat_type") or ""),
    )


def should_handle_message(message: dict) -> bool:
    return str(message.get("chat_type") or "").lower() == "p2p" or bool(message.get("is_at_bot"))


def load_session_bindings(root: Path) -> dict[str, dict]:
    payload = _read_json(session_bindings_path(root))
    bindings = payload.get("bindings")
    if not isinstance(bindings, dict):
        return {}
    return {str(key): value for key, value in bindings.items() if isinstance(value, dict)}


def get_session_binding(root: Path, session_key: str) -> dict | None:
    binding = load_session_bindings(root).get(session_key)
    return dict(binding) if binding is not None else None


def set_session_binding(
    root: Path,
    session_key: str,
    *,
    active_run_id: str | None,
    active_task_id: str | None,
    acl_subject: str,
) -> dict:
    key = str(session_key or "").strip()
    subject = str(acl_subject or "").strip()
    if not key or not subject:
        raise FeishuError("session_key 和 acl_subject 不能为空")
    binding = {
        "active_run_id": str(active_run_id or "").strip() or None,
        "active_task_id": str(active_task_id or "").strip() or None,
        "acl_subject": subject,
    }
    with _state_lock:
        bindings = load_session_bindings(root)
        bindings[key] = binding
        _write_secret_json(session_bindings_path(root), {"version": 1, "bindings": bindings})
    return dict(binding)


def claim_inbound_message(
    root: Path,
    message_id: str,
    *,
    now: float | None = None,
    ttl_seconds: int = INBOUND_DEDUPE_TTL_SECONDS,
    max_entries: int = INBOUND_DEDUPE_MAX_ENTRIES,
) -> bool:
    identity = str(message_id or "").strip()
    if not identity:
        raise FeishuError("message_id 不能为空")
    if ttl_seconds <= 0 or max_entries <= 0:
        raise FeishuError("幂等 TTL 和容量必须大于 0")
    current = time.time() if now is None else float(now)
    cutoff = current - ttl_seconds
    with _state_lock:
        payload = _read_json(inbound_dedupe_path(root))
        raw_entries = payload.get("messages")
        entries = {
            str(key): float(value)
            for key, value in (raw_entries.items() if isinstance(raw_entries, dict) else ())
            if isinstance(value, (int, float)) and float(value) > cutoff
        }
        if identity in entries:
            _write_secret_json(inbound_dedupe_path(root), {"version": 1, "messages": entries})
            return False
        entries[identity] = current
        if len(entries) > max_entries:
            entries = dict(sorted(entries.items(), key=lambda item: (item[1], item[0]))[-max_entries:])
        _write_secret_json(inbound_dedupe_path(root), {"version": 1, "messages": entries})
    return True


def _identity_fingerprint(identity: str) -> str:
    return hashlib.sha256(str(identity or "").encode("utf-8")).hexdigest()[:12]


def _trim_recent(records: dict[str, dict], *, limit: int) -> dict[str, dict]:
    if len(records) <= limit:
        return records
    ordered = sorted(
        records.items(),
        key=lambda item: (str(item[1].get("last_seen_at") or item[1].get("updated_at") or ""), item[0]),
        reverse=True,
    )[:limit]
    return dict(ordered)


def _profile_bucket(kind: str) -> str:
    return "open_ids" if kind == "open_id" else "chat_ids"


def _profile_record(kind: str, identity: str, *, display_name: str = "", chat_type: str = "", seen_at: str = "") -> dict:
    record = {
        "kind": kind,
        "id": identity,
        "id_hash": _identity_fingerprint(identity),
        "updated_at": seen_at or utc_now(),
    }
    if kind == "open_id":
        record["open_id"] = identity
    elif kind == "chat_id":
        record["chat_id"] = identity
    if display_name:
        record["display_name"] = display_name
    if chat_type:
        record["chat_type"] = chat_type
    return record


def _profiles_from_payload(payload: dict) -> dict[str, dict[str, dict]]:
    result = {"open_ids": {}, "chat_ids": {}}
    for bucket in result:
        raw_items = payload.get(bucket)
        if not isinstance(raw_items, dict):
            continue
        result[bucket] = {
            str(key): dict(value)
            for key, value in raw_items.items()
            if str(key).strip() and isinstance(value, dict)
        }
    return result


def _upsert_profile(
    profiles: dict[str, dict[str, dict]],
    *,
    kind: str,
    identity: str,
    display_name: str = "",
    chat_type: str = "",
    seen_at: str = "",
) -> dict:
    clean_identity = str(identity or "").strip()
    if not clean_identity:
        return {}
    bucket = _profile_bucket(kind)
    records = profiles.setdefault(bucket, {})
    current = dict(records.get(clean_identity) or {})
    merged = _profile_record(kind, clean_identity, display_name=display_name, chat_type=chat_type, seen_at=seen_at)
    if current.get("display_name") and not merged.get("display_name"):
        merged["display_name"] = current["display_name"]
    if current.get("chat_type") and not merged.get("chat_type"):
        merged["chat_type"] = current["chat_type"]
    current.update(merged)
    if seen_at and display_name:
        current.pop("lookup_failed_at", None)
        current.pop("lookup_error", None)
    elif seen_at and not display_name:
        current.pop("lookup_failed_at", None)
        current.pop("lookup_error", None)
    records[clean_identity] = current
    profiles[bucket] = _trim_recent(records, limit=IDENTITY_PROFILE_MAX_ENTRIES)
    return dict(current)


def _mark_profile_lookup_failed(
    root: Path,
    *,
    kind: str,
    identity: str,
    error: str,
    seen_at: str | None = None,
) -> None:
    clean_kind = "chat_id" if str(kind or "").strip() == "chat_id" else "open_id"
    clean_identity = str(identity or "").strip()
    if not clean_identity:
        return
    timestamp = str(seen_at or utc_now())
    with _locked_state(root):
        profiles = _profiles_from_payload(_read_json(identity_profiles_path(root)))
        record = _upsert_profile(
            profiles,
            kind=clean_kind,
            identity=clean_identity,
            seen_at=timestamp,
        )
        record["lookup_failed_at"] = timestamp
        record["lookup_error"] = str(error or "")[:240]
        profiles[_profile_bucket(clean_kind)][clean_identity] = record
        _write_secret_json(identity_profiles_path(root), {"version": 1, **profiles})


def _profile_needs_lookup(profile: dict, *, now: float | None = None) -> bool:
    if str(profile.get("display_name") or "").strip():
        return False
    failed_at = str(profile.get("lookup_failed_at") or "").strip()
    if not failed_at:
        return True
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(failed_at.replace("Z", "+00:00"))
        current = time.time() if now is None else float(now)
        return current - parsed.astimezone(timezone.utc).timestamp() >= IDENTITY_PROFILE_LOOKUP_RETRY_SECONDS
    except (TypeError, ValueError, OSError):
        return True


def remember_identity_profile(
    root: Path,
    *,
    kind: str,
    identity: str,
    display_name: str = "",
    chat_type: str = "",
    seen_at: str | None = None,
) -> dict:
    clean_kind = "chat_id" if str(kind or "").strip() == "chat_id" else "open_id"
    timestamp = str(seen_at or utc_now())
    with _locked_state(root):
        profiles = _profiles_from_payload(_read_json(identity_profiles_path(root)))
        record = _upsert_profile(
            profiles,
            kind=clean_kind,
            identity=identity,
            display_name=str(display_name or "").strip(),
            chat_type=str(chat_type or "").strip(),
            seen_at=timestamp,
        )
        _write_secret_json(identity_profiles_path(root), {"version": 1, **profiles})
    return record


def identity_profiles(root: Path) -> dict[str, dict[str, dict]]:
    with _locked_state(root):
        profiles = _profiles_from_payload(_read_json(identity_profiles_path(root)))
    return {"open_ids": dict(profiles["open_ids"]), "chat_ids": dict(profiles["chat_ids"])}


def identity_label_items(root: Path, *, kind: str, identities: list[str]) -> list[dict]:
    bucket = _profile_bucket("chat_id" if str(kind or "") == "chat_id" else "open_id")
    profiles = identity_profiles(root).get(bucket, {})
    items: list[dict] = []
    for identity in identities:
        clean_identity = str(identity or "").strip()
        if not clean_identity:
            continue
        profile = dict(profiles.get(clean_identity) or {})
        item = {
            "kind": "chat_id" if bucket == "chat_ids" else "open_id",
            "id": clean_identity,
            "id_hash": _identity_fingerprint(clean_identity),
        }
        if bucket == "chat_ids":
            item["chat_id"] = clean_identity
        else:
            item["open_id"] = clean_identity
        item.update({key: value for key, value in profile.items() if value not in (None, "")})
        items.append(item)
    return items


def _recent_chats_from_payload(payload: dict) -> dict[str, dict[str, dict]]:
    result = {"groups": {}, "private_chats": {}}
    for bucket in result:
        raw_items = payload.get(bucket)
        if not isinstance(raw_items, dict):
            continue
        result[bucket] = {
            str(key): dict(value)
            for key, value in raw_items.items()
            if str(key).strip() and isinstance(value, dict)
        }
    return result


def _sort_recent(values: list[dict], *, id_key: str, limit: int) -> list[dict]:
    values.sort(
        key=lambda item: (str(item.get("last_seen_at") or ""), str(item.get(id_key) or "")),
        reverse=True,
    )
    return values[:limit]


def record_recent_group(
    root: Path,
    chat_id: str,
    *,
    seen_at: str | None = None,
    display_name: str = "",
) -> dict:
    """Remember a detected group as an admin-only authorization candidate."""
    identity = str(chat_id or "").strip()
    if not identity:
        raise FeishuError("chat_id 不能为空")
    timestamp = str(seen_at or utc_now())
    with _locked_state(root):
        legacy_payload = _read_json(recent_groups_path(root))
        raw_groups = legacy_payload.get("groups")
        groups = {
            str(key): value
            for key, value in (raw_groups.items() if isinstance(raw_groups, dict) else ())
            if isinstance(value, dict) and str(key).strip()
        }
        group_record = {
            "chat_id": identity,
            "chat_id_hash": _identity_fingerprint(identity),
            "last_seen_at": timestamp,
        }
        clean_name = str(display_name or "").strip()
        if clean_name:
            group_record["display_name"] = clean_name
        elif isinstance(groups.get(identity), dict) and groups[identity].get("display_name"):
            group_record["display_name"] = str(groups[identity]["display_name"])
        groups[identity] = group_record
        groups = _trim_recent(groups, limit=RECENT_GROUP_MAX_ENTRIES)
        _write_secret_json(recent_groups_path(root), {"version": 1, "groups": groups})

        recent_payload = _recent_chats_from_payload(_read_json(recent_chats_path(root)))
        recent_payload["groups"][identity] = dict(group_record)
        recent_payload["groups"] = _trim_recent(recent_payload["groups"], limit=RECENT_CHAT_MAX_ENTRIES)
        _write_secret_json(recent_chats_path(root), {"version": 1, **recent_payload})

        profiles = _profiles_from_payload(_read_json(identity_profiles_path(root)))
        _upsert_profile(
            profiles,
            kind="chat_id",
            identity=identity,
            display_name=str(group_record.get("display_name") or ""),
            chat_type="group",
            seen_at=timestamp,
        )
        _write_secret_json(identity_profiles_path(root), {"version": 1, **profiles})
    return dict(groups[identity])


def record_recent_private_chat(
    root: Path,
    *,
    chat_id: str,
    open_id: str,
    seen_at: str | None = None,
    display_name: str = "",
) -> dict:
    """Remember a detected owner/user private chat for settings and owner binding."""
    user_identity = str(open_id or "").strip()
    conversation_id = str(chat_id or "").strip()
    if not user_identity and not conversation_id:
        raise FeishuError("open_id 或 chat_id 不能为空")
    key = user_identity or conversation_id
    timestamp = str(seen_at or utc_now())
    clean_name = str(display_name or "").strip()
    with _locked_state(root):
        recent_payload = _recent_chats_from_payload(_read_json(recent_chats_path(root)))
        current = dict(recent_payload["private_chats"].get(key) or {})
        record = {
            "open_id": user_identity,
            "open_id_hash": _identity_fingerprint(user_identity) if user_identity else "",
            "chat_id": conversation_id,
            "chat_id_hash": _identity_fingerprint(conversation_id) if conversation_id else "",
            "last_seen_at": timestamp,
        }
        if clean_name:
            record["display_name"] = clean_name
        elif current.get("display_name"):
            record["display_name"] = str(current["display_name"])
        recent_payload["private_chats"][key] = record
        recent_payload["private_chats"] = _trim_recent(recent_payload["private_chats"], limit=RECENT_CHAT_MAX_ENTRIES)
        _write_secret_json(recent_chats_path(root), {"version": 1, **recent_payload})

        profiles = _profiles_from_payload(_read_json(identity_profiles_path(root)))
        if user_identity:
            _upsert_profile(
                profiles,
                kind="open_id",
                identity=user_identity,
                display_name=str(record.get("display_name") or ""),
                chat_type="p2p",
                seen_at=timestamp,
            )
        if conversation_id:
            _upsert_profile(
                profiles,
                kind="chat_id",
                identity=conversation_id,
                display_name=str(record.get("display_name") or ""),
                chat_type="p2p",
                seen_at=timestamp,
            )
        _write_secret_json(identity_profiles_path(root), {"version": 1, **profiles})
    return dict(record)


def recent_groups(root: Path, *, limit: int = 20) -> list[dict]:
    """Return detected group IDs to the authenticated local settings UI."""
    if limit <= 0:
        return []
    with _locked_state(root):
        payload = _read_json(recent_groups_path(root))
        chats_payload = _recent_chats_from_payload(_read_json(recent_chats_path(root)))
        profiles = _profiles_from_payload(_read_json(identity_profiles_path(root)))
    raw_groups = payload.get("groups")
    groups_by_id = {
        str(value.get("chat_id") or key): dict(value)
        for key, value in (raw_groups.items() if isinstance(raw_groups, dict) else ())
        if isinstance(value, dict) and str(value.get("chat_id") or key).strip()
    }
    for key, value in chats_payload["groups"].items():
        chat_id = str(value.get("chat_id") or key).strip()
        if chat_id:
            groups_by_id[chat_id] = {**groups_by_id.get(chat_id, {}), **dict(value)}
    for chat_id, value in list(groups_by_id.items()):
        profile = profiles["chat_ids"].get(chat_id)
        if isinstance(profile, dict):
            groups_by_id[chat_id] = {**dict(value), **{key: item for key, item in profile.items() if item not in (None, "")}}
    return _sort_recent(list(groups_by_id.values()), id_key="chat_id", limit=limit)


def recent_private_chats(root: Path, *, limit: int = 20) -> list[dict]:
    """Return detected p2p chat IDs to the authenticated local settings UI."""
    if limit <= 0:
        return []
    with _locked_state(root):
        payload = _recent_chats_from_payload(_read_json(recent_chats_path(root)))
        profiles = _profiles_from_payload(_read_json(identity_profiles_path(root)))
    values = []
    for value in payload["private_chats"].values():
        if not isinstance(value, dict):
            continue
        item = dict(value)
        open_id = str(item.get("open_id") or "").strip()
        chat_id = str(item.get("chat_id") or "").strip()
        if open_id and isinstance(profiles["open_ids"].get(open_id), dict):
            item = {**item, **{key: data for key, data in profiles["open_ids"][open_id].items() if data not in (None, "")}}
            item["open_id"] = open_id
        if chat_id and isinstance(profiles["chat_ids"].get(chat_id), dict):
            chat_profile = profiles["chat_ids"][chat_id]
            if not item.get("display_name") and chat_profile.get("display_name"):
                item["display_name"] = chat_profile["display_name"]
            item["chat_id_hash"] = str(chat_profile.get("id_hash") or item.get("chat_id_hash") or "")
        values.append(item)
    return _sort_recent(values, id_key="open_id", limit=limit)


def _action_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_action_tokens(payload: dict, now: float) -> dict[str, dict]:
    raw_tokens = payload.get("tokens")
    if not isinstance(raw_tokens, dict):
        return {}
    return {
        str(key): value
        for key, value in raw_tokens.items()
        if isinstance(value, dict) and float(value.get("expires_at") or 0) > now
    }


def _confirmation_records(payload: dict) -> dict[str, dict]:
    raw = payload.get("confirmations")
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def terminal_confirmation_card(card: dict, state: str, detail: str = "") -> dict:
    """Return a Schema 2.0 card with actions removed and a terminal status."""
    result = sanitize_card_payload(card)
    labels = {
        "confirmed": ("操作已确认", "grey", "已确认并提交 AHA 执行。"),
        "selected": ("已选择方案", "grey", "已收到选择，AHA 助手将继续处理。"),
        "cancelled": ("操作已取消", "grey", "已取消，本操作不会执行。"),
        "expired": ("确认已失效", "grey", "已超过 24 小时有效期，请重新发起操作。"),
        "stale": ("确认已失效", "grey", "目标状态已变化，请重新发起操作。"),
        "failed": ("操作执行失败", "red", "执行失败，请检查结果后重试。"),
    }
    title, template, description = labels.get(state, ("操作已处理", "grey", "本操作已处理。"))
    header = result.get("header") if isinstance(result.get("header"), dict) else {}
    header["title"] = {"tag": "plain_text", "content": title}
    header["template"] = template
    result["header"] = header
    body = result.get("body") if isinstance(result.get("body"), dict) else {}

    def terminal_element(item: object) -> dict | None:
        if not isinstance(item, dict):
            return None
        if item.get("tag") in {"button", "column_set", "form"}:
            return None
        cloned = dict(item)
        if isinstance(cloned.get("elements"), list):
            cloned["elements"] = [
                child
                for child in (terminal_element(child) for child in cloned["elements"])
                if child is not None
            ]
        return cloned

    elements = [
        item
        for item in (terminal_element(item) for item in (body.get("elements") if isinstance(body.get("elements"), list) else []))
        if item is not None
    ]
    if elements and isinstance(elements[-1], dict) and elements[-1].get("tag") == "markdown":
        elements.pop()
    suffix = f"\n{detail.strip()}" if detail.strip() else ""
    elements.append({"tag": "markdown", "content": f"<font color='grey'>{description}{suffix}</font>"})
    body["elements"] = elements
    result["body"] = body
    return result


def register_confirmation_card(
    root: Path,
    confirmation_id: str,
    *,
    open_id: str,
    session_key: str,
    action: str,
    card: dict,
    expires_at: float,
    now: float | None = None,
) -> dict:
    identity = str(confirmation_id or "").strip()
    if not identity or not isinstance(card, dict):
        raise FeishuError("confirmation_id 和 card 不能为空")
    card = sanitize_card_payload(card)
    current = time.time() if now is None else float(now)
    with _locked_state(root):
        records = _confirmation_records(_read_json(confirmation_cards_path(root)))
        for record in records.values():
            if (
                str(record.get("state") or "pending") == "pending"
                and str(record.get("open_id") or "") == str(open_id or "").strip()
                and str(record.get("session_key") or "") == str(session_key or "").strip()
                and str(record.get("action") or "") == str(action or "").strip()
            ):
                record["state"] = "stale"
                record["resolved_at"] = current
                record["terminal_card"] = terminal_confirmation_card(
                    record.get("card") if isinstance(record.get("card"), dict) else {},
                    "stale",
                    "新的确认请求已替代本卡片。",
                )
                record["card_updated"] = False
        record = {
            "confirmation_id": identity,
            "open_id": str(open_id or "").strip(),
            "session_key": str(session_key or "").strip(),
            "action": str(action or "").strip(),
            "card": card,
            "state": "pending",
            "message_id": "",
            "chat_id": "",
            "issued_at": current,
            "expires_at": float(expires_at),
            "card_updated": False,
        }
        records[identity] = record
        if len(records) > ACTION_TOKEN_MAX_ENTRIES:
            records = dict(
                sorted(records.items(), key=lambda item: (float(item[1].get("issued_at") or 0), item[0]))[
                    -ACTION_TOKEN_MAX_ENTRIES:
                ]
            )
        _write_secret_json(confirmation_cards_path(root), {"version": 1, "confirmations": records})
    return dict(record)


def bind_confirmation_card(root: Path, confirmation_id: str, *, message_id: str, chat_id: str) -> dict:
    identity = str(confirmation_id or "").strip()
    with _locked_state(root):
        records = _confirmation_records(_read_json(confirmation_cards_path(root)))
        record = records.get(identity)
        if not isinstance(record, dict):
            raise FeishuError("confirmation card 不存在", code="confirmation_not_found")
        record["message_id"] = str(message_id or "").strip()
        record["chat_id"] = str(chat_id or "").strip()
        _write_secret_json(confirmation_cards_path(root), {"version": 1, "confirmations": records})
    return dict(record)


def _remove_confirmation_token(tokens: dict[str, dict], confirmation_id: str) -> tuple[dict, dict[str, dict]]:
    for digest, record in list(tokens.items()):
        context = record.get("context") if isinstance(record.get("context"), dict) else {}
        if str(context.get("confirmation_id") or "") == confirmation_id:
            tokens.pop(digest, None)
            return record, tokens
    return {}, tokens


def consume_confirmation_card(
    root: Path,
    *,
    message_id: str,
    open_id: str,
    session_key: str,
    action: str,
    decision: str,
    now: float | None = None,
) -> dict:
    """Consume the exact confirmation bound to a clicked Feishu message."""
    current = time.time() if now is None else float(now)
    with _locked_state(root):
        records = _confirmation_records(_read_json(confirmation_cards_path(root)))
        match = next((item for item in records.values() if str(item.get("message_id") or "") == str(message_id or "")), None)
        if not isinstance(match, dict):
            raise FeishuError("该确认卡片不存在或尚未绑定", code="confirmation_not_found")
        confirmation_id = str(match.get("confirmation_id") or "")
        if str(match.get("state") or "pending") != "pending":
            raise FeishuError("该确认卡片已处理或失效", code="confirmation_already_resolved")
        expected = (str(open_id or "").strip(), str(session_key or "").strip(), str(action or "").strip())
        actual = (
            str(match.get("open_id") or ""),
            str(match.get("session_key") or ""),
            str(match.get("action") or ""),
        )
        if actual != expected:
            raise FeishuError("确认卡片身份、会话或操作不匹配", code="action_token_mismatch")
        tokens = _valid_action_tokens(_read_json(action_tokens_path(root)), current)
        token_record, tokens = _remove_confirmation_token(tokens, confirmation_id)
        if float(match.get("expires_at") or 0) <= current or not token_record:
            match["state"] = "expired"
            match["resolved_at"] = current
            match["terminal_card"] = terminal_confirmation_card(match.get("card") or {}, "expired")
            match["card_updated"] = False
            _write_secret_json(action_tokens_path(root), {"version": 1, "tokens": tokens})
            _write_secret_json(confirmation_cards_path(root), {"version": 1, "confirmations": records})
            raise FeishuError("该确认卡片已过期", code="expired_action_token")
        match["state"] = "processing"
        match["decision"] = str(decision or "")
        match["resolved_at"] = current
        _write_secret_json(action_tokens_path(root), {"version": 1, "tokens": tokens})
        _write_secret_json(confirmation_cards_path(root), {"version": 1, "confirmations": records})
    context = token_record.get("context") if isinstance(token_record.get("context"), dict) else {}
    return {**context, "confirmation_id": confirmation_id, "confirmation_message_id": str(message_id or "")}


def finalize_confirmation_card(root: Path, confirmation_id: str, state: str, detail: str = "") -> dict | None:
    identity = str(confirmation_id or "").strip()
    if not identity:
        return None
    with _locked_state(root):
        records = _confirmation_records(_read_json(confirmation_cards_path(root)))
        record = records.get(identity)
        if not isinstance(record, dict):
            return None
        record["state"] = str(state or "processed")
        record["resolved_at"] = time.time()
        record["terminal_card"] = terminal_confirmation_card(record.get("card") or {}, record["state"], detail)
        record["card_updated"] = False
        _write_secret_json(confirmation_cards_path(root), {"version": 1, "confirmations": records})
    return dict(record)


def confirmation_card_for_message(root: Path, message_id: str) -> dict | None:
    records = _confirmation_records(_read_json(confirmation_cards_path(root)))
    record = next((item for item in records.values() if str(item.get("message_id") or "") == str(message_id or "")), None)
    return dict(record) if isinstance(record, dict) else None


def pending_confirmation_card_updates(root: Path, *, now: float | None = None) -> list[dict]:
    """Mark overdue records expired and return terminal cards still needing PATCH."""
    current = time.time() if now is None else float(now)
    with _locked_state(root):
        records = _confirmation_records(_read_json(confirmation_cards_path(root)))
        changed = False
        for record in records.values():
            if str(record.get("state") or "pending") == "pending" and float(record.get("expires_at") or 0) <= current:
                record["state"] = "expired"
                record["resolved_at"] = current
                record["terminal_card"] = terminal_confirmation_card(record.get("card") or {}, "expired")
                record["card_updated"] = False
                changed = True
        if changed:
            _write_secret_json(confirmation_cards_path(root), {"version": 1, "confirmations": records})
        return [
            dict(record)
            for record in records.values()
            if str(record.get("state") or "pending") not in {"pending", "processing"}
            and str(record.get("message_id") or "")
            and isinstance(record.get("terminal_card"), dict)
            and not record.get("card_updated")
        ]


def mark_confirmation_card_updated(root: Path, confirmation_id: str) -> None:
    with _locked_state(root):
        records = _confirmation_records(_read_json(confirmation_cards_path(root)))
        record = records.get(str(confirmation_id or ""))
        if not isinstance(record, dict):
            return
        record["card_updated"] = True
        record["card_updated_at"] = time.time()
        _write_secret_json(confirmation_cards_path(root), {"version": 1, "confirmations": records})


def sanitize_confirmation_cards(root: Path) -> dict:
    with _locked_state(root):
        records = _confirmation_records(_read_json(confirmation_cards_path(root)))
        sanitized_count = 0
        for record in records.values():
            if not isinstance(record, dict):
                continue
            for key in ("card", "terminal_card", "confirmation_card"):
                if not isinstance(record.get(key), dict):
                    continue
                before = json.dumps(record.get(key), ensure_ascii=False, sort_keys=True)
                cleaned = sanitize_card_payload(record.get(key) or {})
                if json.dumps(cleaned, ensure_ascii=False, sort_keys=True) != before:
                    record[key] = cleaned
                    sanitized_count += 1
        if sanitized_count:
            _write_secret_json(confirmation_cards_path(root), {"version": 1, "confirmations": records})
        return {"sanitized_count": sanitized_count}


def issue_action_token(
    root: Path,
    *,
    open_id: str,
    session_key: str,
    action: str,
    context: dict | None = None,
    ttl_seconds: int = ACTION_TOKEN_TTL_SECONDS,
    max_entries: int = ACTION_TOKEN_MAX_ENTRIES,
    now: float | None = None,
) -> str:
    identity = str(open_id or "").strip()
    session = str(session_key or "").strip()
    action_name = str(action or "").strip()
    if not identity or not session or not action_name:
        raise FeishuError("action token 必须绑定 open_id、session_key 和 action")
    if ttl_seconds <= 0 or max_entries <= 0:
        raise FeishuError("action token TTL 和容量必须大于 0")
    current = time.time() if now is None else float(now)
    token = secrets.token_urlsafe(32)
    digest = _action_token_digest(token)
    record = {
        "open_id": identity,
        "session_key": session,
        "action": action_name,
        "context": context if isinstance(context, dict) else {},
        "issued_at": current,
        "expires_at": current + ttl_seconds,
    }
    with _locked_state(root):
        tokens = _valid_action_tokens(_read_json(action_tokens_path(root)), current)
        tokens = {
            key: value
            for key, value in tokens.items()
            if (
                str(value.get("open_id") or ""),
                str(value.get("session_key") or ""),
                str(value.get("action") or ""),
            )
            != (identity, session, action_name)
        }
        tokens[digest] = record
        if len(tokens) > max_entries:
            tokens = dict(
                sorted(tokens.items(), key=lambda item: (float(item[1].get("issued_at") or 0), item[0]))[-max_entries:]
            )
        _write_secret_json(action_tokens_path(root), {"version": 1, "tokens": tokens})
    return token


def consume_action_token(
    root: Path,
    token: str,
    *,
    open_id: str,
    session_key: str,
    action: str,
    now: float | None = None,
) -> dict:
    raw_token = str(token or "").strip()
    if not raw_token:
        raise FeishuError("action token 不能为空")
    current = time.time() if now is None else float(now)
    digest = _action_token_digest(raw_token)
    with _locked_state(root):
        payload = _read_json(action_tokens_path(root))
        raw_tokens = payload.get("tokens")
        raw_tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
        record = raw_tokens.get(digest)
        if not isinstance(record, dict):
            raise FeishuError("action token 无效或已使用", code="invalid_action_token")
        if float(record.get("expires_at") or 0) <= current:
            raw_tokens.pop(digest, None)
            tokens = _valid_action_tokens({"tokens": raw_tokens}, current)
            _write_secret_json(action_tokens_path(root), {"version": 1, "tokens": tokens})
            raise FeishuError("action token 已过期", code="expired_action_token")
        expected = (
            str(open_id or "").strip(),
            str(session_key or "").strip(),
            str(action or "").strip(),
        )
        actual = (
            str(record.get("open_id") or ""),
            str(record.get("session_key") or ""),
            str(record.get("action") or ""),
        )
        if actual != expected:
            raise FeishuError("action token 身份、会话或操作不匹配", code="action_token_mismatch")
        raw_tokens.pop(digest, None)
        tokens = _valid_action_tokens({"tokens": raw_tokens}, current)
        _write_secret_json(action_tokens_path(root), {"version": 1, "tokens": tokens})
    context = record.get("context")
    return dict(context) if isinstance(context, dict) else {}


def consume_pending_action_token(
    root: Path,
    *,
    open_id: str,
    session_key: str,
    action: str,
    now: float | None = None,
) -> dict:
    """Consume the single pending action for an actor without exposing its raw token."""
    current = time.time() if now is None else float(now)
    expected = (
        str(open_id or "").strip(),
        str(session_key or "").strip(),
        str(action or "").strip(),
    )
    if not all(expected):
        raise FeishuError("待确认操作必须绑定 open_id、session_key 和 action")
    with _locked_state(root):
        tokens = _valid_action_tokens(_read_json(action_tokens_path(root)), current)
        matches = [
            (digest, record)
            for digest, record in tokens.items()
            if (
                str(record.get("open_id") or ""),
                str(record.get("session_key") or ""),
                str(record.get("action") or ""),
            )
            == expected
        ]
        if not matches:
            _write_secret_json(action_tokens_path(root), {"version": 1, "tokens": tokens})
            raise FeishuError("当前会话没有待确认操作，或操作已过期", code="pending_action_not_found")
        if len(matches) != 1:
            raise FeishuError("当前会话存在多个待确认操作，请重新发起", code="ambiguous_pending_action")
        digest, record = matches[0]
        tokens.pop(digest, None)
        _write_secret_json(action_tokens_path(root), {"version": 1, "tokens": tokens})
        context = record.get("context") if isinstance(record.get("context"), dict) else {}
        confirmation_id = str(context.get("confirmation_id") or "")
        if confirmation_id:
            records = _confirmation_records(_read_json(confirmation_cards_path(root)))
            confirmation = records.get(confirmation_id)
            if isinstance(confirmation, dict) and str(confirmation.get("state") or "pending") == "pending":
                confirmation["state"] = "processing"
                confirmation["decision"] = "text"
                confirmation["resolved_at"] = current
                _write_secret_json(confirmation_cards_path(root), {"version": 1, "confirmations": records})
    context = record.get("context")
    return dict(context) if isinstance(context, dict) else {}


def _api_url(base_url: str, endpoint: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", endpoint.lstrip("/"))


def _request_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    body: dict | None = None,
    opener: UrlOpener | None = None,
    timeout: int = API_TIMEOUT_SECONDS,
) -> dict:
    raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if raw_body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=raw_body, headers=headers, method=method)
    open_request = opener or urlopen
    try:
        with open_request(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw_response = response.read()
    except HTTPError as exc:
        try:
            raw_error = exc.read().decode("utf-8", errors="replace")
            parsed_error = json.loads(raw_error) if raw_error else {}
        except (OSError, json.JSONDecodeError):
            parsed_error = {}
        code = parsed_error.get("code") if isinstance(parsed_error, dict) else None
        message = str(parsed_error.get("msg") or parsed_error.get("message") or exc.reason) if isinstance(parsed_error, dict) else str(exc.reason)
        raise FeishuError(
            f"飞书 API HTTP {exc.code}: {message}",
            code=code,
            status=exc.code,
            response=parsed_error if isinstance(parsed_error, dict) else None,
        ) from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise FeishuError(f"飞书 API 请求失败: {exc}") from exc
    except Exception as exc:  # Mock transports and custom openers may use their own error types.
        raise FeishuError(f"飞书 API 请求失败: {exc}") from exc
    try:
        payload = json.loads(raw_response.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeishuError("飞书 API 返回非 JSON", status=status) from exc
    if not isinstance(payload, dict):
        raise FeishuError("飞书 API 返回格式无效", status=status)
    code = payload.get("code", 0)
    if code not in (None, 0):
        message = str(payload.get("msg") or payload.get("message") or "unknown error")
        raise FeishuError(f"飞书 API 错误 {code}: {message}", code=code, status=status, response=payload)
    return payload


def get_tenant_access_token(
    root: Path,
    app_id: str,
    app_secret: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    opener: UrlOpener | None = None,
    timeout: int = API_TIMEOUT_SECONDS,
    now: float | None = None,
    refresh_skew_seconds: int = TOKEN_REFRESH_SKEW_SECONDS,
) -> str:
    application_id = str(app_id or "").strip()
    secret = str(app_secret or "").strip()
    if not application_id or not secret:
        raise FeishuError("app_id 和 app_secret 不能为空")
    current = time.time() if now is None else float(now)
    with _state_lock:
        cache = _read_json(token_cache_path(root))
        tokens = cache.get("tokens") if isinstance(cache.get("tokens"), dict) else {}
        cached = tokens.get(application_id)
        if isinstance(cached, dict) and str(cached.get("token") or "") and float(cached.get("expires_at") or 0) > current + refresh_skew_seconds:
            return str(cached["token"])

        payload = _request_json(
            _api_url(base_url, "/open-apis/auth/v3/tenant_access_token/internal"),
            method="POST",
            body={"app_id": application_id, "app_secret": secret},
            opener=opener,
            timeout=timeout,
        )
        token = str(payload.get("tenant_access_token") or "").strip()
        if not token:
            raise FeishuError("飞书 token 响应缺少 tenant_access_token", response=payload)
        expires_in = payload.get("expire", payload.get("expires_in", 7200))
        try:
            expires_at = current + max(1, int(expires_in))
        except (TypeError, ValueError):
            expires_at = current + 7200
        tokens[application_id] = {"token": token, "expires_at": expires_at}
        _write_secret_json(token_cache_path(root), {"version": 1, "tokens": tokens})
        return token


def _get_api_json(
    root: Path,
    app_id: str,
    app_secret: str,
    endpoint: str,
    *,
    query: dict[str, str] | None = None,
    tenant_access_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    opener: UrlOpener | None = None,
    timeout: int = API_TIMEOUT_SECONDS,
) -> dict:
    access_token = str(tenant_access_token or "").strip() or get_tenant_access_token(
        root,
        app_id,
        app_secret,
        base_url=base_url,
        opener=opener,
        timeout=timeout,
    )
    url = _api_url(base_url, endpoint)
    if query:
        url = f"{url}?{urlencode(query)}"
    return _request_json(url, token=access_token, opener=opener, timeout=timeout)


def fetch_user_profile(
    root: Path,
    app_id: str,
    app_secret: str,
    open_id: str,
    *,
    tenant_access_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    opener: UrlOpener | None = None,
    timeout: int = API_TIMEOUT_SECONDS,
) -> dict:
    identity = str(open_id or "").strip()
    if not identity:
        raise FeishuError("open_id 不能为空")
    payload = _get_api_json(
        root,
        app_id,
        app_secret,
        f"/open-apis/contact/v3/users/{quote(identity, safe='')}",
        query={"user_id_type": "open_id"},
        tenant_access_token=tenant_access_token,
        base_url=base_url,
        opener=opener,
        timeout=timeout,
    )
    data = _object(payload.get("data"))
    user = _object(data.get("user") or data)
    display_name = _first_string(user.get("name"), user.get("nickname"), user.get("en_name"), user.get("display_name"))
    record = {
        "kind": "open_id",
        "id": identity,
        "open_id": _first_string(user.get("open_id"), identity),
        "user_id": _first_string(user.get("user_id")),
        "union_id": _first_string(user.get("union_id")),
        "id_hash": _identity_fingerprint(identity),
        "chat_type": "p2p",
        "updated_at": utc_now(),
    }
    if display_name:
        record["display_name"] = display_name
    return record


def fetch_chat_profile(
    root: Path,
    app_id: str,
    app_secret: str,
    chat_id: str,
    *,
    tenant_access_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    opener: UrlOpener | None = None,
    timeout: int = API_TIMEOUT_SECONDS,
) -> dict:
    identity = str(chat_id or "").strip()
    if not identity:
        raise FeishuError("chat_id 不能为空")
    payload = _get_api_json(
        root,
        app_id,
        app_secret,
        f"/open-apis/im/v1/chats/{quote(identity, safe='')}",
        query={"user_id_type": "open_id"},
        tenant_access_token=tenant_access_token,
        base_url=base_url,
        opener=opener,
        timeout=timeout,
    )
    data = _object(payload.get("data"))
    display_name = _first_string(data.get("name"), data.get("chat_name"))
    record = {
        "kind": "chat_id",
        "id": identity,
        "chat_id": _first_string(data.get("chat_id"), identity),
        "id_hash": _identity_fingerprint(identity),
        "chat_type": "group",
        "updated_at": utc_now(),
    }
    if display_name:
        record["display_name"] = display_name
    owner_id = _first_string(data.get("owner_id"), _nested(data, "owner_id", "open_id"))
    if owner_id:
        record["owner_open_id"] = owner_id
    return record


def refresh_identity_profiles(
    root: Path,
    app_id: str,
    app_secret: str,
    *,
    open_ids: list[str] | None = None,
    chat_ids: list[str] | None = None,
    tenant_access_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    opener: UrlOpener | None = None,
    timeout: int = IDENTITY_PROFILE_LOOKUP_TIMEOUT_SECONDS,
    max_items: int = IDENTITY_PROFILE_LOOKUP_MAX_ITEMS,
) -> dict:
    """Best-effort user/group display-name refresh for the authenticated settings UI."""
    application_id = str(app_id or "").strip()
    secret = str(app_secret or "").strip()
    if not application_id or not secret or max_items <= 0:
        return {"attempted": 0, "updated": 0, "errors": []}
    profiles = identity_profiles(root)
    candidates: list[tuple[str, str]] = []
    for identity in dict.fromkeys(str(item or "").strip() for item in (open_ids or []) if str(item or "").strip()):
        profile = profiles["open_ids"].get(identity, {})
        if _profile_needs_lookup(profile):
            candidates.append(("open_id", identity))
    for identity in dict.fromkeys(str(item or "").strip() for item in (chat_ids or []) if str(item or "").strip()):
        profile = profiles["chat_ids"].get(identity, {})
        if _profile_needs_lookup(profile):
            candidates.append(("chat_id", identity))
    candidates = candidates[:max_items]
    if not candidates:
        return {"attempted": 0, "updated": 0, "errors": []}
    errors: list[str] = []
    updated = 0
    token = str(tenant_access_token or "").strip()
    if not token:
        try:
            token = get_tenant_access_token(
                root,
                application_id,
                secret,
                base_url=base_url,
                opener=opener,
                timeout=timeout,
            )
        except FeishuError as exc:
            return {"attempted": 0, "updated": 0, "errors": [str(exc)]}
    for kind, identity in candidates:
        try:
            if kind == "open_id":
                record = fetch_user_profile(
                    root,
                    application_id,
                    secret,
                    identity,
                    tenant_access_token=token,
                    base_url=base_url,
                    opener=opener,
                    timeout=timeout,
                )
            else:
                record = fetch_chat_profile(
                    root,
                    application_id,
                    secret,
                    identity,
                    tenant_access_token=token,
                    base_url=base_url,
                    opener=opener,
                    timeout=timeout,
                )
            remember_identity_profile(
                root,
                kind=kind,
                identity=identity,
                display_name=str(record.get("display_name") or ""),
                chat_type=str(record.get("chat_type") or ""),
                seen_at=str(record.get("updated_at") or utc_now()),
            )
            if record.get("display_name"):
                updated += 1
        except FeishuError as exc:
            message = str(exc)
            errors.append(message)
            _mark_profile_lookup_failed(root, kind=kind, identity=identity, error=message)
    return {"attempted": len(candidates), "updated": updated, "errors": errors[:3]}


def _send_message(
    root: Path,
    app_id: str,
    app_secret: str,
    receive_id: str,
    *,
    message_type: str,
    content: dict,
    receive_id_type: str,
    tenant_access_token: str | None,
    base_url: str,
    opener: UrlOpener | None,
    timeout: int,
) -> dict:
    recipient = str(receive_id or "").strip()
    if not recipient:
        raise FeishuError("receive_id 不能为空")
    access_token = str(tenant_access_token or "").strip() or get_tenant_access_token(
        root,
        app_id,
        app_secret,
        base_url=base_url,
        opener=opener,
        timeout=timeout,
    )
    query = urlencode({"receive_id_type": receive_id_type})
    try:
        result = _request_json(
            f"{_api_url(base_url, '/open-apis/im/v1/messages')}?{query}",
            method="POST",
            token=access_token,
            body={
                "receive_id": recipient,
                "msg_type": message_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
            opener=opener,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - audit the transport failure, then preserve its original type.
        audit_feishu_channel(
            root,
            direction="outbound",
            kind="card" if message_type == "interactive" else "message",
            status="failed",
            transport="rest",
            chat_id=recipient if receive_id_type == "chat_id" else "",
            open_id=recipient if receive_id_type == "open_id" else "",
            content={"card": content} if message_type == "interactive" else content,
            error=exc,
        )
        raise
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    audit_feishu_channel(
        root,
        direction="outbound",
        kind="card" if message_type == "interactive" else "message",
        status="sent",
        transport="rest",
        message_id=str(data.get("message_id") or ""),
        chat_id=recipient if receive_id_type == "chat_id" else "",
        open_id=recipient if receive_id_type == "open_id" else "",
        content={"card": content} if message_type == "interactive" else content,
    )
    return result


def send_text_message(
    root: Path,
    app_id: str,
    app_secret: str,
    receive_id: str,
    text: str,
    *,
    receive_id_type: str = "chat_id",
    tenant_access_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    opener: UrlOpener | None = None,
    timeout: int = API_TIMEOUT_SECONDS,
) -> dict:
    return _send_message(
        root,
        app_id,
        app_secret,
        receive_id,
        message_type="text",
        content={"text": str(text)},
        receive_id_type=receive_id_type,
        tenant_access_token=tenant_access_token,
        base_url=base_url,
        opener=opener,
        timeout=timeout,
    )


def send_card_message(
    root: Path,
    app_id: str,
    app_secret: str,
    receive_id: str,
    card: dict,
    *,
    receive_id_type: str = "chat_id",
    tenant_access_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    opener: UrlOpener | None = None,
    timeout: int = API_TIMEOUT_SECONDS,
) -> dict:
    if not isinstance(card, dict):
        raise FeishuError("card 必须是 JSON 对象")
    card = sanitize_card_payload(card)
    return _send_message(
        root,
        app_id,
        app_secret,
        receive_id,
        message_type="interactive",
        content=card,
        receive_id_type=receive_id_type,
        tenant_access_token=tenant_access_token,
        base_url=base_url,
        opener=opener,
        timeout=timeout,
    )


def send_image_message(
    root: Path,
    app_id: str,
    app_secret: str,
    receive_id: str,
    image_key: str,
    *,
    receive_id_type: str = "chat_id",
    tenant_access_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    opener: UrlOpener | None = None,
    timeout: int = API_TIMEOUT_SECONDS,
) -> dict:
    key = str(image_key or "").strip()
    if not key:
        raise FeishuError("image_key 不能为空")
    return _send_message(
        root,
        app_id,
        app_secret,
        receive_id,
        message_type="image",
        content={"image_key": key},
        receive_id_type=receive_id_type,
        tenant_access_token=tenant_access_token,
        base_url=base_url,
        opener=opener,
        timeout=timeout,
    )


def send_file_message(
    root: Path,
    app_id: str,
    app_secret: str,
    receive_id: str,
    file_key: str,
    *,
    receive_id_type: str = "chat_id",
    tenant_access_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    opener: UrlOpener | None = None,
    timeout: int = API_TIMEOUT_SECONDS,
) -> dict:
    key = str(file_key or "").strip()
    if not key:
        raise FeishuError("file_key 不能为空")
    return _send_message(
        root,
        app_id,
        app_secret,
        receive_id,
        message_type="file",
        content={"file_key": key},
        receive_id_type=receive_id_type,
        tenant_access_token=tenant_access_token,
        base_url=base_url,
        opener=opener,
        timeout=timeout,
    )


def update_card_message(
    root: Path,
    app_id: str,
    app_secret: str,
    message_id: str,
    card: dict,
    *,
    tenant_access_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    opener: UrlOpener | None = None,
    timeout: int = API_TIMEOUT_SECONDS,
) -> dict:
    identity = str(message_id or "").strip()
    if not identity or not isinstance(card, dict):
        raise FeishuError("message_id 和 card 不能为空")
    access_token = str(tenant_access_token or "").strip() or get_tenant_access_token(
        root,
        app_id,
        app_secret,
        base_url=base_url,
        opener=opener,
        timeout=timeout,
    )
    try:
        result = _request_json(
            _api_url(base_url, f"/open-apis/im/v1/messages/{identity}"),
            method="PATCH",
            token=access_token,
            body={"content": json.dumps(card, ensure_ascii=False)},
            opener=opener,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - audit the transport failure, then preserve its original type.
        audit_feishu_channel(
            root,
            direction="outbound",
            kind="card_update",
            status="failed",
            transport="rest",
            message_id=identity,
            content={"card": card},
            error=exc,
        )
        raise
    audit_feishu_channel(
        root,
        direction="outbound",
        kind="card_update",
        status="updated",
        transport="rest",
        message_id=identity,
        content={"card": card},
    )
    return result


__all__ = [
    "ACTION_TOKEN_MAX_ENTRIES",
    "ACTION_TOKEN_TTL_SECONDS",
    "API_TIMEOUT_SECONDS",
    "DEFAULT_BASE_URL",
    "FeishuError",
    "action_tokens_path",
    "bind_confirmation_card",
    "claim_inbound_message",
    "confirmation_card_for_message",
    "confirmation_cards_path",
    "consume_confirmation_card",
    "consume_action_token",
    "consume_pending_action_token",
    "feishu_dir",
    "fetch_chat_profile",
    "fetch_user_profile",
    "finalize_confirmation_card",
    "get_session_binding",
    "get_tenant_access_token",
    "identity_label_items",
    "identity_profiles",
    "identity_profiles_path",
    "inbound_dedupe_path",
    "issue_action_token",
    "load_session_bindings",
    "mark_confirmation_card_updated",
    "make_session_key",
    "message_attachment_summary",
    "message_resource_attachments",
    "normalize_message_event",
    "pending_confirmation_card_updates",
    "recent_chats_path",
    "recent_groups",
    "recent_groups_path",
    "recent_private_chats",
    "refresh_identity_profiles",
    "record_recent_group",
    "record_recent_private_chat",
    "remember_identity_profile",
    "register_confirmation_card",
    "sanitize_card_payload",
    "sanitize_confirmation_cards",
    "send_card_message",
    "send_file_message",
    "send_image_message",
    "send_text_message",
    "session_bindings_path",
    "session_key_for_message",
    "set_session_binding",
    "should_handle_message",
    "token_cache_path",
    "terminal_confirmation_card",
    "update_card_message",
]
