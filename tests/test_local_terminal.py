from __future__ import annotations

import asyncio
import contextlib
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from aha_cli.services.local_terminal import (
    LocalTerminalSession,
    LocalTerminalShell,
    local_terminal_shell_options,
    normalize_terminal_size,
    resolve_local_terminal_shell,
)
from aha_cli.store.io import write_json
from aha_cli.web.local_terminal import local_terminal_options_response, local_terminal_peer_allowed
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

    def test_windows_shell_options_are_detected_and_auto_prefers_pwsh(self) -> None:
        shells = {
            "pwsh": LocalTerminalShell("pwsh", "PowerShell 7", r"C:\pwsh.exe", (r"C:\pwsh.exe", "-NoLogo")),
            "powershell": LocalTerminalShell(
                "powershell",
                "Windows PowerShell",
                r"C:\powershell.exe",
                (r"C:\powershell.exe", "-NoLogo"),
            ),
            "cmd": LocalTerminalShell("cmd", "Command Prompt", r"C:\cmd.exe", (r"C:\cmd.exe", "/D", "/Q")),
            "wsl": LocalTerminalShell("wsl", "WSL", r"C:\wsl.exe", (r"C:\wsl.exe",)),
        }
        with (
            mock.patch("aha_cli.services.local_terminal._platform.is_windows", return_value=True),
            mock.patch("aha_cli.services.local_terminal._windows_terminal_shells", return_value=shells),
        ):
            payload = local_terminal_shell_options()
            selected = resolve_local_terminal_shell("auto")
            wsl = resolve_local_terminal_shell("wsl")

        self.assertEqual(payload["resolved"], "pwsh")
        self.assertEqual([item["id"] for item in payload["options"]], ["auto", "pwsh", "powershell", "cmd", "wsl"])
        self.assertEqual(selected.id, "pwsh")
        self.assertEqual(wsl.command, (r"C:\wsl.exe",))

    def test_local_terminal_shell_rejects_unknown_or_unavailable_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown local terminal shell"):
            resolve_local_terminal_shell("arbitrary.exe")
        with (
            mock.patch("aha_cli.services.local_terminal._platform.is_windows", return_value=True),
            mock.patch("aha_cli.services.local_terminal._windows_terminal_shells", return_value={}),
            self.assertRaisesRegex(ValueError, "not available"),
        ):
            resolve_local_terminal_shell("pwsh")

    def test_local_terminal_options_endpoint_runs_detection_off_event_loop(self) -> None:
        payload = {
            "default": "auto",
            "resolved": "powershell",
            "options": [{"id": "auto", "label": "Auto"}, {"id": "powershell", "label": "Windows PowerShell"}],
        }
        with mock.patch("aha_cli.web.local_terminal.local_terminal_shell_options", return_value=payload) as detect:
            response = asyncio.run(local_terminal_options_response("GET", "/api/local-terminal/options"))

        self.assertIsNotNone(response)
        headers, body = (response or b"").split(b"\r\n\r\n", 1)
        self.assertIn(b"200 OK", headers)
        self.assertEqual(__import__("json").loads(body), payload)
        detect.assert_called_once_with()

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

    def test_windows_local_terminal_streams_pipe_output_and_input(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO("AHA_WINDOWS_OK 中文\r\n".encode("gbk"))
                self.pid = 123

            def poll(self) -> int:
                return 0

        process = FakeProcess()

        async def run_session() -> tuple[str, bytes]:
            with (
                mock.patch("aha_cli.services.local_terminal._platform.is_windows", return_value=True),
                mock.patch("aha_cli.services.local_terminal.subprocess.Popen", return_value=process) as popen,
                mock.patch("aha_cli.services.local_terminal.assign_parent_death"),
                mock.patch("aha_cli.services.local_terminal.locale.getpreferredencoding", return_value="gbk"),
            ):
                session = LocalTerminalSession(shell=r"C:\Windows\System32\cmd.exe")
                session.start(cols=80, rows=24)
                session.attach_reader(asyncio.get_running_loop())
                chunk = await asyncio.wait_for(session.read(), timeout=1.0)
                session.write("echo 输入\r\n")
                terminal_input = process.stdin.getvalue()
                session.detach_reader(asyncio.get_running_loop())
                await session.terminate()
                command = popen.call_args.args[0]
                self.assertEqual(command[-2:], ["/D", "/Q"])
                return (chunk or b"").decode("utf-8"), terminal_input

        output, terminal_input = asyncio.run(run_session())

        self.assertIn("AHA_WINDOWS_OK 中文", output)
        self.assertEqual(terminal_input.decode("gbk"), "echo 输入\r\n")

    def test_windows_local_terminal_prefers_conpty_and_resizes_it(self) -> None:
        class FakeConPtyProcess:
            def __init__(self) -> None:
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(b"AHA_CONPTY_OK\r\n")
                self.pid = 456
                self.resize = mock.Mock()
                self.closed = False

            def poll(self) -> int:
                return 0

            def close(self) -> None:
                self.closed = True

        process = FakeConPtyProcess()

        async def run_session() -> str:
            with (
                mock.patch("aha_cli.services.local_terminal._platform.is_windows", return_value=True),
                mock.patch("aha_cli.windows_conpty.WindowsConPtyProcess", return_value=process),
                mock.patch("aha_cli.services.local_terminal.assign_parent_death"),
            ):
                session = LocalTerminalSession(shell=r"C:\Windows\System32\cmd.exe")
                session.start(cols=80, rows=24)
                self.assertTrue(session._windows_conpty)
                session.attach_reader(asyncio.get_running_loop())
                chunk = await asyncio.wait_for(session.read(), timeout=1.0)
                session.resize(cols=120, rows=35)
                session.detach_reader(asyncio.get_running_loop())
                await session.terminate()
                return (chunk or b"").decode("utf-8")

        output = asyncio.run(run_session())

        self.assertIn("AHA_CONPTY_OK", output)
        process.resize.assert_called_once_with(cols=120, rows=35)
        self.assertTrue(process.closed)

    def test_windows_local_terminal_uses_selected_shell_command(self) -> None:
        selected = LocalTerminalShell(
            "powershell",
            "Windows PowerShell",
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoLogo"),
        )
        process = mock.Mock()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.pid = 987

        with (
            mock.patch("aha_cli.services.local_terminal._platform.is_windows", return_value=True),
            mock.patch("aha_cli.services.local_terminal.resolve_local_terminal_shell", return_value=selected),
            mock.patch("aha_cli.windows_conpty.WindowsConPtyProcess", return_value=process) as conpty,
            mock.patch("aha_cli.services.local_terminal.assign_parent_death"),
        ):
            session = LocalTerminalSession(shell_id="powershell")
            session.start(cols=90, rows=30)

        self.assertEqual(session.shell_id, "powershell")
        self.assertEqual(session.requested_shell_id, "powershell")
        self.assertEqual(conpty.call_args.args[0], list(selected.command))

    def test_windows_conpty_disconnect_closes_console_before_output_stream(self) -> None:
        release_output = threading.Event()
        read_started = threading.Event()
        events: list[str] = []

        class BlockingOutput(io.BytesIO):
            def read(self, _size: int = -1) -> bytes:
                read_started.set()
                release_output.wait(timeout=2.0)
                return b""

            def close(self) -> None:
                self.assert_console_closed()
                events.append("stdout-close")
                super().close()

            @staticmethod
            def assert_console_closed() -> None:
                if not release_output.is_set():
                    raise AssertionError("output closed before pseudoconsole")

        class FakeConPtyProcess:
            def __init__(self) -> None:
                self.stdin = io.BytesIO()
                self.stdout = BlockingOutput()
                self.pid = 789
                self.running = True

            def poll(self) -> int | None:
                return None if self.running else 1

            def terminate(self) -> None:
                events.append("terminate")
                self.running = False

            def wait(self, timeout: float | None = None) -> int:
                events.append("wait")
                return 1

            def close_pseudo_console(self) -> None:
                events.append("console-close")
                release_output.set()

            def close(self) -> None:
                events.append("process-close")

        async def run_session() -> None:
            process = FakeConPtyProcess()
            session = LocalTerminalSession(shell=r"C:\Windows\System32\cmd.exe")
            session.process = process  # type: ignore[assignment]
            session._windows_stdin = process.stdin
            session._windows_stdout = process.stdout
            session._windows_conpty = True
            session.attach_reader(asyncio.get_running_loop())
            await asyncio.to_thread(read_started.wait, 1.0)
            await asyncio.wait_for(session.terminate(), timeout=2.0)

        with mock.patch("aha_cli.services.local_terminal._platform.is_windows", return_value=True):
            asyncio.run(run_session())

        self.assertLess(events.index("terminate"), events.index("console-close"))
        self.assertLess(events.index("console-close"), events.index("stdout-close"))
        self.assertLess(events.index("stdout-close"), events.index("process-close"))

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
