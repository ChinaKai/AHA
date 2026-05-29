from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.web.task_routes import route_task_agent_request


class WebTaskRouteTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def route(self, root: Path, run_id: str, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload or {}).encode("utf-8")
        return route_task_agent_request(root, run_id, method, path, {}, body)

    def test_task_agent_routes_return_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Task routes", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                detail = self.route(root, run_id, "GET", "/api/task/task-001")
                created = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/tasks",
                    {"title": "Created through route", "backend": "stub", "dispatch": False},
                )
                agent = self.route(root, run_id, "POST", "/api/agents", {"task_id": "task-001", "backend": "stub"})
                agent_config = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/agent-config",
                    {"task_id": "task-001", "agent_id": agent["payload"]["agent"]["id"], "sandbox": "read-only"},
                )
                task_config = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/task-config",
                    {"task_id": "task-001", "proxy_enabled": True, "http_proxy": "http://proxy.local:8080"},
                )
                sent = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/send",
                    {"target": "main", "task_id": "task-001", "role": "main", "sender": "browser", "message": "hello"},
                )
                hidden = self.route(root, run_id, "POST", "/api/task/task-001/hide")

        self.assertEqual(detail["status"], "200 OK")
        self.assertEqual(detail["payload"]["task"]["id"], "task-001")
        self.assertEqual(created["payload"]["task"]["title"], "Created through route")
        self.assertEqual(agent["payload"]["agent"]["role"], "sub")
        self.assertEqual(agent_config["payload"]["agent"]["sandbox"], "read-only")
        self.assertTrue(task_config["payload"]["task"]["preferred_proxy_enabled"])
        self.assertEqual(sent["payload"]["message"]["message"], "hello")
        self.assertTrue(hidden["payload"]["task"]["hidden"])

    def test_ui_server_runs_task_routes_off_event_loop(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "src" / "aha_cli" / "web" / "server.py").read_text(encoding="utf-8")

        self.assertIn("asyncio.to_thread(route_task_agent_request", source)


if __name__ == "__main__":
    unittest.main()
