from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import queue
import tempfile
import unittest
from unittest import mock

from aha_cli.services import feishu_notifications
from aha_cli.services.channel_notifications import wait_for_notification_queue
from aha_cli.services.feishu_notifications import load_subscription_state, notification_message_for_event, notify_event, set_subscription
from aha_cli.locking import exclusive_lock
from aha_cli.services.service_assistant_handoffs import register_service_handoff, service_handoffs_path
from aha_cli.services.feishu_group_handoffs import feishu_group_handoffs_path, register_group_handoff
from aha_cli.store.filesystem import append_event_to_file
from aha_cli.store.io import append_jsonl
from aha_cli.store.paths import event_path, plan_path


def _setup(
    root: Path,
    *,
    notifications_enabled: bool = True,
    app_id: str = "tenant",
    owner_open_id: str = "ou-user",
) -> str:
    run_id = "run-a"
    (root / "config.json").write_text(
        json.dumps(
            {
                "integrations": {
                    "feishu": {
                        "enabled": True,
                        "app_id": app_id,
                        "owner_open_id": owner_open_id,
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


def _set_subscription_in_process(root: str, messages) -> None:
    messages.put("started")
    set_subscription(
        Path(root),
        "tenant:p2p:process-user",
        chat_id="oc-process",
        open_id="ou-process",
        run_id="run-a",
        task_id="task-001",
    )
    messages.put("done")


class FeishuNotificationTests(unittest.TestCase):
    def test_subscription_mutation_waits_for_cross_process_lock(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = feishu_notifications.subscription_state_lock_path(root)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            messages = context.Queue()
            with lock_path.open("a+b") as handle, exclusive_lock(handle):
                process = context.Process(target=_set_subscription_in_process, args=(str(root), messages))
                process.start()
                self.assertEqual(messages.get(timeout=2), "started")
                with self.assertRaises(queue.Empty):
                    messages.get(timeout=0.2)
            self.assertEqual(messages.get(timeout=2), "done")
            process.join(timeout=2)
            state = feishu_notifications.load_subscription_state(root)

        self.assertEqual(process.exitcode, 0)
        self.assertIn("tenant:p2p:process-user", state["subscriptions"])

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
                "ts": "2026-08-05T15:48:41+00:00",
                "type": "task_status_changed",
                "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
            }
            event["event_id"] = append_jsonl(event_path(root, run_id), event)

            message = notification_message_for_event(root, run_id, event)

        self.assertEqual(
            message,
            "Time: 2026-08-05 15:48:41+00:00\nTask: Run A.task-001\nTask Title: Task 1\nStatus: busy -> awaiting\nMessage: 最后一条 agent 回复",
        )

    def test_status_change_is_sent_as_read_only_card(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(
                "aha_cli.services.feishu_notifications._send",
                return_value={"message_id": "om-status"},
            ) as send,
            mock.patch("aha_cli.services.feishu_notifications.audit_feishu_channel") as audit,
        ):
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
            event = {
                "event_id": 3,
                "ts": "2026-08-05T15:48:41+00:00",
                "type": "task_status_changed",
                "data": {
                    "task_id": "task-001",
                    "previous_status": "running",
                    "status": "awaiting_user",
                    "reason": "等待用户确认",
                },
            }

            result = notify_event(root, run_id, event)

        self.assertTrue(result["sent"])
        send.assert_called_once()
        self.assertIn("Status: busy -> awaiting", send.call_args.args[2])
        card = send.call_args.kwargs["card"]
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["template"], "orange")
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("AHA Task 状态更新", rendered)
        self.assertIn("Run A.task-001", rendered)
        self.assertIn("等待用户确认", rendered)
        self.assertNotIn('"tag": "button"', rendered)
        self.assertEqual(audit.call_args.kwargs["kind"], "status_card")

    def test_status_card_header_color_follows_status(self) -> None:
        message = "Time: -\nTask: Run A.task-001\nTask Title: Task 1\nStatus: pending -> busy\nMessage: -"
        expected_templates = {
            "running": "blue",
            "awaiting_user": "orange",
            "completed": "green",
            "failed": "red",
            "blocked": "red",
            "pending": "grey",
        }
        for status, expected in expected_templates.items():
            with self.subTest(status=status):
                card = feishu_notifications._status_notification_card(
                    message,
                    {"data": {"status": status}},
                )
                self.assertEqual(card["header"]["template"], expected)

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

        self.assertIn("Message: 真实最终回复", message)
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

        self.assertIn("Status: awaiting -> busy", message)
        self.assertIn("Message: -", message)

    def test_task_chat_forwards_replies_uses_control_card_and_keeps_other_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-task-chat"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root, notifications_enabled=True)
            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            config["integrations"]["feishu"]["owner_chat_id"] = "oc-user"
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            set_subscription(
                root,
                "tenant:p2p:ou-user",
                chat_id="oc-user",
                open_id="ou-user",
                run_id=run_id,
                task_id="task-001",
                mode="task_chat",
            )

            result = notify_event(
                root,
                run_id,
                {
                    "event_id": 20,
                    "type": "message",
                    "data": {"task_id": "task-001", "sender": "main", "target": "browser", "message": "task chat reply"},
                },
            )
            send.assert_called_once_with(root, "oc-user", "task chat reply", card=None)
            self.assertTrue(result["sent"])

            send.reset_mock()
            echo = notify_event(
                root,
                run_id,
                {
                    "event_id": 21,
                    "type": "message",
                    "data": {"task_id": "task-001", "sender": "feishu", "target": "main", "message": "owner input"},
                },
            )
            self.assertEqual(echo["reason"], "ignored_event")
            send.assert_not_called()

            for index, event_type in enumerate(("agent_command_started", "backend_started", "agent_usage"), start=22):
                result = notify_event(
                    root,
                    run_id,
                    {"event_id": index, "type": event_type, "data": {"task_id": "task-001", "message": event_type}},
                )
                self.assertEqual(result["reason"], "ignored_event")
            send.assert_not_called()

            status_result = notify_event(
                root,
                run_id,
                {
                    "event_id": 30,
                    "type": "task_status_changed",
                    "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
                },
            )
            self.assertEqual(status_result["reason"], "sent")
            control_card = send.call_args.kwargs["card"]
            self.assertEqual(control_card["header"]["title"]["content"], "Task Chat 等待操作")
            self.assertEqual(
                control_card["body"]["elements"][1]["columns"][1]["elements"][0]["behaviors"][0]["value"],
                {"kind": "aha_task_chat_control", "choice_id": "exit"},
            )
            send.reset_mock()

            other_status = notify_event(
                root,
                run_id,
                {
                    "event_id": 31,
                    "type": "task_status_changed",
                    "data": {"task_id": "task-002", "previous_status": "running", "status": "awaiting_user"},
                },
            )
            self.assertTrue(other_status["sent"])
            self.assertIn("task-002", send.call_args.args[2])

    def test_task_chat_forwards_all_visible_chat_events_without_merging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-task-chat"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root, notifications_enabled=True)
            set_subscription(
                root,
                "tenant:p2p:ou-user",
                chat_id="oc-user",
                open_id="ou-user",
                run_id=run_id,
                task_id="task-001",
                mode="task_chat",
            )

            first = notify_event(
                root,
                run_id,
                {
                    "event_id": 50,
                    "type": "agent_message",
                    "data": {"task_id": "task-001", "target": "main", "text": "first update"},
                },
            )
            second = notify_event(
                root,
                run_id,
                {
                    "event_id": 51,
                    "type": "agent_message",
                    "data": {"task_id": "task-001", "target": "main", "text": "second update"},
                },
            )
            error = notify_event(
                root,
                run_id,
                {
                    "event_id": 52,
                    "type": "agent_error",
                    "data": {"task_id": "task-001", "target": "sub-001", "message": "worker failed"},
                },
            )

        self.assertEqual([first["reason"], second["reason"], error["reason"]], ["sent", "sent", "sent"])
        self.assertEqual(
            [call.args[2] for call in send.call_args_list],
            ["first update", "second update", "Agent error (sub-001)\nworker failed"],
        )

    def test_private_subscription_receives_hard_redacted_agent_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-private-error"},
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
                chat_type="p2p",
            )
            result = notify_event(
                root,
                run_id,
                {
                    "event_id": 60,
                    "type": "agent_error",
                    "data": {
                        "task_id": "task-001",
                        "target": "main",
                        "message": "Your access token could not be refreshed because Authorization: Bearer sk-secret was revoked",
                    },
                },
            )

        self.assertEqual(result["reason"], "sent")
        sent_text = send.call_args.args[2]
        self.assertIn("access token could not be refreshed", sent_text)
        self.assertNotIn("Bearer sk-secret", sent_text)

    def test_group_agent_error_sends_redacted_notice_to_originating_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-group-error-notice"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root)
            # The group digital-human turn carried feishu_chat_id in the event stream.
            append_jsonl(
                event_path(root, run_id),
                {
                    "event_id": 80,
                    "type": "message",
                    "data": {
                        "task_id": "task-001",
                        "sender": "feishu",
                        "target": "main",
                        "feishu_channel": "group_digital_human",
                        "feishu_chat_id": "oc-group-chat",
                        "message": "飞书群聊 @ 数字人请求",
                    },
                },
            )
            result = notify_event(
                root,
                run_id,
                {
                    "event_id": 81,
                    "type": "agent_error",
                    "data": {
                        "task_id": "task-001",
                        "target": "main",
                        "message": "Reconnecting... 401 Unauthorized: Missing bearer, url: https://internal.gateway/v1",
                    },
                },
            )

        self.assertEqual(result["reason"], "group_agent_error")
        self.assertEqual(send.call_args.args[1], "oc-group-chat")
        sent_text = send.call_args.args[2]
        self.assertNotIn("https://internal.gateway", sent_text)
        self.assertNotIn("Bearer", sent_text)
        self.assertIn("执行失败", sent_text)

    def test_group_agent_error_without_prior_group_chat_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-noop"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root)
            result = notify_event(
                root,
                run_id,
                {
                    "event_id": 82,
                    "type": "agent_error",
                    "data": {"task_id": "task-001", "target": "main", "message": "some error"},
                },
            )

        self.assertFalse(result["sent"])
        send.assert_not_called()

    def test_group_subscription_receives_generic_agent_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-group-error"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root)
            set_subscription(
                root,
                "tenant:group:chat",
                chat_id="oc-group",
                open_id="ou-user",
                run_id=run_id,
                task_id="task-001",
                chat_type="group",
            )
            result = notify_event(
                root,
                run_id,
                {
                    "event_id": 61,
                    "type": "agent_error",
                    "data": {
                        "task_id": "task-001",
                        "target": "main",
                        "message": "POST https://internal.gateway/v1 failed with Authorization: Bearer sk-secret (status 500)",
                    },
                },
            )

        self.assertEqual(result["reason"], "sent")
        sent_text = send.call_args.args[2]
        self.assertNotIn("https://internal.gateway", sent_text)
        self.assertNotIn("sk-secret", sent_text)
        self.assertIn("执行失败", sent_text)

    def test_failed_status_card_includes_recent_agent_error_in_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-failed-card"},
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
            append_jsonl(
                event_path(root, run_id),
                {
                    "event_id": 70,
                    "type": "agent_error",
                    "data": {
                        "task_id": "task-001",
                        "target": "main",
                        "message": "Your refresh token was revoked; Authorization: Bearer sk-secret is stale",
                    },
                },
            )
            event = {
                "event_id": 71,
                "type": "task_status_changed",
                "data": {"task_id": "task-001", "previous_status": "running", "status": "failed", "exit_code": 1},
            }
            append_jsonl(event_path(root, run_id), event)
            message = notification_message_for_event(root, run_id, event)
            result = notify_event(root, run_id, event)

        self.assertEqual(result["reason"], "sent")
        self.assertIn("refresh token was revoked", message)
        self.assertNotIn("Bearer sk-secret", message)

    def test_backend_event_file_agent_update_enters_task_chat_notification_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-backend-update"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root, notifications_enabled=True)
            set_subscription(
                root,
                "tenant:p2p:ou-user",
                chat_id="oc-user",
                open_id="ou-user",
                run_id=run_id,
                task_id="task-001",
                mode="task_chat",
            )

            append_event_to_file(
                event_path(root, run_id),
                run_id,
                "agent_message",
                {"task_id": "task-001", "target": "main", "text": "backend update"},
            )
            self.assertTrue(wait_for_notification_queue(timeout_seconds=2))

        send.assert_called_once_with(root, "oc-user", "backend update", card=None)

    def test_task_chat_matches_web_chat_visibility_and_deduplicates_final_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-task-chat"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root, notifications_enabled=True)
            set_subscription(
                root,
                "tenant:p2p:ou-user",
                chat_id="oc-user",
                open_id="ou-user",
                run_id=run_id,
                task_id="task-001",
                mode="task_chat",
            )

            update = notify_event(
                root,
                run_id,
                {
                    "event_id": 60,
                    "type": "agent_message",
                    "data": {"task_id": "task-001", "target": "main", "text": "final reply"},
                },
            )
            mirror = notify_event(
                root,
                run_id,
                {
                    "event_id": 1061,
                    "type": "message",
                    "data": {
                        "task_id": "task-001",
                        "sender": "main",
                        "target": "browser",
                        "message": "final reply",
                        "source_turn_identity": "42:final-reply",
                    },
                },
            )
            action_envelope = notify_event(
                root,
                run_id,
                {
                    "event_id": 62,
                    "type": "agent_message",
                    "data": {
                        "task_id": "task-001",
                        "target": "main",
                        "text": json.dumps({"actions": [], "response": "hidden"}),
                    },
                },
            )
            private_sub_update = notify_event(
                root,
                run_id,
                {
                    "event_id": 63,
                    "type": "agent_message",
                    "data": {"task_id": "task-001", "target": "sub-001", "text": "private"},
                },
            )

        self.assertEqual(update["reason"], "sent")
        self.assertEqual(mirror["reason"], "deduplicated")
        self.assertEqual(mirror["deduplicated_count"], 1)
        self.assertEqual(action_envelope["reason"], "ignored_event")
        self.assertEqual(private_sub_update["reason"], "ignored_event")
        send.assert_called_once_with(root, "oc-user", "final reply", card=None)

    def test_task_chat_turn_mirror_does_not_match_expired_update(self) -> None:
        subscription = {
            "task_chat_pending_mirrors": [
                {
                    "agent": "main",
                    "text": "same reply",
                    "event_id": 60,
                    "recorded_at": "2026-08-06T00:00:00+00:00",
                }
            ]
        }
        event = {
            "event_id": 1061,
            "type": "message",
            "data": {
                "sender": "main",
                "message": "same reply",
                "source_turn_identity": "43:same-reply",
            },
        }

        with mock.patch("aha_cli.services.feishu_notifications.utc_now", return_value="2026-08-06T00:03:00+00:00"):
            matched = feishu_notifications._consume_task_chat_mirror(subscription, event)

        self.assertFalse(matched)
        self.assertEqual(subscription["task_chat_pending_mirrors"], [])

    def test_task_chat_control_card_is_invalidated_by_running_and_recreated_afterwards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-control"},
        ) as send, mock.patch("aha_cli.services.feishu_notifications._update_card") as update:
            root = Path(tmp)
            run_id = _setup(root, notifications_enabled=True)
            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            config["integrations"]["feishu"]["owner_chat_id"] = "oc-user"
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            set_subscription(
                root,
                "tenant:p2p:ou-user",
                chat_id="oc-user",
                open_id="ou-user",
                run_id=run_id,
                task_id="task-001",
                mode="task_chat",
            )
            transitions = [
                ("running", "awaiting_user"),
                ("awaiting_user", "running"),
                ("running", "completed"),
            ]
            results = [
                notify_event(
                    root,
                    run_id,
                    {
                        "event_id": 40 + index,
                        "type": "task_status_changed",
                        "data": {"task_id": "task-001", "previous_status": previous, "status": status},
                    },
                )
                for index, (previous, status) in enumerate(transitions)
            ]

        self.assertEqual([result["reason"] for result in results], ["sent", "updated", "sent"])
        self.assertEqual(send.call_count, 2)
        update.assert_called_once()
        self.assertEqual(update.call_args.args[1], "om-control")
        self.assertIn("Task 正在处理中", json.dumps(update.call_args.args[2], ensure_ascii=False))

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

        self.assertIn("Status: awaiting -> busy", message)
        self.assertIn("Message: 请继续修复 这个问题", message)

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
            self.assertIn("Message: 请调研卡片置灰", notification_message_for_event(root, run_id, busy))

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

        self.assertIn("Message: 调研完成，方案可行", message)
        self.assertNotIn("Message: -", message)

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

        self.assertIn("Status: pending -> failed", message)
        self.assertIn("Message: backend launch failed", message)

    def test_system_managed_run_status_change_is_not_pushed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-1"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root)
            plan_path(root, run_id).write_text(
                json.dumps(
                    {
                        "goal": "Feishu Group",
                        "kind": "system",
                        "system_managed": True,
                        "system_purpose": "feishu_group",
                        "tasks": [{"id": "task-001", "title": "Digital human"}],
                    }
                ),
                encoding="utf-8",
            )
            set_subscription(
                root,
                "tenant:p2p:user",
                chat_id="oc-chat",
                open_id="ou-user",
                run_id=run_id,
                task_id="task-001",
            )
            event = {
                "event_id": 31,
                "type": "task_status_changed",
                "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
            }

            message = notification_message_for_event(root, run_id, event)
            result = notify_event(root, run_id, event)

        self.assertEqual(message, "")
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "ignored_event")
        send.assert_not_called()

    def test_system_managed_task_status_change_is_not_pushed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-1"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root)
            plan_path(root, run_id).write_text(
                json.dumps(
                    {
                        "goal": "Run A",
                        "tasks": [
                            {
                                "id": "task-001",
                                "title": "Internal task",
                                "kind": "internal_system",
                                "system_managed": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            set_subscription(
                root,
                "tenant:p2p:user",
                chat_id="oc-chat",
                open_id="ou-user",
                run_id=run_id,
                task_id="task-001",
            )
            event = {
                "event_id": 32,
                "type": "task_status_changed",
                "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
            }

            message = notification_message_for_event(root, run_id, event)
            result = notify_event(root, run_id, event)

        self.assertEqual(message, "")
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "ignored_event")
        send.assert_not_called()

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

    def test_direct_feishu_metadata_reply_targets_original_group_message_and_mentions_sender(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-direct"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root, notifications_enabled=True)
            event = {
                "event_id": 10,
                "type": "message",
                "data": {
                    "task_id": "task-001",
                    "sender": "main",
                    "target": "feishu",
                    "message": "可以公开回答",
                    "feishu_chat_id": "oc-group",
                    "feishu_chat_type": "group",
                    "feishu_reply_to": "om-question",
                    "feishu_mention_open_id": "ou-user",
                },
            }

            result = notify_event(root, run_id, event)

        self.assertTrue(result["sent"])
        send.assert_called_once_with(
            root,
            "oc-group",
            '<at user_id="ou-user"></at> 可以公开回答',
            card=None,
            opts={"reply_to": "om-question"},
        )

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

    def test_steward_reply_to_digital_human_does_not_auto_reply_to_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-group-closed"},
        ) as send:
            root = Path(tmp)
            _setup(root, notifications_enabled=True)
            register_group_handoff(
                root,
                digital_run_id="run-digital",
                digital_task_id="task-digital",
                digital_session_key="tenant:feishu-group-user:ou-user",
                group_chat_id="oc-origin",
                group_message_id="om-question",
                open_id="ou-user",
                owner_open_id="ou-owner",
                owner_chat_id="oc-owner",
                steward_run_id="run-steward",
                steward_task_id="task-steward",
                request_message="请帮我安排发布",
            )
            event = {
                "event_id": 22,
                "type": "message",
                "data": {
                    "task_id": "task-steward",
                    "sender": "main",
                    "target": "feishu-digital-human",
                    "message": "已收到，稍后同步发布安排。",
                },
            }

            result = notify_event(root, "run-steward", event)
            stored = json.loads(feishu_group_handoffs_path(root).read_text(encoding="utf-8"))["handoffs"]

        self.assertEqual(result["reason"], "ignored_event")
        send.assert_not_called()
        self.assertEqual(next(iter(stored.values()))["status"], "pending")

    def test_owner_status_push_is_run_wide_not_limited_to_assistant_task(self) -> None:
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
        self.assertIn("Task: Run A.task-001", send.call_args.args[2])

    def test_owner_status_push_reaches_subscriber_from_another_run(self) -> None:
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
        self.assertIn("Task: run-b.task-002", send.call_args.args[2])

    def test_status_push_ignores_subscriptions_from_other_feishu_app(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-1"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root, app_id="cli-current", owner_open_id="ou-current")
            set_subscription(
                root,
                "cli-old:p2p:user",
                chat_id="oc-old",
                open_id="ou-old",
                run_id=run_id,
                task_id="task-001",
            )
            set_subscription(
                root,
                "cli-current:p2p:user",
                chat_id="oc-current",
                open_id="ou-current",
                run_id=run_id,
                task_id="task-001",
            )
            event = {
                "event_id": 51,
                "type": "task_status_changed",
                "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
            }

            result = notify_event(root, run_id, event)

        self.assertTrue(result["sent"])
        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["skipped_tenant_count"], 1)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], "oc-current")

    def test_status_push_skips_group_subscriptions_and_sends_only_to_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.feishu_notifications._send",
            return_value={"message_id": "om-owner"},
        ) as send:
            root = Path(tmp)
            run_id = _setup(root, app_id="cli-current", owner_open_id="ou-owner")
            set_subscription(
                root,
                "cli-current:group:oc-group",
                chat_id="oc-group",
                open_id="ou-member",
                run_id=run_id,
                task_id="task-group",
                chat_type="group",
            )
            set_subscription(
                root,
                "cli-current:p2p:ou-other",
                chat_id="oc-other",
                open_id="ou-other",
                run_id=run_id,
                task_id="task-other",
            )
            set_subscription(
                root,
                "cli-current:p2p:ou-owner",
                chat_id="oc-owner",
                open_id="ou-owner",
                run_id=run_id,
                task_id="task-owner",
            )
            event = {
                "event_id": 52,
                "type": "task_status_changed",
                "data": {"task_id": "task-001", "previous_status": "running", "status": "awaiting_user"},
            }

            result = notify_event(root, run_id, event)

        self.assertTrue(result["sent"])
        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["skipped_group_count"], 1)
        self.assertEqual(result["skipped_owner_count"], 1)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], "oc-owner")

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
