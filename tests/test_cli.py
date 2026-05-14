from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.backends.codex import build_codex_exec_command, handle_codex_event
from aha_cli.backends.registry import agent_backend_names, agent_backends, backend_names, model_options
from aha_cli.cli import append_message, main, task_dashboard_html, task_snapshot
from aha_cli.services.chat import chat_prompt, status_from_agent_result
from aha_cli.store.filesystem import (
    delete_task,
    inbox_path,
    iter_jsonl_from,
    run_dir,
    set_task_hidden,
    set_task_status,
    status_snapshot,
    update_agent_config,
    write_task_result,
)
from aha_cli.web.server import format_agent_command, format_aha_command, handle_slash_command


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_command_backend_is_not_an_agent_backend(self) -> None:
        self.assertIn("command", backend_names())
        self.assertNotIn("command", agent_backend_names())
        self.assertIn("codex", agent_backend_names())

    def test_model_options_are_bound_to_agent_backend(self) -> None:
        codex_options = model_options("codex")
        stub_options = model_options("stub")
        self.assertEqual(codex_options[0]["name"], "")
        self.assertEqual(codex_options[0]["label"], "default")
        self.assertIn("gpt-5.3-codex", {item["name"] for item in codex_options})
        self.assertEqual(stub_options, [{"name": "", "label": "default"}])
        self.assertIn("commands", agent_backends()[0])

    def test_agent_result_status_detects_blocked_reply(self) -> None:
        self.assertEqual(status_from_agent_result(0, "done"), "completed")
        self.assertEqual(status_from_agent_result(1, "done"), "failed")
        self.assertEqual(status_from_agent_result(0, "文件没有落盘，因为 read-only sandbox"), "blocked")
        self.assertEqual(status_from_agent_result(0, "当前沙箱是只读，写入被拦截"), "blocked")

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
            )
            handle_codex_event(
                json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "pwd", "status": "completed", "exit_code": 0, "aggregated_output": "x" * 1300}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="codex-chat",
            )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["type"], "agent_command_started")
            self.assertEqual(rows[1]["type"], "agent_command_finished")
            self.assertEqual(rows[1]["data"]["command"], "pwd")
            self.assertEqual(len(rows[1]["data"]["output_tail"]), 1200)

    def test_plan_run_merge_with_stub_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                code, _ = self.run_cli("init")
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

    def test_explicit_tasks_are_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--backend", "codex")
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
                self.run_cli("init")
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
                self.run_cli("init")
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
                self.run_cli("init")
                code, plan_output = self.run_cli("plan", "Codex backend", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, output = self.run_cli("run", run_id, "--backend", "codex", "--dry-run")
                self.assertEqual(code, 0)
                self.assertIn("aha_cli codex-runner", output)

    def test_codex_chat_does_not_auto_write_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Codex chat", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "你好", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "真实回复", None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("main -> browser: 真实回复", output)
                self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "completed")
                self.assertEqual(task_snapshot(root, run_id, "task-001")["result"], "")

                code, watch_output = self.run_cli("watch", run_id, "--once")
                self.assertEqual(code, 0)
                self.assertIn("message task=task-001 main -> browser: 真实回复", watch_output)
                self.assertIn("task_status_changed", watch_output)
                self.assertNotIn("task_result_written", watch_output)

    def test_agent_command_does_not_write_task_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Agent command", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(
                    root,
                    run_id,
                    "main",
                    "/status",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    command_namespace="agent",
                    original_command="/agent status",
                )

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "状态：完成", None)) as run_agent:
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("main -> browser: 状态：完成", output)
                self.assertEqual(run_agent.call_args.kwargs["session"]["agent_id"], "main")
                self.assertIsNone(run_agent.call_args.kwargs["session"].get("backend_session_id"))

                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["result"], "")
                self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "pending")

    def test_agent_command_prompt_does_not_replay_task_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Agent command prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(
                    root,
                    run_id,
                    "browser",
                    "状态：已完成，旧任务结果",
                    sender="main",
                    task_id="task-001",
                    role="main",
                    from_agent="main",
                    to_agent="browser",
                )
                command = append_message(
                    root,
                    run_id,
                    "main",
                    "/status",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    command_namespace="agent",
                    original_command="/agent status",
                )

                prompt = chat_prompt(root, run_id, "main", command, "")
                self.assertIn("The user sent an agent command", prompt)
                self.assertIn("/status: report this agent's runtime/session metadata only", prompt)
                self.assertNotIn("Recent events:", prompt)
                self.assertNotIn("旧任务结果", prompt)

    def test_raw_result_file_without_final_metadata_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Stale result", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                path = run_dir(root, run_id) / "results/task-001.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("old auto result\n", encoding="utf-8")

                self.assertEqual(task_snapshot(root, run_id, "task-001")["result"], "")

    def test_finalize_policy_updates_existing_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Finalize chat", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                write_task_result(root, run_id, "task-001", "old final")
                set_task_status(root, run_id, "task-001", "completed", 0)
                append_message(
                    root,
                    run_id,
                    "main",
                    "Generate the Final",
                    sender="aha",
                    task_id="task-001",
                    role="main",
                    result_policy="finalize",
                    original_command="/aha finalize",
                )

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "new final", None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("main -> aha: new final", output)
                self.assertEqual(task_snapshot(root, run_id, "task-001")["result"].strip(), "new final")

    def test_codex_chat_executes_spawn_sub_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Spawn sub", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "delegate this", sender="browser", task_id="task-001", role="main")
                reply = '{"complexity":"medium","actions":[{"type":"spawn_sub","title":"Inspect one slice","backend":"stub","reason":"parallel research"}],"response":"delegating"}'

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, reply, None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("delegating", output)

                code, agents = self.run_cli("agent", "list", run_id, "task-001")
                self.assertEqual(code, 0)
                self.assertIn("sub-001 role=sub backend=stub", agents)

    def test_task_dashboard_and_metadata_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init")
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
                self.assertIn("selected-task-meta", html)
                self.assertIn("selected-agent-info", html)
                self.assertIn("command-menu", html)
                self.assertIn("live-activity", html)
                self.assertIn('data-tab="final"', html)

    def test_aha_status_command_formats_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Command help", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                output = format_aha_command(root, run_id, "task-001", "/aha status")
                self.assertIn("Task: task-001", output)
                self.assertIn("Status: pending", output)

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

    def test_agent_permission_update_is_in_status_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--backend", "codex")
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
                self.run_cli("init", "--backend", "codex")
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
                self.run_cli("init", "--backend", "codex")
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
