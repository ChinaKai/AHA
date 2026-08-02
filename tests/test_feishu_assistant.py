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

    def test_dedicated_run_is_created_with_english_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant.list_run_summaries",
            return_value=[],
        ), mock.patch(
            "aha_cli.services.feishu_assistant.create_plan",
            return_value={"id": "run-feishu"},
        ) as create:
            root = Path(tmp)
            _write_config(root, ["ou_user"])
            run_id = feishu_assistant._dedicated_run(root)

        self.assertEqual(run_id, "run-feishu")
        self.assertEqual(create.call_args.args[:3], (root, "Feishu Assistant", 1))
        self.assertFalse(create.call_args.kwargs["create_default_tasks"])

    def test_dedicated_run_reuses_exact_active_english_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant.list_run_summaries",
            return_value=[
                {"id": "run-other", "goal": "飞书助手", "lifecycle": {"status": "active"}},
                {"id": "run-feishu", "goal": "Feishu Assistant", "lifecycle": {"status": "active"}},
            ],
        ), mock.patch("aha_cli.services.feishu_assistant.create_plan") as create:
            run_id = feishu_assistant._dedicated_run(Path(tmp))

        self.assertEqual(run_id, "run-feishu")
        create.assert_not_called()

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

        self.assertRegex(dm, r"^Feishu Assistant · DM · [0-9a-f]{6}$")
        self.assertRegex(group, r"^Feishu Assistant · Group · [0-9a-f]{6}$")
        self.assertNotEqual(dm, group)
        self.assertEqual(dm, feishu_assistant._assistant_task_title("tenant-1:p2p:ou_user"))

    def test_recreated_session_task_gets_incrementing_suffix(self) -> None:
        session_key = "tenant-1:p2p:ou_user"
        base = feishu_assistant._assistant_task_title(session_key)
        with mock.patch(
            "aha_cli.services.feishu_assistant.require_plan",
            return_value={"tasks": [{"title": base}, {"title": f"{base} #2"}]},
        ):
            title = feishu_assistant._next_assistant_task_title(Path("/tmp"), "run-001", session_key)

        self.assertEqual(title, f"{base} #3")

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
        task = {"id": "task-007", "title": "Feishu Assistant · DM · abc123", "status": "pending"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_assistant._dedicated_run",
            return_value="run-001",
        ), mock.patch(
            "aha_cli.services.feishu_assistant.run_exists",
            return_value=True,
        ), mock.patch(
            "aha_cli.services.feishu_assistant.create_task_and_dispatch",
            return_value=task,
        ) as create, mock.patch(
            "aha_cli.services.feishu_assistant._task_workspace",
            return_value="/workspace",
        ), mock.patch(
            "aha_cli.services.feishu_assistant._next_assistant_task_title",
            return_value="Feishu Assistant · DM · abc123",
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

        self.assertEqual(create.call_args.args[:3], (root, "run-001", "Feishu Assistant · DM · abc123"))
        self.assertEqual(create.call_args.kwargs["backend"], "codex")
        self.assertIsNone(create.call_args.kwargs["model"])
        self.assertIsNone(create.call_args.kwargs["reasoning_effort"])
        self.assertFalse(create.call_args.kwargs["proxy_enabled"])
        self.assertIn("持续对话的真实 AHA 助手", create.call_args.kwargs["description"])
        self.assertEqual(binding["active_task_id"], "task-007")
        self.assertEqual(send.call_args.args[2]["task_id"], "task-007")

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
