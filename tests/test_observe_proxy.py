from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import gzip
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.request import Request, urlopen

from aha_cli.domain.models import default_config, normalize_integrations_config
from aha_cli.services.observe_proxy import (
    CODEX_CHATGPT_DEFAULT_BASE_URL,
    ObserveProxyHandler,
    _artifact_preview,
    _state_path,
    codex_observe_upstream_base_url,
    ensure_observe_proxy,
    observe_proxy_scope,
    observe_proxy_base_url,
    observe_proxy_usage_summary,
    prepare_observe_claude_runtime,
    prepare_observe_codex_runtime,
)
from aha_cli.store.filesystem import iter_jsonl_from
from aha_cli.store.paths import event_path, run_dir


class ObserveProxyTests(unittest.TestCase):
    def test_normalizes_observe_proxy_config(self) -> None:
        config = normalize_integrations_config({"observe_proxy": {"enabled": True, "port": "9999"}})

        self.assertTrue(config["observe_proxy"]["enabled"])
        self.assertEqual(config["observe_proxy"]["port"], 9999)

    def test_prepare_codex_runtime_wraps_provider_base_url(self) -> None:
        cfg = default_config()
        cfg["integrations"]["observe_proxy"].update({"port": 8899})
        task = {"id": "task-001", "observe_proxy": {"enabled": True}}
        codex_config = {
            "env": [
                {
                    "name": "gpt",
                    "OPENAI_BASE_URL": "https://openai.example/v1",
                    "OPENAI_MODEL": "gpt-test",
                    "OPENAI_API_KEY": "test-key",
                }
            ],
            "env_active": "gpt",
        }

        with mock.patch("aha_cli.services.observe_proxy.ensure_observe_proxy", return_value={"ready": True, "port": 8899}):
            wrapped_config, proxy_env, status = prepare_observe_codex_runtime(
                Path("/tmp/aha-test"),
                config=cfg,
                task=task,
                backend_name="codex",
                codex_config=codex_config,
                proxy_env={"HTTP_PROXY": "http://proxy.local:7890", "HTTPS_PROXY": "http://proxy.local:7890", "NO_PROXY": "internal.local"},
                run_id="run-001",
                task_id="task-001",
                agent_id="main",
            )

        self.assertTrue(status["ready"])
        self.assertEqual(status["upstream_base_url"], "https://openai.example/v1")
        self.assertEqual(proxy_env["HTTP_PROXY"], "http://proxy.local:7890")
        self.assertEqual(proxy_env["HTTPS_PROXY"], "http://proxy.local:7890")
        self.assertEqual(proxy_env["OPENAI_BASE_URL"], "http://127.0.0.1:8899/v1")
        self.assertIn("internal.local", proxy_env["NO_PROXY"])
        provider = wrapped_config["_provider_override"]
        self.assertEqual(provider["provider_id"], "aha_observe")
        self.assertEqual(provider["base_url"], "http://127.0.0.1:8899/v1")
        self.assertFalse(provider["requires_openai_auth"])
        self.assertEqual(provider["env_key"], "OPENAI_API_KEY")

    def test_prepare_codex_runtime_preserves_default_openai_auth(self) -> None:
        cfg = default_config()
        cfg["integrations"]["observe_proxy"].update({"port": 8899})
        task = {"id": "task-001", "observe_proxy": {"enabled": True}}

        with mock.patch("aha_cli.services.observe_proxy.ensure_observe_proxy", return_value={"ready": True, "port": 8899}), mock.patch(
            "aha_cli.services.observe_proxy._codex_auth_mode",
            return_value="chatgpt",
        ):
            wrapped_config, _proxy_env, status = prepare_observe_codex_runtime(
                Path("/tmp/aha-test"),
                config=cfg,
                task=task,
                backend_name="codex",
                codex_config={"model": "gpt-5.5"},
                proxy_env={},
                run_id="run-001",
                task_id="task-001",
                agent_id="main",
            )

        self.assertTrue(status["ready"])
        self.assertEqual(status["upstream_base_url"], CODEX_CHATGPT_DEFAULT_BASE_URL)
        provider = wrapped_config["_provider_override"]
        self.assertEqual(provider["base_url"], "http://127.0.0.1:8899/v1")
        self.assertTrue(provider["requires_openai_auth"])
        self.assertNotIn("env_key", provider)

    def test_codex_observe_upstream_uses_openai_for_api_key_auth(self) -> None:
        with mock.patch("aha_cli.services.observe_proxy._codex_auth_mode", return_value="apikey"), mock.patch.dict(
            "os.environ",
            {},
            clear=True,
        ):
            self.assertEqual(codex_observe_upstream_base_url({}), "https://api.openai.com/v1")

    def test_codex_observe_upstream_prefers_explicit_base_url(self) -> None:
        codex_config = {
            "env": [{"name": "openai", "OPENAI_BASE_URL": "https://openai.example/v1", "OPENAI_MODEL": "gpt-test"}],
            "env_active": "openai",
        }

        with mock.patch("aha_cli.services.observe_proxy._codex_auth_mode", return_value="chatgpt"):
            self.assertEqual(codex_observe_upstream_base_url(codex_config), "https://openai.example/v1")

    def test_artifact_preview_decodes_compressed_or_binary_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            body_dir = run_dir(root, run_id) / "network_io" / "task-001" / "main"
            body_dir.mkdir(parents=True)
            gzip_path = body_dir / "response-gzip.body"
            binary_path = body_dir / "response-binary.body"
            gzip_path.write_bytes(gzip.compress(b"data: hello\n\n"))
            binary_path.write_bytes(bytes([0x85, 0x9B, 0x00, 0x00, 0xFF, 0xFE]))

            gzip_preview = _artifact_preview(
                root,
                run_id,
                "network_io/task-001/main/response-gzip.body",
                content_encoding="gzip",
            )
            binary_preview = _artifact_preview(root, run_id, "network_io/task-001/main/response-binary.body")

        self.assertEqual(gzip_preview["preview"], "data: hello\n\n")
        self.assertIn("[binary body", binary_preview["preview"])

    def test_prepare_claude_runtime_overrides_base_url_in_proxy_env(self) -> None:
        cfg = default_config()
        cfg["integrations"]["observe_proxy"].update({"port": 8898})
        task = {"id": "task-001", "observe_proxy": {"enabled": True}}
        claude_config = {
            "env": [
                {
                    "name": "claude",
                    "ANTHROPIC_BASE_URL": "https://anthropic.example",
                    "ANTHROPIC_MODEL": "claude-test",
                    "ANTHROPIC_API_KEY": "test-key",
                }
            ],
            "env_active": "claude",
        }

        with mock.patch("aha_cli.services.observe_proxy.ensure_observe_proxy", return_value={"ready": True, "port": 8898}):
            proxy_env, status = prepare_observe_claude_runtime(
                Path("/tmp/aha-test"),
                config=cfg,
                task=task,
                backend_name="claude",
                claude_config=claude_config,
                proxy_env={"HTTP_PROXY": "http://proxy.local:7890", "HTTPS_PROXY": "http://proxy.local:7890", "NO_PROXY": "internal.local"},
                run_id="run-001",
                task_id="task-001",
                agent_id="main",
            )

        self.assertTrue(status["ready"])
        self.assertEqual(status["upstream_base_url"], "https://anthropic.example")
        self.assertEqual(proxy_env["HTTP_PROXY"], "http://proxy.local:7890")
        self.assertEqual(proxy_env["HTTPS_PROXY"], "http://proxy.local:7890")
        self.assertEqual(proxy_env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8898")
        self.assertIn("internal.local", proxy_env["NO_PROXY"])

    def test_prepare_runtime_skips_when_task_switch_is_off(self) -> None:
        cfg = default_config()
        cfg["integrations"]["observe_proxy"].update({"enabled": True, "port": 8899})

        with mock.patch("aha_cli.services.observe_proxy.ensure_observe_proxy") as ensure_mock:
            wrapped_config, proxy_env, status = prepare_observe_codex_runtime(
                Path("/tmp/aha-test"),
                config=cfg,
                task={"id": "task-001", "observe_proxy": {"enabled": False}},
                backend_name="codex",
                codex_config={},
                proxy_env={},
                run_id="run-001",
                task_id="task-001",
                agent_id="main",
            )

        self.assertFalse(status["enabled"])
        self.assertIsNotNone(wrapped_config)
        self.assertEqual(proxy_env, {})
        ensure_mock.assert_not_called()

    def test_ensure_proxy_process_keeps_network_proxy_env(self) -> None:
        class FakeProcess:
            pid = 12345

            def poll(self) -> None:
                return None

        captured: dict[str, str] = {}

        def fake_popen(*_args: object, **kwargs: object) -> FakeProcess:
            captured.update(kwargs.get("env") or {})
            return FakeProcess()

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.observe_proxy._health",
            side_effect=[False, True, True],
        ), mock.patch("aha_cli.services.observe_proxy.subprocess.Popen", side_effect=fake_popen):
            status = ensure_observe_proxy(
                Path(tmp),
                {"enabled": True, "port": 8897},
                backend="codex",
                upstream_base_url="https://openai.example/v1",
                run_id="run-001",
                task_id="task-001",
                agent_id="main",
                proxy_env={"HTTP_PROXY": "http://proxy.local:7890", "NO_PROXY": "internal.local"},
                provider_env={"OPENAI_API_KEY": "test-key"},
                scope={"run_id": "run-001", "task_id": "task-001", "agent_id": "main"},
            )

        self.assertTrue(status["ready"])
        self.assertEqual(captured["HTTP_PROXY"], "http://proxy.local:7890")
        self.assertEqual(captured["OPENAI_API_KEY"], "test-key")
        self.assertIn("internal.local", captured["NO_PROXY"])
        self.assertIn("127.0.0.1", captured["NO_PROXY"])

    def test_ensure_proxy_does_not_reuse_dead_state_on_healthy_port(self) -> None:
        class FakeProcess:
            pid = 12345

            def poll(self) -> None:
                return None

        started: list[list[str]] = []

        def fake_popen(args: list[str], **_kwargs: object) -> FakeProcess:
            started.append(args)
            return FakeProcess()

        def fake_process_alive(pid: object) -> bool:
            try:
                return int(pid) == 12345
            except (TypeError, ValueError):
                return False

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.observe_proxy._process_alive",
            side_effect=fake_process_alive,
        ), mock.patch("aha_cli.services.observe_proxy._health", return_value=True), mock.patch(
            "aha_cli.services.observe_proxy._port_available",
            return_value=False,
        ), mock.patch(
            "aha_cli.services.observe_proxy._free_port",
            return_value=8898,
        ), mock.patch(
            "aha_cli.services.observe_proxy.subprocess.Popen",
            side_effect=fake_popen,
        ):
            root = Path(tmp)
            scope = observe_proxy_scope("run-001", "task-001", "main")
            state_path = _state_path(root, scope)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "pid": 999999,
                        "port": 8897,
                        "backend": "codex",
                        "upstream_base_url": "https://openai.example/v1",
                        "local_base_url": "http://127.0.0.1:8897/v1",
                        "scope": scope,
                    }
                ),
                encoding="utf-8",
            )

            status = ensure_observe_proxy(
                root,
                {"enabled": True, "port": 8897},
                backend="codex",
                upstream_base_url="https://openai.example/v1",
                run_id="run-001",
                task_id="task-001",
                agent_id="main",
                scope=scope,
            )

        self.assertTrue(status["ready"])
        self.assertTrue(status["started"])
        self.assertEqual(status["port"], 8898)
        self.assertEqual(status["local_base_url"], "http://127.0.0.1:8898/v1")
        self.assertEqual(started[0][started[0].index("--port") + 1], "8898")

    def test_proxy_records_request_and_response_artifacts(self) -> None:
        upstream_paths: list[str] = []
        upstream_accept_encoding: list[str | None] = []

        class UpstreamHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                upstream_paths.append(self.path)
                upstream_accept_encoding.append(self.headers.get("accept-encoding"))
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length)
                payload = {"ok": True, "received": json.loads(body.decode("utf-8")), "usage": {"input_tokens": 3, "output_tokens": 2}}
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            run_dir(root, run_id).mkdir(parents=True)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), ObserveProxyHandler)
            proxy.context = {
                "root": root,
                "run_id": run_id,
                "task_id": "task-001",
                "agent_id": "main",
                "backend": "codex",
                "upstream_base_url": f"http://127.0.0.1:{upstream.server_port}/v1",
                "openai_api_key": "test-key",
                "anthropic_api_key": "",
            }
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            proxy_thread.start()
            try:
                body = json.dumps({"input": "hello"}).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{proxy.server_port}/v1/responses",
                    data=body,
                    headers={"content-type": "application/json", "accept-encoding": "br"},
                    method="POST",
                )
                with urlopen(request, timeout=5) as response:
                    response_payload = json.loads(response.read().decode("utf-8"))
            finally:
                proxy.shutdown()
                upstream.shutdown()

            events, _ = iter_jsonl_from(event_path(root, run_id), 0)
            request_artifact_exists = (run_dir(root, run_id) / events[0]["data"]["request_ref"]).exists()
            response_artifact_exists = (run_dir(root, run_id) / events[1]["data"]["response_ref"]).exists()
            summary = observe_proxy_usage_summary(root, run_id)
            summary_without_recent = observe_proxy_usage_summary(root, run_id, include_recent=False)
            summary_for_task = observe_proxy_usage_summary(root, run_id, recent_task_id="task-001")
            summary_for_other_task = observe_proxy_usage_summary(root, run_id, recent_task_id="task-other")

        self.assertTrue(response_payload["ok"])
        self.assertEqual(upstream_paths, ["/v1/responses"])
        self.assertEqual(upstream_accept_encoding, ["identity"])
        self.assertEqual([event["type"] for event in events], ["agent_network_request", "agent_network_response"])
        self.assertEqual(events[0]["data"]["request_bytes"], len(body))
        self.assertEqual(events[1]["data"]["usage"], {"input_tokens": 3, "output_tokens": 2})
        self.assertTrue(request_artifact_exists)
        self.assertTrue(response_artifact_exists)
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["responses"], 1)
        self.assertEqual(summary["recent"][0]["request"]["preview"], '{"input": "hello"}')
        self.assertIn('"ok": true', summary["recent"][0]["response"]["preview"])
        self.assertEqual(summary_without_recent["recent"], [])
        self.assertEqual(summary_for_task["recent"][0]["task_id"], "task-001")
        self.assertEqual(summary_for_other_task["recent"], [])

    def test_proxy_maps_codex_chatgpt_upstream_without_v1_prefix(self) -> None:
        upstream_paths: list[str] = []

        class UpstreamHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                upstream_paths.append(self.path)
                payload = {"ok": True}
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            run_dir(root, run_id).mkdir(parents=True)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), ObserveProxyHandler)
            proxy.context = {
                "root": root,
                "run_id": run_id,
                "task_id": "task-001",
                "agent_id": "main",
                "backend": "codex",
                "upstream_base_url": f"http://127.0.0.1:{upstream.server_port}/backend-api/codex",
                "openai_api_key": "",
                "anthropic_api_key": "",
            }
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            proxy_thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{proxy.server_port}/v1/models?client_version=0.142.0", timeout=5) as response:
                    response_payload = json.loads(response.read().decode("utf-8"))
            finally:
                proxy.shutdown()
                upstream.shutdown()

        self.assertTrue(response_payload["ok"])
        self.assertEqual(upstream_paths, ["/backend-api/codex/models?client_version=0.142.0"])


if __name__ == "__main__":
    unittest.main()
