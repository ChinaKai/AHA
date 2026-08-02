from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from aha_cli.store.paths import aha_home_path


DEFAULT_BASE_URL = "https://open.feishu.cn"
API_TIMEOUT_SECONDS = 15
TOKEN_REFRESH_SKEW_SECONDS = 60
INBOUND_DEDUPE_TTL_SECONDS = 24 * 60 * 60
INBOUND_DEDUPE_MAX_ENTRIES = 4096
ACTION_TOKEN_TTL_SECONDS = 5 * 60
ACTION_TOKEN_MAX_ENTRIES = 1024

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


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


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
    mentions = [item for item in _list(message.get("mentions")) if isinstance(item, dict)]
    mentioned_open_ids = [value for value in (_mention_open_id(item) for item in mentions) if value]
    expected_bot = str(bot_open_id or "").strip()
    is_at_bot = expected_bot in mentioned_open_ids if expected_bot else bool(mentions)

    message_type = str(message.get("message_type") or "").strip()
    content = _content_object(message.get("content"))
    text = _message_text(message_type, content)
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
        "chat_id": str(message.get("chat_id") or "").strip(),
        "chat_type": str(message.get("chat_type") or "").strip(),
        "message_id": str(message.get("message_id") or "").strip(),
        "root_id": root_id,
        "thread_id": thread_id,
        "parent_id": str(message.get("parent_id") or "").strip(),
        "message_type": message_type,
        "text": text,
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
    with _state_lock:
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
    with _state_lock:
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
    with _state_lock:
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
    return _request_json(
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


__all__ = [
    "ACTION_TOKEN_MAX_ENTRIES",
    "ACTION_TOKEN_TTL_SECONDS",
    "API_TIMEOUT_SECONDS",
    "DEFAULT_BASE_URL",
    "FeishuError",
    "action_tokens_path",
    "claim_inbound_message",
    "consume_action_token",
    "consume_pending_action_token",
    "feishu_dir",
    "get_session_binding",
    "get_tenant_access_token",
    "inbound_dedupe_path",
    "issue_action_token",
    "load_session_bindings",
    "make_session_key",
    "normalize_message_event",
    "send_card_message",
    "send_text_message",
    "session_bindings_path",
    "session_key_for_message",
    "set_session_binding",
    "should_handle_message",
    "token_cache_path",
]
