from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from aha_cli.backends.claude import build_claude_exec_command, claude_permission_mode, handle_claude_event
from aha_cli.backends.codex import build_codex_exec_command, handle_codex_event, is_context_overflow_message, run_codex_exec
from aha_cli.cli import append_message, main, task_dashboard_html, task_snapshot
from aha_cli.services.commit_policy import format_commit_message, validate_commit_message
from aha_cli.services.chat import apply_supervision_host_decision, chat_offset_path, chat_prompt, chat_prompt_with_metrics, load_chat_offset, save_chat_offset
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.services.messages import format_event
from aha_cli.services.session_compact import compact_reset_backend_session
from aha_cli.services.orchestrator import task_assignment_prompt
from aha_cli.store.filesystem import (
    add_agent,
    append_jsonl,
    append_event,
    complete_task,
    conversation_events_page,
    delete_task,
    event_path,
    inbox_path,
    iter_jsonl_from,
    iter_jsonl_reverse,
    list_task_lifecycle_rounds,
    list_task_rounds,
    read_json,
    run_dir,
    mark_task_coordination,
    reopen_task,
    set_agent_status,
    set_task_hidden,
    set_task_status,
    status_snapshot,
    task_context_snapshot,
    task_final_snapshot,
    task_log_page,
    update_task_proxy_config,
    update_task_supervision_config,
    update_agent_config,
    update_agent_runtime,
    write_task_result,
)
from aha_cli.web.server import (
    backend_session_jsonl_info,
    finalization_prompt,
    format_agent_command,
    format_aha_command,
    handle_slash_command,
    workspace_options,
)
from tests.helpers import (
    fetch_ui_response,
    json_response_body,
)


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_codex_resume_command_keeps_workspace_write_scope(self) -> None:
        cmd = build_codex_exec_command(
            codex_bin="codex",
            model=None,
            approval="never",
            sandbox="workspace-write",
            cwd=Path("/tmp/project"),
            output_file=Path("/tmp/out.md"),
            json_events=True,
            session_id="session-123",
        )
        self.assertEqual(cmd[:9], ["codex", "-a", "never", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", "-C", "/tmp/project"])
        self.assertIn("resume", cmd)
        self.assertIn("session-123", cmd)

    def test_codex_command_events_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            handle_codex_event(
                json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "pwd", "status": "in_progress"}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="codex-chat",
                target="sub-001",
            )
            handle_codex_event(
                json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "pwd", "status": "completed", "exit_code": 0, "aggregated_output": "x" * 1300}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="codex-chat",
                target="sub-001",
            )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["type"], "agent_command_started")
            self.assertEqual(rows[1]["type"], "agent_command_finished")
            self.assertEqual(rows[1]["data"]["command"], "pwd")
            self.assertEqual(rows[1]["data"]["target"], "sub-001")
            self.assertEqual(len(rows[1]["data"]["output_tail"]), 1200)

    def test_codex_thread_started_reactivates_reset_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            session = {"status": "reset", "backend_session_id": None}
            handle_codex_event(
                json.dumps({"type": "thread.started", "thread_id": "new-codex-session"}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="codex-chat",
                target="main",
                session=session,
            )

        self.assertEqual(session["backend_session_id"], "new-codex-session")
        self.assertEqual(session["status"], "active")

    def test_codex_context_overflow_event_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            handle_codex_event(
                json.dumps({"type": "error", "message": "Codex ran out of room in the model's context window."}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="codex-chat",
                target="main",
            )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]

        self.assertTrue(is_context_overflow_message("prompt is too long: context length exceeded"))
        self.assertFalse(is_context_overflow_message("authentication failed"))
        self.assertEqual([row["type"] for row in rows], ["agent_error", "agent_context_overflow"])
        self.assertEqual(rows[1]["data"]["reason"], "context_window")

    def test_codex_exec_reports_missing_cli_as_agent_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.md"
            events = Path(tmp) / "events.jsonl"
            with mock.patch("aha_cli.backends.codex.subprocess.Popen", side_effect=FileNotFoundError(2, "No such file or directory", "codex")):
                code, reply, _ = run_codex_exec(
                    "hello",
                    cwd=Path(tmp),
                    output_file=output,
                    events_file=events,
                    run_id="run-001",
                    task_id="task-001",
                    source="codex-chat",
                    target="main",
                )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
            output_text = output.read_text(encoding="utf-8")

        self.assertEqual(code, 127)
        self.assertIn("Failed to start Codex backend command", reply)
        self.assertEqual(output_text, reply)
        self.assertEqual(rows[-1]["type"], "agent_error")
        self.assertEqual(rows[-1]["data"]["reason"], "backend_start_failed")

    def test_claude_permission_mode_maps_sandbox(self) -> None:
        self.assertEqual(claude_permission_mode("research", "read-only"), "plan")
        self.assertEqual(claude_permission_mode("research", "workspace-write"), "acceptEdits")
        self.assertEqual(claude_permission_mode("research", "danger-full-access"), "bypassPermissions")
        self.assertEqual(claude_permission_mode("research", "auto"), "plan")
        self.assertEqual(claude_permission_mode("implementation", "auto"), "acceptEdits")

    def test_claude_resume_command_uses_stream_json(self) -> None:
        cmd = build_claude_exec_command(
            claude_bin="claude",
            model="sonnet",
            permission_mode="acceptEdits",
            session_id="session-123",
        )
        self.assertEqual(cmd[:5], ["claude", "-p", "--output-format", "stream-json", "--verbose"])
        self.assertIn("--model", cmd)
        self.assertIn("sonnet", cmd)
        self.assertIn("--permission-mode", cmd)
        self.assertIn("acceptEdits", cmd)
        self.assertIn("--disallowedTools", cmd)
        self.assertIn("Agent,Task,TaskCreate", cmd)
        self.assertIn("--resume", cmd)
        self.assertIn("session-123", cmd)

    def test_claude_stream_events_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            session: dict = {"status": "reset"}
            handle_claude_event(
                json.dumps({"type": "system", "subtype": "init", "session_id": "claude-session"}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
                session=session,
            )
            handle_claude_event(
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
                session=session,
            )
            handle_claude_event(
                json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "pwd"}}]}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
                session=session,
            )
            handle_claude_event(
                json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}]}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
                session=session,
            )
            handle_claude_event(
                json.dumps({"type": "result", "result": "done", "usage": {"input_tokens": 1}, "session_id": "claude-session"}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
                session=session,
            )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(session["backend_session_id"], "claude-session")
        self.assertEqual(session["status"], "active")
        self.assertEqual([row["type"] for row in rows], ["agent_thread", "agent_message", "agent_command_started", "agent_command_finished", "agent_usage"])
        self.assertEqual(rows[1]["data"]["text"], "hello")
        self.assertEqual(rows[2]["data"]["command"], "pwd")
        self.assertEqual(rows[3]["data"]["output_tail"], "ok")
        self.assertEqual(rows[4]["data"]["usage"]["input_tokens"], 1)

    def test_claude_native_subagent_claims_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            handle_claude_event(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": "3个sub agent已并行启动。"},
                                {
                                    "type": "tool_use",
                                    "id": "tool-1",
                                    "name": "TaskCreate",
                                    "input": {"subject": "分析问题单01"},
                                },
                            ]
                        },
                    }
                ),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
            )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(
            [row["type"] for row in rows],
            ["agent_message", "claimed_sub_without_aha_agent", "native_subagent_tool_used", "agent_command_started"],
        )
        self.assertEqual(rows[1]["data"]["reason"], "assistant_text_claim_without_aha_spawn_sub")
        self.assertEqual(rows[2]["data"]["tool_name"], "TaskCreate")

    def test_claude_context_overflow_event_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            handle_claude_event(
                json.dumps({"type": "error", "message": "prompt is too long: context length exceeded"}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
            )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([row["type"] for row in rows], ["agent_error", "agent_context_overflow"])
        self.assertEqual(rows[1]["data"]["reason"], "context_window")

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

    def test_codex_backend_dry_run_uses_codex_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable")
                code, plan_output = self.run_cli("plan", "Codex backend", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, output = self.run_cli("run", run_id, "--backend", "codex", "--dry-run")
                self.assertEqual(code, 0)
                self.assertIn("aha_cli codex-runner", output)

    def test_claude_backend_dry_run_uses_claude_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable")
                code, plan_output = self.run_cli("plan", "Claude backend", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, output = self.run_cli("run", run_id, "--backend", "claude", "--dry-run")
                self.assertEqual(code, 0)
                self.assertIn("aha_cli claude-runner", output)

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
        self.assertIn("AHA-Task: task-001", assignment_prompt)
        self.assertIn("return ONLY one JSON object", assignment_prompt)
        self.assertIn('"actions"', assignment_prompt)
        self.assertIn("AHA may reuse that abnormal sub-agent slot", assignment_prompt)
        self.assertIn("include `agent_id` in that `spawn_sub` action", assignment_prompt)

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
                self.assertIn("AHA-Task: task-001", main_prompt)
                self.assertIn("AHA-Agent: main", main_prompt)
                self.assertIn("aha commit --type <type>", main_prompt)
                self.assertIn("UI routing changes", main_prompt)
                self.assertIn("AHA may reuse that abnormal sub-agent slot", main_prompt)
                self.assertIn("Spawn/reassign format:", main_prompt)

                sub_message = append_message(root, run_id, "sub-001", "提交你负责的部分", sender="main", task_id="task-001", role="sub")
                sub_prompt = chat_prompt(root, run_id, "sub-001", sub_message, "")

                self.assertIn("commit only files covered by your `assignment` / `created_reason`", sub_prompt)
                self.assertIn("report back to `task-main`", sub_prompt)
                self.assertIn("AHA-Task: task-001", sub_prompt)
                self.assertIn("AHA-Agent: sub-001", sub_prompt)

    def test_commit_policy_formats_validates_and_prints_dry_run_messages(self) -> None:
        message = format_commit_message("feat", "add lazy loading", "task-001", "main", scope="web", aha_scope="lazy-log")

        self.assertEqual(validate_commit_message(message), [])
        self.assertIn("feat(web): add lazy loading", message)
        self.assertIn("AHA-Task: task-001", message)
        self.assertIn("AHA-Agent: main", message)
        self.assertIn("AHA-Scope: lazy-log", message)
        self.assertTrue(validate_commit_message("update stuff\n\nAHA-Task: task-001\n"))

        code, output = self.run_cli(
            "commit",
            "--type",
            "fix",
            "--scope",
            "web",
            "--summary",
            "keep logs scroll stable",
            "--task-id",
            "task-005",
            "--agent",
            "main",
            "--aha-scope",
            "log-scroll",
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertIn("fix(web): keep logs scroll stable", output)
        self.assertIn("AHA-Task: task-005", output)
        self.assertIn("AHA-Agent: main", output)
        with tempfile.TemporaryDirectory() as tmp:
            message_file = Path(tmp) / "COMMIT_EDITMSG"
            message_file.write_text(message, encoding="utf-8")
            code, output = self.run_cli("commit-check", str(message_file))
        self.assertEqual(code, 0)
        self.assertIn("Commit message OK", output)

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

    def test_task_action_resume_alias_reopens_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Resume alias", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                complete_task(root, run_id, "task-001", 0)

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/task/task-001/resume", method="POST"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["task"]["status"], "awaiting_user")

    def test_web_task_creation_autostarts_dispatched_main_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task create autostart", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with mock.patch("aha_cli.web.server.start_backend", return_value={"status": "running", "started": True}) as start_backend:
                    response = asyncio.run(
                        fetch_ui_response(
                            root,
                            run_id,
                            "/api/tasks",
                            method="POST",
                            payload={
                                "title": "Autostart task",
                                "backend": "codex",
                                "sandbox": "danger-full-access",
                                "approval": "never",
                                "dispatch": True,
                            },
                        )
                    )
                    body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["backend"]["status"], "running")
        start_backend.assert_called_once()
        self.assertEqual(start_backend.call_args.args[:3], (root, run_id, "main"))
        self.assertEqual(start_backend.call_args.kwargs["task_id"], body["task"]["id"])
        self.assertTrue(start_backend.call_args.kwargs["from_start"])

    def test_conversation_events_page_filters_and_pages_by_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            append_event(root, run_id, "message", {"task_id": "task-001", "sender": "browser", "to_agent": "main", "message": "one"})
            append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "two"})
            append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "sub-001", "text": "sub"})
            append_event(root, run_id, "agent_message", {"task_id": "task-002", "target": "main", "text": "other task"})

            latest = conversation_events_page(root, run_id, "task-001", "main", limit=1)
            append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "new realtime"})
            realtime, _ = iter_jsonl_from(event_path(root, run_id), latest["after_offset"])
            older = conversation_events_page(root, run_id, "task-001", "main", limit=1, before=latest["next_before_offset"])

        self.assertEqual(latest["count"], 1)
        self.assertTrue(latest["has_more"])
        self.assertEqual(latest["events"][0]["data"]["text"], "two")
        self.assertEqual(realtime[0]["data"]["text"], "new realtime")
        self.assertFalse(older["has_more"])
        self.assertEqual(older["events"][0]["data"]["message"], "one")

    def test_conversation_events_page_includes_supervision_events_for_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            append_event(root, run_id, "main_reported_to_host", {"task_id": "task-001", "host_backend": "stub"})
            append_event(root, run_id, "host_decision", {"task_id": "task-001", "decision": "ask_user"})
            append_event(root, run_id, "main_applied_decision", {"task_id": "task-001", "decision": "ask_user", "applied": True})
            append_event(root, run_id, "host_decision", {"task_id": "task-002", "decision": "stop"})

            main_page = conversation_events_page(root, run_id, "task-001", "main", limit=10)
            sub_page = conversation_events_page(root, run_id, "task-001", "sub-001", limit=10)

        self.assertEqual(
            [event["type"] for event in main_page["events"]],
            ["main_reported_to_host", "host_decision", "main_applied_decision"],
        )
        self.assertEqual(sub_page["events"], [])

    def test_conversation_events_page_shares_host_forwarding_but_hides_aha_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            append_event(
                root,
                run_id,
                "message",
                {
                    "task_id": "task-001",
                    "sender": "main",
                    "target": "host",
                    "from_agent": "main",
                    "to_agent": "host",
                    "agent_id": "host",
                    "display_sender": "main",
                    "display_target": "host",
                    "message": "main reply",
                },
            )
            append_event(
                root,
                run_id,
                "message",
                {
                    "task_id": "task-001",
                    "sender": "AHA",
                    "target": "host",
                    "display_sender": "host",
                    "display_target": "host",
                    "message": "host 正在判断本轮下一步。",
                },
            )
            append_event(
                root,
                run_id,
                "message",
                {
                    "task_id": "task-001",
                    "sender": "browser",
                    "target": "main",
                    "agent_id": "host",
                    "display_sender": "host",
                    "display_target": "main",
                    "message": "next step",
                },
            )

            main_page = conversation_events_page(root, run_id, "task-001", "main", limit=10)
            host_page = conversation_events_page(root, run_id, "task-001", "host", limit=10)

        self.assertEqual([event["data"]["message"] for event in main_page["events"]], ["main reply", "next step"])
        self.assertEqual([event["data"]["message"] for event in host_page["events"]], ["main reply", "next step"])

    def test_conversation_events_api_hides_action_envelope_agent_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Conversation action envelope", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                user_facing_response = "只展示投影后的 response"
                action_envelope = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "record_task_update",
                                "summary": "raw envelope should stay out of timeline",
                                "changed_files": [],
                                "verification": [],
                                "risks": [],
                            }
                        ],
                        "response": user_facing_response,
                    },
                    ensure_ascii=False,
                )
                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": action_envelope})
                append_event(
                    root,
                    run_id,
                    "message",
                    {"task_id": "task-001", "sender": "main", "target": "browser", "message": user_facing_response},
                )

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        timeline_texts = [str(event["data"].get("text") or event["data"].get("message") or "") for event in body["events"]]
        self.assertEqual(timeline_texts, [user_facing_response])
        self.assertNotIn(action_envelope, timeline_texts)
        self.assertFalse(any('"actions"' in text and '"response"' in text for text in timeline_texts))

    def test_web_restart_api_schedules_source_ui_on_8766(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Restart web", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="scheduled\n", stderr="")
                with mock.patch("aha_cli.web.server.subprocess.run", return_value=completed) as run_command:
                    response = asyncio.run(
                        fetch_ui_response(
                            root,
                            run_id,
                            "/api/web/restart",
                            method="POST",
                            payload={"host": "0.0.0.0", "port": 8766},
                        )
                    )
                body = json_response_body(response)
                rows, _ = iter_jsonl_from(event_path(root, run_id), 0)
                events = [row["type"] for row in rows]

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["host"], "0.0.0.0")
        self.assertEqual(body["port"], 8766)
        self.assertEqual(body["service_unit"], "aha-ui-source-8766.service")
        command = run_command.call_args.args[0]
        self.assertEqual(command[0], "systemd-run")
        self.assertIn("--on-active=1s", command)
        command_text = " ".join(command)
        self.assertIn(str(root), command_text)
        self.assertIn("0.0.0.0", command_text)
        self.assertIn("8766", command_text)
        self.assertIn("systemctl --user restart aha-ui-source-8766.service", command_text)
        self.assertIn("web_restart_requested", events)

    def test_conversation_events_api_restores_latest_turn_metrics_outside_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Conversation prompt metrics", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, run_id, "agent_started", {"task_id": "task-001", "target": "main", "sender": "browser"})
                append_event(
                    root,
                    run_id,
                    "agent_prompt_metrics",
                    {
                        "task_id": "task-001",
                        "target": "main",
                        "source": "codex-chat",
                        "total": {"chars": 1234, "bytes": 1234, "lines": 12},
                        "components": {"status_snapshot": {"chars": 1000, "bytes": 1000, "lines": 1}},
                    },
                )
                append_event(root, run_id, "agent_thread", {"task_id": "task-001", "target": "main", "thread_id": "thread-1"})
                append_event(root, run_id, "agent_usage", {"task_id": "task-001", "target": "main", "usage": {"input_tokens": 10}})
                append_event(root, run_id, "agent_finished", {"task_id": "task-001", "target": "main", "exit_code": 0})
                for index in range(10):
                    append_event(
                        root,
                        run_id,
                        "agent_command_finished",
                        {"task_id": "task-001", "target": "main", "command": f"cmd-{index}", "exit_code": 0},
                    )

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=5"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertNotIn("agent_prompt_metrics", [event["type"] for event in body["events"]])
        turn_event_types = [event["type"] for event in body["turn_events"]]
        self.assertEqual(turn_event_types, ["agent_started", "agent_prompt_metrics", "agent_thread", "agent_usage", "agent_finished"])
        metrics = next(event for event in body["turn_events"] if event["type"] == "agent_prompt_metrics")
        self.assertEqual(metrics["data"]["total"]["chars"], 1234)

    def test_conversation_events_api_filters_categories_server_side(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Conversation categories", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "hello", sender="browser", task_id="task-001", role="main")
                append_event(root, run_id, "agent_command_started", {"task_id": "task-001", "target": "main", "command": "pwd"})
                append_event(root, run_id, "agent_command_finished", {"task_id": "task-001", "target": "main", "command": "pwd", "exit_code": 0, "output_tail": "large output"})
                append_event(root, run_id, "agent_usage", {"task_id": "task-001", "target": "main", "usage": {"input_tokens": 10}})
                append_event(root, run_id, "task_status_changed", {"task_id": "task-001", "status": "running"})

                chat_response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20&categories=chat"))
                command_response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20&categories=chat,commands"))
                full_command_response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20&categories=commands&include_command_output=1"))
                none_response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20&categories=none"))

        chat_body = json_response_body(chat_response)
        command_body = json_response_body(command_response)
        full_command_body = json_response_body(full_command_response)
        none_body = json_response_body(none_response)
        self.assertEqual([event["type"] for event in chat_body["events"]], ["message"])
        self.assertEqual(
            [event["type"] for event in command_body["events"]],
            ["message", "agent_command_started", "agent_command_finished"],
        )
        finished = command_body["events"][-1]["data"]
        self.assertNotIn("output_tail", finished)
        self.assertTrue(finished["output_tail_omitted"])
        self.assertEqual(finished["output_tail_chars"], len("large output"))
        self.assertEqual(full_command_body["events"][-1]["data"]["output_tail"], "large output")
        self.assertEqual(none_body["events"], [])

    def test_backend_session_jsonl_info_analyzes_aha_prompt_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session_id = "session-analysis-1"
            session_file = home / ".codex" / "sessions" / "2026" / "05" / "21" / f"rollout-{session_id}.jsonl"
            full_prompt = textwrap.dedent(
                """\
                You are connected to AHA as the real backend agent.

                Current status:
                {'task': 'task-001'}

                User message from browser at 2026-05-21T00:00:00+00:00:
                first request
                """
            )
            delta_prompt = textwrap.dedent(
                """\
                You are connected to AHA as the real backend agent.

                Current delta status:
                {'task': 'task-001'}

                User message from browser at 2026-05-21T00:01:00+00:00:
                second request
                """
            )
            append_jsonl(session_file, {"type": "session_meta", "payload": {"id": session_id}})
            append_jsonl(
                session_file,
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": full_prompt}]}},
            )
            append_jsonl(session_file, {"type": "event_msg", "payload": {"type": "user_message", "message": full_prompt}})
            append_jsonl(
                session_file,
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": delta_prompt}]}},
            )
            append_jsonl(session_file, {"type": "response_item", "payload": {"type": "function_call_output", "output": "tool-output-text"}})
            append_jsonl(
                session_file,
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "reply"}]}},
            )

            with mock.patch("aha_cli.web.server.Path.home", return_value=home):
                info = backend_session_jsonl_info({"backend": "codex", "backend_session_id": session_id})

        analysis = info["analysis"]
        self.assertTrue(info["exists"])
        self.assertGreater(info["size_bytes"], 0)
        self.assertEqual(analysis["line_count"], 6)
        self.assertEqual(analysis["aha_prompt_counts"]["full"], 1)
        self.assertEqual(analysis["aha_prompt_counts"]["sticky_delta"], 1)
        self.assertEqual(analysis["event_msg_prompt_mirror_counts"]["full"], 1)
        self.assertEqual(analysis["aha_prompt_total_count"], 2)
        self.assertEqual(analysis["latest_prompt_mode"], "sticky_delta")
        self.assertGreater(analysis["tool_output_chars"], 0)
        self.assertGreater(analysis["assistant_message_chars"], 0)

    def test_backend_session_jsonl_info_analyzes_claude_session_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            session_id = "claude-session-analysis-1"
            session_file = home / ".claude" / "projects" / "project-a" / f"{session_id}.jsonl"
            full_prompt = textwrap.dedent(
                """\
                You are connected to AHA as the real backend agent.

                Current status:
                {'task': 'task-001'}

                User message from browser at 2026-05-21T00:00:00+00:00:
                first request
                """
            )
            append_jsonl(session_file, {"type": "queue-operation", "operation": "enqueue", "sessionId": session_id, "content": full_prompt})
            append_jsonl(session_file, {"type": "user", "sessionId": session_id, "message": {"role": "user", "content": full_prompt}})
            append_jsonl(
                session_file,
                {"type": "assistant", "sessionId": session_id, "message": {"role": "assistant", "content": [{"type": "text", "text": "reply"}]}},
            )
            append_jsonl(
                session_file,
                {
                    "type": "user",
                    "sessionId": session_id,
                    "message": {"role": "user", "content": [{"type": "tool_result", "content": "tool-output"}]},
                },
            )

            with mock.patch("aha_cli.web.server.Path.home", return_value=home):
                info = backend_session_jsonl_info({"backend": "claude", "backend_session_id": session_id})

        analysis = info["analysis"]
        self.assertTrue(info["exists"])
        self.assertEqual(analysis["backend"], "claude")
        self.assertEqual(analysis["type_counts"]["user"], 2)
        self.assertEqual(analysis["aha_prompt_counts"]["full"], 1)
        self.assertEqual(analysis["event_msg_prompt_mirror_counts"]["full"], 1)
        self.assertEqual(analysis["response_item_counts"]["message:user"], 1)
        self.assertEqual(analysis["response_item_counts"]["message:assistant"], 1)
        self.assertEqual(analysis["response_item_counts"]["tool_result:user"], 1)
        self.assertGreater(analysis["tool_output_chars"], 0)
        self.assertGreater(analysis["assistant_message_chars"], 0)

    def test_compact_reset_archives_backend_session_and_injects_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
            root = Path(tmp)
            home = Path(home_tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Compact reset", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_id = "compact-reset-session-1"
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = session_id
                session_file.write_text(json.dumps(session), encoding="utf-8")
                append_jsonl(
                    home / ".codex" / "sessions" / "2026" / "05" / "21" / f"rollout-{session_id}.jsonl",
                    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "old prompt"}]}},
                )
                append_message(root, run_id, "main", "previous request", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.session_compact.Path.home", return_value=home):
                    payload = compact_reset_backend_session(root, run_id, "task-001", "main", reason="manual")

                updated = read_json(session_file)
                summary_exists = (run_dir(root, run_id) / payload["summary_path"]).exists()
                prompt = chat_prompt(
                    root,
                    run_id,
                    "main",
                    {"sender": "browser", "message": "next request", "task_id": "task-001", "role": "main"},
                    "prefix",
                )

        self.assertEqual(payload["old_backend_session_id"], session_id)
        self.assertIsNone(updated["backend_session_id"])
        self.assertEqual(updated["history_backend_sessions"][0]["backend_session_id"], session_id)
        self.assertEqual(updated["compact_summary"]["archived_backend_session_id"], session_id)
        self.assertTrue(summary_exists)
        self.assertIn("Backend compact summary from previous session", prompt)
        self.assertIn("previous request", prompt)

    def test_compact_reset_api_uses_selected_agent_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
            root = Path(tmp)
            home = Path(home_tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Compact reset API", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_id = "compact-reset-api-session-1"
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = session_id
                session_file.write_text(json.dumps(session), encoding="utf-8")
                append_jsonl(
                    home / ".codex" / "sessions" / "2026" / "05" / "21" / f"rollout-{session_id}.jsonl",
                    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "old prompt"}]}},
                )

                with mock.patch("aha_cli.services.session_compact.Path.home", return_value=home):
                    response = asyncio.run(
                        fetch_ui_response(
                            root,
                            run_id,
                            "/api/task/task-001/session/compact-reset",
                            method="POST",
                            payload={"target": "main", "reason": "manual", "restart": False},
                        )
                    )
                body = json_response_body(response)
                updated = read_json(session_file)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["compact_reset"]["old_backend_session_id"], session_id)
        self.assertIsNone(updated["backend_session_id"])
        self.assertEqual(updated["history_backend_sessions"][0]["backend_session_id"], session_id)

    def test_events_api_replays_from_saved_offset_after_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Replay events", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                initial = json_response_body(asyncio.run(fetch_ui_response(root, run_id, "/api/events?offset=-1")))
                last_event_id = initial["offset"]

                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "missed-1"})
                append_event(root, run_id, "agent_finished", {"task_id": "task-001", "target": "main", "exit_code": 0})

                first_page = json_response_body(
                    asyncio.run(fetch_ui_response(root, run_id, f"/api/events?offset={last_event_id}&limit=1"))
                )
                replay = json_response_body(asyncio.run(fetch_ui_response(root, run_id, f"/api/events?offset={last_event_id}&limit=10")))

            self.assertEqual(first_page["events"][0]["data"]["text"], "missed-1")
            self.assertTrue(first_page["has_more"])
            self.assertGreater(first_page["offset"], last_event_id)
            self.assertEqual([event["type"] for event in replay["events"]], ["agent_message", "agent_finished"])
            self.assertEqual(replay["events"][1]["data"]["exit_code"], 0)
            self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "pending")

    def test_reverse_jsonl_reader_pages_by_byte_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            for index in range(5):
                append_event(root, run_id, "message", {"task_id": "task-001", "sender": "browser", "to_agent": "main", "message": f"line-{index}-" + ("x" * 40)})

            path = event_path(root, run_id)
            newest = list(iter_jsonl_reverse(path, chunk_size=32))
            older = list(iter_jsonl_reverse(path, before=newest[0][0], chunk_size=32))

        self.assertEqual(newest[0][1]["data"]["message"].split("-", 2)[:2], ["line", "4"])
        self.assertEqual(older[0][1]["data"]["message"].split("-", 2)[:2], ["line", "3"])
        self.assertGreater(newest[0][0], older[0][0])

    def test_task_log_page_tails_and_pages_by_byte_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Logs", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                task = task_snapshot(root, run_id, "task-001")["task"]
                log_path = run_dir(root, run_id) / task["log_file"]
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("\n".join(f"line-{index}" for index in range(5)) + "\n", encoding="utf-8")

                latest = task_log_page(root, run_id, "task-001", limit=2)
                older = task_log_page(root, run_id, "task-001", limit=2, before=latest["next_before_offset"])

        self.assertEqual(latest["text"], "line-3\nline-4")
        self.assertTrue(latest["has_more"])
        self.assertEqual(older["text"], "line-1\nline-2")
        self.assertTrue(older["has_more"])

    def test_task_log_page_falls_back_to_event_log_when_task_log_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Event logs", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "first"})
                append_event(root, run_id, "agent_message", {"task_id": "task-002", "target": "main", "text": "other task"})
                append_event(root, run_id, "agent_command_finished", {"task_id": "task-001", "target": "main", "command": "pwd", "output_tail": "second"})

                latest = task_log_page(root, run_id, "task-001", limit=1)
                older = task_log_page(root, run_id, "task-001", limit=1, before=latest["next_before_offset"], source=latest["source"])

        self.assertEqual(latest["source"], "events")
        self.assertIn("agent_command_finished", latest["text"])
        self.assertIn("second", latest["text"])
        self.assertNotIn("other task", latest["text"])
        self.assertEqual(older["source"], "events")
        self.assertIn("first", older["text"])

    def test_task_lightweight_snapshots_exclude_heavy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Lightweight", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "large message", sender="browser", task_id="task-001")
                write_task_result(root, run_id, "task-001", "final text")

                final = task_final_snapshot(root, run_id, "task-001")
                context = task_context_snapshot(root, run_id, "task-001")

        self.assertEqual(final["result"].strip(), "final text")
        self.assertNotIn("messages", final)
        self.assertNotIn("log", final)
        self.assertIn("prompt", context)
        self.assertNotIn("messages", context)
        self.assertNotIn("log", context)

    def test_workspace_options_include_multiple_project_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hl_root = base / "hl_project"
            my_root = base / "my_project"
            (hl_root / "fw_omni_builder").mkdir(parents=True)
            (my_root / "aha").mkdir(parents=True)

            options = workspace_options([hl_root, my_root])

        self.assertEqual(
            options,
            [
                {
                    "name": "fw_omni_builder",
                    "label": "hl_project/fw_omni_builder",
                    "path": str(hl_root / "fw_omni_builder"),
                    "root": str(hl_root),
                },
                {
                    "name": "aha",
                    "label": "my_project/aha",
                    "path": str(my_root / "aha"),
                    "root": str(my_root),
                },
            ],
        )

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

    def test_task_proxy_config_and_agent_toggle_are_in_status_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "Proxy defaults",
                    "--agents",
                    "1",
                    "--http-proxy",
                    "http://127.0.0.1:7890",
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                update_agent_config(root, run_id, "task-001", "sub-001", proxy_enabled=False)
                task = update_task_proxy_config(
                    root,
                    run_id,
                    "task-001",
                    proxy_enabled=False,
                    http_proxy="http://127.0.0.1:8888",
                    https_proxy="http://127.0.0.1:8888",
                    no_proxy="localhost,127.0.0.1",
                )
                self.assertFalse(task["preferred_proxy_enabled"])

                snapshot = status_snapshot(root, run_id)
                task = snapshot["tasks"][0]
                agents = {agent["id"]: agent for agent in task["agents"]}

        self.assertEqual(task["preferred_http_proxy"], "http://127.0.0.1:8888")
        self.assertEqual(task["preferred_https_proxy"], "http://127.0.0.1:8888")
        self.assertEqual(task["preferred_no_proxy"], "localhost,127.0.0.1")
        self.assertFalse(task["preferred_proxy_enabled"])
        self.assertTrue(agents["main"]["proxy_enabled"])
        self.assertFalse(agents["sub-001"]["proxy_enabled"])

    def test_task_proxy_config_api_updates_existing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Proxy API", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task-config",
                        method="POST",
                        payload={
                            "task_id": "task-001",
                            "proxy_enabled": True,
                            "http_proxy": "http://proxy.local:8080",
                            "https_proxy": "http://proxy.local:8080",
                            "no_proxy": "localhost,127.0.0.1",
                        },
                    )
                )
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertTrue(body["task"]["preferred_proxy_enabled"])
        self.assertEqual(body["task"]["preferred_http_proxy"], "http://proxy.local:8080")
        self.assertEqual(body["task"]["preferred_no_proxy"], "localhost,127.0.0.1")

    def test_task_proxy_action_api_updates_existing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Proxy action API", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task/task-001/proxy",
                        method="POST",
                        payload={
                            "proxy_enabled": True,
                            "http_proxy": "http://proxy.local:8080",
                            "https_proxy": "http://proxy.local:8080",
                            "no_proxy": "localhost,127.0.0.1",
                        },
                    )
                )
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertTrue(body["task"]["preferred_proxy_enabled"])
        self.assertEqual(body["task"]["preferred_http_proxy"], "http://proxy.local:8080")

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
