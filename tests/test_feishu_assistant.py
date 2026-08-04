from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.services import feishu_assistant
from aha_cli.services.feishu import get_session_binding, set_session_binding
from aha_cli.services.feishu_notifications import set_subscription
from aha_cli.services.feishu_owner import feishu_owner_state_path
from aha_cli.store.paths import aha_home_path


class _CompletedFuture:
    def __init__(self, value: object) -> None:
        self.value = value

    def result(self, timeout: float | None = None) -> object:
        return self.value


class _SendResult:
    success = True
    message_id = "om_reply"


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object, object]] = []
        self.updated: list[tuple[str, dict]] = []

    async def send(self, target: str, message: object, opts: object = None) -> _SendResult:
        self.sent.append((target, message, opts))
        return _SendResult()

    async def update_card(self, message_id: str, card: dict) -> _SendResult:
        self.updated.append((message_id, card))
        return _SendResult()

    def schedule(self, coroutine) -> _CompletedFuture:
        return _CompletedFuture(asyncio.run(coroutine))


def _write_config(
    root: Path,
    allowed: list[str],
    *,
    allowed_chat_ids: list[str] | None = None,
    group_access_mode: str = "allowed_users",
) -> None:
    (root / "config.json").write_text(
        json.dumps(
            {
                "backend": "codex",
                "integrations": {
                    "feishu": {
                        "enabled": True,
                        "app_id": "cli_test",
                        "allowed_open_ids": allowed,
                        "allowed_chat_ids": allowed_chat_ids or [],
                        "group_access_mode": group_access_mode,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _payload(**changes: object) -> dict:
    payload = {
        "tenant_key": "tenant-1",
        "open_id": "ou_user",
        "chat_id": "oc_chat",
        "chat_type": "p2p",
        "message_id": "om_1",
        "text": "帮我看一下当前任务",
        "is_at_bot": False,
        "sender_is_bot": False,
    }
    payload.update(changes)
    return payload


class FeishuAssistantTests(unittest.TestCase):
    def test_group_message_without_bot_mention_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_config(root, ["ou_user"])
            channel = FakeChannel()
            feishu_assistant._handle_message(
                root,
                "",
                channel,
                _payload(chat_type="group", message_id="om_group", is_at_bot=False),
            )
        self.assertEqual(channel.sent, [])

    def test_group_message_without_bot_mention_is_ignored_even_when_legacy_switch_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant.handle_send_payload",
        ) as send:
            root = Path(tmp)
            _write_config(root, ["ou_user"], allowed_chat_ids=["oc_chat"])
            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            config["integrations"]["feishu"]["group_mentions_only"] = False
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            channel = FakeChannel()
            feishu_assistant._handle_message(
                root,
                "",
                channel,
                _payload(chat_type="group", message_id="om_group_no_at", is_at_bot=False),
            )

        send.assert_not_called()
        self.assertEqual(channel.sent, [])

    def test_unlisted_user_is_denied_before_session_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_config(root, [])
            channel = FakeChannel()
            feishu_assistant._handle_message(root, "", channel, _payload())
        self.assertEqual(len(channel.sent), 1)
        self.assertIn("尚未被授权", channel.sent[0][1]["text"])
        self.assertIn("open_id：ou_user", channel.sent[0][1]["text"])

    def test_unlisted_group_user_open_id_is_not_disclosed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_config(root, [])
            channel = FakeChannel()
            feishu_assistant._handle_message(
                root,
                "",
                channel,
                _payload(chat_type="group", message_id="om_denied_group", is_at_bot=True),
            )
        self.assertNotIn("ou_user", channel.sent[0][1]["text"])
        self.assertIn("该群尚未被授权", channel.sent[0][1]["text"])

    def test_group_acl_requires_allowed_chat_and_allowed_user_by_default(self) -> None:
        config = {
            "allowed_open_ids": ["ou_user"],
            "allowed_chat_ids": ["oc_allowed"],
            "group_access_mode": "allowed_users",
        }
        self.assertEqual(
            feishu_assistant._authorization_error(
                config, chat_type="group", chat_id="oc_other", open_id="ou_user"
            ),
            "chat_not_allowed",
        )
        self.assertEqual(
            feishu_assistant._authorization_error(
                config, chat_type="group", chat_id="oc_allowed", open_id="ou_other"
            ),
            "user_not_allowed",
        )
        self.assertEqual(
            feishu_assistant._authorization_error(
                config, chat_type="group", chat_id="oc_allowed", open_id="ou_user"
            ),
            "",
        )

    def test_group_acl_can_allow_all_members_without_affecting_private_chat(self) -> None:
        config = {
            "allowed_open_ids": ["ou_admin"],
            "allowed_chat_ids": ["oc_allowed"],
            "group_access_mode": "all_members",
        }
        self.assertEqual(
            feishu_assistant._authorization_error(
                config, chat_type="group", chat_id="oc_allowed", open_id="ou_member"
            ),
            "",
        )
        self.assertEqual(
            feishu_assistant._authorization_error(
                config, chat_type="p2p", chat_id="oc_private", open_id="ou_member"
            ),
            "user_not_allowed",
        )

    def test_group_message_is_recorded_as_recent_before_acl_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_config(root, [])
            feishu_assistant._handle_message(
                root,
                "",
                FakeChannel(),
                _payload(chat_type="group", chat_id="oc_detected", message_id="om_detected", is_at_bot=True),
            )
            detected = json.loads((aha_home_path(root) / "feishu" / "recent_groups.json").read_text())
        self.assertEqual(detected["groups"]["oc_detected"]["chat_id"], "oc_detected")

    def test_dedicated_run_is_created_with_english_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant.ensure_service_assistant_run",
            return_value="run-feishu",
        ) as ensure:
            root = Path(tmp)
            _write_config(root, ["ou_user"])
            run_id = feishu_assistant._dedicated_run(root)

        self.assertEqual(run_id, "run-feishu")
        self.assertEqual(ensure.call_args.args[0], root)
        self.assertEqual(ensure.call_args.args[1]["backend"], "codex")

    def test_dedicated_run_reuses_exact_active_english_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant.ensure_service_assistant_run",
            return_value="run-feishu",
        ) as ensure:
            run_id = feishu_assistant._dedicated_run(Path(tmp))

        self.assertEqual(run_id, "run-feishu")
        ensure.assert_called_once()

    def test_old_session_binding_is_migrated_to_dedicated_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant._dedicated_run",
            return_value="run-feishu",
        ):
            root = Path(tmp)
            set_session_binding(
                root,
                "tenant-1:p2p:ou_user",
                active_run_id="run-old",
                active_task_id="task-old",
                acl_subject="ou_user",
            )
            binding = feishu_assistant._binding(root, "tenant-1:p2p:ou_user", "ou_user", "run-default")

        self.assertEqual(binding["active_run_id"], "run-feishu")
        self.assertIsNone(binding["active_task_id"])

    def test_natural_language_routes_to_bound_real_agent_without_fixed_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant._dedicated_run",
            return_value="run-001",
        ), mock.patch(
            "aha_cli.services.feishu_assistant.run_exists",
            return_value=True,
        ), mock.patch(
            "aha_cli.services.feishu_assistant._active_task",
            return_value={"id": "task-006", "status": "running"},
        ), mock.patch(
            "aha_cli.services.feishu_assistant.handle_send_payload",
            return_value={"ok": True},
        ) as send, mock.patch(
            "aha_cli.services.feishu_assistant.set_subscription",
        ) as subscribe:
            root = Path(tmp)
            _write_config(root, ["ou_user"])
            set_session_binding(
                root,
                "tenant-1:p2p:ou_user",
                active_run_id="run-001",
                active_task_id="task-006",
                acl_subject="ou_user",
            )
            channel = FakeChannel()
            feishu_assistant._handle_message(root, "", channel, _payload(text="帮助"))

        self.assertEqual(send.call_args.args[1], "run-001")
        self.assertEqual(send.call_args.args[2]["task_id"], "task-006")
        self.assertEqual(send.call_args.args[2]["message"], "帮助")
        self.assertIs(send.call_args.kwargs["command_handler"], feishu_assistant._never_handle_command)
        subscribe.assert_called_once()
        self.assertEqual(channel.sent[-1][1]["text"], "已交给 AHA agent，回复会推送到本会话。")

    def test_slash_text_is_also_routed_to_agent(self) -> None:
        handled, agent_message, payload = feishu_assistant._never_handle_command(
            Path("/tmp"),
            "run-001",
            {},
            "/help",
            "task-001",
        )
        self.assertFalse(handled)
        self.assertIsNone(agent_message)
        self.assertEqual(payload, {})

    def test_assistant_task_title_is_stable_and_session_specific(self) -> None:
        dm = feishu_assistant._assistant_task_title("tenant-1:p2p:ou_user")
        group = feishu_assistant._assistant_task_title("tenant-1:group:oc_chat")

        self.assertRegex(dm, r"^AHA Assistant · DM · [0-9a-f]{6}$")
        self.assertRegex(group, r"^AHA Assistant · Group · [0-9a-f]{6}$")
        self.assertNotEqual(dm, group)
        self.assertEqual(dm, feishu_assistant._assistant_task_title("tenant-1:p2p:ou_user"))

    def test_recreated_session_task_gets_incrementing_suffix(self) -> None:
        session_key = "tenant-1:p2p:ou_user"
        base = feishu_assistant._assistant_task_title(session_key)
        self.assertEqual(base, feishu_assistant._assistant_task_title(session_key))
        self.assertNotEqual(base, feishu_assistant._assistant_task_title("tenant-1:p2p:ou_other"))

    def test_assistant_backend_and_model_override_global_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "backend": "codex",
                        "codex": {"model": "gpt-global"},
                        "integrations": {
                            "feishu": {
                                "backend": "claude",
                                "model": "claude-sonnet-4-6",
                                "reasoning_effort": "high",
                                "proxy_enabled": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            backend, model = feishu_assistant._assistant_backend(root)
            defaults = feishu_assistant._assistant_agent_defaults(root)

        self.assertEqual((backend, model), ("claude", "claude-sonnet-4-6"))
        self.assertEqual(defaults["reasoning_effort"], "high")
        self.assertTrue(defaults["proxy_enabled"])

    def test_first_message_creates_persistent_assistant_task(self) -> None:
        task = {"id": "task-007", "title": "AHA Assistant · DM · abc123", "status": "pending", "kind": "service_assistant", "system_managed": True}
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant._dedicated_run",
            return_value="run-001",
        ), mock.patch(
            "aha_cli.services.feishu_assistant.run_exists",
            return_value=True,
        ), mock.patch(
            "aha_cli.services.feishu_assistant.ensure_service_assistant_task",
            return_value=task,
        ) as create, mock.patch(
            "aha_cli.services.feishu_assistant.handle_send_payload",
            return_value={"ok": True},
        ) as send, mock.patch(
            "aha_cli.services.feishu_assistant.set_subscription",
        ):
            root = Path(tmp)
            _write_config(root, ["ou_user"])
            set_session_binding(
                root,
                "tenant-1:p2p:ou_user",
                active_run_id="run-001",
                active_task_id=None,
                acl_subject="ou_user",
            )
            channel = FakeChannel()
            feishu_assistant._handle_message(root, "", channel, _payload())
            binding = get_session_binding(root, "tenant-1:p2p:ou_user")

        self.assertEqual(create.call_args.args[:3], (root, "run-001", "tenant-1:p2p:ou_user"))
        self.assertEqual(create.call_args.args[3]["backend"], "codex")
        self.assertEqual(binding["active_task_id"], "task-007")
        self.assertEqual(send.call_args.args[2]["task_id"], "task-007")

    def test_private_chat_records_owner_binding_for_group_handoff(self) -> None:
        task = {
            "id": "task-owner",
            "title": "AHA Assistant · DM · abc123",
            "status": "pending",
            "kind": "service_assistant",
            "system_managed": True,
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant._dedicated_run",
            return_value="run-001",
        ), mock.patch(
            "aha_cli.services.feishu_assistant.run_exists",
            return_value=True,
        ), mock.patch(
            "aha_cli.services.feishu_assistant.ensure_service_assistant_task",
            return_value=task,
        ), mock.patch(
            "aha_cli.services.feishu_assistant.handle_send_payload",
            return_value={"ok": True},
        ):
            root = Path(tmp)
            _write_config(root, ["ou_user"])
            feishu_assistant._handle_message(root, "", FakeChannel(), _payload(chat_id="oc_owner_dm"))
            state = json.loads(feishu_owner_state_path(root).read_text(encoding="utf-8"))

        self.assertEqual(state["owners"]["tenant-1"]["open_id"], "ou_user")
        self.assertEqual(state["owners"]["tenant-1"]["chat_id"], "oc_owner_dm")
        self.assertEqual(state["owners"]["tenant-1"]["session_key"], "tenant-1:p2p:ou_user")

    def test_allowed_group_all_members_routes_to_digital_human_without_group_subscription(self) -> None:
        task = {"id": "task-group", "status": "pending", "kind": "feishu_group_digital_human"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant._feishu_group_run",
            return_value="run-001",
        ), mock.patch(
            "aha_cli.services.feishu_assistant.ensure_feishu_group_task",
            return_value=task,
        ), mock.patch(
            "aha_cli.services.feishu_assistant.mark_feishu_group_task_interaction",
        ), mock.patch(
            "aha_cli.services.feishu_assistant.handle_send_payload",
            return_value={"ok": True},
        ) as send, mock.patch(
            "aha_cli.services.feishu_assistant.set_subscription",
        ) as subscribe:
            root = Path(tmp)
            _write_config(
                root,
                ["ou_admin"],
                allowed_chat_ids=["oc_chat"],
                group_access_mode="all_members",
            )
            channel = FakeChannel()
            feishu_assistant._handle_message(
                root,
                "",
                channel,
                _payload(chat_type="group", open_id="ou_member", is_at_bot=True),
            )
            binding = get_session_binding(root, "tenant-1:feishu-group-user:ou_member")

        self.assertEqual(send.call_args.args[2]["task_id"], "task-group")
        self.assertEqual(send.call_args.args[1], "run-001")
        self.assertIn("飞书群聊 @ 数字人请求", send.call_args.args[2]["message"])
        self.assertEqual(send.call_args.args[2]["reply_target"], "feishu")
        self.assertEqual(send.call_args.args[2]["feishu_chat_id"], "oc_chat")
        self.assertEqual(send.call_args.args[2]["feishu_mention_open_id"], "ou_member")
        self.assertEqual(send.call_args.args[2]["feishu_channel"], "group_digital_human")
        self.assertEqual(binding["active_task_id"], "task-group")
        subscribe.assert_not_called()
        self.assertEqual(channel.sent, [])

    def test_group_digital_human_task_is_keyed_by_sender_across_groups(self) -> None:
        task = {"id": "task-user", "status": "pending", "kind": "feishu_group_digital_human"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant._feishu_group_run",
            return_value="run-001",
        ), mock.patch(
            "aha_cli.services.feishu_assistant.ensure_feishu_group_task",
            return_value=task,
        ) as ensure, mock.patch(
            "aha_cli.services.feishu_assistant._active_group_task",
            side_effect=[None, task],
        ), mock.patch(
            "aha_cli.services.feishu_assistant.mark_feishu_group_task_interaction",
        ), mock.patch(
            "aha_cli.services.feishu_assistant.handle_send_payload",
            return_value={"ok": True},
        ):
            root = Path(tmp)
            _write_config(root, ["ou_admin"], allowed_chat_ids=["oc_a", "oc_b"], group_access_mode="all_members")
            channel = FakeChannel()
            feishu_assistant._handle_message(
                root,
                "",
                channel,
                _payload(chat_type="group", chat_id="oc_a", open_id="ou_member", message_id="om_a", is_at_bot=True),
            )
            feishu_assistant._handle_message(
                root,
                "",
                channel,
                _payload(chat_type="group", chat_id="oc_b", open_id="ou_member", message_id="om_b", is_at_bot=True),
            )

        ensure.assert_called_once()

    def test_duplicate_message_is_not_sent_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant._dedicated_run",
            return_value="run-001",
        ), mock.patch(
            "aha_cli.services.feishu_assistant.run_exists",
            return_value=True,
        ), mock.patch(
            "aha_cli.services.feishu_assistant._active_task",
            return_value={"id": "task-006", "status": "running"},
        ), mock.patch(
            "aha_cli.services.feishu_assistant.handle_send_payload",
            return_value={"ok": True},
        ) as send, mock.patch(
            "aha_cli.services.feishu_assistant.set_subscription",
        ):
            root = Path(tmp)
            _write_config(root, ["ou_user"])
            set_session_binding(
                root,
                "tenant-1:p2p:ou_user",
                active_run_id="run-001",
                active_task_id="task-006",
                acl_subject="ou_user",
            )
            channel = FakeChannel()
            feishu_assistant._handle_message(root, "", channel, _payload())
            feishu_assistant._handle_message(root, "", channel, _payload())

        send.assert_called_once()

    def test_confirmation_card_action_uses_bound_actor_and_pending_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant.resolve_confirmation",
            return_value={"cancelled": False, "tool_message": "trusted result"},
        ) as resolve, mock.patch(
            "aha_cli.services.feishu_assistant.handle_send_payload",
            return_value={"ok": True},
        ) as send:
            root = Path(tmp)
            _write_config(root, ["ou_user"])
            set_subscription(
                root,
                "tenant-1:p2p:ou_user",
                chat_id="oc_chat",
                open_id="ou_user",
                run_id="run-001",
                task_id="task-006",
            )
            channel = FakeChannel()

            feishu_assistant._handle_card_action(
                root,
                channel,
                {
                    "kind": "card_action",
                    "chat_id": "oc_chat",
                    "message_id": "om_card",
                    "open_id": "ou_user",
                    "action": {
                        "kind": "aha_service_confirmation",
                        "decision": "confirm",
                    },
                },
            )

        resolve.assert_called_once_with(
            root,
            open_id="ou_user",
            session_key="tenant-1:p2p:ou_user",
            text="确认",
            message_id="om_card",
        )
        self.assertEqual(send.call_args.args[1], "run-001")
        self.assertEqual(send.call_args.args[2]["task_id"], "task-006")
        self.assertEqual(send.call_args.args[2]["message"], "trusted result")
        self.assertIn("操作已确认并执行", channel.sent[-1][1]["text"])

    def test_choice_card_action_returns_selected_option_to_assistant_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant.resolve_choice",
            return_value={"choice": True, "cancelled": False, "tool_message": "trusted choice"},
        ) as resolve, mock.patch(
            "aha_cli.services.feishu_assistant.handle_send_payload",
            return_value={"ok": True},
        ) as send:
            root = Path(tmp)
            _write_config(root, ["ou_user"])
            set_subscription(
                root,
                "tenant-1:p2p:ou_user",
                chat_id="oc_chat",
                open_id="ou_user",
                run_id="run-001",
                task_id="task-006",
            )
            channel = FakeChannel()

            feishu_assistant._handle_card_action(
                root,
                channel,
                {
                    "kind": "card_action",
                    "chat_id": "oc_chat",
                    "message_id": "om_choice",
                    "open_id": "ou_user",
                    "action": {
                        "kind": "aha_service_choice",
                        "choice_id": "private",
                    },
                },
            )

        resolve.assert_called_once_with(
            root,
            open_id="ou_user",
            session_key="tenant-1:p2p:ou_user",
            message_id="om_choice",
            choice_id="private",
        )
        self.assertEqual(send.call_args.args[1], "run-001")
        self.assertEqual(send.call_args.args[2]["task_id"], "task-006")
        self.assertEqual(send.call_args.args[2]["message"], "trusted choice")
        self.assertIn("已收到选择", channel.sent[-1][1]["text"])

    def test_choice_card_action_passes_form_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant.resolve_choice",
            return_value={"choice": True, "cancelled": False, "tool_message": "trusted choice"},
        ) as resolve, mock.patch(
            "aha_cli.services.feishu_assistant.handle_send_payload",
            return_value={"ok": True},
        ):
            root = Path(tmp)
            _write_config(root, ["ou_user"])
            set_subscription(
                root,
                "tenant-1:p2p:ou_user",
                chat_id="oc_chat",
                open_id="ou_user",
                run_id="run-001",
                task_id="task-006",
            )
            channel = FakeChannel()

            feishu_assistant._handle_card_action(
                root,
                channel,
                {
                    "kind": "card_action",
                    "chat_id": "oc_chat",
                    "message_id": "om_choice",
                    "open_id": "ou_user",
                    "action": {
                        "kind": "aha_service_choice",
                        "choice_id": "__submit_task_config__",
                    },
                    "form_values": {"run_id": "run-a", "backend_model": "codex::gpt-5.5"},
                },
            )

        resolve.assert_called_once_with(
            root,
            open_id="ou_user",
            session_key="tenant-1:p2p:ou_user",
            message_id="om_choice",
            choice_id="__submit_task_config__",
            form_values={"run_id": "run-a", "backend_model": "codex::gpt-5.5"},
        )

    def test_bare_confirmation_text_is_forwarded_as_normal_private_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant.run_exists",
            return_value=True,
        ), mock.patch(
            "aha_cli.services.feishu_assistant._active_task",
            return_value={"id": "task-006", "status": "awaiting_user"},
        ), mock.patch(
            "aha_cli.services.feishu_assistant.handle_send_payload",
            return_value={"ok": True},
        ) as send:
            root = Path(tmp)
            _write_config(root, ["ou_user"])
            set_session_binding(
                root,
                "tenant-1:p2p:ou_user",
                active_run_id="run-001",
                active_task_id="task-006",
                acl_subject="ou_user",
            )
            channel = FakeChannel()

            feishu_assistant._handle_message(root, "", channel, _payload(text="确认"))

        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[2]["message"], "确认")
        self.assertNotIn("无法处理确认", channel.sent[-1][1]["text"])

    def test_resolved_confirmation_updates_original_card_before_reply(self) -> None:
        channel = FakeChannel()
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant.mark_confirmation_card_updated",
        ) as marked:
            feishu_assistant._finish_confirmation(
                Path(tmp),
                channel,
                chat_id="oc_chat",
                message_id="om_user_reply",
                run_id="run-001",
                task_id="task-006",
                confirmation={
                    "cancelled": True,
                    "confirmation_id": "confirmation-1",
                    "confirmation_message_id": "om_card",
                    "confirmation_card": {"schema": "2.0", "header": {"template": "grey"}},
                    "user_response": "已取消。",
                },
            )

        self.assertEqual(channel.updated[0][0], "om_card")
        self.assertEqual(channel.updated[0][1]["header"]["template"], "grey")
        marked.assert_called_once_with(Path(tmp), "confirmation-1")
        self.assertIn("已取消", channel.sent[-1][1]["text"])

    def test_task_workspace_skips_missing_previous_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            selected = feishu_assistant._task_workspace(Path(tmp), "run-001")

        self.assertEqual(selected, str(aha_home_path(Path(tmp)).resolve()))

    def test_active_task_with_missing_workspace_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant.task_snapshot",
            return_value={
                "task": {
                    "id": "task-002",
                    "status": "awaiting_user",
                    "workspace_path": str(Path(tmp) / "missing"),
                }
            },
        ):
            active = feishu_assistant._active_task(Path(tmp), "run-001", "task-002")

        self.assertIsNone(active)


if __name__ == "__main__":
    unittest.main()
