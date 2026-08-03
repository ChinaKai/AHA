from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.locking import exclusive_lock
from aha_cli.services.feishu import FeishuError, make_session_key
from aha_cli.services.feishu_notifications import load_subscription_state
from aha_cli.services.feishu_runtime import feishu_config
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import aha_home_path

_state_lock = threading.RLock()


def feishu_owner_state_path(root: Path) -> Path:
    return aha_home_path(root) / "feishu" / "owner.json"


def feishu_owner_state_lock_path(root: Path) -> Path:
    return aha_home_path(root) / "feishu" / ".owner.lock"


@contextmanager
def _locked_owner_state(root: Path):
    lock_path = feishu_owner_state_lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_path.parent.chmod(0o700)
    except OSError:
        pass
    with _state_lock, lock_path.open("a+b") as handle, exclusive_lock(handle):
        yield


def _load(root: Path) -> dict:
    try:
        state = read_json(feishu_owner_state_path(root))
    except (FileNotFoundError, OSError, ValueError):
        state = {}
    owners = state.get("owners") if isinstance(state.get("owners"), dict) else {}
    private_chats = state.get("private_chats") if isinstance(state.get("private_chats"), dict) else {}
    return {
        "version": 1,
        "owners": {str(key): value for key, value in owners.items() if isinstance(value, dict)},
        "private_chats": {str(key): value for key, value in private_chats.items() if isinstance(value, dict)},
        "updated_at": str(state.get("updated_at") or ""),
    }


def _save(root: Path, state: dict) -> None:
    path = feishu_owner_state_path(root)
    state["updated_at"] = utc_now()
    write_json(path, state)
    try:
        path.chmod(0o600)
        path.parent.chmod(0o700)
    except OSError:
        pass


def _private_chat_key(tenant_key: str, open_id: str) -> str:
    return f"{tenant_key}:{open_id}"


def remember_owner_private_chat(
    root: Path,
    *,
    tenant_key: str,
    open_id: str,
    chat_id: str,
    session_key: str,
) -> dict:
    tenant = str(tenant_key or "").strip()
    owner = str(open_id or "").strip()
    chat = str(chat_id or "").strip()
    session = str(session_key or "").strip()
    if not tenant or not owner or not chat or not session:
        raise FeishuError("记录飞书主人私聊需要 tenant_key、open_id、chat_id 和 session_key")
    record = {
        "tenant_key": tenant,
        "open_id": owner,
        "chat_id": chat,
        "session_key": session,
        "source": "private_chat",
        "updated_at": utc_now(),
    }
    with _locked_owner_state(root):
        state = _load(root)
        state["private_chats"][_private_chat_key(tenant, owner)] = dict(record)
        current = state["owners"].get(tenant)
        if not isinstance(current, dict) or not str(current.get("open_id") or "").strip():
            state["owners"][tenant] = {**record, "source": "first_private_chat", "bound_at": utc_now()}
        elif str(current.get("open_id") or "") == owner:
            state["owners"][tenant] = {**current, **record}
        _save(root, state)
    return dict(record)


def _subscription_chat(root: Path, session_key: str) -> str:
    try:
        state = load_subscription_state(root)
    except (OSError, ValueError):
        return ""
    subscription = state.get("subscriptions", {}).get(str(session_key or ""))
    if isinstance(subscription, dict) and subscription.get("enabled"):
        return str(subscription.get("chat_id") or "").strip()
    return ""


def _subscription_tenant_key(session_key: object) -> str:
    return str(session_key or "").split(":", 1)[0].strip()


def _subscription_chat_type(session_key: object, subscription: dict) -> str:
    value = str(subscription.get("chat_type") or "").strip().lower()
    if value:
        return value
    return "group" if ":group:" in str(session_key or "").lower() else "p2p"


def _subscription_chat_for_owner(root: Path, tenant_key: str, open_id: str) -> str:
    tenant = str(tenant_key or "").strip()
    owner = str(open_id or "").strip()
    if not tenant or not owner:
        return ""
    try:
        state = load_subscription_state(root)
    except (OSError, ValueError):
        return ""
    subscriptions = state.get("subscriptions") if isinstance(state.get("subscriptions"), dict) else {}
    for session_key, subscription in reversed(list(subscriptions.items())):
        if not isinstance(subscription, dict) or not subscription.get("enabled"):
            continue
        if _subscription_tenant_key(session_key) != tenant:
            continue
        if _subscription_chat_type(session_key, subscription) != "p2p":
            continue
        if str(subscription.get("open_id") or "").strip() != owner:
            continue
        chat_id = str(subscription.get("chat_id") or "").strip()
        if chat_id:
            return chat_id
    return ""


