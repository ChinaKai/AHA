from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.store.event_notifications import EventNotificationListener
from aha_cli.store.filesystem import add_agent, append_event, append_event_to_file
from aha_cli.store.paths import event_path
from aha_cli.websocket import server as ws_server
from aha_cli.websocket.server import handle_ws_client, realtime_debug_log, ws_read_text
from tests.helpers import fetch_initial_ws_messages, fetch_ws_messages


class WebSocketTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_websocket_starts_from_tail_for_large_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Websocket tail", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                for index in range(1000):
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": f"old-ws-event-{index}"})

                messages = asyncio.run(fetch_initial_ws_messages(root, run_id))

        self.assertEqual([message["type"] for message in messages], ["status"])

    def test_websocket_sends_heartbeat_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Websocket heartbeat", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with mock.patch("aha_cli.websocket.server.WS_HEARTBEAT_INTERVAL", 0.01):
                    messages = asyncio.run(fetch_ws_messages(root, run_id, timeout=0.3, max_messages=2))

        self.assertEqual(messages[0]["type"], "status")
        self.assertEqual(messages[1]["type"], "heartbeat")
        self.assertIn("last_event_id", messages[1])

    def test_websocket_realtime_debug_does_not_create_missing_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            realtime_debug_log("ws", _root=root, run_id="missing-run", phase="heartbeat_sent")

            self.assertFalse((root / ".aha" / "runs" / "missing-run").exists())

    def test_append_event_to_file_notifies_event_listener(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-notify"
            events_file = event_path(root, run_id)
            try:
                listener = EventNotificationListener(events_file)
            except OSError as exc:
                self.skipTest(f"event notifications unavailable: {exc}")
            try:

                async def wait_for_notification() -> None:
                    wait_task = asyncio.create_task(listener.wait())
                    await asyncio.sleep(0)
                    append_event_to_file(events_file, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "wake"})
                    await asyncio.wait_for(wait_task, timeout=0.5)

                asyncio.run(wait_for_notification())
            finally:
                listener.close()

    def test_websocket_wakes_from_event_notification_before_poll_fallback(self) -> None:
        async def fetch_notified_event(root: Path, run_id: str) -> tuple[list[dict], int]:
            position_calls = 0
            original_event_stream_position = ws_server.event_stream_position

            def counted_event_stream_position(call_root: Path, call_run_id: str) -> int:
                nonlocal position_calls
                position_calls += 1
                return original_event_stream_position(call_root, call_run_id)

            with mock.patch("aha_cli.websocket.server.event_stream_position", side_effect=counted_event_stream_position):
                server = await asyncio.start_server(
                    lambda reader, writer: handle_ws_client(root, run_id, reader, writer, 10.0),
                    "127.0.0.1",
                    0,
                )
                host, port = server.sockets[0].getsockname()
                writer = None
                try:
                    reader, writer = await asyncio.open_connection(host, port)
                    writer.write(
                        (
                            "GET / HTTP/1.1\r\n"
                            "Host: test\r\n"
                            "Upgrade: websocket\r\n"
                            "Connection: Upgrade\r\n"
                            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                            "Sec-WebSocket-Version: 13\r\n"
                            "\r\n"
                        ).encode("ascii")
                    )
                    await writer.drain()
                    await reader.readuntil(b"\r\n\r\n")
                    status = json.loads(await asyncio.wait_for(ws_read_text(reader), timeout=0.5))
                    for _ in range(100):
                        if position_calls >= 3:
                            break
                        await asyncio.sleep(0.01)
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "notified"})
                    event = json.loads(await asyncio.wait_for(ws_read_text(reader), timeout=0.5))
                    return [status, event], position_calls
                finally:
                    if writer and not writer.is_closing():
                        writer.close()
                        await writer.wait_closed()
                    server.close()
                    await server.wait_closed()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Websocket notify wake", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                messages, position_calls = asyncio.run(fetch_notified_event(root, run_id))

        self.assertEqual([message["type"] for message in messages], ["status", "event"])
        self.assertEqual(messages[1]["data"]["data"]["text"], "notified")
        self.assertLessEqual(position_calls, 4)

    def test_websocket_status_supports_lite_selected_task_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Websocket lite", "--agents", "2")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-002", backend="codex", role="sub")

                messages = asyncio.run(fetch_ws_messages(root, run_id, "/?lite=1&selected_task_id=task-001", max_messages=1))

        self.assertEqual(messages[0]["type"], "status")
        tasks = {task["id"]: task for task in messages[0]["data"]["tasks"]}
        self.assertGreaterEqual(len(tasks["task-001"]["agents"]), 1)
        self.assertEqual(tasks["task-002"]["agent_count"], 2)
        self.assertEqual(tasks["task-002"]["agents"], [])

    def test_websocket_replays_from_last_event_id_after_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Websocket replay", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                baseline = append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "before-disconnect"})
                missed_one = append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "missed-ws-1"})
                missed_two = append_event(root, run_id, "agent_finished", {"task_id": "task-001", "target": "main", "exit_code": 0})

                messages = asyncio.run(
                    fetch_ws_messages(root, run_id, f"/?last_event_id={baseline['event_id']}", max_messages=3)
                )

        self.assertEqual([message["type"] for message in messages], ["status", "event", "event"])
        replayed = [message["data"] for message in messages if message["type"] == "event"]
        self.assertEqual([event["event_id"] for event in replayed], [missed_one["event_id"], missed_two["event_id"]])
        self.assertEqual(replayed[0]["data"]["text"], "missed-ws-1")
        self.assertEqual(replayed[1]["type"], "agent_finished")

    def test_websocket_same_cursor_replays_same_events_to_multiple_clients(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Websocket multi replay", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                baseline = append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "before-clients"})
                expected = [
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "client-replay-1"})[
                        "event_id"
                    ],
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "client-replay-2"})[
                        "event_id"
                    ],
                ]

                async def fetch_both() -> tuple[list[dict], list[dict]]:
                    first_messages, second_messages = await asyncio.gather(
                        fetch_ws_messages(root, run_id, f"/?after_event_id={baseline['event_id']}", max_messages=3),
                        fetch_ws_messages(root, run_id, f"/?after_event_id={baseline['event_id']}", max_messages=3),
                    )
                    return first_messages, second_messages

                first, second = asyncio.run(fetch_both())

        first_events = [message["data"]["event_id"] for message in first if message["type"] == "event"]
        second_events = [message["data"]["event_id"] for message in second if message["type"] == "event"]
        self.assertEqual(first_events, expected)
        self.assertEqual(second_events, expected)
