from __future__ import annotations

import threading
from contextlib import contextmanager
import hashlib
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.locking import exclusive_lock
from aha_cli.services.feishu import FeishuError, make_session_key
from aha_cli.services.feishu_notifications import load_subscription_state, remove_subscriptions
from aha_cli.services.feishu_runtime import feishu_config, feishu_credentials
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import aha_home_path, config_path

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


def _fingerprint(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _configured_allowed_open_ids(integration: dict) -> list[str]:
    raw = integration.get("allowed_open_ids")
    if isinstance(raw, str):
        raw = [item.strip() for item in raw.replace("\n", ",").split(",")]
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(item or "").strip() for item in raw if str(item or "").strip()))


def _tenant_keys_for_open_id(root: Path, open_id: str) -> set[str]:
    owner = str(open_id or "").strip()
    if not owner:
        return set()
    tenants: set[str] = set()
    for candidate in _owner_candidates_for_open_id(root, owner):
        if str(candidate.get("chat_id") or "").strip():
            tenant = str(candidate.get("tenant_key") or "").strip()
            if tenant:
                tenants.add(tenant)
    return tenants


def _open_id_identity_tenants(root: Path, open_id: str) -> tuple[set[str], set[str]]:
    owner = str(open_id or "").strip()
    if not owner:
        return set(), set()
    current_state = _load(root)
    owner_tenants: set[str] = set()
    for tenant, record in current_state["owners"].items():
        if isinstance(record, dict) and str(record.get("open_id") or "").strip() == owner:
            owner_tenants.add(str(tenant or "").strip())
    for record in current_state["private_chats"].values():
        if isinstance(record, dict) and str(record.get("open_id") or "").strip() == owner:
            tenant = str(record.get("tenant_key") or "").strip()
            if tenant:
                owner_tenants.add(tenant)
    subscription_tenants: set[str] = set()
    try:
        subscription_state = load_subscription_state(root)
    except (OSError, ValueError):
        subscription_state = {}
    subscriptions = subscription_state.get("subscriptions") if isinstance(subscription_state.get("subscriptions"), dict) else {}
    for session_key, subscription in subscriptions.items():
        if not isinstance(subscription, dict) or not subscription.get("enabled"):
            continue
        if _subscription_chat_type(session_key, subscription) != "p2p":
            continue
        if str(subscription.get("open_id") or "").strip() != owner:
            continue
        tenant = _subscription_tenant_key(session_key)
        if tenant:
            subscription_tenants.add(tenant)
    return owner_tenants, subscription_tenants


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


def _owner_candidates_for_open_id(root: Path, open_id: str) -> list[dict]:
    owner = str(open_id or "").strip()
    if not owner:
        return []
    candidates: list[dict] = []
    state = _load(root)

    def add_candidate(*, tenant_key: object, chat_id: object, session_key: object = "", source: str) -> None:
        tenant = str(tenant_key or "").strip()
        if not tenant:
            return
        chat = str(chat_id or "").strip()
        session = str(session_key or "").strip() or _session_key(tenant, owner)
        key = (tenant, chat, session)
        for existing in candidates:
            if (existing.get("tenant_key"), existing.get("chat_id"), existing.get("session_key")) == key:
                return
        candidates.append(
            _owner_result(
                ok=bool(chat),
                reason="" if chat else "owner_chat_missing",
                source=source,
                tenant_key=tenant,
                open_id=owner,
                chat_id=chat,
                session_key=session,
            )
        )

    for tenant, record in state["owners"].items():
        if not isinstance(record, dict) or str(record.get("open_id") or "").strip() != owner:
            continue
        add_candidate(
            tenant_key=tenant,
            chat_id=record.get("chat_id"),
            session_key=record.get("session_key"),
            source=str(record.get("source") or "owner_state"),
        )

    for record in state["private_chats"].values():
        if not isinstance(record, dict) or str(record.get("open_id") or "").strip() != owner:
            continue
        add_candidate(
            tenant_key=record.get("tenant_key"),
            chat_id=record.get("chat_id"),
            session_key=record.get("session_key"),
            source="private_chat",
        )

    try:
        subscription_state = load_subscription_state(root)
    except (OSError, ValueError):
        subscription_state = {}
    subscriptions = subscription_state.get("subscriptions") if isinstance(subscription_state.get("subscriptions"), dict) else {}
    for session_key, subscription in reversed(list(subscriptions.items())):
        if not isinstance(subscription, dict) or not subscription.get("enabled"):
            continue
        if _subscription_chat_type(session_key, subscription) != "p2p":
            continue
        if str(subscription.get("open_id") or "").strip() != owner:
            continue
        add_candidate(
            tenant_key=_subscription_tenant_key(session_key),
            chat_id=subscription.get("chat_id"),
            session_key=session_key,
            source="p2p_subscription",
        )
    return candidates


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


