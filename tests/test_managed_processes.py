from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.managed_processes import (
    ManagedProcessConflict,
    ManagedProcessError,
    list_managed_processes,
    managed_process_status,
    reconcile_managed_processes,
    start_managed_process,
    stop_managed_process,
)
from aha_cli.store.filesystem import set_task_status
from tests.helpers import fetch_ui_response, json_response_body


class ManagedProcessTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def create_run(self, root: Path) -> str:
        with mock.patch("pathlib.Path.cwd", return_value=root):
            self.run_cli("init", "--portable", "--backend", "stub")
            code, output = self.run_cli("plan", "Managed process", "--agents", "1")
        self.assertEqual(code, 0)
        return output.splitlines()[0].split(": ", 1)[1]

    @staticmethod
    def sleeper_command() -> list[str]:
        return [sys.executable, "-u", "-c", "import time; print('ready', flush=True); time.sleep(30)"]

    def test_managed_process_survives_caller_and_stops_as_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            try:
                started = start_managed_process(
                    root,
                    run_id,
                    "task-001",
                    "main",
                    "preview",
                    self.sleeper_command(),
                )
                duplicate = start_managed_process(
                    root,
                    run_id,
                    "task-001",
                    "main",
                    "preview",
                    self.sleeper_command(),
                )
                status = managed_process_status(root, run_id, "task-001", "main", "preview")
                listed = list_managed_processes(root, run_id, "task-001", "main")
                with self.assertRaises(ManagedProcessConflict):
                    start_managed_process(
                        root,
                        run_id,
                        "task-001",
                        "main",
                        "preview",
                        [sys.executable, "-c", "print('different')"],
                    )
            finally:
                stopped = stop_managed_process(root, run_id, "task-001", "main", "preview")

            self.assertTrue(started["started"])
            self.assertTrue(started["alive"])
            self.assertTrue(duplicate["already_running"])
            self.assertTrue(status["alive"])
            self.assertEqual([item["name"] for item in listed], ["preview"])
            self.assertFalse(stopped["alive"])
            self.assertEqual(stopped["status"], "stopped")
            deadline = time.monotonic() + 2
            log_path = Path(started["log_path"])
            while time.monotonic() < deadline and "ready" not in log_path.read_text(encoding="utf-8", errors="replace"):
                time.sleep(0.02)
            self.assertIn("ready", log_path.read_text(encoding="utf-8", errors="replace"))

    def test_managed_process_rejects_cwd_outside_task_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            run_id = self.create_run(root)

            with self.assertRaises(ManagedProcessError):
                start_managed_process(
                    root,
                    run_id,
                    "task-001",
                    "main",
                    "escape",
                    self.sleeper_command(),
                    cwd=root.parent,
                )

    def test_terminal_task_reconciliation_stops_managed_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            start_managed_process(root, run_id, "task-001", "main", "terminal", self.sleeper_command())

            set_task_status(root, run_id, "task-001", "failed", 1)
            stopped = reconcile_managed_processes(root)
            status = managed_process_status(root, run_id, "task-001", "main", "terminal")

        self.assertEqual(stopped, 1)
        self.assertFalse(status["alive"])

    def test_web_route_starts_reports_and_stops_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            payload = {
                "run_id": run_id,
                "task_id": "task-001",
                "agent_id": "main",
                "name": "web-preview",
                "command": self.sleeper_command(),
            }
            try:
                started_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/managed-processes",
                        method="POST",
                        payload=payload,
                        auth_token="configured-but-loopback-is-trusted",
                    )
                )
                started = json_response_body(started_response)
                status_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/managed-processes?run_id={run_id}&task_id=task-001&agent_id=main&name=web-preview",
                        auth_token="configured-but-loopback-is-trusted",
                    )
                )
                status = json_response_body(status_response)
            finally:
                stopped_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/managed-processes",
                        method="DELETE",
                        payload=payload,
                        auth_token="configured-but-loopback-is-trusted",
                    )
                )
                stopped = json_response_body(stopped_response)

            self.assertTrue(started_response.startswith(b"HTTP/1.1 201 Created"))
            self.assertTrue(started["process"]["alive"])
            self.assertTrue(status["process"]["alive"])
            self.assertFalse(stopped["process"]["alive"])

    def test_web_route_rejects_unauthenticated_non_loopback_process_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            payload = {
                "run_id": run_id,
                "task_id": "task-001",
                "agent_id": "main",
                "name": "remote-preview",
                "command": self.sleeper_command(),
            }
            with mock.patch("aha_cli.web.server.local_terminal_peer_allowed", return_value=False):
                response = asyncio.run(
                    fetch_ui_response(root, run_id, "/api/managed-processes", method="POST", payload=payload)
                )

        self.assertTrue(response.startswith(b"HTTP/1.1 403 Forbidden"))
        self.assertIn("loopback access or Web auth", json_response_body(response)["error"])

    def test_cli_uses_backend_scope_environment(self) -> None:
        response = {"ok": True, "process": {"name": "preview", "status": "running"}}
        with (
            mock.patch.dict(
                "os.environ",
                {"AHA_RUN_ID": "run-001", "AHA_TASK_ID": "task-001", "AHA_AGENT_ID": "main"},
                clear=False,
            ),
            mock.patch("aha_cli.cli.managed_process_request", return_value=response) as request,
        ):
            code, output = self.run_cli(
                "managed-process",
                "start",
                "preview",
                "--cwd",
                ".",
                "--",
                sys.executable,
                "-m",
                "http.server",
                "8790",
            )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), response)
        self.assertEqual(request.call_args.kwargs["run_id"], "run-001")
        self.assertEqual(request.call_args.kwargs["task_id"], "task-001")
        self.assertEqual(request.call_args.kwargs["command"], [sys.executable, "-m", "http.server", "8790"])


if __name__ == "__main__":
    unittest.main()
