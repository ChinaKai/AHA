from __future__ import annotations

import concurrent.futures
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock

from aha_cli.backends.registry import CODEX_DEFAULT_MODEL
from aha_cli.backends.claude import run_claude_exec
from aha_cli.cli import main
from aha_cli.services import backend_runtime as backend_runtime_module
from aha_cli.services.backend_runtime import (
    _agent_chat_command,
    _claude_session_jsonl_path,
    _process_matches_home,
    _provider_id_for_model,
    _resolve_wsl_target,
    _state_wsl_context,
    _wsl_backend_process_env,
    _wsl_session_paths,
    backend_status,
    detect_runtime_context_compaction,
    start_backend,
    stop_task_backends,
)
from aha_cli.store.filesystem import add_agent, append_event, read_json, session_path, update_agent_config, write_json


class BackendRuntimeTests(unittest.TestCase):
    def test_provider_id_for_model_resolves_env_group_provider(self) -> None:
        cfg = {
            "codex": {
                "env": [
                    {"name": "deepseek-deepseek-v4-flash-452b42ce", "AHA_PROVIDER_ID": "deepseek-provider"},
                    {"name": "hualai-deepseek-v4-flash", "AHA_PROVIDER_ID": "hualai-provider"},
                ],
            },
        }

        # env: selector resolves to the matching group's provider.
        self.assertEqual(
            _provider_id_for_model(cfg, "codex", "env:deepseek-deepseek-v4-flash-452b42ce"),
            "deepseek-provider",
        )
        # Non-env selector (already resolved model name) cannot resolve a provider.
        self.assertIsNone(_provider_id_for_model(cfg, "codex", "deepseek-v4-flash"))

    def test_agent_chat_command_wsl_target_wraps_in_wsl(self) -> None:
        command = _agent_chat_command(
            "run1",
            "main",
            backend="codex",
            aha_home=Path("C:/Users/toope/.aha"),
            codex_bin="codex",
            task_id="task-005",
            wsl_target={
                "distro": "Ubuntu-24.04",
                "aha_home": "/mnt/c/Users/toope/.aha",
                "aha_bin": "/mnt/c/Users/toope/AppData/Local/AHA/aha",
                "backend_bin": "/home/kaikai/.nvm/versions/node/v24.18.0/bin/codex",
            },
        )

        self.assertEqual(command[0], "wsl.exe")
        self.assertIn("-d", command)
        self.assertIn("Ubuntu-24.04", command)
        self.assertEqual(command[4], "bash")
        self.assertEqual(command[5], "-c")
        script = command[6]
        self.assertIn("--codex-bin", script)
        self.assertIn("/home/kaikai/.nvm/versions/node/v24.18.0/bin/codex", script)
        self.assertIn("--home", script)
        self.assertIn("/mnt/c/Users/toope/.aha", script)
        # wsl.exe forwards the -c argument verbatim, so the script must not be
        # wrapped as a whole; a fully single-quoted script becomes one word
        # inside bash and cannot be exec'd (exit 127).
        words = shlex.split(script)
        self.assertEqual(words[0], "python3")
        self.assertIn("/mnt/c/Users/toope/AppData/Local/AHA/aha", words)
        self.assertIn("You are connected to AHA as the real backend agent.", words)

    def test_agent_chat_command_no_wsl_target_uses_plain_command(self) -> None:
        command = _agent_chat_command(
            "run1",
            "main",
            backend="codex",
            aha_home=Path("C:/Users/toope/.aha"),
            codex_bin="codex",
            task_id="task-005",
        )

        self.assertNotIn("wsl.exe", command)
        self.assertIn("codex-chat", command)

    def test_wsl_backend_process_env_scrubs_windows_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "PATH": r"C:\Users\toope\AppData\Local\AHA;C:\Windows\System32",
                "OPENAI_API_KEY": "secret",
                "PYTHONPATH": "src",
                "SystemRoot": r"C:\Windows",
            },
            clear=True,
        ):
            env = _wsl_backend_process_env(
                {
                    "AHA_ROOT": r"C:\Users\toope\.aha",
                    "AHA_WSL_DISTRO": "Ubuntu-24.04",
                    "AHA_MODEL": "",
                    "WSLENV": "AHA_WSL_DISTRO:AHA_WSL_AHA_HOME",
                }
            )

        # Only the Windows basics plus the WSLENV-forwarded AHA vars cross the
        # hop; the service PATH (translated AHA install dir first) and any
        # provider secrets must stay behind on the Windows side.
        self.assertEqual(set(env), {"SystemRoot", "AHA_ROOT", "AHA_WSL_DISTRO", "WSLENV"})
        self.assertEqual(env["SystemRoot"], r"C:\Windows")

    def test_wsl_backend_process_env_defaults_system_root(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            env = _wsl_backend_process_env({"AHA_WSL_DISTRO": "Ubuntu-24.04"})

        self.assertEqual(env["SystemRoot"], r"C:\Windows")
        self.assertEqual(env["AHA_WSL_DISTRO"], "Ubuntu-24.04")

    def test_wsl_backend_process_env_passes_task_proxy(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            env = _wsl_backend_process_env(
                {"AHA_WSL_DISTRO": "Ubuntu-24.04"},
                {
                    "HTTP_PROXY": "http://proxy.test:7890",
                    "no_proxy": "localhost,127.0.0.1",
                    "UNRELATED_VAR": "must-not-cross",
                },
            )

        # Proxy config is the one non-AHA payload allowed across the hop, so
        # WSL backends honor the same task egress settings; unknown vars stay
        # behind with the rest of the service environment.
        self.assertEqual(env["HTTP_PROXY"], "http://proxy.test:7890")
        self.assertEqual(env["no_proxy"], "localhost,127.0.0.1")
        self.assertNotIn("UNRELATED_VAR", env)

    def test_start_backend_wsl_launch_uses_scrubbed_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "WSL env scrub", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

            class FakeProcess:
                pid = 4242

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "PATH": r"C:\Users\toope\AppData\Local\AHA;C:\Windows\System32",
                        "OPENAI_API_KEY": "secret",
                        "SystemRoot": r"C:\Windows",
                    },
                    clear=True,
                ),
                mock.patch(
                    "aha_cli.services.backend_runtime._resolve_wsl_target",
                    return_value={
                        "distro": "Ubuntu-24.04",
                        "aha_home": "/mnt/c/Users/toope/.aha",
                        "aha_bin": "/mnt/c/Users/toope/AppData/Local/AHA/aha",
                        "backend_bin": "/home/kaikai/.nvm/versions/node/v24.18.0/bin/codex",
                        "python": "/usr/bin/python3",
                    },
                ),
                mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
            ):
                start_backend(root / ".aha", run_id, "main", task_id="task-001")

        command = popen.call_args.args[0]
        self.assertEqual(command[0], "wsl.exe")
        env = popen.call_args.kwargs["env"]
        self.assertNotIn("PATH", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["SystemRoot"], r"C:\Windows")
        self.assertEqual(env["AHA_WSL_DISTRO"], "Ubuntu-24.04")
        self.assertEqual(env["AHA_WSL_AHA_HOME"], "/mnt/c/Users/toope/.aha")
        self.assertIn("AHA_WSL_DISTRO", env["WSLENV"])
        self.assertIn("AHA_WSL_AHA_HOME", env["WSLENV"])

    def test_start_backend_wsl_launch_forwards_task_proxy_via_wslenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "WSL proxy forward", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

            class FakeProcess:
                pid = 4242

            with (
                mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}, clear=True),
                mock.patch(
                    "aha_cli.services.backend_runtime._resolve_wsl_target",
                    return_value={
                        "distro": "Ubuntu-24.04",
                        "aha_home": "/mnt/c/Users/toope/.aha",
                        "aha_bin": "/mnt/c/Users/toope/AppData/Local/AHA/aha",
                        "backend_bin": "/home/kaikai/.nvm/versions/node/v24.18.0/bin/codex",
                        "python": "/usr/bin/python3",
                    },
                ),
                mock.patch(
                    "aha_cli.services.backend_runtime._backend_proxy_env",
                    return_value={"HTTP_PROXY": "http://proxy.test:7890", "NO_PROXY": "localhost"},
                ),
                mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
            ):
                start_backend(root / ".aha", run_id, "main", task_id="task-001")

        env = popen.call_args.kwargs["env"]
        # Proxy vars ride the scrubbed env AND get declared in WSLENV, or
        # wsl.exe would drop them at the hop.
        self.assertEqual(env["HTTP_PROXY"], "http://proxy.test:7890")
        self.assertEqual(env["NO_PROXY"], "localhost")
        for part in ("AHA_WSL_DISTRO", "HTTP_PROXY", "NO_PROXY"):
            self.assertIn(part, env["WSLENV"].split(":"))

    def test_resolve_wsl_target_requires_wsl_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Non-WSL workspace -> None.
            self.assertIsNone(_resolve_wsl_target(root, r"C:\Users\toope\proj", "codex"))

            # WSL workspace but no native backend -> None.
            with mock.patch(
                "aha_cli.services.wsl_backend.wsl_backends_for_workspace",
                return_value={},
            ):
                self.assertIsNone(
                    _resolve_wsl_target(root, r"\\wsl.localhost\Ubuntu-24.04\home\kaikai\proj", "codex")
                )

    def test_resolve_wsl_target_builds_target_with_native_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch(
                "aha_cli.services.wsl_backend.wsl_backends_for_workspace",
                return_value={"codex": "/home/kaikai/.nvm/versions/node/v24.18.0/bin/codex"},
            ):
                with mock.patch(
                    "aha_cli.services.backend_runtime._running_zipapp_path",
                    return_value=Path("/tmp/aha"),
                ):
                    target = _resolve_wsl_target(
                        root,
                        r"\\wsl.localhost\Ubuntu-24.04\home\kaikai\proj",
                        "codex",
                    )
            self.assertIsNotNone(target)
            self.assertEqual(target["distro"], "Ubuntu-24.04")
            self.assertEqual(target["backend_bin"], "/home/kaikai/.nvm/versions/node/v24.18.0/bin/codex")
            self.assertIn("aha_home", target)

    def test_state_wsl_context_derives_home_from_launch_command(self) -> None:
        # Backends started before WSL home probing stored no home; derive the
        # distro and native home from the recorded wsl.exe launch command.
        command = [
            "wsl.exe",
            "-d",
            "Ubuntu-24.04",
            "--",
            "bash",
            "-c",
            "python3 /mnt/c/Users/toope/AppData/Local/AHA/aha --home /mnt/c/Users/toope/.aha "
            "claude-chat run1 main --claude-bin /home/kaikai/.local/bin/claude --task-id task-004",
        ]
        state = {"command": command}
        distro, native_home = _state_wsl_context(state)
        self.assertEqual(distro, "Ubuntu-24.04")
        self.assertEqual(native_home, "/home/kaikai")

    def test_state_wsl_context_uses_stored_fields_when_present(self) -> None:
        state = {"command": ["wsl.exe"], "wsl_distro": "Ubuntu-24.04", "wsl_native_home": "/home/kaikai"}
        self.assertEqual(_state_wsl_context(state), ("Ubuntu-24.04", "/home/kaikai"))

    def test_stop_wsl_backend_process_skips_non_wsl(self) -> None:
        # Non-WSL state: no distro -> no wsl pkill attempted.
        with mock.patch("aha_cli.services.backend_runtime.subprocess.run") as run:
            from aha_cli.services.backend_runtime import _stop_wsl_backend_process
            _stop_wsl_backend_process("run1", "main", None, {"backend": "claude-chat"}, timeout=3)
            run.assert_not_called()

    def test_stop_wsl_backend_process_builds_pkill_script(self) -> None:
        from aha_cli.services.backend_runtime import _stop_wsl_backend_process
        state = {"wsl_distro": "Ubuntu-24.04", "backend": "claude-chat"}
        captured = {}

        class FakeRun:
            def __init__(self, args, **kwargs):
                captured["args"] = list(args)
                captured["kwargs"] = kwargs

        with mock.patch("aha_cli.services.backend_runtime.subprocess.run", FakeRun):
            _stop_wsl_backend_process("run1", "main", "task-7", state, timeout=3)

        args = captured["args"]
        self.assertEqual(args[1], "-d")
        self.assertEqual(args[2], "Ubuntu-24.04")
        script = args[-1]
        # Regex trick so the pkill command line does not match itself.
        self.assertIn("[c]laude-chat run1 main --task-id task-7", script)
        self.assertIn("pgrep -f", script)
        self.assertIn("kill -TERM", script)

    def test_wait_for_worker_stop_noops_when_no_proc(self) -> None:
        # On a Windows host there is no /proc; the wait must be a no-op so stop
        # never hangs on an undiscoverable distro worker.
        from aha_cli.services.backend_runtime import _wait_for_worker_stop
        with mock.patch("aha_cli.services.backend_runtime.Path", spec=Path) as PathMock:
            PathMock.return_value.is_dir.return_value = False
            with mock.patch("aha_cli.services.backend_runtime._discover_backend_process") as discover:
                _wait_for_worker_stop(Path("/tmp"), "run1", "main", None, timeout=2)
            discover.assert_not_called()

    def test_wait_for_worker_stop_polls_until_worker_gone(self) -> None:
        from aha_cli.services.backend_runtime import _wait_for_worker_stop
        calls = {"n": 0}

        def fake_discover(*args, **kwargs):
            calls["n"] += 1
            return (9001, "claude-chat") if calls["n"] < 3 else None

        with mock.patch("aha_cli.services.backend_runtime.Path", spec=Path) as PathMock:
            PathMock.return_value.is_dir.return_value = True
            with mock.patch("aha_cli.services.backend_runtime._discover_backend_process", side_effect=fake_discover):
                _wait_for_worker_stop(Path("/tmp"), "run1", "main", None, timeout=5)
        self.assertGreaterEqual(calls["n"], 3)

    def test_state_wsl_context_returns_none_for_non_wsl_command(self) -> None:
        self.assertEqual(_state_wsl_context({"command": ["pythonw.exe", "claude-chat", "run1"]}), (None, None))

    def test_wsl_session_paths_builds_unc_candidates(self) -> None:
        from aha_cli.store.ws_target import wsl_unc_from_native

        unc = wsl_unc_from_native("Ubuntu-24.04", "/home/kaikai")
        self.assertEqual(unc, "\\\\wsl.localhost\\Ubuntu-24.04\\home\\kaikai")
        # The helper returns a list (empty when the UNC base does not resolve on
        # this host); the generated base must carry distro + native home so the
        # Windows Web service can reach the WSL session files.
        candidates = list(
            _wsl_session_paths(
                "Ubuntu-24.04",
                "/home/kaikai",
                Path(".claude") / "projects",
                "*/*abc123.jsonl",
            )
        )
        self.assertIsInstance(candidates, list)

    def test_claude_session_jsonl_path_prefers_local_home_then_wsl(self) -> None:
        # Fake session id: local Path.home() misses and the WSL UNC base does
        # not resolve on this test host, so no crash and None is returned.
        path = _claude_session_jsonl_path(
            "328ecd4f-86f0-4179-b9d8-cbf5b62502df",
            distro="Ubuntu-24.04",
            native_home="/home/kaikai",
        )
        self.assertIsNone(path)

    def test_resolve_wsl_target_returns_none_without_onebin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch(
                "aha_cli.services.wsl_backend.wsl_backends_for_workspace",
                return_value={"codex": "/home/kaikai/.nvm/versions/node/v24.18.0/bin/codex"},
            ):
                with mock.patch("aha_cli.services.backend_runtime._running_zipapp_path", return_value=None):
                    target = _resolve_wsl_target(
                        root,
                        r"\\wsl.localhost\Ubuntu-24.04\home\kaikai\proj",
                        "codex",
                    )
            self.assertIsNone(target)

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
        self.assertNotIn("--requested-model", command)
        self.assertEqual(status["requested_model"], CODEX_DEFAULT_MODEL)
        self.assertEqual(status["resolved_model"], CODEX_DEFAULT_MODEL)
        self.assertEqual(status["model"], CODEX_DEFAULT_MODEL)

    def test_start_backend_passes_agent_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Backend reasoning effort", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_agent_config(root / ".aha", run_id, "task-001", "main", reasoning_effort="xhigh")

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    status = start_backend(root / ".aha", run_id, "main", task_id="task-001")

        command = popen.call_args.args[0]
        self.assertIn("--reasoning-effort", command)
        self.assertEqual(command[command.index("--reasoning-effort") + 1], "xhigh")
        self.assertEqual(status["reasoning_effort"], "xhigh")

    def test_task_codex_backend_default_ignores_configured_env_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                cfg_path = root / ".aha" / "config.json"
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg["codex"]["model"] = "env:kimi-k2.6"
                cfg["codex"]["env"] = [
                    {
                        "name": "kimi-k2.6",
                        "OPENAI_API_KEY": "work-key",
                        "OPENAI_BASE_URL": "https://kimi.test/v1",
                        "OPENAI_MODEL": "kimi-k2.6",
                    }
                ]
                cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
                code, plan_output = self.run_cli("plan", "Codex task default ignores env model", "--agents", "1")
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
        self.assertEqual(status["requested_model"], CODEX_DEFAULT_MODEL)
        self.assertEqual(status["resolved_model"], CODEX_DEFAULT_MODEL)

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
        self.assertEqual(env["OPENAI_API_KEY"], "work-key")
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "env:openai")
        self.assertEqual(status["requested_model"], "env:openai")
        self.assertEqual(status["resolved_model"], "kimi-k2.6")

    def test_start_backend_normalizes_historical_model_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                cfg_path = root / ".aha" / "config.json"
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg["claude"]["env"] = [
                    {
                        "name": "kimi-k2.6",
                        "ANTHROPIC_API_KEY": "kimi-key",
                        "ANTHROPIC_BASE_URL": "https://kimi.test",
                        "ANTHROPIC_MODEL": "kimi-k2.6",
                    },
                    {
                        "name": "MiniMax-M2.7-highspeed",
                        "ANTHROPIC_API_KEY": "minimax-key",
                        "ANTHROPIC_BASE_URL": "https://minimax.test",
                        "ANTHROPIC_MODEL": "MiniMax-M2.7-highspeed",
                    },
                ]
                cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
                code, plan_output = self.run_cli("plan", "Historical model aliases", "--agents", "3")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    def __init__(self, pid: int) -> None:
                        self.pid = pid

                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch(
                        "aha_cli.services.backend_runtime.subprocess.Popen",
                        side_effect=[FakeProcess(5001), FakeProcess(5002), FakeProcess(5003)],
                    ) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    codex_status = start_backend(root / ".aha", run_id, "sub-001", backend="codex", model="gpt5.5", task_id="task-001")
                    kimi_status = start_backend(root / ".aha", run_id, "sub-002", backend="claude", model="kimi", task_id="task-001")
                    minimax_status = start_backend(root / ".aha", run_id, "sub-003", backend="claude", model="minimax", task_id="task-001")

        commands = [call.args[0] for call in popen.call_args_list]
        envs = [call.kwargs["env"] for call in popen.call_args_list]

        self.assertEqual(codex_status["requested_model"], "gpt5.5")
        self.assertEqual(codex_status["resolved_model"], "gpt-5.5")
        self.assertEqual(commands[0][commands[0].index("--model") + 1], "gpt-5.5")

        self.assertEqual(kimi_status["requested_model"], "kimi")
        self.assertEqual(kimi_status["resolved_model"], "kimi-k2.6")
        self.assertEqual(commands[1][commands[1].index("--model") + 1], "env:kimi-k2.6")
        self.assertEqual(envs[1]["ANTHROPIC_MODEL"], "kimi-k2.6")

        self.assertEqual(minimax_status["requested_model"], "minimax")
        self.assertEqual(minimax_status["resolved_model"], "MiniMax-M2.7-highspeed")
        self.assertEqual(commands[2][commands[2].index("--model") + 1], "env:MiniMax-M2.7-highspeed")
        self.assertEqual(envs[2]["ANTHROPIC_MODEL"], "MiniMax-M2.7-highspeed")

    def test_backend_status_reports_context_pressure_from_latest_codex_token_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aha_root = root / ".aha"
            home = root / "home"
            with (
                mock.patch("pathlib.Path.cwd", return_value=root),
                mock.patch("pathlib.Path.home", return_value=home),
            ):
                self.run_cli("--home", str(aha_root), "init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("--home", str(aha_root), "plan", "Context pressure", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()),
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(aha_root, run_id, "main", task_id="task-001")
                session_file = session_path(aha_root, run_id, "task-001", "main")
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
                    aha_root,
                    run_id,
                    "agent_usage",
                    {"task_id": "task-001", "target": "main", "usage": {"input_tokens": 99999999}},
                )
                append_event(
                    aha_root,
                    run_id,
                    "agent_prompt_metrics",
                    {
                        "task_id": "task-001",
                        "target": "main",
                        "source": "codex-chat",
                        "total": {"tokens": 219640, "chars": 1234, "bytes": 1234, "lines": 12},
                    },
                )

                status = backend_status(aha_root, run_id, "main", task_id="task-001")

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
        self.assertEqual(status["context_pressure"]["aha_prompt_tokens"], 219640)
        self.assertEqual(status["context_pressure"]["backend_input_tokens"], 226853)
        self.assertEqual(status["context_pressure"]["estimated_backend_history_tokens"], 7213)
        self.assertEqual(status["context_pressure"]["aha_overhead_ratio"], round(219640 / 226853, 6))
        self.assertEqual(status["context_pressure"]["prompt_tokens"], 219640)
        self.assertEqual(status["context_pressure"]["runtime_input_tokens"], 226853)
        self.assertEqual(status["context_pressure"]["pressure_source"], "runtime.last_token_usage.input_tokens")

    def test_detect_runtime_context_compaction_from_codex_token_count_drop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            session = {"backend_session_id": "codex-drop-session"}
            codex_session = home / ".codex" / "sessions" / "2026" / "07" / "08" / "rollout-codex-drop-session.jsonl"
            codex_session.parent.mkdir(parents=True)
            rows = [
                {
                    "timestamp": "2026-07-08T13:29:56.685Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "model_context_window": 258400,
                            "last_token_usage": {"input_tokens": 219987, "total_tokens": 220000},
                        },
                    },
                },
                {
                    "timestamp": "2026-07-08T13:30:40.978Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "model_context_window": 258400,
                            "last_token_usage": {"input_tokens": 34445, "total_tokens": 34500},
                        },
                    },
                },
            ]
            codex_session.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            with mock.patch("pathlib.Path.home", return_value=home):
                signal = detect_runtime_context_compaction(root / ".aha", "run-1", "main", "task-001", session)

        self.assertEqual(signal["backend_session_id"], "codex-drop-session")
        self.assertEqual(signal["previous"]["input_tokens"], 219987)
        self.assertEqual(signal["current"]["input_tokens"], 34445)
        self.assertEqual(signal["drop_tokens"], 185542)
        self.assertGreater(signal["drop_percent"], 70)
        self.assertTrue(signal["signature"].startswith("runtime_drop:"))

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

    def test_backend_status_reports_claude_context_pressure_from_latest_unique_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                cfg_path = root / ".aha" / "config.json"
                cfg = read_json(cfg_path)
                cfg["claude"] = {
                    "model": "env:test-gateway",
                    "env": [
                        {
                            "name": "test-gateway",
                            "ANTHROPIC_MODEL": "gateway-model",
                            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "300000",
                        }
                    ],
                }
                cfg["context_windows"] = {"claude": {"gateway-model": 123456}}
                write_json(cfg_path, cfg)
                code, plan_output = self.run_cli("plan", "Claude context pressure", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                transcript = root / "claude-session.jsonl"
                response_usage = {
                    "input_tokens": 1000,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 3000,
                    "output_tokens": 400,
                }
                rows = [
                    {"type": "assistant", "message": {"id": "response-1", "model": "gateway-model", "usage": {"input_tokens": 10}}},
                    {"type": "assistant", "message": {"id": "response-2", "model": "gateway-model", "usage": response_usage}},
                    {"type": "assistant", "message": {"id": "response-2", "model": "gateway-model", "usage": response_usage}},
                    {
                        "type": "assistant",
                        "is_api_error_message": True,
                        "message": {"id": "synthetic-error", "model": "<synthetic>", "usage": {}},
                    },
                ]
                transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()),
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                    mock.patch("aha_cli.services.backend_runtime._claude_session_jsonl_path", return_value=transcript),
                ):
                    start_backend(
                        root / ".aha",
                        run_id,
                        "main",
                        backend="claude",
                        model="env:test-gateway",
                        task_id="task-001",
                    )
                    write_json(
                        session_path(root / ".aha", run_id, "task-001", "main"),
                        {
                            "backend_session_id": "claude-session",
                            "backend": "claude",
                            "requested_model": "env:test-gateway",
                            "model": "gateway-model",
                        },
                    )
                    append_event(
                        root / ".aha",
                        run_id,
                        "agent_usage",
                        {
                            "task_id": "task-001",
                            "target": "main",
                            "usage": {
                                "input_tokens": 999_000,
                                "cache_read_input_tokens": 999_000,
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
        self.assertEqual(status["latest_usage"]["input_tokens"], 999_000)
        self.assertEqual(status["context_pressure"]["backend"], "claude")
        self.assertEqual(status["context_pressure"]["context_window"], 300_000)
        self.assertEqual(status["context_pressure"]["context_window_source"], "runtime")
        self.assertEqual(status["context_pressure"]["input_tokens"], 6000)
        self.assertEqual(status["context_pressure"]["runtime_effective_input_tokens"], 6000)
        self.assertEqual(status["context_pressure"]["pressure_source"], "runtime.last_token_usage.effective_input_tokens")
        self.assertEqual(status["context_pressure"]["percent"], 2.0)

    def test_backend_status_scans_event_log_once_for_activity_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Runtime event scan", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(root / ".aha", run_id, "agent_started", {"task_id": "task-001", "target": "main"})
                append_event(root / ".aha", run_id, "message", {"task_id": "task-001", "sender": "main", "message": "reply"})
                append_event(
                    root / ".aha",
                    run_id,
                    "agent_usage",
                    {"task_id": "task-001", "target": "main", "usage": {"input_tokens": 123}},
                )
                append_event(
                    root / ".aha",
                    run_id,
                    "agent_prompt_metrics",
                    {"task_id": "task-001", "target": "main", "total": {"tokens": 45}},
                )

                with (
                    mock.patch("aha_cli.services.backend_runtime._discover_backend_process", return_value=None),
                    mock.patch(
                        "aha_cli.services.backend_runtime.iter_jsonl_reverse",
                        wraps=backend_runtime_module.iter_jsonl_reverse,
                    ) as reverse_events,
                ):
                    status = backend_status(root / ".aha", run_id, "main", task_id="task-001")

        self.assertEqual(reverse_events.call_count, 1)
        self.assertEqual(status["latest_usage"]["input_tokens"], 123)
        self.assertEqual(status["latest_prompt_metrics"]["total"]["tokens"], 45)
        self.assertIsNotNone(status["last_started_at"])
        self.assertIsNotNone(status["last_reply_at"])

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
                # Claude env groups are a named list; the selected group is
                # activated by passing ``env:<name>`` as the model.
                cfg["claude"]["env"] = [
                    {
                        "name": "work",
                        "ANTHROPIC_API_KEY": "test-key",
                        "ANTHROPIC_BASE_URL": "https://claude.test",
                        "ANTHROPIC_MODEL": "kimi-k2.6",
                    }
                ]
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
                    start_backend(root / ".aha", run_id, "main", backend="claude", model="env:work", task_id="task-001")

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

    def test_start_backend_uses_official_claude_model_without_env_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                cfg_path = root / ".aha" / "config.json"
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg["claude"]["model"] = "claude-sonnet-4-6"
                cfg["claude"]["env_active"] = "work"
                cfg["claude"]["env"] = [
                    {
                        "name": "work",
                        "ANTHROPIC_API_KEY": "work-key",
                        "ANTHROPIC_BASE_URL": "https://claude.test",
                        "ANTHROPIC_MODEL": "kimi-k2.6",
                    }
                ]
                cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
                code, plan_output = self.run_cli("plan", "Claude official model", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4244

                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    status = start_backend(root / ".aha", run_id, "main", backend="claude", task_id="task-001")

        env = popen.call_args.kwargs["env"]
        command = popen.call_args.args[0]
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ANTHROPIC_MODEL", env)
        self.assertIn("--model", command)
        self.assertEqual(command[command.index("--model") + 1], "claude-sonnet-4-6")
        self.assertEqual(status["requested_model"], "claude-sonnet-4-6")
        self.assertEqual(status["resolved_model"], "claude-sonnet-4-6")

    def test_claude_exec_reports_missing_custom_env_auth_before_cli(self) -> None:
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
                    claude_config={
                        "env_active": "work",
                        "env": [
                            {
                                "name": "work",
                                "ANTHROPIC_BASE_URL": "https://claude.test",
                                "ANTHROPIC_MODEL": "kimi-k2.6",
                            }
                        ],
                    },
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

    def test_start_backend_uses_proxy_for_selected_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "claude")
                write_json(
                    root / ".aha" / "config.json",
                    {
                        "backend": "claude",
                        "codex": {"proxy": {"http_proxy": "http://codex.proxy:7890"}},
                        "claude": {
                            "proxy": {
                                "http_proxy": "http://claude.proxy:7890",
                                "https_proxy": "http://claude.proxy:7890",
                                "no_proxy": "localhost,127.0.0.1",
                            }
                        },
                    },
                )
                code, plan_output = self.run_cli("plan", "Backend proxy by provider", "--agents", "1", "--enable-proxy")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()) as popen,
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root / ".aha", run_id, "main", backend="claude", task_id="task-001")

        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["HTTP_PROXY"], "http://claude.proxy:7890")
        self.assertEqual(env["HTTPS_PROXY"], "http://claude.proxy:7890")
        self.assertNotEqual(env["HTTP_PROXY"], "http://codex.proxy:7890")

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

    def test_mark_backend_stopped_accepts_wsl_worker_self_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "WSL self stop", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

            backend_runtime = backend_runtime_module
            state = {
                "target": "main",
                "task_id": "task-001",
                "backend": "claude-chat",
                "status": "running",
                "pid": 4242,
                "wsl_distro": "Ubuntu-24.04",
                "wsl_native_home": "/home/kaikai",
            }
            backend_runtime._write_state(root / ".aha", run_id, "main", state, "task-001")

            # The worker runs inside the distro: its own pid lives in a different
            # namespace than the recorded Windows-side wsl.exe pid (4242), so a
            # raw pid comparison would wrongly reject the stop. The caller's
            # cmdline matches the backend worker signature instead.
            with (
                # The recorded wsl.exe host is still alive while the worker is
                # calling self-stop, so pid_is_running must return True for the
                # state pid to reach the cmdline-signature branch (the bug).
                mock.patch(
                    "aha_cli.services.backend_runtime.pid_is_running",
                    side_effect=lambda pid: pid == 4242,
                ),
                mock.patch(
                    "aha_cli.services.backend_runtime._pid_is_backend_worker",
                    return_value=True,
                ) as pid_check,
            ):
                result = backend_runtime.mark_backend_stopped(
                    root / ".aha",
                    run_id,
                    "main",
                    task_id="task-001",
                    pid=31337,
                )

        self.assertEqual(result["status"], "stopped")
        self.assertNotIn("stale_stop_ignored", result)
        pid_check.assert_called_once()
        self.assertEqual(pid_check.call_args.args[:3], (31337, run_id, "main"))

    def test_mark_backend_stopped_rejects_unrelated_process_with_live_state_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Stale stop", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

            backend_runtime = backend_runtime_module
            state = {
                "target": "main",
                "task_id": "task-001",
                "backend": "claude-chat",
                "status": "running",
                "pid": 4242,
            }
            backend_runtime._write_state(root / ".aha", run_id, "main", state, "task-001")

            # A stale process (different pid, state pid still alive, and the
            # caller does not carry the backend worker signature) must NOT mark
            # the current backend stopped.
            with mock.patch(
                "aha_cli.services.backend_runtime.pid_is_running",
                side_effect=lambda pid: pid == 4242,
            ):
                result = backend_runtime.mark_backend_stopped(
                    root / ".aha",
                    run_id,
                    "main",
                    task_id="task-001",
                    pid=31337,
                )

        self.assertEqual(result["status"], "running")
        self.assertTrue(result["stale_stop_ignored"])
