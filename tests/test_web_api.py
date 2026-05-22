from __future__ import annotations

import asyncio
import gzip
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main, task_snapshot
from aha_cli.services.chat import chat_offset_path
from aha_cli.store.filesystem import (
    add_agent,
    append_event,
    conversation_events_page,
    event_path,
    inbox_path,
    iter_jsonl_from,
    iter_jsonl_reverse,
    read_json,
    run_dir,
    set_task_status,
    status_snapshot,
    task_context_snapshot,
    task_final_snapshot,
    task_log_page,
    update_agent_config,
    update_task_proxy_config,
    update_task_supervision_config,
    write_task_result,
)
from aha_cli.web.server import handle_send_payload, workspace_options
from tests.helpers import fetch_ui_response, json_response_body


class WebApiTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_api_bootstrap_works_without_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            response = asyncio.run(fetch_ui_response(root, "", "/api/bootstrap"))
            body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(body["aha_home"], str(root))
        self.assertFalse(body["initialized"])
        self.assertIn("default_workspace_path", body)
        self.assertEqual(body["default_run_id"], "")
        self.assertEqual(body["runs"], [])

    def test_api_workspace_registration_can_create_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            workspace = Path(tmp) / "repo"
            root.mkdir()
            workspace.mkdir()
            add_response = asyncio.run(
                fetch_ui_response(root, "", "/api/workspaces", method="POST", payload={"path": str(workspace), "name": "demo"})
            )
            add_body = json_response_body(add_response)
            create_response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/runs",
                    method="POST",
                    payload={"goal": "Web setup", "mode": "research", "workspace_id": add_body["workspace"]["id"]},
                )
            )
            create_body = json_response_body(create_response)
            plan = read_json(root / "runs" / create_body["run"]["id"] / "plan.json")

        self.assertTrue(add_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertTrue(create_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(plan["tasks"][0]["workspace_id"], "ws-001")
        self.assertEqual(plan["tasks"][0]["workspace_path"], str(workspace))

    def test_api_run_creation_accepts_proxy_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/runs",
                    method="POST",
                    payload={
                        "goal": "Proxy setup",
                        "mode": "research",
                        "backend": "codex",
                        "proxy_enabled": True,
                        "http_proxy": "http://127.0.0.1:7890",
                        "https_proxy": "http://127.0.0.1:7890",
                        "no_proxy": "localhost,127.0.0.1",
                    },
                )
            )
            body = json_response_body(response)
            plan = read_json(root / "runs" / body["run"]["id"] / "plan.json")
            task = plan["tasks"][0]

        self.assertTrue(response.startswith(b"HTTP/1.1 201 Created"))
        self.assertTrue(task["preferred_proxy_enabled"])
        self.assertEqual(task["preferred_http_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(task["preferred_https_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(task["preferred_no_proxy"], "localhost,127.0.0.1")
        self.assertTrue(task["agents"][0]["proxy_enabled"])

    def test_api_run_creation_can_dispatch_initial_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            with mock.patch("aha_cli.web.server.start_backend", return_value={"status": "running", "started": True}) as start_backend:
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        "",
                        "/api/runs",
                        method="POST",
                        payload={
                            "goal": "Web setup",
                            "mode": "research",
                            "backend": "codex",
                            "task_titles": ["Web setup"],
                            "dispatch": True,
                        },
                    )
                )
                body = json_response_body(response)
            run_id = body["run"]["id"]
            plan = read_json(root / "runs" / run_id / "plan.json")
            events, _ = iter_jsonl_from(root / "runs" / run_id / "events.jsonl", 0)

        self.assertTrue(response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(plan["tasks"][0]["title"], "Web setup")
        self.assertTrue(any(event["type"] == "task_dispatched" and event["data"]["task_id"] == "task-001" for event in events))
        start_backend.assert_called_once()
        self.assertEqual(start_backend.call_args.args[:3], (root, run_id, "main"))
        self.assertEqual(start_backend.call_args.kwargs["task_id"], "task-001")
        self.assertTrue(start_backend.call_args.kwargs["from_start"])

    def test_api_runs_lists_and_creates_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Default session", "--agents", "1")
                self.assertEqual(code, 0)
                default_run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                runs_response = asyncio.run(fetch_ui_response(root, default_run_id, "/api/runs"))
                runs_body = json_response_body(runs_response)
                create_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        default_run_id,
                        "/api/runs",
                        method="POST",
                        payload={"goal": "Second session", "agents": 1, "mode": "research"},
                    )
                )
                create_body = json_response_body(create_response)
                updated_response = asyncio.run(fetch_ui_response(root, default_run_id, "/api/runs"))
                updated_body = json_response_body(updated_response)

        self.assertTrue(runs_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(runs_body["default_run_id"], default_run_id)
        self.assertIn(default_run_id, {item["id"] for item in runs_body["runs"]})
        self.assertTrue(create_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertTrue(create_body["ok"])
        self.assertEqual(create_body["run"]["goal"], "Second session")
        self.assertIn(create_body["run"]["id"], {item["id"] for item in updated_body["runs"]})

    def test_api_run_archive_exports_and_imports_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "UI archive",
                    "--agents",
                    "1",
                    "--enable-proxy",
                    "--http-proxy",
                    "http://user:secret@example.test:8080",
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = "ui-backend-secret"
                session_file.write_text(json.dumps(session), encoding="utf-8")
                log_file = run_dir(root, run_id) / "logs" / "backend.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_file.write_text("backend log", encoding="utf-8")

                export_response = asyncio.run(
                    fetch_ui_response(root, run_id, f"/api/run/export?run_id={run_id}&no_logs=1", timeout=2.0)
                )
                export_headers, archive_bytes = export_response.split(b"\r\n\r\n", 1)
                with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as exported:
                    names = set(exported.getnames())
                    plan = json.load(exported.extractfile("run/plan.json"))

                boundary = "----aha-test-boundary"
                multipart_body = (
                    (
                        f"--{boundary}\r\n"
                        'Content-Disposition: form-data; name="archive"; filename="run.tar.gz"\r\n'
                        "Content-Type: application/gzip\r\n"
                        "\r\n"
                    ).encode("ascii")
                    + archive_bytes
                    + f"\r\n--{boundary}--\r\n".encode("ascii")
                )
                import_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/run/import",
                        timeout=3.0,
                        method="POST",
                        body=multipart_body,
                        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    )
                )
                import_body = json_response_body(import_response)
                imported_run_id = import_body["imported_run_id"]
                imported_status = status_snapshot(root, imported_run_id)

        self.assertTrue(export_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn(b'Content-Disposition: attachment; filename="aha-run-', export_headers)
        self.assertIn("aha-run-manifest.json", names)
        self.assertIn("run/plan.json", names)
        self.assertNotIn("run/logs/backend.log", names)
        self.assertEqual(plan["tasks"][0]["preferred_http_proxy"], "<redacted>")
        self.assertNotIn("secret", json.dumps(plan))
        self.assertTrue(import_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(import_body["source_run_id"], run_id)
        self.assertNotEqual(imported_run_id, run_id)
        self.assertIn(imported_run_id, {item["id"] for item in import_body["runs"]})
        self.assertEqual(imported_status["tasks"][0]["agents"][0]["session_status"], "imported")
        self.assertIsNone(imported_status["tasks"][0]["agents"][0]["backend_session_id"])

    def test_api_routes_can_target_non_default_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, first_output = self.run_cli("plan", "First session", "--agents", "1")
                self.assertEqual(code, 0)
                first_run_id = first_output.splitlines()[0].split(": ", 1)[1]
                code, second_output = self.run_cli("plan", "Second session", "--agents", "1")
                self.assertEqual(code, 0)
                second_run_id = second_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, second_run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "second-only-event"})
                append_message(root, second_run_id, "main", "second conversation", sender="browser", task_id="task-001", role="main")

                default_status = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, "/api/status")))
                selected_status = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, f"/api/status?run_id={second_run_id}")))
                events = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, f"/api/events?run_id={second_run_id}&offset=0&limit=50")))
                conversation = json_response_body(
                    asyncio.run(fetch_ui_response(root, first_run_id, f"/api/conversation-events?run_id={second_run_id}&task_id=task-001&target=main"))
                )
                send_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        first_run_id,
                        "/api/send",
                        method="POST",
                        payload={"run_id": second_run_id, "target": "manual-target", "message": "sent to second"},
                    )
                )
                task_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        first_run_id,
                        "/api/tasks",
                        method="POST",
                        payload={"run_id": second_run_id, "title": "Second extra task", "dispatch": False},
                    )
                )
                first_after = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, "/api/status")))
                second_after = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, f"/api/status?run_id={second_run_id}")))
                first_manual, _ = iter_jsonl_from(inbox_path(root, first_run_id, "manual-target"), 0)
                second_manual, _ = iter_jsonl_from(inbox_path(root, second_run_id, "manual-target"), 0)

        self.assertEqual(default_status["run_id"], first_run_id)
        self.assertEqual(default_status["goal"], "First session")
        self.assertEqual(selected_status["run_id"], second_run_id)
        self.assertEqual(selected_status["goal"], "Second session")
        self.assertEqual(events["run_id"], second_run_id)
        self.assertTrue(any(event.get("data", {}).get("text") == "second-only-event" for event in events["events"]))
        self.assertTrue(any(event.get("data", {}).get("message") == "second conversation" for event in conversation["events"]))
        self.assertTrue(send_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(task_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(first_after["tasks"][0]["title"], "Map the relevant files, concepts, and terminology for the goal.")
        self.assertEqual(len(first_after["tasks"]), 1)
        self.assertEqual(len(second_after["tasks"]), 2)
        self.assertEqual(first_manual, [])
        self.assertEqual(second_manual[-1]["message"], "sent to second")

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

                with mock.patch("aha_cli.web.server.start_backend", return_value={"status": "running"}) as start:
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

    def test_api_routes_fallback_to_latest_run_without_server_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Default fallback", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                runs_response = asyncio.run(fetch_ui_response(root, "", "/api/runs"))
                status_response = asyncio.run(fetch_ui_response(root, "", "/api/status"))

        self.assertTrue(runs_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(status_response.startswith(b"HTTP/1.1 200 OK"))
        runs_body = json_response_body(runs_response)
        status_body = json_response_body(status_response)
        self.assertEqual(runs_body["default_run_id"], run_id)
        self.assertEqual(status_body["run_id"], run_id)
        self.assertEqual(status_body["goal"], "Default fallback")

    def test_ui_core_endpoints_return_without_full_event_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Fast UI endpoints", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                for index in range(3000):
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": f"event-{index}"})
                set_task_status(root, run_id, "task-001", "completed", exit_code=0)

                responses = {
                    target: asyncio.run(fetch_ui_response(root, run_id, target))
                    for target in ("/", "/static/app.js", "/api/status", "/api/events?offset=-1")
                }

        for response in responses.values():
            self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        events_body = json_response_body(responses["/api/events?offset=-1"])
        self.assertEqual(events_body["events"], [])
        self.assertGreater(events_body["offset"], 0)
        status_body = json_response_body(responses["/api/status"])
        self.assertEqual(status_body["tasks"][0]["display_status"], "completed")

    def test_ui_gzips_large_static_and_json_responses_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Gzip UI", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                script_response = asyncio.run(
                    fetch_ui_response(root, run_id, "/static/app.js", headers={"Accept-Encoding": "gzip"})
                )
                status_response = asyncio.run(
                    fetch_ui_response(root, run_id, "/api/status?lite=1", headers={"Accept-Encoding": "gzip"})
                )

        self.assertIn(b"Content-Encoding: gzip\r\n", script_response)
        self.assertIn(b"Content-Encoding: gzip\r\n", status_response)
        script_body = gzip.decompress(script_response.split(b"\r\n\r\n", 1)[1]).decode("utf-8")
        status_body = json.loads(gzip.decompress(status_response.split(b"\r\n\r\n", 1)[1]).decode("utf-8"))
        self.assertIn("conversationPageLimit", script_body)
        self.assertEqual(status_body["run_id"], run_id)

    def test_api_events_uses_snapshot_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Paged events", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                for index in range(10):
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": f"event-{index}"})

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/events?offset=0&limit=3"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(len(body["events"]), 3)
        self.assertEqual(body["limit"], 3)
        self.assertTrue(body["has_more"])
        self.assertLess(body["offset"], body["snapshot_offset"])

    def test_web_task_creation_autostarts_dispatched_main_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task create autostart", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with mock.patch("aha_cli.web.server.start_backend", return_value={"status": "running", "started": True}) as start_backend:
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

    def test_conversation_events_page_filters_and_pages_by_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            append_event(root, run_id, "message", {"task_id": "task-001", "sender": "browser", "to_agent": "main", "message": "one"})
            append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "two"})
            append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "sub-001", "text": "sub"})
            append_event(root, run_id, "agent_message", {"task_id": "task-002", "target": "main", "text": "other task"})

            latest = conversation_events_page(root, run_id, "task-001", "main", limit=1)
            append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "new realtime"})
            realtime, _ = iter_jsonl_from(event_path(root, run_id), latest["after_offset"])
            older = conversation_events_page(root, run_id, "task-001", "main", limit=1, before=latest["next_before_offset"])

        self.assertEqual(latest["count"], 1)
        self.assertTrue(latest["has_more"])
        self.assertEqual(latest["events"][0]["data"]["text"], "two")
        self.assertEqual(realtime[0]["data"]["text"], "new realtime")
        self.assertFalse(older["has_more"])
        self.assertEqual(older["events"][0]["data"]["message"], "one")

    def test_conversation_events_page_includes_supervision_events_for_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            append_event(root, run_id, "main_reported_to_host", {"task_id": "task-001", "host_backend": "stub"})
            append_event(root, run_id, "host_decision", {"task_id": "task-001", "decision": "ask_user"})
            append_event(root, run_id, "main_applied_decision", {"task_id": "task-001", "decision": "ask_user", "applied": True})
            append_event(root, run_id, "host_decision", {"task_id": "task-002", "decision": "stop"})

            main_page = conversation_events_page(root, run_id, "task-001", "main", limit=10)
            sub_page = conversation_events_page(root, run_id, "task-001", "sub-001", limit=10)

        self.assertEqual(
            [event["type"] for event in main_page["events"]],
            ["main_reported_to_host", "host_decision", "main_applied_decision"],
        )
        self.assertEqual(sub_page["events"], [])

    def test_conversation_events_page_shares_host_forwarding_but_hides_aha_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            append_event(
                root,
                run_id,
                "message",
                {
                    "task_id": "task-001",
                    "sender": "main",
                    "target": "host",
                    "from_agent": "main",
                    "to_agent": "host",
                    "agent_id": "host",
                    "display_sender": "main",
                    "display_target": "host",
                    "message": "main reply",
                },
            )
            append_event(
                root,
                run_id,
                "message",
                {
                    "task_id": "task-001",
                    "sender": "AHA",
                    "target": "host",
                    "display_sender": "host",
                    "display_target": "host",
                    "message": "host 正在判断本轮下一步。",
                },
            )
            append_event(
                root,
                run_id,
                "message",
                {
                    "task_id": "task-001",
                    "sender": "browser",
                    "target": "main",
                    "agent_id": "host",
                    "display_sender": "host",
                    "display_target": "main",
                    "message": "next step",
                },
            )

            main_page = conversation_events_page(root, run_id, "task-001", "main", limit=10)
            host_page = conversation_events_page(root, run_id, "task-001", "host", limit=10)

        self.assertEqual([event["data"]["message"] for event in main_page["events"]], ["main reply", "next step"])
        self.assertEqual([event["data"]["message"] for event in host_page["events"]], ["main reply", "next step"])

    def test_conversation_events_api_hides_action_envelope_agent_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Conversation action envelope", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                user_facing_response = "只展示投影后的 response"
                action_envelope = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "record_task_update",
                                "summary": "raw envelope should stay out of timeline",
                                "changed_files": [],
                                "verification": [],
                                "risks": [],
                            }
                        ],
                        "response": user_facing_response,
                    },
                    ensure_ascii=False,
                )
                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": action_envelope})
                append_event(
                    root,
                    run_id,
                    "message",
                    {"task_id": "task-001", "sender": "main", "target": "browser", "message": user_facing_response},
                )

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        timeline_texts = [str(event["data"].get("text") or event["data"].get("message") or "") for event in body["events"]]
        self.assertEqual(timeline_texts, [user_facing_response])
        self.assertNotIn(action_envelope, timeline_texts)
        self.assertFalse(any('"actions"' in text and '"response"' in text for text in timeline_texts))

    def test_web_restart_api_schedules_source_ui_on_8766(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Restart web", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="scheduled\n", stderr="")
                with mock.patch("aha_cli.web.server.subprocess.run", return_value=completed) as run_command:
                    response = asyncio.run(
                        fetch_ui_response(
                            root,
                            run_id,
                            "/api/web/restart",
                            method="POST",
                            payload={"host": "0.0.0.0", "port": 8766},
                        )
                    )
                body = json_response_body(response)
                rows, _ = iter_jsonl_from(event_path(root, run_id), 0)
                events = [row["type"] for row in rows]

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["host"], "0.0.0.0")
        self.assertEqual(body["port"], 8766)
        self.assertEqual(body["service_unit"], "aha-ui-source-8766.service")
        command = run_command.call_args.args[0]
        self.assertEqual(command[0], "systemd-run")
        self.assertIn("--on-active=1s", command)
        command_text = " ".join(command)
        self.assertIn(str(root), command_text)
        self.assertIn("0.0.0.0", command_text)
        self.assertIn("8766", command_text)
        self.assertIn("systemctl --user restart aha-ui-source-8766.service", command_text)
        self.assertIn("web_restart_requested", events)

    def test_conversation_events_api_restores_latest_turn_metrics_outside_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Conversation prompt metrics", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, run_id, "agent_started", {"task_id": "task-001", "target": "main", "sender": "browser"})
                append_event(
                    root,
                    run_id,
                    "agent_prompt_metrics",
                    {
                        "task_id": "task-001",
                        "target": "main",
                        "source": "codex-chat",
                        "total": {"chars": 1234, "bytes": 1234, "lines": 12},
                        "components": {"status_snapshot": {"chars": 1000, "bytes": 1000, "lines": 1}},
                    },
                )
                append_event(root, run_id, "agent_thread", {"task_id": "task-001", "target": "main", "thread_id": "thread-1"})
                append_event(root, run_id, "agent_usage", {"task_id": "task-001", "target": "main", "usage": {"input_tokens": 10}})
                append_event(root, run_id, "agent_finished", {"task_id": "task-001", "target": "main", "exit_code": 0})
                for index in range(10):
                    append_event(
                        root,
                        run_id,
                        "agent_command_finished",
                        {"task_id": "task-001", "target": "main", "command": f"cmd-{index}", "exit_code": 0},
                    )

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=5"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertNotIn("agent_prompt_metrics", [event["type"] for event in body["events"]])
        turn_event_types = [event["type"] for event in body["turn_events"]]
        self.assertEqual(turn_event_types, ["agent_started", "agent_prompt_metrics", "agent_thread", "agent_usage", "agent_finished"])
        metrics = next(event for event in body["turn_events"] if event["type"] == "agent_prompt_metrics")
        self.assertEqual(metrics["data"]["total"]["chars"], 1234)

    def test_conversation_events_api_filters_categories_server_side(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Conversation categories", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "hello", sender="browser", task_id="task-001", role="main")
                append_event(root, run_id, "agent_command_started", {"task_id": "task-001", "target": "main", "command": "pwd"})
                append_event(root, run_id, "agent_command_finished", {"task_id": "task-001", "target": "main", "command": "pwd", "exit_code": 0, "output_tail": "large output"})
                append_event(root, run_id, "agent_usage", {"task_id": "task-001", "target": "main", "usage": {"input_tokens": 10}})
                append_event(root, run_id, "task_status_changed", {"task_id": "task-001", "status": "running"})

                chat_response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20&categories=chat"))
                command_response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20&categories=chat,commands"))
                full_command_response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20&categories=commands&include_command_output=1"))
                none_response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20&categories=none"))

        chat_body = json_response_body(chat_response)
        command_body = json_response_body(command_response)
        full_command_body = json_response_body(full_command_response)
        none_body = json_response_body(none_response)
        self.assertEqual([event["type"] for event in chat_body["events"]], ["message"])
        self.assertEqual(
            [event["type"] for event in command_body["events"]],
            ["message", "agent_command_started", "agent_command_finished"],
        )
        finished = command_body["events"][-1]["data"]
        self.assertNotIn("output_tail", finished)
        self.assertTrue(finished["output_tail_omitted"])
        self.assertEqual(finished["output_tail_chars"], len("large output"))
        self.assertEqual(full_command_body["events"][-1]["data"]["output_tail"], "large output")
        self.assertEqual(none_body["events"], [])

    def test_events_api_replays_from_saved_offset_after_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Replay events", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                initial = json_response_body(asyncio.run(fetch_ui_response(root, run_id, "/api/events?offset=-1")))
                last_event_id = initial["offset"]

                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "missed-1"})
                append_event(root, run_id, "agent_finished", {"task_id": "task-001", "target": "main", "exit_code": 0})

                first_page = json_response_body(
                    asyncio.run(fetch_ui_response(root, run_id, f"/api/events?offset={last_event_id}&limit=1"))
                )
                replay = json_response_body(asyncio.run(fetch_ui_response(root, run_id, f"/api/events?offset={last_event_id}&limit=10")))

            self.assertEqual(first_page["events"][0]["data"]["text"], "missed-1")
            self.assertTrue(first_page["has_more"])
            self.assertGreater(first_page["offset"], last_event_id)
            self.assertEqual([event["type"] for event in replay["events"]], ["agent_message", "agent_finished"])
            self.assertEqual(replay["events"][1]["data"]["exit_code"], 0)
            self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "pending")

    def test_reverse_jsonl_reader_pages_by_byte_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            for index in range(5):
                append_event(root, run_id, "message", {"task_id": "task-001", "sender": "browser", "to_agent": "main", "message": f"line-{index}-" + ("x" * 40)})

            path = event_path(root, run_id)
            newest = list(iter_jsonl_reverse(path, chunk_size=32))
            older = list(iter_jsonl_reverse(path, before=newest[0][0], chunk_size=32))

        self.assertEqual(newest[0][1]["data"]["message"].split("-", 2)[:2], ["line", "4"])
        self.assertEqual(older[0][1]["data"]["message"].split("-", 2)[:2], ["line", "3"])
        self.assertGreater(newest[0][0], older[0][0])

    def test_task_log_page_tails_and_pages_by_byte_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Logs", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                task = task_snapshot(root, run_id, "task-001")["task"]
                log_path = run_dir(root, run_id) / task["log_file"]
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("\n".join(f"line-{index}" for index in range(5)) + "\n", encoding="utf-8")

                latest = task_log_page(root, run_id, "task-001", limit=2)
                older = task_log_page(root, run_id, "task-001", limit=2, before=latest["next_before_offset"])

        self.assertEqual(latest["text"], "line-3\nline-4")
        self.assertTrue(latest["has_more"])
        self.assertEqual(older["text"], "line-1\nline-2")
        self.assertTrue(older["has_more"])

    def test_task_log_page_falls_back_to_event_log_when_task_log_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Event logs", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "first"})
                append_event(root, run_id, "agent_message", {"task_id": "task-002", "target": "main", "text": "other task"})
                append_event(root, run_id, "agent_command_finished", {"task_id": "task-001", "target": "main", "command": "pwd", "output_tail": "second"})

                latest = task_log_page(root, run_id, "task-001", limit=1)
                older = task_log_page(root, run_id, "task-001", limit=1, before=latest["next_before_offset"], source=latest["source"])

        self.assertEqual(latest["source"], "events")
        self.assertIn("agent_command_finished", latest["text"])
        self.assertIn("second", latest["text"])
        self.assertNotIn("other task", latest["text"])
        self.assertEqual(older["source"], "events")
        self.assertIn("first", older["text"])

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
