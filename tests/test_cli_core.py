from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest import mock

from aha_cli.cli import append_message, main, task_dashboard_html, task_snapshot
from aha_cli.services.chat import chat_prompt
from aha_cli.services.commit_policy import (
    format_commit_message,
    generated_by_for_backend_model,
    validate_commit_message,
)
from aha_cli.services.orchestrator import task_assignment_prompt
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.io import write_json
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
    set_task_status,
    status_snapshot,
    task_context_snapshot,
    task_final_snapshot,
    update_agent_config,
    update_agent_runtime,
    update_task_supervision_config,
    update_task_proxy_config,
    write_task_result,
)
from aha_cli.web.server import format_agent_command, format_aha_command, handle_slash_command, workspace_options
from tests.helpers import isolated_cli_environment


def write_cleanup_plan(run_path: Path, run_id: str, *, temporary: bool = False) -> None:
    data = {
        "id": run_id,
        "goal": "Cleanup CLI test",
        "mode": "research",
        "created_at": "2026-05-30T00:00:00+00:00",
        "updated_at": "2026-05-30T00:00:00+00:00",
        "write_scopes": [],
        "tasks": [],
    }
    if temporary:
        data["temporary"] = True
    write_json(run_path / "plan.json", data)


def touch_tree(path: Path, mtime: float) -> None:
    for item in [path, *path.rglob("*")]:
        os.utime(item, (mtime, mtime))