def _private_chat_state(root: Path, tenant_key: str, open_id: str) -> dict:
    state = _load(root)
    record = state["private_chats"].get(_private_chat_key(tenant_key, open_id))
    return dict(record) if isinstance(record, dict) else {}


def _owner_from_state(root: Path, tenant_key: str) -> dict:
    state = _load(root)
    record = state["owners"].get(str(tenant_key or ""))
    return dict(record) if isinstance(record, dict) else {}


def _session_key(tenant_key: str, open_id: str) -> str:
    return make_session_key(tenant_key=tenant_key, open_id=open_id, chat_id="", chat_type="p2p")


def _resolve_owner_chat(root: Path, tenant_key: str, open_id: str, configured_chat_id: str = "") -> tuple[str, str]:
    session_key = _session_key(tenant_key, open_id)
    chat_id = str(configured_chat_id or "").strip()
    if not chat_id:
        chat_id = _subscription_chat(root, session_key)
    if not chat_id:
        chat_id = _subscription_chat_for_owner(root, tenant_key, open_id)
    if not chat_id:
        chat_id = str(_private_chat_state(root, tenant_key, open_id).get("chat_id") or "").strip()
    return chat_id, session_key


def _owner_result(
    *,
    ok: bool,
    reason: str,
    source: str,
    tenant_key: str,
    open_id: str,
    chat_id: str,
    session_key: str,
) -> dict:
    return {
        "ok": ok,
        "reason": reason,
        "source": source,
        "tenant_key": tenant_key,
        "open_id": open_id,
        "chat_id": chat_id,
        "session_key": session_key,
    }


def resolve_feishu_owner(root: Path, *, tenant_key: str, config: dict | None = None) -> dict:
    tenant = str(tenant_key or "").strip()
    if not tenant:
        return {"ok": False, "reason": "tenant_missing"}
    integration = config if isinstance(config, dict) else feishu_config(root)
    configured_owner = str(integration.get("owner_open_id") or "").strip()
    configured_chat = str(integration.get("owner_chat_id") or "").strip()
    configured_missing: dict | None = None
    if configured_owner:
        chat_id, session_key = _resolve_owner_chat(root, tenant, configured_owner, configured_chat)
        result = _owner_result(
            ok=bool(chat_id),
            reason="" if chat_id else "owner_chat_missing",
            source="config",
            tenant_key=tenant,
            open_id=configured_owner,
            chat_id=chat_id,
            session_key=session_key,
        )
        if chat_id:
            return result
        configured_missing = result

    state_owner = _owner_from_state(root, tenant)
    state_open_id = str(state_owner.get("open_id") or "").strip()
    if state_open_id:
        chat_id, session_key = _resolve_owner_chat(root, tenant, state_open_id, str(state_owner.get("chat_id") or ""))
        source = str(state_owner.get("source") or "owner_state")
        if configured_missing is not None and chat_id:
            source = f"{source}_fallback"
        result = _owner_result(
            ok=bool(chat_id),
            reason="" if chat_id else "owner_chat_missing",
            source=source,
            tenant_key=tenant,
            open_id=state_open_id,
            chat_id=chat_id,
            session_key=session_key,
        )
        if chat_id or configured_missing is None:
            return result

    allowed = [
        str(item or "").strip()
        for item in (integration.get("allowed_open_ids") if isinstance(integration.get("allowed_open_ids"), list) else [])
        if str(item or "").strip()
    ]
    if len(allowed) == 1:
        chat_id, session_key = _resolve_owner_chat(root, tenant, allowed[0], configured_chat)
        source = "single_allowed_open_id_fallback" if configured_missing is not None and chat_id else "single_allowed_open_id"
        result = _owner_result(
            ok=bool(chat_id),
            reason="" if chat_id else "owner_chat_missing",
            source=source,
            tenant_key=tenant,
            open_id=allowed[0],
            chat_id=chat_id,
            session_key=session_key,
        )
        if chat_id or configured_missing is None:
            return result
    if configured_missing is not None:
        return configured_missing
    return {"ok": False, "reason": "owner_unresolved", "tenant_key": tenant}


__all__ = [
    "feishu_owner_state_path",
    "remember_owner_private_chat",
    "resolve_feishu_owner",
]
