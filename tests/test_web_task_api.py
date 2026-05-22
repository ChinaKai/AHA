from __future__ import annotations

import asyncio
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main
from aha_cli.services.chat import chat_offset_path
from aha_cli.store.filesystem import (
    add_agent,
    complete_task,
    inbox_path,
    iter_jsonl_from,
    read_json,
    run_dir,
    status_snapshot,
    task_context_snapshot,
    task_final_snapshot,
    update_agent_config,
    update_task_proxy_config,
    update_task_supervision_config,
    write_task_result,
)
from aha_cli.web.server import handle_send_payload, workspace_options
from tests.helpers import fetch_ui_response, json_response_body


class WebTaskApiTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_api_task_create_accepts_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task details", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={
                            "title": "Detailed task",
                            "description": "Use the attached notes and preserve existing behavior.",
                            "dispatch": False,
                        },
                    )
                )
                body = json_response_body(response)
                status = status_snapshot(root, run_id)
                context = task_context_snapshot(root, run_id, body["task"]["id"])

        self.assertTrue(body["ok"])
        self.assertEqual(body["task"]["description"], "Use the attached notes and preserve existing behavior.")
        self.assertEqual(status["tasks"][-1]["description"], "Use the attached notes and preserve existing behavior.")
        self.assertIn("Use the attached notes and preserve existing behavior.", context["prompt"])

    def test_api_task_create_accepts_supervision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Create supervised task", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/tasks",
                        method="POST",
                        payload={
                            "title": "Supervised task",
                            "dispatch": False,
                            "supervision": {
                                "mode": "assisted",
                                "host_backend": "claude",
                                "real_agent_enabled": True,
                                "max_rounds": 9,
                            },
                        },
                    )
                )
                body = json_response_body(response)
                task = status_snapshot(root, run_id)["tasks"][-1]

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["task"]["supervision"]["mode"], "assisted")
        self.assertEqual(body["task"]["supervision"]["host_backend"], "claude")
        self.assertEqual(body["task"]["supervision"]["host_agent_id"], "host")
        self.assertTrue(body["task"]["supervision"]["real_agent_enabled"])
        self.assertEqual(body["task"]["supervision"]["max_rounds"], 9)
        self.assertEqual(task["supervision"], body["task"]["supervision"])
        self.assertTrue(any(agent["id"] == "host" and agent["role"] == "host" for agent in task["agents"]))

    def test_api_task_supervision_config_updates_existing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Supervision API", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                initial = status_snapshot(root, run_id)["tasks"][0]
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task/task-001/supervision",
                        method="POST",
                        payload={
                            "mode": "assisted",
                            "max_rounds": 7,
                        },
                    )
                )
                body = json_response_body(response)
                updated = status_snapshot(root, run_id)["tasks"][0]

        self.assertEqual(initial["supervision"]["mode"], "manual")
        self.assertEqual(initial["supervision"]["channel"], "main_only")
        self.assertFalse(initial["supervision"]["real_agent_enabled"])
        self.assertTrue(body["ok"])
        self.assertEqual(body["task"]["supervision"]["mode"], "assisted")
        self.assertEqual(body["task"]["supervision"]["host_backend"], "stub")
        self.assertFalse(body["task"]["supervision"]["real_agent_enabled"])
        self.assertEqual(body["task"]["supervision"]["max_rounds"], 7)
        self.assertNotIn("allowed_actions", body["task"]["supervision"])
        self.assertEqual(updated["supervision"], body["task"]["supervision"])

    def test_task_supervision_host_agent_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Supervision host", "--agents", "1")
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
                task = status_snapshot(root, run_id)["tasks"][0]

        host = next(agent for agent in task["agents"] if agent["role"] == "host")
        self.assertEqual(host["backend"], "claude")
        self.assertEqual(host["workspace_path"], task["workspace_path"])
        self.assertEqual(host["sandbox"], "read-only")
        self.assertEqual(host["approval"], "never")
        self.assertEqual(task["supervision"]["host_agent_id"], "host")

    def test_send_to_supervision_host_stores_note_without_autostart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Host note", "--agents", "1")
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

                with mock.patch("aha_cli.web.task_messaging.start_backend", return_value={"status": "running"}) as start:
                    response = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "host",
                            "task_id": "task-001",
                            "role": "host",
                            "sender": "browser",
                            "from_agent": "browser",
                            "to_agent": "host",
                            "message": "后续收到测试消息后再决定是否让 main 继续。",
                        },
                    )
                host_inbox = inbox_path(root, run_id, "host")
                host_messages, _ = iter_jsonl_from(host_inbox, 0)
                offset = read_json(chat_offset_path(run_dir(root, run_id), "host", "task-001"))
                host_inbox_size = host_inbox.stat().st_size

        self.assertTrue(response["ok"])
        self.assertNotIn("backend", response)
        start.assert_not_called()
        self.assertEqual(host_messages[-1]["message"], "后续收到测试消息后再决定是否让 main 继续。")
        self.assertEqual(offset["offset"], host_inbox_size)

    def test_web_task_creation_autostarts_dispatched_main_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task create autostart", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with mock.patch("aha_cli.web.task_runtime.start_backend", return_value={"status": "running", "started": True}) as start_backend:
                    response = asyncio.run(
                        fetch_ui_response(
                            root,
                            run_id,
                            "/api/tasks",
                            method="POST",
                            payload={
                                "title": "Autostart task",
                                "backend": "codex",
                                "sandbox": "danger-full-access",
                                "approval": "never",
                                "dispatch": True,
                            },
                        )
                    )
                    body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["backend"]["status"], "running")
        start_backend.assert_called_once()
        self.assertEqual(start_backend.call_args.args[:3], (root, run_id, "main"))
        self.assertEqual(start_backend.call_args.kwargs["task_id"], body["task"]["id"])
        self.assertTrue(start_backend.call_args.kwargs["from_start"])

    def test_task_action_resume_alias_reopens_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Resume alias", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                complete_task(root, run_id, "task-001", 0)

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/task/task-001/resume", method="POST"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["task"]["status"], "awaiting_user")

    def test_task_lightweight_snapshots_exclude_heavy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Lightweight", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "large message", sender="browser", task_id="task-001")
                write_task_result(root, run_id, "task-001", "final text")

                final = task_final_snapshot(root, run_id, "task-001")
                context = task_context_snapshot(root, run_id, "task-001")

        self.assertEqual(final["result"].strip(), "final text")
        self.assertNotIn("messages", final)
        self.assertNotIn("log", final)
        self.assertIn("prompt", context)
        self.assertNotIn("messages", context)
        self.assertNotIn("log", context)

    def test_workspace_options_include_multiple_project_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hl_root = base / "hl_project"
            my_root = base / "my_project"
            (hl_root / "fw_omni_builder").mkdir(parents=True)
            (my_root / "aha").mkdir(parents=True)

            options = workspace_options([hl_root, my_root])

        self.assertEqual(
            options,
            [
                {
                    "name": "fw_omni_builder",
                    "label": "hl_project/fw_omni_builder",
                    "path": str(hl_root / "fw_omni_builder"),
                    "root": str(hl_root),
                },
                {
                    "name": "aha",
                    "label": "my_project/aha",
                    "path": str(my_root / "aha"),
                    "root": str(my_root),
                },
            ],
        )

    def test_task_proxy_config_and_agent_toggle_are_in_status_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "Proxy defaults",
                    "--agents",
                    "1",
                    "--http-proxy",
                    "http://127.0.0.1:7890",
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                update_agent_config(root, run_id, "task-001", "sub-001", proxy_enabled=False)
                task = update_task_proxy_config(
                    root,
                    run_id,
                    "task-001",
                    proxy_enabled=False,
                    http_proxy="http://127.0.0.1:8888",
                    https_proxy="http://127.0.0.1:8888",
                    no_proxy="localhost,127.0.0.1",
                )
                self.assertFalse(task["preferred_proxy_enabled"])

                snapshot = status_snapshot(root, run_id)
                task = snapshot["tasks"][0]
                agents = {agent["id"]: agent for agent in task["agents"]}

        self.assertEqual(task["preferred_http_proxy"], "http://127.0.0.1:8888")
        self.assertEqual(task["preferred_https_proxy"], "http://127.0.0.1:8888")
        self.assertEqual(task["preferred_no_proxy"], "localhost,127.0.0.1")
        self.assertFalse(task["preferred_proxy_enabled"])
        self.assertTrue(agents["main"]["proxy_enabled"])
        self.assertFalse(agents["sub-001"]["proxy_enabled"])

    def test_task_proxy_config_api_updates_existing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Proxy API", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-config",
                        method="POST",
                        payload={
                            "task_id": "task-001",
                            "proxy_enabled": True,
                            "http_proxy": "http://proxy.local:8080",
                            "https_proxy": "http://proxy.local:8080",
                            "no_proxy": "localhost,127.0.0.1",
                        },
                    )
                )
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertTrue(body["task"]["preferred_proxy_enabled"])
        self.assertEqual(body["task"]["preferred_http_proxy"], "http://proxy.local:8080")
        self.assertEqual(body["task"]["preferred_no_proxy"], "localhost,127.0.0.1")

    def test_task_proxy_action_api_updates_existing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Proxy action API", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task/task-001/proxy",
                        method="POST",
                        payload={
                            "proxy_enabled": True,
                            "http_proxy": "http://proxy.local:8080",
                            "https_proxy": "http://proxy.local:8080",
                            "no_proxy": "localhost,127.0.0.1",
                        },
                    )
                )
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertTrue(body["task"]["preferred_proxy_enabled"])
        self.assertEqual(body["task"]["preferred_http_proxy"], "http://proxy.local:8080")


if __name__ == "__main__":
    unittest.main()
