from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.store.io import write_json
from tests.helpers import fetch_ui_response, json_response_body


def response_headers(response: bytes) -> dict[str, str]:
    header_text = response.split(b"\r\n\r\n", 1)[0].decode("utf-8")
    headers: dict[str, str] = {}
    for line in header_text.split("\r\n")[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
    return headers


class WebAuthTests(unittest.TestCase):
    def test_auth_token_protects_ui_apis_but_not_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()

            denied = asyncio.run(fetch_ui_response(root, "", "/api/bootstrap", auth_token="secret"))
            health = asyncio.run(fetch_ui_response(root, "", "/api/health", auth_token="secret"))
            allowed = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/bootstrap",
                    headers={"Authorization": "Bearer secret"},
                    auth_token="secret",
                )
            )

        self.assertTrue(denied.startswith(b"HTTP/1.1 401 Unauthorized"))
        self.assertTrue(health.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(allowed.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(json_response_body(health)["auth_required"])
        self.assertEqual(json_response_body(allowed)["aha_home"], str(root))

    def test_query_token_sets_http_only_cookie_for_browser_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()

            first = asyncio.run(fetch_ui_response(root, "", "/?token=secret", auth_token="secret"))
            cookie = response_headers(first)["set-cookie"]
            second = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/bootstrap",
                    headers={"Cookie": cookie.split(";", 1)[0]},
                    auth_token="secret",
                )
            )

        self.assertTrue(first.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn("aha_web_token=secret", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertTrue(second.startswith(b"HTTP/1.1 200 OK"))

    def test_invalid_shell_token_still_loads_login_ui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()

            wrong_query = asyncio.run(fetch_ui_response(root, "", "/?token=wrong", auth_token="secret"))
            stale_cookie = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/",
                    headers={"Cookie": "aha_web_token=wrong"},
                    auth_token="secret",
                )
            )
            static_asset = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/static/app.js",
                    headers={"Cookie": "aha_web_token=wrong"},
                    auth_token="secret",
                )
            )
            api_denied = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/bootstrap",
                    headers={"Cookie": "aha_web_token=wrong"},
                    auth_token="secret",
                )
            )

        self.assertTrue(wrong_query.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn(b'id="login-view"', wrong_query)
        self.assertNotIn("set-cookie", response_headers(wrong_query))
        self.assertTrue(stale_cookie.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn(b'id="login-view"', stale_cookie)
        self.assertTrue(static_asset.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn(b"window.AHAAppRuntime.start", static_asset)
        self.assertTrue(api_denied.startswith(b"HTTP/1.1 401 Unauthorized"))

    def test_login_endpoint_sets_and_logout_clears_auth_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()

            shell = asyncio.run(fetch_ui_response(root, "", "/", auth_token="secret"))
            static_asset = asyncio.run(fetch_ui_response(root, "", "/static/app.js", auth_token="secret"))
            denied = asyncio.run(fetch_ui_response(root, "", "/api/bootstrap", auth_token="secret"))
            bad_login = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/login",
                    method="POST",
                    payload={"token": "wrong"},
                    auth_token="secret",
                )
            )
            login = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/login",
                    method="POST",
                    payload={"token": "secret"},
                    auth_token="secret",
                )
            )
            cookie = response_headers(login)["set-cookie"]
            allowed = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/bootstrap",
                    headers={"Cookie": cookie.split(";", 1)[0]},
                    auth_token="secret",
                )
            )
            logout = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/logout",
                    method="POST",
                    headers={"Cookie": cookie.split(";", 1)[0]},
                    auth_token="secret",
                )
            )
            cleared_cookie = response_headers(logout)["set-cookie"]
            denied_after_logout = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/bootstrap",
                    headers={"Cookie": cleared_cookie.split(";", 1)[0]},
                    auth_token="secret",
                )
            )

        self.assertTrue(shell.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(static_asset.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(denied.startswith(b"HTTP/1.1 401 Unauthorized"))
        self.assertTrue(bad_login.startswith(b"HTTP/1.1 401 Unauthorized"))
        self.assertTrue(login.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn("aha_web_token=secret", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertTrue(allowed.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn("aha_web_token=\"\"", cleared_cookie)
        self.assertIn("Max-Age=0", cleared_cookie)
        self.assertTrue(denied_after_logout.startswith(b"HTTP/1.1 401 Unauthorized"))

    def test_websocket_handshake_requires_auth_when_enabled(self) -> None:
        async def fetch_ws_handshake(root: Path, token: str, target: str) -> bytes:
            from aha_cli.web.server import handle_ui_client

            server = await asyncio.start_server(lambda reader, writer: handle_ui_client(root, "run-1", reader, writer, token), "127.0.0.1", 0)
            host, port = server.sockets[0].getsockname()
            try:
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
                writer.close()
                await writer.wait_closed()
                return response
            finally:
                server.close()
                await server.wait_closed()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            write_json(
                root / "runs" / "run-1" / "plan.json",
                {
                    "id": "run-1",
                    "goal": "Auth WS",
                    "mode": "research",
                    "created_at": "2026-05-31T00:00:00+00:00",
                    "updated_at": "2026-05-31T00:00:00+00:00",
                    "tasks": [],
                },
            )
            denied = asyncio.run(fetch_ws_handshake(root, "secret", "/ws"))
            allowed = asyncio.run(fetch_ws_handshake(root, "secret", "/ws?token=secret"))

        self.assertTrue(denied.startswith(b"HTTP/1.1 401 Unauthorized"))
        self.assertTrue(allowed.startswith(b"HTTP/1.1 101 Switching Protocols"))

    def test_ui_websocket_uses_low_latency_interval(self) -> None:
        async def fetch_ws_interval(root: Path) -> list[float]:
            from aha_cli.web.server import UI_WS_INTERVAL_SECONDS, handle_ui_client

            intervals: list[float] = []

            async def fake_handle_ws_connection(*args, **_kwargs) -> None:
                intervals.append(float(args[4]))

            server = await asyncio.start_server(
                lambda reader, writer: handle_ui_client(root, "run-1", reader, writer),
                "127.0.0.1",
                0,
            )
            host, port = server.sockets[0].getsockname()
            try:
                with mock.patch("aha_cli.web.server.handle_ws_connection", side_effect=fake_handle_ws_connection):
                    reader, writer = await asyncio.open_connection(host, port)
                    writer.write(
                        (
                            "GET /ws HTTP/1.1\r\n"
                            "Host: test\r\n"
                            "Upgrade: websocket\r\n"
                            "Connection: Upgrade\r\n"
                            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                            "Sec-WebSocket-Version: 13\r\n"
                            "\r\n"
                        ).encode("ascii")
                    )
                    await writer.drain()
                    await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1)
                    writer.close()
                    await writer.wait_closed()
                    await asyncio.sleep(0)
            finally:
                server.close()
                await server.wait_closed()
            self.assertEqual(UI_WS_INTERVAL_SECONDS, 0.1)
            return intervals

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            write_json(
                root / "runs" / "run-1" / "plan.json",
                {
                    "id": "run-1",
                    "goal": "Realtime interval",
                    "mode": "research",
                    "created_at": "2026-06-01T00:00:00+00:00",
                    "updated_at": "2026-06-01T00:00:00+00:00",
                    "tasks": [],
                },
            )

            intervals = asyncio.run(fetch_ws_interval(root))

        self.assertEqual(intervals, [0.1])


if __name__ == "__main__":
    unittest.main()
