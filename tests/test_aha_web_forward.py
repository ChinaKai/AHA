from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.aha_http_client import web_forward
from tests.helpers import isolated_cli_environment


class AhaWebForwardCliTests(unittest.TestCase):
    def run_cli(self, *args: str, aha_home: Path | None = None) -> tuple[int, str]:
        out = io.StringIO()
        env = {"AHA_HOME": str(aha_home)} if aha_home else {}
        with isolated_cli_environment(), mock.patch.dict(os.environ, env, clear=False), mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def _init_run(self, tmp: str) -> tuple[Path, str]:
        root = Path(tmp)
        aha_root = root / ".aha"
        with mock.patch("pathlib.Path.cwd", return_value=root):
            self.run_cli("init", "--portable", aha_home=aha_root)
            code, plan_output = self.run_cli("plan", "Web forward", "--agents", "1", aha_home=aha_root)
            self.assertEqual(code, 0)
            run_id = plan_output.splitlines()[0].split(": ", 1)[1]
        return aha_root, run_id

    def test_cmd_browser_forwards_to_windows_web(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)

            def fake_forward(_root, method, path, *, query=None, payload=None, web_token=None, timeout=30.0):
                self.assertEqual(method, "POST")
                self.assertEqual(path, f"/api/task/task-001/browser-action")
                self.assertEqual(payload["action"], "navigate")
                return {"ok": True, "url": payload["args"]["url"]}

            with (
                mock.patch("aha_cli.cli.should_forward_to_windows_web", return_value=True),
                mock.patch("aha_cli.cli.web_forward", side_effect=fake_forward) as forward,
            ):
                code, output = self.run_cli(
                    "browser",
                    "navigate",
                    run_id,
                    "task-001",
                    "https://example.com",
                    aha_home=root,
                )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["url"], "https://example.com")
            forward.assert_called_once()

    def test_cmd_browser_uses_local_bridge_when_not_forwarding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)

            with (
                mock.patch("aha_cli.cli.should_forward_to_windows_web", return_value=False),
                mock.patch("aha_cli.cli.web_forward") as forward,
            ):
                # When not forwarding, cmd_browser calls the local
                # browser_bridge_request (which would hit the IPC socket).
                with mock.patch(
                    "aha_cli.cli.browser_bridge_request",
                    new_callable=mock.AsyncMock,
                    return_value={"ok": True, "url": "https://example.com"},
                ) as local:
                    code, output = self.run_cli(
                        "browser",
                        "navigate",
                        run_id,
                        "task-001",
                        "https://example.com",
                        aha_home=root,
                    )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertTrue(payload["ok"])
            forward.assert_not_called()
            local.assert_called_once()

    def test_cmd_hardware_send_forwards_to_windows_web(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)

            def fake_forward(_root, method, path, *, query=None, payload=None, web_token=None, timeout=15.0):
                self.assertEqual(method, "POST")
                self.assertEqual(path, "/api/task/task-001/hardware-send")
                self.assertEqual(payload["data"], "printenv\\r")
                return {"ok": True, "device": "COM3", "record": {"id": 1}}

            with (
                mock.patch("aha_cli.cli.should_forward_to_windows_web", return_value=True),
                mock.patch("aha_cli.cli.web_forward", side_effect=fake_forward) as forward,
                mock.patch(
                    "aha_cli.cli._task_hardware_write_allowed",
                    return_value=True,
                ),
            ):
                code, output = self.run_cli(
                    "hardware-send",
                    run_id,
                    "task-001",
                    "--data",
                    "printenv\\r",
                    aha_home=root,
                )

            self.assertEqual(code, 0)
            self.assertIn("Queued send via Windows AHA", output)
            forward.assert_called_once()

    def test_cmd_hardware_stop_forwards_to_windows_web(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)

            def fake_forward(_root, method, path, *, query=None, payload=None, web_token=None, timeout=15.0):
                self.assertEqual(path, "/api/task/task-001/hardware-stop")
                return {"ok": True, "command": "stop"}

            with (
                mock.patch("aha_cli.cli.should_forward_to_windows_web", return_value=True),
                mock.patch("aha_cli.cli.web_forward", side_effect=fake_forward) as forward,
            ):
                code, output = self.run_cli(
                    "hardware-stop",
                    run_id,
                    "task-001",
                    aha_home=root,
                )

            self.assertEqual(code, 0)
            self.assertIn("Queued stop via Windows AHA", output)
            forward.assert_called_once()

    def test_cmd_hardware_send_local_when_not_forwarding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)

            with (
                mock.patch("aha_cli.cli.should_forward_to_windows_web", return_value=False),
                mock.patch("aha_cli.cli.web_forward") as forward,
                mock.patch("aha_cli.cli._task_hardware_write_allowed", return_value=True),
            ):
                # Local path hits ensure_bridge -> append_bridge_control; no device
                # configured so it errors, proving forwarding was not used.
                with mock.patch("aha_cli.cli._bridge_target", return_value=(None, 115200)):
                    code, output = self.run_cli(
                        "hardware-send",
                        run_id,
                        "task-001",
                        "--data",
                        "x",
                        aha_home=root,
                    )

            self.assertEqual(code, 2)
            self.assertNotIn("Queued send via Windows AHA", output)
            forward.assert_not_called()


if __name__ == "__main__":
    unittest.main()
