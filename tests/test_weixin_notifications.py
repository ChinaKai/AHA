from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.services import weixin_notifications
from aha_cli.store.filesystem import create_plan


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

    def test_notify_allowed_message_routes(self) -> None:
        allowed_routes = [
            ("browser", "main"),
            ("main", "browser"),
            ("main", "host"),
            ("host", "main"),
            ("host", "browser"),
        ]
        for index, (sender, target) in enumerate(allowed_routes, start=1):
            with self.subTest(route=f"{sender}->{target}"):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    run_id = self.create_run(root)
                    weixin_notifications.set_notifications_enabled(root, run_id, True)
                    event = {
                        "event_id": 100 + index,
                        "type": "message",
                        "data": {
                            "task_id": "task-001",
                            "sender": sender,
                            "target": target,
                            "message": f"{sender} to {target}",
                        },
                    }
                    with (
                        mock.patch("aha_cli.services.weixin_notifications.load_account", return_value={"token": "mock-token", "user_id": "user-1@im.wechat"}),
                        mock.patch("aha_cli.services.weixin_notifications.send_test_notification", return_value={"message_id": "msg-1"}) as send,
                    ):
                        result = weixin_notifications.notify_event(root, run_id, event)

                self.assertTrue(result["sent"])
                send.assert_called_once()
                message = send.call_args.args[2]
                self.assertIn("AHA 消息通知", message)
                self.assertIn("Run: Notify goal", message)
                self.assertIn("Notify task (task-001)", message)
                self.assertIn(f"Route: {sender} -> {target}", message)
                self.assertIn(f"内容: {sender} to {target}", message)

    def test_notify_message_dedupes_same_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            weixin_notifications.set_notifications_enabled(root, run_id, True)
            event = {
                "event_id": 123,
                "type": "message",
                "data": {"task_id": "task-001", "sender": "main", "target": "browser", "message": "done"},
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

    def test_notify_message_uses_display_route_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            weixin_notifications.set_notifications_enabled(root, run_id, True)
            event = {
                "event_id": 123,
                "type": "message",
                "data": {
                    "task_id": "task-001",
                    "sender": "browser",
                    "target": "main",
                    "display_sender": "host",
                    "display_target": "main",
                    "message": "host forwarded message",
                },
            }
            with (
                mock.patch("aha_cli.services.weixin_notifications.load_account", return_value={"token": "mock-token", "user_id": "user-1@im.wechat"}),
                mock.patch("aha_cli.services.weixin_notifications.send_test_notification", return_value={"message_id": "msg-1"}) as send,
            ):
                result = weixin_notifications.notify_event(root, run_id, event)

        self.assertTrue(result["sent"])
        message = send.call_args.args[2]
        self.assertIn("Route: host -> main", message)
        self.assertIn("内容: host forwarded message", message)

    def test_notify_disallowed_message_route_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            weixin_notifications.set_notifications_enabled(root, run_id, True)
            event = {
                "event_id": 123,
                "type": "message",
                "data": {"task_id": "task-001", "sender": "sub-001", "target": "main", "message": "internal"},
            }
            with (
                mock.patch("aha_cli.services.weixin_notifications.load_account", return_value={"token": "mock-token", "user_id": "user-1@im.wechat"}),
                mock.patch("aha_cli.services.weixin_notifications.send_test_notification", return_value={"message_id": "msg-1"}) as send,
            ):
                result = weixin_notifications.notify_event(root, run_id, event)

        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "empty_message")
        send.assert_not_called()

    def test_notify_status_and_round_events_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            weixin_notifications.set_notifications_enabled(root, run_id, True)
            events = [
                {"event_id": 123, "type": "task_status_changed", "data": {"task_id": "task-001", "status": "awaiting_user"}},
                {"event_id": 124, "type": "task_round_recorded", "data": {"task_id": "task-001", "round_id": "round-001"}},
            ]
            with (
                mock.patch("aha_cli.services.weixin_notifications.load_account", return_value={"token": "mock-token", "user_id": "user-1@im.wechat"}),
                mock.patch("aha_cli.services.weixin_notifications.send_test_notification", return_value={"message_id": "msg-1"}) as send,
            ):
                results = [weixin_notifications.notify_event(root, run_id, event) for event in events]

        self.assertEqual([result["reason"] for result in results], ["ignored_event", "ignored_event"])
        send.assert_not_called()

    def test_notify_message_without_event_id_uses_route_and_text_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            weixin_notifications.set_notifications_enabled(root, run_id, True)
            event = {
                "type": "message",
                "data": {"task_id": "task-001", "sender": "host", "target": "browser", "message": "please review"},
            }
            with (
                mock.patch("aha_cli.services.weixin_notifications.load_account", return_value={"token": "mock-token", "user_id": "user-1@im.wechat"}),
                mock.patch("aha_cli.services.weixin_notifications.send_test_notification", return_value={"message_id": "msg-1"}) as send,
            ):
                first = weixin_notifications.notify_event(root, run_id, event)
                second = weixin_notifications.notify_event(root, run_id, event)

        self.assertTrue(first["sent"])
        self.assertEqual(second["reason"], "duplicate")
        self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
