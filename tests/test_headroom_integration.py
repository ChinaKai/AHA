from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.domain.models import default_config, normalize_headroom_integration_config
from aha_cli.services.headroom_integration import (
    codex_proxy_env_for_headroom,
    codex_upstream_base_url,
    ensure_headroom_proxy,
    headroom_should_wrap_codex,
    headroom_status,
    headroom_scope,
    headroom_usage_summary,
    merge_no_proxy_values,
    prepare_headroom_codex_runtime,
)
from aha_cli.store.filesystem import append_event


class HeadroomIntegrationTests(unittest.TestCase):
    def test_normalizes_headroom_config_defaults(self) -> None:
        config = normalize_headroom_integration_config({"enabled": True, "port": "9999", "mode": "cache", "network_proxy": "custom"})

        self.assertTrue(config["enabled"])
        self.assertEqual(config["package"], "headroom-ai[proxy]")
        self.assertEqual(config["command"], "headroom")
        self.assertEqual(config["port"], 9999)
        self.assertEqual(config["mode"], "cache")
        self.assertNotIn("network_proxy", config)
        self.assertNotIn("http_proxy", config)

    def test_status_reports_command_installation_and_health(self) -> None:
        cfg = default_config()
        cfg["integrations"]["headroom"]["enabled"] = True
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.headroom_integration.shutil.which",
            return_value="/usr/bin/headroom",
        ), mock.patch("aha_cli.services.headroom_integration._headroom_health", return_value=True):
            status = headroom_status(Path(tmp), cfg)

        self.assertTrue(status["enabled"])
        self.assertTrue(status["installed"])
        self.assertTrue(status["running"])
        self.assertEqual(status["command_path"], "/usr/bin/headroom")

    def test_usage_summary_counts_ready_and_skipped_turns_by_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            append_event(root, run_id, "headroom_integration_ready", {"task_id": "task-001", "agent_id": "main", "ready": True})
            append_event(root, run_id, "headroom_integration_ready", {"task_id": "task-002", "agent_id": "main", "ready": True})
            append_event(root, run_id, "headroom_integration_skipped", {"task_id": "task-002", "agent_id": "sub-001", "reason": "not_ready"})

            summary = headroom_usage_summary(root, run_id)

        self.assertEqual(summary["ready_turns"], 2)
        self.assertEqual(summary["skipped_turns"], 1)
        self.assertEqual(summary["task_count"], 2)
        task_002 = next(task for task in summary["tasks"] if task["task_id"] == "task-002")
        self.assertEqual(task_002["ready_turns"], 1)
        self.assertEqual(task_002["skipped_turns"], 1)
        self.assertEqual([agent["agent_id"] for agent in task_002["agents"]], ["main", "sub-001"])
        self.assertEqual(task_002["agents"][1]["last_reason"], "not_ready")

    def test_usage_summary_no_longer_derives_enabled_tasks_from_token_saving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"

            summary = headroom_usage_summary(root, run_id)

        self.assertEqual(summary["enabled_tasks"], 0)
        self.assertEqual(summary["ready_turns"], 0)
        self.assertEqual(summary["skipped_turns"], 0)
        self.assertEqual(summary["task_count"], 0)
        self.assertEqual(summary["tasks"], [])

    def test_codex_upstream_base_url_reads_process_env(self) -> None:
        with mock.patch.dict(os.environ, {"OPENAI_BASE_URL": "https://openai.example/v1"}, clear=True):
            self.assertEqual(codex_upstream_base_url({}), "https://openai.example/v1")

        with mock.patch.dict(os.environ, {"OPENAI_BASE_URL": "http://127.0.0.1:8787/v1"}, clear=True):
            self.assertIsNone(codex_upstream_base_url({}))

    def test_proxy_env_adds_local_no_proxy_without_dropping_existing_proxy(self) -> None:
        proxy_env = codex_proxy_env_for_headroom(
            {"HTTP_PROXY": "http://proxy:7890", "NO_PROXY": "internal.local"},
            "http://127.0.0.1:8787/v1",
        )

        self.assertEqual(proxy_env["HTTP_PROXY"], "http://proxy:7890")
        self.assertEqual(proxy_env["OPENAI_BASE_URL"], "http://127.0.0.1:8787/v1")
        self.assertIn("localhost", proxy_env["NO_PROXY"])
        self.assertIn("127.0.0.1", proxy_env["NO_PROXY"])
        self.assertIn("internal.local", proxy_env["NO_PROXY"])
        self.assertEqual(proxy_env["NO_PROXY"], proxy_env["no_proxy"])
        self.assertEqual(merge_no_proxy_values("localhost", "internal.local"), "localhost,127.0.0.1,::1,internal.local")

    def test_prepare_runtime_never_wraps_from_task_token_saving(self) -> None:
        cfg = default_config()
        cfg["integrations"]["headroom"]["enabled"] = True
        task = {"token_saving": {"enabled": True, "provider": "map"}}

        self.assertFalse(headroom_should_wrap_codex(cfg, task, "codex"))
        codex_config, proxy_env, status = prepare_headroom_codex_runtime(
            Path("/tmp/aha-test"),
            config=cfg,
            task=task,
            backend_name="codex",
            codex_config={"env": []},
            proxy_env={},
        )

        self.assertEqual(codex_config, {"env": []})
        self.assertEqual(proxy_env, {})
        self.assertEqual(status["reason"], "not_selected")

    def test_prepare_runtime_does_not_consider_litellm_bridge_when_not_selected(self) -> None:
        cfg = default_config()
        cfg["integrations"]["headroom"].update({"enabled": True, "port": 8989})
        task = {"token_saving": {"enabled": True, "provider": "map"}}
        codex_config = {
            "env_active": "kimi-k2.6",
            "env": [
                {
                    "name": "kimi-k2.6",
                    "ANTHROPIC_BASE_URL": "https://api.kimi.com/coding/",
                    "ANTHROPIC_MODEL": "kimi-k2.6",
                    "ANTHROPIC_API_KEY": "kimi-key",
                }
            ],
        }

        with mock.patch("aha_cli.services.headroom_integration.ensure_headroom_proxy") as ensure_proxy:
            wrapped_config, proxy_env, status = prepare_headroom_codex_runtime(
                Path("/tmp/aha-test"),
                config=cfg,
                task=task,
                backend_name="codex",
                codex_config=codex_config,
                proxy_env={},
            )

        ensure_proxy.assert_not_called()
        self.assertIs(wrapped_config, codex_config)
        self.assertEqual(proxy_env, {})
        self.assertEqual(status["reason"], "not_selected")

    def test_ensure_proxy_uses_agent_proxy_env_for_headroom_process(self) -> None:
        class FakeProcess:
            pid = 12345

            def poll(self) -> None:
                return None

        captured: dict[str, str] = {}

        def fake_popen(*_args: object, **kwargs: object) -> FakeProcess:
            captured.update(kwargs.get("env") or {})
            return FakeProcess()

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"OPENAI_BASE_URL": "https://openai.example/v1"},
            clear=True,
        ), mock.patch(
            "aha_cli.services.headroom_integration.shutil.which",
            return_value="/usr/bin/headroom",
        ), mock.patch(
            "aha_cli.services.headroom_integration._headroom_health",
            side_effect=[False, False, True, True],
        ), mock.patch("aha_cli.services.headroom_integration.subprocess.Popen", side_effect=fake_popen):
            status = ensure_headroom_proxy(
                Path(tmp),
                {"enabled": True, "command": "headroom", "port": 8787, "mode": "token", "ccr_enabled": False},
                upstream_base_url="https://openai.example/v1",
                proxy_env={"HTTP_PROXY": "http://agent.proxy:7890", "NO_PROXY": "internal.local"},
                provider_env={},
                scope=headroom_scope("run-001", "task-001", "main"),
            )
            state_path = Path(tmp) / ".aha" / "runtime" / "headroom" / "run-001" / "task-001" / "main.json"
            state_path_exists = state_path.exists()

        self.assertTrue(status["ready"])
        self.assertEqual(status["scope"], {"run_id": "run-001", "task_id": "task-001", "agent_id": "main"})
        self.assertTrue(state_path_exists)
        self.assertEqual(captured["HTTP_PROXY"], "http://agent.proxy:7890")
        self.assertIn("internal.local", captured["NO_PROXY"])
        self.assertIn("127.0.0.1", captured["NO_PROXY"])
        self.assertEqual(captured["OPENAI_TARGET_API_URL"], "https://openai.example/v1")


if __name__ == "__main__":
    unittest.main()
