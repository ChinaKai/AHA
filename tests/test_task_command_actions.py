from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main
from aha_cli.services.chat import chat_offset_path
from aha_cli.store.filesystem import (
    complete_task,
    event_path,
    inbox_path,
    iter_jsonl_from,
    list_task_rounds,
    run_dir,
    set_agent_status,
    set_task_status,
    task_snapshot,
    update_task_supervision_config,
)
from aha_cli.web.task_messaging import handle_send_payload, task_host_review_message_blocker
from aha_cli.web.task_command_actions import (
    compact_reset_selected_agent,
    interrupt_selected_agent,
    record_task_checkpoint,
    reopen_selected_task,
    request_task_finalization,
)


class TaskCommandActionTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def init_run(self, root: Path) -> str:
        with mock.patch("pathlib.Path.cwd", return_value=root):
            self.run_cli("init", "--portable", "--backend", "codex")
            code, plan_output = self.run_cli("plan", "Task commands", "--agents", "1")
            self.assertEqual(code, 0)
            return plan_output.splitlines()[0].split(": ", 1)[1]

    def test_compact_reset_selected_agent_returns_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)
            with mock.patch(
                "aha_cli.web.task_command_actions.compact_reset_backend_session",
                return_value={"old_backend_session_id": "old", "summary_path": "summaries/main.md"},
            ) as compact_reset:
                message, payload = compact_reset_selected_agent(root, run_id, "task-001", "main")

        compact_reset.assert_called_once()
        self.assertIn("Compact-reset completed", message)
        self.assertEqual(payload["old_backend_session_id"], "old")

    def test_checkpoint_records_task_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)

            message = record_task_checkpoint(root, run_id, "task-001", "/aha checkpoint first checkpoint")
            rounds = list_task_rounds(root, run_id, "task-001")

        self.assertIn("Checkpoint recorded", message)
        self.assertEqual(rounds[0]["summary"], "first checkpoint")

    def test_final_requests_main_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)

            final_message = request_task_finalization(root, run_id, "task-001", "/aha final")
            main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
            detail = task_snapshot(root, run_id, "task-001")["task"]

        self.assertIn("Finalization requested", final_message)
        self.assertEqual(detail["status"], "running")
        self.assertEqual(main_messages[-1]["result_policy"], "finalize")
        self.assertIn("Generate or update the task Final", main_messages[-1]["message"])

    def test_reopen_selected_task_unlocks_completed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)
            complete_task(root, run_id, "task-001", 0)

            message = reopen_selected_task(root, run_id, "task-001")
            detail = task_snapshot(root, run_id, "task-001")["task"]

        self.assertIn("reopened", message)
        self.assertEqual(detail["status"], "awaiting_user")

    def test_interrupt_selected_agent_stops_busy_backend_and_records_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)
            set_task_status(root, run_id, "task-001", "running")
            set_agent_status(root, run_id, "task-001", "main", "running")
            append_message(root, run_id, "main", "queued", sender="browser", task_id="task-001", role="main")

            with (
                mock.patch("aha_cli.web.task_command_actions.backend_status", return_value={"status": "busy", "pid": 1234}),
                mock.patch("aha_cli.web.task_command_actions.stop_backend", return_value={"status": "stopped", "pid": None}) as stop_backend,
            ):
                message, payload = interrupt_selected_agent(root, run_id, "task-001", "main")
            offset = json.loads(chat_offset_path(run_dir(root, run_id), "main", "task-001").read_text(encoding="utf-8"))["offset"]
            inbox_size = inbox_path(root, run_id, "main").stat().st_size
            detail = task_snapshot(root, run_id, "task-001")["task"]
            events, _ = iter_jsonl_from(event_path(root, run_id), 0)

        self.assertIn("Interrupted main", message)
        self.assertTrue(payload["interrupted"])
        stop_backend.assert_called_once()
        self.assertEqual(offset, inbox_size)
        self.assertEqual(detail["status"], "awaiting_user")
        self.assertEqual(detail["agents"][0]["status"], "interrupted")
        self.assertTrue(any(event["type"] == "agent_interrupted" for event in events))

    def test_interrupt_selected_agent_stops_idle_running_backend_and_records_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)
            set_task_status(root, run_id, "task-001", "running")
            set_agent_status(root, run_id, "task-001", "main", "running")
            append_message(root, run_id, "main", "queued", sender="browser", task_id="task-001", role="main")

            with (
                mock.patch("aha_cli.web.task_command_actions.backend_status", return_value={"status": "running", "pid": 1234}),
                mock.patch("aha_cli.web.task_command_actions.stop_backend", return_value={"status": "stopped", "pid": None}) as stop_backend,
            ):
                message, payload = interrupt_selected_agent(root, run_id, "task-001", "main")
            offset = json.loads(chat_offset_path(run_dir(root, run_id), "main", "task-001").read_text(encoding="utf-8"))["offset"]
            inbox_size = inbox_path(root, run_id, "main").stat().st_size
            detail = task_snapshot(root, run_id, "task-001")["task"]

        self.assertIn("Interrupted main", message)
        self.assertTrue(payload["interrupted"])
        stop_backend.assert_called_once()
        self.assertEqual(offset, inbox_size)
        self.assertEqual(detail["status"], "awaiting_user")
        self.assertEqual(detail["agents"][0]["status"], "interrupted")

    def test_interrupt_selected_agent_recovers_stale_stopped_running_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)
            set_task_status(root, run_id, "task-001", "running")
            set_agent_status(root, run_id, "task-001", "main", "running")

            with (
                mock.patch("aha_cli.web.task_command_actions.backend_status", return_value={"status": "stopped", "pid": None}),
                mock.patch("aha_cli.web.task_command_actions.stop_backend") as stop_backend,
            ):
                message, payload = interrupt_selected_agent(root, run_id, "task-001", "main")
            detail = task_snapshot(root, run_id, "task-001")["task"]
            events, _ = iter_jsonl_from(event_path(root, run_id), 0)

        self.assertIn("Recovered stale stopped backend", message)
        self.assertTrue(payload["interrupted"])
        self.assertEqual(payload["reason"], "stale_recovered")
        stop_backend.assert_not_called()
        self.assertEqual(detail["status"], "awaiting_user")
        self.assertEqual(detail["agents"][0]["status"], "interrupted")
        self.assertTrue(any(event["type"] == "agent_status_recovered" for event in events))

    def test_interrupt_selected_host_clears_main_host_wait_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)
            update_task_supervision_config(
                root,
                run_id,
                "task-001",
                mode="assisted",
                host_backend="codex",
                real_agent_enabled=True,
            )
            set_task_status(root, run_id, "task-001", "running")
            set_agent_status(root, run_id, "task-001", "main", "waiting", waiting_reason="host")
            set_agent_status(root, run_id, "task-001", "host", "running")

            with (
                mock.patch("aha_cli.web.task_command_actions.backend_status", return_value={"status": "running", "pid": 1234}),
                mock.patch("aha_cli.web.task_command_actions.stop_backend", return_value={"status": "stopped", "pid": None}) as stop_backend,
            ):
                _message, payload = interrupt_selected_agent(root, run_id, "task-001", "host")

            detail = task_snapshot(root, run_id, "task-001")["task"]
            main_agent = next(agent for agent in detail["agents"] if agent["id"] == "main")
            host_agent = next(agent for agent in detail["agents"] if agent["id"] == "host")
            blocker = task_host_review_message_blocker(root, run_id, "task-001", "main")

            with (
                mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "stopped"}),
                mock.patch("aha_cli.web.task_messaging.start_backend", return_value={"status": "running"}) as start_backend,
            ):
                followup = handle_send_payload(
                    root,
                    run_id,
                    {
                        "target": "main",
                        "role": "main",
                        "task_id": "task-001",
                        "from_agent": "browser",
                        "to_agent": "main",
                        "sender": "browser",
                        "message": "continue after host interrupt",
                    },
                    command_handler=lambda *_args: (False, None, {}),
                    debug_logger=lambda *_args, **_kwargs: None,
                )

        self.assertTrue(payload["interrupted"])
        stop_backend.assert_called_once()
        self.assertEqual(detail["status"], "awaiting_user")
        self.assertEqual(host_agent["status"], "interrupted")
        self.assertEqual(main_agent["status"], "completed")
        self.assertNotIn("waiting_reason", main_agent)
        self.assertIsNone(blocker)
        self.assertTrue(followup["ok"])
        self.assertFalse(followup.get("deferred", False))
        start_backend.assert_called_once()

    def test_interrupt_stopped_pending_host_clears_main_host_wait_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)
            update_task_supervision_config(
                root,
                run_id,
                "task-001",
                mode="assisted",
                host_backend="codex",
                real_agent_enabled=True,
            )
            set_task_status(root, run_id, "task-001", "running")
            set_agent_status(root, run_id, "task-001", "main", "waiting", waiting_reason="host")
            set_agent_status(root, run_id, "task-001", "host", "pending")
            append_message(root, run_id, "host", "stale host message", sender="main", task_id="task-001", role="host")

            with (
                mock.patch("aha_cli.web.task_command_actions.backend_status", return_value={"status": "stopped", "pid": None}),
                mock.patch(
                    "aha_cli.web.task_command_actions.stop_backend",
                    return_value={"status": "stopped", "pid": None, "already_stopped": True},
                ) as stop_backend,
            ):
                message, payload = interrupt_selected_agent(root, run_id, "task-001", "host")

            offset = json.loads(chat_offset_path(run_dir(root, run_id), "host", "task-001").read_text(encoding="utf-8"))["offset"]
            inbox_size = inbox_path(root, run_id, "host").stat().st_size
            detail = task_snapshot(root, run_id, "task-001")["task"]
            main_agent = next(agent for agent in detail["agents"] if agent["id"] == "main")
            host_agent = next(agent for agent in detail["agents"] if agent["id"] == "host")
            blocker = task_host_review_message_blocker(root, run_id, "task-001", "main")

            with (
                mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "stopped"}),
                mock.patch("aha_cli.web.task_messaging.start_backend", return_value={"status": "running"}) as start_backend,
            ):
                followup = handle_send_payload(
                    root,
                    run_id,
                    {
                        "target": "main",
                        "role": "main",
                        "task_id": "task-001",
                        "from_agent": "browser",
                        "to_agent": "main",
                        "sender": "browser",
                        "message": "continue after stopped host interrupt",
                    },
                    command_handler=lambda *_args: (False, None, {}),
                    debug_logger=lambda *_args, **_kwargs: None,
                )

        self.assertIn("Interrupted host", message)
        self.assertTrue(payload["interrupted"])
        stop_backend.assert_called_once()
        self.assertEqual(offset, inbox_size)
        self.assertEqual(detail["status"], "awaiting_user")
        self.assertEqual(host_agent["status"], "interrupted")
        self.assertEqual(main_agent["status"], "completed")
        self.assertNotIn("waiting_reason", main_agent)
        self.assertIsNone(blocker)
        self.assertTrue(followup["ok"])
        self.assertFalse(followup.get("deferred", False))
        start_backend.assert_called_once()


if __name__ == "__main__":
    unittest.main()
