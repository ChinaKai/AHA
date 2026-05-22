from __future__ import annotations

import asyncio
import gzip
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main
from aha_cli.services.chat import chat_offset_path
from aha_cli.store.filesystem import (
    append_event,
    inbox_path,
    iter_jsonl_from,
    read_json,
    run_dir,
    set_task_status,
    status_snapshot,
    task_context_snapshot,
    update_task_supervision_config,
)
from aha_cli.web.server import handle_send_payload
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
