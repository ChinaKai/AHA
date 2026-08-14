from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.agent_lifecycle import record_agent_interrupt
from aha_cli.services.chat_offsets import advance_chat_offset_to_inbox_end, chat_offset_path
from aha_cli.store.filesystem import (
    event_path,
    inbox_path,
    iter_jsonl_from,
    run_dir,
    set_agent_status,
    set_task_status,
    task_snapshot,
)
from tests.helpers import isolated_cli_environment


class AgentLifecycleTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with isolated_cli_environment(), mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def init_run(self, root: Path) -> str:
        with mock.patch("pathlib.Path.cwd", return_value=root):
            self.run_cli("init", "--portable", "--backend", "codex")
            code, plan_output = self.run_cli("plan", "Agent lifecycle", "--agents", "1")
            self.assertEqual(code, 0)
            return plan_output.splitlines()[0].split(": ", 1)[1]

    def test_record_agent_interrupt_advances_offset_and_marks_interrupted(self) -> None:
        # The terminal transition must be atomic: a stopped backend leaves the
        # chat cursor past every message already in the inbox, the agent marked
        # interrupted, and a recovery context for its next turn -- never a killed
        # process with an unadvanced offset (the stale-replay bug class).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)
            set_task_status(root, run_id, "task-001", "running")
            set_agent_status(root, run_id, "task-001", "main", "running")
            inbox = inbox_path(root, run_id, "main")
            inbox.write_text("already-delivered\n", encoding="utf-8")
            inbox_size = inbox.stat().st_size

            agent = record_agent_interrupt(
                root,
                run_id,
                "task-001",
                "main",
                reason="test_reason",
                recovery_context="上一轮 agent 工作异常中断测试",
            )
            offset = json.loads(chat_offset_path(run_dir(root, run_id), "main", "task-001").read_text(encoding="utf-8"))["offset"]
            events, _ = iter_jsonl_from(event_path(root, run_id), 0)
            detail = task_snapshot(root, run_id, "task-001")["task"]

        self.assertEqual(offset, inbox_size)
        self.assertEqual(agent["status"], "interrupted")
        self.assertEqual(agent["recovery_context"], "上一轮 agent 工作异常中断测试")
        self.assertEqual(agent["recovery_context_reason"], "test_reason")
        self.assertEqual(agent["recovery_context_consumed_at"], "")
        self.assertEqual(detail["agents"][0]["status"], "interrupted")
        self.assertTrue(any(event["type"] == "agent_status_changed" for event in events))
        self.assertTrue(any(event["type"] == "agent_runtime_updated" for event in events))

    def test_advance_chat_offset_to_inbox_end_skips_unread(self) -> None:
        # Advancing the cursor past the current inbox end is what stops the next
        # backend start from re-reading messages already delivered before a stop.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.init_run(root)
            inbox = inbox_path(root, run_id, "main")
            inbox.write_text("already-delivered\n", encoding="utf-8")
            inbox_size = inbox.stat().st_size

            advance_chat_offset_to_inbox_end(root, run_id, "main", "task-001")

            offset = json.loads(chat_offset_path(run_dir(root, run_id), "main", "task-001").read_text(encoding="utf-8"))["offset"]
        self.assertEqual(offset, inbox_size)


if __name__ == "__main__":
    unittest.main()
