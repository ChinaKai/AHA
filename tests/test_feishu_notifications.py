from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.services import feishu_notifications
from aha_cli.services.feishu_notifications import notification_message_for_event, notify_event, set_subscription
from aha_cli.services.service_assistant_handoffs import register_service_handoff, service_handoffs_path
from aha_cli.store.io import append_jsonl
from aha_cli.store.paths import event_path, plan_path


def _setup(root: Path, *, notifications_enabled: bool = True) -> str:
    run_id = "run-a"
    (root / "config.json").write_text(
        json.dumps(
            {
                "integrations": {
                    "feishu": {
                        "enabled": True,
                        "notifications_enabled": notifications_enabled,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    path = plan_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"goal": "Run A", "tasks": [{"id": "task-001", "title": "Task 1"}]}),
        encoding="utf-8",
    )
    return run_id


class FeishuNotificationTests(unittest.TestCase):
    def test_status_change_contains_transition_and_latest_agent_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = _setup(root)
            append_jsonl(
                event_path(root, run_id),
                {
                    "event_id": 1,
                    "type": "task_status_changed",
                    "data": {"task_id": "task-001", "previous_status": "pending", "status": "running"},
                },
            )
            append_jsonl(
                event_path(root, run_id),
                {
                    "event_id": 2,
                    "type": "message",
                    "data": {"task_id": "task-001", "sender": "main", "target": "browser", "message": "最后一条\nagent 回复"},
                },
            )
            event = {
                "type": "task_status_changed",
                "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
            }
            event["event_id"] = append_jsonl(event_path(root, run_id), event)

            message = notification_message_for_event(root, run_id, event)

        self.assertEqual(
            message,
            "run-a task-001:\nstatus: busy->awaiting\nmessage: 最后一条 agent 回复",
        )

    def test_persisted_event_offset_finds_reply_before_current_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = _setup(root)
            append_jsonl(
                event_path(root, run_id),
                {
                    "ts": "2026-08-01T00:00:00+00:00",
                    "type": "task_status_changed",
                    "data": {"task_id": "task-001", "previous_status": "awaiting_user", "status": "running"},
                },
            )
            append_jsonl(
                event_path(root, run_id),
                {
                    "ts": "2026-08-01T00:00:01+00:00",
                    "type": "message",
                    "data": {"task_id": "task-001", "sender": "main", "target": "feishu", "message": "真实最终回复"},
                },
            )
            event = {
                "ts": "2026-08-01T00:00:02+00:00",
                "type": "task_status_changed",
                "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
            }
            event["event_id"] = append_jsonl(event_path(root, run_id), event)
            append_jsonl(
                event_path(root, run_id),
                {
                    "ts": "2026-08-01T00:00:03+00:00",
                    "type": "message",
                    "data": {"task_id": "task-001", "sender": "main", "target": "feishu", "message": "未来回复"},
                },
            )

            message = notification_message_for_event(root, run_id, event)

        self.assertIn("message: 真实最终回复", message)
        self.assertNotIn("未来回复", message)

    def test_status_change_does_not_reuse_reply_from_previous_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = _setup(root)
            append_jsonl(
                event_path(root, run_id),
                {
                    "type": "message",
                    "ts": "2026-08-01T00:00:00+00:00",
                    "data": {"task_id": "task-001", "sender": "main", "target": "browser", "message": "old reply"},
                },
            )
            append_jsonl(
                event_path(root, run_id),
                {
                    "type": "task_status_changed",
                    "ts": "2026-08-01T00:00:01+00:00",
                    "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
                },
            )
            event = {
                "type": "task_status_changed",
                "ts": "2026-08-01T00:00:02+00:00",
                "data": {"task_id": "task-001", "previous_status": "awaiting_user", "status": "running"},
            }
            append_jsonl(event_path(root, run_id), event)

            message = notification_message_for_event(root, run_id, event)

        self.assertIn("status: awaiting->busy", message)
        self.assertIn("message: -", message)

    def test_entering_busy_contains_triggering_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = _setup(root)
            append_jsonl(
                event_path(root, run_id),
                {
                    "type": "task_status_changed",
                    "ts": "2026-08-01T00:00:00+00:00",
                    "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
                },
            )
            append_jsonl(
                event_path(root, run_id),
                {
                    "type": "message",
                    "ts": "2026-08-01T00:00:01+00:00",
                    "data": {
                        "task_id": "task-001",
                        "sender": "browser",
                        "target": "main",
                        "message": "请继续修复\n这个问题",
                    },
                },
            )
            event = {
                "type": "task_status_changed",
                "ts": "2026-08-01T00:00:02+00:00",
                "data": {"task_id": "task-001", "previous_status": "awaiting_user", "status": "running"},
            }
            append_jsonl(event_path(root, run_id), event)

            message = notification_message_for_event(root, run_id, event)

        self.assertIn("status: awaiting->busy", message)
        self.assertIn("message: 请继续修复 这个问题", message)

    def test_service_assistant_routed_status_uses_request_and_target_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = _setup(root)
            append_jsonl(
                event_path(root, run_id),
                {
                    "type": "message",
                    "ts": "2026-08-01T00:00:01+00:00",
                    "data": {
                        "task_id": "task-001",
                        "sender": "feishu-assistant",
                        "target": "main",
                        "message": "请调研卡片置灰",
                    },
                },
            )
            busy = {
                "type": "task_status_changed",
                "ts": "2026-08-01T00:00:02+00:00",
                "data": {"task_id": "task-001", "previous_status": "awaiting_user", "status": "running"},
            }
            append_jsonl(event_path(root, run_id), busy)
            self.assertIn("message: 请调研卡片置灰", notification_message_for_event(root, run_id, busy))

            append_jsonl(
                event_path(root, run_id),
                {
                    "type": "message",
                    "ts": "2026-08-01T00:00:03+00:00",
                    "data": {
                        "task_id": "task-001",
                        "sender": "main",
                        "target": "feishu-assistant",
                        "message": "调研完成，方案可行",
                    },
                },
            )
            awaiting = {
                "type": "task_status_changed",
                "ts": "2026-08-01T00:00:04+00:00",
                "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
            }
            append_jsonl(event_path(root, run_id), awaiting)

            message = notification_message_for_event(root, run_id, awaiting)

        self.assertIn("message: 调研完成，方案可行", message)
        self.assertNotIn("message: -", message)

    def test_system_status_uses_event_reason_when_no_chat_message_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = _setup(root)
            event = {
                "type": "task_status_changed",
                "ts": "2026-08-01T00:00:02+00:00",
                "data": {
                    "task_id": "task-001",
                    "previous_status": "pending",
                    "status": "failed",
                    "reason": "backend launch failed",
                },
            }
            append_jsonl(event_path(root, run_id), event)

            message = notification_message_for_event(root, run_id, event)

        self.assertIn("status: pending->failed", message)
        self.assertIn("message: backend launch failed", message)

    def test_status_events_without_event_ids_use_distinct_delivery_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-1"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root)
            set_subscription(
                root,
                "tenant:p2p:user",
                chat_id="oc-chat",
                open_id="ou-user",
                run_id=run_id,
                task_id="task-001",
            )
            first = {
                "ts": "2026-08-01T00:00:01+00:00",
                "type": "task_status_changed",
                "data": {"task_id": "task-001", "previous_status": "awaiting_user", "status": "running"},
            }
            second = {
                "ts": "2026-08-01T00:00:02+00:00",
                "type": "task_status_changed",
                "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
            }

            first_result = notify_event(root, run_id, first)
            second_result = notify_event(root, run_id, second)

            state = feishu_notifications.load_subscription_state(root)

        self.assertTrue(first_result["sent"])
        self.assertTrue(second_result["sent"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(len(state["sent"]), 2)

    def test_direct_agent_reply_is_used_when_status_push_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = _setup(root, notifications_enabled=False)
            event = {
                "event_id": 1,
                "type": "message",
                "data": {"task_id": "task-001", "sender": "main", "target": "feishu", "message": "agent reply"},
            }
            status_event = {
                "event_id": 2,
                "type": "task_status_changed",
                "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
            }

            self.assertEqual(notification_message_for_event(root, run_id, event), "agent reply")
            self.assertEqual(notification_message_for_event(root, run_id, status_event), "")

    def test_service_confirmation_reply_sends_interactive_card(self) -> None:
        card = {
            "schema": "2.0",
            "body": {
                "elements": [
                    {
                        "tag": "button",
                        "behaviors": [{"type": "callback", "value": {"decision": "confirm"}}],
                    }
                ]
            },
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-card"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root, notifications_enabled=False)
            set_subscription(
                root,
                "tenant:p2p:user",
                chat_id="oc-chat",
                open_id="ou-user",
                run_id=run_id,
                task_id="task-001",
            )
            event = {
                "event_id": 11,
                "type": "message",
                "data": {
                    "task_id": "task-001",
                    "sender": "main",
                    "target": "feishu",
                    "message": "请确认操作",
                    "feishu_card": card,
                    "feishu_confirmation_id": "confirmation-1",
                },
            }

            with mock.patch("aha_cli.services.feishu_notifications.bind_confirmation_card") as bind:
                result = notify_event(root, run_id, event)

        self.assertTrue(result["sent"])
        send.assert_called_once_with(root, "oc-chat", "请确认操作", card=card)
        bind.assert_called_once_with(
            root,
            "confirmation-1",
            message_id="om-card",
            chat_id="oc-chat",
        )

    def test_target_task_reply_automatically_closes_service_assistant_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-closed"},
        ) as send:
            root = Path(tmp)
            _setup(root, notifications_enabled=True)
            set_subscription(
                root,
                "tenant:p2p:user",
                chat_id="oc-origin",
                open_id="ou-user",
                run_id="run-assistant",
                task_id="task-assistant",
            )
            register_service_handoff(
                root,
                assistant_run_id="run-assistant",
                assistant_task_id="task-assistant",
                session_key="tenant:p2p:user",
                chat_id="oc-origin",
                open_id="ou-user",
                target_run_id="run-a",
                target_task_id="task-001",
                request_message="请调研卡片置灰",
            )
            event = {
                "event_id": 20,
                "type": "message",
                "data": {
                    "task_id": "task-001",
                    "sender": "main",
                    "target": "feishu-assistant",
                    "message": "调研完成：飞书支持更新原卡片。",
                },
            }

            result = notify_event(root, "run-a", event)
            status_result = notify_event(
                root,
                "run-a",
                {
                    "event_id": 21,
                    "type": "task_status_changed",
                    "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
                },
            )
            stored = json.loads(service_handoffs_path(root).read_text(encoding="utf-8"))["handoffs"]

        self.assertEqual(result["reason"], "service_handoff_closed")
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], "oc-origin")
        self.assertIn("AHA 跟进已完成", send.call_args.args[2])
        self.assertIn("调研完成", send.call_args.args[2])
        self.assertEqual(next(iter(stored.values()))["status"], "delivered")
        self.assertFalse(status_result["sent"])
        self.assertEqual(send.call_count, 1)

    def test_status_push_is_run_wide_not_limited_to_assistant_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-1"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root)
            set_subscription(
                root,
                "tenant:p2p:user",
                chat_id="oc-chat",
                open_id="ou-user",
                run_id=run_id,
                task_id="task-assistant",
            )
            event = {
                "event_id": 4,
                "type": "task_status_changed",
                "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
            }

            result = notify_event(root, run_id, event)

        self.assertTrue(result["sent"])
        send.assert_called_once()
        self.assertIn("run-a task-001", send.call_args.args[2])

    def test_status_push_reaches_subscriber_from_another_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-1"},
        ) as send:
            root = Path(tmp)
            _setup(root)
            set_subscription(
                root,
                "tenant:p2p:user",
                chat_id="oc-chat",
                open_id="ou-user",
                run_id="run-a",
                task_id="task-assistant",
            )
            event = {
                "event_id": 5,
                "type": "task_status_changed",
                "data": {"task_id": "task-002", "previous_status": "running", "status": "awaiting_user"},
            }

            result = notify_event(root, "run-b", event)

        self.assertTrue(result["sent"])
        send.assert_called_once()
        self.assertIn("run-b task-002", send.call_args.args[2])

    def test_global_status_push_deduplicates_same_chat_across_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-1"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root)
            set_subscription(
                root,
                "tenant:p2p:user:first",
                chat_id="oc-chat",
                open_id="ou-user",
                run_id="run-a",
                task_id="task-001",
            )
            set_subscription(
                root,
                "tenant:p2p:user:second",
                chat_id="oc-chat",
                open_id="ou-user",
                run_id="run-b",
                task_id="task-002",
            )
            event = {
                "event_id": 6,
                "type": "task_status_changed",
                "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
            }

            result = notify_event(root, run_id, event)

        self.assertEqual(result["sent_count"], 1)
        send.assert_called_once()

    def test_agent_reply_waits_for_status_summary_when_push_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = _setup(root)
            event = {
                "event_id": 1,
                "type": "message",
                "data": {"task_id": "task-001", "sender": "main", "target": "feishu", "message": "agent reply"},
            }
            self.assertEqual(notification_message_for_event(root, run_id, event), "")


if __name__ == "__main__":
    unittest.main()
