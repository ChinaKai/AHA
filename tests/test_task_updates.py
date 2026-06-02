from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.task_updates import handle_record_task_update_action, task_update_round_payload
from aha_cli.store.filesystem import event_path, iter_jsonl_from, list_task_rounds


class TaskUpdateActionTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_task_update_round_payload_accepts_aliases_and_defaults(self) -> None:
        payload = task_update_round_payload(
            {
                "summary": "  done  ",
                "files": ["src/app.py"],
                "checks": ["tests passed"],
            }
        )

        self.assertEqual(
            payload,
            {
                "trigger": "main_turn",
                "summary": "done",
                "changed_files": ["src/app.py"],
                "verification": ["tests passed"],
                "risks": None,
                "agents": ["main"],
            },
        )

    def test_handle_record_task_update_action_records_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Record task update", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                result = handle_record_task_update_action(
                    root,
                    run_id,
                    "task-001",
                    {
                        "summary": "Implemented slice",
                        "changed_files": ["src/aha_cli/services/task_updates.py"],
                        "verification": ["unit tests"],
                        "risks": [],
                    },
                )
                rounds = list_task_rounds(root, run_id, "task-001")

        self.assertEqual(result, {"type": "record_task_update", "round_id": "round-001"})
        self.assertEqual(rounds[0]["summary"], "Implemented slice")
        self.assertEqual(rounds[0]["changed_files"], ["src/aha_cli/services/task_updates.py"])
        self.assertEqual(rounds[0]["verification"], ["unit tests"])

    def test_handle_record_task_update_action_skips_missing_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Skip task update", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                result = handle_record_task_update_action(root, run_id, "task-001", {"summary": " "})
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)

        self.assertIsNone(result)
        self.assertTrue(
            any(
                event["type"] == "action_skipped"
                and event["data"] == {"task_id": "task-001", "type": "record_task_update", "reason": "missing summary"}
                for event in events
            )
        )


if __name__ == "__main__":
    unittest.main()
