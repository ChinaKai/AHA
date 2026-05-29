from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest import mock

from aha_cli.backends.claude import build_claude_exec_command, claude_config_env, claude_permission_mode, handle_claude_event
from aha_cli.backends.codex import (
    build_codex_exec_command,
    codex_config_env,
    codex_config_overrides,
    handle_codex_event,
    is_context_overflow_message,
    run_codex_exec,
)
from aha_cli.backends.registry import CODEX_DEFAULT_MODEL
from aha_cli.cli import append_message, main
from aha_cli.services.chat import chat_offset_path, chat_prompt, save_chat_offset
from aha_cli.services.session_compact import compact_reset_backend_session
from aha_cli.store.filesystem import append_jsonl, inbox_path, iter_jsonl_from, read_json, run_dir
from aha_cli.web.server import backend_session_jsonl_info
from tests.helpers import fetch_ui_response, json_response_body


class BackendRunnerSessionTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_claude_config_env_uses_active_named_group(self) -> None:
        env = claude_config_env(
            {
                "env_active": "prod",
                "env": [
                    {
                        "name": "dev",
                        "ANTHROPIC_API_KEY": "dev-key",
                        "ANTHROPIC_BASE_URL": "https://dev.example",
                    },
                    {
                        "name": "prod",
                        "ANTHROPIC_API_KEY": "prod-key",
                        "ANTHROPIC_BASE_URL": "https://prod.example",
                        "ANTHROPIC_MODEL": "claude-prod",
                    },
                ],
            }
        )

        self.assertEqual(env["ANTHROPIC_API_KEY"], "prod-key")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://prod.example")
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-prod")

    def test_claude_config_env_can_disable_env_groups_for_official_claude(self) -> None:
        env = claude_config_env(
            {
                "env_active": None,
                "env": [{"name": "prod", "ANTHROPIC_API_KEY": "prod-key", "ANTHROPIC_MODEL": "claude-prod"}],
            }
        )

        self.assertEqual(env, {})

    def test_claude_config_env_keeps_legacy_first_group_without_active_field(self) -> None:
        env = claude_config_env(
            {"env": [{"name": "prod", "ANTHROPIC_API_KEY": "prod-key", "ANTHROPIC_MODEL": "claude-prod"}]}
        )

        self.assertEqual(env["ANTHROPIC_API_KEY"], "prod-key")
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-prod")

    def test_claude_config_env_keeps_legacy_dict_shape(self) -> None:
        env = claude_config_env({"env": {"api_key": "test-key", "base_url": "https://claude.test"}})

        self.assertEqual(env["ANTHROPIC_API_KEY"], "test-key")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://claude.test")

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

    def test_codex_exec_uses_env_group_model_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.md"
            output.write_text("done", encoding="utf-8")
            session: dict = {}

            class FakeProcess:
                stdin = io.StringIO()
                stdout = io.StringIO("")

                def wait(self) -> int:
                    return 0

            with (
                mock.patch.dict("os.environ", {}, clear=True),
                mock.patch("aha_cli.backends.codex.subprocess.Popen", return_value=FakeProcess()) as popen,
            ):
                code, reply, updated_session = run_codex_exec(
                    "hello",
                    cwd=Path(tmp),
                    output_file=output,
                    model="env:openai",
                    session=session,
                    codex_config={
                        "env": [
                            {
                                "name": "openai",
                                "OPENAI_BASE_URL": "https://openai.test/v1",
                                "OPENAI_MODEL": "kimi-k2.6",
                                "OPENAI_API_KEY": "openai-key",
                                "CODEX_WIRE_API": "chat",
                                "CODEX_ENV_KEY": "MINIMAX_API_KEY",
                            }
                        ]
                    },
                )

            self.assertEqual(code, 0)
            self.assertEqual(reply, "done")
            self.assertIs(updated_session, session)
            self.assertEqual(session["requested_model"], "env:openai")
            self.assertEqual(session["resolved_model"], "kimi-k2.6")
            command = popen.call_args.args[0]
            env = popen.call_args.kwargs["env"]
            self.assertIn("-m", command)
            self.assertEqual(command[command.index("-m") + 1], "kimi-k2.6")
            joined_command = " ".join(command)
            self.assertIn('model_provider="aha_codex_env_', joined_command)
            self.assertIn("model_providers.", joined_command)
            self.assertIn('wire_api="responses"', joined_command)
            self.assertIn("requires_openai_auth=false", joined_command)
            self.assertIn('env_key="MINIMAX_API_KEY"', joined_command)
            self.assertNotIn("OPENAI_BASE_URL", env)
            self.assertEqual(env["MINIMAX_API_KEY"], "openai-key")
            self.assertEqual(codex_config_env({"env_active": None, "env": [{"name": "openai", "OPENAI_API_KEY": "x"}]}), {})

    def test_codex_env_group_generates_provider_overrides(self) -> None:
        overrides = codex_config_overrides(
            {
                "env_active": "work",
                "env": [
                    {
                        "name": "work",
                        "OPENAI_BASE_URL": "https://openai.test/v1",
                        "OPENAI_MODEL": "model-x",
                        "OPENAI_API_KEY": "key",
                        "CODEX_WIRE_API": "responses",
                        "CODEX_ENV_KEY": "OPENAI_API_KEY",
                    }
                ],
            }
        )

        joined = " ".join(overrides)
        self.assertIn("model_provider=", joined)
        self.assertIn('model_providers.aha_codex_env_', joined)
        self.assertIn('base_url="https://openai.test/v1"', joined)
        self.assertIn('env_key="OPENAI_API_KEY"', joined)
        self.assertIn('wire_api="responses"', joined)
        self.assertIn("requires_openai_auth=false", joined)

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

    def test_codex_event_ignores_non_object_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            common_kwargs = {
                "events_file": events,
                "run_id": "run",
                "task_id": "task-001",
                "source": "codex-chat",
                "target": "main",
            }
            handle_codex_event(json.dumps("plain string"), **common_kwargs)
            handle_codex_event(json.dumps(["unexpected"]), **common_kwargs)
            handle_codex_event(json.dumps({"type": "item.completed", "item": "not an object"}), **common_kwargs)
            handle_codex_event(json.dumps({"type": "turn.completed", "usage": "not an object"}), **common_kwargs)
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([row["type"] for row in rows], ["agent_usage"])
        self.assertEqual(rows[0]["data"]["usage"], {})

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
        self.assertNotIn("--sandbox", cmd)
        self.assertIn("--permission-mode", cmd)
        self.assertIn("acceptEdits", cmd)
        self.assertIn("--disallowedTools", cmd)
        disallowed_tools = cmd[cmd.index("--disallowedTools") + 1].split(",")
        self.assertEqual(disallowed_tools[:3], ["Agent", "Task", "TaskCreate"])
        self.assertIn("AskUserQuestion", disallowed_tools)
        self.assertIn("ExitPlanMode", disallowed_tools)
        self.assertIn("--resume", cmd)
        self.assertIn("session-123", cmd)

    def test_claude_plan_command_adds_global_readonly_dir(self) -> None:
        cmd = build_claude_exec_command(
            claude_bin="claude",
            model=None,
            permission_mode="plan",
            session_id=None,
        )

        self.assertNotIn("--sandbox", cmd)
        self.assertIn("--add-dir", cmd)
        self.assertEqual(cmd[cmd.index("--add-dir") + 1], "/")

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

    def test_compact_reset_archives_backend_session_and_keeps_prompt_lean(self) -> None:
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
                offset_file = chat_offset_path(run_dir(root, run_id), "main", "task-001")
                offset = read_json(offset_file)
                inbox_size = inbox_path(root, run_id, "main").stat().st_size
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
        self.assertEqual(offset["offset"], inbox_size)
        self.assertNotIn("Backend compact summary from previous session", prompt)
        self.assertIn("previous request", prompt)
        self.assertIn("Recent conversation chains", prompt)
        self.assertIn("Intent priority policy:", prompt)

    def test_compact_reset_preserves_existing_task_scoped_chat_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Compact reset offset", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_id = "compact-reset-offset-session-1"
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = session_id
                session_file.write_text(json.dumps(session), encoding="utf-8")

                append_message(root, run_id, "main", "already processed", sender="browser", task_id="task-001", role="main")
                inbox = inbox_path(root, run_id, "main")
                preserved_offset = inbox.stat().st_size
                offset_file = chat_offset_path(run_dir(root, run_id), "main", "task-001")
                save_chat_offset(offset_file, preserved_offset)
                append_message(root, run_id, "main", "queued after offset", sender="browser", task_id="task-001", role="main")

                compact_reset_backend_session(root, run_id, "task-001", "main", reason="manual")

                offset = read_json(offset_file)
                queued, _ = iter_jsonl_from(inbox, preserved_offset)

        self.assertEqual(offset["offset"], preserved_offset)
        self.assertEqual([item["message"] for item in queued], ["queued after offset"])

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
