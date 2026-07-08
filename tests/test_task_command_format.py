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
    format_aha_kb_command,
    format_aha_command,
    format_task_journal_for_prompt,
)


class TaskCommandFormatTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_format_aha_supported_and_removed_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Format command", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                help_text = format_aha_command(root, run_id, "task-001", "/aha")
                status_text = format_aha_command(root, run_id, "task-001", "/aha status")
                agents_text = format_aha_command(root, run_id, "task-001", "/aha agents")
                checkpoint_text = format_aha_command(root, run_id, "task-001", "/aha checkpoint done")
                phase_text = format_aha_command(root, run_id, "task-001", "/aha phase implement")
                session_text = format_aha_command(root, run_id, "task-001", "/aha session compact-reset")
                finalize_text = format_aha_command(root, run_id, "task-001", "/aha finalize")
                complete_text = format_aha_command(root, run_id, "task-001", "/aha complete")
                map_text = format_aha_command(root, run_id, "task-001", "/aha map status")
                done_text = format_aha_command(root, run_id, "task-001", "/aha done")
                unknown_text = format_aha_command(root, run_id, "task-001", "/aha missing")

        self.assertNotIn("/aha final", help_text)
        self.assertIn("/aha kb <message>", help_text)
        self.assertNotIn("/aha nav <message>", help_text)
        self.assertNotIn("/aha map", help_text)
        self.assertIn("/aha complete", help_text)
        self.assertIn("/aha reopen", help_text)
        self.assertIn("/aha interrupt", help_text)
        self.assertIn("/agent <command>", help_text)
        self.assertNotIn("/aha checkpoint", help_text)
        self.assertNotIn("/aha status", help_text)
        self.assertNotIn("/aha agents", help_text)
        self.assertNotIn("/aha done", help_text)
        self.assertNotIn("/aha finalize", help_text)
        self.assertIn("mark the task completed", complete_text)
        self.assertIn("Unsupported AHA command", map_text)
        for text in (status_text, agents_text, checkpoint_text, phase_text, session_text, finalize_text, done_text, unknown_text):
            self.assertIn("Unsupported AHA command", text)

    def test_format_aha_kb_command_forwards_minimal_knowledge_prompt(self) -> None:
        handled, forwarded, reply = format_aha_kb_command("/aha kb 将刚才整理的蓝牙配网流程输出到知识库")
        empty_handled, empty_forwarded, empty_reply = format_aha_kb_command("/aha kb")

        self.assertFalse(handled)
        self.assertIsNotNone(forwarded)
        self.assertIsNone(reply)
        self.assertIn("AHA knowledge-base feedback request.", forwarded or "")
        self.assertIn("Use only your existing backend session context", forwarded or "")
        self.assertIn("Do not generate project navigation entries here", forwarded or "")
        self.assertIn("Write pending knowledge-base candidates directly", forwarded or "")
        self.assertIn("kb add --pending", forwarded or "")
        self.assertIn("Do not append `<aha_knowledge_candidates>`", forwarded or "")
        self.assertIn("assets/<entry-slug>/<filename>", forwarded or "")
        self.assertIn("Do not invent image paths", forwarded or "")
        self.assertIn("do not merely say AHA should add the image later", forwarded or "")
        self.assertIn("task_memo_assets/", forwarded or "")
        self.assertIn("SVG is supported as a normal image asset", forwarded or "")
        self.assertIn("do not paste raw SVG markup into the article body", forwarded or "")
        self.assertIn("diagram.svg", forwarded or "")
        self.assertNotIn("<aha_knowledge_candidates>[", forwarded or "")
        self.assertNotIn("![alt]", forwarded or "")
        self.assertIn("将刚才整理的蓝牙配网流程输出到知识库", forwarded or "")
        self.assertTrue(empty_handled)
        self.assertIsNone(empty_forwarded)
        self.assertIn("Usage: /aha kb <message>", empty_reply or "")

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
        self.assertIn("AHA finalization request.", prompt)
        self.assertIn("Use your resumed backend session context", prompt)
        self.assertIn("Return concise Markdown only", prompt)
        self.assertNotIn("Final source range:", prompt)
        self.assertNotIn("Task journal (chronological ordered list):", prompt)
        self.assertNotIn("<aha_knowledge_candidates>", prompt)
        self.assertNotIn("完成小修复", prompt)
        self.assertIn("1. (empty)", empty_journal)


if __name__ == "__main__":
    unittest.main()