def resolve_feishu_owner_by_open_id(root: Path, *, open_id: str, config: dict | None = None) -> dict:
    owner = str(open_id or "").strip()
    if not owner:
        return {"ok": False, "reason": "owner_open_id_missing"}
    integration = config if isinstance(config, dict) else feishu_config(root)
    configured_owner = str(integration.get("owner_open_id") or "").strip()
    if configured_owner and configured_owner != owner:
        return {
            "ok": False,
            "reason": "owner_mismatch",
            "open_id": owner,
            "expected_open_id": configured_owner,
        }
    configured_chat = str(integration.get("owner_chat_id") or "").strip()
    candidates = _owner_candidates_for_open_id(root, owner)
    if configured_chat:
        for candidate in candidates:
            if str(candidate.get("chat_id") or "") == configured_chat:
                return {**candidate, "source": f"{candidate.get('source')}_configured_chat"}
        for candidate in candidates:
            tenant = str(candidate.get("tenant_key") or "").strip()
            if tenant:
                return {
                    **candidate,
                    "ok": True,
                    "reason": "",
                    "chat_id": configured_chat,
                    "session_key": str(candidate.get("session_key") or _session_key(tenant, owner)),
                    "source": f"{candidate.get('source')}_configured_chat",
                }

    with_chat = [candidate for candidate in candidates if str(candidate.get("chat_id") or "").strip()]
    if with_chat:
        return with_chat[0]
    if candidates:
        return candidates[0]
    return {"ok": False, "reason": "owner_unresolved", "open_id": owner}


