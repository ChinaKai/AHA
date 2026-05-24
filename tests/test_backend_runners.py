from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest import mock

from aha_cli.backends.claude import build_claude_exec_command, claude_permission_mode, handle_claude_event
from aha_cli.backends.codex import build_codex_exec_command, handle_codex_event, is_context_overflow_message, run_codex_exec
from aha_cli.backends.registry import CODEX_DEFAULT_MODEL
from aha_cli.cli import append_message, main
from aha_cli.services.chat import chat_prompt
from aha_cli.services.session_compact import compact_reset_backend_session
from aha_cli.store.filesystem import append_jsonl, read_json, run_dir
from aha_cli.web.server import backend_session_jsonl_info
from tests.helpers import fetch_ui_response, json_response_body


class BackendRunnerSessionTests(unittest.TestCase):
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
        self.assertEqual(
            cmd[:11],
            [
                "codex",
                "-m",
                CODEX_DEFAULT_MODEL,
                "-a",
                "never",
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "workspace-write",
                "-C",
                "/tmp/project",
            ],
        )
        self.assertIn("resume", cmd)
        self.assertIn("session-123", cmd)

    def test_codex_exec_records_resolved_default_model_in_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.md"
            output.write_text("done", encoding="utf-8")
            session: dict = {}

            class FakeProcess:
                stdin = io.StringIO()
                stdout = io.StringIO("")

                def wait(self) -> int:
                    return 0

            with mock.patch("aha_cli.backends.codex.subprocess.Popen", return_value=FakeProcess()) as popen:
                code, reply, updated_session = run_codex_exec(
                    "hello",
                    cwd=Path(tmp),
                    output_file=output,
                    model=None,
                    session=session,
                )

            self.assertEqual(code, 0)
            self.assertEqual(reply, "done")
            self.assertIs(updated_session, session)
            self.assertIsNone(session["requested_model"])
            self.assertEqual(session["resolved_model"], CODEX_DEFAULT_MODEL)
            self.assertEqual(session["model"], CODEX_DEFAULT_MODEL)
            command = popen.call_args.args[0]
            self.assertEqual(command[:3], ["codex", "-m", CODEX_DEFAULT_MODEL])

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
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "pwd",
                            "status": "completed",
                            "exit_code": 0,
                            "aggregated_output": "x" * 1300,
                        },
                    }
                ),
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
            with mock.patch(
                "aha_cli.backends.codex.subprocess.Popen",
                side_effect=FileNotFoundError(2, "No such file or directory", "codex"),
            ):
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
        self.assertEqual(
            [row["type"] for row in rows],
            ["agent_thread", "agent_message", "agent_command_started", "agent_command_finished", "agent_usage"],
        )
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
                self.assertIn(f"--model {CODEX_DEFAULT_MODEL}", output)

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

            with mock.patch("aha_cli.web.session_debug.Path.home", return_value=home):
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

            with mock.patch("aha_cli.web.session_debug.Path.home", return_value=home):
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
        self.assertIn(
            "Intent priority: current user message > task journal / active intent > compact summary / recent messages > original task description",
            prompt,
        )
        self.assertIn("original_request:", prompt)
        self.assertIn("Completed or superseded original requirements should not be restarted", prompt)
        self.assertIn("Explicit exclusions from recent user messages override older requirements", prompt)
        self.assertIn("Next action should come from the latest active user intent", prompt)

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
