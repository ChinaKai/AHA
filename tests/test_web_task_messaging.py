from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.chat import chat_offset_path
from aha_cli.store.filesystem import (
    complete_task,
    inbox_path,
    iter_jsonl_from,
    run_dir,
    set_agent_status,
    set_task_status,
    update_task_supervision_config,
)
from aha_cli.web.task_messaging import handle_send_payload


class WebTaskMessagingTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_send_autostarts_backend_after_recording_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Autostart message", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)

                with (
                    mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.task_messaging.start_backend", return_value={"status": "running"}) as start_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "task_id": "task-001",
                            "role": "main",
                            "sender": "browser",
                            "message": "continue",
                        },
                        command_handler=lambda *_args: (False, None, {}),
                        debug_logger=lambda *_args, **_kwargs: None,
                    )

                offset = json.loads(chat_offset_path(run_dir(root, run_id), "main", "task-001").read_text(encoding="utf-8"))["offset"]
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), offset)

        self.assertEqual(result["backend"]["status"], "running")
        start_backend.assert_called_once()
        self.assertFalse(start_backend.call_args.kwargs["from_start"])
        self.assertEqual([item["message"] for item in messages], ["continue"])

    def test_supervision_host_message_advances_offset_without_autostart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Host message", "--agents", "1")
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

                with mock.patch("aha_cli.web.task_messaging.start_backend") as start_backend:
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "host",
                            "task_id": "task-001",
                            "role": "host",
                            "sender": "browser",
                            "from_agent": "browser",
                            "to_agent": "host",
                            "message": "host note",
                        },
                        command_handler=lambda *_args: (False, None, {}),
                        debug_logger=lambda *_args, **_kwargs: None,
                    )

                inbox = inbox_path(root, run_id, "host")
                offset = json.loads(chat_offset_path(run_dir(root, run_id), "host", "task-001").read_text(encoding="utf-8"))["offset"]
                inbox_size = inbox.stat().st_size

        self.assertTrue(result["ok"])
        self.assertNotIn("backend", result)
        start_backend.assert_not_called()
        self.assertEqual(offset, inbox_size)

    def test_completed_task_blocks_followup_until_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Locked message", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                complete_task(root, run_id, "task-001", 0)

                with self.assertRaisesRegex(ValueError, "use /aha reopen"):
                    handle_send_payload(
                        root,
                        run_id,
                        {"target": "main", "task_id": "task-001", "role": "main", "sender": "browser", "message": "blocked"},
                        command_handler=lambda *_args: (False, None, {}),
                        debug_logger=lambda *_args, **_kwargs: None,
                    )

    def test_command_handler_can_handle_send_without_storing_agent_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Command message", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                result = handle_send_payload(
                    root,
                    run_id,
                    {"target": "main", "task_id": "task-001", "role": "main", "sender": "browser", "message": "/aha status"},
                    command_handler=lambda *_args: (True, None, {"message": {"message": "status"}}),
                    prepared_backend_starter=lambda *_args: None,
                    debug_logger=lambda *_args, **_kwargs: None,
                )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)

        self.assertTrue(result["ok"])
        self.assertEqual(result["handled_by"], "aha")
        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
