from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main, task_snapshot
from aha_cli.services.chat import chat_offset_path
from aha_cli.store.filesystem import inbox_path, iter_jsonl_from, read_json, run_dir, update_task_supervision_config
from aha_cli.web.task_runtime import (
    message_backend_autostart_config,
    prepare_task_main_autostart,
    request_task_finalization_with_backend,
    start_dispatched_task_backend,
    start_prepared_backend,
)


class TaskRuntimeTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_prepare_and_start_backend_uses_task_agent_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Runtime autostart", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "already processed", sender="browser", task_id="task-001", role="main")
                offset_file = chat_offset_path(run_dir(root, run_id), "main", "task-001")
                self.assertFalse(offset_file.exists())

                with mock.patch("aha_cli.web.task_runtime.backend_status", return_value={"status": "stopped"}):
                    autostart = prepare_task_main_autostart(root, run_id, "task-001")
                with mock.patch("aha_cli.web.task_runtime.start_backend", return_value={"status": "running"}) as start:
                    backend = start_prepared_backend(root, run_id, autostart)
                offset = read_json(offset_file)["offset"]
                inbox_size = inbox_path(root, run_id, "main").stat().st_size

        self.assertEqual(autostart["backend"], "codex")
        self.assertEqual(autostart["target"], "main")
        self.assertEqual(autostart["task_id"], "task-001")
        self.assertEqual(offset, inbox_size)
        self.assertEqual(backend["status"], "running")
        start.assert_called_once()
        self.assertEqual(start.call_args.args[:3], (root, run_id, "main"))
        self.assertFalse(start.call_args.kwargs["from_start"])
        self.assertEqual(start.call_args.kwargs["task_id"], "task-001")

    def test_request_finalization_with_backend_starts_prepared_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Runtime final", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with (
                    mock.patch("aha_cli.web.task_runtime.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.task_runtime.start_backend", return_value={"status": "running", "started": True}) as start,
                ):
                    payload = request_task_finalization_with_backend(root, run_id, "task-001", "/aha final")
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                detail = task_snapshot(root, run_id, "task-001")

        self.assertIn("Finalization requested", payload["message"])
        self.assertEqual(payload["backend"]["status"], "running")
        start.assert_called_once()
        self.assertEqual(messages[-1]["result_policy"], "finalize")
        self.assertEqual(messages[-1]["original_command"], "/aha final")
        self.assertEqual(detail["task"]["status"], "running")
        self.assertTrue(detail["task"]["coordination"]["final_summary_requested_at"])

    def test_start_dispatched_task_backend_uses_from_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Runtime dispatch", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                task = task_snapshot(root, run_id, "task-001")["task"]

                with (
                    mock.patch("aha_cli.web.task_runtime.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.task_runtime.start_backend", return_value={"status": "running"}) as start,
                ):
                    skipped = start_dispatched_task_backend(root, run_id, task, False)
                    started = start_dispatched_task_backend(root, run_id, task, True)

        self.assertIsNone(skipped)
        self.assertEqual(started["status"], "running")
        start.assert_called_once()
        self.assertTrue(start.call_args.kwargs["from_start"])
        self.assertEqual(start.call_args.kwargs["task_id"], "task-001")

    def test_supervision_host_target_does_not_autostart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Runtime host", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                )
                with mock.patch("aha_cli.web.task_runtime.backend_status", return_value={"status": "stopped"}) as backend_status:
                    autostart = message_backend_autostart_config(root, run_id, "task-001", "host")

        self.assertIsNone(autostart)
        backend_status.assert_not_called()
