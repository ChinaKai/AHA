from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs

from aha_cli.cli import append_message, main
from aha_cli.store.filesystem import append_event, event_path, iter_jsonl_from
from aha_cli.web.system_routes import system_route_response
from tests.helpers import json_response_body


class WebSystemRoutesTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_system_routes_return_status_backend_and_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "System status", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with mock.patch("aha_cli.web.status.aha_version", return_value="20260527.057e500"):
                    status = system_route_response(root, run_id, "GET", "/api/status", parse_qs("lite=1&task_id=task-001"))
                backends = system_route_response(root, run_id, "HEAD", "/api/backends", {})
                models = system_route_response(root, run_id, "GET", "/api/models", parse_qs("backend=codex"))
                invalid_models = system_route_response(root, run_id, "GET", "/api/models", parse_qs("backend=nope"))
                with mock.patch("aha_cli.web.system_routes.backend_status", return_value={"status": "running", "pid": 123}):
                    backend = system_route_response(root, run_id, "GET", "/api/backend", parse_qs("target=main&task_id=task-001"))

        self.assertTrue(status and status.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(json_response_body(status)["run_id"], run_id)
        self.assertEqual(json_response_body(status)["aha_version"], "20260527.057e500")
        self.assertTrue(backends and backends.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(backends.split(b"\r\n\r\n", 1)[1], b"")
        self.assertTrue(models and models.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(json_response_body(models)["backend"], "codex")
        self.assertTrue(invalid_models and invalid_models.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertEqual(json_response_body(backend)["pid"], 123)

    def test_system_routes_return_events_and_conversation_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "System events", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "hello", sender="browser", task_id="task-001", role="main")
                append_event(root, run_id, "agent_command_finished", {"task_id": "task-001", "target": "main", "command": "pwd", "exit_code": 0, "output_tail": "large output"})

                events = system_route_response(root, run_id, "GET", "/api/events", parse_qs("offset=0&limit=2"))
                conversation = system_route_response(
                    root,
                    run_id,
                    "GET",
                    "/api/conversation-events",
                    parse_qs("task_id=task-001&target=main&categories=chat,commands&limit=20"),
                )
                missing_task = system_route_response(root, run_id, "GET", "/api/conversation-events", parse_qs("target=main"))

        self.assertTrue(events and events.startswith(b"HTTP/1.1 200 OK"))
        events_body = json_response_body(events)
        self.assertEqual(events_body["limit"], 2)
        self.assertTrue(events_body["has_more"])
        self.assertTrue(conversation and conversation.startswith(b"HTTP/1.1 200 OK"))
        conversation_body = json_response_body(conversation)
        self.assertEqual(conversation_body["categories"], ["chat", "commands"])
        self.assertEqual([event["type"] for event in conversation_body["events"]], ["message", "agent_command_finished"])
        self.assertNotIn("output_tail", conversation_body["events"][-1]["data"])
        self.assertTrue(conversation_body["events"][-1]["data"]["output_tail_omitted"])
        self.assertTrue(missing_task and missing_task.startswith(b"HTTP/1.1 400 Bad Request"))

    def test_system_routes_record_debug_and_schedule_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "System debug", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                debug_body = json.dumps({"run_id": run_id, "seq": 7, "ignored": "private"}).encode("utf-8")
                debug = system_route_response(root, "", "POST", "/api/debug/realtime", {}, debug_body)
                completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="scheduled\n", stderr="")
                with mock.patch("aha_cli.web.system_routes.subprocess.run", return_value=completed) as run_command:
                    restart = system_route_response(
                        root,
                        run_id,
                        "POST",
                        "/api/web/restart",
                        {},
                        json.dumps({"host": "0.0.0.0", "port": 8766}).encode("utf-8"),
                    )
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                log_text = (root / ".aha" / "runs" / run_id / "logs" / "realtime-debug.log").read_text(encoding="utf-8")

        self.assertTrue(debug and debug.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(restart and restart.startswith(b"HTTP/1.1 200 OK"))
        restart_body = json_response_body(restart)
        self.assertEqual(restart_body["service_unit"], "aha-ui-source-8766.service")
        self.assertEqual(run_command.call_args.args[0][0], "systemd-run")
        self.assertIn('"source": "client"', log_text)
        self.assertIn('"seq": 7', log_text)
        self.assertNotIn("ignored", log_text)
        self.assertTrue(any(event["type"] == "web_restart_requested" for event in events))

    def test_system_routes_handle_weixin_console_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Weixin routes", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                status_payload = {"ok": True, "paired": False, "pairing": None}
                pair_payload = {"ok": True, "paired": False, "pairing": {"status": "waiting", "qrcode_svg": "<svg/>"}}
                reset_payload = {"ok": True, "paired": False, "pairing": None, "account": None, "error": ""}
                sent_payload = {"ok": True, "sent": True, "message_id": "msg-1", "target": "user-1@im.wechat"}
                notifications_payload = {"enabled": True, "ready": True, "sent_count": 0, "updated_at": "now", "last_sent_at": ""}
                reset_notifications_payload = {"enabled": False, "ready": False, "sent_count": 0, "updated_at": "now", "last_sent_at": ""}
                with (
                    mock.patch("aha_cli.web.system_routes.weixin_status_snapshot", return_value=status_payload) as status_snapshot,
                    mock.patch("aha_cli.web.system_routes.start_pairing", return_value=pair_payload) as start_pair,
                    mock.patch("aha_cli.web.system_routes.reset_pairing", return_value=reset_payload) as reset_pair,
                    mock.patch("aha_cli.web.system_routes.send_test_notification", return_value=sent_payload) as send_test,
                    mock.patch(
                        "aha_cli.web.system_routes.set_notifications_enabled",
                        side_effect=[reset_notifications_payload, notifications_payload],
                    ) as set_notifications,
                ):
                    status = system_route_response(root, run_id, "GET", "/api/weixin", parse_qs(""))
                    pair = system_route_response(root, run_id, "POST", "/api/weixin/pair", parse_qs(""))
                    reset = system_route_response(root, run_id, "POST", "/api/weixin/reset", parse_qs(""))
                    test = system_route_response(
                        root,
                        run_id,
                        "POST",
                        "/api/weixin/test",
                        parse_qs(""),
                        json.dumps({"message": "hello"}).encode("utf-8"),
                    )
                    notifications = system_route_response(
                        root,
                        run_id,
                        "POST",
                        "/api/weixin/notifications",
                        parse_qs(""),
                        json.dumps({"enabled": True}).encode("utf-8"),
                    )
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)

        self.assertTrue(status and status.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(pair and pair.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(reset and reset.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(test and test.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(notifications and notifications.startswith(b"HTTP/1.1 200 OK"))
        self.assertFalse(json_response_body(status)["paired"])
        self.assertFalse(json_response_body(status)["notifications"]["enabled"])
        self.assertEqual(json_response_body(pair)["pairing"]["status"], "waiting")
        self.assertFalse(json_response_body(reset)["paired"])
        self.assertFalse(json_response_body(reset)["notifications"]["enabled"])
        self.assertEqual(json_response_body(test)["message_id"], "msg-1")
        self.assertTrue(json_response_body(notifications)["notifications"]["enabled"])
        status_snapshot.assert_called_once_with(root, run_id)
        start_pair.assert_called_once_with(root, run_id)
        reset_pair.assert_called_once_with(root, run_id)
        send_test.assert_called_once_with(root, run_id, "hello")
        self.assertEqual(
            set_notifications.call_args_list,
            [mock.call(root, run_id, False), mock.call(root, run_id, True)],
        )
        self.assertTrue(any(event["type"] == "weixin_pairing_reset" for event in events))
        self.assertTrue(any(event["type"] == "weixin_notifications_updated" for event in events))

    def test_weixin_status_fetches_recent_received_messages_when_paired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Weixin received", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                status_payload = {"ok": True, "paired": True, "pairing": None, "received_messages": []}
                updates_payload = {
                    "ok": True,
                    "message_count": 2,
                    "recent_messages": [
                        {"from_user_id": "user-1@im.wechat", "text": "second", "received_at": "2026-05-25T00:00:02+00:00"},
                        {"from_user_id": "user-1@im.wechat", "text": "first", "received_at": "2026-05-25T00:00:01+00:00"},
                    ],
                }
                with (
                    mock.patch("aha_cli.web.system_routes.weixin_status_snapshot", return_value=status_payload),
                    mock.patch("aha_cli.web.system_routes.recent_received_messages", return_value=[]) as recent_messages,
                    mock.patch("aha_cli.web.system_routes.fetch_updates", return_value=updates_payload) as fetch_updates,
                    mock.patch("aha_cli.web.system_routes.notification_status", return_value={"enabled": False}),
                ):
                    status = system_route_response(root, run_id, "GET", "/api/weixin", parse_qs(""))

        self.assertTrue(status and status.startswith(b"HTTP/1.1 200 OK"))
        body = json_response_body(status)
        self.assertEqual(body["received_message_count"], 2)
        self.assertEqual([item["text"] for item in body["received_messages"]], ["second", "first"])
        recent_messages.assert_called_once_with(root)
        fetch_updates.assert_called_once_with(root)
