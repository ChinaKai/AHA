from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.chat_offsets import (
    acquire_chat_consumer,
    chat_offset_path,
    chat_turn_checkpoint_path,
    chat_turn_result_recoverable,
    finish_chat_turn,
    load_chat_offset,
    load_prepared_chat_turn,
    load_chat_turn_checkpoint,
    release_chat_consumer,
    safe_target_name,
    save_chat_offset,
    save_chat_turn_preparation,
    save_chat_turn_result,
    worker_backend_should_exit_after_turn,
)
from aha_cli.store.filesystem import (
    append_jsonl,
    inbox_path,
    iter_jsonl_from,
    read_json,
    reopen_task,
    run_dir,
    set_task_status,
)


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

    def test_save_chat_offset_never_moves_backwards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            offset_file = Path(tmp) / "runtime" / "offset.json"

            save_chat_offset(offset_file, 120)
            save_chat_offset(offset_file, 40)

            payload = read_json(offset_file)

        self.assertEqual(payload["offset"], 120)

    def test_chat_consumer_lock_rejects_a_second_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            first = acquire_chat_consumer(run, "main", "task-001")
            try:
                second = acquire_chat_consumer(run, "main", "task-001")
                self.assertIsNotNone(first)
                self.assertIsNone(second)
            finally:
                release_chat_consumer(first)

            replacement = acquire_chat_consumer(run, "main", "task-001")
            try:
                self.assertIsNotNone(replacement)
            finally:
                release_chat_consumer(replacement)

    def test_chat_turn_checkpoint_survives_until_finished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = chat_turn_checkpoint_path(Path(tmp), "main", "task-001")
            item = {"sender": "browser", "message": "fix sequencing", "task_id": "task-001"}

            save_chat_turn_result(path, 88, item, exit_code=0, reply="done")
            executed = load_chat_turn_checkpoint(path, 88, item)
            finish_chat_turn(path, 88, item)
            finished = load_chat_turn_checkpoint(path, 88, item)

        self.assertEqual(executed["phase"], "executed")
        self.assertEqual(executed["reply"], "done")
        self.assertEqual(finished["phase"], "finished")

    def test_chat_turn_result_is_not_recovered_after_backend_or_model_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = chat_turn_checkpoint_path(Path(tmp), "main", "task-001")
            item = {"sender": "browser", "message": "continue", "task_id": "task-001"}

            save_chat_turn_result(
                path,
                88,
                item,
                exit_code=0,
                reply="old success",
                backend="claude",
                model="env:gateway-a",
            )
            checkpoint = load_chat_turn_checkpoint(path, 88, item)

        self.assertTrue(chat_turn_result_recoverable(checkpoint, "claude", "env:gateway-a"))
        self.assertFalse(chat_turn_result_recoverable(checkpoint, "codex", "gpt-5.6-sol"))
        self.assertFalse(chat_turn_result_recoverable(checkpoint, "claude", "env:gateway-b"))

        legacy_checkpoint = dict(checkpoint or {})
        legacy_checkpoint.pop("backend", None)
        legacy_checkpoint.pop("model", None)
        legacy_checkpoint["prompt_event"] = {"data": {"source": "claude-chat"}}
        self.assertTrue(chat_turn_result_recoverable(legacy_checkpoint, "claude"))
        self.assertFalse(chat_turn_result_recoverable(legacy_checkpoint, "codex"))

    def test_failed_chat_turn_result_is_never_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = chat_turn_checkpoint_path(Path(tmp), "main", "task-001")
            item = {"sender": "browser", "message": "fix", "task_id": "task-001"}

            save_chat_turn_result(
                path,
                88,
                item,
                exit_code=1,
                reply="Prompt is too long",
                backend="claude",
                model="env:gateway-a",
            )
            checkpoint = load_chat_turn_checkpoint(path, 88, item)

        # A failed turn has no successful side effects to preserve; recovering it
        # would make a reopen hit the old failure instead of retrying the message.
        self.assertFalse(chat_turn_result_recoverable(checkpoint, "claude", "env:gateway-a"))
        self.assertFalse(chat_turn_result_recoverable(checkpoint, "claude", "env:gateway-b"))

        legacy_checkpoint = dict(checkpoint or {})
        legacy_checkpoint.pop("backend", None)
        legacy_checkpoint.pop("model", None)
        legacy_checkpoint["prompt_event"] = {"data": {"source": "claude-chat"}}
        self.assertFalse(chat_turn_result_recoverable(legacy_checkpoint, "claude"))

        missing_exit_code = dict(checkpoint or {})
        missing_exit_code.pop("exit_code", None)
        self.assertFalse(chat_turn_result_recoverable(missing_exit_code, "claude", "env:gateway-a"))

    def test_empty_successful_chat_turn_result_is_never_recovered(self) -> None:
        checkpoint = {
            "phase": "executed",
            "exit_code": 0,
            "reply": "",
            "backend": "claude",
            "model": "env:gateway-a",
        }

        self.assertFalse(chat_turn_result_recoverable(checkpoint, "claude", "env:gateway-a"))

    def test_reopen_discards_pending_messages_and_stale_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Reopen boundary", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

            inbox = inbox_path(root, run_id, "main")
            run = run_dir(root, run_id)
            append_jsonl(
                inbox,
                {"sender": "browser", "message": "failed message", "task_id": "task-001"},
            )
            failed_boundary = inbox.stat().st_size
            failed_item = {"sender": "browser", "message": "failed message", "task_id": "task-001"}
            checkpoint_file = chat_turn_checkpoint_path(run, "main", "task-001")
            save_chat_turn_result(
                checkpoint_file,
                failed_boundary,
                failed_item,
                exit_code=0,
                reply="",
                backend="stub",
            )
            set_task_status(root, run_id, "task-001", "failed", 0)

            reopen_task(root, run_id, "task-001")

            offset_file = chat_offset_path(run, "main", "task-001")
            self.assertEqual(read_json(offset_file)["offset"], failed_boundary)
            discarded = read_json(checkpoint_file)
            self.assertEqual(discarded["phase"], "discarded")
            self.assertEqual(discarded["discard_reason"], "task_reopened")
            self.assertEqual(discarded["reopen_boundary_offset"], failed_boundary)

            append_jsonl(
                inbox,
                {"sender": "browser", "message": "new follow-up", "task_id": "task-001"},
            )
            pending, _ = iter_jsonl_from(inbox, load_chat_offset(inbox, offset_file, from_start=False))

        self.assertEqual([item["message"] for item in pending], ["new follow-up"])

    def test_prepared_chat_turn_preserves_merged_item_through_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = chat_turn_checkpoint_path(Path(tmp), "main", "task-001")
            item = {
                "sender": "feishu",
                "message": "merged request",
                "task_id": "task-001",
                "feishu_merged_count": 3,
            }

            save_chat_turn_preparation(path, 0, 240, item)
            prepared = load_prepared_chat_turn(path, 0)
            save_chat_turn_result(path, 240, item, exit_code=0, reply="done")
            executed = load_prepared_chat_turn(path, 0)

        self.assertEqual(prepared["phase"], "prepared")
        self.assertEqual(prepared["item"], item)
        self.assertEqual(executed["phase"], "executed")
        self.assertEqual(executed["source_offset"], 0)
        self.assertEqual(executed["item"], item)

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
