from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.services.aha_http_client import (
    AhaHttpClientError,
    aha_platform_is_windows,
    running_in_wsl,
    should_forward_to_windows_web,
    web_forward,
)
from aha_cli.store.filesystem import write_json


def _write_service_runtime(root: Path, *, platform: str = "Windows", port: int = 8788, status: str = "running") -> None:
    runtime_dir = root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        runtime_dir / "service.json",
        {
            "schema_version": 1,
            "service": "aha-web",
            "status": status,
            "platform": platform,
            "bind_host": "0.0.0.0",
            "bind_port": str(port),
            "auth_required": True,
            "aha_home": str(root),
        },
    )


class AhaHttpClientTests(unittest.TestCase):
    def test_aha_platform_is_windows_reads_service_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir(parents=True)
            _write_service_runtime(root, platform="Windows")
            self.assertTrue(aha_platform_is_windows(root))
            _write_service_runtime(root, platform="Linux")
            self.assertFalse(aha_platform_is_windows(root))

    def test_aha_platform_is_windows_defaults_false_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(aha_platform_is_windows(Path(tmp)))

    def test_running_in_wsl_detects_wsl_marker(self) -> None:
        marker = Path("/proc/sys/fs/binfmt_misc/WSLInterop")
        with mock.patch("sys.platform", "linux"), mock.patch(
            "aha_cli.services.aha_http_client.Path.exists", return_value=True
        ):
            self.assertTrue(running_in_wsl())
        with mock.patch("sys.platform", "linux"), mock.patch(
            "aha_cli.services.aha_http_client.Path.exists", return_value=False
        ):
            self.assertFalse(running_in_wsl())
        with mock.patch("sys.platform", "win32"):
            self.assertFalse(running_in_wsl())

    def test_should_forward_only_in_wsl_with_windows_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir(parents=True)
            _write_service_runtime(root, platform="Windows")
            with mock.patch("aha_cli.services.aha_http_client.running_in_wsl", return_value=True):
                self.assertTrue(should_forward_to_windows_web(root))
            with mock.patch("aha_cli.services.aha_http_client.running_in_wsl", return_value=False):
                self.assertFalse(should_forward_to_windows_web(root))
            # WSL-hosted AHA stays local.
            _write_service_runtime(root, platform="Linux")
            with mock.patch("aha_cli.services.aha_http_client.running_in_wsl", return_value=True):
                self.assertFalse(should_forward_to_windows_web(root))

    def test_web_forward_builds_dynamic_url_and_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir(parents=True)
            _write_service_runtime(root, port=8899)
            (root / "web-token").write_text("secret-token", encoding="utf-8")

            request_url = ""
            request_token: str | None = None

            def fake_urlopen(request, timeout=None):
                nonlocal request_url, request_token
                request_url = request.full_url
                for name, value in request.header_items():
                    if name.lower() == "x-aha-token":
                        request_token = value

                class Response:
                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return False

                    def read(self):
                        return json.dumps({"ok": True, "echo": 1}).encode("utf-8")

                return Response()

            with mock.patch("aha_cli.services.aha_http_client.urlopen", side_effect=fake_urlopen):
                result = web_forward(root, "POST", "/api/task/task-001/browser-action", payload={"action": "navigate"})

            self.assertEqual(result["ok"], True)
            self.assertIn("127.0.0.1:8899", request_url)
            self.assertIn("/api/task/task-001/browser-action", request_url)
            self.assertEqual(request_token, "secret-token")

    def test_web_forward_raises_on_error_response(self) -> None:
        from urllib.error import HTTPError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir(parents=True)
            _write_service_runtime(root)

            class FakeStream:
                def read(self):
                    return json.dumps({"error": "boom"}).encode("utf-8")

                def close(self) -> None:
                    return None

            def raising_urlopen(request, timeout=None):
                raise HTTPError(request.full_url, 409, "Conflict", {}, FakeStream())

            with mock.patch("aha_cli.services.aha_http_client.urlopen", side_effect=raising_urlopen):
                with self.assertRaises(AhaHttpClientError) as ctx:
                    web_forward(root, "POST", "/api/task/task-001/browser-action")
            self.assertEqual(str(ctx.exception), "boom")

    def test_web_forward_raises_when_service_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir(parents=True)
            _write_service_runtime(root, status="stopped")
            with self.assertRaises(AhaHttpClientError):
                web_forward(root, "GET", "/api/health")


if __name__ == "__main__":
    unittest.main()
