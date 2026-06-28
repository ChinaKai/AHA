from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main
from aha_cli.services.run_retention import apply_run_retention
from aha_cli.store.filesystem import (
    append_event,
    event_path,
    inbox_path,
    iter_jsonl_from,
    read_json,
    run_dir,
    set_agent_status,
    set_task_status,
    status_snapshot,
)
from aha_cli.store.task_memos import create_task_memo
from aha_cli.store.ui_state import update_ui_state
from aha_cli.web.run_routes import handle_run_workspace_route
from tests.helpers import (
    AHA_RUNTIME_ENV_KEYS,
    fetch_ui_response,
    isolated_cli_environment,
    json_response_body,
)


class WebRunApiTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with isolated_cli_environment(allow_temp_aha_home=False), mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_run_cli_ignores_runtime_aha_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            leaked_home = Path(tmp) / "leaked-home"
            root.mkdir()
            leaked_home.mkdir()
            env = {key: "leaked-value" for key in AHA_RUNTIME_ENV_KEYS}
            env["AHA_HOME"] = str(leaked_home)
            env["AHA_ROOT"] = str(leaked_home)
            with mock.patch.dict("os.environ", env, clear=False), mock.patch("pathlib.Path.cwd", return_value=root):
                code, _ = self.run_cli("init", "--portable", "--backend", "codex")
                self.assertEqual(code, 0)
                code, plan_output = self.run_cli("plan", "Isolated run", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

            self.assertTrue((root / ".aha" / "runs" / run_id / "plan.json").exists())
            self.assertFalse((leaked_home / "runs").exists())

    def test_api_bootstrap_works_without_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            with mock.patch("aha_cli.web.run_api.aha_version", return_value="20260527.057e500"):
                response = asyncio.run(fetch_ui_response(root, "", "/api/bootstrap"))
            body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(body["aha_home"], str(root))
        self.assertEqual(body["aha_version"], "20260527.057e500")
        self.assertFalse(body["initialized"])
        self.assertEqual(body["config"]["backend"], "stub")
        self.assertEqual(body["config"]["default_parallel"], 10)
        self.assertEqual(body["config_backend_options"], ["codex", "claude"])
        workflow_templates = body["workflow_templates"]
        self.assertEqual(workflow_templates[0]["id"], "auto")
        self.assertIn("fault-debug", {item["id"] for item in workflow_templates})
        self.assertTrue(all({"id", "label", "description", "guidance", "order"} <= set(item) for item in workflow_templates))
        self.assertIn("default_workspace_path", body)
        self.assertEqual(body["default_run_id"], "")
        self.assertEqual(body["runs"], [])
        self.assertFalse(body["memo_summary"]["available"])
        self.assertEqual(body["memo_summary"]["counts"]["total"], 0)
        self.assertEqual(body["skill_options"], [])

    def test_api_bootstrap_includes_discovered_skill_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            workspace = Path(tmp) / "workspace"
            skill_dir = root / "skills" / "board-debug"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Board Debug\n\nUse UART safely.\n", encoding="utf-8")
            workspace_skill_dir = workspace / ".aha" / "skills" / "workspace-skill"
            workspace_skill_dir.mkdir(parents=True)
            (workspace_skill_dir / "SKILL.md").write_text("# Workspace Skill\n", encoding="utf-8")
            with mock.patch("pathlib.Path.cwd", return_value=workspace):
                response = asyncio.run(fetch_ui_response(root, "", "/api/bootstrap"))
            body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(
            body["skill_options"],
            [
                {
                    "id": "board-debug",
                    "label": "Board Debug",
                    "path": str(skill_dir / "SKILL.md"),
                    "source": "aha_home",
                }
            ],
        )

    def test_api_bootstrap_includes_current_run_memo_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Memo summary run", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                with mock.patch("aha_cli.store.task_memos.date") as date_mock:
                    date_mock.today.return_value.isoformat.return_value = "2026-06-16"
                    date_mock.fromisoformat.side_effect = lambda value: __import__("datetime").date.fromisoformat(value)
                    create_task_memo(
                        root / ".aha",
                        run_id,
                        {
                            "title": "Today memo",
                            "scheduled_date": "2026-06-16",
                            "status": "todo",
                        },
                    )
                    create_task_memo(
                        root / ".aha",
                        run_id,
                        {
                            "title": "Overdue memo",
                            "scheduled_date": "2026-06-01",
                            "end_date": "2026-06-02",
                            "status": "doing",
                        },
                    )
                    create_task_memo(
                        root / ".aha",
                        run_id,
                        {
                            "title": "Closed memo",
                            "scheduled_date": "2026-06-15",
                            "status": "closed",
                            "closed_at": "2026-06-16",
                        },
                    )
                    update_ui_state(root / ".aha", run_id, {"last_selected_memo_id": "memo-002"})
                    with mock.patch("aha_cli.web.run_api.read_task_memos", side_effect=AssertionError("memo summary cache miss")):
                        response = asyncio.run(fetch_ui_response(root, run_id, "/api/bootstrap"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        summary = body["memo_summary"]
        self.assertTrue(summary["available"])
        self.assertEqual(summary["run_id"], run_id)
        self.assertEqual(summary["last_selected_memo_id"], "memo-002")
        self.assertEqual(summary["counts"]["total"], 3)
        self.assertEqual(summary["counts"]["active"], 2)
        self.assertEqual(summary["counts"]["todo"], 1)
        self.assertEqual(summary["counts"]["doing"], 1)
        self.assertEqual(summary["counts"]["closed"], 1)
        self.assertEqual(summary["counts"]["today"], 1)
        self.assertEqual(summary["counts"]["overdue"], 1)

    def test_api_bootstrap_can_initialize_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            workspace_root = Path(tmp) / "projects"
            response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/bootstrap",
                    method="POST",
                    payload={
                        "backend": "codex",
                        "default_parallel": 2,
                        "default_mode": "implementation",
                        "workspace_roots": [str(workspace_root)],
                        "codex": {
                            "model": "gpt-5.5",
                            "proxy": {
                                "enabled": True,
                                "http_proxy": "http://codex.proxy:7890",
                                "https_proxy": "http://codex.proxy:7890",
                                "no_proxy": "localhost,127.0.0.1",
                            },
                            "env_active": "openai",
                            "env": [
                                {
                                    "name": "openai",
                                    "OPENAI_BASE_URL": "https://openai.test/v1",
                                    "OPENAI_MODEL": "gpt-5.5",
                                    "OPENAI_API_KEY": "openai-key",
                                    "CODEX_WIRE_API": "responses",
                                    "CODEX_ENV_KEY": "OPENAI_API_KEY",
                                }
                            ],
                            "sandbox": "workspace-write",
                            "approval": "never",
                            "json": True,
                        },
                        "claude": {
                            "model": "env:work",
                            "proxy": {
                                "enabled": False,
                                "http_proxy": "http://claude.proxy:7890",
                                "https_proxy": "http://claude.proxy:7890",
                                "no_proxy": "localhost,127.0.0.1",
                            },
                            "env_active": "work",
                            "env": [
                                {
                                    "name": "work",
                                    "ANTHROPIC_BASE_URL": "https://claude.test",
                                    "ANTHROPIC_MODEL": "claude-sonnet",
                                    "ANTHROPIC_API_KEY": "test-key",
                                }
                            ],
                        },
                    },
                )
            )
            body = json_response_body(response)
            cfg = read_json(root / "config.json")

        self.assertTrue(response.startswith(b"HTTP/1.1 201 Created"))
        self.assertTrue(body["initialized"])
        self.assertEqual(cfg["backend"], "codex")
        self.assertEqual(cfg["default_parallel"], 2)
        self.assertEqual(cfg["default_mode"], "implementation")
        self.assertEqual(cfg["workspace_roots"], [str(workspace_root)])
        self.assertIsNone(cfg["proxy"]["http_proxy"])
        self.assertTrue(cfg["codex"]["proxy"]["enabled"])
        self.assertEqual(cfg["codex"]["proxy"]["http_proxy"], "http://codex.proxy:7890")
        self.assertFalse(cfg["claude"]["proxy"]["enabled"])
        self.assertEqual(body["config"]["claude"]["proxy"]["https_proxy"], "http://claude.proxy:7890")
        self.assertEqual(cfg["codex"]["model"], "gpt-5.5")
        self.assertEqual(cfg["codex"]["env_active"], "openai")
        self.assertEqual(
            cfg["codex"]["env"],
            [
                {
                    "name": "openai",
                    "OPENAI_BASE_URL": "https://openai.test/v1",
                    "OPENAI_MODEL": "gpt-5.5",
                    "OPENAI_API_KEY": "openai-key",
                    "CODEX_WIRE_API": "responses",
                    "CODEX_ENV_KEY": "OPENAI_API_KEY",
                }
            ],
        )
        self.assertEqual(cfg["codex"]["sandbox"], "workspace-write")
        self.assertEqual(cfg["claude"]["model"], "env:work")
        self.assertEqual(cfg["claude"]["env_active"], "work")
        self.assertEqual(
            cfg["claude"]["env"],
            [
                {
                    "name": "work",
                    "ANTHROPIC_BASE_URL": "https://claude.test",
                    "ANTHROPIC_MODEL": "claude-sonnet",
                    "ANTHROPIC_API_KEY": "test-key",
                }
            ],
        )

    def test_api_bootstrap_rejects_invalid_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            response = asyncio.run(
                fetch_ui_response(root, "", "/api/bootstrap", method="POST", payload={"backend": "bogus"})
            )

        self.assertTrue(response.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertFalse((root / "config.json").exists())

    def test_api_bootstrap_persists_headroom_integration_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/bootstrap",
                    method="POST",
                    payload={
                        "backend": "codex",
                        "integrations": {
                            "headroom": {
                                "enabled": True,
                                "package": "headroom-ai[proxy]",
                                "command": "/opt/headroom/bin/headroom",
                                "port": 8989,
                                "mode": "cache",
                                "network_proxy": "custom",
                                "http_proxy": "http://proxy:7890",
                                "https_proxy": "http://proxy:7890",
                                "no_proxy": "internal.local",
                                "ccr_enabled": True,
                            }
                        },
                    },
                )
            )
            cfg = read_json(root / "config.json")

        self.assertTrue(response.startswith(b"HTTP/1.1 201 Created"))
        self.assertTrue(cfg["integrations"]["headroom"]["enabled"])
        self.assertEqual(cfg["integrations"]["headroom"]["command"], "/opt/headroom/bin/headroom")
        self.assertEqual(cfg["integrations"]["headroom"]["port"], 8989)
        self.assertEqual(cfg["integrations"]["headroom"]["mode"], "cache")
        self.assertNotIn("network_proxy", cfg["integrations"]["headroom"])
        self.assertNotIn("http_proxy", cfg["integrations"]["headroom"])
        self.assertNotIn("https_proxy", cfg["integrations"]["headroom"])
        self.assertNotIn("no_proxy", cfg["integrations"]["headroom"])
        self.assertTrue(cfg["integrations"]["headroom"]["ccr_enabled"])

    def test_api_headroom_integration_status_reports_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "aha_cli.services.headroom_integration.shutil.which",
            return_value="/usr/bin/headroom",
        ), mock.patch("aha_cli.services.headroom_integration._headroom_health", return_value=True):
            root = Path(tmp) / ".aha"
            root.mkdir()
            (root / "config.json").write_text(
                json.dumps({"integrations": {"headroom": {"enabled": True, "port": 8989}}}),
                encoding="utf-8",
            )
            response = asyncio.run(fetch_ui_response(root, "", "/api/integrations/headroom"))
            body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["headroom"]["enabled"])
        self.assertTrue(body["headroom"]["installed"])
        self.assertTrue(body["headroom"]["running"])
        self.assertEqual(body["headroom"]["port"], 8989)

    def test_api_bootstrap_can_select_official_claude_without_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/bootstrap",
                    method="POST",
                    payload={
                        "backend": "claude",
                        "claude": {
                            "model": "claude-sonnet-4-6",
                            "env_active": "",
                            "env": [
                                {
                                    "name": "work",
                                    "ANTHROPIC_BASE_URL": "https://claude.test",
                                    "ANTHROPIC_MODEL": "claude-sonnet",
                                    "ANTHROPIC_API_KEY": "test-key",
                                }
                            ],
                        },
                    },
                )
            )
            cfg = read_json(root / "config.json")

        self.assertTrue(response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(cfg["claude"]["model"], "claude-sonnet-4-6")
        self.assertIsNone(cfg["claude"]["env_active"])
        self.assertEqual(cfg["claude"]["env"][0]["name"], "work")

    def test_api_bootstrap_force_updates_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            (root / "config.json").write_text(json.dumps({"backend": "codex", "default_parallel": 3}), encoding="utf-8")
            blocked = asyncio.run(
                fetch_ui_response(root, "", "/api/bootstrap", method="POST", payload={"backend": "claude"})
            )
            updated = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/bootstrap",
                    method="POST",
                    payload={"backend": "claude", "default_parallel": 10, "force": True},
                )
            )
            cfg = read_json(root / "config.json")

        self.assertTrue(blocked.startswith(b"HTTP/1.1 409 Conflict"))
        self.assertTrue(updated.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(cfg["backend"], "claude")
        self.assertEqual(cfg["default_parallel"], 10)

    def test_api_bootstrap_force_preserves_existing_knowledge_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            existing_knowledge = {
                "enabled": True,
                "path": str(Path(tmp) / "knowledge"),
                "git": {
                    "enabled": True,
                    "remote": "git@example.com:aha/knowledge.git",
                    "branch": "main",
                    "auto_commit": True,
                    "auto_push": False,
                    "auto_pull": True,
                    "author_name": "AHA",
                    "author_email": "aha@local",
                },
                "curation": {"gate": "manual"},
                "project_nav": {"enabled": True, "maintain_during_task": True},
                "retrieval": {"max_entries": 7, "max_chars": 5000, "inject_mode": "references", "summary_chars": 180},
            }
            (root / "config.json").write_text(
                json.dumps({"backend": "codex", "default_parallel": 3, "knowledge": existing_knowledge}),
                encoding="utf-8",
            )

            response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/bootstrap",
                    method="POST",
                    payload={"backend": "claude", "default_parallel": 10, "force": True},
                )
            )
            body = json_response_body(response)
            cfg = read_json(root / "config.json")

        self.assertTrue(response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(cfg["backend"], "claude")
        self.assertEqual(cfg["knowledge"], existing_knowledge)
        self.assertTrue(body["config"]["knowledge"]["enabled"])
        self.assertTrue(body["config"]["knowledge"]["project_nav"]["enabled"])

    def test_api_bootstrap_rejects_non_ui_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            response = asyncio.run(
                fetch_ui_response(root, "", "/api/bootstrap", method="POST", payload={"backend": "command"})
            )

        self.assertTrue(response.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertFalse((root / "config.json").exists())

    def test_api_workspace_registration_can_create_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            workspace = Path(tmp) / "repo"
            root.mkdir()
            workspace.mkdir()
            add_response = asyncio.run(
                fetch_ui_response(root, "", "/api/workspaces", method="POST", payload={"path": str(workspace), "name": "demo"})
            )
            add_body = json_response_body(add_response)
            create_response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/runs",
                    method="POST",
                    payload={"goal": "Web setup", "mode": "research", "workspace_id": add_body["workspace"]["id"]},
                )
            )
            create_body = json_response_body(create_response)
            plan = read_json(root / "runs" / create_body["run"]["id"] / "plan.json")

        self.assertTrue(add_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertTrue(create_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(plan["tasks"][0]["workspace_id"], "ws-001")
        self.assertEqual(plan["tasks"][0]["workspace_path"], str(workspace))

    def test_api_run_creation_uses_config_backend_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            (root / "config.json").write_text(
                json.dumps({"backend": "claude", "claude": {"model": "sonnet"}}),
                encoding="utf-8",
            )
            create_response = asyncio.run(
                fetch_ui_response(root, "", "/api/runs", method="POST", payload={"goal": "Use configured backend"})
            )
            create_body = json_response_body(create_response)
            plan = read_json(root / "runs" / create_body["run"]["id"] / "plan.json")

        self.assertTrue(create_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(plan["tasks"][0]["preferred_backend"], "claude")

    def test_api_run_creation_accepts_proxy_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/runs",
                    method="POST",
                    payload={
                        "goal": "Proxy setup",
                        "mode": "research",
                        "backend": "codex",
                        "collaboration_mode": "team",
                        "workflow_template": "EMBEDDED-DRIVER",
                        "proxy_enabled": True,
                        "http_proxy": "http://127.0.0.1:7890",
                        "https_proxy": "http://127.0.0.1:7890",
                        "no_proxy": "localhost,127.0.0.1",
                    },
                )
            )
            body = json_response_body(response)
            run_id = body["run"]["id"]
            plan = read_json(root / "runs" / run_id / "plan.json")
            task = plan["tasks"][0]
            snapshot = status_snapshot(root, run_id)
            status_task = snapshot["tasks"][0]
            status_proxy = snapshot["proxy"]

        self.assertTrue(response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(task["collaboration_mode"], "team")
        self.assertEqual(task["workflow_template"], "embedded-driver")
        self.assertEqual(task["max_sub_agents"], 2)
        self.assertEqual(status_task["collaboration_mode"], "team")
        self.assertEqual(status_task["workflow_template"], "embedded-driver")
        self.assertEqual(status_task["max_sub_agents"], 2)
        self.assertEqual(status_task["workspace_path"], task["workspace_path"])
        self.assertEqual(status_task["preferred_sub_backend"], "codex")
        self.assertEqual(plan["proxy"]["http_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(plan["proxy"]["https_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(plan["proxy"]["no_proxy"], "localhost,127.0.0.1")
        self.assertEqual(status_proxy["http_proxy"], "http://127.0.0.1:7890")
        self.assertTrue(task["preferred_proxy_enabled"])
        self.assertIsNone(task["preferred_http_proxy"])
        self.assertIsNone(task["preferred_https_proxy"])
        self.assertIsNone(task["preferred_no_proxy"])
        self.assertTrue(task["agents"][0]["proxy_enabled"])

    def test_api_run_creation_uses_core_proxy_default_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/bootstrap",
                    method="POST",
                    payload={
                        "backend": "codex",
                        "proxy": {
                            "enabled": True,
                            "http_proxy": "http://core.proxy:7890",
                            "https_proxy": "http://core.proxy:7890",
                            "no_proxy": "localhost,127.0.0.1",
                        },
                    },
                )
            )
            self.assertTrue(response.startswith(b"HTTP/1.1 201 Created"))
            create_response = asyncio.run(fetch_ui_response(root, "", "/api/runs", method="POST", payload={"goal": "Core proxy"}))
            body = json_response_body(create_response)
            run_id = body["run"]["id"]
            plan = read_json(root / "runs" / run_id / "plan.json")
            snapshot = status_snapshot(root, run_id)

        self.assertTrue(create_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertTrue(plan["tasks"][0]["preferred_proxy_enabled"])
        self.assertIsNone(plan["proxy"]["http_proxy"])
        self.assertEqual(snapshot["proxy"]["http_proxy"], "http://core.proxy:7890")
        self.assertTrue(snapshot["tasks"][0]["run_proxy_configured"])

    def test_api_run_creation_uses_backend_proxy_default_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/bootstrap",
                    method="POST",
                    payload={
                        "backend": "codex",
                        "codex": {
                            "proxy": {
                                "enabled": False,
                                "http_proxy": "http://codex.proxy:7890",
                                "https_proxy": "http://codex.proxy:7890",
                                "no_proxy": "localhost,127.0.0.1",
                            },
                        },
                        "claude": {
                            "proxy": {
                                "enabled": True,
                                "http_proxy": "http://claude.proxy:7890",
                                "https_proxy": "http://claude.proxy:7890",
                                "no_proxy": "localhost,127.0.0.1",
                            },
                        },
                    },
                )
            )
            self.assertTrue(response.startswith(b"HTTP/1.1 201 Created"))
            claude_response = asyncio.run(fetch_ui_response(root, "", "/api/runs", method="POST", payload={"goal": "Claude proxy", "backend": "claude"}))
            claude_body = json_response_body(claude_response)
            claude_run_id = claude_body["run"]["id"]
            claude_plan = read_json(root / "runs" / claude_run_id / "plan.json")
            claude_snapshot = status_snapshot(root, claude_run_id)
            codex_response = asyncio.run(fetch_ui_response(root, "", "/api/runs", method="POST", payload={"goal": "Codex proxy", "backend": "codex"}))
            codex_body = json_response_body(codex_response)
            codex_run_id = codex_body["run"]["id"]
            codex_plan = read_json(root / "runs" / codex_run_id / "plan.json")
            codex_snapshot = status_snapshot(root, codex_run_id)

        self.assertTrue(claude_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertTrue(claude_plan["tasks"][0]["preferred_proxy_enabled"])
        self.assertTrue(claude_snapshot["tasks"][0]["run_proxy_enabled"])
        self.assertTrue(claude_snapshot["tasks"][0]["run_proxy_configured"])
        self.assertTrue(codex_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertFalse(codex_plan["tasks"][0]["preferred_proxy_enabled"])
        self.assertFalse(codex_snapshot["tasks"][0]["run_proxy_enabled"])
        self.assertTrue(codex_snapshot["tasks"][0]["run_proxy_configured"])

    def test_api_run_creation_rejects_unknown_execution_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            bad_mode = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/runs",
                    method="POST",
                    payload={"goal": "Bad run mode", "collaboration_mode": "crowd"},
                )
            )
            bad_workflow = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/runs",
                    method="POST",
                    payload={"goal": "Bad run workflow", "workflow_template": "unknown"},
                )
            )

        self.assertTrue(bad_mode.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertIn("unknown collaboration mode: crowd", json_response_body(bad_mode)["error"])
        self.assertTrue(bad_workflow.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertIn("unknown workflow template: unknown", json_response_body(bad_workflow)["error"])

    def test_api_run_proxy_updates_run_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            create_response = asyncio.run(
                fetch_ui_response(root, "", "/api/runs", method="POST", payload={"goal": "Run proxy"})
            )
            run_id = json_response_body(create_response)["run"]["id"]

            response = asyncio.run(
                fetch_ui_response(
                    root,
                    run_id,
                    f"/api/runs/{run_id}/proxy",
                    method="PATCH",
                    payload={
                        "proxy_enabled": True,
                        "http_proxy": "http://proxy.local:8080",
                        "https_proxy": "http://proxy.local:8080",
                        "no_proxy": "localhost,127.0.0.1",
                    },
                )
            )
            body = json_response_body(response)
            snapshot = status_snapshot(root, run_id)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["proxy"]["http_proxy"], "http://proxy.local:8080")
        self.assertEqual(snapshot["proxy"]["https_proxy"], "http://proxy.local:8080")
        self.assertTrue(snapshot["tasks"][0]["run_proxy_configured"])

    def test_api_run_creation_can_dispatch_initial_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            with mock.patch("aha_cli.web.task_runtime.start_backend", return_value={"status": "running", "started": True}) as start_backend:
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        "",
                        "/api/runs",
                        method="POST",
                        payload={
                            "goal": "Web setup",
                            "mode": "research",
                            "backend": "codex",
                            "task_titles": ["Web setup"],
                            "dispatch": True,
                        },
                    )
                )
                body = json_response_body(response)
            run_id = body["run"]["id"]
            plan = read_json(root / "runs" / run_id / "plan.json")
            events, _ = iter_jsonl_from(root / "runs" / run_id / "events.jsonl", 0)

        self.assertTrue(response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(plan["tasks"][0]["title"], "Web setup")
        self.assertTrue(any(event["type"] == "task_dispatched" and event["data"]["task_id"] == "task-001" for event in events))
        start_backend.assert_called_once()
        self.assertEqual(start_backend.call_args.args[:3], (root, run_id, "main"))
        self.assertEqual(start_backend.call_args.kwargs["task_id"], "task-001")
        self.assertTrue(start_backend.call_args.kwargs["from_start"])

    def test_api_run_creation_can_skip_initial_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            with mock.patch("aha_cli.web.task_runtime.start_backend") as start_backend:
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        "",
                        "/api/runs",
                        method="POST",
                        payload={
                            "goal": "Named run",
                            "create_initial_task": False,
                            "task_titles": ["Should be ignored"],
                            "dispatch": True,
                        },
                    )
                )
                body = json_response_body(response)
            run_id = body["run"]["id"]
            plan = read_json(root / "runs" / run_id / "plan.json")

        self.assertTrue(response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(plan["goal"], "Named run")
        self.assertEqual(plan["tasks"], [])
        start_backend.assert_not_called()

    def test_api_runs_lists_and_creates_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Default session", "--agents", "1")
                self.assertEqual(code, 0)
                default_run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                runs_response = asyncio.run(fetch_ui_response(root, default_run_id, "/api/runs"))
                runs_body = json_response_body(runs_response)
                create_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        default_run_id,
                        "/api/runs",
                        method="POST",
                        payload={"goal": "Second session", "agents": 1, "mode": "research"},
                    )
                )
                create_body = json_response_body(create_response)
                updated_response = asyncio.run(fetch_ui_response(root, default_run_id, "/api/runs"))
                updated_body = json_response_body(updated_response)

        self.assertTrue(runs_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(runs_body["default_run_id"], default_run_id)
        self.assertIn(default_run_id, {item["id"] for item in runs_body["runs"]})
        self.assertTrue(create_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertTrue(create_body["ok"])
        self.assertEqual(create_body["run"]["goal"], "Second session")
        self.assertEqual(create_body["run"]["lifecycle_status"], "active")
        self.assertIn(create_body["run"]["id"], {item["id"] for item in updated_body["runs"]})

    def test_api_run_can_be_renamed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Original run", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                rename_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/runs/{run_id}",
                        method="PATCH",
                        payload={"name": "Renamed run"},
                    )
                )
                rename_body = json_response_body(rename_response)
                runs_response = asyncio.run(fetch_ui_response(root, run_id, "/api/runs"))
                runs_body = json_response_body(runs_response)
                plan = read_json(root / ".aha" / "runs" / run_id / "plan.json")
                events, _ = iter_jsonl_from(root / ".aha" / "runs" / run_id / "events.jsonl", 0)

        self.assertTrue(rename_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(rename_body["ok"])
        self.assertEqual(rename_body["run"]["goal"], "Renamed run")
        self.assertEqual(rename_body["run"]["lifecycle_status"], "active")
        self.assertEqual(plan["goal"], "Renamed run")
        self.assertIn(run_id, {item["id"] for item in rename_body["runs"]})
        self.assertIn("Renamed run", {item["goal"] for item in runs_body["runs"]})
        self.assertTrue(any(event["type"] == "run_renamed" and event["data"]["name"] == "Renamed run" for event in events))

    def test_api_run_rename_rejects_empty_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Original run", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        f"/api/runs/{run_id}",
                        method="PATCH",
                        payload={"name": "   "},
                    )
                )
                plan = read_json(root / ".aha" / "runs" / run_id / "plan.json")

        self.assertTrue(response.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertEqual(plan["goal"], "Original run")

    def test_api_run_delete_removes_non_current_run_and_rejects_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, first_output = self.run_cli("plan", "Current run", "--agents", "1")
                self.assertEqual(code, 0)
                current_run_id = first_output.splitlines()[0].split(": ", 1)[1]
                code, old_output = self.run_cli("plan", "Old run", "--agents", "1")
                self.assertEqual(code, 0)
                old_run_id = old_output.splitlines()[0].split(": ", 1)[1]

                delete_response = asyncio.run(
                    fetch_ui_response(root, current_run_id, f"/api/runs/{old_run_id}", method="DELETE")
                )
                delete_body = json_response_body(delete_response)
                current_response = asyncio.run(
                    fetch_ui_response(root, current_run_id, f"/api/runs/{current_run_id}", method="DELETE")
                )
                current_body = json_response_body(current_response)

        self.assertTrue(delete_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(delete_body["ok"])
        self.assertEqual(delete_body["deleted"]["run_id"], old_run_id)
        self.assertNotIn(old_run_id, {item["id"] for item in delete_body["runs"]})
        self.assertFalse((root / ".aha" / "runs" / old_run_id).exists())
        self.assertTrue(current_response.startswith(b"HTTP/1.1 409 Conflict"))
        self.assertEqual(current_body["reason"], "current_run")

    def test_api_run_delete_force_removes_heartbeat_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, current_output = self.run_cli("plan", "Current run", "--agents", "1")
                self.assertEqual(code, 0)
                current_run_id = current_output.splitlines()[0].split(": ", 1)[1]
                code, heartbeat_output = self.run_cli("plan", "Heartbeat run", "--agents", "1")
                self.assertEqual(code, 0)
                heartbeat_run_id = heartbeat_output.splitlines()[0].split(": ", 1)[1]
                heartbeat_log = root / ".aha" / "runs" / heartbeat_run_id / "logs" / "realtime-debug.log"
                heartbeat_log.parent.mkdir(parents=True, exist_ok=True)
                heartbeat_log.write_text('{"phase":"heartbeat_sent"}\n', encoding="utf-8")

                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        current_run_id,
                        f"/api/runs/{heartbeat_run_id}?current_run_id={current_run_id}&force=1",
                        method="DELETE",
                    )
                )
                body = json_response_body(response)
                deleted_path_exists = (root / ".aha" / "runs" / heartbeat_run_id).exists()

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["deleted"]["run_id"], heartbeat_run_id)
        self.assertEqual(body["deleted"]["reason"], "forced")
        self.assertFalse(deleted_path_exists)

    def test_api_run_lifecycle_allows_idle_current_and_blocks_running_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, current_output = self.run_cli("plan", "Current idle run", "--agents", "1")
                self.assertEqual(code, 0)
                current_run_id = current_output.splitlines()[0].split(": ", 1)[1]
                code, running_output = self.run_cli("plan", "Running run", "--agents", "1")
                self.assertEqual(code, 0)
                running_run_id = running_output.splitlines()[0].split(": ", 1)[1]
                aha_home = root / ".aha"
                set_task_status(aha_home, running_run_id, "task-001", "running")
                set_agent_status(aha_home, running_run_id, "task-001", "main", "running")

                idle_current_response = asyncio.run(
                    fetch_ui_response(
                        aha_home,
                        current_run_id,
                        f"/api/runs/{current_run_id}/lifecycle",
                        method="PATCH",
                        payload={"status": "hidden", "current_run_id": current_run_id},
                    )
                )
                idle_current_body = json_response_body(idle_current_response)
                running_response = asyncio.run(
                    fetch_ui_response(
                        aha_home,
                        current_run_id,
                        f"/api/runs/{running_run_id}/lifecycle",
                        method="PATCH",
                        payload={"status": "hidden", "current_run_id": current_run_id},
                    )
                )
                running_body = json_response_body(running_response)

        self.assertTrue(idle_current_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(idle_current_body["run"]["lifecycle_status"], "hidden")
        self.assertTrue(running_response.startswith(b"HTTP/1.1 409 Conflict"))
        self.assertEqual(running_body["reason"], "running_work")

    def test_api_run_archive_exports_and_imports_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "UI archive",
                    "--agents",
                    "1",
                    "--enable-proxy",
                    "--http-proxy",
                    "http://user:secret@example.test:8080",
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = "ui-backend-secret"
                session_file.write_text(json.dumps(session), encoding="utf-8")
                log_file = run_dir(root, run_id) / "logs" / "backend.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_file.write_text("backend log", encoding="utf-8")
                memo_asset = run_dir(root, run_id) / "task_memo_assets" / "memo-test.png"
                memo_asset.parent.mkdir(parents=True, exist_ok=True)
                memo_asset.write_bytes(b"memo image")

                export_response = asyncio.run(
                    fetch_ui_response(root, run_id, f"/api/run/export?run_id={run_id}&no_logs=1", timeout=2.0)
                )
                export_headers, archive_bytes = export_response.split(b"\r\n\r\n", 1)
                with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as exported:
                    names = set(exported.getnames())
                    plan = json.load(exported.extractfile("run/plan.json"))

                boundary = "----aha-test-boundary"
                multipart_body = (
                    (
                        f"--{boundary}\r\n"
                        'Content-Disposition: form-data; name="archive"; filename="run.tar.gz"\r\n'
                        "Content-Type: application/gzip\r\n"
                        "\r\n"
                    ).encode("ascii")
                    + archive_bytes
                    + f"\r\n--{boundary}--\r\n".encode("ascii")
                )
                import_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/run/import",
                        timeout=3.0,
                        method="POST",
                        body=multipart_body,
                        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    )
                )
                import_body = json_response_body(import_response)
                imported_run_id = import_body["imported_run_id"]
                imported_status = status_snapshot(root, imported_run_id)
                imported_memo_asset = (run_dir(root, imported_run_id) / "task_memo_assets" / "memo-test.png").read_bytes()

        self.assertTrue(export_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn(b'Content-Disposition: attachment; filename="aha-run-', export_headers)
        self.assertIn("aha-run-manifest.json", names)
        self.assertIn("run/plan.json", names)
        self.assertIn("run/task_memo_assets/memo-test.png", names)
        self.assertNotIn("run/logs/backend.log", names)
        self.assertEqual(plan["proxy"]["http_proxy"], "<redacted>")
        self.assertNotIn("secret", json.dumps(plan))
        self.assertTrue(import_response.startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(import_body["source_run_id"], run_id)
        self.assertNotEqual(imported_run_id, run_id)
        self.assertEqual(import_body["run"]["lifecycle_status"], "active")
        self.assertIn(imported_run_id, {item["id"] for item in import_body["runs"]})
        self.assertEqual(imported_memo_asset, b"memo image")
        self.assertEqual(imported_status["tasks"][0]["agents"][0]["session_status"], "imported")
        self.assertIsNone(imported_status["tasks"][0]["agents"][0]["backend_session_id"])

    def test_api_run_maintenance_visibility_is_read_only(self) -> None:
        def stopped_backend(_root: Path, _run_id: str, target: str, task_id: str | None) -> dict:
            return {
                "status": "stopped",
                "backend": "codex",
                "target": target,
                "task_id": task_id,
                "last_pid": 1234,
                "stopped_at": "2026-05-31T00:00:00+00:00",
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict("os.environ", {"HOME": tmp}, clear=True), mock.patch(
                "pathlib.Path.cwd",
                return_value=root,
            ):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Maintenance visibility", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                aha_home = root / ".aha"
                task_id = "task-001"
                set_task_status(aha_home, run_id, task_id, "running")
                set_agent_status(aha_home, run_id, task_id, "main", "running")
                log_file = run_dir(aha_home, run_id) / "logs" / "backend.log"
                prompt_file = run_dir(aha_home, run_id) / "prompts" / "main.md"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                prompt_file.parent.mkdir(parents=True, exist_ok=True)
                log_file.write_text("backend log", encoding="utf-8")
                prompt_file.write_text("prompt", encoding="utf-8")

                with mock.patch("aha_cli.services.run_recovery.backend_status", side_effect=stopped_backend):
                    retention_response = asyncio.run(
                        fetch_ui_response(aha_home, run_id, f"/api/runs/{run_id}/retention?top=1")
                    )
                    recovery_response = asyncio.run(
                        fetch_ui_response(aha_home, run_id, f"/api/runs/{run_id}/recovery")
                    )
                    maintenance_response = asyncio.run(
                        fetch_ui_response(aha_home, run_id, f"/api/runs/{run_id}/maintenance?top=1")
                    )
                task = status_snapshot(aha_home, run_id)["tasks"][0]
                events_text = event_path(aha_home, run_id).read_text(encoding="utf-8")

        retention_body = json_response_body(retention_response)
        recovery_body = json_response_body(recovery_response)
        maintenance_body = json_response_body(maintenance_response)
        self.assertTrue(retention_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(retention_body["retention"]["dry_run"])
        self.assertIn("logs/backend.log", {item["path"] for item in retention_body["retention"]["candidates"]})
        self.assertTrue(recovery_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(recovery_body["recovery"]["dry_run"])
        self.assertEqual(recovery_body["recovery"]["candidates"][0]["agent_id"], "main")
        self.assertTrue(maintenance_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(maintenance_body["retention"]["run_id"], run_id)
        self.assertEqual(maintenance_body["recovery"]["candidates"][0]["task_id"], task_id)
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["agents"][0]["status"], "running")
        self.assertNotIn("agent_status_recovered", events_text)

    def test_api_run_maintenance_actions_are_guarded(self) -> None:
        def stopped_backend(_root: Path, _run_id: str, target: str, task_id: str | None) -> dict:
            return {
                "status": "stopped",
                "backend": "codex",
                "target": target,
                "task_id": task_id,
                "last_pid": 1234,
                "stopped_at": "2026-05-31T00:00:00+00:00",
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict("os.environ", {"HOME": tmp}, clear=True), mock.patch(
                "pathlib.Path.cwd",
                return_value=root,
            ):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, current_output = self.run_cli("plan", "Current run", "--agents", "1")
                self.assertEqual(code, 0)
                current_run_id = current_output.splitlines()[0].split(": ", 1)[1]
                code, old_output = self.run_cli("plan", "Old run", "--agents", "1")
                self.assertEqual(code, 0)
                old_run_id = old_output.splitlines()[0].split(": ", 1)[1]
                aha_home = root / ".aha"
                old_log = run_dir(aha_home, old_run_id) / "logs" / "backend.log"
                old_prompt = run_dir(aha_home, old_run_id) / "prompts" / "main.md"
                old_log.parent.mkdir(parents=True, exist_ok=True)
                old_prompt.parent.mkdir(parents=True, exist_ok=True)
                old_log.write_text("backend log", encoding="utf-8")
                old_prompt.write_text("prompt", encoding="utf-8")

                blocked_retention = asyncio.run(
                    fetch_ui_response(
                        aha_home,
                        current_run_id,
                        f"/api/runs/{old_run_id}/retention",
                        method="POST",
                        payload={"action": "archive"},
                    )
                )
                archive_response = asyncio.run(
                    fetch_ui_response(
                        aha_home,
                        current_run_id,
                        f"/api/runs/{old_run_id}/retention",
                        method="POST",
                        payload={"action": "archive", "confirm": "archive"},
                    )
                )
                archive_body = json_response_body(archive_response)
                archive_path = Path(archive_body["retention"]["archive"]["path"])
                archive_exists = archive_path.exists()
                old_log.unlink()
                restore_response = asyncio.run(
                    fetch_ui_response(
                        aha_home,
                        current_run_id,
                        f"/api/runs/{old_run_id}/retention-archive/restore",
                        method="POST",
                        payload={"archive": str(archive_path), "confirm": "restore archive"},
                    )
                )
                old_log_restored = old_log.exists()

                task_id = "task-001"
                set_task_status(aha_home, current_run_id, task_id, "running")
                set_agent_status(aha_home, current_run_id, task_id, "main", "running")
                blocked_recovery = asyncio.run(
                    fetch_ui_response(
                        aha_home,
                        current_run_id,
                        f"/api/runs/{current_run_id}/recovery",
                        method="POST",
                        payload={"task_id": task_id, "agent_id": "main"},
                    )
                )
                with (
                    mock.patch("aha_cli.services.run_recovery.backend_status", side_effect=stopped_backend),
                    mock.patch("aha_cli.web.status.backend_status", side_effect=stopped_backend),
                ):
                    recovery_response = asyncio.run(
                        fetch_ui_response(
                            aha_home,
                            current_run_id,
                            f"/api/runs/{current_run_id}/recovery",
                            method="POST",
                            payload={"task_id": task_id, "agent_id": "main", "confirm": "recover stale agent"},
                        )
                    )
                recovered_task = status_snapshot(aha_home, current_run_id)["tasks"][0]

        self.assertTrue(blocked_retention.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertEqual(json_response_body(blocked_retention)["reason"], "confirm_required")
        self.assertTrue(archive_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(archive_exists)
        self.assertTrue(restore_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(old_log_restored)
        self.assertTrue(blocked_recovery.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertEqual(json_response_body(blocked_recovery)["reason"], "confirm_required")
        self.assertTrue(recovery_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(json_response_body(recovery_response)["recovery"]["recovered_count"], 1)
        self.assertEqual(recovered_task["status"], "awaiting_user")
        self.assertEqual(recovered_task["agents"][0]["status"], "interrupted")

    def test_api_retention_archive_inspect_and_restore_are_run_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, old_output = self.run_cli("plan", "Old run", "--agents", "1")
                self.assertEqual(code, 0)
                old_run_id = old_output.splitlines()[0].split(": ", 1)[1]
                code, current_output = self.run_cli("plan", "Current run", "--agents", "1")
                self.assertEqual(code, 0)
                current_run_id = current_output.splitlines()[0].split(": ", 1)[1]
                aha_home = root / ".aha"
                log_file = run_dir(aha_home, old_run_id) / "logs" / "backend.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_file.write_text("backend log", encoding="utf-8")
                retention = apply_run_retention(aha_home, old_run_id, force=True, now=100000)
                archive_name = Path(retention["archive"]["path"]).name

                list_response = asyncio.run(
                    fetch_ui_response(aha_home, current_run_id, f"/api/runs/{old_run_id}/retention-archives")
                )
                inspect_response = asyncio.run(
                    fetch_ui_response(aha_home, current_run_id, f"/api/runs/{old_run_id}/retention-archives/{archive_name}")
                )
                restore_response = asyncio.run(
                    fetch_ui_response(
                        aha_home,
                        current_run_id,
                        f"/api/runs/{old_run_id}/retention-archives/{archive_name}/restore",
                        method="POST",
                        payload={"confirm": "restore archive"},
                    )
                )
                log_file_restored = log_file.exists()
                current_restore_response = asyncio.run(
                    fetch_ui_response(
                        aha_home,
                        old_run_id,
                        f"/api/runs/{old_run_id}/retention-archives/{archive_name}/restore",
                        method="POST",
                        payload={"confirm": "restore archive"},
                    )
                )
                unsafe_response = asyncio.run(
                    fetch_ui_response(aha_home, current_run_id, f"/api/runs/{old_run_id}/retention-archives/..%2F{archive_name}")
                )

        list_body = json_response_body(list_response)
        inspect_body = json_response_body(inspect_response)
        restore_body = json_response_body(restore_response)
        unsafe_body = json_response_body(unsafe_response)
        self.assertTrue(list_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(list_body["retention_archives"]["archives"][0]["name"], archive_name)
        self.assertTrue(inspect_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(inspect_body["retention_archive"]["archive_name"], archive_name)
        self.assertTrue(restore_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(restore_body["restore"]["restored"][0]["path"], "logs/backend.log")
        self.assertTrue(log_file_restored)
        self.assertTrue(current_restore_response.startswith(b"HTTP/1.1 409 Conflict"))
        self.assertTrue(unsafe_response.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertEqual(unsafe_body["reason"], "invalid_archive_name")

    def test_api_routes_can_target_non_default_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, first_output = self.run_cli("plan", "First session", "--agents", "1")
                self.assertEqual(code, 0)
                first_run_id = first_output.splitlines()[0].split(": ", 1)[1]
                code, second_output = self.run_cli("plan", "Second session", "--agents", "1")
                self.assertEqual(code, 0)
                second_run_id = second_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, second_run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "second-only-event"})
                append_message(root, second_run_id, "main", "second conversation", sender="browser", task_id="task-001", role="main")

                default_status = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, "/api/status")))
                selected_status = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, f"/api/status?run_id={second_run_id}")))
                events = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, f"/api/events?run_id={second_run_id}&offset=0&limit=50")))
                conversation = json_response_body(
                    asyncio.run(fetch_ui_response(root, first_run_id, f"/api/conversation-events?run_id={second_run_id}&task_id=task-001&target=main"))
                )
                send_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        first_run_id,
                        "/api/send",
                        method="POST",
                        payload={"run_id": second_run_id, "target": "manual-target", "message": "sent to second"},
                    )
                )
                task_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        first_run_id,
                        "/api/tasks",
                        method="POST",
                        payload={"run_id": second_run_id, "title": "Second extra task", "dispatch": False},
                    )
                )
                first_after = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, "/api/status")))
                second_after = json_response_body(asyncio.run(fetch_ui_response(root, first_run_id, f"/api/status?run_id={second_run_id}")))
                first_manual, _ = iter_jsonl_from(inbox_path(root, first_run_id, "manual-target"), 0)
                second_manual, _ = iter_jsonl_from(inbox_path(root, second_run_id, "manual-target"), 0)

        self.assertEqual(default_status["run_id"], first_run_id)
        self.assertEqual(default_status["goal"], "First session")
        self.assertEqual(selected_status["run_id"], second_run_id)
        self.assertEqual(selected_status["goal"], "Second session")
        self.assertEqual(events["run_id"], second_run_id)
        self.assertTrue(any(event.get("data", {}).get("text") == "second-only-event" for event in events["events"]))
        self.assertTrue(any(event.get("data", {}).get("message") == "second conversation" for event in conversation["events"]))
        self.assertTrue(send_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(task_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(first_after["tasks"][0]["title"], "Map the relevant files, concepts, and terminology for the goal.")
        self.assertEqual(len(first_after["tasks"]), 1)
        self.assertEqual(len(second_after["tasks"]), 2)
        self.assertEqual(first_manual, [])
        self.assertEqual(second_manual[-1]["message"], "sent to second")

    def test_api_routes_fallback_to_latest_run_without_server_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Default fallback", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                runs_response = asyncio.run(fetch_ui_response(root, "", "/api/runs"))
                status_response = asyncio.run(fetch_ui_response(root, "", "/api/status"))

        self.assertTrue(runs_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(status_response.startswith(b"HTTP/1.1 200 OK"))
        runs_body = json_response_body(runs_response)
        status_body = json_response_body(status_response)
        self.assertEqual(runs_body["default_run_id"], run_id)
        self.assertEqual(status_body["run_id"], run_id)
        self.assertEqual(status_body["goal"], "Default fallback")

    def test_run_routes_module_handles_workspace_and_run_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            workspace = Path(tmp) / "repo"
            root.mkdir()
            workspace.mkdir()

            workspace_response = handle_run_workspace_route(
                root,
                "",
                "POST",
                "/api/workspaces",
                {},
                {},
                json.dumps({"path": str(workspace), "name": "demo"}).encode("utf-8"),
            )
            workspace_body = json_response_body(workspace_response or b"")
            run_response = handle_run_workspace_route(
                root,
                "",
                "POST",
                "/api/runs",
                {},
                {},
                json.dumps({"goal": "Routed run", "mode": "research", "workspace_id": workspace_body["workspace"]["id"]}).encode("utf-8"),
            )
            run_body = json_response_body(run_response or b"")
            run_id = run_body["run"]["id"]
            bootstrap_response = handle_run_workspace_route(root, run_id, "GET", "/api/bootstrap", {}, {}, b"")
            runs_response = handle_run_workspace_route(root, run_id, "GET", "/api/runs", {}, {}, b"")
            plan = read_json(root / "runs" / run_id / "plan.json")

        self.assertTrue((workspace_response or b"").startswith(b"HTTP/1.1 201 Created"))
        self.assertTrue((run_response or b"").startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(plan["tasks"][0]["workspace_id"], "ws-001")
        self.assertEqual(plan["tasks"][0]["workspace_path"], str(workspace))
        self.assertEqual(json_response_body(bootstrap_response or b"")["default_run_id"], run_id)
        self.assertIn(run_id, {item["id"] for item in json_response_body(runs_response or b"")["runs"]})

    def test_run_routes_module_exports_and_imports_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Route archive", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                export_response = handle_run_workspace_route(
                    root,
                    run_id,
                    "GET",
                    "/api/run/export",
                    {"run_id": [run_id], "no_logs": ["1"]},
                    {},
                    b"",
                )
                export_headers, archive_bytes = (export_response or b"").split(b"\r\n\r\n", 1)
                with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as exported:
                    names = set(exported.getnames())

                boundary = "----aha-route-test-boundary"
                multipart_body = (
                    (
                        f"--{boundary}\r\n"
                        'Content-Disposition: form-data; name="archive"; filename="run.tar.gz"\r\n'
                        "Content-Type: application/gzip\r\n"
                        "\r\n"
                    ).encode("ascii")
                    + archive_bytes
                    + f"\r\n--{boundary}--\r\n".encode("ascii")
                )
                import_response = handle_run_workspace_route(
                    root,
                    run_id,
                    "POST",
                    "/api/run/import",
                    {},
                    {"content-type": f"multipart/form-data; boundary={boundary}"},
                    multipart_body,
                )
                import_body = json_response_body(import_response or b"")

        self.assertTrue((export_response or b"").startswith(b"HTTP/1.1 200 OK"))
        self.assertIn(b'Content-Disposition: attachment; filename="aha-run-', export_headers)
        self.assertIn("aha-run-manifest.json", names)
        self.assertTrue((import_response or b"").startswith(b"HTTP/1.1 201 Created"))
        self.assertEqual(import_body["source_run_id"], run_id)
        self.assertNotEqual(import_body["imported_run_id"], run_id)
