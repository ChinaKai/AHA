from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

from aha_cli.backends.claude import (
    apply_claude_environment,
    build_claude_exec_command,
    claude_cli_model,
    claude_config_env,
    claude_context_window,
    claude_config_for_model,
    claude_permission_mode,
    handle_claude_event,
    _is_claude_turn_result,
    run_claude_exec,
)
from aha_cli.backends.codex import (
    build_codex_exec_command,
    codex_callback_events,
    codex_config_for_model,
    codex_config_overrides,
    codex_config_with_provider_override,
    handle_codex_event,
    is_context_overflow_message,
    run_codex_exec,
)
from aha_cli.backends.registry import CODEX_DEFAULT_MODEL
from aha_cli.cli import append_message, main
from aha_cli.services.chat import chat_offset_path, chat_prompt, save_chat_offset
from aha_cli.services.session_compact import compact_reset_backend_session
from aha_cli.store.filesystem import append_event, append_jsonl, inbox_path, iter_jsonl_from, read_json, run_dir
from aha_cli.store.sessions import FORCE_FULL_PROMPT_NEXT_TURN_KEY
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
                        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000",
                    },
                ],
            }
        )

        self.assertEqual(env["ANTHROPIC_API_KEY"], "prod-key")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://prod.example")
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-prod")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "claude-prod")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "claude-prod")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "claude-prod")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "claude-prod")
        self.assertEqual(env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"], "1")
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "200000")
        self.assertEqual(env["API_TIMEOUT_MS"], "600000")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
        self.assertNotIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", env)

    def test_claude_environment_supports_role_models_auth_token_and_runtime_policy(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "inherited-key",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_CUSTOM_HEADERS": "x-test: inherited",
        }
        apply_claude_environment(
            env,
            {
                "env_active": "gateway",
                "env": [
                    {
                        "name": "gateway",
                        "ANTHROPIC_AUTH_TOKEN": "gateway-token",
                        "ANTHROPIC_MODEL": "claude-sonnet-custom",
                        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-custom",
                        "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-custom",
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-custom",
                        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "256000",
                    }
                ],
            },
        )

        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "gateway-token")
        self.assertEqual(env["ANTHROPIC_DEFAULT_FABLE_MODEL"], "claude-sonnet-custom")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "claude-opus-custom")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "claude-haiku-custom")
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", env)
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "256000")
        # 256K 窗口不再自动注入 DISABLE_COMPACT，让 Claude CLI 在接近窗口时自动压缩。
        self.assertNotIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", env)
        self.assertNotIn("DISABLE_COMPACT", env)
        self.assertEqual(env["API_TIMEOUT_MS"], "600000")
        self.assertNotIn("CLAUDE_CODE_USE_VERTEX", env)
        self.assertNotIn("ANTHROPIC_CUSTOM_HEADERS", env)

    def test_claude_gateway_infers_one_million_context_without_forcing_manual_compaction(self) -> None:
        config = {
            "env": [
                {
                    "name": "deepseek",
                    "ANTHROPIC_MODEL": "claude-deepseek-v4-flash[1m]",
                    "ANTHROPIC_AUTH_TOKEN": "gateway-token",
                }
            ]
        }

        env = claude_config_env(config)

        self.assertEqual(claude_context_window(config), 1_000_000)
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "1000000")
        self.assertNotIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", env)
        self.assertNotIn("DISABLE_COMPACT", env)

    def test_claude_explicit_disable_compact_still_forces_manual_compaction(self) -> None:
        config = {
            "env": [
                {
                    "name": "manual-compact",
                    "ANTHROPIC_MODEL": "claude-gpt-5.6-sol",
                    "ANTHROPIC_AUTH_TOKEN": "gateway-token",
                    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "256000",
                    "DISABLE_COMPACT": "1",
                }
            ]
        }

        env = claude_config_env(config)

        # 用户显式配置 DISABLE_COMPACT 时仍生效，并回填窗口上限。
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "256000")
        self.assertEqual(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "256000")
        self.assertEqual(env["DISABLE_COMPACT"], "1")

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
        env = claude_config_env(
            {
                "env": {
                    "api_key": "test-key",
                    "base_url": "https://claude.test",
                    "model": "claude-custom",
                    "small_fast_model": "claude-fast",
                    "context_window": "200000",
                }
            }
        )

        self.assertEqual(env["ANTHROPIC_API_KEY"], "test-key")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://claude.test")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "claude-fast")
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "200000")

    def test_claude_official_model_disables_env_group_injection(self) -> None:
        base_config = {
            "env_active": "work",
            "env": [
                {
                    "name": "work",
                    "ANTHROPIC_API_KEY": "work-key",
                    "ANTHROPIC_BASE_URL": "https://claude.test",
                    "ANTHROPIC_MODEL": "kimi-k2.6",
                }
            ],
        }

        config = claude_config_for_model(base_config, "claude-sonnet-4-6")

        self.assertEqual(claude_config_env(config), {})
        self.assertEqual(claude_cli_model("claude-sonnet-4-6", base_config), "claude-sonnet-4-6")

    def test_claude_env_alias_uses_env_group_without_cli_model(self) -> None:
        base_config = {
            "env": [
                {
                    "name": "kimi-k2.6",
                    "ANTHROPIC_API_KEY": "kimi-key",
                    "ANTHROPIC_BASE_URL": "https://kimi.test",
                    "ANTHROPIC_MODEL": "kimi-k2.6",
                }
            ],
        }

        config = claude_config_for_model(base_config, "kimi")

        self.assertIsNone(claude_cli_model("kimi", base_config))
        self.assertEqual(config["env_active"], "kimi-k2.6")
        self.assertEqual(claude_config_env(config)["ANTHROPIC_MODEL"], "kimi-k2.6")

    def test_claude_exec_allows_official_model_without_env_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.md"
            events = Path(tmp) / "events.jsonl"
            base_config = {
                "env_active": "work",
                "env": [
                    {
                        "name": "work",
                        "ANTHROPIC_API_KEY": "work-key",
                        "ANTHROPIC_BASE_URL": "https://claude.test",
                        "ANTHROPIC_MODEL": "kimi-k2.6",
                    }
                ],
            }
            claude_config = claude_config_for_model(base_config, "claude-sonnet-4-6")

            class FakeProcess:
                stdin = io.StringIO()
                stdout = io.StringIO(json.dumps({"type": "result", "result": "done", "session_id": "session-123"}) + "\n")

                def wait(self) -> int:
                    return 0

            with (
                mock.patch.dict("os.environ", {}, clear=True),
                mock.patch("aha_cli.backends.claude.subprocess.Popen", return_value=FakeProcess()) as popen,
            ):
                code, reply, _ = run_claude_exec(
                    "hello",
                    cwd=Path(tmp),
                    output_file=output,
                    model=claude_cli_model("claude-sonnet-4-6", base_config),
                    events_file=events,
                    run_id="run-001",
                    task_id="task-001",
                    source="claude-chat",
                    target="main",
                    claude_config=claude_config,
                )

        command = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        self.assertEqual(code, 0)
        self.assertEqual(reply, "done")
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "claude-sonnet-4-6")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ANTHROPIC_MODEL", env)

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

    def test_codex_config_overrides_include_reasoning_effort(self) -> None:
        overrides = codex_config_overrides({"reasoning_effort": "xhigh"})

        self.assertEqual(overrides, ["-c", 'model_reasoning_effort="xhigh"'])

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

    def test_codex_exec_uses_env_group_provider_override(self) -> None:
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
            self.assertIn('model_provider="aha_codex_openai_', joined_command)
            self.assertIn('model_providers.aha_codex_openai_', joined_command)
            self.assertIn('base_url="https://openai.test/v1"', joined_command)
            self.assertIn('wire_api="chat"', joined_command)
            self.assertIn('env_key="MINIMAX_API_KEY"', joined_command)
            self.assertEqual(env["MINIMAX_API_KEY"], "openai-key")
            self.assertEqual(env["OPENAI_API_KEY"], "openai-key")

    def test_codex_env_group_accepts_claude_style_provider_config(self) -> None:
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
                code, reply, _ = run_codex_exec(
                    "hello",
                    cwd=Path(tmp),
                    output_file=output,
                    model="env:custom-k2.6",
                    session=session,
                    codex_config={
                        "env": [
                            {
                                "name": "custom-k2.6",
                                "ANTHROPIC_BASE_URL": "https://api.example.test/v1",
                                "ANTHROPIC_MODEL": "custom-k2.6",
                                "ANTHROPIC_API_KEY": "custom-key",
                            },
                            {
                                "name": "MiniMax-M3",
                                "ANTHROPIC_BASE_URL": "https://api.minimaxi.com/anthropic",
                                "ANTHROPIC_MODEL": "MiniMax-M3",
                                "ANTHROPIC_API_KEY": "minimax-key",
                            },
                        ]
                    },
                )

        self.assertEqual(code, 0)
        self.assertEqual(reply, "done")
        self.assertEqual(session["resolved_model"], "custom-k2.6")
        command = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        joined_command = " ".join(command)
        self.assertIn('base_url="https://api.example.test/v1"', joined_command)
        self.assertIn('wire_api="responses"', joined_command)
        self.assertEqual(env["OPENAI_API_KEY"], "custom-key")

    def test_codex_env_group_rewrites_minimax_anthropic_url_for_responses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.md"
            output.write_text("done", encoding="utf-8")

            class FakeProcess:
                stdin = io.StringIO()
                stdout = io.StringIO("")

                def wait(self) -> int:
                    return 0

            with (
                mock.patch.dict("os.environ", {}, clear=True),
                mock.patch("aha_cli.backends.codex.subprocess.Popen", return_value=FakeProcess()) as popen,
            ):
                code, reply, _ = run_codex_exec(
                    "hello",
                    cwd=Path(tmp),
                    output_file=output,
                    model="env:MiniMax-M2.7-highspeed",
                    session={},
                    codex_config={
                        "env": [
                            {
                                "name": "MiniMax-M2.7-highspeed",
                                "OPENAI_BASE_URL": "https://api.minimaxi.com/anthropic",
                                "OPENAI_MODEL": "MiniMax-M2.7-highspeed",
                                "OPENAI_API_KEY": "minimax-key",
                            }
                        ]
                    },
                )

        self.assertEqual(code, 0)
        self.assertEqual(reply, "done")
        command = popen.call_args.args[0]
        joined_command = " ".join(command)
        self.assertIn('base_url="https://api.minimaxi.com/v1"', joined_command)
        self.assertIn('wire_api="responses"', joined_command)

    def test_codex_env_group_marks_kimi_for_litellm_responses_bridge(self) -> None:
        cfg = codex_config_for_model(
            {
                "env": [
                    {
                        "name": "kimi-k2.6",
                        "OPENAI_BASE_URL": "https://api.kimi.com/coding/",
                        "OPENAI_MODEL": "kimi-k2.6",
                        "OPENAI_API_KEY": "kimi-key",
                    }
                ]
            },
            "env:kimi-k2.6",
        )

        provider = cfg["_provider_override"]
        bridge = provider["_litellm_responses_bridge"]
        self.assertEqual(provider["wire_api"], "responses")
        self.assertEqual(provider["base_url"], "https://api.kimi.com/coding/v1")
        self.assertEqual(bridge["upstream_base_url"], "https://api.kimi.com/coding/v1")
        self.assertEqual(bridge["upstream_model"], "kimi-for-coding")
        self.assertEqual(bridge["client_model"], "kimi-k2.6")

    def test_codex_exec_routes_kimi_env_group_through_local_litellm_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.md"
            output.write_text("done", encoding="utf-8")

            class FakeProcess:
                stdin = io.StringIO()
                stdout = io.StringIO("")

                def wait(self) -> int:
                    return 0

            class FakeBridge:
                base_url = "http://127.0.0.1:19001/v1"

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            with (
                mock.patch.dict("os.environ", {}, clear=True),
                mock.patch("aha_cli.backends.codex.start_litellm_responses_bridge", return_value=FakeBridge()) as bridge,
                mock.patch("aha_cli.backends.codex.subprocess.Popen", return_value=FakeProcess()) as popen,
            ):
                code, reply, _ = run_codex_exec(
                    "hello",
                    cwd=Path(tmp),
                    output_file=output,
                    model="env:kimi-k2.6",
                    session={},
                    codex_config={
                        "env": [
                            {
                                "name": "kimi-k2.6",
                                "ANTHROPIC_BASE_URL": "https://api.kimi.com/coding/",
                                "ANTHROPIC_MODEL": "kimi-k2.6",
                                "ANTHROPIC_API_KEY": "kimi-key",
                            }
                        ]
                    },
                )

        self.assertEqual(code, 0)
        self.assertEqual(reply, "done")
        bridge.assert_called_once()
        command = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        joined_command = " ".join(command)
        self.assertIn("-m", command)
        self.assertEqual(command[command.index("-m") + 1], "kimi-k2.6")
        self.assertIn('base_url="http://127.0.0.1:19001/v1"', joined_command)
        self.assertIn('wire_api="responses"', joined_command)
        self.assertEqual(env["OPENAI_API_KEY"], "kimi-key")

    def test_codex_provider_override_generates_config_args(self) -> None:
        cfg = codex_config_with_provider_override(
            {"model": "gpt-5.5", "env": [{"name": "ignored"}]},
            provider_id="aha_headroom",
            name="AHA Headroom",
            base_url="http://127.0.0.1:8787/v1",
        )

        joined = " ".join(codex_config_overrides(cfg))

        self.assertIn("env", cfg)
        self.assertIn('model_provider="aha_headroom"', joined)
        self.assertIn('model_providers.aha_headroom.base_url="http://127.0.0.1:8787/v1"', joined)
        self.assertIn('model_providers.aha_headroom.wire_api="responses"', joined)
        self.assertIn("model_providers.aha_headroom.requires_openai_auth=false", joined)

    def test_codex_exec_adds_common_user_bin_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_home = tmp_path / "home"
            nvm_bin = fake_home / ".nvm" / "versions" / "node" / "v24.15.0" / "bin"
            local_bin = fake_home / ".local" / "bin"
            nvm_bin.mkdir(parents=True)
            local_bin.mkdir(parents=True)
            output = tmp_path / "reply.md"
            output.write_text("done", encoding="utf-8")

            class FakeProcess:
                stdin = io.StringIO()
                stdout = io.StringIO("")

                def wait(self) -> int:
                    return 0

            with (
                mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True),
                mock.patch("aha_cli.services.backend_paths.Path.home", return_value=fake_home),
                mock.patch("aha_cli.backends.codex.subprocess.Popen", return_value=FakeProcess()) as popen,
            ):
                code, reply, _ = run_codex_exec("hello", cwd=tmp_path, output_file=output)

            self.assertEqual(code, 0)
            self.assertEqual(reply, "done")
            parts = popen.call_args.kwargs["env"]["PATH"].split(os.pathsep)
            self.assertLess(parts.index(str(local_bin)), parts.index("/usr/bin"))
            self.assertLess(parts.index(str(nvm_bin)), parts.index("/usr/bin"))

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
            artifact_text = (events.parent / rows[1]["data"]["output_ref"]["path"]).read_text(encoding="utf-8")

        self.assertEqual(rows[0]["type"], "agent_command_started")
        self.assertEqual(rows[1]["type"], "agent_command_finished")
        self.assertEqual(rows[1]["data"]["command"], "pwd")
        self.assertEqual(rows[1]["data"]["target"], "sub-001")
        self.assertEqual(len(rows[1]["data"]["output_tail"]), 1200)
        self.assertEqual(rows[1]["data"]["output_chars"], 1300)
        output_ref = rows[1]["data"]["output_ref"]
        self.assertEqual(output_ref["kind"], "command_output")
        self.assertEqual(output_ref["chars"], 1300)
        self.assertTrue(output_ref["path"].startswith("tasks/task-001/artifacts/command-output/sub-001-"))
        self.assertEqual(artifact_text, "x" * 1300)

    def test_codex_callback_events_include_modern_json_stream(self) -> None:
        started = codex_callback_events(json.dumps({
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": "{\"cmd\":\"sed -n '1,20p' pyproject.toml\"}",
            },
        }))
        usage = codex_callback_events(json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"total_tokens": 42},
                    "last_token_usage": {"total_tokens": 7},
                },
            },
        }))

        self.assertEqual(started[0][0], "agent_command_started")
        self.assertEqual(started[0][1]["tool_name"], "exec_command")
        self.assertIn("pyproject.toml", started[0][1]["command"])
        self.assertEqual(usage[0][0], "agent_usage")
        self.assertEqual(usage[0][1]["usage"]["total_token_usage"]["total_tokens"], 42)

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
            handle_codex_event(
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="codex-chat",
                target="main",
                session=session,
            )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(session["backend_session_id"], "new-codex-session")
        self.assertEqual(session["status"], "active")
        self.assertEqual(rows[-1]["data"]["backend_session_id"], "new-codex-session")

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

    def test_codex_auto_context_compact_marks_next_prompt_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            session: dict = {"backend_session_id": "codex-session-1"}
            handle_codex_event(
                json.dumps({"type": "thread.compacted", "reason": "context_window_auto_compact"}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="codex-chat",
                target="main",
                session=session,
            )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([row["type"] for row in rows], ["backend_auto_context_compact"])
        self.assertEqual(session[FORCE_FULL_PROMPT_NEXT_TURN_KEY]["reason"], "backend_auto_context_compact")
        self.assertEqual(session[FORCE_FULL_PROMPT_NEXT_TURN_KEY]["raw_type"], "thread.compacted")

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

    def test_codex_exec_finishes_after_native_completion_when_stdout_stays_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.md"
            events = Path(tmp) / "events.jsonl"
            message = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}) + "\n"
            completion = json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}) + "\n"

            class BlockingStdout:
                def __init__(self) -> None:
                    self.lines = iter([message, completion])
                    self.release = threading.Event()

                def __iter__(self) -> "BlockingStdout":
                    return self

                def __next__(self) -> str:
                    try:
                        return next(self.lines)
                    except StopIteration:
                        pass
                    self.release.wait(5)
                    raise StopIteration

            class FakeProcess:
                pid = 1234

                def __init__(self) -> None:
                    self.stdin = io.StringIO()
                    self.stdout = BlockingStdout()

                def poll(self) -> int:
                    return 0

                def terminate(self) -> None:
                    raise AssertionError("an exited backend parent must not terminate its detached child")

            process = FakeProcess()

            with mock.patch("aha_cli.backends.codex.subprocess.Popen", return_value=process):
                code, reply, _ = run_codex_exec(
                    "hello",
                    cwd=Path(tmp),
                    output_file=output,
                    events_file=events,
                    run_id="run-001",
                    task_id="task-001",
                    source="codex-chat",
                    target="main",
                    completion_grace_seconds=0.01,
                )
            process.stdout.release.set()
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
            output_text = output.read_text(encoding="utf-8")

        self.assertEqual(code, 0)
        self.assertEqual(reply, "done")
        self.assertEqual(output_text, "done")
        self.assertEqual([row["type"] for row in rows], ["agent_message", "agent_usage", "backend_completion_grace_exceeded"])
        self.assertEqual(rows[-1]["data"]["backend"], "codex")
        self.assertEqual(rows[-1]["data"]["process_exit_code"], 0)

    def test_claude_exec_finishes_after_native_completion_when_stdout_stays_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.md"
            events = Path(tmp) / "events.jsonl"
            completion = json.dumps({"type": "result", "result": "done", "usage": {"output_tokens": 1}}) + "\n"

            class BlockingStdout:
                def __init__(self) -> None:
                    self.sent = False
                    self.release = threading.Event()

                def __iter__(self) -> "BlockingStdout":
                    return self

                def __next__(self) -> str:
                    if not self.sent:
                        self.sent = True
                        return completion
                    self.release.wait(5)
                    raise StopIteration

            class FakeProcess:
                pid = 5678

                def __init__(self) -> None:
                    self.stdin = io.StringIO()
                    self.stdout = BlockingStdout()

            process = FakeProcess()

            def terminate(_process) -> int:
                process.stdout.release.set()
                return 137

            with (
                mock.patch("aha_cli.backends.claude.subprocess.Popen", return_value=process),
                mock.patch("aha_cli.backends.process_stream._finish_completed_backend_process", side_effect=terminate) as cleanup,
            ):
                code, reply, _ = run_claude_exec(
                    "hello",
                    cwd=Path(tmp),
                    output_file=output,
                    events_file=events,
                    run_id="run-001",
                    task_id="task-001",
                    source="claude-chat",
                    target="main",
                    completion_grace_seconds=0.01,
                )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(code, 0)
        self.assertEqual(reply, "done")
        cleanup.assert_called_once_with(process)
        self.assertEqual([row["type"] for row in rows], ["agent_usage", "backend_completion_grace_exceeded"])
        self.assertEqual(rows[-1]["data"]["backend"], "claude")
        self.assertEqual(rows[-1]["data"]["process_exit_code"], 137)

    def test_is_claude_turn_result_excludes_task_notifications(self) -> None:
        self.assertTrue(_is_claude_turn_result(json.dumps({"type": "result", "result": "done"})))
        self.assertTrue(_is_claude_turn_result(json.dumps({"type": "result", "origin": {}, "result": "done"})))
        self.assertFalse(
            _is_claude_turn_result(
                json.dumps({"type": "result", "origin": {"kind": "task-notification"}, "result": "", "num_turns": 0})
            )
        )
        self.assertFalse(_is_claude_turn_result(json.dumps({"type": "assistant", "message": {}})))
        self.assertFalse(_is_claude_turn_result("not json"))

    def test_claude_task_notification_result_is_not_a_turn_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.md"
            events = Path(tmp) / "events.jsonl"
            task_notification = json.dumps(
                {"type": "result", "origin": {"kind": "task-notification"}, "result": "", "num_turns": 0}
            ) + "\n"
            genuine = json.dumps({"type": "result", "result": "real answer"}) + "\n"

            class SequentialStdout:
                def __init__(self) -> None:
                    self.lines = iter([task_notification, genuine])

                def __iter__(self) -> "SequentialStdout":
                    return self

                def __next__(self) -> str:
                    return next(self.lines)

            class FakeProcess:
                pid = 5679

                def __init__(self) -> None:
                    self.stdin = io.StringIO()
                    self.stdout = SequentialStdout()

                def wait(self) -> int:
                    return 0

            process = FakeProcess()
            with (
                mock.patch("aha_cli.backends.claude.subprocess.Popen", return_value=process),
                mock.patch("aha_cli.backends.process_stream._finish_completed_backend_process") as cleanup,
            ):
                code, reply, _ = run_claude_exec(
                    "hello",
                    cwd=Path(tmp),
                    output_file=output,
                    events_file=events,
                    run_id="run-001",
                    task_id="task-001",
                    source="claude-chat",
                    target="main",
                )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(code, 0)
        self.assertEqual(reply, "real answer")
        cleanup.assert_not_called()
        self.assertEqual([row["type"] for row in rows], ["agent_usage", "agent_usage"])

    def test_backend_completion_with_normal_eof_does_not_force_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.md"
            output.write_text("done", encoding="utf-8")

            class FakeProcess:
                stdin = io.StringIO()
                stdout = io.StringIO(json.dumps({"type": "turn.completed", "usage": {}}) + "\n")

                def wait(self) -> int:
                    return 0

            with (
                mock.patch("aha_cli.backends.codex.subprocess.Popen", return_value=FakeProcess()),
                mock.patch("aha_cli.backends.process_stream._finish_completed_backend_process") as cleanup,
            ):
                code, reply, _ = run_codex_exec("hello", cwd=Path(tmp), output_file=output)

        self.assertEqual(code, 0)
        self.assertEqual(reply, "done")
        cleanup.assert_not_called()

    def test_backend_without_completion_preserves_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.md"
            output.write_text("backend failed", encoding="utf-8")

            class FakeProcess:
                stdin = io.StringIO()
                stdout = io.StringIO(json.dumps({"type": "error", "message": "failed"}) + "\n")

                def wait(self) -> int:
                    return 9

            with mock.patch("aha_cli.backends.codex.subprocess.Popen", return_value=FakeProcess()):
                code, reply, _ = run_codex_exec("hello", cwd=Path(tmp), output_file=output)

        self.assertEqual(code, 9)
        self.assertEqual(reply, "backend failed")

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
            reasoning_effort="xhigh",
            session_id="session-123",
        )
        self.assertEqual(cmd[:5], ["claude", "-p", "--output-format", "stream-json", "--verbose"])
        self.assertIn("--model", cmd)
        self.assertIn("sonnet", cmd)
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "xhigh")
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
            text_result = handle_claude_event(
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
                session=session,
            )
            started_result = handle_claude_event(
                json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "pwd"}}]}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
                session=session,
            )
            finished_result = handle_claude_event(
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
        self.assertEqual(rows[0]["data"]["backend_session_id"], "claude-session")
        self.assertEqual(rows[1]["data"]["text"], "hello")
        self.assertEqual(rows[2]["data"]["command"], "pwd")
        self.assertEqual(rows[3]["data"]["output_tail"], "ok")
        self.assertEqual(rows[3]["data"]["output_chars"], 2)
        self.assertNotIn("output_ref", rows[3]["data"])
        self.assertEqual(rows[4]["data"]["backend_session_id"], "claude-session")
        self.assertEqual(rows[4]["data"]["usage"]["input_tokens"], 1)
        self.assertEqual(text_result["events"][0][0], "agent_message")
        self.assertEqual(started_result["events"][0][0], "agent_command_started")
        self.assertEqual(started_result["events"][0][1]["command"], "pwd")
        self.assertEqual(finished_result["events"][0][0], "agent_command_finished")
        self.assertEqual(finished_result["events"][0][1]["output_tail"], "ok")

    def test_claude_large_tool_result_records_output_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            content = "y" * 1300
            result = handle_claude_event(
                json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": content}]}}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
            )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
            artifact_text = (events.parent / rows[0]["data"]["output_ref"]["path"]).read_text(encoding="utf-8")

        self.assertEqual(rows[0]["type"], "agent_command_finished")
        self.assertEqual(rows[0]["data"]["output_chars"], 1300)
        self.assertEqual(len(rows[0]["data"]["output_tail"]), 1200)
        output_ref = rows[0]["data"]["output_ref"]
        self.assertEqual(output_ref["kind"], "command_output")
        self.assertEqual(output_ref["chars"], 1300)
        self.assertTrue(output_ref["path"].startswith("tasks/task-001/artifacts/command-output/main-"))
        self.assertEqual(artifact_text, content)
        self.assertEqual(result["events"][0][1]["output_ref"]["path"], output_ref["path"])

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

    def test_claude_auto_context_compact_marks_next_prompt_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            session: dict = {"backend_session_id": "claude-session-1"}
            handle_claude_event(
                json.dumps({"type": "system", "subtype": "context_compacted", "message": "context was compacted automatically"}),
                events_file=events,
                run_id="run",
                task_id="task-001",
                source="claude-chat",
                target="main",
                session=session,
            )
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([row["type"] for row in rows], ["backend_auto_context_compact"])
        self.assertEqual(session[FORCE_FULL_PROMPT_NEXT_TURN_KEY]["reason"], "backend_auto_context_compact")
        self.assertEqual(session[FORCE_FULL_PROMPT_NEXT_TURN_KEY]["subtype"], "context_compacted")

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
            aha_root = root / ".aha"
            home = Path(home_tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("--home", str(aha_root), "init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("--home", str(aha_root), "plan", "Compact reset", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_id = "compact-reset-session-1"
                session_file = run_dir(aha_root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = session_id
                session_file.write_text(json.dumps(session), encoding="utf-8")
                append_jsonl(
                    home / ".codex" / "sessions" / "2026" / "05" / "21" / f"rollout-{session_id}.jsonl",
                    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "old prompt"}]}},
                )
                append_message(aha_root, run_id, "main", "previous request", sender="browser", task_id="task-001", role="main")
                append_event(
                    aha_root,
                    run_id,
                    "agent_usage",
                    {
                        "task_id": "task-001",
                        "target": "main",
                        "usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 30, "total_tokens": 999},
                    },
                )

                with mock.patch("aha_cli.services.session_compact.Path.home", return_value=home):
                    payload = compact_reset_backend_session(aha_root, run_id, "task-001", "main", reason="manual")

                updated = read_json(session_file)
                summary_exists = (run_dir(aha_root, run_id) / payload["summary_path"]).exists()
                offset_file = chat_offset_path(run_dir(aha_root, run_id), "main", "task-001")
                offset = read_json(offset_file)
                inbox_size = inbox_path(aha_root, run_id, "main").stat().st_size
                prompt = chat_prompt(
                    aha_root,
                    run_id,
                    "main",
                    {"sender": "browser", "message": "next request", "task_id": "task-001", "role": "main"},
                    "prefix",
                )

        self.assertEqual(payload["old_backend_session_id"], session_id)
        self.assertIsNone(updated["backend_session_id"])
        self.assertEqual(updated["history_backend_sessions"][0]["backend_session_id"], session_id)
        self.assertEqual(updated["history_backend_sessions"][0]["last_usage"]["input_tokens"], 120)
        self.assertEqual(updated["history_backend_sessions"][0]["token_summary"]["total_tokens"], 150)
        self.assertEqual(updated["history_backend_sessions"][0]["token_summary"]["cached_tokens"], 20)
        self.assertEqual(updated["compact_summary"]["archived_backend_session_id"], session_id)
        self.assertEqual(updated[FORCE_FULL_PROMPT_NEXT_TURN_KEY]["reason"], "backend_session_compact_reset")
        self.assertEqual(updated[FORCE_FULL_PROMPT_NEXT_TURN_KEY]["trigger"], "manual")
        self.assertEqual(updated[FORCE_FULL_PROMPT_NEXT_TURN_KEY]["summary_path"], payload["summary_path"])
        self.assertTrue(summary_exists)
        self.assertEqual(offset["offset"], inbox_size)
        self.assertIn("Backend compact summary from previous session", prompt)
        self.assertIn("- reason: `manual`", prompt)
        self.assertIn("previous request", prompt)
        self.assertIn("Recent conversation chains", prompt)
        self.assertIn("Intent priority:", prompt)

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


