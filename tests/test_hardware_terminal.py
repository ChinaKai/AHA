from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.store.io import write_json
from aha_cli.web.hardware_terminal import (
    handle_hardware_terminal_ws_connection,
    hardware_terminal_target,
)
from aha_cli.web.server import handle_ui_client


class _Writer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _IpcWriter(_Writer):
    def __init__(self) -> None:
        super().__init__()
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None


class HardwareTerminalTests(unittest.TestCase):
    def test_target_selects_serial_and_network_transports(self) -> None:
        task = {
            "hardware_debug": {
                "mode": "both",
                "serial": {"device": "/dev/ttyUSB0", "baudrate": 115200},
                "network": {"device_ip": "192.168.1.20"},
                "credentials": {"username": "root", "password": "secret"},
                "permissions": {"access": "read_write"},
            }
        }
        serial = hardware_terminal_target(task, "uart")
        network = hardware_terminal_target(task, "telnet")
        self.assertEqual(serial["transport"], "serial")
        self.assertEqual(serial["endpoint"], "/dev/ttyUSB0")
        self.assertEqual(serial["transports"], ["serial", "network"])
        self.assertEqual(network["transport"], "network")
        self.assertEqual(network["endpoint"], "192.168.1.20:23")

    def test_websocket_stream_preserves_ansi_and_accepts_raw_input(self) -> None:
        sent: list[dict] = []
        ipc_writer = _IpcWriter()

        async def fake_send(_writer, message: str) -> None:
            sent.append(json.loads(message))

        async def run() -> None:
            writer = _Writer()
            messages = iter(
                [
                    json.dumps({"type": "input", "data": "\\r"}),
                    json.dumps({"type": "resize", "cols": 120, "rows": 40}),
                    json.dumps({"type": "close"}),
                ]
            )

            async def fake_read(_reader) -> str:
                return next(messages)

            target = {
                "transport": "serial",
                "transports": ["serial"],
                "endpoint": "/dev/ttyUSB0",
                "device": "/dev/ttyUSB0",
                "baudrate": 115200,
            }
            initial = {"events": [{"direction": "rx", "data": "\u001b[2Jboard> "}], "after_offset": 8}
            ipc_reader = asyncio.StreamReader()
            ipc_reader.feed_data(
                b'{"type":"ready","protocol":1,"after_offset":8}\n'
                b'{"type":"output","data":"\\u001b[31mLIVE\\u001b[0m","offset":12}\n'
            )
            with (
                mock.patch(
                    "aha_cli.web.hardware_terminal.task_snapshot",
                    return_value={
                        "task": {
                            "status": "running",
                            "hardware_debug": {
                                "mode": "serial",
                                "serial": {"device": "/dev/ttyUSB0", "baudrate": 115200},
                                "permissions": {"access": "read_write"},
                            },
                        }
                    },
                ),
                mock.patch("aha_cli.web.hardware_terminal.hardware_terminal_target", return_value=target),
                mock.patch("aha_cli.web.hardware_terminal._ensure_target", return_value={"alive": True}),
                mock.patch("aha_cli.web.hardware_terminal._target_status", return_value={"alive": True, "status": "running"}),
                mock.patch("aha_cli.web.hardware_terminal._stream_page", return_value=initial),
                mock.patch(
                    "aha_cli.web.hardware_terminal._open_target_ipc",
                    return_value=(ipc_reader, ipc_writer),
                ),
                mock.patch("aha_cli.web.hardware_terminal.ws_read_text", side_effect=fake_read),
                mock.patch("aha_cli.web.hardware_terminal.ws_send_text", side_effect=fake_send),
            ):
                await handle_hardware_terminal_ws_connection(
                    Path("/tmp/aha"),
                    "run-1",
                    "/ws/hardware-terminal?task_id=task-1&transport=serial&cols=80&rows=24",
                    asyncio.StreamReader(),
                    writer,
                )
            self.assertTrue(writer.closed)

        asyncio.run(run())
        ipc_frames = [json.loads(line) for line in bytes(ipc_writer.data).splitlines()]
        self.assertIn({"type": "input", "data": "\\r"}, ipc_frames)
        self.assertIn({"type": "resize", "cols": 120, "rows": 40}, ipc_frames)
        self.assertEqual(sent[0]["type"], "ready")
        self.assertEqual(sent[1], {"type": "output", "data": "\u001b[2Jboard> "})
        self.assertIn({"type": "output", "data": "\u001b[31mLIVE\u001b[0m"}, sent)

    def test_active_read_only_websocket_streams_output_and_rejects_input(self) -> None:
        sent: list[dict] = []
        ipc_writer = _IpcWriter()

        async def fake_send(_writer, message: str) -> None:
            sent.append(json.loads(message))

        async def run() -> None:
            browser_writer = _Writer()
            messages = iter(
                [
                    json.dumps({"type": "input", "data": "version\\r"}),
                    json.dumps({"type": "close"}),
                ]
            )

            async def fake_read(_reader) -> str:
                return next(messages)

            target = {
                "transport": "serial",
                "transports": ["serial"],
                "endpoint": "/dev/ttyUSB0",
                "device": "/dev/ttyUSB0",
                "baudrate": 115200,
            }
            ipc_reader = asyncio.StreamReader()
            ipc_reader.feed_data(b'{"type":"ready","protocol":1,"after_offset":4}\n')
            task = {
                "status": "running",
                "hardware_debug": {
                    "mode": "serial",
                    "serial": {"device": "/dev/ttyUSB0", "baudrate": 115200},
                    "permissions": {"access": "read_only"},
                },
            }
            with (
                mock.patch("aha_cli.web.hardware_terminal.task_snapshot", return_value={"task": task}),
                mock.patch("aha_cli.web.hardware_terminal.hardware_terminal_target", return_value=target),
                mock.patch("aha_cli.web.hardware_terminal._ensure_target", return_value={"alive": True}) as ensure,
                mock.patch("aha_cli.web.hardware_terminal._target_status", return_value={"alive": True}),
                mock.patch(
                    "aha_cli.web.hardware_terminal._stream_page",
                    return_value={"events": [{"direction": "rx", "data": "board> "}], "after_offset": 4},
                ),
                mock.patch(
                    "aha_cli.web.hardware_terminal._open_target_ipc",
                    return_value=(ipc_reader, ipc_writer),
                ) as open_ipc,
                mock.patch("aha_cli.web.hardware_terminal.ws_read_text", side_effect=fake_read),
                mock.patch("aha_cli.web.hardware_terminal.ws_send_text", side_effect=fake_send),
            ):
                await handle_hardware_terminal_ws_connection(
                    Path("/tmp/aha"),
                    "run-1",
                    "/ws/hardware-terminal?task_id=task-1&transport=serial",
                    asyncio.StreamReader(),
                    browser_writer,
                )
            ensure.assert_called_once()
            open_ipc.assert_called_once()

        asyncio.run(run())
        ipc_frames = [json.loads(line) for line in bytes(ipc_writer.data).splitlines()]
        self.assertFalse(any(frame.get("type") == "input" for frame in ipc_frames))
        self.assertTrue(sent[0]["read_only"])
        self.assertIn({"type": "output", "data": "board> "}, sent)
        self.assertIn({"type": "error", "message": "terminal is read-only"}, sent)

    def test_server_routes_hardware_terminal_websocket(self) -> None:
        async def run() -> tuple[bytes, list[str]]:
            calls: list[str] = []
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_json(
                    root / "runs" / "run-1" / "plan.json",
                    {
                        "id": "run-1",
                        "goal": "Hardware terminal",
                        "mode": "research",
                        "created_at": "2026-07-15T00:00:00+00:00",
                        "updated_at": "2026-07-15T00:00:00+00:00",
                        "tasks": [],
                    },
                )

                async def fake_handler(_root, _run_id, target, _reader, writer) -> None:
                    calls.append(target)
                    writer.close()
                    await writer.wait_closed()

                server = await asyncio.start_server(
                    lambda reader, writer: handle_ui_client(root, "run-1", reader, writer),
                    "127.0.0.1",
                    0,
                )
                host, port = server.sockets[0].getsockname()
                try:
                    with mock.patch("aha_cli.web.server.handle_hardware_terminal_ws_connection", side_effect=fake_handler):
                        reader, writer = await asyncio.open_connection(host, port)
                        writer.write(
                            (
                                "GET /ws/hardware-terminal?run_id=run-1&task_id=task-1&transport=serial HTTP/1.1\r\n"
                                "Host: test\r\n"
                                "Upgrade: websocket\r\n"
                                "Connection: Upgrade\r\n"
                                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                                "Sec-WebSocket-Version: 13\r\n\r\n"
                            ).encode("ascii")
                        )
                        await writer.drain()
                        response = await reader.readuntil(b"\r\n\r\n")
                        writer.close()
                        await writer.wait_closed()
                finally:
                    server.close()
                    await server.wait_closed()
                return response, calls

        response, calls = asyncio.run(run())
        self.assertTrue(response.startswith(b"HTTP/1.1 101 Switching Protocols"))
        self.assertEqual(len(calls), 1)
        self.assertIn("/ws/hardware-terminal", calls[0])


if __name__ == "__main__":
    unittest.main()
