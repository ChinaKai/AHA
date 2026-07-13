from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.services.local_terminal import LocalTerminalSession, normalize_terminal_size
from aha_cli.store.io import write_json
from aha_cli.web.local_terminal import local_terminal_peer_allowed
from aha_cli.web.server import handle_ui_client


class LocalTerminalTests(unittest.TestCase):
    @staticmethod
    async def _terminal_handshake(
        root: Path,
        target: str = "/ws/terminal?run_id=run-1",
        auth_token: str = "",
        patch_peer_allowed: bool | None = None,
    ) -> tuple[bytes, list[tuple[str, str]]]:
        calls: list[tuple[str, str]] = []

        async def fake_terminal_handler(call_root: Path, call_run_id: str, call_target: str, _reader, writer) -> None:
            calls.append((str(call_root), call_run_id))
            assert "/ws/terminal" in call_target
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(
            lambda reader, writer: handle_ui_client(root, "run-1", reader, writer, auth_token),
            "127.0.0.1",
            0,
        )
        host, port = server.sockets[0].getsockname()
        peer_patch = (
            mock.patch("aha_cli.web.server.local_terminal_peer_allowed", return_value=patch_peer_allowed)
            if patch_peer_allowed is not None
            else contextlib.nullcontext()
        )
        try:
            with peer_patch, mock.patch("aha_cli.web.server.handle_local_terminal_ws_connection", side_effect=fake_terminal_handler):
                reader, writer = await asyncio.open_connection(host, port)
                writer.write(
                    (
                        f"GET {target} HTTP/1.1\r\n"
                        "Host: test\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                        "Sec-WebSocket-Version: 13\r\n"
                        "\r\n"
                    ).encode("ascii")
                )
                await writer.drain()
                response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1)
                await asyncio.sleep(0)
                writer.close()
                await writer.wait_closed()
                return response, calls
        finally:
            server.close()
            await server.wait_closed()

    @staticmethod
    def _write_run_plan(root: Path) -> None:
        write_json(
            root / "runs" / "run-1" / "plan.json",
            {
                "id": "run-1",
                "goal": "Local terminal",
                "mode": "research",
                "created_at": "2026-07-13T00:00:00+00:00",
                "updated_at": "2026-07-13T00:00:00+00:00",
                "tasks": [],
            },
        )

    def test_normalize_terminal_size_bounds_values(self) -> None:
        self.assertEqual(normalize_terminal_size("10", "3"), (20, 8))
        self.assertEqual(normalize_terminal_size("999", "999"), (240, 80))
        self.assertEqual(normalize_terminal_size("100", "28"), (100, 28))

    def test_local_terminal_peer_allows_loopback_only(self) -> None:
        self.assertTrue(local_terminal_peer_allowed(("127.0.0.1", 1234)))
        self.assertTrue(local_terminal_peer_allowed(("::1", 1234, 0, 0)))
        self.assertFalse(local_terminal_peer_allowed(("192.168.1.2", 1234)))

    def test_local_terminal_session_streams_shell_output(self) -> None:
        async def run_session() -> str:
            session = LocalTerminalSession(shell="/bin/sh")
            loop = asyncio.get_running_loop()
            session.start(cols=80, rows=24)
            session.attach_reader(loop)
            try:
                session.write('printf "AHA_TERMINAL_OK %s\\n" "$TERM"\nexit\n')
                output = ""
                for _ in range(20):
                    chunk = await asyncio.wait_for(session.read(), timeout=1.0)
                    if chunk is None:
                        break
                    output += chunk.decode("utf-8", errors="replace")
                    if "AHA_TERMINAL_OK xterm-256color" in output:
                        break
                return output
            finally:
                session.detach_reader(loop)
                await session.terminate()

        output = asyncio.run(run_session())

        self.assertIn("AHA_TERMINAL_OK", output)
        self.assertIn("AHA_TERMINAL_OK xterm-256color", output)

    def test_ui_server_routes_terminal_websocket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            self._write_run_plan(root)

            response, calls = asyncio.run(self._terminal_handshake(root))

        self.assertTrue(response.startswith(b"HTTP/1.1 101 Switching Protocols"))
        self.assertEqual(calls, [(str(root), "run-1")])

    def test_terminal_websocket_rejects_remote_peer_without_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            self._write_run_plan(root)

            response, calls = asyncio.run(self._terminal_handshake(root, patch_peer_allowed=False))

        self.assertTrue(response.startswith(b"HTTP/1.1 403 Forbidden"))
        self.assertEqual(calls, [])

    def test_terminal_websocket_allows_authenticated_remote_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            self._write_run_plan(root)

            response, calls = asyncio.run(
                self._terminal_handshake(
                    root,
                    target="/ws/terminal?run_id=run-1&token=secret",
                    auth_token="secret",
                    patch_peer_allowed=False,
                )
            )

        self.assertTrue(response.startswith(b"HTTP/1.1 101 Switching Protocols"))
        self.assertEqual(calls, [(str(root), "run-1")])
