from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from aha_cli.store.io import write_json
from aha_cli.web.browser_session import browser_options_response, handle_browser_session_ws_connection
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


class BrowserSessionTests(unittest.TestCase):
    def test_browser_options_detection_runs_outside_the_event_loop(self) -> None:
        detection_threads: list[int] = []

        def detect() -> dict:
            detection_threads.append(threading.get_ident())
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return {"chrome": False, "msedge": False, "chromium": True}

        event_loop_thread = threading.get_ident()
        with mock.patch("aha_cli.web.browser_session.available_browser_channels", side_effect=detect):
            response = asyncio.run(browser_options_response("GET", "/api/browser/options"))

        self.assertTrue(response and response.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn(b'"chromium": true', response or b"")
        self.assertEqual(len(detection_threads), 1)
        self.assertNotEqual(detection_threads[0], event_loop_thread)

    def test_native_browser_websocket_subscribes_to_page_frames(self) -> None:
        sent: list[dict] = []
        ipc_writer = _IpcWriter()

        async def fake_send(_writer, message: str) -> None:
            sent.append(json.loads(message))

        async def run() -> None:
            browser_writer = _Writer()

            async def fake_read(_reader) -> str:
                await asyncio.sleep(0)
                return json.dumps({"type": "close"})

            ipc_reader = asyncio.StreamReader()
            with (
                mock.patch(
                    "aha_cli.web.browser_session.open_browser_bridge_ipc",
                    return_value=(
                        ipc_reader,
                        ipc_writer,
                        {
                            "type": "ready",
                            "state": {
                                "status": "running",
                                "display": {"requested": "native", "active": "native"},
                            },
                        },
                    ),
                ),
                mock.patch("aha_cli.web.browser_session.ws_read_text", side_effect=fake_read),
                mock.patch("aha_cli.web.browser_session.ws_send_text", side_effect=fake_send),
            ):
                await handle_browser_session_ws_connection(
                    Path("/tmp/aha"),
                    "run-1",
                    "/ws/browser-session?task_id=task-1",
                    asyncio.StreamReader(),
                    browser_writer,
                )

        asyncio.run(run())

        ipc_frames = [json.loads(line) for line in bytes(ipc_writer.data).splitlines()]
        self.assertEqual(len(ipc_frames), 1)
        self.assertEqual(ipc_frames[0]["action"], "subscribe")
        self.assertEqual(ipc_frames[0]["source"], "user")
        self.assertEqual(ipc_frames[0]["agent_id"], "browser")
        self.assertEqual(sent[0]["state"]["display"]["active"], "native")

    def test_websocket_proxies_commands_as_user_actions(self) -> None:
        sent: list[dict] = []
        ipc_writer = _IpcWriter()

        async def fake_send(_writer, message: str) -> None:
            sent.append(json.loads(message))

        async def run() -> None:
            browser_writer = _Writer()
            messages = iter(
                [
                    json.dumps(
                        {
                            "type": "command",
                            "id": "cmd-1",
                            "action": "navigate",
                            "args": {"url": "https://example.com"},
                            "source": "agent",
                            "agent_id": "untrusted",
                        }
                    ),
                    json.dumps({"type": "close"}),
                ]
            )

            async def fake_read(_reader) -> str:
                await asyncio.sleep(0)
                return next(messages)

            ipc_reader = asyncio.StreamReader()
            ipc_reader.feed_data(b'{"type":"event","event":"state","state":{"status":"running"}}\n')
            with (
                mock.patch(
                    "aha_cli.web.browser_session.open_browser_bridge_ipc",
                    return_value=(
                        ipc_reader,
                        ipc_writer,
                        {"type": "ready", "state": {"status": "running"}},
                    ),
                ),
                mock.patch("aha_cli.web.browser_session.ws_read_text", side_effect=fake_read),
                mock.patch("aha_cli.web.browser_session.ws_send_text", side_effect=fake_send),
            ):
                await handle_browser_session_ws_connection(
                    Path("/tmp/aha"),
                    "run-1",
                    "/ws/browser-session?task_id=task-1",
                    asyncio.StreamReader(),
                    browser_writer,
                )
            self.assertTrue(browser_writer.closed)

        asyncio.run(run())
        ipc_frames = [json.loads(line) for line in bytes(ipc_writer.data).splitlines()]
        command = next(frame for frame in ipc_frames if frame.get("id") == "cmd-1")
        self.assertEqual(command["action"], "navigate")
        self.assertEqual(command["source"], "user")
        self.assertEqual(command["agent_id"], "browser")
        self.assertEqual(sent[0]["type"], "ready")
        self.assertTrue(any(item.get("event") == "state" for item in sent))

    def test_server_routes_browser_session_websocket(self) -> None:
        async def run() -> tuple[bytes, list[str]]:
            calls: list[str] = []
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_json(
                    root / "runs" / "run-1" / "plan.json",
                    {
                        "id": "run-1",
                        "goal": "Shared browser",
                        "mode": "research",
                        "created_at": "2026-07-29T00:00:00+00:00",
                        "updated_at": "2026-07-29T00:00:00+00:00",
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
                    with mock.patch(
                        "aha_cli.web.server.handle_browser_session_ws_connection",
                        side_effect=fake_handler,
                    ):
                        reader, writer = await asyncio.open_connection(host, port)
                        writer.write(
                            (
                                "GET /ws/browser-session?run_id=run-1&task_id=task-1 HTTP/1.1\r\n"
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
        self.assertIn("/ws/browser-session", calls[0])


if __name__ == "__main__":
    unittest.main()
