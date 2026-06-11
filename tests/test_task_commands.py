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
    ensure_session,
    event_path,
    inbox_path,
    iter_jsonl_from,
    list_task_rounds,
    read_json,
    run_dir,
    save_session,
    set_agent_status,
    set_task_status,
    status_snapshot,
    task_snapshot,
)
from aha_cli.web.task_commands import format_agent_command, format_aha_command, handle_slash_command


class TaskCommandTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_task_commands_format_status_and_agent_forwarding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aha_root = root / ".aha"
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("--home", str(aha_root), "init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("--home", str(aha_root), "plan", "Command status", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                status_text = format_aha_command(aha_root, run_id, "task-001", "/aha status")
                agent_text = format_aha_command(aha_root, run_id, "task-001", "/aha agents")
                help_text = format_aha_command(aha_root, run_id, "task-001", "/aha help")
                handled, forwarded, reply = format_agent_command(aha_root, run_id, "task-001", "main", "/agent status")
                empty_handled, empty_forwarded, empty_reply = format_agent_command(aha_root, run_id, "task-001", "main", "/agent")

        self.assertIn("Task: task-001", status_text)
        self.assertIn("Backend: stub", status_text)
        self.assertIn("- main role=task-main backend=stub", agent_text)
        self.assertIn("/aha phase <phase> [summary]", help_text)
        self.assertFalse(handled)
        self.assertEqual(forwarded, "/status")
        self.assertIsNone(reply)
        self.assertTrue(empty_handled)
        self.assertIsNone(empty_forwarded)
        self.assertIn("Usage: /agent <command>", empty_reply or "")

    def test_task_commands_checkpoint_and_finalization(self) -> None:
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
                rounds = list_task_rounds(root, run_id, "task-001")
                main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                event_types = [json.loads(line)["type"] for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task = status_snapshot(root, run_id)["tasks"][0]

        self.assertTrue(handled)
        self.assertIsNone(forwarded)
        self.assertIn("Checkpoint recorded", checkpoint["message"]["message"])
        self.assertEqual(rounds[0]["summary"], "完成第一轮")
        self.assertTrue(final_handled)
        self.assertIsNone(final_forwarded)
        self.assertIn("Finalization requested", final_payload["message"]["message"])
        self.assertIn("Task journal", main_messages[-1]["message"])
        self.assertIn("完成第一轮", main_messages[-1]["message"])
        self.assertIn("task_final_requested", event_types)
        self.assertEqual(task["status"], "running")

    def test_task_commands_reopen_and_compact_reset(self) -> None:
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
                with mock.patch(
                    "aha_cli.web.task_command_actions.compact_reset_backend_session",
                    return_value={"old_backend_session_id": "session-1", "summary_path": "summaries/main.md"},
                ) as compact_reset:
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
        compact_reset.assert_called_once_with(root, run_id, "task-001", "main", reason="manual", restart=True)
        self.assertEqual(reset_payload["compact_reset"]["old_backend_session_id"], "session-1")

    def test_task_commands_phase_transition_resets_backend_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aha_root = root / ".aha"
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("--home", str(aha_root), "init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("--home", str(aha_root), "plan", "Phase command", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session = ensure_session(aha_root, run_id, "task-001", "main", "codex")
                session["backend_session_id"] = "phase-session-1"
                save_session(aha_root, session)

                handled, forwarded, payload = handle_slash_command(
                    aha_root,
                    run_id,
                    {"sender": "browser", "target": "main"},
                    "/aha phase implement start implementation",
                    "task-001",
                )
                detail = task_snapshot(aha_root, run_id, "task-001")
                updated_session = next(item for item in detail["sessions"] if item["agent_id"] == "main")
                events, _ = iter_jsonl_from(event_path(aha_root, run_id), 0)
                rounds = list_task_rounds(aha_root, run_id, "task-001")

        self.assertTrue(handled)
        self.assertIsNone(forwarded)
        self.assertEqual(payload["phase_transition"]["phase"], "implement")
        self.assertEqual(payload["phase_transition"]["compact_reset"]["old_backend_session_id"], "phase-session-1")
        self.assertIsNone(updated_session["backend_session_id"])
        self.assertEqual(updated_session["phase"], "implement")
        self.assertEqual(updated_session["compact_summary"]["reason"], "phase:implement")
        self.assertTrue(any(event["type"] == "backend_session_phase_changed" for event in events))
        self.assertEqual(rounds[-1]["trigger"], "phase_transition")
        self.assertEqual(rounds[-1]["summary"], "start implementation")

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
