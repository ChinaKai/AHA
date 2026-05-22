from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.services.chat_offsets import (
    chat_offset_path,
    load_chat_offset,
    safe_target_name,
    save_chat_offset,
    worker_backend_should_exit_after_turn,
)
from aha_cli.store.filesystem import append_jsonl, read_json


class ChatOffsetTests(unittest.TestCase):
    def test_chat_offset_path_is_task_scoped_and_safe(self) -> None:
        run = Path("/tmp/run")

        self.assertEqual(safe_target_name("team/main"), "team_main")
        self.assertEqual(chat_offset_path(run, "main"), run / "runtime" / "chat-offset-main.json")
        self.assertEqual(
            chat_offset_path(run, "sub/001", "task/001"),
            run / "runtime" / "chat-offset-task_001-sub_001.json",
        )

    def test_load_chat_offset_recovers_from_stale_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / "inbox.jsonl"
            offset_file = root / "offset.json"
            append_jsonl(inbox, {"message": "one"})
            actual_offset = inbox.stat().st_size
            save_chat_offset(offset_file, actual_offset + 100)

            offset = load_chat_offset(inbox, offset_file, from_start=False)
            from_start = load_chat_offset(inbox, offset_file, from_start=True)

        self.assertEqual(offset, actual_offset)
        self.assertEqual(from_start, 0)

    def test_save_chat_offset_writes_offset_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            offset_file = Path(tmp) / "runtime" / "offset.json"

            save_chat_offset(offset_file, 42)
            payload = read_json(offset_file)

        self.assertEqual(payload["offset"], 42)
        self.assertIn("updated_at", payload)

    def test_worker_backend_exit_waits_for_pending_work(self) -> None:
        root = Path("/tmp/root")
        inbox = Path("/tmp/inbox")

        with (
            mock.patch("aha_cli.services.chat_offsets.task_snapshot", return_value={"task": {"status": "awaiting_user"}}),
            mock.patch("aha_cli.services.chat_offsets.task_has_incomplete_sub_agents", return_value=False),
        ):
            self.assertTrue(worker_backend_should_exit_after_turn(root, "run", "task-001", "task-001", inbox, 0))

        with mock.patch("aha_cli.services.chat_offsets.task_snapshot", return_value={"task": {"status": "running"}}):
            self.assertFalse(worker_backend_should_exit_after_turn(root, "run", "task-001", "task-001", inbox, 0))

        self.assertFalse(worker_backend_should_exit_after_turn(root, "run", None, "task-001", inbox, 0))


if __name__ == "__main__":
    unittest.main()
