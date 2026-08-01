from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.services import feishu_assistant
from aha_cli.services.feishu import get_session_binding, set_session_binding


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

    async def send(self, target: str, message: object, opts: object = None) -> _SendResult:
        self.sent.append((target, message, opts))
        return _SendResult()

    def schedule(self, coroutine) -> _CompletedFuture:
        return _CompletedFuture(asyncio.run(coroutine))


def _write_config(root: Path, allowed: list[str]) -> None:
    (root / "config.json").write_text(
        json.dumps(
            {
                "backend": "codex",
                "integrations": {
                    "feishu": {
                        "enabled": True,
                        "app_id": "cli_test",
                        "allowed_open_ids": allowed,
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
        self.assertIn("请私聊机器人", channel.sent[0][1]["text"])

    def test_message_without_run_explains_required_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant.list_run_summaries",
            return_value=[],
        ):
            root = Path(tmp)
            _write_config(root, ["ou_user"])
            channel = FakeChannel()
            feishu_assistant._handle_message(root, "", channel, _payload())

        self.assertIn("尚无可用 Run", channel.sent[-1][1]["text"])

    def test_natural_language_routes_to_bound_real_agent_without_fixed_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
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

    def test_first_message_creates_persistent_assistant_task(self) -> None:
        task = {"id": "task-007", "title": "AHA 飞书助手", "status": "pending"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant.run_exists",
            return_value=True,
        ), mock.patch(
            "aha_cli.services.feishu_assistant.create_task_and_dispatch",
            return_value=task,
        ) as create, mock.patch(
            "aha_cli.services.feishu_assistant._task_workspace",
            return_value="/workspace",
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
                active_task_id=None,
                acl_subject="ou_user",
            )
            channel = FakeChannel()
            feishu_assistant._handle_message(root, "", channel, _payload())
            binding = get_session_binding(root, "tenant-1:p2p:ou_user")

        self.assertEqual(create.call_args.args[:3], (root, "run-001", "AHA 飞书助手"))
        self.assertEqual(create.call_args.kwargs["backend"], "codex")
        self.assertIn("持续对话的真实 AHA 助手", create.call_args.kwargs["description"])
        self.assertEqual(binding["active_task_id"], "task-007")
        self.assertEqual(send.call_args.args[2]["task_id"], "task-007")

    def test_duplicate_message_is_not_sent_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
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

    def test_task_workspace_skips_missing_previous_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as workspace, mock.patch(
            "aha_cli.services.feishu_assistant.require_plan",
            return_value={
                "tasks": [
                    {"id": "task-001", "workspace_path": workspace},
                    {"id": "task-002", "workspace_path": str(Path(tmp) / "missing")},
                ]
            },
        ):
            selected = feishu_assistant._task_workspace(Path(tmp), "run-001")

        self.assertEqual(selected, workspace)

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
