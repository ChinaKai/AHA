from __future__ import annotations

import io
import os
from pathlib import Path
import threading
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main, task_snapshot
from aha_cli.services.chat import _ensure_runtime_context_env, _refresh_backend_model_env, chat_offset_path
from aha_cli.store.config import load_config
from aha_cli.store.filesystem import inbox_path, iter_jsonl_from, read_json, run_dir, update_task_supervision_config
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import NAVIGATION_SLUG, init_knowledge_base, project_key, write_entry
from aha_cli.store.paths import config_path
from aha_cli.web.task_runtime import (
    message_backend_autostart_config,
    prepare_task_main_autostart,
    queue_backend_start,
    request_task_finalization_with_backend,
    start_dispatched_task_backend,
    start_prepared_backend,
)


class TaskRuntimeTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_queue_backend_start_reports_async_failure_to_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Runtime autostart failure", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                seen: list[str] = []
                notified = threading.Event()

                def on_failure(exc: BaseException) -> None:
                    seen.append(str(exc))
                    notified.set()

                autostart = {
                    "backend": "codex",
                    "target": "main",
                    "task_id": "task-001",
                    "model": "gpt-test",
                    "sandbox": "workspace-write",
                    "approval": "never",
                }
                with mock.patch(
                    "aha_cli.web.task_runtime._start_backend_from_autostart",
                    side_effect=RuntimeError("backend start failed"),
                ):
                    result = queue_backend_start(root, run_id, autostart, failure_callback=on_failure)

                self.assertTrue(result["queued"])
                self.assertTrue(notified.wait(2))
                self.assertEqual(seen, ["backend start failed"])

    def test_prepare_and_start_backend_uses_task_agent_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Runtime autostart", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "already processed", sender="browser", task_id="task-001", role="main")
                offset_file = chat_offset_path(run_dir(root, run_id), "main", "task-001")
                self.assertFalse(offset_file.exists())

                with mock.patch("aha_cli.web.task_runtime.backend_status", return_value={"status": "stopped"}):
                    autostart = prepare_task_main_autostart(root, run_id, "task-001")
                with mock.patch("aha_cli.web.task_runtime.start_backend", return_value={"status": "running"}) as start:
                    backend = start_prepared_backend(root, run_id, autostart)
                offset = read_json(offset_file)["offset"]
                inbox_size = inbox_path(root, run_id, "main", "task-001").stat().st_size

        self.assertEqual(autostart["backend"], "codex")
        self.assertEqual(autostart["target"], "main")
        self.assertEqual(autostart["task_id"], "task-001")
        self.assertEqual(offset, inbox_size)
        self.assertEqual(backend["status"], "running")
        start.assert_called_once()
        self.assertEqual(start.call_args.args[:3], (root, run_id, "main"))
        self.assertFalse(start.call_args.kwargs["from_start"])
        self.assertEqual(start.call_args.kwargs["task_id"], "task-001")

    def test_request_finalization_with_backend_starts_prepared_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Runtime final", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with (
                    mock.patch("aha_cli.web.task_runtime.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.task_runtime.start_backend", return_value={"status": "running", "started": True}) as start,
                ):
                    payload = request_task_finalization_with_backend(root, run_id, "task-001", "/aha final")
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main", "task-001"), 0)
                detail = task_snapshot(root, run_id, "task-001")

        self.assertIn("Finalization requested", payload["message"])
        self.assertEqual(payload["backend"]["status"], "running")
        start.assert_called_once()
        self.assertEqual(messages[-1]["result_policy"], "finalize")
        self.assertEqual(messages[-1]["original_command"], "/aha final")
        self.assertEqual(messages[-1]["final_context"]["source"], "task_journal")
        self.assertEqual(messages[-1]["final_context"]["journal_count"], 0)
        self.assertEqual(messages[-1]["final_context"]["to_at"], detail["task"]["coordination"]["final_summary_requested_at"])
        self.assertIn("AHA finalization request.", messages[-1]["message"])
        self.assertIn("Knowledge/nav feedback context:", messages[-1]["message"])
        # Knowledge base is enabled by default; nav index is not initialized in
        # this run so feedback stays inactive (no hidden sidecar).
        self.assertIn("knowledge_enabled: true", messages[-1]["message"])
        self.assertIn("Knowledge/nav feedback is not active", messages[-1]["message"])
        self.assertNotIn("Final source range:", messages[-1]["message"])
        self.assertNotIn("<aha_knowledge_candidates>", messages[-1]["message"])
        self.assertEqual(detail["task"]["status"], "running")
        self.assertTrue(detail["task"]["coordination"]["final_summary_requested_at"])

    def test_request_finalization_injects_nav_feedback_context_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Runtime nav final", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                cfg = load_config(root)
                cfg["knowledge"]["enabled"] = True
                cfg["knowledge"]["project_nav"]["enabled"] = True
                write_json(config_path(root), cfg)
                key = project_key(root, goal="Runtime nav final")
                init_knowledge_base(root, cfg)
                write_entry(
                    root,
                    config=cfg,
                    scope="project",
                    kind="navigation",
                    project_key_value=key,
                    title="Runtime nav",
                    body="## 项目介绍\nRuntime nav index.\n",
                    slug=NAVIGATION_SLUG,
                    meta={"type": "navigation"},
                )

                payload = request_task_finalization_with_backend(root, run_id, "task-001", "/aha final", autostart_backend=False)
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main", "task-001"), 0)

        prompt = messages[-1]["message"]
        self.assertIn("Finalization requested", payload["message"])
        self.assertIn("knowledge_enabled: true", prompt)
        self.assertIn("project_nav_enabled: true", prompt)
        self.assertIn("project_nav_index_exists: true", prompt)
        self.assertIn(f"project_key: {key}", prompt)
        self.assertIn("This is a byproduct of finalizing the current task, not a new task", prompt)
        self.assertIn("Do not inspect files, run commands, or broaden analysis only to maintain nav.", prompt)
        self.assertIn("<aha_knowledge_candidates>", prompt)

    def test_start_dispatched_task_backend_uses_from_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Runtime dispatch", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                task = task_snapshot(root, run_id, "task-001")["task"]

                with (
                    mock.patch("aha_cli.web.task_runtime.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.task_runtime.start_backend", return_value={"status": "running"}) as start,
                ):
                    skipped = start_dispatched_task_backend(root, run_id, task, False)
                    started = start_dispatched_task_backend(root, run_id, task, True)

        self.assertIsNone(skipped)
        self.assertEqual(started["status"], "running")
        start.assert_called_once()
        self.assertTrue(start.call_args.kwargs["from_start"])
        self.assertEqual(start.call_args.kwargs["task_id"], "task-001")

    def test_supervision_host_target_does_not_autostart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Runtime host", "--agents", "1")
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
                with mock.patch("aha_cli.web.task_runtime.backend_status", return_value={"status": "stopped"}) as backend_status:
                    autostart = message_backend_autostart_config(root, run_id, "task-001", "host")

        self.assertIsNone(autostart)
        backend_status.assert_not_called()

    def test_ensure_runtime_context_env_rebuilds_missing_context(self) -> None:
        # WSL watchers lose the service env at the wsl.exe hop; the chat
        # worker must rebuild the runtime context from its argv so CLI
        # helpers (aha send / aha commit / runs --current-run) keep working.
        with mock.patch.dict(os.environ, {}, clear=True):
            _ensure_runtime_context_env(Path("/mnt/c/Users/x/.aha"), "run-1", "sub-001", "codex", "task-001")

            self.assertEqual(os.environ["AHA_HOME"], "/mnt/c/Users/x/.aha")
            self.assertEqual(os.environ["AHA_ROOT"], "/mnt/c/Users/x/.aha")
            self.assertEqual(os.environ["AHA_RUN_ID"], "run-1")
            self.assertEqual(os.environ["AHA_AGENT_ID"], "sub-001")
            self.assertEqual(os.environ["AHA_TASK_ID"], "task-001")
            self.assertEqual(os.environ["AHA_BACKEND"], "codex")
            # Mirrors the Windows-side locale hardening: the onebin's pipes and
            # file I/O must stay UTF-8 even on a POSIX-locale distro.
            self.assertEqual(os.environ["PYTHONUTF8"], "1")

    def test_ensure_runtime_context_env_keeps_host_values_and_skips_empty_task(self) -> None:
        with mock.patch.dict(os.environ, {"AHA_RUN_ID": "host-run"}, clear=True):
            _ensure_runtime_context_env(Path("/aha"), "run-1", "main", "claude", None)

            # Windows-hosted watchers already carry the service values; the
            # rebuild must stay idempotent and never overwrite them.
            self.assertEqual(os.environ["AHA_RUN_ID"], "host-run")
            self.assertEqual(os.environ["AHA_BACKEND"], "claude")
            self.assertNotIn("AHA_TASK_ID", os.environ)

    def test_refresh_backend_model_env_tracks_generated_by(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            _refresh_backend_model_env("claude", "glm-5.3[1m]")

            self.assertEqual(os.environ["AHA_MODEL"], "glm-5.3[1m]")
            self.assertEqual(os.environ["AHA_GENERATED_BY"], "AHA Claude glm-5.3[1m]")

    def test_chat_worker_exports_runtime_context_for_cli_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Runtime context env", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "你好", sender="browser", task_id="task-001", role="main")

                with (
                    mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}, clear=True),
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "回复", None)) as run_exec,
                ):
                    code, _output = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")
                    self.assertEqual(code, 0)
                    # The watcher process env carries the context the spawned
                    # backend CLI (and its Bash-tool children) inherit.
                    self.assertEqual(os.environ["AHA_RUN_ID"], run_id)
                    self.assertEqual(os.environ["AHA_AGENT_ID"], "main")
                    self.assertEqual(os.environ["AHA_TASK_ID"], "task-001")
                    self.assertEqual(os.environ["AHA_BACKEND"], "codex")
                    self.assertEqual(os.environ["AHA_ROOT"], str(root / ".aha"))
                    # Pins home discovery so agent-shell aha calls never fall
                    # back to a cwd-walk .aha inside the workspace tree.
                    self.assertEqual(os.environ["AHA_HOME"], str(root / ".aha"))
                    self.assertTrue(os.environ["AHA_GENERATED_BY"].startswith("AHA Codex"))
            run_exec.assert_called_once()
