from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.chat_offsets import (
    chat_offset_path,
    load_chat_offset,
    safe_target_name,
    save_chat_offset,
    worker_backend_should_exit_after_turn,
)
from aha_cli.store.filesystem import append_jsonl, read_json, set_task_status


class ChatOffsetTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

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

    def test_main_waiting_backend_exits_after_turn_while_task_keeps_running(self) -> None:
        root = Path("/tmp/root")
        inbox = Path("/tmp/inbox")
        task = {
            "status": "running",
            "agents": [
                {"id": "main", "status": "waiting", "waiting_reason": "subagents"},
                {"id": "sub-001", "status": "running", "role": "sub"},
            ],
        }

        with (
            mock.patch("aha_cli.services.chat_offsets.task_snapshot", return_value={"task": task}),
            mock.patch("aha_cli.services.chat_offsets.task_has_incomplete_sub_agents", return_value=True),
        ):
            self.assertTrue(
                worker_backend_should_exit_after_turn(
                    root,
                    "run",
                    "task-001",
                    "task-001",
                    inbox,
                    0,
                    target="main",
                )
            )
            self.assertFalse(
                worker_backend_should_exit_after_turn(
                    root,
                    "run",
                    "task-001",
                    "task-001",
                    inbox,
                    0,
                    target="sub-001",
                )
            )

        task["agents"][0]["waiting_reason"] = "host"
        with mock.patch("aha_cli.services.chat_offsets.task_snapshot", return_value={"task": task}):
            self.assertTrue(
                worker_backend_should_exit_after_turn(
                    root,
                    "run",
                    "task-001",
                    "task-001",
                    inbox,
                    0,
                    target="main",
                )
            )

    def test_main_waiting_backend_does_not_exit_with_unprocessed_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path("/tmp/root")
            inbox = Path(tmp) / "inbox.jsonl"
            append_jsonl(inbox, {"message": "new work"})
            task = {
                "status": "running",
                "agents": [{"id": "main", "status": "waiting", "waiting_reason": "host"}],
            }

            with mock.patch("aha_cli.services.chat_offsets.task_snapshot", return_value={"task": task}):
                self.assertFalse(
                    worker_backend_should_exit_after_turn(
                        root,
                        "run",
                        "task-001",
                        "task-001",
                        inbox,
                        0,
                        target="main",
                    )
                )

    def test_task_scoped_backend_exits_when_idle_and_task_is_awaiting_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Idle backend", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "awaiting_user")

                with (
                    mock.patch("aha_cli.services.chat.worker_backend_should_exit_after_turn", return_value=True) as should_exit,
                    mock.patch("aha_cli.services.chat.mark_backend_stopped") as mark_stopped,
                    mock.patch("aha_cli.services.chat.time.sleep", side_effect=AssertionError("idle backend should exit before sleeping")),
                ):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001")

        self.assertEqual(code, 0)
        should_exit.assert_called_once()
        mark_stopped.assert_called_once()
        self.assertEqual(mark_stopped.call_args.args[:3], (root / ".aha", run_id, "main"))
        self.assertEqual(mark_stopped.call_args.kwargs["task_id"], "task-001")


if __name__ == "__main__":
    unittest.main()
