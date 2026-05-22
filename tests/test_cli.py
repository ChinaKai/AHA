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
from aha_cli.services.orchestrator import (
    action_response_text,
    execute_actions,
    extract_action_payload,
    monitor_task_coordination,
    record_sub_agent_report,
    task_assignment_prompt,
)
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
    handle_send_payload,
    handle_slash_command,
    recover_stale_running_agent,
    web_status_snapshot,
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

    def test_chat_offset_persists_unprocessed_messages_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Offsets", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                inbox = inbox_path(root, run_id, "main")
                offset_file = chat_offset_path(run_dir(root, run_id), "main")
                initial_offset = load_chat_offset(inbox, offset_file, from_start=False)
                append_message(root, run_id, "main", "queued while stopped", sender="browser", task_id="task-001", role="main")
                save_chat_offset(offset_file, initial_offset)

                self.assertEqual(load_chat_offset(inbox, offset_file, from_start=False), initial_offset)
                self.assertGreater(inbox.stat().st_size, initial_offset)

    def test_task_scoped_chat_offsets_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)

            global_offset = chat_offset_path(run, "main")
            task_one_offset = chat_offset_path(run, "main", "task-001")
            task_two_offset = chat_offset_path(run, "main", "task-002")

        self.assertNotEqual(global_offset, task_one_offset)
        self.assertNotEqual(task_one_offset, task_two_offset)
        self.assertEqual(task_one_offset.name, "chat-offset-task-001-main.json")

    def test_task_scoped_codex_chat_skips_other_task_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task scoped workers", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                self.run_cli("task", "add", run_id, "Second task", "--no-dispatch")
                append_message(root, run_id, "main", "task two", sender="browser", task_id="task-002", role="main")
                append_message(root, run_id, "main", "task one", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "task one reply", None)) as run_agent:
                    code, output = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

                self.assertEqual(code, 0)
                self.assertEqual(run_agent.call_count, 1)
                self.assertIn("task-001", run_agent.call_args.args[0])
                self.assertNotIn("User message from browser", output)
                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)
                self.assertEqual(browser_messages[-1]["message"], "task one reply")
                self.assertEqual(browser_messages[-1]["task_id"], "task-001")
                scoped_offset = chat_offset_path(run_dir(root, run_id), "main", "task-001")
                self.assertTrue(scoped_offset.exists())
                self.assertFalse(chat_offset_path(run_dir(root, run_id), "main", "task-002").exists())

    def test_codex_chat_once_saves_offset_after_processed_message_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task scoped offsets", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "first", sender="browser", task_id="task-001", role="main")
                append_message(root, run_id, "main", "second", sender="browser", task_id="task-001", role="main")

                with mock.patch(
                    "aha_cli.services.chat.run_codex_exec",
                    side_effect=[(0, "reply one", None), (0, "reply two", None)],
                ) as run_agent:
                    code, first_output = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")
                    self.assertEqual(code, 0)
                    code, second_output = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--once")
                    self.assertEqual(code, 0)

                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)

        self.assertEqual(run_agent.call_count, 2)
        self.assertIn("main -> browser: reply one", first_output)
        self.assertIn("main -> browser: reply two", second_output)
        self.assertIn("User message from browser", run_agent.call_args_list[0].args[0])
        self.assertIn("first", run_agent.call_args_list[0].args[0])
        self.assertIn("second", run_agent.call_args_list[1].args[0])
        self.assertEqual([item["message"] for item in browser_messages[-2:]], ["reply one", "reply two"])

    def test_codex_chat_passes_latest_task_proxy_env_to_codex_exec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "Chat proxy env",
                    "--agents",
                    "1",
                    "--http-proxy",
                    "http://127.0.0.1:7890",
                    "--https-proxy",
                    "http://127.0.0.1:7890",
                    "--no-proxy",
                    "localhost,127.0.0.1",
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_proxy_config(root, run_id, "task-001", http_proxy="http://127.0.0.1:8888")
                append_message(root, run_id, "main", "use proxy", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "reply", None)) as run_agent:
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

        self.assertEqual(code, 0)
        proxy_env = run_agent.call_args.kwargs["proxy_env"]
        self.assertEqual(proxy_env["HTTP_PROXY"], "http://127.0.0.1:8888")
        self.assertEqual(proxy_env["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(proxy_env["NO_PROXY"], "localhost,127.0.0.1")
        self.assertEqual(proxy_env["http_proxy"], "http://127.0.0.1:8888")

    def test_chat_prompt_redacts_proxy_values_from_status_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "Prompt redaction",
                    "--agents",
                    "1",
                    "--http-proxy",
                    "http://user:secret@proxy.local:7890",
                    "--https-proxy",
                    "http://user:secret@proxy.local:7890",
                    "--no-proxy",
                    "internal.local",
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                prompt = chat_prompt(
                    root,
                    run_id,
                    "main",
                    {
                        "sender": "browser",
                        "message": "hello",
                        "task_id": "task-001",
                        "role": "main",
                    },
                    "prefix",
                )

        self.assertNotIn("secret", prompt)
        self.assertNotIn("proxy.local:7890", prompt)
        self.assertNotIn("internal.local", prompt)
        self.assertIn("'preferred_http_proxy': '<set>'", prompt)

    def test_chat_prompt_with_metrics_reports_sizes_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Prompt metrics", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "old-event"})

                prompt, metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    {
                        "sender": "browser",
                        "message": "super-secret-user-text",
                        "task_id": "task-001",
                        "role": "main",
                    },
                    "prefix",
                )

        metrics_json = json.dumps(metrics, ensure_ascii=False)
        self.assertEqual(metrics["total"]["chars"], len(prompt))
        self.assertEqual(metrics["components"]["user_message"]["chars"], len("super-secret-user-text"))
        self.assertGreater(metrics["components"]["status_snapshot"]["chars"], 0)
        self.assertGreater(metrics["components"]["recent_events"]["chars"], 0)
        self.assertGreater(metrics["components"]["task_context"]["chars"], 0)
        self.assertNotIn("super-secret-user-text", metrics_json)
        self.assertNotIn("old-event", metrics_json)

    def test_chat_prompt_filters_private_host_notes_from_main_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Host note isolation", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(
                    root,
                    run_id,
                    "host",
                    "让 main 下一轮只回复数字2",
                    sender="browser",
                    task_id="task-001",
                    role="host",
                    from_agent="browser",
                    to_agent="host",
                )
                main_message = append_message(
                    root,
                    run_id,
                    "main",
                    "请直接回复数字1",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    from_agent="browser",
                    to_agent="main",
                )

                prompt = chat_prompt(root, run_id, "main", main_message, "")

        self.assertIn("请直接回复数字1", prompt)
        self.assertNotIn("让 main 下一轮只回复数字2", prompt)

    def test_chat_prompt_hides_supervision_host_from_main_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Host prompt isolation", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                )
                task = status_snapshot(root, run_id)["tasks"][0]
                append_message(
                    root,
                    run_id,
                    "browser",
                    "Agents:\n- host role=host backend=claude assignment=task supervision host agent",
                    sender="AHA",
                    task_id="task-001",
                    role="aha",
                    from_agent="aha",
                    to_agent="browser",
                    agent_id="main",
                )
                append_event(
                    root,
                    run_id,
                    "agent_message",
                    {
                        "source": "claude-chat",
                        "task_id": "task-001",
                        "target": "main",
                        "item_type": "agent_message",
                        "text": "我处理 03 号，让host agent处理02号重启问题。",
                    },
                )
                main_message = append_message(
                    root,
                    run_id,
                    "main",
                    "你可以和 sub agent 一人一个问题",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    from_agent="browser",
                    to_agent="main",
                )

                prompt = chat_prompt(root, run_id, "main", main_message, "")

        self.assertTrue(any(agent["role"] == "host" for agent in task["agents"]))
        self.assertIn("你可以和 sub agent 一人一个问题", prompt)
        self.assertNotIn("host role=host", prompt)
        self.assertNotIn("'id': 'host'", prompt)
        self.assertNotIn("'role': 'host'", prompt)
        self.assertNotIn("让host agent处理02号", prompt)
        self.assertNotIn("task supervision host agent", prompt)

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

    def test_codex_chat_does_not_auto_write_final_or_complete_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Codex chat", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "你好", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "真实回复", None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("main -> browser: 真实回复", output)
                self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "awaiting_user")
                self.assertEqual(task_snapshot(root, run_id, "task-001")["result"], "")

                code, watch_output = self.run_cli("watch", run_id, "--once")
                self.assertEqual(code, 0)
                self.assertIn("message task=task-001 main -> browser: 真实回复", watch_output)
                self.assertIn("task_status_changed", watch_output)
                self.assertNotIn("task_result_written", watch_output)

    def test_codex_chat_records_supervision_stub_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Supervision stub", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    max_rounds=5,
                )
                append_message(root, run_id, "main", "托管测试", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "托管回复", None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task_status = status_snapshot(root, run_id)["tasks"][0]["status"]

        self.assertEqual(code, 0)
        self.assertIn("main -> browser: 托管回复", output)
        self.assertIn("main_reported_to_host", [row["type"] for row in rows])
        host_decisions = [row for row in rows if row["type"] == "host_decision"]
        self.assertEqual(host_decisions[-1]["data"]["decision"], "ask_user")
        self.assertNotIn("allowed", host_decisions[-1]["data"])
        applied = [row for row in rows if row["type"] == "main_applied_decision"]
        self.assertEqual(applied[-1]["data"]["effect"], "await_user")
        self.assertEqual(task_status, "awaiting_user")

    def test_codex_chat_records_claude_supervision_host_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude supervision host", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                    max_rounds=5,
                )
                append_message(
                    root,
                    run_id,
                    "host",
                    "后续如果要继续，优先用列表格式。",
                    sender="browser",
                    task_id="task-001",
                    role="host",
                    from_agent="browser",
                    to_agent="host",
                )
                append_message(root, run_id, "main", "托管测试", sender="browser", task_id="task-001", role="main")
                host_reply = '{"decision":"continue","reason":"needs priority","response":"先按阻塞项排优先级。","actions":[]}'

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "托管回复", None)),
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}) as start_host,
                ):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                with (
                    mock.patch("aha_cli.services.chat.run_claude_exec", return_value=(0, host_reply, None)) as host_run,
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}) as start_main,
                ):
                    host_code, _host_output = self.run_cli(
                        "claude-chat",
                        run_id,
                        "host",
                        "--sender",
                        "host",
                        "--sandbox",
                        "read-only",
                        "--task-id",
                        "task-001",
                        "--once",
                    )
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task = status_snapshot(root, run_id)["tasks"][0]
                main_page = conversation_events_page(root, run_id, "task-001", "main", limit=20)
                host_page = conversation_events_page(root, run_id, "task-001", "host", limit=20)
                host_inbox_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "host"), 0)

        self.assertEqual(code, 0)
        self.assertEqual(host_code, 0)
        self.assertIn("main -> browser: 托管回复", output)
        start_host.assert_called_once()
        self.assertEqual(start_host.call_args.args[2], "host")
        self.assertEqual(host_run.call_args.kwargs["permission_mode"], "plan")
        self.assertIn("后续如果要继续，优先用列表格式。", host_run.call_args.args[0])
        self.assertIn("托管回复", host_run.call_args.args[0])
        host = next(agent for agent in task["agents"] if agent["role"] == "host")
        self.assertEqual(host["backend"], "claude")
        self.assertEqual(host["sandbox"], "read-only")
        start_main.assert_called_once()
        self.assertTrue(any(row.get("message") == "托管回复" for row in host_inbox_messages))
        display_messages = [
            row["data"]
            for row in rows
            if row["type"] == "message" and row["data"].get("display_sender")
        ]
        self.assertEqual(
            [(row["display_sender"], row["display_target"], row["message"]) for row in display_messages],
            [("main", "host", "托管回复"), ("host", "main", "先按阻塞项排优先级。")],
        )
        self.assertFalse(any((row["data"].get("conversation") == "supervision") for row in rows if row["type"] == "message"))
        self.assertFalse(any("AHA 启动托管 host" in row["data"].get("message", "") for row in rows if row["type"] == "message"))
        self.assertFalse(any("host 正在判断" in row["data"].get("message", "") for row in rows if row["type"] == "message"))
        main_host_messages = [
            row["data"]
            for row in rows
            if row["type"] == "message"
            and row["data"].get("sender") == "main"
            and row["data"].get("target") == "host"
            and row["data"].get("message") == "托管回复"
        ]
        self.assertEqual(len(main_host_messages), 1)
        self.assertEqual(main_host_messages[0]["from_agent"], "main")
        self.assertEqual(main_host_messages[0]["to_agent"], "host")
        self.assertEqual(main_host_messages[0]["agent_id"], "host")
        routed_user_messages = [
            row["data"]
            for row in rows
            if row["type"] == "message"
            and row["data"].get("sender") == "browser"
            and row["data"].get("target") == "main"
            and row["data"].get("message") == "先按阻塞项排优先级。"
        ]
        self.assertEqual(len(routed_user_messages), 1)
        self.assertEqual(routed_user_messages[0]["from_agent"], "browser")
        self.assertEqual(routed_user_messages[0]["to_agent"], "main")
        self.assertEqual(routed_user_messages[0]["display_sender"], "host")
        self.assertEqual(routed_user_messages[0]["display_target"], "main")
        self.assertEqual(routed_user_messages[0]["agent_id"], "host")
        self.assertIn("browser -> main", format_event({"type": "message", "ts": "now", "data": routed_user_messages[0]}))
        self.assertTrue(any(event.get("data", {}).get("message") == "先按阻塞项排优先级。" for event in main_page["events"]))
        self.assertTrue(
            any(
                (event.get("data") or {}).get("display_sender") == "main"
                and (event.get("data") or {}).get("display_target") == "host"
                for event in main_page["events"]
            )
        )
        self.assertTrue(any((event.get("data") or {}).get("display_sender") == "main" and (event.get("data") or {}).get("display_target") == "host" for event in host_page["events"]))
        self.assertTrue(any((event.get("data") or {}).get("display_sender") == "host" and (event.get("data") or {}).get("display_target") == "main" for event in host_page["events"]))
        self.assertTrue(any(row["type"] == "main_reported_to_host" for row in rows))
        host_decisions = [row for row in rows if row["type"] == "host_decision"]
        self.assertEqual(host_decisions[-1]["data"]["host_backend"], "claude")
        self.assertEqual(host_decisions[-1]["data"]["decision"], "continue")
        self.assertEqual(host_decisions[-1]["data"]["executed_action_count"], 0)
        self.assertTrue(host_decisions[-1]["data"]["routed_to_main"])
        applied = [row for row in rows if row["type"] == "main_applied_decision"]
        self.assertEqual(applied[-1]["data"]["effect"], "routed_to_main")
        self.assertTrue(applied[-1]["data"]["routed_to_main"])
        self.assertEqual(task["status"], "running")

    def test_claude_supervision_wait_does_not_route_main_while_sub_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude supervision waits", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                    max_rounds=5,
                )
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_agent_status(root, run_id, "task-001", sub["id"], "running")
                set_task_status(root, run_id, "task-001", "running")

                with mock.patch("aha_cli.services.chat.start_backend") as start_main:
                    result = apply_supervision_host_decision(
                        root,
                        run_id,
                        "task-001",
                        host_agent_id="host",
                        host_reply='{"decision":"continue","reason":"waiting for sub","response":"好。","actions":[]}',
                        exit_code=0,
                    )

                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task = status_snapshot(root, run_id)["tasks"][0]

        self.assertTrue(result["waiting"])
        self.assertFalse(result["routed_to_main"])
        start_main.assert_not_called()
        self.assertEqual(task["status"], "running")
        self.assertEqual(next(agent for agent in task["agents"] if agent["id"] == "main")["status"], "waiting")
        self.assertFalse(
            any(
                row["type"] == "message"
                and row["data"].get("display_sender") == "host"
                and row["data"].get("display_target") == "main"
                for row in rows
            )
        )
        host_decisions = [row for row in rows if row["type"] == "host_decision"]
        self.assertEqual(host_decisions[-1]["data"]["decision"], "wait")
        self.assertTrue(host_decisions[-1]["data"]["waiting"])
        applied = [row for row in rows if row["type"] == "main_applied_decision"]
        self.assertEqual(applied[-1]["data"]["effect"], "waiting")

    def test_codex_chat_empty_main_reply_waits_when_sub_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Empty main reply with sub", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_agent_status(root, run_id, "task-001", sub["id"], "running")
                set_task_status(root, run_id, "task-001", "running")
                append_message(root, run_id, "main", "继续", sender="browser", task_id="task-001", role="main")

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "", None)),
                    mock.patch("aha_cli.services.chat.stop_task_backends") as stop_backends,
                ):
                    code, _output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task = status_snapshot(root, run_id)["tasks"][0]

        self.assertEqual(code, 0)
        stop_backends.assert_not_called()
        self.assertEqual(task["status"], "running")
        self.assertEqual(next(agent for agent in task["agents"] if agent["id"] == "main")["status"], "waiting")
        self.assertTrue(any(row["type"] == "agent_message_skipped" for row in rows))
        self.assertFalse(any(row["type"] == "agent_error" and row["data"].get("target") == "main" for row in rows))

    def test_codex_chat_routes_claude_supervision_ask_user_to_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude supervision asks user", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                    max_rounds=5,
                )
                append_message(root, run_id, "main", "托管测试", sender="browser", task_id="task-001", role="main")
                host_reply = '{"decision":"ask_user","reason":"needs real user","response":"你要继续改代码吗？","actions":[]}'

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "托管回复", None)),
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}) as start_host,
                ):
                    code, _output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                with (
                    mock.patch("aha_cli.services.chat.run_claude_exec", return_value=(0, host_reply, None)),
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}) as start_main,
                ):
                    host_code, _host_output = self.run_cli(
                        "claude-chat",
                        run_id,
                        "host",
                        "--sender",
                        "host",
                        "--sandbox",
                        "read-only",
                        "--task-id",
                        "task-001",
                        "--once",
                    )
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task = status_snapshot(root, run_id)["tasks"][0]
                main_page = conversation_events_page(root, run_id, "task-001", "main", limit=20)
                host_page = conversation_events_page(root, run_id, "task-001", "host", limit=20)

        self.assertEqual(code, 0)
        self.assertEqual(host_code, 0)
        start_host.assert_called_once()
        start_main.assert_not_called()
        main_host_messages = [
            row["data"]
            for row in rows
            if row["type"] == "message"
            and row["data"].get("sender") == "main"
            and row["data"].get("target") == "host"
            and row["data"].get("message") == "托管回复"
        ]
        self.assertEqual(len(main_host_messages), 1)
        self.assertEqual(main_host_messages[0]["display_sender"], "main")
        self.assertEqual(main_host_messages[0]["display_target"], "host")
        host_browser_messages = [
            row["data"]
            for row in rows
            if row["type"] == "message"
            and row["data"].get("sender") == "host"
            and row["data"].get("target") == "browser"
            and row["data"].get("message") == "你要继续改代码吗？"
        ]
        self.assertEqual(len(host_browser_messages), 1)
        self.assertEqual(host_browser_messages[0]["display_sender"], "host")
        self.assertEqual(host_browser_messages[0]["display_target"], "browser")
        self.assertEqual(host_browser_messages[0]["agent_id"], "main")
        self.assertTrue(any(event.get("data", {}).get("message") == "你要继续改代码吗？" for event in main_page["events"]))
        self.assertTrue(any(event.get("data", {}).get("message") == "你要继续改代码吗？" for event in host_page["events"]))
        host_decisions = [row for row in rows if row["type"] == "host_decision"]
        self.assertEqual(host_decisions[-1]["data"]["decision"], "ask_user")
        self.assertTrue(host_decisions[-1]["data"]["routed_to_browser"])
        applied = [row for row in rows if row["type"] == "main_applied_decision"]
        self.assertEqual(applied[-1]["data"]["effect"], "await_user")
        self.assertTrue(applied[-1]["data"]["routed_to_browser"])
        self.assertEqual(task["status"], "awaiting_user")

    def test_claude_supervision_max_rounds_are_per_user_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude supervision max rounds", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                    max_rounds=1,
                )
                for index in range(3):
                    append_event(
                        root,
                        run_id,
                        "host_decision",
                        {
                            "task_id": "task-001",
                            "host_agent_id": "host",
                            "decision": "continue",
                            "host_round": index + 1,
                        },
                    )
                append_message(root, run_id, "main", "新一轮测试", sender="browser", task_id="task-001", role="main")
                host_reply = '{"decision":"continue","reason":"new turn","response":"继续处理这一轮。","actions":[]}'

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "main 本轮回复", None)),
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}),
                ):
                    code, _output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                with (
                    mock.patch("aha_cli.services.chat.run_claude_exec", return_value=(0, host_reply, None)),
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}) as start_main,
                ):
                    host_code, _host_output = self.run_cli(
                        "claude-chat",
                        run_id,
                        "host",
                        "--sender",
                        "host",
                        "--sandbox",
                        "read-only",
                        "--task-id",
                        "task-001",
                        "--once",
                    )
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]

        self.assertEqual(code, 0)
        self.assertEqual(host_code, 0)
        start_main.assert_called_once()
        host_decisions = [row for row in rows if row["type"] == "host_decision"]
        self.assertEqual(host_decisions[-1]["data"]["host_round"], 1)
        self.assertTrue(host_decisions[-1]["data"]["routed_to_main"])

    def test_codex_chat_routes_claude_supervision_stop_to_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude supervision stops", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                    max_rounds=5,
                )
                append_message(root, run_id, "main", "托管测试", sender="browser", task_id="task-001", role="main")
                host_reply = '{"decision":"stop","reason":"done","response":"没问题，这轮可以结束。","actions":[]}'

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "托管回复", None)),
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}) as start_host,
                ):
                    code, _output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                with (
                    mock.patch("aha_cli.services.chat.run_claude_exec", return_value=(0, host_reply, None)),
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}) as start_main,
                ):
                    host_code, _host_output = self.run_cli(
                        "claude-chat",
                        run_id,
                        "host",
                        "--sender",
                        "host",
                        "--sandbox",
                        "read-only",
                        "--task-id",
                        "task-001",
                        "--once",
                    )
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task = status_snapshot(root, run_id)["tasks"][0]

        self.assertEqual(code, 0)
        self.assertEqual(host_code, 0)
        start_host.assert_called_once()
        start_main.assert_not_called()
        host_browser_messages = [
            row["data"]
            for row in rows
            if row["type"] == "message"
            and row["data"].get("sender") == "host"
            and row["data"].get("target") == "browser"
            and row["data"].get("message") == "没问题，这轮可以结束。"
        ]
        self.assertEqual(len(host_browser_messages), 1)
        self.assertEqual(host_browser_messages[0]["display_sender"], "host")
        self.assertEqual(host_browser_messages[0]["display_target"], "browser")
        host_main_messages = [
            row["data"]
            for row in rows
            if row["type"] == "message"
            and row["data"].get("display_sender") == "host"
            and row["data"].get("display_target") == "main"
        ]
        self.assertEqual(host_main_messages, [])
        host_decisions = [row for row in rows if row["type"] == "host_decision"]
        self.assertEqual(host_decisions[-1]["data"]["decision"], "stop")
        self.assertTrue(host_decisions[-1]["data"]["routed_to_browser"])
        applied = [row for row in rows if row["type"] == "main_applied_decision"]
        self.assertEqual(applied[-1]["data"]["effect"], "stopped")
        self.assertTrue(applied[-1]["data"]["routed_to_browser"])
        self.assertEqual(task["status"], "awaiting_user")

    def test_codex_chat_records_prompt_metrics_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Codex prompt metrics", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "measure prompt", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "reply", None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]

        metrics_events = [row for row in rows if row["type"] == "agent_prompt_metrics"]
        self.assertEqual(code, 0)
        self.assertEqual(len(metrics_events), 1)
        metrics = metrics_events[0]["data"]
        self.assertEqual(metrics["source"], "codex-chat")
        self.assertEqual(metrics["task_id"], "task-001")
        self.assertGreater(metrics["total"]["chars"], 0)
        self.assertGreater(metrics["components"]["status_snapshot"]["chars"], 0)
        self.assertGreater(metrics["components"]["task_context"]["chars"], 0)

    def test_agent_command_does_not_write_task_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
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
                self.run_cli("init", "--portable", "--backend", "codex")
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

    def test_raw_result_file_without_final_metadata_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
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
                self.run_cli("init", "--portable", "--backend", "codex")
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
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["status"], "completed")
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["exit_code"], 0)

    def test_final_driven_completion_reopen_preserves_round_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final loop", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                append_message(root, run_id, "main", "先做第一轮", sender="browser", task_id="task-001", role="main")
                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "第一轮已处理", None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["status"], "awaiting_user")
                self.assertEqual(task_snapshot(root, run_id, "task-001")["result"], "")
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                self.assertTrue(any(event["type"] == "agent_finished" for event in events))
                self.assertFalse(any(event["type"] == "task_completed" for event in events))

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/aha final",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("Finalization requested", payload["message"]["message"])

                first_final = "第一轮 Final"
                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, first_final, None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--once")

                self.assertEqual(code, 0)
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "completed")
                self.assertEqual(detail["result"].strip(), first_final)
                first_rounds = list_task_lifecycle_rounds(root, run_id, "task-001")
                self.assertEqual(len(first_rounds), 1)
                self.assertEqual(first_rounds[0]["round_id"], "round-001")
                self.assertEqual(first_rounds[0]["status"], "finalized")
                self.assertEqual((run_dir(root, run_id) / first_rounds[0]["final_path"]).read_text(encoding="utf-8").strip(), first_final)

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/aha reopen",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("reopened", payload["message"]["message"])
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "awaiting_user")
                self.assertEqual(detail["result"].strip(), first_final)
                reopened_rounds = list_task_lifecycle_rounds(root, run_id, "task-001")
                self.assertEqual(len(reopened_rounds), 2)
                self.assertEqual(reopened_rounds[0]["status"], "finalized")
                self.assertEqual(reopened_rounds[1]["round_id"], "round-002")
                self.assertEqual(reopened_rounds[1]["status"], "active")
                self.assertEqual(reopened_rounds[1]["reopened_from_round_id"], "round-001")
                self.assertEqual((run_dir(root, run_id) / reopened_rounds[0]["final_path"]).read_text(encoding="utf-8").strip(), first_final)

                append_message(root, run_id, "main", "继续第二轮", sender="browser", task_id="task-001", role="main")
                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "第二轮已处理", None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--once")
                self.assertEqual(code, 0)
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["status"], "awaiting_user")

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/aha final",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("Finalization requested", payload["message"]["message"])

                second_final = "第二轮 Final"
                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, second_final, None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--once")

                self.assertEqual(code, 0)
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "completed")
                self.assertEqual(detail["result"].strip(), second_final)
                final_rounds = list_task_lifecycle_rounds(root, run_id, "task-001")
                self.assertEqual(len(final_rounds), 2)
                self.assertEqual(final_rounds[0]["status"], "finalized")
                self.assertEqual(final_rounds[1]["status"], "finalized")
                self.assertEqual((run_dir(root, run_id) / final_rounds[0]["final_path"]).read_text(encoding="utf-8").strip(), first_final)
                self.assertEqual((run_dir(root, run_id) / final_rounds[1]["final_path"]).read_text(encoding="utf-8").strip(), second_final)

    def test_final_api_returns_task_overview_and_preserves_round_finals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final overview", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                task = task_snapshot(root, run_id, "task-001")["task"]
                first_final = "#### Final\n## 阶段总览\n第一轮 raw final 正文只应保留在 round 快照文件中"
                second_final = "#### Final\n## 阶段总览\n第二轮 raw final 正文只应保留在 round 快照文件中"

                write_task_result(root, run_id, "task-001", first_final)
                complete_task(root, run_id, "task-001", 0)
                first_round = list_task_lifecycle_rounds(root, run_id, "task-001")[0]
                first_round_final = run_dir(root, run_id) / first_round["final_path"]

                reopen_task(root, run_id, "task-001")
                write_task_result(root, run_id, "task-001", second_final)
                complete_task(root, run_id, "task-001", 0)
                second_round = list_task_lifecycle_rounds(root, run_id, "task-001")[1]
                second_round_final = run_dir(root, run_id) / second_round["final_path"]
                reopen_task(root, run_id, "task-001")

                snapshot = task_final_snapshot(root, run_id, "task-001")
                api_response = asyncio.run(fetch_ui_response(root, run_id, "/api/task/task-001/final"))
                api_body = json_response_body(api_response)
                overview_text = (run_dir(root, run_id) / "results/task-001.md").read_text(encoding="utf-8")

                self.assertTrue(api_response.startswith(b"HTTP/1.1 200 OK"))
                self.assertEqual(api_body["result"], overview_text)
                self.assertNotEqual(api_body["result"].strip(), second_final)
                self.assertIn("task-001", api_body["result"])
                self.assertIn(task["title"], api_body["result"])
                for expected in ("round-001", "round-002", "round-003"):
                    self.assertIn(expected, api_body["result"])
                self.assertIn("Raw final:", api_body["result"])
                self.assertNotIn(first_final, api_body["result"])
                self.assertNotIn(second_final, api_body["result"])
                self.assertNotIn("#### Final", api_body["result"])
                self.assertNotIn("## 阶段总览", api_body["result"])
                self.assertIn(first_round["final_path"], api_body["result"])
                self.assertIn(second_round["final_path"], api_body["result"])
                self.assertRegex(api_body["result"].lower(), r"reopen|reopened|复开|重开|重新打开|继续")
                self.assertEqual(first_round_final.read_text(encoding="utf-8").strip(), first_final)
                self.assertEqual(second_round_final.read_text(encoding="utf-8").strip(), second_final)
                self.assertEqual([item["round_id"] for item in snapshot["finals"]], ["round-001", "round-002"])

    def test_record_task_update_uses_current_lifecycle_round_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Journal lifecycle", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                write_task_result(root, run_id, "task-001", "第一轮 Final")
                complete_task(root, run_id, "task-001", 0)
                reopen_task(root, run_id, "task-001")

                reply = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "record_task_update",
                                "summary": "第二轮修复 Round/Journal 关联",
                                "changed_files": ["src/aha_cli/store/filesystem.py"],
                            }
                        ],
                        "response": "已记录",
                    },
                    ensure_ascii=False,
                )

                executed = execute_actions(root, run_id, "task-001", reply)

                self.assertEqual(executed, [{"type": "record_task_update", "round_id": "round-002"}])
                journal = list_task_rounds(root, run_id, "task-001")
                self.assertEqual(journal[0]["round_id"], "round-002")
                self.assertEqual(journal[0]["round_sequence"], 2)
                self.assertEqual(journal[0]["sequence"], 2)
                self.assertEqual(journal[0]["journal_id"], "journal-001")
                self.assertEqual(journal[0]["journal_sequence"], 1)
                journal_text = (run_dir(root, run_id) / "results/task-001.md").read_text(encoding="utf-8")
                self.assertIn("轮次：`round-002`", journal_text)
                self.assertNotIn("轮次：`round-001`", journal_text)
                prompt = finalization_prompt("task-001", "Journal lifecycle", journal)
                self.assertIn("round-002", prompt)
                self.assertNotIn("round-001", prompt)
                final = task_final_snapshot(root, run_id, "task-001")
                self.assertIn("round-001", final["result"])
                self.assertEqual(final["finals"][0]["round_id"], "round-001")
                self.assertEqual((run_dir(root, run_id) / final["finals"][0]["final_path"]).read_text(encoding="utf-8").strip(), "第一轮 Final")

    def test_final_summary_stops_task_scoped_backends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final cleanup", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_agent_status(root, run_id, "task-001", "sub-001", "completed", 0)
                set_task_status(root, run_id, "task-001", "running")
                append_message(
                    root,
                    run_id,
                    "main",
                    "Produce final summary",
                    sender="aha",
                    task_id="task-001",
                    role="main",
                    result_policy="finalize",
                    reply_target="browser",
                )

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "final summary", None)),
                    mock.patch("aha_cli.services.chat.stop_task_backends", return_value=[]) as stop_backends,
                    mock.patch("aha_cli.services.chat.mark_backend_stopped") as mark_stopped,
                ):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

                self.assertEqual(code, 0)
                stop_backends.assert_called_once()
                self.assertEqual(stop_backends.call_args.args[:3], (root / ".aha", run_id, "task-001"))
                self.assertIn("exclude_pid", stop_backends.call_args.kwargs)
                mark_stopped.assert_called_once()
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["status"], "completed")

    def test_task_scoped_main_backend_stops_after_round_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Round cleanup", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_agent_status(root, run_id, "task-001", sub["id"], "completed", 0)
                set_task_status(root, run_id, "task-001", "running")
                append_message(
                    root,
                    run_id,
                    "main",
                    render_prompt_template("task_round_summary.md", task_id="task-001"),
                    sender="aha",
                    task_id="task-001",
                    role="main",
                    reply_target="browser",
                    coordination="subagents_complete",
                )

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "round summary", None)),
                    mock.patch("aha_cli.services.chat.mark_backend_stopped") as mark_stopped,
                ):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

                self.assertEqual(code, 0)
                mark_stopped.assert_called_once()
                self.assertEqual(mark_stopped.call_args.args[:3], (root / ".aha", run_id, "main"))
                self.assertEqual(mark_stopped.call_args.kwargs["task_id"], "task-001")
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "awaiting_user")
                self.assertEqual(next(agent for agent in detail["task"]["agents"] if agent["id"] == "main")["status"], "completed")

    def test_task_scoped_main_backend_keeps_running_while_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Waiting backend", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_agent_status(root, run_id, "task-001", sub["id"], "running")
                set_task_status(root, run_id, "task-001", "running")
                append_message(root, run_id, "main", "继续", sender="browser", task_id="task-001", role="main")

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "", None)),
                    mock.patch("aha_cli.services.chat.mark_backend_stopped") as mark_stopped,
                ):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

                self.assertEqual(code, 0)
                mark_stopped.assert_not_called()
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "running")
                self.assertEqual(next(agent for agent in detail["task"]["agents"] if agent["id"] == "main")["status"], "waiting")

    def test_codex_chat_executes_spawn_sub_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
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
                self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "running")
                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)
                self.assertIn("等待子 agent 完成", browser_messages[-1]["message"])

    def test_execute_actions_reuses_interrupted_sub_agent_at_max(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recover sub slot", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_two = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_three = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                append_message(root, run_id, sub["id"], "old assignment", sender="main", task_id="task-001", role="sub")
                set_agent_status(root, run_id, "task-001", sub["id"], "interrupted")
                set_agent_status(root, run_id, "task-001", sub_two["id"], "completed", 0)
                set_agent_status(root, run_id, "task-001", sub_three["id"], "completed", 0)
                update_agent_runtime(root, run_id, "task-001", sub["id"], recovery_context="old failure")
                reply = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "spawn_sub",
                                "title": "Recover and inspect issue 02",
                                "backend": "codex",
                                "reason": "previous sub interrupted",
                            }
                        ],
                        "response": "请求 AHA 恢复 sub",
                    }
                )

                with mock.patch("aha_cli.services.orchestrator.start_backend") as start_backend_mock:
                    executed = execute_actions(root, run_id, "task-001", reply)

                detail = task_snapshot(root, run_id, "task-001")["task"]
                agent = next(item for item in detail["agents"] if item["id"] == sub["id"])
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, sub["id"]), 0)
                offset = load_chat_offset(
                    inbox_path(root, run_id, sub["id"]),
                    chat_offset_path(run_dir(root, run_id), sub["id"], "task-001"),
                    from_start=False,
                )
                new_messages, _ = iter_jsonl_from(inbox_path(root, run_id, sub["id"]), offset)
                rows, _ = iter_jsonl_from(event_path(root, run_id), 0)

        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0]["type"], "spawn_sub")
        self.assertTrue(executed[0]["reused"])
        self.assertEqual(executed[0]["agent"]["id"], sub["id"])
        self.assertEqual(agent["status"], "pending")
        self.assertEqual(agent["assignment"], "Recover and inspect issue 02")
        self.assertEqual(agent["recovery_context"], "")
        self.assertEqual(len([item for item in detail["agents"] if item.get("role") == "sub"]), 3)
        self.assertEqual(messages[-1]["message"], "Recover and inspect issue 02")
        self.assertEqual([item["message"] for item in new_messages], ["Recover and inspect issue 02"])
        start_backend_mock.assert_called_once()
        self.assertFalse(any(row["type"] == "action_skipped" and row["data"].get("type") == "spawn_sub" for row in rows))
        self.assertTrue(any(row["type"] == "sub_agent_reused" for row in rows))

    def test_execute_actions_reuses_distinct_sub_agents_in_spawn_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recover several sub slots", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_two = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_three = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_agent_status(root, run_id, "task-001", sub["id"], "completed", 0)
                set_agent_status(root, run_id, "task-001", sub_two["id"], "interrupted")
                set_agent_status(root, run_id, "task-001", sub_three["id"], "interrupted")
                reply = json.dumps(
                    {
                        "actions": [
                            {"type": "spawn_sub", "title": "Inspect issue 02", "backend": "codex"},
                            {"type": "spawn_sub", "title": "Inspect issue 04", "backend": "codex"},
                            {"type": "spawn_sub", "title": "Inspect issue 03", "backend": "codex"},
                        ],
                        "response": "请求 AHA 分配三个 sub",
                    }
                )

                with mock.patch("aha_cli.services.orchestrator.start_backend") as start_backend_mock:
                    executed = execute_actions(root, run_id, "task-001", reply)

                detail = task_snapshot(root, run_id, "task-001")["task"]
                rows, _ = iter_jsonl_from(event_path(root, run_id), 0)
                sub_two_messages, _ = iter_jsonl_from(inbox_path(root, run_id, sub_two["id"]), 0)
                sub_three_messages, _ = iter_jsonl_from(inbox_path(root, run_id, sub_three["id"]), 0)

        self.assertEqual([item["agent"]["id"] for item in executed], [sub_two["id"], sub_three["id"]])
        self.assertEqual([item["agent"]["assignment"] for item in executed], ["Inspect issue 02", "Inspect issue 04"])
        self.assertEqual(next(item for item in detail["agents"] if item["id"] == sub_two["id"])["assignment"], "Inspect issue 02")
        self.assertEqual(next(item for item in detail["agents"] if item["id"] == sub_three["id"])["assignment"], "Inspect issue 04")
        self.assertEqual(sub_two_messages[-1]["message"], "Inspect issue 02")
        self.assertEqual(sub_three_messages[-1]["message"], "Inspect issue 04")
        self.assertEqual(start_backend_mock.call_count, 2)
        self.assertEqual(len([row for row in rows if row["type"] == "sub_agent_reused"]), 2)
        self.assertTrue(any(row["type"] == "action_skipped" and row["data"].get("reason") == "max_sub_agents reached" for row in rows))

    def test_execute_actions_spawn_sub_can_target_specific_existing_sub_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Retarget one sub slot", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_two = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_three = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_agent_status(root, run_id, "task-001", sub["id"], "completed", 0)
                set_agent_status(root, run_id, "task-001", sub_two["id"], "completed", 0)
                set_agent_status(root, run_id, "task-001", sub_three["id"], "completed", 0)
                reply = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "spawn_sub",
                                "agent_id": sub_three["id"],
                                "title": "Reassign issue 04",
                                "backend": "codex",
                            }
                        ],
                        "response": "指定 sub-003 重新分析",
                    }
                )

                with mock.patch("aha_cli.services.orchestrator.start_backend") as start_backend_mock:
                    executed = execute_actions(root, run_id, "task-001", reply)

                detail = task_snapshot(root, run_id, "task-001")["task"]
                rows, _ = iter_jsonl_from(event_path(root, run_id), 0)
                sub_three_messages, _ = iter_jsonl_from(inbox_path(root, run_id, sub_three["id"]), 0)

        agent = next(item for item in detail["agents"] if item["id"] == sub_three["id"])
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0]["agent"]["id"], sub_three["id"])
        self.assertEqual(executed[0]["requested_agent_id"], sub_three["id"])
        self.assertEqual(agent["status"], "pending")
        self.assertEqual(agent["assignment"], "Reassign issue 04")
        self.assertEqual(sub_three_messages[-1]["message"], "Reassign issue 04")
        start_backend_mock.assert_called_once()
        self.assertFalse(any(row["type"] == "action_skipped" and row["data"].get("type") == "spawn_sub" for row in rows))
        self.assertTrue(
            any(
                row["type"] == "sub_agent_reused"
                and row["data"].get("agent_id") == sub_three["id"]
                and row["data"].get("reason") == "spawn_sub assigned to requested sub-agent"
                for row in rows
            )
        )

    def test_execute_actions_reports_spawn_sub_skipped_without_reusable_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "No sub slot", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_two = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_three = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_agent_status(root, run_id, "task-001", sub["id"], "completed", 0)
                set_agent_status(root, run_id, "task-001", sub_two["id"], "completed", 0)
                set_agent_status(root, run_id, "task-001", sub_three["id"], "completed", 0)
                reply = json.dumps(
                    {
                        "actions": [{"type": "spawn_sub", "title": "Extra sub", "backend": "codex"}],
                        "response": "请求 AHA 创建 sub",
                    }
                )

                with mock.patch("aha_cli.services.orchestrator.start_backend") as start_backend_mock:
                    executed = execute_actions(root, run_id, "task-001", reply)

                rows, _ = iter_jsonl_from(event_path(root, run_id), 0)
                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)
                detail = task_snapshot(root, run_id, "task-001")["task"]

        self.assertEqual(executed, [])
        start_backend_mock.assert_not_called()
        self.assertEqual(len([item for item in detail["agents"] if item.get("role") == "sub"]), 3)
        self.assertTrue(any(row["type"] == "action_skipped" and row["data"].get("reason") == "max_sub_agents reached" for row in rows))
        self.assertEqual(browser_messages[-1]["sender"], "aha")
        self.assertIn("没有创建新的 sub-agent", browser_messages[-1]["message"])

    def test_codex_chat_flags_subagent_claim_without_spawn_sub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sub claim mismatch", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "delegate this", sender="browser", task_id="task-001", role="main")
                reply = "3个sub agent已并行启动。我现在继续分析。"

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, reply, None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                detail = task_snapshot(root, run_id, "task-001")

        self.assertEqual(code, 0)
        self.assertFalse(any(agent["id"].startswith("sub-") for agent in detail["task"]["agents"]))
        mismatch_events = [event for event in events if event["type"] == "claimed_sub_without_aha_agent"]
        self.assertEqual(len(mismatch_events), 1)
        self.assertEqual(mismatch_events[0]["data"]["reason"], "reply_claim_without_spawn_sub_action")

    def test_action_parser_ignores_embedded_json_examples(self) -> None:
        reply = textwrap.dedent(
            """\
            我建议使用这个格式：

            ```json
            {"actions":[{"type":"route_to_agent","agent_id":"...","message":"..."}],"response":"..."}
            ```
            """
        ).strip()

        self.assertIsNone(extract_action_payload(reply))
        self.assertEqual(action_response_text(reply), reply)

    def test_invalid_top_level_action_schema_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Bad route schema", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                reply = json.dumps(
                    {
                        "action": "route_to_agent",
                        "agent_id": "sub-001",
                        "message": "This should not route",
                        "response": "sent",
                    }
                )

                with mock.patch("aha_cli.services.orchestrator.start_backend") as start_backend_mock:
                    executed = execute_actions(root, run_id, "task-001", reply)

                self.assertEqual(executed, [])
                self.assertIn("Invalid AHA action schema", action_response_text(reply))
                start_backend_mock.assert_not_called()
                sub_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "sub-001"), 0)
                self.assertEqual(sub_messages, [])
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                self.assertTrue(any(event["type"] == "invalid_action_schema" for event in events))

    def test_execute_actions_records_task_update_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Journal task", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                reply = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "record_task_update",
                                "summary": "修复 action schema 误解析",
                                "changed_files": ["src/aha_cli/services/orchestrator.py"],
                                "verification": ["tests OK"],
                                "risks": [],
                            }
                        ],
                        "response": "已记录",
                    },
                    ensure_ascii=False,
                )

                executed = execute_actions(root, run_id, "task-001", reply)

                self.assertEqual(executed, [{"type": "record_task_update", "round_id": "round-001"}])
                rounds = list_task_rounds(root, run_id, "task-001")
                self.assertEqual(rounds[0]["summary"], "修复 action schema 误解析")
                self.assertEqual(rounds[0]["changed_files"], ["src/aha_cli/services/orchestrator.py"])
                snapshot = task_final_snapshot(root, run_id, "task-001")
                self.assertEqual(snapshot["result_meta"]["policy"], "journal")
                self.assertIn("修复 action schema 误解析", snapshot["result"])
                self.assertIn("## 任务轮次", snapshot["result"])
                self.assertIn("1. `round-001` 修复 action schema 误解析", snapshot["result"])
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                self.assertTrue(any(event["type"] == "task_round_recorded" for event in events))

    def test_codex_chat_record_task_update_does_not_wait_for_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Record round", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "完成了一个小修复", sender="browser", task_id="task-001", role="main")
                reply = json.dumps(
                    {
                        "actions": [{"type": "record_task_update", "summary": "完成小修复"}],
                        "response": "完成",
                    },
                    ensure_ascii=False,
                )

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, reply, None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                self.assertIn("完成", output)
                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)
                self.assertNotIn("等待子 agent 完成", "\n".join(item["message"] for item in browser_messages))
                self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "awaiting_user")

    def test_finalization_prompt_includes_task_journal(self) -> None:
        prompt = finalization_prompt(
            "task-001",
            "Journal task",
            [
                {
                    "round_id": "round-001",
                    "trigger": "main_turn",
                    "summary": "完成小修复",
                    "changed_files": ["src/app.py"],
                    "verification": ["unit tests"],
                    "risks": [],
                }
            ],
        )

        self.assertIn("Task journal (chronological ordered list):", prompt)
        self.assertIn("1. 完成小修复", prompt)
        self.assertIn("round-001", prompt)
        self.assertIn("完成小修复", prompt)
        self.assertIn("Use the Task journal as the primary source", prompt)
        self.assertIn("chronological ordered list", prompt)

    def test_aha_checkpoint_records_task_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Checkpoint task", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                handled, forwarded, response = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main"},
                    "/aha checkpoint 完成第一轮清理",
                    "task-001",
                )

                self.assertTrue(handled)
                self.assertIsNone(forwarded)
                self.assertIn("Checkpoint recorded", response["message"]["message"])
                rounds = list_task_rounds(root, run_id, "task-001")
                self.assertEqual(rounds[0]["trigger"], "manual")
                self.assertEqual(rounds[0]["summary"], "完成第一轮清理")

    def test_codex_chat_autostarts_codex_sub_agent_from_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Spawn codex sub", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "delegate this", sender="browser", task_id="task-001", role="main")
                reply = '{"complexity":"complex","actions":[{"type":"spawn_sub","title":"Inspect one slice","backend":"codex","reason":"parallel research"}],"response":"delegating"}'

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, reply, None)),
                    mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_backend,
                ):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                start_backend.assert_called_once()
                self.assertEqual(start_backend.call_args.args[:3], (root / ".aha", run_id, "sub-001"))
                self.assertTrue(start_backend.call_args.kwargs["from_start"])
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "sub-001"), 0)
                self.assertEqual(messages[-1]["message"], "Inspect one slice")
                detail = task_snapshot(root, run_id, "task-001")
                sub_agent = next(agent for agent in detail["task"]["agents"] if agent["id"] == "sub-001")
                self.assertEqual(sub_agent["assignment"], "Inspect one slice")
                self.assertTrue(detail["task"]["coordination"]["followup_started_at"])

    def test_main_routes_followup_to_responsible_sub_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Owned follow-up", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_agent_status(root, run_id, "task-001", "sub-001", "completed", 0)
                mark_task_coordination(
                    root,
                    run_id,
                    "task-001",
                    final_summary_requested_at="2026-05-15T00:00:00+00:00",
                    final_summary_completed_at="2026-05-15T00:01:00+00:00",
                )
                set_task_status(root, run_id, "task-001", "completed", 0)
                reopen_task(root, run_id, "task-001")
                append_message(root, run_id, "main", "这个范围继续调整", sender="browser", task_id="task-001", role="main")
                reply = json.dumps(
                    {
                        "complexity": "simple",
                        "actions": [
                            {
                                "type": "route_to_agent",
                                "agent_id": "sub-001",
                                "message": "请继续调整你负责的范围",
                                "reason": "sub-001 owns this scope",
                            }
                        ],
                        "response": "已转给 sub-001 处理。",
                    }
                )

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, reply, None)),
                    mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_backend,
                ):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                self.assertIn("main -> browser: 已转给 sub-001 处理。", output)
                self.assertNotIn("route_to_agent", output)
                sub_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "sub-001"), 0)
                self.assertEqual(sub_messages[-1]["message"], "请继续调整你负责的范围")
                self.assertEqual(sub_messages[-1]["coordination"], "routed_by_main")
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "running")
                self.assertIsNone(detail["task"]["exit_code"])
                self.assertEqual(detail["task"]["coordination"]["final_summary_requested_at"], "")
                self.assertEqual(detail["task"]["coordination"]["final_summary_completed_at"], "")
                self.assertTrue(detail["task"]["coordination"]["followup_started_at"])
                start_backend.assert_called_once()

                report = record_sub_agent_report(root, run_id, "task-001", "sub-001", "sub follow-up done")
                self.assertTrue(report["round_summary_requested"])
                self.assertFalse(report["final_requested"])

    def test_sub_agent_skips_completed_task_even_with_old_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Terminal follow-up", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_agent_status(root, run_id, "task-001", "sub-001", "pending")
                set_task_status(root, run_id, "task-001", "completed", 0)
                mark_task_coordination(root, run_id, "task-001", followup_started_at="2026-05-15T00:00:00+00:00")
                append_message(root, run_id, "sub-001", "继续处理你负责的范围", sender="main", task_id="task-001", role="sub")

                with mock.patch("aha_cli.services.chat.run_codex_exec") as run_agent:
                    code, output = self.run_cli(
                        "codex-chat",
                        run_id,
                        "sub-001",
                        "--task-id",
                        "task-001",
                        "--sender",
                        "sub-001",
                        "--from-start",
                        "--once",
                )

                self.assertEqual(code, 0)
                self.assertNotIn("sub-001 -> main", output)
                run_agent.assert_not_called()
                detail = task_snapshot(root, run_id, "task-001")
                sub_agent = next(agent for agent in detail["task"]["agents"] if agent["id"] == "sub-001")
                self.assertEqual(detail["task"]["status"], "completed")
                self.assertEqual(sub_agent["status"], "pending")
                self.assertNotIn("final_summary_requested_at", detail["task"].get("coordination") or {})
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                self.assertTrue(any(event["type"] == "agent_message_skipped" for event in events))

    def test_sub_agent_reports_wait_then_request_main_round_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sub reports", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_task_status(root, run_id, "task-001", "running")

                first = record_sub_agent_report(root, run_id, "task-001", "sub-001", "sub-001 done")
                self.assertTrue(first["handled"])
                self.assertFalse(first.get("round_summary_requested"))
                detail = task_snapshot(root, run_id, "task-001")
                statuses = {agent["id"]: agent["status"] for agent in detail["task"]["agents"]}
                self.assertEqual(statuses["sub-001"], "completed")
                self.assertEqual(statuses["sub-002"], "pending")

                second = record_sub_agent_report(root, run_id, "task-001", "sub-002", "sub-002 done")
                self.assertTrue(second["round_summary_requested"])
                self.assertFalse(second["final_requested"])
                main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                self.assertEqual(main_messages[-1]["sender"], "aha")
                self.assertEqual(main_messages[-1]["coordination"], "subagents_complete")
                self.assertNotIn("result_policy", main_messages[-1])
                self.assertEqual(main_messages[-1]["reply_target"], "browser")
                self.assertIn("round summary", main_messages[-1]["message"])

    def test_coordination_watchdog_recovers_stopped_pending_sub_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Watchdog", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_task_status(root, run_id, "task-001", "running")

                with mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_backend:
                    actions = monitor_task_coordination(root, run_id)

                self.assertIn({"type": "agent_recovered", "task_id": "task-001", "agent_id": "sub-001"}, actions)
                start_backend.assert_called_once()
                self.assertFalse(start_backend.call_args.kwargs["from_start"])
                detail = task_snapshot(root, run_id, "task-001")
                sub_agent = next(agent for agent in detail["task"]["agents"] if agent["id"] == "sub-001")
                self.assertEqual(sub_agent["recovery_attempts"], 1)

    def test_main_does_not_reply_to_sub_agent_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "No loops", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                append_message(root, run_id, "main", "done", sender="sub-001", task_id="task-001", role="sub", from_agent="sub-001", to_agent="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec") as run_agent:
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                run_agent.assert_not_called()
                sub_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "sub-001"), 0)
                self.assertEqual(sub_messages, [])

    def test_sub_agent_skips_messages_after_final_summary_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Terminal sub", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                append_message(root, run_id, "sub-001", "no-op ack", sender="main", task_id="task-001", role="sub", from_agent="main", to_agent="sub-001")

                mark_task_coordination(root, run_id, "task-001", final_summary_requested_at="2026-05-15T00:00:00+00:00")

                with mock.patch("aha_cli.services.chat.run_codex_exec") as run_agent:
                    code, _ = self.run_cli("codex-chat", run_id, "sub-001", "--from-start", "--once")

                self.assertEqual(code, 0)
                run_agent.assert_not_called()

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

    def test_web_status_snapshot_includes_agent_backend_process_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Backend badges", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                mark_task_coordination(root, run_id, "task-001", followup_started_at="2026-05-15T00:00:00+00:00")

                def fake_backend_status(_root: Path, _run_id: str, target: str = "main", task_id: str | None = None) -> dict:
                    return {
                        "target": target,
                        "task_id": task_id,
                        "status": "running" if target == "main" else "stopped",
                        "pid": 1234 if target == "main" else None,
                        "last_reply_at": "2026-05-15T00:00:00+00:00" if target == "main" else None,
                    }

                with mock.patch("aha_cli.web.server.backend_status", side_effect=fake_backend_status):
                    snapshot = web_status_snapshot(root, run_id)

        self.assertEqual(snapshot["tasks"][0]["coordination"]["followup_started_at"], "2026-05-15T00:00:00+00:00")
        agents = {agent["id"]: agent for agent in snapshot["tasks"][0]["agents"]}
        self.assertEqual(snapshot["tasks"][0]["activity_status"], "idle")
        self.assertEqual(agents["main"]["backend_process_status"], "running")
        self.assertEqual(agents["main"]["backend_process_pid"], 1234)
        self.assertEqual(agents["sub-001"]["backend_process_status"], "stopped")
        self.assertIsNone(agents["sub-001"]["backend_process_pid"])

    def test_web_status_snapshot_lite_skips_nonselected_idle_backend_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Lite status", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_agent_status(root, run_id, "task-001", sub["id"], "completed", 0)

                with mock.patch("aha_cli.web.server.backend_status") as backend_status_mock:
                    snapshot = web_status_snapshot(root, run_id, lite=True, selected_task_id="task-other")
                    snapshot_without_selection = web_status_snapshot(root, run_id, lite=True)

        backend_status_mock.assert_not_called()
        task = snapshot["tasks"][0]
        self.assertEqual(task["agent_count"], 2)
        self.assertEqual(task["agents"], [])
        self.assertEqual(snapshot_without_selection["tasks"][0]["agent_count"], 2)
        self.assertEqual(snapshot_without_selection["tasks"][0]["agents"], [])

    def test_web_status_snapshot_recovers_stale_running_agent_after_backend_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Interrupted service restart", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")

                with mock.patch("aha_cli.web.server.backend_status", return_value={"status": "stopped", "pid": None}):
                    snapshot = web_status_snapshot(root, run_id)
                persisted = task_snapshot(root, run_id, "task-001")["task"]
                event_log = event_path(root, run_id).read_text(encoding="utf-8")

        task = snapshot["tasks"][0]
        agent = task["agents"][0]
        self.assertEqual(task["status"], "awaiting_user")
        self.assertEqual(task["current_status"], "awaiting_user")
        self.assertEqual(task["display_status"], "awaiting_user")
        self.assertEqual(task["activity_status"], "idle")
        self.assertEqual(agent["status"], "interrupted")
        self.assertEqual(agent["backend_process_status"], "stopped")

        self.assertEqual(persisted["status"], "awaiting_user")
        self.assertEqual(persisted["agents"][0]["status"], "interrupted")
        self.assertIn("agent_status_recovered", event_log)

    def test_stale_recovery_does_not_reopen_terminal_task_from_old_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final race", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")
                stale_task = status_snapshot(root, run_id)["tasks"][0]
                stale_agent = stale_task["agents"][0]

                set_task_status(root, run_id, "task-001", "completed", 0)
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)
                recovered = recover_stale_running_agent(
                    root,
                    run_id,
                    stale_task,
                    stale_agent,
                    {"status": "stopped", "pid": None},
                )
                persisted = task_snapshot(root, run_id, "task-001")["task"]
                event_log = event_path(root, run_id).read_text(encoding="utf-8")

        self.assertFalse(recovered)
        self.assertEqual(persisted["status"], "completed")
        self.assertEqual(persisted["agents"][0]["status"], "completed")
        self.assertNotIn("agent_status_recovered", event_log)

    def test_recovered_agent_followup_includes_recovery_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recover context", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")

                with mock.patch("aha_cli.web.server.backend_status", return_value={"status": "stopped", "pid": None}):
                    web_status_snapshot(root, run_id)
                recovered = task_snapshot(root, run_id, "task-001")["task"]
                self.assertIn("工作异常中断", recovered["agents"][0]["recovery_context"])

                with (
                    mock.patch("aha_cli.web.server.backend_status", return_value={"status": "stopped", "pid": None}),
                    mock.patch("aha_cli.web.server.start_backend", return_value={"status": "running"}) as start_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "sender": "browser",
                            "message": "继续",
                        },
                    )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                sent_text = messages[-1]["message"]
                consumed = task_snapshot(root, run_id, "task-001")["task"]
                event_log = event_path(root, run_id).read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        start_backend.assert_called_once()
        self.assertIn("工作异常中断", sent_text)
        self.assertIn("用户当前发送的新消息：\n继续", sent_text)
        self.assertEqual(consumed["agents"][0]["recovery_context"], "")
        self.assertIn("agent_recovery_context_consumed", event_log)

    def test_recovered_running_sub_agent_queues_notice_to_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sub recovery while main runs", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")
                set_agent_status(root, run_id, "task-001", sub["id"], "running")
                stale_task = task_snapshot(root, run_id, "task-001")["task"]
                stale_sub = next(agent for agent in stale_task["agents"] if agent["id"] == sub["id"])

                recovered = recover_stale_running_agent(
                    root,
                    run_id,
                    stale_task,
                    stale_sub,
                    {"status": "stopped", "pid": None},
                )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                detail = task_snapshot(root, run_id, "task-001")["task"]
                event_log = event_path(root, run_id).read_text(encoding="utf-8")

        self.assertTrue(recovered)
        self.assertEqual(detail["status"], "running")
        self.assertEqual(next(agent for agent in detail["agents"] if agent["id"] == "main")["status"], "running")
        self.assertEqual(next(agent for agent in detail["agents"] if agent["id"] == sub["id"])["status"], "interrupted")
        self.assertEqual(messages[-1]["sender"], "aha")
        self.assertEqual(messages[-1]["coordination"], "agent_recovery_notice")
        self.assertIn(sub["id"], messages[-1]["message"])
        self.assertIn("不要假设它已经完成", messages[-1]["message"])
        self.assertIn("task_recovery_context_recorded", event_log)

    def test_recovered_idle_sub_agent_adds_context_to_next_main_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sub recovery before follow-up", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "waiting")
                set_agent_status(root, run_id, "task-001", sub["id"], "running")
                stale_task = task_snapshot(root, run_id, "task-001")["task"]
                stale_sub = next(agent for agent in stale_task["agents"] if agent["id"] == sub["id"])

                recover_stale_running_agent(
                    root,
                    run_id,
                    stale_task,
                    stale_sub,
                    {"status": "stopped", "pid": None},
                )
                context_task = task_snapshot(root, run_id, "task-001")["task"]
                main_agent = next(agent for agent in context_task["agents"] if agent["id"] == "main")
                self.assertIn(sub["id"], main_agent["recovery_context"])

                with (
                    mock.patch("aha_cli.web.server.backend_status", return_value={"status": "stopped", "pid": None}),
                    mock.patch("aha_cli.web.server.start_backend", return_value={"status": "running"}),
                ):
                    handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "sender": "browser",
                            "message": "继续",
                        },
                    )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                consumed = task_snapshot(root, run_id, "task-001")["task"]

        self.assertIn(sub["id"], messages[-1]["message"])
        self.assertIn("用户当前发送的新消息：\n继续", messages[-1]["message"])
        self.assertEqual(next(agent for agent in consumed["agents"] if agent["id"] == "main")["recovery_context"], "")

    def test_web_status_snapshot_keeps_outcome_during_active_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Follow-up state", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "completed", 0)
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")

                with mock.patch("aha_cli.web.server.backend_status", return_value={"status": "busy", "pid": 1234}):
                    snapshot = web_status_snapshot(root, run_id)

        task = snapshot["tasks"][0]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["current_status"], "completed")
        self.assertEqual(task["outcome_status"], "completed")
        self.assertEqual(task["display_status"], "completed")
        self.assertEqual(task["activity_status"], "busy")

    def test_web_send_autostarts_stopped_task_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Web follow-up", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)
                append_message(root, run_id, "main", "old already-seen message", sender="browser", task_id="task-001", role="main")

                offset_file = chat_offset_path(run_dir(root, run_id), "main", "task-001")
                self.assertFalse(offset_file.exists())

                with (
                    mock.patch("aha_cli.web.server.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.server.start_backend", return_value={"status": "running", "started": True}) as start_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "sender": "browser",
                            "message": "new follow-up",
                        },
                    )

                self.assertTrue(result["ok"])
                self.assertEqual(result["backend"]["status"], "running")
                start_backend.assert_called_once()
                self.assertFalse(start_backend.call_args.kwargs["from_start"])
                self.assertEqual(start_backend.call_args.kwargs["task_id"], "task-001")
                offset = json.loads(offset_file.read_text(encoding="utf-8"))["offset"]
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), offset)
                self.assertEqual([item["message"] for item in messages], ["new follow-up"])

    def test_web_send_autostarts_claude_task_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude web follow-up", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_agent_runtime(root, run_id, "task-001", "main", backend="claude")
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)

                with (
                    mock.patch("aha_cli.web.server.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.server.start_backend", return_value={"status": "running", "started": True}) as start_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "sender": "browser",
                            "message": "new follow-up",
                        },
                    )

        self.assertTrue(result["ok"])
        start_backend.assert_called_once()
        self.assertEqual(start_backend.call_args.kwargs["backend"], "claude")

    def test_web_send_blocks_completed_task_until_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Locked task", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                complete_task(root, run_id, "task-001", 0)

                payload = {
                    "target": "main",
                    "role": "main",
                    "task_id": "task-001",
                    "from_agent": "browser",
                    "to_agent": "main",
                    "sender": "browser",
                    "message": "should be blocked",
                }
                with self.assertRaisesRegex(ValueError, "use /aha reopen"):
                    handle_send_payload(root, run_id, payload)

                reopened = handle_send_payload(root, run_id, {**payload, "message": "/aha reopen"})
                self.assertTrue(reopened["ok"])
                detail = task_snapshot(root, run_id, "task-001")["task"]
                self.assertEqual(detail["status"], "awaiting_user")
                self.assertEqual(detail["coordination"]["final_summary_requested_at"], "")

                with (
                    mock.patch("aha_cli.web.server.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.server.start_backend", return_value={"status": "running"}) as start_backend,
                ):
                    sent = handle_send_payload(root, run_id, {**payload, "message": "follow-up"})

                self.assertTrue(sent["ok"])
                start_backend.assert_called_once()

    def test_aha_interrupt_stops_backend_and_marks_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Interrupt turn", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")
                append_message(root, run_id, "main", "in-flight", sender="browser", task_id="task-001", role="main")
                offset_file = chat_offset_path(run_dir(root, run_id), "main", "task-001")
                save_chat_offset(offset_file, 0)

                with (
                    mock.patch("aha_cli.web.server.backend_status", return_value={"status": "busy", "pid": 1234}),
                    mock.patch("aha_cli.web.server.stop_backend", return_value={"status": "stopped", "pid": None, "target": "main"}) as stop_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "sender": "browser",
                            "message": "/aha interrupt",
                        },
                    )

                self.assertTrue(result["ok"])
                self.assertTrue(result["interrupt"]["interrupted"])
                stop_backend.assert_called_once()
                detail = task_snapshot(root, run_id, "task-001")["task"]
                self.assertEqual(detail["status"], "awaiting_user")
                self.assertEqual(detail["agents"][0]["status"], "interrupted")
                self.assertIsNotNone(detail["agents"][0]["finished_at"])
                self.assertEqual(json.loads(offset_file.read_text(encoding="utf-8"))["offset"], inbox_path(root, run_id, "main").stat().st_size)

    def test_aha_interrupt_ignores_idle_backend_listener(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Idle listener", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)

                with (
                    mock.patch("aha_cli.web.server.backend_status", return_value={"status": "running", "pid": 1234}),
                    mock.patch("aha_cli.web.server.stop_backend") as stop_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "sender": "browser",
                            "message": "/aha interrupt",
                        },
                    )

                self.assertTrue(result["ok"])
                self.assertFalse(result["interrupt"]["interrupted"])
                self.assertEqual(result["interrupt"]["reason"], "not_busy")
                stop_backend.assert_not_called()
                detail = task_snapshot(root, run_id, "task-001")["task"]
                self.assertEqual(detail["status"], "awaiting_user")
                self.assertEqual(detail["agents"][0]["status"], "completed")

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

    def test_chat_prompt_uses_recent_events_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recent prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                for index in range(30):
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": f"prompt-event-{index}"})

                prompt = chat_prompt(root, run_id, "main", {"sender": "browser", "message": "status"}, "")

        self.assertIn("prompt-event-29", prompt)
        self.assertNotIn("prompt-event-0", prompt)

    def test_chat_prompt_scopes_status_and_events_to_current_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Prompt scoping", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                code, _ = self.run_cli("task", "add", run_id, "Foreign verbose task title that should stay out", "--no-dispatch")
                self.assertEqual(code, 0)
                code, _ = self.run_cli("task", "add", run_id, "Current compact prompt title", "--no-dispatch")
                self.assertEqual(code, 0)
                append_event(root, run_id, "agent_message", {"task_id": "task-002", "target": "main", "text": "foreign-event"})
                append_event(root, run_id, "agent_message", {"task_id": "task-003", "target": "main", "text": "current-event"})

                prompt = chat_prompt(
                    root,
                    run_id,
                    "main",
                    {"sender": "browser", "message": "status", "task_id": "task-003", "role": "main"},
                    "",
                )

        self.assertIn("Current compact prompt title", prompt)
        self.assertIn("current-event", prompt)
        self.assertIn("task_counts", prompt)
        self.assertNotIn("Foreign verbose task title that should stay out", prompt)
        self.assertNotIn("foreign-event", prompt)

    def test_chat_prompt_uses_delta_for_existing_sticky_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sticky prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = "backend-session-1"
                session_file.write_text(json.dumps(session), encoding="utf-8")
                append_event(
                    root,
                    run_id,
                    "agent_message",
                    {"task_id": "task-001", "target": "main", "text": "already-in-backend-session"},
                )
                append_event(root, run_id, "agent_finished", {"task_id": "task-001", "target": "main", "exit_code": 0})
                append_event(root, run_id, "task_hidden", {"task_id": "task-001"})

                prompt, metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    {
                        "sender": "browser",
                        "message": "next request",
                        "task_id": "task-001",
                        "role": "main",
                        "ts": "2026-01-01T00:00:00+00:00",
                    },
                    "prefix",
                )

        self.assertEqual(metrics["prompt_mode"], "sticky_delta")
        self.assertIn("Current task delta:", prompt)
        self.assertIn("backend-session-1", prompt)
        self.assertIn("next request", prompt)
        self.assertIn("task_hidden", prompt)
        self.assertNotIn("Ownership and routing policy", prompt)
        self.assertNotIn("already-in-backend-session", prompt)
        self.assertIn("sticky_context", metrics["components"])
        self.assertIn("delta_status", metrics["components"])
        self.assertNotIn("task_context", metrics["components"])

    def test_finalization_prompt_omits_recent_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(
                    root,
                    run_id,
                    "message",
                    {
                        "task_id": "task-001",
                        "sender": "browser",
                        "target": "main",
                        "message": "NOISY_RECENT_EVENT" * 1000,
                    },
                )

                prompt, metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    {
                        "sender": "aha",
                        "message": finalization_prompt("task-001", "Final prompt", []),
                        "task_id": "task-001",
                        "role": "main",
                        "result_policy": "finalize",
                        "ts": "2026-01-01T00:00:00+00:00",
                    },
                    "prefix",
                )

        self.assertTrue(metrics["is_finalization"])
        self.assertEqual(metrics["event_limit"], 0)
        self.assertIn("omitted for finalization", prompt)
        self.assertNotIn("NOISY_RECENT_EVENT", prompt)
        self.assertLess(metrics["components"]["recent_events"]["chars"], 120)

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
