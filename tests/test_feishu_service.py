from __future__ import annotations

import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from urllib.error import HTTPError

from aha_cli.services import feishu


class FakeResponse:
    def __init__(self, payload: dict | bytes, *, status: int = 200) -> None:
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class QueueOpener:
    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[object, int]] = []

    def __call__(self, request: object, *, timeout: int) -> FakeResponse:
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def message_event(
    *,
    chat_type: str = "p2p",
    message_id: str = "om_1",
    text: str = "hello",
    mentions: list[dict] | None = None,
    root_id: str = "",
) -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_type": "im.message.receive_v1",
            "tenant_key": "tenant-header",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_user"},
                "sender_type": "user",
                "tenant_key": "tenant-sender",
            },
            "message": {
                "message_id": message_id,
                "root_id": root_id,
                "parent_id": "om_parent" if root_id else "",
                "chat_id": "oc_chat",
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": text}),
                "mentions": mentions or [],
            },
        },
    }


class FeishuServiceTests(unittest.TestCase):
    def test_recent_groups_are_deduplicated_sorted_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feishu.record_recent_group(root, "oc_a", seen_at="2026-08-01T00:00:00Z")
            feishu.record_recent_group(root, "oc_b", seen_at="2026-08-02T00:00:00Z")
            feishu.record_recent_group(root, "oc_a", seen_at="2026-08-03T00:00:00Z")
            groups = feishu.recent_groups(root)
            mode = stat.S_IMODE(feishu.recent_groups_path(root).stat().st_mode)

        self.assertEqual([item["chat_id"] for item in groups], ["oc_a", "oc_b"])
        self.assertEqual(mode, 0o600)

    def test_normalize_message_event_extracts_identity_thread_and_text(self) -> None:
        normalized = feishu.normalize_message_event(message_event(root_id="om_root"))

        self.assertEqual(normalized["tenant_key"], "tenant-sender")
        self.assertEqual(normalized["open_id"], "ou_user")
        self.assertEqual(normalized["chat_id"], "oc_chat")
        self.assertEqual(normalized["chat_type"], "p2p")
        self.assertEqual(normalized["message_id"], "om_1")
        self.assertEqual(normalized["root_id"], "om_root")
        self.assertEqual(normalized["thread_id"], "om_root")
        self.assertEqual(normalized["parent_id"], "om_parent")
        self.assertEqual(normalized["text"], "hello")
        self.assertFalse(normalized["is_at_bot"])
        self.assertTrue(feishu.should_handle_message(normalized))

    def test_group_message_requires_exact_bot_mention_when_bot_id_is_known(self) -> None:
        mentions = [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "AHA"}]
        normalized = feishu.normalize_message_event(
            message_event(chat_type="group", text="@_user_1 create task", mentions=mentions),
            bot_open_id="ou_bot",
        )

        self.assertTrue(normalized["is_at_bot"])
        self.assertEqual(normalized["text"], "create task")
        self.assertTrue(feishu.should_handle_message(normalized))

        other = feishu.normalize_message_event(
            message_event(chat_type="group", mentions=mentions),
            bot_open_id="ou_other_bot",
        )
        self.assertFalse(other["is_at_bot"])
        self.assertFalse(feishu.should_handle_message(other))

    def test_normalize_post_message_collects_human_text(self) -> None:
        payload = message_event()
        payload["event"]["message"]["message_type"] = "post"
        payload["event"]["message"]["content"] = json.dumps(
            {
                "zh_cn": {
                    "title": "Task",
                    "content": [[{"tag": "text", "text": "line one"}], [{"tag": "text", "text": "line two"}]],
                }
            }
        )

        normalized = feishu.normalize_message_event(payload)

        self.assertEqual(normalized["text"], "Task\nline one\nline two")

    def test_rejects_unrelated_event_type(self) -> None:
        payload = message_event()
        payload["header"]["event_type"] = "contact.user.created_v3"

        with self.assertRaisesRegex(feishu.FeishuError, "不支持"):
            feishu.normalize_message_event(payload)

    def test_session_keys_isolate_people_groups_and_tenants(self) -> None:
        p2p_a = feishu.make_session_key(tenant_key="t1", open_id="u1", chat_id="c1", chat_type="p2p")
        p2p_b = feishu.make_session_key(tenant_key="t1", open_id="u2", chat_id="c1", chat_type="p2p")
        group_a = feishu.make_session_key(tenant_key="t1", open_id="u1", chat_id="c1", chat_type="group")
        group_b = feishu.make_session_key(tenant_key="t2", open_id="u1", chat_id="c1", chat_type="group")

        self.assertEqual(len({p2p_a, p2p_b, group_a, group_b}), 4)
        self.assertEqual(p2p_a, "t1:p2p:u1")
        self.assertEqual(group_a, "t1:group:c1")
        with self.assertRaisesRegex(feishu.FeishuError, "会话类型"):
            feishu.make_session_key(tenant_key="t1", open_id="u1", chat_id="c1", chat_type="unknown")

    def test_session_binding_is_persisted_with_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = "tenant:p2p:user"
            saved = feishu.set_session_binding(
                root,
                key,
                active_run_id="run-001",
                active_task_id="task-006",
                acl_subject="feishu:ou_user",
            )

            self.assertEqual(feishu.get_session_binding(root, key), saved)
            self.assertEqual(saved["active_task_id"], "task-006")
            self.assertEqual(stat.S_IMODE(feishu.session_bindings_path(root).stat().st_mode), 0o600)

    def test_inbound_dedupe_expires_and_trims_oldest_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(feishu.claim_inbound_message(root, "m1", now=1, ttl_seconds=20, max_entries=2))
            self.assertFalse(feishu.claim_inbound_message(root, "m1", now=2, ttl_seconds=20, max_entries=2))
            self.assertTrue(feishu.claim_inbound_message(root, "m2", now=3, ttl_seconds=20, max_entries=2))
            self.assertTrue(feishu.claim_inbound_message(root, "m3", now=4, ttl_seconds=20, max_entries=2))
            saved = json.loads(feishu.inbound_dedupe_path(root).read_text(encoding="utf-8"))["messages"]

            self.assertNotIn("m1", saved)
            self.assertEqual(set(saved), {"m2", "m3"})
            self.assertTrue(feishu.claim_inbound_message(root, "m2", now=30, ttl_seconds=20, max_entries=2))

    def test_action_token_is_bound_one_time_and_keeps_server_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = feishu.issue_action_token(
                root,
                open_id="ou_user",
                session_key="tenant:p2p:ou_user",
                action="create_task",
                context={"run_id": "run-server", "title": "trusted"},
                now=100,
            )
            stored = feishu.action_tokens_path(root).read_text(encoding="utf-8")
            self.assertNotIn(token, stored)

            with self.assertRaisesRegex(feishu.FeishuError, "不匹配"):
                feishu.consume_action_token(
                    root,
                    token,
                    open_id="ou_attacker",
                    session_key="tenant:p2p:ou_user",
                    action="create_task",
                    now=101,
                )

            context = feishu.consume_action_token(
                root,
                token,
                open_id="ou_user",
                session_key="tenant:p2p:ou_user",
                action="create_task",
                now=102,
            )
            self.assertEqual(context, {"run_id": "run-server", "title": "trusted"})
            with self.assertRaisesRegex(feishu.FeishuError, "无效或已使用"):
                feishu.consume_action_token(
                    root,
                    token,
                    open_id="ou_user",
                    session_key="tenant:p2p:ou_user",
                    action="create_task",
                    now=103,
                )
            with self.assertRaisesRegex(feishu.FeishuError, "没有待确认"):
                feishu.consume_pending_action_token(
                    root,
                    open_id="ou_user",
                    session_key="tenant:p2p:ou_user",
                    action="service_assistant_change",
                    now=104,
                )

    def test_action_token_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = feishu.issue_action_token(
                root,
                open_id="ou_user",
                session_key="session",
                action="select_task",
                ttl_seconds=5,
                now=100,
            )

            with self.assertRaisesRegex(feishu.FeishuError, "已过期"):
                feishu.consume_action_token(
                    root,
                    token,
                    open_id="ou_user",
                    session_key="session",
                    action="select_task",
                    now=105,
                )

    def test_plain_confirmation_consumes_only_pending_action_and_new_preview_replaces_old(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = feishu.issue_action_token(
                root,
                open_id="ou_user",
                session_key="tenant:p2p:ou_user",
                action="service_assistant_change",
                context={"version": 1},
                now=100,
            )
            feishu.issue_action_token(
                root,
                open_id="ou_user",
                session_key="tenant:p2p:ou_user",
                action="service_assistant_change",
                context={"version": 2},
                now=101,
            )

            context = feishu.consume_pending_action_token(
                root,
                open_id="ou_user",
                session_key="tenant:p2p:ou_user",
                action="service_assistant_change",
                now=102,
            )

            self.assertEqual(context, {"version": 2})
            with self.assertRaisesRegex(feishu.FeishuError, "无效或已使用"):
                feishu.consume_action_token(
                    root,
                    first,
                    open_id="ou_user",
                    session_key="tenant:p2p:ou_user",
                    action="service_assistant_change",
                    now=103,
                )

    def test_card_confirmation_is_bound_to_exact_message_and_old_card_cannot_consume_new_action(self) -> None:
        card = {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": "确认"}}, "body": {"elements": []}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in (1, 2):
                confirmation_id = f"confirmation-{index}"
                feishu.issue_action_token(
                    root,
                    open_id="ou_user",
                    session_key="tenant:p2p:ou_user",
                    action="service_assistant_change",
                    context={"version": index, "confirmation_id": confirmation_id},
                    now=100 + index,
                )
                feishu.register_confirmation_card(
                    root,
                    confirmation_id,
                    open_id="ou_user",
                    session_key="tenant:p2p:ou_user",
                    action="service_assistant_change",
                    card=card,
                    expires_at=500,
                    now=100 + index,
                )
                feishu.bind_confirmation_card(root, confirmation_id, message_id=f"om-{index}", chat_id="oc-chat")

            with self.assertRaisesRegex(feishu.FeishuError, "已处理或失效"):
                feishu.consume_confirmation_card(
                    root,
                    message_id="om-1",
                    open_id="ou_user",
                    session_key="tenant:p2p:ou_user",
                    action="service_assistant_change",
                    decision="确认",
                    now=103,
                )
            context = feishu.consume_confirmation_card(
                root,
                message_id="om-2",
                open_id="ou_user",
                session_key="tenant:p2p:ou_user",
                action="service_assistant_change",
                decision="确认",
                now=104,
            )

        self.assertEqual(context["version"], 2)
        self.assertEqual(context["confirmation_message_id"], "om-2")

    def test_expired_confirmation_card_becomes_grey_and_has_no_buttons(self) -> None:
        card = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "请确认"}, "template": "orange"},
            "body": {"elements": [{"tag": "column_set", "columns": []}]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feishu.register_confirmation_card(
                root,
                "confirmation-expired",
                open_id="ou_user",
                session_key="session",
                action="change",
                card=card,
                expires_at=105,
                now=100,
            )
            feishu.bind_confirmation_card(root, "confirmation-expired", message_id="om-expired", chat_id="oc-chat")
            updates = feishu.pending_confirmation_card_updates(root, now=106)

        self.assertEqual(len(updates), 1)
        terminal = updates[0]["terminal_card"]
        self.assertEqual(terminal["header"]["template"], "grey")
        self.assertIn("失效", terminal["header"]["title"]["content"])
        self.assertFalse(any(item.get("tag") == "column_set" for item in terminal["body"]["elements"]))

    def test_tenant_token_is_cached_until_refresh_window(self) -> None:
        opener = QueueOpener(
            FakeResponse({"code": 0, "tenant_access_token": "t-one", "expire": 3600}),
            FakeResponse({"code": 0, "tenant_access_token": "t-two", "expire": 3600}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = feishu.get_tenant_access_token(root, "cli_app", "secret", opener=opener, now=100)
            cached = feishu.get_tenant_access_token(root, "cli_app", "secret", opener=opener, now=200)
            refreshed = feishu.get_tenant_access_token(root, "cli_app", "secret", opener=opener, now=3650)

            self.assertEqual((first, cached, refreshed), ("t-one", "t-one", "t-two"))
            self.assertEqual(len(opener.requests), 2)
            first_request = opener.requests[0][0]
            self.assertTrue(first_request.full_url.endswith("/open-apis/auth/v3/tenant_access_token/internal"))
            self.assertEqual(json.loads(first_request.data), {"app_id": "cli_app", "app_secret": "secret"})
            self.assertEqual(stat.S_IMODE(feishu.token_cache_path(root).stat().st_mode), 0o600)

    def test_send_text_and_card_build_feishu_message_requests(self) -> None:
        opener = QueueOpener(FakeResponse({"code": 0, "data": {"message_id": "om_text"}}), FakeResponse({"code": 0}))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text_result = feishu.send_text_message(
                root,
                "",
                "",
                "oc_chat",
                "hello",
                tenant_access_token="tenant-token",
                opener=opener,
            )
            feishu.send_card_message(
                root,
                "",
                "",
                "ou_user",
                {"elements": [{"tag": "markdown", "content": "card"}]},
                receive_id_type="open_id",
                tenant_access_token="tenant-token",
                opener=opener,
            )

        self.assertEqual(text_result["data"]["message_id"], "om_text")
        text_request = opener.requests[0][0]
        self.assertIn("receive_id_type=chat_id", text_request.full_url)
        self.assertEqual(text_request.get_header("Authorization"), "Bearer tenant-token")
        text_body = json.loads(text_request.data)
        self.assertEqual(text_body["msg_type"], "text")
        self.assertEqual(json.loads(text_body["content"]), {"text": "hello"})
        card_request = opener.requests[1][0]
        self.assertIn("receive_id_type=open_id", card_request.full_url)
        card_body = json.loads(card_request.data)
        self.assertEqual(card_body["msg_type"], "interactive")
        self.assertEqual(json.loads(card_body["content"])["elements"][0]["content"], "card")

    def test_update_card_uses_message_patch_endpoint(self) -> None:
        opener = QueueOpener(FakeResponse({"code": 0}))
        card = {"schema": "2.0", "body": {"elements": []}}
        with tempfile.TemporaryDirectory() as tmp:
            feishu.update_card_message(
                Path(tmp),
                "",
                "",
                "om-card",
                card,
                tenant_access_token="tenant-token",
                opener=opener,
            )

        request = opener.requests[0][0]
        self.assertEqual(request.method, "PATCH")
        self.assertTrue(request.full_url.endswith("/open-apis/im/v1/messages/om-card"))
        self.assertEqual(json.loads(json.loads(request.data)["content"]), card)

    def test_api_payload_error_is_wrapped_as_feishu_error(self) -> None:
        opener = QueueOpener(FakeResponse({"code": 99991400, "msg": "rate limited"}))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(feishu.FeishuError) as raised:
                feishu.send_text_message(
                    Path(tmp),
                    "",
                    "",
                    "oc_chat",
                    "hello",
                    tenant_access_token="token",
                    opener=opener,
                )

        self.assertEqual(raised.exception.code, 99991400)
        self.assertIn("rate limited", str(raised.exception))

    def test_custom_transport_error_is_wrapped_as_feishu_error(self) -> None:
        opener = QueueOpener(RuntimeError("transport stopped"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(feishu.FeishuError, "transport stopped"):
                feishu.send_text_message(
                    Path(tmp),
                    "",
                    "",
                    "oc_chat",
                    "hello",
                    tenant_access_token="token",
                    opener=opener,
                )

    def test_http_error_body_is_wrapped_as_feishu_error(self) -> None:
        error = HTTPError(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(json.dumps({"code": 99991400, "msg": "slow down"}).encode("utf-8")),
        )
        opener = QueueOpener(error)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(feishu.FeishuError) as raised:
                feishu.send_text_message(
                    Path(tmp),
                    "",
                    "",
                    "oc_chat",
                    "hello",
                    tenant_access_token="token",
                    opener=opener,
                )

        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(raised.exception.code, 99991400)


if __name__ == "__main__":
    unittest.main()