class ProcessStreamTreeKillTests(unittest.TestCase):
    """Grace-exceeded backend cleanup must tree-kill a lingering parent.

    Regression for task-010's ``native_completion_without_stdout_eof``: a Claude
    CLI that completes but keeps its stdout pipe open via an inherited descendant
    must be reaped together with that descendant, not just the bare parent.
    """

    def test_finish_completed_backend_tree_kills_when_parent_refuses_to_die(self) -> None:
        from aha_cli.backends import process_stream

        calls: list[object] = []

        class StubbornProcess:
            pid = 4242

            def poll(self) -> None:
                return None  # parent still alive

            def terminate(self) -> None:
                calls.append("terminate")

            def wait(self, timeout: float) -> None:
                calls.append(("wait", timeout))
                import subprocess as _subprocess

                raise _subprocess.TimeoutExpired("fake", timeout)  # does not exit within termination wait

            def kill(self) -> None:
                calls.append("kill")

        with mock.patch("aha_cli.backends.process_stream.terminate_process_tree") as tree_kill:
            exit_code = process_stream._finish_completed_backend_process(StubbornProcess())

        tree_kill.assert_called_once_with(4242)
        self.assertIn("terminate", calls)
        self.assertIn("kill", calls)

    def test_finish_completed_backend_skips_tree_kill_when_parent_exited(self) -> None:
        from aha_cli.backends import process_stream

        class ExitedProcess:
            pid = 4243

            def poll(self) -> int:
                return 0

            def terminate(self) -> None:
                raise AssertionError("must not terminate an already-exited parent")

            def wait(self, timeout: float) -> None:
                raise AssertionError("must not wait on an already-exited parent")

        with mock.patch("aha_cli.backends.process_stream.terminate_process_tree") as tree_kill:
            exit_code = process_stream._finish_completed_backend_process(ExitedProcess())

        tree_kill.assert_not_called()
        self.assertEqual(exit_code, 0)
