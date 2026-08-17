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
from aha_cli.services.hardware_file_transfer import HardwareFileTransferResult
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
                self.assertEqual(query, {"run_id": run_id})
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
                self.assertEqual(query, {"run_id": run_id})
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

    def test_cmd_hardware_attach_forwards_to_windows_web_and_tails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)

            def fake_forward(_root, method, path, *, query=None, payload=None, web_token=None, timeout=15.0):
                self.assertEqual(method, "POST")
                self.assertEqual(path, "/api/task/task-001/hardware-attach")
                self.assertEqual(query, {"run_id": run_id})
                self.assertEqual(payload["channel"], "serial")
                return {"ok": True, "transport": "serial", "endpoint": "COM6", "bridge": {"status": "running"}}

            with (
                mock.patch("aha_cli.cli.should_forward_to_windows_web", return_value=True),
                mock.patch("aha_cli.cli.web_forward", side_effect=fake_forward) as forward,
                mock.patch("aha_cli.cli._tail_device_stream") as tail,
            ):
                code, output = self.run_cli(
                    "hardware-attach",
                    run_id,
                    "task-001",
                    "--channel",
                    "serial",
                    aha_home=root,
                )

            self.assertEqual(code, 0)
            self.assertIn("Windows AHA bridge owns COM6", output)
            forward.assert_called_once()
            tail.assert_called_once_with(root, "COM6", replay=False)

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

    def test_cmd_hardware_file_send_uses_forwarded_bridge_status_and_literal_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)
            source = Path(tmp) / "payload.bin"
            source.write_bytes(b"payload")
            requests: list[tuple[str, dict | None, dict]] = []

            def fake_forward(_root, method, path, *, query=None, payload=None, web_token=None, timeout=30.0):
                self.assertEqual(method, "POST")
                requests.append((path, query, payload or {}))
                if path.endswith("/hardware-attach"):
                    return {"ok": True, "bridge": {"status": "running"}}
                return {"ok": True}

            def fake_transfer(_root, _device, source_path, destination, *, send_text, **_kwargs):
                send_text("D0:\\0000\n")
                return HardwareFileTransferResult(
                    source=str(source_path),
                    destination=destination,
                    size=7,
                    sha256="239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5",
                    chunks=1,
                    retries=0,
                    elapsed_seconds=0.1,
                )

            with (
                mock.patch("aha_cli.cli.should_forward_to_windows_web", return_value=True),
                mock.patch("aha_cli.cli.web_forward", side_effect=fake_forward),
                mock.patch("aha_cli.cli._task_hardware_write_allowed", return_value=True),
                mock.patch("aha_cli.cli._bridge_target", return_value=("COM6", 115200)),
                mock.patch("aha_cli.cli.send_file_via_shell", side_effect=fake_transfer),
            ):
                code, output = self.run_cli(
                    "hardware-file-send",
                    run_id,
                    "task-001",
                    str(source),
                    "/tmp/payload.bin",
                    "--quiet",
                    "--json",
                    aha_home=root,
                )

            self.assertEqual(code, 0)
            self.assertTrue(json.loads(output)["ok"])
            self.assertEqual(requests[0][0], "/api/task/task-001/hardware-attach")
            self.assertEqual(requests[0][1], {"run_id": run_id})
            self.assertEqual(requests[1][0], "/api/task/task-001/hardware-send")
            self.assertEqual(requests[1][1], {"run_id": run_id})
            self.assertEqual(requests[1][2]["data"], "D0:\\\\0000\n")

    def test_cmd_hardware_file_send_supports_network_web_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)
            source = Path(tmp) / "payload.bin"
            source.write_bytes(b"payload")
            requests: list[tuple[str, dict | None, dict]] = []

            def fake_forward(_root, method, path, *, query=None, payload=None, web_token=None, timeout=30.0):
                self.assertEqual(method, "POST")
                requests.append((path, query, payload or {}))
                if path.endswith("/hardware-attach"):
                    return {"ok": True, "bridge": {"status": "running"}}
                return {"ok": True}

            def fake_transfer(_root, endpoint, source_path, destination, *, send_text, stream_path=None, **_kwargs):
                self.assertEqual(endpoint, "192.168.1.20:23")
                self.assertIsNotNone(stream_path)
                send_text("D0:\\0000\n")
                return HardwareFileTransferResult(
                    source=str(source_path),
                    destination=destination,
                    size=7,
                    sha256="239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5",
                    chunks=1,
                    retries=0,
                    elapsed_seconds=0.1,
                )

            with (
                mock.patch("aha_cli.cli.should_forward_to_windows_web", return_value=True),
                mock.patch("aha_cli.cli.web_forward", side_effect=fake_forward),
                mock.patch("aha_cli.cli._task_hardware_write_allowed", return_value=True),
                mock.patch("aha_cli.cli._network_bridge_target", return_value=("192.168.1.20", 23, "root", "secret")),
                mock.patch("aha_cli.cli.send_file_via_shell", side_effect=fake_transfer),
            ):
                code, output = self.run_cli(
                    "hardware-file-send",
                    run_id,
                    "task-001",
                    str(source),
                    "/tmp/payload.bin",
                    "--channel",
                    "network",
                    "--quiet",
                    "--json",
                    aha_home=root,
                )

            self.assertEqual(code, 0)
            result = json.loads(output)
            self.assertEqual(result["channel"], "network")
            self.assertEqual(result["endpoint"], "192.168.1.20:23")
            self.assertEqual(requests[0][0], "/api/task/task-001/hardware-attach")
            self.assertEqual(requests[0][1], {"run_id": run_id})
            self.assertEqual(requests[0][2]["channel"], "network")
            self.assertEqual(requests[1][0], "/api/task/task-001/hardware-send")
            self.assertEqual(requests[1][1], {"run_id": run_id})
            self.assertEqual(requests[1][2]["channel"], "network")
            self.assertEqual(requests[1][2]["data"], "D0:\\\\0000\n")

    def test_cmd_hardware_file_send_uses_raw_v3_for_capable_forwarded_serial_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)
            source = Path(tmp) / "payload.bin"
            source.write_bytes(b"payload")
            controls: list[dict] = []

            def fake_forward(_root, method, path, *, query=None, payload=None, web_token=None, timeout=30.0):
                self.assertEqual(method, "POST")
                self.assertTrue(path.endswith("/hardware-attach"))
                self.assertEqual(query, {"run_id": run_id})
                return {
                    "ok": True,
                    "bridge": {"status": "running", "capabilities": ["serial-transfer-v1", "serial-transfer-v2"]},
                }

            def fake_raw_transfer(
                _root,
                _device,
                source_path,
                destination,
                *,
                send_text,
                send_bytes,
                chunk_size,
                **_kwargs,
            ):
                self.assertEqual(chunk_size, 16 * 1024)
                send_text("F 0 1 deadbeef\n")
                send_bytes(b"\x00\xff")
                return HardwareFileTransferResult(
                    source=str(source_path),
                    destination=destination,
                    size=7,
                    sha256="239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5",
                    chunks=1,
                    retries=0,
                    elapsed_seconds=0.1,
                )

            with (
                mock.patch("aha_cli.cli.should_forward_to_windows_web", return_value=True),
                mock.patch("aha_cli.cli.web_forward", side_effect=fake_forward),
                mock.patch("aha_cli.cli._task_hardware_write_allowed", return_value=True),
                mock.patch("aha_cli.cli._bridge_target", return_value=("COM6", 115200)),
                mock.patch("aha_cli.cli.append_bridge_control", side_effect=lambda _root, _device, command: controls.append(command)),
                mock.patch("aha_cli.cli.send_file_via_raw_shell", side_effect=fake_raw_transfer),
            ):
                code, output = self.run_cli(
                    "hardware-file-send",
                    run_id,
                    "task-001",
                    str(source),
                    "/tmp/payload.bin",
                    "--quiet",
                    "--json",
                    aha_home=root,
                )

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["receiver"], "shell-raw-v3")
            self.assertEqual([item["cmd"] for item in controls], [
                "transfer_begin",
                "transfer_send",
                "transfer_send_bytes",
                "transfer_end",
            ])
            self.assertEqual(controls[2]["data"], "AP8=")


if __name__ == "__main__":
    unittest.main()
