from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.web.task_command_format import (
    finalization_prompt,
    format_agent_command,
    format_aha_command,
    format_task_journal_for_prompt,
)


class TaskCommandFormatTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_format_aha_help_status_and_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Format command", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                help_text = format_aha_command(root, run_id, "task-001", "/aha help")
                status_text = format_aha_command(root, run_id, "task-001", "/aha status")
                agents_text = format_aha_command(root, run_id, "task-001", "/aha agents")
                finalize_text = format_aha_command(root, run_id, "task-001", "/aha finalize")
                complete_text = format_aha_command(root, run_id, "task-001", "/aha complete")
                done_text = format_aha_command(root, run_id, "task-001", "/aha done")
                unknown_text = format_aha_command(root, run_id, "task-001", "/aha missing")

        self.assertIn("/aha checkpoint <summary>", help_text)
        self.assertNotIn("/aha complete", help_text)
        self.assertNotIn("/aha done", help_text)
        self.assertNotIn("/aha finalize", help_text)
        self.assertIn("/agent <command>", help_text)
        self.assertIn("Task: task-001 Map the relevant files", status_text)
        self.assertIn("Backend: stub", status_text)
        self.assertIn("- main role=task-main backend=stub", agents_text)
        self.assertIn("Unknown AHA command", finalize_text)
        self.assertIn("Unknown AHA command", complete_text)
        self.assertIn("Unknown AHA command", done_text)
        self.assertIn("Unknown AHA command", unknown_text)

    def test_format_aha_without_task_and_missing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Missing task", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                no_task_text = format_aha_command(root, run_id, None, "/aha status")
                missing_text = format_aha_command(root, run_id, "task-missing", "/aha status")

        self.assertEqual(no_task_text, "No task is selected.")
        self.assertEqual(missing_text, "Task not found: task-missing")

    def test_format_agent_command(self) -> None:
        handled, forwarded, reply = format_agent_command(Path("."), "run", "task-001", "main", "/agent status")
        slash_handled, slash_forwarded, slash_reply = format_agent_command(Path("."), "run", "task-001", "main", "/agent /compact")
        empty_handled, empty_forwarded, empty_reply = format_agent_command(Path("."), "run", "task-001", "main", "/agent")

        self.assertFalse(handled)
        self.assertEqual(forwarded, "/status")
        self.assertIsNone(reply)
        self.assertFalse(slash_handled)
        self.assertEqual(slash_forwarded, "/compact")
        self.assertIsNone(slash_reply)
        self.assertTrue(empty_handled)
        self.assertIsNone(empty_forwarded)
        self.assertIn("Usage: /agent <command>", empty_reply or "")

    def test_task_journal_and_finalization_prompt_formatting(self) -> None:
        journal = format_task_journal_for_prompt(
            [
                {
                    "journal_id": "journal-001",
                    "at": "2026-01-01T00:00:00+00:00",
                    "round_id": "round-001",
                    "trigger": "main_turn",
                    "summary": "完成小修复",
                    "changed_files": ["src/app.py"],
                    "verification": ["unit tests"],
                    "risks": ["manual smoke pending"],
                }
            ]
        )
        prompt = finalization_prompt("task-001", "Final task", [{"round_id": "round-001", "trigger": "main_turn", "summary": "完成小修复"}])
        empty_journal = format_task_journal_for_prompt([])

        self.assertIn("Task journal (chronological ordered list):", journal)
        self.assertIn("1. 完成小修复", journal)
        self.assertIn("journal_id: journal-001", journal)
        self.assertIn("at: 2026-01-01T00:00:00+00:00", journal)
        self.assertIn("files: src/app.py", journal)
        self.assertIn("verification: unit tests", journal)
        self.assertIn("risks: manual smoke pending", journal)
        self.assertIn("Final source range:", prompt)
        self.assertIn("Use the Task journal as the primary source", prompt)
        self.assertIn("Summarize only the Final source range above", prompt)
        self.assertIn("<aha_knowledge_candidates>", prompt)
        self.assertIn('For `kind="solutions"`', prompt)
        self.assertIn('For `kind="wiki"`', prompt)
        self.assertIn("Navigation updates are incremental", prompt)
        self.assertIn("Prefer 0-3 high-quality candidates", prompt)
        self.assertIn("完成小修复", prompt)
        self.assertIn("1. (empty)", empty_journal)


if __name__ == "__main__":
    unittest.main()
