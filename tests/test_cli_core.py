from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from aha_cli.cli import append_message, main, task_dashboard_html, task_snapshot
from aha_cli.services.chat import chat_prompt
from aha_cli.services.commit_policy import format_commit_message, validate_commit_message
from aha_cli.services.orchestrator import task_assignment_prompt
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.filesystem import (
    add_agent,
    append_event,
    conversation_events_page,
    delete_task,
    event_path,
    inbox_path,
    iter_jsonl_from,
    read_json,
    set_agent_status,
    set_task_hidden,
    status_snapshot,
    task_context_snapshot,
    task_final_snapshot,
    update_agent_config,
    update_agent_runtime,
    update_task_proxy_config,
    write_task_result,
)
from aha_cli.web.server import format_agent_command, format_aha_command, handle_slash_command, workspace_options


class CliCoreTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_plan_run_merge_with_stub_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                code, _ = self.run_cli("init", "--portable")
                self.assertEqual(code, 0)

                code, plan_output = self.run_cli("plan", "Study a repo", "--agents", "2")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, _ = self.run_cli("run", run_id, "--parallel", "2")
                self.assertEqual(code, 0)

                code, status = self.run_cli("status", run_id)
                self.assertEqual(code, 0)
                self.assertIn("[completed]", status)

                code, merge_output = self.run_cli("merge", run_id)
                self.assertEqual(code, 0)
                self.assertIn("merged-report.md", merge_output)
                self.assertTrue((root / ".aha" / "runs" / run_id / "merged-report.md").exists())

    def test_plan_uses_aha_home_env_without_local_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "workspace"
            home = Path(tmp) / "aha-home"
            cwd.mkdir()
            with mock.patch("pathlib.Path.cwd", return_value=cwd), mock.patch.dict(os.environ, {"AHA_HOME": str(home)}):
                code, plan_output = self.run_cli("plan", "Home env", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = next(line.split(": ", 1)[1] for line in plan_output.splitlines() if line.startswith("Created run:"))

                self.assertTrue((home / "config.json").exists())
                self.assertTrue((home / "runs" / run_id / "plan.json").exists())
                self.assertFalse((cwd / ".aha").exists())
                plan = read_json(home / "runs" / run_id / "plan.json")
                self.assertEqual(plan["tasks"][0]["workspace_path"], str(cwd))

                code, status = self.run_cli("status", run_id)
                self.assertEqual(code, 0)
                self.assertIn("Goal: Home env", status)

    def test_global_home_option_uses_custom_aha_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "workspace"
            home = Path(tmp) / "custom-home"
            cwd.mkdir()
            with mock.patch("pathlib.Path.cwd", return_value=cwd):
                code, plan_output = self.run_cli("--home", str(home), "plan", "Custom home", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = next(line.split(": ", 1)[1] for line in plan_output.splitlines() if line.startswith("Created run:"))

                self.assertTrue((home / "config.json").exists())
                self.assertTrue((home / "runs" / run_id / "plan.json").exists())
                self.assertFalse((cwd / ".aha").exists())

                code, status = self.run_cli("--home", str(home), "status", run_id)
                self.assertEqual(code, 0)
                self.assertIn("Goal: Custom home", status)

    def test_init_uses_aha_home_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "workspace"
            home = Path(tmp) / "env-home"
            cwd.mkdir()
            with mock.patch("pathlib.Path.cwd", return_value=cwd), mock.patch.dict(os.environ, {"AHA_HOME": str(home)}):
                code, output = self.run_cli("init")

                self.assertEqual(code, 0)
                self.assertIn(f"Initialized AHA home: {home}", output)
                self.assertTrue((home / "config.json").exists())
                self.assertFalse((cwd / ".aha").exists())

    def test_init_defaults_to_user_aha_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "workspace"
            user_home = Path(tmp) / "user-home"
            expected_home = user_home / ".aha"
            cwd.mkdir()
            user_home.mkdir()
            with (
                mock.patch("pathlib.Path.cwd", return_value=cwd),
                mock.patch("pathlib.Path.home", return_value=user_home),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                code, output = self.run_cli("init")

                self.assertEqual(code, 0)
                self.assertIn(f"Initialized AHA home: {expected_home}", output)
                self.assertTrue((expected_home / "config.json").exists())
                self.assertFalse((cwd / ".aha").exists())

    def test_init_portable_uses_local_dot_aha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "workspace"
            cwd.mkdir()
            with mock.patch("pathlib.Path.cwd", return_value=cwd), mock.patch.dict(os.environ, {}, clear=True):
                code, output = self.run_cli("init", "--portable")

                self.assertEqual(code, 0)
                self.assertIn(f"Initialized AHA home: {cwd / '.aha'}", output)
                self.assertTrue((cwd / ".aha" / "config.json").exists())

    def test_workspace_registry_can_drive_plan_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "launcher"
            home = Path(tmp) / "aha-home"
            workspace = Path(tmp) / "repo"
            cwd.mkdir()
            workspace.mkdir()
            with mock.patch("pathlib.Path.cwd", return_value=cwd):
                code, add_output = self.run_cli("--home", str(home), "workspace", "add", str(workspace), "--name", "demo")
                self.assertEqual(code, 0)
                self.assertIn(f"ws-001 demo {workspace}", add_output)
                self.assertTrue((home / "workspaces" / "ws-001.json").exists())

                code, list_output = self.run_cli("--home", str(home), "workspace", "list")
                self.assertEqual(code, 0)
                self.assertIn(f"ws-001 demo {workspace}", list_output)

                code, plan_output = self.run_cli("--home", str(home), "plan", "Workspace plan", "--agents", "1", "--workspace", "ws-001")
                self.assertEqual(code, 0)
                run_id = next(line.split(": ", 1)[1] for line in plan_output.splitlines() if line.startswith("Created run:"))
                plan = read_json(home / "runs" / run_id / "plan.json")
                self.assertEqual(plan["tasks"][0]["workspace_id"], "ws-001")
                self.assertEqual(plan["tasks"][0]["workspace_path"], str(workspace))

    def test_ui_can_start_without_existing_run(self) -> None:
        async def fake_ui_server(root: Path, run_id: str, host: str, port: int, poll_interval: int) -> None:
            calls.append((root, run_id, host, port, poll_interval))

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            calls: list[tuple[Path, str, str, int, int]] = []
            with mock.patch("aha_cli.cli.run_ui_server", side_effect=fake_ui_server):
                code, _ = self.run_cli("--home", str(home), "ui", "--host", "127.0.0.1", "--port", "0")

            self.assertEqual(code, 0)
            self.assertTrue((home / "config.json").exists())
            self.assertEqual(calls, [(home, "", "127.0.0.1", 0, 1000)])

    def test_empty_command_defaults_to_ui(self) -> None:
        async def fake_ui_server(root: Path, run_id: str, host: str, port: int, poll_interval: int) -> None:
            calls.append((root, run_id, host, port, poll_interval))

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            calls: list[tuple[Path, str, str, int, int]] = []
            with mock.patch("aha_cli.cli.run_ui_server", side_effect=fake_ui_server):
                code, _ = self.run_cli("--home", str(home))

            self.assertEqual(code, 0)
            self.assertTrue((home / "config.json").exists())
            self.assertEqual(calls, [(home, "", "0.0.0.0", 8766, 1000)])

    def test_explicit_tasks_are_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, output = self.run_cli(
                    "plan",
                    "Goal",
                    "--task",
                    "Task A",
                    "--task",
                    "Task B",
                )
                self.assertEqual(code, 0)
                self.assertIn("Task A", output)
                self.assertIn("Task B", output)

    def test_send_and_watch_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable")
                code, plan_output = self.run_cli("plan", "Observe agents", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, send_output = self.run_cli("send", run_id, "task-001", "hello", "agent")
                self.assertEqual(code, 0)
                self.assertIn("hello agent", send_output)

                code, watch_output = self.run_cli("watch", run_id, "--once")
                self.assertEqual(code, 0)
                self.assertIn("Observe agents", watch_output)
                self.assertIn("message main -> task-001: hello agent", watch_output)

    def test_auto_reply_writes_response_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable")
                code, plan_output = self.run_cli("plan", "Reply demo", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, _ = self.run_cli("send", run_id, "main", "你好", "--sender", "browser")
                self.assertEqual(code, 0)

                code, reply_output = self.run_cli("auto-reply", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("main -> browser: 收到：你好", reply_output)

                code, watch_output = self.run_cli("watch", run_id, "--once")
                self.assertEqual(code, 0)
                self.assertIn("message browser -> main: 你好", watch_output)
                self.assertIn("message main -> browser: 收到：你好", watch_output)

    def test_prompts_include_commit_ownership_policy(self) -> None:
        assignment_prompt = task_assignment_prompt(
            {
                "id": "task-001",
                "title": "Commit work",
                "workspace_path": "/tmp/project",
                "max_sub_agents": 2,
                "delegation_policy": "auto",
                "preferred_backend": "codex",
            }
        )
        self.assertIn("Commit ownership policy:", assignment_prompt)
        self.assertIn("route it to that sub-agent with `route_to_agent`", assignment_prompt)
        self.assertIn("Never ask a sub-agent to commit files outside its assignment", assignment_prompt)
        self.assertIn("Commit message policy:", assignment_prompt)
        self.assertIn("Generated-by: AHA Codex GPT-5.5", assignment_prompt)
        self.assertIn("Keep task, agent, and scope tracking in the AHA journal", assignment_prompt)
        self.assertNotIn("AHA-Task: task-001", assignment_prompt)
        self.assertIn("return ONLY one JSON object", assignment_prompt)
        self.assertIn('"actions"', assignment_prompt)
        self.assertIn("Completed, stopped, failed, interrupted, or blocked", assignment_prompt)
        self.assertIn("Include a stable `scope_id`", assignment_prompt)
        self.assertIn("include `agent_id` in that `spawn_sub` action", assignment_prompt)
        self.assertIn("Collaboration mode:", assignment_prompt)
        self.assertIn("agent owns the efficiency decision", assignment_prompt)
        self.assertIn("never split work just to use more agents", assignment_prompt)
        self.assertIn("Spend the first 60 seconds decomposing", assignment_prompt)
        self.assertIn("optimize for end-to-end efficiency", assignment_prompt)
        self.assertIn("reduce the critical path", assignment_prompt)
        self.assertIn("simple or tightly coupled work", assignment_prompt)
        self.assertIn("Do not split work just to use more agents", assignment_prompt)
        self.assertIn("raise your parallelism sensitivity", assignment_prompt)
        self.assertIn("state the practical reason briefly", assignment_prompt)
        self.assertIn("clear scope/file ownership", assignment_prompt)
        self.assertIn("Task-main owns integration, final review, verification, and commits", assignment_prompt)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Commit routing", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                update_agent_runtime(root, run_id, "task-001", "sub-001", assignment="UI routing changes")
                main_message = append_message(root, run_id, "main", "提交代码", sender="browser", task_id="task-001", role="main")
                main_prompt = chat_prompt(root, run_id, "main", main_message, "")

                self.assertIn("Commit ownership policy:", main_prompt)
                self.assertIn("Route format:", main_prompt)
                self.assertIn('"type": "route_to_agent"', main_prompt)
                self.assertIn('"type": "record_task_update"', main_prompt)
                self.assertNotIn("Return a JSON action `route_to_agent`", main_prompt)
                self.assertIn("route commit work to the sub-agent that owns the changed scope", main_prompt)
                self.assertIn("Commit message policy:", main_prompt)
                self.assertIn("Generated-by: AHA Codex GPT-5.5", main_prompt)
                self.assertIn("Keep task, agent, and scope tracking in the AHA journal", main_prompt)
                self.assertNotIn("AHA-Task: task-001", main_prompt)
                self.assertNotIn("AHA-Agent: main", main_prompt)
                self.assertIn("aha commit --type <type>", main_prompt)
                self.assertIn("UI routing changes", main_prompt)
                self.assertIn("Completed, stopped, failed, interrupted, or blocked", main_prompt)
                self.assertIn("Include a stable `scope_id`", main_prompt)
                self.assertIn("Spawn/reassign format:", main_prompt)
                self.assertIn("spend the first 60 seconds decomposing", main_prompt)
                self.assertIn("optimize for end-to-end efficiency", main_prompt)
                self.assertIn("reduce the critical path", main_prompt)
                self.assertIn("simple or tightly coupled work", main_prompt)
                self.assertIn("Do not split work just to use more agents", main_prompt)
                self.assertIn("raise your parallelism sensitivity", main_prompt)
                self.assertIn("state the practical reason briefly", main_prompt)
                self.assertIn("disjoint scope/file ownership", main_prompt)
                self.assertIn("task-main responsible for integration, final review, verification, and commits", main_prompt)

                sub_message = append_message(root, run_id, "sub-001", "提交你负责的部分", sender="main", task_id="task-001", role="sub")
                sub_prompt = chat_prompt(root, run_id, "sub-001", sub_message, "")

                self.assertIn("commit only files covered by your `assignment` / `created_reason`", sub_prompt)
                self.assertIn("report back to `task-main`", sub_prompt)
                self.assertIn("Generated-by: AHA Codex GPT-5.5", sub_prompt)
                self.assertNotIn("AHA-Task: task-001", sub_prompt)
                self.assertNotIn("AHA-Agent: sub-001", sub_prompt)

    def test_task_add_collaboration_modes_map_to_sub_agent_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Collaboration modes", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, task_output = self.run_cli(
                    "task",
                    "add",
                    run_id,
                    "Pair task",
                    "--collaboration-mode",
                    "pair",
                    "--no-dispatch",
                )
                self.assertEqual(code, 0)
                task = json.loads(task_output)

        self.assertEqual(task["collaboration_mode"], "pair")
        self.assertEqual(task["delegation_policy"], "auto")
        self.assertEqual(task["max_sub_agents"], 1)

    def test_task_add_legacy_disabled_delegation_infers_solo_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Legacy delegation", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, task_output = self.run_cli(
                    "task",
                    "add",
                    run_id,
                    "Solo task",
                    "--delegation-policy",
                    "disabled",
                    "--no-dispatch",
                )
                self.assertEqual(code, 0)
                task = json.loads(task_output)

        self.assertEqual(task["collaboration_mode"], "solo")
        self.assertEqual(task["delegation_policy"], "disabled")
        self.assertEqual(task["max_sub_agents"], 0)

    def test_commit_policy_formats_validates_and_prints_dry_run_messages(self) -> None:
        message = format_commit_message("feat", "add lazy loading", scope="web")

        self.assertEqual(validate_commit_message(message), [])
        self.assertIn("feat(web): add lazy loading", message)
        self.assertIn("Generated-by: AHA Codex GPT-5.5", message)
        self.assertNotIn("AHA-Task:", message)
        self.assertNotIn("AHA-Agent:", message)
        self.assertNotIn("AHA-Scope:", message)
        self.assertTrue(validate_commit_message("update stuff\n\nGenerated-by: AHA Codex GPT-5.5\n"))
        self.assertIn(
            "commit body must include exactly one Generated-by trailer",
            validate_commit_message("fix(web): missing generator\n"),
        )
        self.assertEqual(validate_commit_message("fix(web): alternate generator\n\nGenerated-by: AHA Codex GPT-5.4\n"), [])
        self.assertIn(
            "commit body Generated-by value must be exactly: AHA Codex GPT-5.5",
            validate_commit_message(
                "fix(web): wrong generator\n\nGenerated-by: AHA Codex GPT-5.4\n",
                expected_generated_by="AHA Codex GPT-5.5",
            ),
        )
        self.assertIn(
            "commit body must include exactly one Generated-by trailer",
            validate_commit_message(
                "fix(web): duplicate generator\n\n"
                "Generated-by: AHA Codex GPT-5.5\n"
                "Generated-by: AHA Codex GPT-5.5\n"
            ),
        )
        self.assertIn(
            "commit body should not include AHA task/agent/scope trailers; keep that tracking in the AHA journal",
            validate_commit_message(
                "fix(web): old metadata\n\n"
                "Generated-by: AHA Codex GPT-5.5\n"
                "AHA-Task: task-001\n"
                "AHA-Agent: main\n"
            ),
        )

        code, output = self.run_cli(
            "commit",
            "--type",
            "fix",
            "--scope",
            "web",
            "--summary",
            "keep logs scroll stable",
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertIn("fix(web): keep logs scroll stable", output)
        self.assertIn("Generated-by: AHA Codex GPT-5.5", output)
        self.assertNotIn("AHA-Task:", output)
        self.assertNotIn("AHA-Agent:", output)
        with mock.patch.dict(os.environ, {"AHA_BACKEND": "codex", "AHA_MODEL": "gpt-5.4"}, clear=False):
            code, dynamic_output = self.run_cli(
                "commit",
                "--type",
                "fix",
                "--scope",
                "web",
                "--summary",
                "use task generator",
                "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertIn("Generated-by: AHA Codex GPT-5.4", dynamic_output)
        code, legacy_output = self.run_cli(
            "commit",
            "--type",
            "fix",
            "--scope",
            "web",
            "--summary",
            "accept legacy metadata flags",
            "--task-id",
            "task-005",
            "--agent",
            "main",
            "--aha-scope",
            "log-scroll",
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertIn("Generated-by: AHA Codex GPT-5.5", legacy_output)
        self.assertNotIn("AHA-Task:", legacy_output)
        self.assertNotIn("AHA-Agent:", legacy_output)
        self.assertNotIn("AHA-Scope:", legacy_output)
        with tempfile.TemporaryDirectory() as tmp:
            message_file = Path(tmp) / "COMMIT_EDITMSG"
            message_file.write_text(message, encoding="utf-8")
            code, output = self.run_cli("commit-check", str(message_file))
            self.assertEqual(code, 0)
            expected_code, _ = self.run_cli("commit-check", "--generated-by", "AHA Codex GPT-5.4", str(message_file))
        self.assertIn("Commit message OK", output)
        self.assertEqual(expected_code, 1)

    def test_task_dashboard_and_metadata_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable")
                code, plan_output = self.run_cli("plan", "Task UI", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                payload = append_message(root, run_id, "main", "hello", sender="browser", task_id="task-001", role="main")
                self.assertEqual(payload["task_id"], "task-001")
                self.assertEqual(payload["role"], "main")

                detail = task_snapshot(root, run_id, "task-001")
                self.assertIn("prompt", detail)
                self.assertEqual(detail["task"]["id"], "task-001")

                html = task_dashboard_html(run_id, 1000)
                self.assertIn("task-list", html)
                self.assertIn("agent-target", html)
                self.assertIn("workspace-select", html)
                self.assertIn("workspace-custom", html)
                self.assertIn("show-hidden", html)
                self.assertIn('id="task-model"', html)
                self.assertIn('id="task-sandbox"', html)
                self.assertIn('id="task-approval"', html)
                self.assertIn('id="task-http-proxy"', html)
                self.assertIn('id="task-https-proxy"', html)
                self.assertIn('id="task-no-proxy"', html)
                self.assertIn('id="task-proxy-editor"', html)
                self.assertIn("selected-task-meta", html)
                self.assertIn("selected-agent-info", html)
                self.assertIn("backend-status", html)
                self.assertIn("pending-messages", html)
                self.assertIn("command-menu", html)
                self.assertIn("conversation-filters", html)
                self.assertIn('data-tab="final"', html)

    def test_package_onebin_builds_executable_with_ui_static(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "aha"
            code, output = self.run_cli("package", "onebin", "--output", str(artifact))
            self.assertEqual(code, 0, output)
            self.assertTrue(artifact.is_file())
            self.assertTrue(os.access(artifact, os.X_OK))

            help_run = subprocess.run([str(artifact), "--help"], capture_output=True, text=True, timeout=10)
            self.assertEqual(help_run.returncode, 0, help_run.stderr)
            self.assertIn("Agent-help-agent", help_run.stdout)

            bad_check = subprocess.run(
                [str(artifact), "commit-check", "--generated-by", "AHA Codex GPT-5.5", "-"],
                input="fix(web): wrong generator\n\nGenerated-by: AHA Codex GPT-5.4\n",
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(bad_check.returncode, 1, bad_check.stderr)
            self.assertIn("Generated-by value must be exactly", bad_check.stderr)

            aha_home = root / ".aha"
            workspace = root / "workspace"
            workspace.mkdir()
            init_run = subprocess.run(
                [str(artifact), "--home", str(aha_home), "init", "--backend", "stub"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(init_run.returncode, 0, init_run.stderr)
            plan_run = subprocess.run(
                [str(artifact), "--home", str(aha_home), "plan", "One-bin run", "--agents", "1", "--workspace-path", str(workspace)],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(plan_run.returncode, 0, plan_run.stderr)
            run_id = plan_run.stdout.splitlines()[0].split(": ", 1)[1]

            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            proc = subprocess.Popen(
                [str(artifact), "--home", str(aha_home), "ui", run_id, "--host", "127.0.0.1", "--port", str(port)],
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                html = ""
                for _ in range(50):
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2) as response:
                            html = response.read().decode("utf-8")
                        break
                    except (urllib.error.URLError, TimeoutError):
                        time.sleep(0.1)
                if not html:
                    stdout, stderr = proc.communicate(timeout=1)
                    self.fail(f"one-bin UI did not start\nstdout={stdout}\nstderr={stderr}")
                self.assertIn('id="run-export"', html)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/static/app.js", timeout=1) as response:
                    script = response.read().decode("utf-8")
                self.assertIn("runExportEl", script)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate(timeout=5)
                else:
                    proc.communicate(timeout=1)

    def test_aha_status_command_formats_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Command help", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                output = format_aha_command(root, run_id, "task-001", "/aha status")
                self.assertIn("Task: task-001", output)
                self.assertIn("Status: pending", output)

                backend_output = format_aha_command(root, run_id, "task-001", "/aha backend status")
                self.assertIn("Unknown AHA command", backend_output)

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/aha status",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertEqual(payload["message"]["sender"], "AHA")
                self.assertEqual(payload["message"]["agent_id"], "main")
                page = conversation_events_page(root, run_id, "task-001", "main", limit=10)
                messages = [event["data"] for event in page["events"] if event["type"] == "message"]
                self.assertTrue(any(message.get("message") == "/aha status" and message.get("agent_id") == "main" for message in messages))
                self.assertTrue(any(message.get("sender") == "AHA" and "Task: task-001" in message.get("message", "") for message in messages))

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("AHA commands:", payload["message"]["message"])

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/aha final",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("message", payload)
                main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                self.assertEqual(main_messages[-1]["sender"], "aha")
                self.assertEqual(main_messages[-1]["result_policy"], "finalize")
                self.assertEqual(main_messages[-1]["original_command"], "/aha final")
                self.assertIn("Generate or update the task Final", main_messages[-1]["message"])

                handled, agent_message, reply = format_agent_command(root, run_id, "task-001", "main", "/agent help")
                self.assertFalse(handled)
                self.assertEqual(agent_message, "/help")
                self.assertIsNone(reply)

                handled, agent_message, reply = format_agent_command(root, run_id, "task-001", "main", "/agent status")
                self.assertFalse(handled)
                self.assertEqual(agent_message, "/status")
                self.assertIsNone(reply)

                handled, agent_message, reply = format_agent_command(root, run_id, "task-001", "main", "/agent")
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("Usage: /agent <command>", reply or "")

    def test_backend_cli_command_is_not_exposed(self) -> None:
        err = io.StringIO()
        with mock.patch("sys.stderr", err), self.assertRaises(SystemExit) as raised:
            main(["backend"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice: 'backend'", err.getvalue())

    def test_watch_tail_starts_at_current_event_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Tail watch", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "old-event"})

                code, watch_output = self.run_cli("watch", run_id, "--once", "--tail")

        self.assertEqual(code, 0)
        self.assertIn("Tail watch", watch_output)
        self.assertNotIn("old-event", watch_output)

    def test_prompt_templates_are_packaged_and_renderable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        prompt = render_prompt_template(
            "backend_chat_delta.md",
            prefix="prefix",
            target="main",
            mode_instruction="reply",
            run_goal="goal",
            status="status",
            sticky_context="context",
            recent_events="events",
            sender="browser",
            ts="2026-01-01T00:00:00+00:00",
            message="hello",
        )

        self.assertIn('"aha_cli.prompts" = ["*.md"]', pyproject)
        self.assertIn("You are the AHA backend agent for `main`.", prompt)
        self.assertIn("User message from browser", prompt)

    def test_agent_permission_update_is_in_status_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Permissions", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                agent = update_agent_config(root, run_id, "task-001", "main", sandbox="workspace-write", approval="never")
                self.assertEqual(agent["sandbox"], "workspace-write")

                snapshot = status_snapshot(root, run_id)
                task = snapshot["tasks"][0]
                self.assertEqual(task["preferred_sandbox"], "workspace-write")
                self.assertEqual(task["agents"][0]["sandbox"], "workspace-write")
                self.assertEqual(task["agents"][0]["approval"], "never")

    def test_task_hide_restore_and_soft_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task visibility", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                hidden = set_task_hidden(root, run_id, "task-001", True)
                self.assertTrue(hidden["hidden"])
                snapshot = status_snapshot(root, run_id)
                self.assertTrue(snapshot["tasks"][0]["hidden"])

                restored = set_task_hidden(root, run_id, "task-001", False)
                self.assertFalse(restored["hidden"])

                deleted = delete_task(root, run_id, "task-001")
                self.assertIsNotNone(deleted["deleted_at"])
                snapshot = status_snapshot(root, run_id)
                self.assertEqual(snapshot["tasks"], [])

    def test_task_agent_and_session_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Manage agents", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, task_output = self.run_cli(
                    "task",
                    "add",
                    run_id,
                    "Extra task",
                    "--backend",
                    "codex",
                    "--workspace-path",
                    str(root),
                    "--max-sub-agents",
                    "2",
                )
                self.assertEqual(code, 0)
                self.assertIn("Extra task", task_output)
                self.assertIn('"workspace_path"', task_output)
                self.assertIn('"delegation_policy": "auto"', task_output)
                code, watch_output = self.run_cli("watch", run_id, "--once")
                self.assertEqual(code, 0)
                self.assertIn("task_dispatched", watch_output)
                self.assertIn("You are now running in AHA mode", watch_output)

                code, agent_output = self.run_cli("agent", "add", run_id, "task-001", "--backend", "stub")
                self.assertEqual(code, 0)
                self.assertIn("sub-001", agent_output)

                code, list_output = self.run_cli("agent", "list", run_id, "task-001")
                self.assertEqual(code, 0)
                self.assertIn("main role=task-main", list_output)
                self.assertIn("sub-001 role=sub backend=stub", list_output)

                code, sessions = self.run_cli("session", "list", run_id, "--task-id", "task-001")
                self.assertEqual(code, 0)
                self.assertIn('"agent_id": "main"', sessions)
                self.assertIn('"agent_id": "sub-001"', sessions)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