class CliCoreTests(unittest.TestCase):
    def run_cli(self, *args: str, allow_aha_keys: set[str] | None = None) -> tuple[int, str]:
        out = io.StringIO()
        with isolated_cli_environment(allow_aha_keys=allow_aha_keys or ()), mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_global_version_option_reports_app_version(self) -> None:
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AHA_VERSION": "20260531.test"}), mock.patch("sys.stdout", out):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), "aha 20260531.test")

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

    def test_runs_cleanup_dry_run_lists_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            tmp_root = Path(tmp) / "tmp-root"
            tmp_root.mkdir()
            run_path = home / "runs" / "temp-run"
            write_cleanup_plan(run_path, "temp-run", temporary=True)
            (run_path / ".aha-temp-run").write_text("", encoding="utf-8")
            touch_tree(run_path, 1000)

            code, output = self.run_cli(
                "--home",
                str(home),
                "runs",
                "cleanup",
                "--dry-run",
                "--json",
                "--tmp-root",
                str(tmp_root),
                "--stale-seconds",
                "60",
            )
            payload = json.loads(output)

            self.assertEqual(code, 0)
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["deleted"][0]["action"], "would_delete")
            self.assertTrue(run_path.exists())

    def test_runs_cleanup_apply_deletes_stale_temp_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            tmp_root = Path(tmp) / "tmp-root"
            tmp_root.mkdir()
            run_path = home / "runs" / "temp-run"
            write_cleanup_plan(run_path, "temp-run", temporary=True)
            (run_path / ".aha-temp-run").write_text("", encoding="utf-8")
            touch_tree(run_path, 1000)

            code, output = self.run_cli(
                "--home",
                str(home),
                "runs",
                "cleanup",
                "--apply",
                "--json",
                "--tmp-root",
                str(tmp_root),
                "--stale-seconds",
                "60",
            )
            payload = json.loads(output)

            self.assertEqual(code, 0)
            self.assertFalse(payload["dry_run"])
            self.assertEqual(payload["deleted"][0]["action"], "deleted")
            self.assertFalse(run_path.exists())

    def test_runs_cleanup_rejects_non_temp_scan_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"

            code, output = self.run_cli(
                "--home",
                str(home),
                "runs",
                "cleanup",
                "--dry-run",
                "--json",
                "--tmp-root",
                str(Path.cwd()),
            )
            payload = json.loads(output)

            self.assertEqual(code, 1)
            self.assertEqual(payload["errors"][0]["reason"], "unsafe_tmp_root")

    def test_runs_delete_removes_non_current_run_and_rejects_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            old_run = home / "runs" / "old-run"
            current_run = home / "runs" / "current-run"
            write_cleanup_plan(old_run, "old-run")
            write_cleanup_plan(current_run, "current-run")

            delete_code, delete_output = self.run_cli(
                "--home",
                str(home),
                "runs",
                "delete",
                "old-run",
                "--json",
                "--current-run",
                "current-run",
            )
            current_code, _ = self.run_cli(
                "--home",
                str(home),
                "runs",
                "delete",
                "current-run",
                "--force",
                "--current-run",
                "current-run",
            )
            payload = json.loads(delete_output)

            self.assertEqual(delete_code, 0)
            self.assertEqual(payload["deleted"]["run_id"], "old-run")
            self.assertFalse(old_run.exists())
            self.assertEqual(current_code, 2)
            self.assertTrue(current_run.exists())

    def test_runs_delete_force_removes_active_heartbeat_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            run_path = home / "runs" / "active-run"
            write_cleanup_plan(run_path, "active-run")
            log = run_path / "logs" / "realtime-debug.log"
            log.parent.mkdir(parents=True)
            log.write_text('{"phase":"heartbeat_sent"}\n', encoding="utf-8")

            blocked_code, _ = self.run_cli("--home", str(home), "runs", "delete", "active-run")
            forced_code, forced_output = self.run_cli("--home", str(home), "runs", "delete", "active-run", "--force", "--json")
            payload = json.loads(forced_output)

            self.assertEqual(blocked_code, 2)
            self.assertEqual(forced_code, 0)
            self.assertEqual(payload["deleted"]["reason"], "forced")
            self.assertFalse(run_path.exists())

    def test_runs_retention_reports_run_usage_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            run_path = home / "runs" / "run-001"
            write_cleanup_plan(run_path, "run-001")
            log = run_path / "logs" / "backend.log"
            log.parent.mkdir(parents=True)
            log.write_text("backend log", encoding="utf-8")

            code, output = self.run_cli("--home", str(home), "runs", "retention", "run-001", "--json", "--top", "1")
            payload = json.loads(output)

            self.assertEqual(code, 0)
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["run_id"], "run-001")
            self.assertTrue(run_path.exists())
            self.assertEqual(payload["largest_files"][0]["path"], "plan.json")

    def test_runs_retention_apply_archives_and_force_compacts_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            archive_dir = Path(tmp) / "archives"
            run_path = home / "runs" / "run-001"
            write_cleanup_plan(run_path, "run-001")
            log = run_path / "logs" / "backend.log"
            prompt = run_path / "prompts" / "main.md"
            chat = run_path / "chat" / "main.md"
            for path, text in ((log, "log"), (prompt, "prompt"), (chat, "chat")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")

            apply_code, apply_output = self.run_cli(
                "--home",
                str(home),
                "runs",
                "retention",
                "run-001",
                "--apply",
                "--json",
                "--archive-dir",
                str(archive_dir),
            )
            force_code, force_output = self.run_cli(
                "--home",
                str(home),
                "runs",
                "retention",
                "run-001",
                "--apply",
                "--force",
                "--json",
                "--archive-dir",
                str(archive_dir),
            )
            force_payload = json.loads(force_output)
            archive_path = Path(json.loads(apply_output)["archive"]["path"])

            self.assertEqual(apply_code, 0)
            self.assertEqual(force_code, 0)
            self.assertTrue(archive_path.exists())
            self.assertFalse(log.exists())
            self.assertFalse(prompt.exists())
            self.assertTrue(chat.exists())
            self.assertEqual({item["path"] for item in force_payload["deleted"]}, {"logs/backend.log", "prompts/main.md"})

            list_code, list_output = self.run_cli(
                "--home",
                str(home),
                "runs",
                "retention-archive",
                "list",
                "run-001",
                "--archive-dir",
                str(archive_dir),
                "--json",
            )
            inspect_code, inspect_output = self.run_cli(
                "--home",
                str(home),
                "runs",
                "retention-archive",
                "inspect",
                str(archive_path),
                "--json",
            )
            restore_code, restore_output = self.run_cli(
                "--home",
                str(home),
                "runs",
                "retention-archive",
                "restore",
                str(archive_path),
                "--json",
            )
            list_payload = json.loads(list_output)
            inspect_payload = json.loads(inspect_output)
            restore_payload = json.loads(restore_output)

            self.assertEqual(list_code, 0)
            self.assertEqual(inspect_code, 0)
            self.assertEqual(restore_code, 0)
            self.assertEqual(list_payload["archives"][0]["source_run_id"], "run-001")
            self.assertEqual(inspect_payload["file_count"], 2)
            self.assertEqual({item["path"] for item in restore_payload["restored"]}, {"logs/backend.log", "prompts/main.md"})
            self.assertTrue(log.exists())
            self.assertTrue(prompt.exists())

    def test_runs_retention_apply_rejects_current_and_force_without_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            run_path = home / "runs" / "current-run"
            write_cleanup_plan(run_path, "current-run")

            force_code, _ = self.run_cli("--home", str(home), "runs", "retention", "current-run", "--force")
            current_code, _ = self.run_cli(
                "--home",
                str(home),
                "runs",
                "retention",
                "current-run",
                "--apply",
                "--force",
                "--current-run",
                "current-run",
            )

            self.assertEqual(force_code, 2)
            self.assertEqual(current_code, 2)
            self.assertTrue(run_path.exists())

    def test_runs_retention_policy_reports_and_applies_only_safe_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            archive_dir = Path(tmp) / "archives"
            old_run = home / "runs" / "old-run"
            current_run = home / "runs" / "current-run"
            write_cleanup_plan(old_run, "old-run")
            write_cleanup_plan(current_run, "current-run")
            old_log = old_run / "logs" / "backend.log"
            current_log = current_run / "logs" / "backend.log"
            for path, text in ((old_log, "large-old-log"), (current_log, "large-current-log")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")

            report_code, report_output = self.run_cli(
                "--home",
                str(home),
                "runs",
                "retention-policy",
                "--current-run",
                "current-run",
                "--max-candidate-bytes",
                "1",
                "--json",
            )
            apply_code, apply_output = self.run_cli(
                "--home",
                str(home),
                "runs",
                "retention-policy",
                "--current-run",
                "current-run",
                "--max-candidate-bytes",
                "1",
                "--apply-if-over-limit",
                "--force",
                "--archive-dir",
                str(archive_dir),
                "--json",
            )
            report_payload = json.loads(report_output)
            apply_payload = json.loads(apply_output)
            applied_runs = {item["run_id"]: item for item in apply_payload["runs"]}

            self.assertEqual(report_code, 0)
            self.assertEqual(apply_code, 0)
            self.assertTrue(report_payload["dry_run"])
            self.assertEqual(report_payload["summary"]["eligible_runs"], 1)
            self.assertFalse(old_log.exists())
            self.assertTrue(current_log.exists())
            self.assertIsNotNone(applied_runs["old-run"]["archive"])
            self.assertIsNone(applied_runs["current-run"]["archive"])
            self.assertEqual(applied_runs["current-run"]["guard"]["reason"], "current_run")

    def test_runs_retention_policy_can_persist_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            report_dir = Path(tmp) / "reports"
            run_path = home / "runs" / "run-001"
            write_cleanup_plan(run_path, "run-001")
            log = run_path / "logs" / "backend.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("large-log", encoding="utf-8")

            code, output = self.run_cli(
                "--home",
                str(home),
                "runs",
                "retention-policy",
                "--max-candidate-bytes",
                "1",
                "--write-report",
                "--report-dir",
                str(report_dir),
                "--json",
            )
            payload = json.loads(output)
            scheduled = payload["scheduled_report"]

            self.assertEqual(code, 0)
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["summary"]["over_limit_runs"], 1)
            self.assertTrue(Path(scheduled["path"]).exists())
            self.assertTrue((report_dir / "latest.json").exists())

    def test_runs_diagnose_json_uses_read_only_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            diagnostic = {
                "aha_home": str(home),
                "current_run_id": "current-run",
                "visible_runs": [],
                "active_heartbeat_runs": [],
                "runs": [],
                "cleanup": {"candidates": []},
                "services": {"listeners": [], "processes": [], "service_units": []},
            }
            with mock.patch("aha_cli.cli.diagnose_runs", return_value=diagnostic) as diagnose:
                code, output = self.run_cli(
                    "--home",
                    str(home),
                    "runs",
                    "diagnose",
                    "--json",
                    "--current-run",
                    "current-run",
                    "--stale-seconds",
                    "60",
                    "--active-heartbeat-seconds",
                    "30",
                )

            payload = json.loads(output)

            self.assertEqual(code, 0)
            self.assertEqual(payload["current_run_id"], "current-run")
            diagnose.assert_called_once()
            args, kwargs = diagnose.call_args
            self.assertEqual(args[0], home)
            self.assertEqual(kwargs["current_run_id"], "current-run")
            self.assertEqual(kwargs["stale_seconds"], 60)
            self.assertEqual(kwargs["active_heartbeat_seconds"], 30)

    def test_runs_diagnose_text_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            diagnostic = {
                "aha_home": str(home),
                "current_run_id": "current-run",
                "visible_runs": [{"id": "current-run"}],
                "active_heartbeat_runs": ["current-run"],
                "runs": [
                    {
                        "run_id": "current-run",
                        "lifecycle_status": "active",
                        "active_heartbeat": True,
                        "cleanup": {"dry_run_action": "protect", "reason": "current_run"},
                    }
                ],
                "cleanup": {"candidates": []},
                "services": {
                    "listeners": [{"port": "8788", "process": "python3", "pid": "123"}],
                    "processes": [{"pid": "123", "stat": "Sl", "command": "python3 -m aha_cli ui"}],
                    "service_units": [{"unit": "aha.service", "active": "active", "sub": "running"}],
                },
            }
            with mock.patch("aha_cli.cli.diagnose_runs", return_value=diagnostic):
                code, output = self.run_cli("--home", str(home), "runs", "diagnose")

            self.assertEqual(code, 0)
            self.assertIn("AHA runs diagnose", output)
            self.assertIn("current_run: current-run", output)
            self.assertIn("current-run: protect (current_run)", output)

    def test_runs_recover_dry_run_and_apply_stale_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recover stale runtime", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")

                stopped = {"status": "stopped", "pid": None, "backend": "codex"}
                with mock.patch("aha_cli.services.run_recovery.backend_status", return_value=stopped):
                    dry_code, dry_output = self.run_cli("runs", "recover", run_id, "--json")
                with (
                    mock.patch("aha_cli.services.run_recovery.backend_status", return_value=stopped),
                    mock.patch("aha_cli.web.status.backend_status", return_value=stopped),
                ):
                    apply_code, apply_output = self.run_cli(
                        "runs",
                        "recover",
                        run_id,
                        "--task-id",
                        "task-001",
                        "--agent-id",
                        "main",
                        "--apply",
                        "--json",
                    )
                task = task_snapshot(root, run_id, "task-001")["task"]

            dry_payload = json.loads(dry_output)
            apply_payload = json.loads(apply_output)

        self.assertEqual(dry_code, 0)
        self.assertTrue(dry_payload["dry_run"])
        self.assertEqual(dry_payload["candidates"][0]["agent_id"], "main")
        self.assertEqual(apply_code, 0)
        self.assertEqual(apply_payload["recovered_count"], 1)
        self.assertEqual(task["status"], "awaiting_user")
        self.assertEqual(task["agents"][0]["status"], "interrupted")

    def test_runs_recover_apply_requires_exact_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recover stale runtime", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")
                with mock.patch("aha_cli.services.run_recovery.backend_status", return_value={"status": "stopped"}):
                    apply_code, _ = self.run_cli("runs", "recover", run_id, "--apply")

        self.assertEqual(apply_code, 2)

    def test_task_recover_dry_run_and_apply_stale_reopened_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recover reopened host", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="codex",
                    real_agent_enabled=True,
                )
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "waiting", waiting_reason="host")
                set_agent_status(root, run_id, "task-001", "host", "running")

                stopped = {"status": "stopped", "pid": None, "backend": "codex"}
                with mock.patch("aha_cli.services.run_recovery.backend_status", return_value=stopped):
                    dry_code, dry_output = self.run_cli("task", "recover", run_id, "task-001", "--json")
                with (
                    mock.patch("aha_cli.services.run_recovery.backend_status", return_value=stopped),
                    mock.patch("aha_cli.web.status.backend_status", return_value=stopped),
                ):
                    apply_code, apply_output = self.run_cli("task", "recover", run_id, "task-001", "--apply", "--json")
                task = task_snapshot(root, run_id, "task-001")["task"]
                main_agent = next(agent for agent in task["agents"] if agent["id"] == "main")
                host_agent = next(agent for agent in task["agents"] if agent["id"] == "host")

            dry_payload = json.loads(dry_output)
            apply_payload = json.loads(apply_output)

        self.assertEqual(dry_code, 0)
        self.assertEqual(dry_payload["candidates"][0]["task_status"], "awaiting_user")
        self.assertEqual(dry_payload["candidates"][0]["agent_id"], "host")
        self.assertEqual(apply_code, 0)
        self.assertEqual(apply_payload["recovered_count"], 1)
        self.assertEqual(task["status"], "awaiting_user")
        self.assertEqual(host_agent["status"], "interrupted")
        self.assertEqual(main_agent["status"], "completed")

    def test_runs_lifecycle_hides_archives_and_restores_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            run_path = home / "runs" / "old-run"
            write_cleanup_plan(run_path, "old-run")

            hide_code, hide_output = self.run_cli(
                "--home",
                str(home),
                "runs",
                "lifecycle",
                "old-run",
                "hidden",
                "--json",
                "--current-run",
                "current-run",
            )
            archive_code, _ = self.run_cli(
                "--home",
                str(home),
                "runs",
                "lifecycle",
                "old-run",
                "archived",
                "--current-run",
                "current-run",
            )
            restore_code, restore_output = self.run_cli(
                "--home",
                str(home),
                "runs",
                "lifecycle",
                "old-run",
                "active",
                "--current-run",
                "current-run",
            )
            hidden_payload = json.loads(hide_output)
            plan = read_json(run_path / "plan.json")

            self.assertEqual(hide_code, 0)
            self.assertEqual(hidden_payload["run"]["lifecycle_status"], "hidden")
            self.assertEqual(archive_code, 0)
            self.assertEqual(restore_code, 0)
            self.assertIn("old-run lifecycle=active", restore_output)
            self.assertEqual(plan["lifecycle_status"], "active")
            self.assertFalse(plan["hidden"])
            self.assertFalse(plan["archived"])

    def test_runs_lifecycle_allows_idle_current_heartbeat_and_rejects_running_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            write_cleanup_plan(home / "runs" / "current-run", "current-run")
            write_cleanup_plan(home / "runs" / "old-run", "old-run")
            running_code, running_output = self.run_cli(
                "--home",
                str(home),
                "plan",
                "Running run",
                "--agents",
                "1",
            )
            self.assertEqual(running_code, 0)
            running_run_id = next(line.split(": ", 1)[1] for line in running_output.splitlines() if line.startswith("Created run:"))
            set_task_status(home, running_run_id, "task-001", "running")
            set_agent_status(home, running_run_id, "task-001", "main", "running")

            current_code, _ = self.run_cli(
                "--home",
                str(home),
                "runs",
                "lifecycle",
                "current-run",
                "hidden",
                "--current-run",
                "current-run",
            )
            missing_code, _ = self.run_cli("--home", str(home), "runs", "lifecycle", "missing-run", "hidden")
            heartbeat_log = home / "runs" / "old-run" / "logs" / "backend.log"
            heartbeat_log.parent.mkdir(parents=True, exist_ok=True)
            heartbeat_log.write_text('{"phase":"heartbeat_sent"}\n', encoding="utf-8")
            heartbeat_code, _ = self.run_cli("--home", str(home), "runs", "lifecycle", "old-run", "archived")
            active_code, _ = self.run_cli("--home", str(home), "runs", "lifecycle", running_run_id, "hidden")

            self.assertEqual(current_code, 0)
            self.assertEqual(missing_code, 2)
            self.assertEqual(heartbeat_code, 0)
            self.assertEqual(active_code, 2)

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

    def test_ui_does_not_initialize_config_before_bootstrap_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            run_ui = mock.AsyncMock()
            with mock.patch.dict(os.environ, {"AHA_HOME": str(home)}), mock.patch("aha_cli.cli.run_ui_server", run_ui):
                code, _ = self.run_cli("ui", "--host", "127.0.0.1", "--port", "0")

                self.assertEqual(code, 0)
                self.assertFalse((home / "config.json").exists())
                run_ui.assert_awaited_once()

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
        async def fake_ui_server(root: Path, run_id: str, host: str, port: int, poll_interval: int, auth_token: str = "") -> None:
            calls.append((root, run_id, host, port, poll_interval, auth_token))

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            calls: list[tuple[Path, str, str, int, int]] = []
            with mock.patch("aha_cli.cli.run_ui_server", side_effect=fake_ui_server):
                code, _ = self.run_cli("--home", str(home), "ui", "--host", "127.0.0.1", "--port", "0")

            self.assertEqual(code, 0)
            self.assertFalse((home / "config.json").exists())
            self.assertEqual(calls, [(home, "", "127.0.0.1", 0, 1000, "")])

    def test_empty_command_defaults_to_ui(self) -> None:
        async def fake_ui_server(root: Path, run_id: str, host: str, port: int, poll_interval: int, auth_token: str = "") -> None:
            calls.append((root, run_id, host, port, poll_interval, auth_token))

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            calls: list[tuple[Path, str, str, int, int]] = []
            with mock.patch("aha_cli.cli.run_ui_server", side_effect=fake_ui_server):
                code, _ = self.run_cli("--home", str(home))

            self.assertEqual(code, 0)
            self.assertFalse((home / "config.json").exists())
            self.assertEqual(calls, [(home, "", "127.0.0.1", 8766, 1000, "")])

    def test_ui_reads_auth_token_file(self) -> None:
        async def fake_ui_server(root: Path, run_id: str, host: str, port: int, poll_interval: int, auth_token: str = "") -> None:
            calls.append((root, run_id, host, port, poll_interval, auth_token))

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aha-home"
            token_file = Path(tmp) / "web-token"
            token_file.write_text("secret-token\n", encoding="utf-8")
            calls: list[tuple[Path, str, str, int, int, str]] = []
            with mock.patch("aha_cli.cli.run_ui_server", side_effect=fake_ui_server):
                code, _ = self.run_cli(
                    "--home",
                    str(home),
                    "ui",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--auth-token-file",
                    str(token_file),
                )

            self.assertEqual(code, 0)
            self.assertEqual(calls, [(home, "", "127.0.0.1", 0, 1000, "secret-token")])

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

    def test_hardware_io_command_records_timeline_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable")
                code, plan_output = self.run_cli("plan", "Hardware I/O helper", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                code, output = self.run_cli(
                    "hardware-io",
                    run_id,
                    "task-001",
                    "--agent-id",
                    "main",
                    "--channel",
                    "uart",
                    "--endpoint",
                    "/dev/ttyUSB0@115200",
                    "--direction",
                    "rx",
                    "--data",
                    "Sgs #",
                    "--json",
                )
                record = json.loads(output)
                hardware_rows, _ = iter_jsonl_from(root / ".aha" / "runs" / run_id / "tasks" / "task-001" / "hardware_io.jsonl", 0)
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)

        self.assertEqual(code, 0)
        self.assertEqual(record["task_id"], "task-001")
        self.assertEqual(record["channel"], "uart")
        self.assertEqual(record["direction"], "rx")
        self.assertEqual(record["data"], "Sgs #")
        self.assertEqual(hardware_rows[0]["endpoint"], "/dev/ttyUSB0@115200")
        hardware_events = [event for event in events if event["type"] == "hardware_io"]
        self.assertEqual(hardware_events[-1]["data"]["offset"], record["offset"])

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
                "preferred_sub_model": "env:work",
                "workflow_template": "fault-debug",
            }
        )
        self.assertIn("Preferred sub-agent model:", assignment_prompt)
        self.assertIn("env:work", assignment_prompt)
        self.assertIn("Commit ownership policy:", assignment_prompt)
        self.assertIn("route it to that sub-agent with `route_to_agent`", assignment_prompt)
        self.assertIn("Never ask a sub-agent to commit files outside its assignment", assignment_prompt)
        self.assertIn("Commit message policy:", assignment_prompt)
        self.assertIn("Generated-by: AHA Codex GPT-5.5", assignment_prompt)
        self.assertIn("Include a concise commit body", assignment_prompt)
        self.assertIn("last non-empty line", assignment_prompt)
        self.assertIn("Keep task, agent, and scope tracking in the AHA journal", assignment_prompt)
        self.assertNotIn("AHA-Task: task-001", assignment_prompt)
        self.assertIn("return ONLY one JSON object", assignment_prompt)
        self.assertIn('"actions"', assignment_prompt)
        self.assertIn("Completed, stopped, failed, interrupted, or blocked", assignment_prompt)
        self.assertIn("Include a stable `scope_id`", assignment_prompt)
        self.assertIn("For a brand-new sub-agent, omit `agent_id`", assignment_prompt)
        self.assertIn("Include `agent_id` in `spawn_sub` only when intentionally reusing", assignment_prompt)
        self.assertIn("Collaboration mode:", assignment_prompt)
        self.assertIn("Workflow template:", assignment_prompt)
        self.assertIn("fault-debug", assignment_prompt)
        self.assertIn("Fault debug:", assignment_prompt)
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
                self.assertIn("Include a concise commit body", main_prompt)
                self.assertIn("--body <change-summary>", main_prompt)
                self.assertIn("Keep task, agent, and scope tracking in the AHA journal", main_prompt)
                self.assertNotIn("AHA-Task: task-001", main_prompt)
                self.assertNotIn("AHA-Agent: main", main_prompt)
                self.assertIn("aha commit --type <type>", main_prompt)
                self.assertIn("UI routing changes", main_prompt)
                self.assertIn("Completed, stopped, failed, interrupted, or blocked", main_prompt)
                self.assertIn("Include a stable `scope_id`", main_prompt)
                self.assertIn("Spawn/reassign format:", main_prompt)
                self.assertIn("- preferred_sub_model:", main_prompt)
                self.assertIn('"model": null', main_prompt)
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
                self.assertIn("last non-empty line", sub_prompt)
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
                    "--workflow-template",
                    "fault-debug",
                    "--no-dispatch",
                )
                self.assertEqual(code, 0)
                task = json.loads(task_output)

        self.assertEqual(task["collaboration_mode"], "pair")
        self.assertEqual(task["workflow_template"], "fault-debug")
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
        body_message = format_commit_message(
            "fix",
            "resolve staged diff root",
            scope="commit",
            body="- Resolve the Git root before inspecting staged changes.\n- Print git stderr when inspection fails.",
        )

        self.assertEqual(validate_commit_message(message), [])
        self.assertIn("feat(web): add lazy loading", message)
        self.assertIn("Generated-by: AHA Codex GPT-5.5", message)
        self.assertEqual(validate_commit_message(body_message), [])
        self.assertIn("- Resolve the Git root before inspecting staged changes.", body_message)
        self.assertTrue(body_message.rstrip().endswith("Generated-by: AHA Codex GPT-5.5"))
        self.assertEqual(generated_by_for_backend_model("claude", "env:kimi-k2.6"), "AHA Claude kimi-k2.6")
        self.assertEqual(generated_by_for_backend_model("claude", "env:MiniMax-M3"), "AHA Claude MiniMax-M3")
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
            "commit body Generated-by trailer must be the last non-empty line",
            validate_commit_message(
                "fix(web): generated trailer not last\n\n"
                "Generated-by: AHA Codex GPT-5.5\n\n"
                "- Late body line\n"
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
        self.assertIn(
            "commit body should not include unsupported trailers: Co-Authored-By",
            validate_commit_message(
                "fix(web): stray coauthor\n\n"
                "Generated-by: AHA Codex GPT-5.5\n"
                "Co-Authored-By: Claude Opus <claude@example.com>\n"
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
        code, body_output = self.run_cli(
            "commit",
            "--type",
            "fix",
            "--scope",
            "web",
            "--summary",
            "keep logs scroll stable",
            "--body",
            "- Preserve the selected log viewport.",
            "--body",
            "- Keep the generated trailer last.",
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertIn("- Preserve the selected log viewport.", body_output)
        self.assertIn("- Keep the generated trailer last.", body_output)
        self.assertTrue(body_output.rstrip().endswith("Generated-by: AHA Codex GPT-5.5"))
        with mock.patch.dict(os.environ, {"AHA_BACKEND": "codex", "AHA_MODEL": "gpt-5.4", "AHA_GENERATED_BY": ""}, clear=False):
            code, dynamic_output = self.run_cli(
                "commit",
                "--type",
                "fix",
                "--scope",
                "web",
                "--summary",
                "use task generator",
                "--dry-run",
                allow_aha_keys={"AHA_BACKEND", "AHA_MODEL", "AHA_GENERATED_BY"},
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
            body_file = Path(tmp) / "body.txt"
            message_file.write_text(message, encoding="utf-8")
            body_file.write_text("- Read commit details from a file.\n", encoding="utf-8")
            code, output = self.run_cli("commit-check", str(message_file))
            self.assertEqual(code, 0)
            expected_code, _ = self.run_cli("commit-check", "--generated-by", "AHA Codex GPT-5.4", str(message_file))
            body_file_code, body_file_output = self.run_cli(
                "commit",
                "--type",
                "docs",
                "--scope",
                "commit",
                "--summary",
                "document body files",
                "--body-file",
                str(body_file),
                "--dry-run",
            )
        self.assertIn("Commit message OK", output)
        self.assertEqual(expected_code, 1)
        self.assertEqual(body_file_code, 0)
        self.assertIn("- Read commit details from a file.", body_file_output)

    def test_commit_uses_git_root_when_parent_has_aha_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            (parent / ".aha").mkdir()
            repo = parent / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
            (repo / "file.txt").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)

            with mock.patch("pathlib.Path.cwd", return_value=repo):
                code, _ = self.run_cli(
                    "commit",
                    "--type",
                    "chore",
                    "--scope",
                    "test",
                    "--summary",
                    "use git root",
                    "--body",
                    "- Resolve the Git root before inspecting staged changes.",
                )

            self.assertEqual(code, 0)
            commit = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=repo, check=True, stdout=subprocess.PIPE, text=True)
            self.assertIn("chore(test): use git root", commit.stdout)
            self.assertIn("- Resolve the Git root before inspecting staged changes.", commit.stdout)
            self.assertIn("Generated-by: AHA Codex GPT-5.5", commit.stdout)
            self.assertTrue(commit.stdout.rstrip().endswith("Generated-by: AHA Codex GPT-5.5"))
            staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo)
            self.assertEqual(staged.returncode, 0)

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
                self.assertIn("task-visibility-filter", html)
                self.assertNotIn("show-hidden", html)
                self.assertIn('id="task-model"', html)
                self.assertIn('id="task-sandbox"', html)
                self.assertIn('id="task-approval"', html)
                self.assertNotIn('id="run-http-proxy"', html)
                bootstrap_script = Path(__file__).resolve().parents[1].joinpath("src/aha_cli/web/static/bootstrap_config.js").read_text(encoding="utf-8")
                self.assertIn('bootstrapProxyFieldsHtml("codex"', bootstrap_script)
                self.assertIn('bootstrapProxyFieldsHtml("claude"', bootstrap_script)
                self.assertIn('id="task-proxy-editor"', html)
                self.assertIn("selected-task-meta", html)
                self.assertIn("selected-agent-info", html)
                self.assertIn("backend-status", html)
                self.assertIn("pending-messages", html)
                self.assertIn("command-menu", html)
                self.assertIn("conversation-filters", html)
                self.assertIn('data-mobile-action="final"', html)

    def test_package_onebin_builds_executable_with_ui_static(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "aha"
            code, output = self.run_cli("package", "onebin", "--output", str(artifact))
            self.assertEqual(code, 0, output)
            self.assertTrue(artifact.is_file())
            self.assertTrue(os.access(artifact, os.X_OK))
            with zipfile.ZipFile(artifact) as archive:
                build_version = archive.read("aha_cli/_build_version.py").decode("utf-8")
            self.assertRegex(build_version, r"BUILD_VERSION = '\d{8}\.[0-9a-f]{7}'")

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

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/static/app_runtime_wiring.js", timeout=1) as response:
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

    def test_aha_slash_commands_are_limited_and_agent_command_forwards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Command help", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                output = format_aha_command(root, run_id, "task-001", "/aha status")
                self.assertIn("Unsupported AHA command", output)

                backend_output = format_aha_command(root, run_id, "task-001", "/aha backend status")
                self.assertIn("Unsupported AHA command", backend_output)

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
                self.assertTrue(any(message.get("sender") == "AHA" and "Unsupported AHA command" in message.get("message", "") for message in messages))

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("/aha final", payload["message"]["message"])
                self.assertIn("/aha reopen", payload["message"]["message"])
                self.assertIn("/aha interrupt", payload["message"]["message"])
                self.assertIn("/agent <command>", payload["message"]["message"])
                self.assertNotIn("/aha status", payload["message"]["message"])

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
            sticky_context="context",
            recovery_context="",
            recent_conversation="conversation",
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
