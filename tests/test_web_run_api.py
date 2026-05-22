from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main
from aha_cli.store.filesystem import append_event, inbox_path, iter_jsonl_from, read_json, run_dir, status_snapshot
from tests.helpers import fetch_ui_response, json_response_body


class WebRunApiTests(unittest.TestCase):
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
