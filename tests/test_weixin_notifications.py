from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.services import weixin_notifications
from aha_cli.store.filesystem import create_plan
from aha_cli.store.io import append_jsonl
from aha_cli.store.rounds import task_rounds_path


class WeixinNotificationsTests(unittest.TestCase):
    def create_run(self, root: Path) -> str:
        plan = create_plan(root, "Notify goal", 1, "research", ["Notify task"], [])
        return str(plan["id"])

    def test_status_defaults_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            status = weixin_notifications.notification_status(root, run_id)

        self.assertFalse(status["enabled"])
        self.assertFalse(status["ready"])
        self.assertEqual(status["sent_count"], 0)

    def test_notify_status_change_sends_waiting_message_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            weixin_notifications.set_notifications_enabled(root, run_id, True)
            event = {
                "event_id": 123,
                "type": "task_status_changed",
                "data": {"task_id": "task-001", "status": "awaiting_user", "exit_code": None},
            }
            with (
                mock.patch("aha_cli.services.weixin_notifications.load_account", return_value={"token": "mock-token", "user_id": "user-1@im.wechat"}),
                mock.patch("aha_cli.services.weixin_notifications.send_test_notification", return_value={"message_id": "msg-1"}) as send,
            ):
                first = weixin_notifications.notify_event(root, run_id, event)
                second = weixin_notifications.notify_event(root, run_id, event)

        self.assertTrue(first["sent"])
        self.assertEqual(second["reason"], "duplicate")
        send.assert_called_once()
        message = send.call_args.args[2]
        self.assertIn("AHA 等待你处理", message)
        self.assertIn("Notify task (task-001)", message)

    def test_notify_round_recorded_sends_completion_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            append_jsonl(
                task_rounds_path(root, run_id, "task-001"),
                {
                    "task_id": "task-001",
                    "round_id": "round-001",
                    "journal_id": "journal-001",
                    "summary": "完成微信通知通道",
                    "changed_files": ["src/aha_cli/services/weixin_notifications.py"],
                    "verification": ["python3 -m unittest tests.test_weixin_notifications"],
                    "risks": [],
                },
            )
            weixin_notifications.set_notifications_enabled(root, run_id, True)
            event = {
                "event_id": 456,
                "type": "task_round_recorded",
                "data": {"task_id": "task-001", "round_id": "round-001", "journal_id": "journal-001"},
            }
            with (
                mock.patch("aha_cli.services.weixin_notifications.load_account", return_value={"token": "mock-token", "user_id": "user-1@im.wechat"}),
                mock.patch("aha_cli.services.weixin_notifications.send_test_notification", return_value={"message_id": "msg-2"}) as send,
            ):
                result = weixin_notifications.notify_event(root, run_id, event)

        self.assertTrue(result["sent"])
        message = send.call_args.args[2]
        self.assertIn("AHA 完成摘要", message)
        self.assertIn("完成微信通知通道", message)
        self.assertIn("python3 -m unittest tests.test_weixin_notifications", message)


if __name__ == "__main__":
    unittest.main()
