from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import is_feishu_group_task
from aha_cli.services.feishu import bind_confirmation_card, get_session_binding, set_session_binding
from aha_cli.services.feishu_group import FEISHU_GROUP_HANDOFF_ACK
from aha_cli.services.feishu_group_handoffs import mark_group_handoff, register_group_handoff
from aha_cli.services.feishu_notifications import load_subscription_state, set_subscription
from aha_cli.services.feishu_owner import resolve_feishu_owner
from aha_cli.services.feishu_runtime import feishu_config
from aha_cli.services.feishu_work_run import feishu_work_run_status
from aha_cli.store.config import load_config
from aha_cli.store.io import iter_jsonl_reverse
from aha_cli.store.paths import event_path
from aha_cli.store.runs import run_exists

FEISHU_GROUP_HANDOFF_ACTION = "feishu_group_handoff"


def _bool_argument(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "是", "新建"}
    return False


def _assistant_agent_defaults(root: Path) -> dict[str, object]:
    global_config = load_config(root)
    integration = feishu_config(root)
    backend = str(integration.get("backend") or global_config.get("backend") or "codex")
    backend_config = global_config.get(backend) if isinstance(global_config.get(backend), dict) else {}
    model = str(integration.get("model") or backend_config.get("model") or "").strip() or None
    reasoning_effort = str(integration.get("reasoning_effort") or backend_config.get("reasoning_effort") or "").strip() or None
    backend_proxy = backend_config.get("proxy") if isinstance(backend_config.get("proxy"), dict) else {}
    configured_proxy_enabled = integration.get("proxy_enabled")
    proxy_enabled = (
        bool(configured_proxy_enabled)
        if isinstance(configured_proxy_enabled, bool)
        else bool(backend_proxy.get("enabled"))
    )
    return {
        "backend": backend,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "proxy_enabled": proxy_enabled,
    }


def _latest_group_request(root: Path, run_id: str, task_id: str) -> dict:
    for _offset, event in iter_jsonl_reverse(event_path(root, run_id)):
        if str(event.get("type") or "") != "message":
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if str(data.get("task_id") or "") != str(task_id or ""):
            continue
        senders = {str(data.get(key) or "").strip().lower() for key in ("sender", "from_agent", "display_sender")}
        targets = {str(data.get(key) or "").strip().lower() for key in ("target", "to_agent", "display_target")}
        if "feishu" not in senders or "main" not in targets:
            continue
        if str(data.get("feishu_channel") or "") != "group_digital_human":
            continue
        return dict(data)
    return {}


def _steward_forward_message(root: Path, origin: dict, action: dict, *, handoff: dict | None = None) -> str:
    arguments = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
    reason = str(arguments.get("reason") or action.get("reason") or "").strip()
    summary = str(arguments.get("summary") or arguments.get("question") or "").strip()
    detail = str(arguments.get("details") or arguments.get("detail") or arguments.get("request_detail") or "").strip()
    original = str(origin.get("feishu_original_text") or origin.get("message") or "").strip()
    requester_open_id = str(origin.get("feishu_mention_open_id") or "").strip()
    attachments = origin.get("feishu_attachments") if isinstance(origin.get("feishu_attachments"), list) else []
    merged = bool((handoff or {}).get("merged_existing"))
    parts = [
        "AHA 系统生成的飞书群聊转单信封" + ("（已合并到现有待处理单）" if merged else ""),
        "",
        "说明：本消息只包含群聊转单事实，不是数字人对管家的处理指令。后续处理 SOP 由私聊管家的系统提示词决定。",
        "",
        "原群聊问题：",
        original,
    ]
    if requester_open_id:
        parts.extend(["", "群聊提问者 open_id：", requester_open_id])
    if attachments:
        parts.extend(["", "飞书附件（资源摘要，尚未下载或分析内容）："])
        for index, item in enumerate(attachments[:8], start=1):
            if not isinstance(item, dict):
                continue
            name = str(item.get("file_name") or item.get("name") or "").strip()
            resource_type = str(item.get("type") or "attachment")
            key = str(item.get("image_key") or item.get("file_key") or item.get("media_key") or "").strip()
            label = f"{index}. {resource_type}"
            if name:
                label += f" {name}"
            if key:
                label += f" key={key[:6]}...{key[-4:]}" if len(key) > 12 else f" key={key}"
            parts.append(label)
    if summary:
        parts.extend(["", "数字人摘要：", summary])
    if detail:
        parts.extend(["", "需求详情：", detail])
    if reason:
        parts.extend(["", "转发原因：", reason])
    if merged and isinstance(handoff, dict):
        parts.extend(["", "当前合并后的需求上下文：", str(handoff.get("request_preview") or "")])
    if isinstance(handoff, dict) and handoff.get("id"):
        parts.extend(
            [
                "",
                "转单字段：",
                f"handoff_id={handoff.get('id')}",
                f"thread_id={handoff.get('thread_id') or handoff.get('id')}",
                f"status={handoff.get('status') or 'pending'}",
            ]
        )
    work_run = feishu_work_run_status(root)
    default_run = work_run.get("default_run") if isinstance(work_run.get("default_run"), dict) else {}
    if work_run:
        if work_run.get("default_run_available"):
            parts.extend(
                [
                    "",
                    "默认工作 Run：",
                    f"{work_run.get('default_run_id')} · {default_run.get('goal') or '-'}",
                ]
            )
        else:
            parts.extend(
                [
                    "",
                    "默认工作 Run：",
                    "未绑定。计划类待办需要主人先在飞书助手设置里选择默认归属 Run，或本次手动指定 run_id。",
                ]
            )
    return "\n".join(parts).strip()


