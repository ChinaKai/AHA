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
    read_json,
    run_dir,
    set_agent_status,
    set_task_status,
    status_snapshot,
)
from aha_cli.web.task_commands import format_agent_command, format_aha_command, handle_slash_command


class TaskCommandTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_task_commands_format_supported_commands_and_agent_forwarding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aha_root = root / ".aha"
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("--home", str(aha_root), "init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("--home", str(aha_root), "plan", "Command status", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                help_text = format_aha_command(aha_root, run_id, "task-001", "/aha")
                status_text = format_aha_command(aha_root, run_id, "task-001", "/aha status")
                agent_text = format_aha_command(aha_root, run_id, "task-001", "/aha agents")
                handled, forwarded, reply = format_agent_command(aha_root, run_id, "task-001", "main", "/agent status")
                empty_handled, empty_forwarded, empty_reply = format_agent_command(aha_root, run_id, "task-001", "main", "/agent")

        self.assertNotIn("/aha final", help_text)
        self.assertIn("/aha kb <message>", help_text)
        self.assertIn("/aha nav <message>", help_text)
        self.assertIn("/aha complete", help_text)
        self.assertIn("/aha reopen", help_text)
        self.assertIn("/aha interrupt", help_text)
        self.assertIn("/agent <command>", help_text)
        self.assertNotIn("/aha status", help_text)
        self.assertIn("Unsupported AHA command", status_text)
        self.assertIn("Unsupported AHA command", agent_text)
        self.assertFalse(handled)
        self.assertEqual(forwarded, "/status")
        self.assertIsNone(reply)
        self.assertTrue(empty_handled)
        self.assertIsNone(empty_forwarded)
        self.assertIn("Usage: /agent <command>", empty_reply or "")

    def test_task_commands_removed_final_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Command final", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                handled, forwarded, checkpoint = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main"},
                    "/aha checkpoint 完成第一轮",
                    "task-001",
                )
                final_handled, final_forwarded, final_payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main"},
                    "/aha final",
                    "task-001",
                )
                main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                event_types = [json.loads(line)["type"] for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task = status_snapshot(root, run_id)["tasks"][0]
                rounds = list_task_rounds(root, run_id, "task-001")

        self.assertTrue(handled)
        self.assertIsNone(forwarded)
        self.assertIn("Unsupported AHA command", checkpoint["message"]["message"])
        self.assertEqual(rounds, [])
        self.assertTrue(final_handled)
        self.assertIsNone(final_forwarded)
        self.assertIn("Unsupported AHA command", final_payload["message"]["message"])
        self.assertEqual(main_messages, [])
        self.assertNotIn("task_final_requested", event_types)
        self.assertEqual(task["status"], "pending")

    def test_task_commands_direct_complete_does_not_request_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Command complete", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with mock.patch("aha_cli.web.task_command_actions.stop_task_backends", return_value=[]) as stop_backends:
                    handled, forwarded, payload = handle_slash_command(
                        root,
                        run_id,
                        {"sender": "browser", "target": "main"},
                        "/aha complete",
                        "task-001",
                    )
                main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                event_types = [json.loads(line)["type"] for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task = status_snapshot(root, run_id)["tasks"][0]

        self.assertTrue(handled)
        self.assertIsNone(forwarded)
        self.assertEqual(payload["completion"]["mode"], "direct")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["agents"][0]["status"], "completed")
        self.assertEqual(main_messages, [])
        self.assertIn("task_completed", event_types)
        self.assertNotIn("task_final_requested", event_types)
        stop_backends.assert_called_once()

    def test_cli_task_complete_marks_task_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "CLI complete", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with mock.patch("aha_cli.web.task_command_actions.stop_task_backends", return_value=[]) as stop_backends:
                    code, output = self.run_cli("task", "complete", run_id, "task-001")
                task = status_snapshot(root, run_id)["tasks"][0]
                main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)

        self.assertEqual(code, 0)
        self.assertIn("completed.", output)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["agents"][0]["status"], "completed")
        self.assertEqual(main_messages, [])
        stop_backends.assert_called_once()

    def test_task_commands_reopen_and_removed_compact_reset_slash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Command reset", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                complete_task(root, run_id, "task-001", 0)

                reopen_handled, _, reopen_payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main"},
                    "/aha reopen",
                    "task-001",
                )
                reset_handled, _, reset_payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main"},
                    "/aha session compact-reset",
                    "task-001",
                )
                task = status_snapshot(root, run_id)["tasks"][0]

        self.assertTrue(reopen_handled)
        self.assertIn("reopened", reopen_payload["message"]["message"])
        self.assertEqual(task["status"], "awaiting_user")
        self.assertTrue(reset_handled)
        self.assertNotIn("compact_reset", reset_payload)
        self.assertIn("Unsupported AHA command", reset_payload["message"]["message"])

    def test_task_commands_interrupt_busy_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Command interrupt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")
                append_message(root, run_id, "main", "in-flight", sender="browser", task_id="task-001", role="main")

                with (
                    mock.patch("aha_cli.web.task_command_actions.backend_status", return_value={"status": "busy", "pid": 1234}),
                    mock.patch("aha_cli.web.task_command_actions.stop_backend", return_value={"status": "stopped", "pid": None, "target": "main"}) as stop_backend,
                ):
                    handled, forwarded, payload = handle_slash_command(
                        root,
                        run_id,
                        {"sender": "browser", "target": "main"},
                        "/aha interrupt",
                        "task-001",
                    )
                task = status_snapshot(root, run_id)["tasks"][0]
                offset = read_json(chat_offset_path(run_dir(root, run_id), "main", "task-001"))

        self.assertTrue(handled)
        self.assertIsNone(forwarded)
        self.assertTrue(payload["interrupt"]["interrupted"])
        stop_backend.assert_called_once()
        self.assertEqual(task["status"], "awaiting_user")
        self.assertEqual(task["agents"][0]["status"], "interrupted")
        self.assertGreater(offset["offset"], 0)


if __name__ == "__main__":
    unittest.main()
