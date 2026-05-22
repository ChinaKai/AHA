from __future__ import annotations

import asyncio
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.store.filesystem import add_agent, append_event
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