def _send_owner_handoff_card(
    root: Path,
    *,
    owner_chat_id: str,
    owner_open_id: str,
    steward_session_key: str,
    steward_run_id: str,
    steward_task_id: str,
    handoff: dict,
) -> dict:
    try:
        from aha_cli.services.service_assistant_actions import prepare_group_handoff_owner_card

        action = prepare_group_handoff_owner_card(
            root,
            steward_run_id,
            steward_task_id,
            actor={
                "open_id": owner_open_id,
                "session_key": steward_session_key,
                "chat_id": owner_chat_id,
            },
            handoff=handoff,
        )
        card = action.get("confirmation_card") if isinstance(action.get("confirmation_card"), dict) else None
        if not card:
            return {"ok": False, "sent": False, "reason": "card_unavailable"}
        from aha_cli.services.feishu_notifications import send_direct_message

        result = send_direct_message(root, owner_chat_id, "", card=card)
        message_id = str(result.get("message_id") or "")
        confirmation_id = str(action.get("confirmation_id") or "")
        if message_id and confirmation_id:
            bind_confirmation_card(root, confirmation_id, message_id=message_id, chat_id=owner_chat_id)
        return {"ok": True, "sent": True, "message_id": message_id, "confirmation_id": confirmation_id}
    except Exception as exc:  # noqa: BLE001 - owner card is best-effort; the handoff message still reaches steward.
        return {"ok": False, "sent": False, "error": str(exc)[:500]}


