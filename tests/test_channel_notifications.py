from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from aha_cli.services.channel_notifications import (
    deliver_notification_event,
    enabled_notification_channels,
    enqueue_notification_event,
    wait_for_notification_queue,
)


class ChannelNotificationTests(unittest.TestCase):
    def test_enabled_channels_follow_integration_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "integrations": {
                            "weixin": {"enabled": False},
                            "feishu": {"enabled": True, "notifications_enabled": True},
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(enabled_notification_channels(root), ["feishu"])

    def test_feishu_channel_stays_enabled_for_direct_replies_when_status_push_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "integrations": {
                            "feishu": {"enabled": True, "notifications_enabled": False},
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(enabled_notification_channels(root), ["feishu"])

    def test_deliver_isolates_channel_failures(self) -> None:
        event = {"type": "message", "event_id": 10, "data": {"task_id": "task-001"}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.weixin_notifications.notify_event",
            side_effect=RuntimeError("weixin down"),
        ), mock.patch(
            "aha_cli.services.feishu_notifications.notify_event",
            return_value={"ok": True, "sent": True},
        ):
            root = Path(tmp)
            results = deliver_notification_event(root, "run-001", event, ["weixin", "feishu"])

        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[1], {"channel": "feishu", "ok": True, "sent": True})

    def test_bounded_wait_times_out_and_later_drains(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocked_delivery(root: Path, run_id: str, event: dict) -> list[dict]:
            started.set()
            release.wait(timeout=1.0)
            return []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(
                json.dumps({"integrations": {"feishu": {"enabled": True}}}),
                encoding="utf-8",
            )
            try:
                with mock.patch(
                    "aha_cli.services.channel_notifications.deliver_notification_event",
                    side_effect=blocked_delivery,
                ):
                    result = enqueue_notification_event(root, "run-001", {"type": "message"})
                    self.assertTrue(result["queued"])
                    self.assertTrue(started.wait(timeout=1.0))
                    before = time.monotonic()
                    self.assertFalse(wait_for_notification_queue(timeout_seconds=0.01))
                    self.assertLess(time.monotonic() - before, 0.5)
                    release.set()
                    self.assertTrue(wait_for_notification_queue(timeout_seconds=1.0))
            finally:
                release.set()


if __name__ == "__main__":
    unittest.main()