def cleanup_feishu_identity_state(root: Path, *, config: dict | None = None, dry_run: bool = False) -> dict:
    """Prune stale owner/user private-chat state after the Feishu app or owner changes.

    Group subscriptions are intentionally left untouched: group access is governed by
    allowed_chat_ids and the @-only digital-human route, not by the owner p2p inbox.
    """

    integration = config if isinstance(config, dict) else feishu_config(root)
    owner_open_id = str(integration.get("owner_open_id") or "").strip()
    if not owner_open_id:
        return {"ok": False, "reason": "owner_open_id_missing"}

    current_app_id, _app_secret = feishu_credentials(integration)
    current_app_id = str(current_app_id or "").strip()
    if current_app_id:
        owner_chat_id = _subscription_chat_for_owner(root, current_app_id, owner_open_id)
        if not owner_chat_id:
            owner_chat_id = str(_private_chat_state(root, current_app_id, owner_open_id).get("chat_id") or "").strip()
        owner_session_key = _session_key(current_app_id, owner_open_id)
        current_tenants = {current_app_id}
    else:
        owner = resolve_feishu_owner_by_open_id(root, open_id=owner_open_id, config=integration)
        owner_chat_id = str(owner.get("chat_id") or integration.get("owner_chat_id") or "").strip()
        owner_session_key = str(owner.get("session_key") or "").strip()
        current_tenants = _tenant_keys_for_open_id(root, owner_open_id)
        if str(owner.get("tenant_key") or "").strip():
            current_tenants.add(str(owner.get("tenant_key") or "").strip())

    allowed = _configured_allowed_open_ids(integration)
    kept_allowed: list[str] = []
    removed_allowed: list[str] = []
    for open_id in allowed:
        state_tenants, subscription_tenants = _open_id_identity_tenants(root, open_id)
        seen_tenants = state_tenants | subscription_tenants
        has_current_state = bool(current_tenants and seen_tenants & current_tenants)
        has_stale_state = bool(current_tenants and seen_tenants and not has_current_state)
        if open_id == owner_open_id or has_current_state or not has_stale_state:
            kept_allowed.append(open_id)
        else:
            removed_allowed.append(open_id)
    if owner_open_id not in kept_allowed:
        kept_allowed.insert(0, owner_open_id)
    kept_allowed = list(dict.fromkeys(kept_allowed))

    config_updated = False
    config_would_update = False
    path = config_path(root)
    try:
        raw_config = read_json(path)
    except (FileNotFoundError, OSError, ValueError):
        raw_config = {}
    if isinstance(raw_config, dict):
        integrations = raw_config.get("integrations")
        integrations = dict(integrations) if isinstance(integrations, dict) else {}
        feishu = integrations.get("feishu")
        feishu = dict(feishu) if isinstance(feishu, dict) else {}
        config_would_update = _configured_allowed_open_ids(feishu) != kept_allowed
        if config_would_update and not dry_run:
            feishu["allowed_open_ids"] = kept_allowed
            integrations["feishu"] = feishu
            raw_config["integrations"] = integrations
            write_json(path, raw_config)
            config_updated = True

    with _locked_owner_state(root):
        state = _load(root)
        original_owner_count = len(state["owners"])
        original_private_count = len(state["private_chats"])
        next_owners = {
            str(tenant): record
            for tenant, record in state["owners"].items()
            if isinstance(record, dict)
            and str(record.get("open_id") or "").strip() == owner_open_id
            and (not current_tenants or str(tenant or "").strip() in current_tenants)
        }
        next_private_chats = {
            str(key): record
            for key, record in state["private_chats"].items()
            if isinstance(record, dict)
            and str(record.get("open_id") or "").strip() == owner_open_id
            and (not current_tenants or str(record.get("tenant_key") or "").strip() in current_tenants)
        }
        removed_owner_records = original_owner_count - len(next_owners)
        removed_private_chats = original_private_count - len(next_private_chats)
        if not dry_run:
            state["owners"] = next_owners
            state["private_chats"] = next_private_chats
        if not dry_run and (removed_owner_records or removed_private_chats):
            _save(root, state)

    remove_subscription_keys: set[str] = set()
    try:
        subscription_state = load_subscription_state(root)
    except (OSError, ValueError):
        subscription_state = {}
    subscriptions = subscription_state.get("subscriptions") if isinstance(subscription_state.get("subscriptions"), dict) else {}
    for session_key, subscription in subscriptions.items():
        if not isinstance(subscription, dict):
            continue
        if _subscription_chat_type(session_key, subscription) != "p2p":
            continue
        open_id = str(subscription.get("open_id") or "").strip()
        tenant = _subscription_tenant_key(session_key)
        chat_id = str(subscription.get("chat_id") or "").strip()
        if open_id in removed_allowed:
            remove_subscription_keys.add(str(session_key))
            continue
        if open_id == owner_open_id:
            if current_tenants and tenant and tenant not in current_tenants:
                remove_subscription_keys.add(str(session_key))
                continue
            if owner_chat_id and chat_id and chat_id != owner_chat_id:
                remove_subscription_keys.add(str(session_key))
                continue
            if owner_session_key and str(session_key) != owner_session_key and owner_chat_id and chat_id == owner_chat_id:
                remove_subscription_keys.add(str(session_key))
                continue
        elif current_tenants and tenant and tenant not in current_tenants and open_id not in kept_allowed:
            remove_subscription_keys.add(str(session_key))

    if dry_run:
        removed_subscriptions = len(remove_subscription_keys)
    else:
        removed_subscriptions = remove_subscriptions(root, remove_subscription_keys).get("removed_count", 0)
    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "current_app_id": _fingerprint(current_app_id),
        "owner_open_id": _fingerprint(owner_open_id),
        "owner_chat_id": _fingerprint(owner_chat_id),
        "current_tenant_count": len(current_tenants),
        "would_update_config": config_would_update,
        "config_updated": config_updated,
        "allowed_open_id_count": len(kept_allowed),
        "removed_allowed_open_id_count": len(removed_allowed),
        "removed_owner_record_count": removed_owner_records,
        "removed_private_chat_count": removed_private_chats,
        "removed_subscription_count": removed_subscriptions,
    }


__all__ = [
    "cleanup_feishu_identity_state",
    "feishu_owner_state_path",
    "remember_owner_private_chat",
    "resolve_feishu_owner",
    "resolve_feishu_owner_by_open_id",
]