def prepare_feishu_group_handoff_action(
    root: Path,
    run_id: str,
    task: dict,
    action: dict,
    *,
    origin_message: dict | None = None,
) -> dict:
    if not is_feishu_group_task(task):
        return {
            "type": FEISHU_GROUP_HANDOFF_ACTION,
            "ok": False,
            "user_response": "当前 Task 不是飞书群聊数字人，不能执行数字人转管家操作。",
        }
    task_id = str(task.get("id") or "")
    candidate = dict(origin_message or {})
    origin = (
        candidate
        if str(candidate.get("task_id") or "") == task_id
        and str(candidate.get("feishu_channel") or "") == "group_digital_human"
        else _latest_group_request(root, run_id, task_id)
    )
    if not origin:
        return {
            "type": FEISHU_GROUP_HANDOFF_ACTION,
            "ok": False,
            "user_response": "无法定位原始飞书群聊消息，本次转发未执行。",
        }
    tenant_key = str(origin.get("feishu_tenant_key") or "").strip()
    requester_open_id = str(origin.get("feishu_mention_open_id") or "").strip()
    group_chat_id = str(origin.get("feishu_chat_id") or "").strip()
    group_message_id = str(origin.get("feishu_message_id") or origin.get("feishu_reply_to") or "").strip()
    digital_session_key = str(origin.get("feishu_session_key") or "").strip()
    arguments = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
    merge_handoff_id = str(arguments.get("merge_handoff_id") or "").strip()
    force_new_handoff = _bool_argument(arguments.get("new_handoff")) or _bool_argument(
        arguments.get("force_new_handoff")
    )
    if not tenant_key or not requester_open_id or not group_chat_id:
        return {
            "type": FEISHU_GROUP_HANDOFF_ACTION,
            "ok": False,
            "user_response": "飞书群聊身份信息不完整，本次转发未执行。",
        }
    defaults = _assistant_agent_defaults(root)
    from aha_cli.services.service_assistant import ensure_service_assistant_run, ensure_service_assistant_task

    steward_run_id = ensure_service_assistant_run(root, defaults)
    if not steward_run_id or not run_exists(root, steward_run_id):
        return {
            "type": FEISHU_GROUP_HANDOFF_ACTION,
            "ok": False,
            "user_response": "私聊管家暂不可用，本次转发未执行。",
        }
    owner = resolve_feishu_owner(root, tenant_key=tenant_key, config=feishu_config(root))
    owner_open_id = str(owner.get("open_id") or "").strip()
    owner_chat_id = str(owner.get("chat_id") or "").strip()
    steward_session_key = str(owner.get("session_key") or "").strip()
    if not owner_open_id:
        return {
            "type": FEISHU_GROUP_HANDOFF_ACTION,
            "ok": False,
            "user_response": "问题已记录，但还没有绑定主人身份。请主人先私聊飞书管家完成绑定。",
        }
    if not owner_chat_id or not steward_session_key:
        return {
            "type": FEISHU_GROUP_HANDOFF_ACTION,
            "ok": False,
            "user_response": "问题已记录，但还没有主人私聊会话。请主人先私聊飞书管家后再重试。",
        }
    binding = get_session_binding(root, steward_session_key)
    if binding is None or str(binding.get("active_run_id") or "") != steward_run_id:
        set_session_binding(
            root,
            steward_session_key,
            active_run_id=steward_run_id,
            active_task_id=None,
            acl_subject=owner_open_id,
        )
    steward_task = ensure_service_assistant_task(root, steward_run_id, steward_session_key, defaults)
    set_session_binding(
        root,
        steward_session_key,
        active_run_id=steward_run_id,
        active_task_id=str(steward_task.get("id") or ""),
        acl_subject=owner_open_id,
    )
    current_subscription = load_subscription_state(root).get("subscriptions", {}).get(steward_session_key)
    active_task_chat = (
        isinstance(current_subscription, dict)
        and current_subscription.get("enabled")
        and str(current_subscription.get("mode") or "") == "task_chat"
    )
    if not active_task_chat:
        set_subscription(
            root,
            steward_session_key,
            chat_id=owner_chat_id,
            open_id=owner_open_id,
            run_id=steward_run_id,
            task_id=str(steward_task.get("id") or ""),
            chat_type="p2p",
        )
    request_message = str(origin.get("feishu_original_text") or origin.get("message") or "")
    attachments = origin.get("feishu_attachments") if isinstance(origin.get("feishu_attachments"), list) else []
    if attachments:
        request_message = "\n".join(
            [
                request_message.strip(),
                "飞书附件：",
                *[
                    f"- {str(item.get('type') or 'attachment')} "
                    f"{str(item.get('file_name') or item.get('name') or '').strip()}".strip()
                    for item in attachments
                    if isinstance(item, dict)
                ],
            ]
        ).strip()
    handoff = register_group_handoff(
        root,
        digital_run_id=run_id,
        digital_task_id=task_id,
        digital_session_key=digital_session_key,
        group_chat_id=group_chat_id,
        group_message_id=group_message_id,
        open_id=requester_open_id,
        owner_open_id=owner_open_id,
        owner_chat_id=owner_chat_id,
        steward_run_id=steward_run_id,
        steward_task_id=str(steward_task.get("id") or ""),
        request_message=request_message,
        request_summary=str(arguments.get("summary") or arguments.get("question") or "").strip(),
        request_detail=str(arguments.get("details") or arguments.get("detail") or arguments.get("request_detail") or "").strip(),
        handoff_reason=str(arguments.get("reason") or action.get("reason") or "").strip(),
        merge_handoff_id=merge_handoff_id,
        force_new=force_new_handoff,
    )
    try:
        from aha_cli.services.service_assistant_actions import (
            find_existing_group_handoff_memo,
            group_handoff_existing_memo_reply,
            link_group_handoff_to_existing_memo,
        )

        existing_memo = find_existing_group_handoff_memo(root, handoff)
        if existing_memo is not None:
            linked = link_group_handoff_to_existing_memo(root, handoff, existing_memo)
            memo = linked.get("memo") if isinstance(linked.get("memo"), dict) else {}
            return {
                "type": FEISHU_GROUP_HANDOFF_ACTION,
                "ok": True,
                "handoff_id": str((linked.get("handoff") or handoff).get("id") or ""),
                "merged_existing": bool(handoff.get("merged_existing")),
                "merge_source": str(handoff.get("merge_source") or ""),
                "linked_existing_memo": True,
                "memo_id": str(memo.get("id") or ""),
                "memo_run_id": str(linked.get("run_id") or ""),
                "steward_run_id": steward_run_id,
                "steward_task_id": str(steward_task.get("id") or ""),
                "owner_open_id": owner_open_id,
                "owner_card_result": {"ok": True, "sent": False, "reason": "existing_memo"},
                "forward_result": {"ok": True, "skipped": True, "reason": "existing_memo"},
                "user_response": group_handoff_existing_memo_reply(memo),
            }
    except Exception:
        pass
    owner_card_result = _send_owner_handoff_card(
        root,
        owner_chat_id=owner_chat_id,
        owner_open_id=owner_open_id,
        steward_session_key=steward_session_key,
        steward_run_id=steward_run_id,
        steward_task_id=str(steward_task.get("id") or ""),
        handoff=handoff,
    )
    try:
        from aha_cli.web.task_messaging import handle_send_payload

        result = handle_send_payload(
            root,
            steward_run_id,
            {
                "task_id": str(steward_task.get("id") or ""),
                "target": "main",
                "sender": "feishu-group",
                "reply_target": "feishu",
                "message": _steward_forward_message(root, origin, action, handoff=handoff),
                "feishu_group_handoff_id": str(handoff.get("id") or ""),
                "feishu_chat_id": owner_chat_id,
                "feishu_mention_open_id": owner_open_id,
                "feishu_channel": "private_steward",
                "feishu_tenant_key": tenant_key,
                "feishu_chat_type": "p2p",
                "feishu_session_key": steward_session_key,
            },
            background_backend_start=not bool(owner_card_result.get("sent")),
            suppress_backend_start=bool(owner_card_result.get("sent")),
        )
    except Exception as exc:  # noqa: BLE001 - preserve user-facing failure without breaking the agent turn.
        mark_group_handoff(
            root,
            str(handoff.get("id") or ""),
            "pending" if handoff.get("merged_existing") else "failed",
            error=str(exc),
        )
        return {
            "type": FEISHU_GROUP_HANDOFF_ACTION,
            "ok": False,
            "handoff_id": str(handoff.get("id") or ""),
            "user_response": "问题已记录，但转发给主人失败，请稍后重试。",
        }
    return {
        "type": FEISHU_GROUP_HANDOFF_ACTION,
        "ok": True,
        "handoff_id": str(handoff.get("id") or ""),
        "merged_existing": bool(handoff.get("merged_existing")),
        "merge_source": str(handoff.get("merge_source") or ""),
        "steward_run_id": steward_run_id,
        "steward_task_id": str(steward_task.get("id") or ""),
        "owner_open_id": owner_open_id,
        "owner_card_result": owner_card_result,
        "forward_result": result,
        "user_response": FEISHU_GROUP_HANDOFF_ACK,
    }


__all__ = [
    "FEISHU_GROUP_HANDOFF_ACTION",
    "prepare_feishu_group_handoff_action",
]
