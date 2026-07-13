from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs

from aha_cli.cli import append_message, main
from aha_cli.store.filesystem import append_event, event_path, iter_jsonl_from
from aha_cli.web import system_routes
from aha_cli.web.run_api import ApiRunNotFound
from aha_cli.web.system_routes import consume_web_restart_requested, system_route_response
from tests.helpers import json_response_body


class WebSystemRoutesTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def run_git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if not shutil.which("git"):
            self.skipTest("git is not available")
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)

    def test_health_route_does_not_require_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            with mock.patch("aha_cli.web.system_routes.aha_version", return_value="20260531.abcdef0"):
                response = system_route_response(root, "", "GET", "/api/health", {})
                head_response = system_route_response(root, "", "HEAD", "/api/health", {})

        self.assertTrue(response and response.startswith(b"HTTP/1.1 200 OK"))
        body = json_response_body(response)
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "aha-web")
        self.assertEqual(body["aha_home"], str(root))
        self.assertEqual(body["aha_version"], "20260531.abcdef0")
        self.assertEqual(body["bind_port"], "")
        self.assertFalse(body["initialized"])
        self.assertEqual(body["default_run_id"], "")
        self.assertFalse(body["default_run_available"])
        self.assertTrue(head_response and head_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(head_response.split(b"\r\n\r\n", 1)[1], b"")

    def test_access_control_route_reports_bind_risk_from_host_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            local = system_route_response(root, "", "GET", "/api/access-control", {}, headers={"host": "127.0.0.1:8788"})
            remote = system_route_response(root, "", "GET", "/api/access-control", {}, headers={"host": "192.168.1.10:8788"})

        self.assertTrue(local and local.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(remote and remote.startswith(b"HTTP/1.1 200 OK"))
        local_body = json_response_body(local)
        remote_body = json_response_body(remote)
        self.assertEqual(local_body["auth_mode"], "none")
        self.assertEqual(local_body["risk_level"], "low")
        self.assertTrue(local_body["loopback"])
        self.assertEqual(remote_body["risk_level"], "high")
        self.assertFalse(remote_body["loopback"])
        self.assertIn("authenticated reverse proxy", remote_body["recommendation"])

    def test_access_control_route_reports_configured_network_visible_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            response = system_route_response(
                root,
                "",
                "GET",
                "/api/access-control",
                {},
                headers={"host": "127.0.0.1:8788"},
                auth_required=True,
                bind_host="0.0.0.0",
                bind_port=8788,
            )
            health = system_route_response(
                root,
                "",
                "GET",
                "/api/health",
                {},
                headers={"host": "127.0.0.1:8788"},
                auth_required=True,
                bind_host="0.0.0.0",
                bind_port=8788,
            )

        self.assertTrue(response and response.startswith(b"HTTP/1.1 200 OK"))
        body = json_response_body(response)
        self.assertEqual(body["auth_mode"], "token")
        self.assertEqual(body["hostname"], "127.0.0.1")
        self.assertEqual(body["bind_host"], "0.0.0.0")
        self.assertEqual(body["bind_port"], "8788")
        self.assertTrue(body["bind_network_visible"])
        self.assertEqual(body["risk_level"], "high")
        self.assertIn("protected by token auth", body["recommendation"])
        self.assertTrue(health and health.startswith(b"HTTP/1.1 200 OK"))
        health_body = json_response_body(health)
        self.assertEqual(health_body["bind_host"], "0.0.0.0")
        self.assertEqual(health_body["bind_port"], "8788")
        self.assertTrue(health_body["bind_network_visible"])

    def test_system_routes_return_status_backend_and_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "System status", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with mock.patch("aha_cli.web.status.aha_version", return_value="20260527.057e500"):
                    status = system_route_response(root, run_id, "GET", "/api/status", parse_qs("lite=1&task_id=task-001"))
                    tasks = system_route_response(root, run_id, "GET", "/api/tasks", parse_qs("lite=1&task_id=task-001"))
                backends = system_route_response(root, run_id, "HEAD", "/api/backends", {})
                models = system_route_response(root, run_id, "GET", "/api/models", parse_qs("backend=codex"))
                invalid_models = system_route_response(root, run_id, "GET", "/api/models", parse_qs("backend=nope"))
                with mock.patch("aha_cli.web.system_routes.cached_backend_status", return_value={"status": "running", "pid": 123}):
                    backend = system_route_response(root, run_id, "GET", "/api/backend", parse_qs("target=main&task_id=task-001"))
                with mock.patch(
                    "aha_cli.web.system_routes.web_agents_runtime_snapshot",
                    return_value={"task_id": "task-001", "agents": [{"id": "main", "status": "running", "resolved_model": "gpt-5.5"}]},
                ):
                    runtime = system_route_response(root, run_id, "GET", "/api/agents/runtime", parse_qs("task_id=task-001"))
                with mock.patch("aha_cli.web.system_routes.recover_stale_running_agents", return_value={"recovered_count": 0}):
                    recovery = system_route_response(root, run_id, "POST", "/api/agents/recover-stale", parse_qs("task_id=task-001"), b"{}")

        self.assertTrue(status and status.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(json_response_body(status)["run_id"], run_id)
        self.assertEqual(json_response_body(status)["aha_version"], "20260527.057e500")
        self.assertTrue(tasks and tasks.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(json_response_body(tasks)["run_id"], run_id)
        self.assertNotIn("backend_process_status", json_response_body(tasks)["tasks"][0]["agents"][0])
        self.assertTrue(backends and backends.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(backends.split(b"\r\n\r\n", 1)[1], b"")
        self.assertTrue(models and models.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(json_response_body(models)["backend"], "codex")
        self.assertTrue(invalid_models and invalid_models.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertEqual(json_response_body(backend)["pid"], 123)
        self.assertTrue(runtime and runtime.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(json_response_body(runtime)["agents"][0]["resolved_model"], "gpt-5.5")
        self.assertTrue(recovery and recovery.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(json_response_body(recovery)["ok"])

    def test_ui_state_route_persists_selected_task_for_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "UI state", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                global_initial = system_route_response(root, "", "GET", "/api/ui-state", {})
                global_saved = system_route_response(
                    root,
                    "",
                    "PATCH",
                    "/api/ui-state",
                    {},
                    json.dumps({"last_selected_run_id": run_id}).encode("utf-8"),
                )
                initial = system_route_response(root, "", "GET", "/api/ui-state", parse_qs(f"run_id={run_id}"))
                saved = system_route_response(
                    root,
                    "",
                    "PATCH",
                    "/api/ui-state",
                    {},
                    json.dumps({"run_id": run_id, "last_selected_task_id": "task-001"}).encode("utf-8"),
                )
                loaded = system_route_response(root, "", "GET", "/api/ui-state", parse_qs(f"run_id={run_id}"))
                missing_field = system_route_response(root, run_id, "PATCH", "/api/ui-state", {}, b"{}")
                global_state_file = root / ".aha" / "ui_state.json"
                global_state_file_payload = json.loads(global_state_file.read_text(encoding="utf-8"))
                state_file = root / ".aha" / "runs" / run_id / "ui_state.json"
                state_file_payload = json.loads(state_file.read_text(encoding="utf-8"))

        self.assertTrue(global_initial and global_initial.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(json_response_body(global_initial)["last_selected_run_id"], "")
        self.assertTrue(global_saved and global_saved.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(json_response_body(global_saved)["last_selected_run_id"], run_id)
        self.assertTrue(initial and initial.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(json_response_body(initial)["last_selected_run_id"], run_id)
        self.assertEqual(json_response_body(initial)["last_selected_task_id"], "")
        self.assertTrue(saved and saved.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(json_response_body(saved)["last_selected_task_id"], "task-001")
        self.assertTrue(loaded and loaded.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(json_response_body(loaded)["last_selected_task_id"], "task-001")
        self.assertEqual(global_state_file_payload, {"last_selected_run_id": run_id})
        self.assertEqual(state_file_payload, {"last_selected_task_id": "task-001", "last_selected_memo_id": ""})
        self.assertTrue(missing_field and missing_field.startswith(b"HTTP/1.1 400 Bad Request"))

    def test_system_routes_return_events_and_conversation_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "System events", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "hello", sender="browser", task_id="task-001", role="main")
                append_event(root, run_id, "agent_command_finished", {"task_id": "task-001", "target": "main", "command": "pwd", "exit_code": 0, "output_tail": "large output"})

                events = system_route_response(root, run_id, "GET", "/api/events", parse_qs("offset=0&limit=2"))
                conversation = system_route_response(
                    root,
                    run_id,
                    "GET",
                    "/api/conversation-events",
                    parse_qs("task_id=task-001&target=main&categories=chat,commands&limit=20"),
                )
                missing_task = system_route_response(root, run_id, "GET", "/api/conversation-events", parse_qs("target=main"))

        self.assertTrue(events and events.startswith(b"HTTP/1.1 200 OK"))
        events_body = json_response_body(events)
        self.assertEqual(events_body["limit"], 2)
        self.assertTrue(events_body["has_more"])
        self.assertTrue(conversation and conversation.startswith(b"HTTP/1.1 200 OK"))
        conversation_body = json_response_body(conversation)
        self.assertEqual(conversation_body["categories"], ["chat", "commands"])
        self.assertEqual([event["type"] for event in conversation_body["events"]], ["message", "agent_command_finished"])
        self.assertNotIn("output_tail", conversation_body["events"][-1]["data"])
        self.assertTrue(conversation_body["events"][-1]["data"]["output_tail_omitted"])
        self.assertTrue(missing_task and missing_task.startswith(b"HTTP/1.1 400 Bad Request"))

    def test_system_routes_record_debug_and_request_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "System debug", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                debug_body = json.dumps({"run_id": run_id, "seq": 7, "ignored": "private"}).encode("utf-8")
                debug = system_route_response(root, "", "POST", "/api/debug/realtime", {}, debug_body)
                restart = system_route_response(root, run_id, "POST", "/api/web/restart", {}, b"{}")
                restart_requested = consume_web_restart_requested()
                install_bin = root / "bin" / "aha"
                install_bin.parent.mkdir(parents=True)
                install_bin.write_text("#!/bin/sh\n", encoding="utf-8")
                upgrade_env = {
                    "AHA_SOURCE_ROOT": "",
                    "AHA_INSTALL_BIN": str(install_bin),
                    "AHA_SERVICE_NAME": "aha.service",
                    "AHA_RELEASE_REPO": "ChinaKai/AHA",
                    "AHA_RELEASE_VERSION": "latest",
                    "AHA_RELEASE_ASSET": "aha",
                    "AHA_RELEASE_URL": "",
                }
                with mock.patch.dict(os.environ, upgrade_env, clear=False), mock.patch("aha_cli.web.system_routes.subprocess.Popen") as popen:
                    popen.return_value.pid = 12345
                    upgrade = system_route_response(root, run_id, "POST", "/api/web/upgrade", {}, b"{}")
                    upgrade_call = popen.call_args
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                log_text = (root / ".aha" / "runs" / run_id / "logs" / "realtime-debug.log").read_text(encoding="utf-8")

        self.assertTrue(debug and debug.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(restart and restart.startswith(b"HTTP/1.1 200 OK"))
        restart_body = json_response_body(restart)
        self.assertTrue(restart_requested)
        self.assertEqual(restart_body["restart"], "process-exit")
        self.assertEqual(restart_body["exit_code"], 75)
        self.assertTrue(upgrade and upgrade.startswith(b"HTTP/1.1 200 OK"))
        upgrade_body = json_response_body(upgrade)
        self.assertTrue(upgrade_body["ok"])
        self.assertEqual(upgrade_body["upgrade"], "service-upgrade-user")
        expected_upgrade_command = [
            str(install_bin),
            "service",
            "upgrade-user",
            "--bin",
            str(install_bin),
            "--no-health-check",
            "--json",
            "--service-name",
            "aha.service",
            "--repo",
            "ChinaKai/AHA",
            "--version",
            "latest",
            "--asset-name",
            "aha",
        ]
        self.assertEqual(upgrade_body["command"], expected_upgrade_command)
        self.assertEqual(upgrade_body["pid"], 12345)
        self.assertEqual(upgrade_call.args[0], expected_upgrade_command)
        self.assertEqual(Path(upgrade_call.kwargs["cwd"]), Path.home())
        self.assertIn('"source": "client"', log_text)
        self.assertIn('"seq": 7', log_text)
        self.assertNotIn("ignored", log_text)
        self.assertTrue(any(event["type"] == "web_restart_requested" for event in events))
        self.assertTrue(any(event["type"] == "web_upgrade_requested" for event in events))

    def test_web_upgrade_command_does_not_use_runtime_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "other-workspace"
            (workspace / "scripts").mkdir(parents=True)
            (workspace / "scripts" / "install_user_service.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            install_bin = root / "bin" / "aha"
            install_bin.parent.mkdir(parents=True)
            install_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"AHA_SOURCE_ROOT": "", "AHA_INSTALL_BIN": str(install_bin), "AHA_SERVICE_NAME": "aha-test.service"}, clear=False),
                mock.patch("pathlib.Path.cwd", return_value=workspace),
            ):
                command = system_routes._web_upgrade_command()

        self.assertEqual(command[:5], [str(install_bin), "service", "upgrade-user", "--bin", str(install_bin)])
        self.assertNotIn(str(workspace), " ".join(command))

    def test_web_upgrade_command_requires_installed_onebin_for_source_runtime(self) -> None:
        with (
            mock.patch.dict(os.environ, {"AHA_SOURCE_ROOT": "", "AHA_INSTALL_BIN": ""}, clear=False),
            mock.patch("sys.argv", ["/usr/bin/python3"]),
        ):
            with self.assertRaises(FileNotFoundError):
                system_routes._web_upgrade_command()

    def test_web_upgrade_route_rejects_source_runtime_without_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Source upgrade hidden", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
            install_bin = root / "bin" / "aha"
            install_bin.parent.mkdir(parents=True)
            install_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"AHA_SOURCE_ROOT": str(root), "AHA_INSTALL_BIN": str(install_bin)}, clear=False),
                mock.patch("aha_cli.web.system_routes.subprocess.Popen") as popen,
            ):
                response = system_route_response(root, run_id, "POST", "/api/web/upgrade", {}, b"{}")

        self.assertTrue(response and response.startswith(b"HTTP/1.1 409 Conflict"))
        body = json_response_body(response)
        self.assertFalse(body["web_upgrade"]["available"])
        self.assertEqual(body["web_upgrade"]["action"], "publish")
        self.assertEqual(body["web_upgrade"]["mode"], "source")
        popen.assert_not_called()

    def test_web_publish_route_commits_next_tag_and_pushes_to_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "aha-home"
            source = tmp_path / "source"
            remote = tmp_path / "remote.git"
            root.mkdir()
            source.mkdir()
            remote.mkdir()
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Publish release", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

            self.run_git(remote, "init", "--bare", "--initial-branch=main")
            self.run_git(source, "init", "--initial-branch=main")
            self.run_git(source, "remote", "add", "origin", str(remote))
            (source / "README.md").write_text("# AHA\n", encoding="utf-8")
            self.run_git(source, "add", "README.md")
            self.run_git(
                source,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "chore: initial",
            )
            self.run_git(source, "tag", "v1.2.3")
            self.run_git(source, "push", "-u", "origin", "main", "--tags")
            (source / "CHANGELOG.md").write_text("release notes\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"AHA_SOURCE_ROOT": str(source), "AHA_INSTALL_BIN": ""}, clear=False):
                preview_response = system_route_response(root, run_id, "GET", "/api/web/publish/status", {}, b"")
                response = system_route_response(root, run_id, "POST", "/api/web/publish", {}, json.dumps({"tag": "v1.2.9"}).encode("utf-8"))

            events, _ = iter_jsonl_from(event_path(root, run_id), 0)

            self.assertTrue(preview_response and preview_response.startswith(b"HTTP/1.1 200 OK"))
            preview = json_response_body(preview_response)
            self.assertTrue(preview["ok"])
            self.assertEqual(preview["publish"], "source-release-preview")
            self.assertTrue(preview["dirty"])
            self.assertEqual(preview["dirty_count"], 1)
            self.assertIn("CHANGELOG.md", preview["changed_paths"])
            self.assertEqual(preview["ahead"], 0)
            self.assertEqual(preview["behind"], 0)
            self.assertEqual(preview["latest_tag"], "v1.2.3")
            self.assertEqual(preview["next_tag"], "v1.2.4")
            self.assertTrue(response and response.startswith(b"HTTP/1.1 200 OK"))
            body = json_response_body(response)
            self.assertTrue(body["ok"])
            self.assertEqual(body["publish"], "source-release")
            self.assertEqual(body["previous_tag"], "v1.2.3")
            self.assertEqual(body["tag"], "v1.2.9")
            self.assertTrue(body["committed"])
            self.assertEqual(body["branch"], "main")
            self.assertIn("main", body["pushed"])
            self.assertIn("v1.2.9", body["pushed"])
            self.assertEqual(self.run_git(source, "status", "--porcelain").stdout.strip(), "")
            self.assertEqual(self.run_git(source, "log", "-1", "--format=%s").stdout.strip(), "chore(release): publish v1.2.9")
            self.assertEqual(self.run_git(source, "rev-parse", "HEAD").stdout.strip(), body["commit"])
            remote_tag = self.run_git(source, "ls-remote", "--tags", "origin", "refs/tags/v1.2.9").stdout.strip()
            self.assertIn("refs/tags/v1.2.9", remote_tag)
            self.assertTrue(any(event["type"] == "web_publish_requested" for event in events))

            with mock.patch.dict(os.environ, {"AHA_SOURCE_ROOT": str(source), "AHA_INSTALL_BIN": ""}, clear=False):
                duplicate = system_route_response(root, run_id, "POST", "/api/web/publish", {}, json.dumps({"tag": "v1.2.9"}).encode("utf-8"))
                invalid = system_route_response(root, run_id, "POST", "/api/web/publish", {}, json.dumps({"tag": "release-1"}).encode("utf-8"))
            self.assertTrue(duplicate and duplicate.startswith(b"HTTP/1.1 400 Bad Request"))
            self.assertIn("already exists", json_response_body(duplicate)["error"])
            self.assertTrue(invalid and invalid.startswith(b"HTTP/1.1 400 Bad Request"))
            self.assertIn("vX.Y.Z", json_response_body(invalid)["error"])

    def test_web_upgrade_env_uses_core_proxy_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aha_home = root / ".aha"
            aha_home.mkdir()
            (aha_home / "config.json").write_text(
                json.dumps(
                    {
                        "proxy": {
                            "enabled": True,
                            "http_proxy": "http://127.0.0.1:7897",
                            "https_proxy": "http://127.0.0.1:7897",
                            "no_proxy": "localhost,127.0.0.1,::1",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://old", "HTTPS_PROXY": "http://old"}, clear=False):
                env = system_routes._web_upgrade_env(root)

        self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1,::1")
        self.assertEqual(env["http_proxy"], "http://127.0.0.1:7897")
        self.assertEqual(env["https_proxy"], "http://127.0.0.1:7897")
        self.assertEqual(env["no_proxy"], "localhost,127.0.0.1,::1")

    def test_realtime_debug_rejects_deleted_run_without_recreating_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Deleted debug", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                run_path = root / ".aha" / "runs" / run_id
                shutil.rmtree(run_path)
                debug_body = json.dumps({"run_id": run_id, "seq": 8}).encode("utf-8")

                with self.assertRaises(ApiRunNotFound):
                    system_route_response(root, "", "POST", "/api/debug/realtime", {}, debug_body)

            self.assertFalse(run_path.exists())

    def test_system_routes_handle_weixin_console_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Weixin routes", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                status_payload = {"ok": True, "paired": False, "pairing": None}
                pair_payload = {"ok": True, "paired": False, "pairing": {"status": "waiting", "qrcode_svg": "<svg/>"}}
                reset_payload = {"ok": True, "paired": False, "pairing": None, "account": None, "error": ""}
                sent_payload = {"ok": True, "sent": True, "message_id": "msg-1", "target": "user-1@im.wechat"}
                notifications_payload = {"enabled": True, "ready": True, "sent_count": 0, "updated_at": "now", "last_sent_at": ""}
                reset_notifications_payload = {"enabled": False, "ready": False, "sent_count": 0, "updated_at": "now", "last_sent_at": ""}
                with (
                    mock.patch("aha_cli.web.system_routes.weixin_status_snapshot", return_value=status_payload) as status_snapshot,
                    mock.patch("aha_cli.web.system_routes.start_pairing", return_value=pair_payload) as start_pair,
                    mock.patch("aha_cli.web.system_routes.reset_pairing", return_value=reset_payload) as reset_pair,
                    mock.patch("aha_cli.web.system_routes.send_test_notification", return_value=sent_payload) as send_test,
                    mock.patch(
                        "aha_cli.web.system_routes.set_notifications_enabled",
                        side_effect=[reset_notifications_payload, notifications_payload],
                    ) as set_notifications,
                ):
                    status = system_route_response(root, run_id, "GET", "/api/weixin", parse_qs(""))
                    pair = system_route_response(root, run_id, "POST", "/api/weixin/pair", parse_qs(""))
                    reset = system_route_response(root, run_id, "POST", "/api/weixin/reset", parse_qs(""))
                    test = system_route_response(
                        root,
                        run_id,
                        "POST",
                        "/api/weixin/test",
                        parse_qs(""),
                        json.dumps({"message": "hello"}).encode("utf-8"),
                    )
                    notifications = system_route_response(
                        root,
                        run_id,
                        "POST",
                        "/api/weixin/notifications",
                        parse_qs(""),
                        json.dumps({"enabled": True}).encode("utf-8"),
                    )
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)

        self.assertTrue(status and status.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(pair and pair.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(reset and reset.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(test and test.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(notifications and notifications.startswith(b"HTTP/1.1 200 OK"))
        self.assertFalse(json_response_body(status)["paired"])
        self.assertFalse(json_response_body(status)["notifications"]["enabled"])
        self.assertEqual(json_response_body(pair)["pairing"]["status"], "waiting")
        self.assertFalse(json_response_body(reset)["paired"])
        self.assertFalse(json_response_body(reset)["notifications"]["enabled"])
        self.assertEqual(json_response_body(test)["message_id"], "msg-1")
        self.assertTrue(json_response_body(notifications)["notifications"]["enabled"])
        status_snapshot.assert_called_once_with(root, run_id)
        start_pair.assert_called_once_with(root, run_id)
        reset_pair.assert_called_once_with(root, run_id)
        send_test.assert_called_once_with(root, run_id, "hello")
        self.assertEqual(
            set_notifications.call_args_list,
            [mock.call(root, run_id, False), mock.call(root, run_id, True)],
        )
        self.assertTrue(any(event["type"] == "weixin_pairing_reset" for event in events))
        self.assertTrue(any(event["type"] == "weixin_notifications_updated" for event in events))

    def test_weixin_status_fetches_recent_received_messages_when_paired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Weixin received", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                status_payload = {"ok": True, "paired": True, "pairing": None, "received_messages": []}
                updates_payload = {
                    "ok": True,
                    "message_count": 2,
                    "recent_messages": [
                        {"from_user_id": "user-1@im.wechat", "text": "second", "received_at": "2026-05-25T00:00:02+00:00"},
                        {"from_user_id": "user-1@im.wechat", "text": "first", "received_at": "2026-05-25T00:00:01+00:00"},
                    ],
                }
                with (
                    mock.patch("aha_cli.web.system_routes.weixin_status_snapshot", return_value=status_payload),
                    mock.patch("aha_cli.web.system_routes.recent_received_messages", return_value=[]) as recent_messages,
                    mock.patch("aha_cli.web.system_routes.fetch_updates", return_value=updates_payload) as fetch_updates,
                    mock.patch("aha_cli.web.system_routes.notification_status", return_value={"enabled": False}),
                ):
                    status = system_route_response(root, run_id, "GET", "/api/weixin", parse_qs(""))

        self.assertTrue(status and status.startswith(b"HTTP/1.1 200 OK"))
        body = json_response_body(status)
        self.assertEqual(body["received_message_count"], 2)
        self.assertEqual([item["text"] for item in body["received_messages"]], ["second", "first"])
        recent_messages.assert_called_once_with(root)
        fetch_updates.assert_called_once_with(root)
