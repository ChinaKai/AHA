from __future__ import annotations

import concurrent.futures
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from aha_cli.backends.registry import CODEX_DEFAULT_MODEL
from aha_cli.backends.claude import run_claude_exec
from aha_cli.cli import main
from aha_cli.services.backend_runtime import _process_matches_home, backend_status, start_backend, stop_task_backends
from aha_cli.store.filesystem import add_agent, append_event, read_json, session_path, update_agent_config, write_json


class BackendRuntimeTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_start_backend_serializes_concurrent_autostart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Backend start lock", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                    concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
                ):
                    futures = [
                        pool.submit(start_backend, root, run_id, "main", task_id="task-001")
                        for _ in range(2)
                    ]
                    results = [future.result(timeout=10) for future in futures]

        self.assertEqual(popen.call_count, 1)
        self.assertEqual(sum(1 for result in results if result.get("started")), 1)
        self.assertEqual(sum(1 for result in results if result.get("already_running")), 1)

    def test_start_backend_preserves_home_and_absolute_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Backend env", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch.dict(os.environ, {"PYTHONPATH": "src"}, clear=False),
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", task_id="task-001")

        command = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        self.assertIn("--home", command)
        self.assertEqual(command[command.index("--home") + 1], str(root / ".aha"))
        self.assertTrue(Path(env["PYTHONPATH"].split(os.pathsep)[0]).is_absolute())
        self.assertEqual(env["AHA_ROOT"], str(root / ".aha"))
        self.assertEqual(env["AHA_RUN_ID"], run_id)
        self.assertEqual(env["AHA_TASK_ID"], "task-001")
        self.assertEqual(env["AHA_AGENT_ID"], "main")
        self.assertEqual(env["AHA_BACKEND"], "codex")
        self.assertEqual(env["AHA_MODEL"], CODEX_DEFAULT_MODEL)
        self.assertEqual(env["AHA_GENERATED_BY"], "AHA Codex GPT-5.5")

    def test_start_codex_backend_resolves_default_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Codex default model", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    status = start_backend(root / ".aha", run_id, "main", task_id="task-001")

        command = popen.call_args.args[0]
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], CODEX_DEFAULT_MODEL)
        self.assertIn("--requested-model", command)
        self.assertEqual(command[command.index("--requested-model") + 1], "")
        self.assertIsNone(status["requested_model"])
        self.assertEqual(status["resolved_model"], CODEX_DEFAULT_MODEL)
        self.assertEqual(status["model"], CODEX_DEFAULT_MODEL)

    def test_start_backend_uses_selected_codex_env_model_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                cfg_path = root / ".aha" / "config.json"
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg["codex"]["env"] = [
                    {
                        "name": "openai",
                        "OPENAI_API_KEY": "work-key",
                        "OPENAI_BASE_URL": "https://openai.test/v1",
                        "OPENAI_MODEL": "kimi-k2.6",
                        "CODEX_WIRE_API": "responses",
                        "CODEX_ENV_KEY": "MINIMAX_API_KEY",
                    }
                ]
                cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
                code, plan_output = self.run_cli("plan", "Codex env model", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4244

                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    status = start_backend(root / ".aha", run_id, "main", backend="codex", model="env:openai", task_id="task-001")

        env = popen.call_args.kwargs["env"]
        command = popen.call_args.args[0]
        self.assertEqual(env["MINIMAX_API_KEY"], "work-key")
        self.assertNotIn("OPENAI_BASE_URL", env)
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "env:openai")
        self.assertEqual(status["requested_model"], "env:openai")
        self.assertEqual(status["resolved_model"], "kimi-k2.6")

    def test_backend_status_reports_context_pressure_from_latest_codex_token_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            with (
                mock.patch("pathlib.Path.cwd", return_value=root),
                mock.patch("pathlib.Path.home", return_value=home),
            ):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Context pressure", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()),
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", task_id="task-001")
                session_file = session_path(root / ".aha", run_id, "task-001", "main")
                session = read_json(session_file)
                session["backend_session_id"] = "codex-session-123"
                write_json(session_file, session)
                codex_session = home / ".codex" / "sessions" / "2026" / "05" / "24" / "rollout-codex-session-123.jsonl"
                codex_session.parent.mkdir(parents=True)
                codex_session.write_text(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "model_context_window": 123456,
                                    "last_token_usage": {"input_tokens": 10, "total_tokens": 11},
                                },
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                with codex_session.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "token_count",
                                    "info": {
                                        "model_context_window": 258400,
                                        "last_token_usage": {
                                            "input_tokens": 226853,
                                            "cached_input_tokens": 226176,
                                            "output_tokens": 296,
                                            "reasoning_output_tokens": 0,
                                            "total_tokens": 227149,
                                        },
                                    },
                                },
                            }
                        )
                        + "\n"
                    )
                append_event(
                    root / ".aha",
                    run_id,
                    "agent_usage",
                    {"task_id": "task-001", "target": "main", "usage": {"input_tokens": 99999999}},
                )
                append_event(
                    root / ".aha",
                    run_id,
                    "agent_prompt_metrics",
                    {
                        "task_id": "task-001",
                        "target": "main",
                        "source": "codex-chat",
                        "total": {"tokens": 219640, "chars": 1234, "bytes": 1234, "lines": 12},
                    },
                )

                status = backend_status(root / ".aha", run_id, "main", task_id="task-001")

        self.assertEqual(status["latest_usage"]["input_tokens"], 99999999)
        self.assertEqual(status["latest_prompt_metrics"]["total"]["tokens"], 219640)
        self.assertEqual(status["runtime_context_window"], 258400)
        self.assertEqual(status["runtime_context_usage"]["input_tokens"], 226853)
        self.assertEqual(status["runtime_context_usage"]["cached_input_tokens"], 226176)
        self.assertEqual(status["context_pressure"]["context_window"], 258400)
        self.assertEqual(status["context_pressure"]["context_window_source"], "runtime")
        self.assertAlmostEqual(status["context_pressure"]["ratio"], 226853 / 258400, places=6)
        self.assertEqual(status["context_pressure"]["level"], "high")
        self.assertEqual(status["context_pressure"]["input_tokens"], 226853)
        self.assertEqual(status["context_pressure"]["prompt_tokens"], 219640)
        self.assertEqual(status["context_pressure"]["runtime_input_tokens"], 226853)
        self.assertEqual(status["context_pressure"]["pressure_source"], "runtime.last_token_usage.input_tokens")

    def test_backend_status_keeps_context_pressure_unknown_without_prompt_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Context pressure unknown", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()),
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", task_id="task-001")
                append_event(
                    root / ".aha",
                    run_id,
                    "agent_usage",
                    {"task_id": "task-001", "target": "main", "usage": {"input_tokens": 99999999}},
                )
                append_event(
                    root / ".aha",
                    run_id,
                    "agent_prompt_metrics",
                    {
                        "task_id": "task-001",
                        "target": "main",
                        "source": "codex-chat",
                        "total": {"chars": 1234, "bytes": 1234, "lines": 12},
                    },
                )

                status = backend_status(root / ".aha", run_id, "main", task_id="task-001")

        self.assertEqual(status["latest_usage"]["input_tokens"], 99999999)
        self.assertIsNone(status["context_pressure"]["prompt_tokens"])
        self.assertEqual(status["context_pressure"]["prompt_chars"], 1234)
        self.assertIsNone(status["context_pressure"]["percent"])
        self.assertEqual(status["context_pressure"]["level"], "unknown")

    def test_start_backend_adds_common_user_bin_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            fake_home = Path(tmp) / "home"
            nvm_bin = fake_home / ".nvm" / "versions" / "node" / "v24.15.0" / "bin"
            local_bin = fake_home / ".local" / "bin"
            nvm_bin.mkdir(parents=True)
            local_bin.mkdir(parents=True)
            root.mkdir()
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Backend PATH", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True),
                    mock.patch("aha_cli.services.backend_runtime.Path.home", return_value=fake_home),
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", task_id="task-001")

        parts = popen.call_args.kwargs["env"]["PATH"].split(os.pathsep)
        self.assertLess(parts.index(str(local_bin)), parts.index("/usr/bin"))
        self.assertLess(parts.index(str(nvm_bin)), parts.index("/usr/bin"))

    def test_start_backend_uses_zipapp_invocation_for_onebin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "aha"
            code, output = self.run_cli("package", "onebin", "--output", str(artifact))
            self.assertEqual(code, 0, output)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "One-bin backend start", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.sys.argv", [str(artifact)]),
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", task_id="task-001")

        command = popen.call_args.args[0]
        self.assertEqual(command[:2], [sys.executable, str(artifact.resolve())])
        self.assertIn("codex-chat", command)
        self.assertIn("--home", command)
        self.assertEqual(command[command.index("--home") + 1], str(root / ".aha"))

    def test_start_backend_uses_claude_chat_command_for_claude_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude backend start", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", backend="claude", task_id="task-001", claude_bin="claude-dev")

        command = popen.call_args.args[0]
        self.assertIn("claude-chat", command)
        self.assertIn("--claude-bin", command)
        self.assertEqual(command[command.index("--claude-bin") + 1], "claude-dev")

    def test_backend_status_reports_discovered_claude_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude backend discovery", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with (
                    mock.patch(
                        "aha_cli.services.backend_runtime._discover_backend_process",
                        return_value=(4242, "claude-chat"),
                    ),
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    status = backend_status(root / ".aha", run_id, "main", task_id="task-001")

        self.assertEqual(status["backend"], "claude-chat")
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["pid"], 4242)

    def test_backend_status_reports_claude_context_pressure_from_latest_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude context pressure", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()),
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", backend="claude", task_id="task-001")
                    append_event(
                        root / ".aha",
                        run_id,
                        "agent_usage",
                        {
                            "task_id": "task-001",
                            "target": "main",
                            "usage": {
                                "input_tokens": 1000,
                                "cache_read_input_tokens": 2000,
                                "cache_creation_input_tokens": 3000,
                                "output_tokens": 400,
                            },
                        },
                    )
                    append_event(
                        root / ".aha",
                        run_id,
                        "agent_prompt_metrics",
                        {
                            "task_id": "task-001",
                            "target": "main",
                            "source": "claude-chat",
                            "total": {"chars": 1234, "bytes": 1234, "lines": 12},
                        },
                    )

                    status = backend_status(root / ".aha", run_id, "main", task_id="task-001")

        self.assertEqual(status["backend"], "claude-chat")
        self.assertEqual(status["runtime_context_usage"]["input_tokens"], 1000)
        self.assertEqual(status["runtime_context_usage"]["cache_read_input_tokens"], 2000)
        self.assertEqual(status["context_pressure"]["backend"], "claude")
        self.assertEqual(status["context_pressure"]["context_window"], 200_000)
        self.assertEqual(status["context_pressure"]["input_tokens"], 6000)
        self.assertEqual(status["context_pressure"]["runtime_effective_input_tokens"], 6000)
        self.assertEqual(status["context_pressure"]["pressure_source"], "runtime.last_token_usage.effective_input_tokens")
        self.assertEqual(status["context_pressure"]["percent"], 3.0)

    def test_backend_process_home_matching_rejects_other_aha_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_home = root / "current" / ".aha"
            other_home = root / "other" / ".aha"
            current_home.mkdir(parents=True)
            other_home.mkdir(parents=True)
            parts = [
                sys.executable,
                "-m",
                "aha_cli",
                "--home",
                str(other_home),
                "claude-chat",
                "run-001",
                "main",
                "--task-id",
                "task-024",
            ]

            self.assertFalse(_process_matches_home(parts, current_home))
            parts[parts.index("--home") + 1] = str(current_home)
            self.assertTrue(_process_matches_home(parts, current_home))

    def test_start_backend_injects_claude_env_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                cfg_path = root / ".aha" / "config.json"
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg["claude"]["env"] = {
                    "ANTHROPIC_API_KEY": "test-key",
                    "base_url": "https://claude.test",
                }
                cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
                code, plan_output = self.run_cli("plan", "Claude env", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", backend="claude", task_id="task-001")

        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_API_KEY"], "test-key")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://claude.test")

    def test_start_backend_uses_selected_claude_env_model_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                cfg_path = root / ".aha" / "config.json"
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg["claude"]["env"] = [
                    {
                        "name": "work",
                        "ANTHROPIC_API_KEY": "work-key",
                        "ANTHROPIC_BASE_URL": "https://claude.test",
                        "ANTHROPIC_MODEL": "kimi-k2.6",
                    }
                ]
                cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
                code, plan_output = self.run_cli("plan", "Claude env model", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4243

                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    status = start_backend(root / ".aha", run_id, "main", backend="claude", model="env:work", task_id="task-001")

        env = popen.call_args.kwargs["env"]
        command = popen.call_args.args[0]
        self.assertEqual(env["ANTHROPIC_API_KEY"], "work-key")
        self.assertEqual(env["ANTHROPIC_MODEL"], "kimi-k2.6")
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "env:work")
        self.assertEqual(status["requested_model"], "env:work")
        self.assertEqual(status["resolved_model"], "kimi-k2.6")

    def test_claude_exec_reports_missing_auth_env_before_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.md"
            events = Path(tmp) / "events.jsonl"
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch("aha_cli.backends.claude.subprocess.Popen") as popen,
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

            self.assertEqual(code, 1)
            self.assertIn("Claude authentication is not configured", reply)
            self.assertEqual(output.read_text(encoding="utf-8"), reply)
            popen.assert_not_called()

    def test_claude_exec_reports_missing_cli_as_agent_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reply.md"
            events = Path(tmp) / "events.jsonl"
            with (
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True),
                mock.patch("aha_cli.backends.claude.subprocess.Popen", side_effect=FileNotFoundError(2, "No such file or directory", "claude")),
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
            output_text = output.read_text(encoding="utf-8")

        self.assertEqual(code, 127)
        self.assertIn("Failed to start Claude backend command", reply)
        self.assertEqual(output_text, reply)
        self.assertEqual(rows[-1]["type"], "agent_error")
        self.assertEqual(rows[-1]["data"]["reason"], "backend_start_failed")

    def test_start_backend_applies_task_proxy_env_for_enabled_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "Backend proxy env",
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

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch.dict(os.environ, {"HTTP_PROXY": "http://outer", "NO_PROXY": "outer"}, clear=False),
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", task_id="task-001")

        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1")
        self.assertEqual(env["http_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(env["https_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(env["no_proxy"], "localhost,127.0.0.1")

    def test_start_backend_clears_inherited_proxy_env_for_disabled_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "Backend no proxy env",
                    "--agents",
                    "1",
                    "--http-proxy",
                    "http://127.0.0.1:7890",
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_agent_config(root / ".aha", run_id, "task-001", "main", proxy_enabled=False)

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch.dict(
                        os.environ,
                        {"HTTP_PROXY": "http://outer", "HTTPS_PROXY": "http://outer", "NO_PROXY": "outer"},
                        clear=False,
                    ),
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", task_id="task-001")

        env = popen.call_args.kwargs["env"]
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn("NO_PROXY", env)
        self.assertNotIn("http_proxy", env)

    def test_backend_activity_can_be_filtered_by_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Scoped backend activity", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                self.run_cli("task", "add", run_id, "Second task", "--no-dispatch")
                append_event(root, run_id, "agent_started", {"target": "main", "task_id": "task-002"})

                task_one = backend_status(root, run_id, "main", task_id="task-001")
                task_two = backend_status(root, run_id, "main", task_id="task-002")

        self.assertFalse(task_one["busy"])
        self.assertTrue(task_two["busy"])

    def test_stop_task_backends_skips_current_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Stop task workers", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")

                def fake_status(_root: Path, _run_id: str, target: str = "main", task_id: str | None = None) -> dict:
                    return {
                        "target": target,
                        "task_id": task_id,
                        "status": "running",
                        "pid": 111 if target == "main" else 222,
                    }

                with (
                    mock.patch("aha_cli.services.backend_runtime.backend_status", side_effect=fake_status),
                    mock.patch("aha_cli.services.backend_runtime.stop_backend", side_effect=lambda _root, _run_id, target, **_kwargs: {"target": target, "stopped": True}) as stop_backend,
                ):
                    stopped = stop_task_backends(root, run_id, "task-001", exclude_pid=111)

        self.assertEqual(stopped, [{"target": "sub-001", "stopped": True}])
        stop_backend.assert_called_once()
        self.assertEqual(stop_backend.call_args.args[:3], (root, run_id, "sub-001"))
