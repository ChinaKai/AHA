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
    event_path,
    inbox_path,
    iter_jsonl_from,
    run_dir,
    set_agent_status,
    set_task_status,
    update_task_supervision_config,
)
from aha_cli.web.task_messaging import handle_send_payload
from tests.helpers import isolated_cli_environment


class WebTaskMessagingTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with isolated_cli_environment(), mock.patch("sys.stdout", out):
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

    def test_aha_agent_routed_commands_queue_backend_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "AHA command autostart", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)
                queued: list[dict | None] = []

                def queue_backend_start(_root: Path, _run_id: str, autostart: dict | None) -> dict | None:
                    queued.append(autostart)
                    return {"queued": True, "target": autostart["target"], "task_id": autostart["task_id"]} if autostart else None

                with mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "stopped"}):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "task_id": "task-001",
                            "role": "main",
                            "sender": "browser",
                            "message": "/aha kb 更新知识库导航状态说明",
                        },
                        queued_backend_starter=queue_backend_start,
                        background_backend_start=True,
                        debug_logger=lambda *_args, **_kwargs: None,
                    )
                offset = json.loads(chat_offset_path(run_dir(root, run_id), "main", "task-001").read_text(encoding="utf-8"))["offset"]
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), offset)

        self.assertTrue(result["ok"])
        self.assertTrue(result["backend_start"]["queued"])
        self.assertEqual(result["backend_start"]["target"], "main")
        self.assertEqual(result["backend_start"]["task_id"], "task-001")
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["backend"], "codex")
        self.assertEqual(messages[-1]["command_namespace"], "aha_kb")
        self.assertEqual(messages[-1]["original_command"], "/aha kb 更新知识库导航状态说明")
        self.assertTrue(messages[-1]["plain_sticky"])
        self.assertIn("AHA knowledge-base feedback request.", messages[-1]["message"])

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

    def test_send_message_adds_memo_attachment_resolution_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Image chat", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                message = "Please inspect this image.\n\n![shot](task_memo_assets/ab/file.png)"

                with mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "busy"}):
                    handle_send_payload(
                        root,
                        run_id,
                        {"target": "main", "task_id": "task-001", "role": "main", "sender": "browser", "message": message},
                        command_handler=lambda *_args: (False, None, {}),
                        debug_logger=lambda *_args, **_kwargs: None,
                    )
                    first_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                    handle_send_payload(
                        root,
                        run_id,
                        {"target": "main", "task_id": "task-001", "role": "main", "sender": "browser", "message": first_messages[0]["message"]},
                        command_handler=lambda *_args: (False, None, {}),
                        debug_logger=lambda *_args, **_kwargs: None,
                    )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)

        asset_dir = str((run_dir(root, run_id) / "task_memo_assets").resolve())
        self.assertIn("AHA memo attachment resolution:", messages[0]["message"])
        self.assertIn(asset_dir, messages[0]["message"])
        self.assertIn("do not search for them relative to the workspace", messages[0]["message"])
        self.assertEqual(messages[1]["message"].count("AHA memo attachment resolution:"), 1)

    def test_send_message_preserves_image_fields_for_prompt_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Image payload", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "busy"}):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "task_id": "task-001",
                            "role": "main",
                            "sender": "browser",
                            "message": "看图",
                            "images": [{"path": "task_memo_assets/ab/shot.png", "mime": "image/png"}],
                        },
                        command_handler=lambda *_args: (False, None, {}),
                        debug_logger=lambda *_args, **_kwargs: None,
                    )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)

        self.assertTrue(result["ok"])
        self.assertEqual(messages[0]["images"], [{"path": "task_memo_assets/ab/shot.png", "mime": "image/png"}])

    def test_send_while_backend_busy_marks_message_plain_sticky(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Busy plain sticky", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with (
                    mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "busy"}),
                    mock.patch("aha_cli.web.task_messaging.start_backend") as start_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "task_id": "task-001",
                            "role": "main",
                            "sender": "browser",
                            "message": "继续刚才的问题",
                        },
                        command_handler=lambda *_args: (False, None, {}),
                        debug_logger=lambda *_args, **_kwargs: None,
                    )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)

        self.assertTrue(result["ok"])
        self.assertNotIn("backend", result)
        self.assertEqual(messages[-1]["message"], "继续刚才的问题")
        self.assertTrue(messages[-1]["plain_sticky"])
        start_backend.assert_not_called()

    def test_send_to_main_defers_while_supervision_host_review_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Host review pending", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="codex",
                    real_agent_enabled=True,
                )
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)
                set_agent_status(root, run_id, "task-001", "host", "pending")

                with (
                    mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "busy"}),
                    mock.patch("aha_cli.web.task_messaging.start_backend") as start_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "task_id": "task-001",
                            "role": "main",
                            "sender": "browser",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "message": "should stay pending",
                        },
                        command_handler=lambda *_args: (False, None, {}),
                        debug_logger=lambda *_args, **_kwargs: None,
                    )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]

        self.assertTrue(result["ok"])
        self.assertTrue(result["deferred"])
        self.assertEqual(result["reason"], "host_review")
        self.assertEqual(messages, [])
        start_backend.assert_not_called()
        self.assertTrue(any(row["type"] == "message_deferred" and row["data"].get("reason") == "host_review" for row in rows))

    def test_send_to_main_allows_stale_pending_supervision_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Stale host pending", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="codex",
                    real_agent_enabled=True,
                )
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)
                set_agent_status(root, run_id, "task-001", "host", "pending")

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
                            "from_agent": "browser",
                            "to_agent": "main",
                            "message": "continue after stale host",
                        },
                        command_handler=lambda *_args: (False, None, {}),
                        debug_logger=lambda *_args, **_kwargs: None,
                    )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)

        self.assertTrue(result["ok"])
        self.assertFalse(result.get("deferred", False))
        self.assertEqual(result["backend"]["status"], "running")
        self.assertEqual([item["message"] for item in messages], ["continue after stale host"])
        start_backend.assert_called_once()

    def test_send_to_main_allows_stale_running_supervision_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Stale host running", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="codex",
                    real_agent_enabled=True,
                )
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "waiting", waiting_reason="host")
                set_agent_status(root, run_id, "task-001", "host", "running")

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
                            "from_agent": "browser",
                            "to_agent": "main",
                            "message": "continue after stopped host",
                        },
                        command_handler=lambda *_args: (False, None, {}),
                        debug_logger=lambda *_args, **_kwargs: None,
                    )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)

        self.assertTrue(result["ok"])
        self.assertFalse(result.get("deferred", False))
        self.assertEqual([item["message"] for item in messages], ["continue after stopped host"])
        start_backend.assert_called_once()

    def test_send_to_main_allows_terminal_stopped_host_with_stale_main_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Terminal host stale wait", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="codex",
                    real_agent_enabled=True,
                )
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "waiting", waiting_reason="host")
                set_agent_status(root, run_id, "task-001", "host", "interrupted")

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
                            "from_agent": "browser",
                            "to_agent": "main",
                            "message": "continue after interrupted host",
                        },
                        command_handler=lambda *_args: (False, None, {}),
                        debug_logger=lambda *_args, **_kwargs: None,
                    )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)

        self.assertTrue(result["ok"])
        self.assertFalse(result.get("deferred", False))
        self.assertEqual([item["message"] for item in messages], ["continue after interrupted host"])
        start_backend.assert_called_once()

    def test_send_to_main_defers_while_main_waits_for_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Main host wait", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="codex",
                    real_agent_enabled=True,
                )
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "waiting", waiting_reason="host")
                set_agent_status(root, run_id, "task-001", "host", "completed", 0)

                with mock.patch("aha_cli.web.task_messaging.start_backend") as start_backend:
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "task_id": "task-001",
                            "role": "main",
                            "sender": "browser",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "message": "should stay pending",
                        },
                        command_handler=lambda *_args: (False, None, {}),
                        debug_logger=lambda *_args, **_kwargs: None,
                    )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)

        self.assertTrue(result["deferred"])
        self.assertEqual(messages, [])
        start_backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
