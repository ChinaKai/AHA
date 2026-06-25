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
    add_agent,
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
    complete_selected_task,
    interrupt_selected_agent,
    record_task_checkpoint,
    reopen_selected_task,
    request_task_finalization,
)
from tests.helpers import isolated_cli_environment


class TaskCommandActionTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with isolated_cli_environment(), mock.patch("sys.stdout", out):
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

    def test_complete_selected_task_marks_complete_without_final_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)
            sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
            set_agent_status(root, run_id, "task-001", sub["id"], "running")
            set_task_status(root, run_id, "task-001", "running")

            with mock.patch("aha_cli.web.task_command_actions.stop_task_backends", return_value=[{"target": sub["id"], "status": "stopped"}]) as stop_backends:
                message, payload = complete_selected_task(root, run_id, "task-001")
            main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
            detail = task_snapshot(root, run_id, "task-001")
            events, _ = iter_jsonl_from(event_path(root, run_id), 0)
            main_agent = next(agent for agent in detail["task"]["agents"] if agent["id"] == "main")
            sub_agent = next(agent for agent in detail["task"]["agents"] if agent["id"] == sub["id"])

        self.assertIn("completed.", message)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "direct")
        self.assertEqual(detail["task"]["status"], "completed")
        self.assertEqual(main_agent["status"], "completed")
        self.assertEqual(sub_agent["status"], "stopped")
        self.assertEqual(detail["result"], "")
        self.assertEqual(main_messages, [])
        self.assertIn("completion_marked_at", detail["task"]["coordination"])
        self.assertTrue(any(event["type"] == "task_completed" for event in events))
        self.assertFalse(any(event["type"] == "task_final_requested" for event in events))
        stop_backends.assert_called_once()

    def test_reopen_selected_task_unlocks_completed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)
            complete_task(root, run_id, "task-001", 0)

            message = reopen_selected_task(root, run_id, "task-001")
            detail = task_snapshot(root, run_id, "task-001")["task"]

        self.assertIn("reopened", message)
        self.assertEqual(detail["status"], "awaiting_user")

    def test_reopen_selected_task_recovers_stale_host(self) -> None:
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
            complete_task(root, run_id, "task-001", 0)
            set_agent_status(root, run_id, "task-001", "main", "waiting", waiting_reason="host")
            set_agent_status(root, run_id, "task-001", "host", "running")

            with mock.patch("aha_cli.web.status.backend_status", return_value={"status": "stopped", "pid": None}):
                message = reopen_selected_task(root, run_id, "task-001")
            detail = task_snapshot(root, run_id, "task-001")["task"]
            main_agent = next(agent for agent in detail["agents"] if agent["id"] == "main")
            host_agent = next(agent for agent in detail["agents"] if agent["id"] == "host")

        self.assertIn("Recovered 1 stale agent", message)
        self.assertEqual(detail["status"], "awaiting_user")
        self.assertEqual(host_agent["status"], "interrupted")
        self.assertEqual(main_agent["status"], "completed")

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

    def test_interrupt_last_waited_sub_agent_requests_main_round_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)
            sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
            set_task_status(root, run_id, "task-001", "running")
            set_agent_status(root, run_id, "task-001", "main", "waiting", waiting_reason="subagents")
            set_agent_status(root, run_id, "task-001", sub["id"], "running")
            append_message(root, run_id, sub["id"], "in-flight", sender="main", task_id="task-001", role="sub")

            with (
                mock.patch("aha_cli.web.task_command_actions.backend_status", return_value={"status": "running", "pid": 1234}),
                mock.patch(
                    "aha_cli.web.task_command_actions.stop_backend",
                    return_value={"status": "stopped", "pid": None, "target": sub["id"]},
                ) as stop_backend,
                mock.patch("aha_cli.services.orchestrator.backend_status", return_value={"status": "stopped"}),
                mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_main,
            ):
                message, payload = interrupt_selected_agent(root, run_id, "task-001", sub["id"])

            detail = task_snapshot(root, run_id, "task-001")["task"]
            main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
            events, _ = iter_jsonl_from(event_path(root, run_id), 0)

        self.assertIn(f"Interrupted {sub['id']}", message)
        self.assertTrue(payload["interrupted"])
        stop_backend.assert_called_once()
        start_main.assert_called_once()
        self.assertEqual(detail["status"], "running")
        self.assertEqual(next(agent for agent in detail["agents"] if agent["id"] == sub["id"])["status"], "interrupted")
        self.assertTrue(any(message.get("coordination") == "subagents_complete" for message in main_messages))
        self.assertTrue(any(event["type"] == "task_round_summary_requested" for event in events))

    def test_interrupt_one_waited_sub_agent_keeps_main_waiting_for_remaining_subs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)
            sub_one = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
            sub_two = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
            set_task_status(root, run_id, "task-001", "running")
            set_agent_status(root, run_id, "task-001", "main", "waiting", waiting_reason="subagents")
            set_agent_status(root, run_id, "task-001", sub_one["id"], "running")
            set_agent_status(root, run_id, "task-001", sub_two["id"], "running")

            with (
                mock.patch("aha_cli.web.task_command_actions.backend_status", return_value={"status": "running", "pid": 1234}),
                mock.patch(
                    "aha_cli.web.task_command_actions.stop_backend",
                    return_value={"status": "stopped", "pid": None, "target": sub_one["id"]},
                ),
                mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_main,
            ):
                _message, payload = interrupt_selected_agent(root, run_id, "task-001", sub_one["id"])

            detail = task_snapshot(root, run_id, "task-001")["task"]
            main_agent = next(agent for agent in detail["agents"] if agent["id"] == "main")
            main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)

        self.assertTrue(payload["interrupted"])
        self.assertEqual(detail["status"], "running")
        self.assertEqual(main_agent["status"], "waiting")
        self.assertEqual(main_agent["waiting_reason"], "subagents")
        self.assertEqual(next(agent for agent in detail["agents"] if agent["id"] == sub_one["id"])["status"], "interrupted")
        self.assertEqual(next(agent for agent in detail["agents"] if agent["id"] == sub_two["id"])["status"], "running")
        self.assertFalse(any(message.get("coordination") == "subagents_complete" for message in main_messages))
        start_main.assert_not_called()

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
