from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.services.run_recovery import RunRecoveryError, format_stale_runtime_recovery, run_stale_runtime_recovery
from aha_cli.store.filesystem import create_plan, event_path, inbox_path, set_agent_status, set_task_status, task_snapshot


def make_running_plan(root: Path) -> tuple[str, str, str]:
    plan = create_plan(root, "Recover stale runtime", 1, "research", [], [], backend="codex")
    run_id = plan["id"]
    task_id = plan["tasks"][0]["id"]
    agent_id = "main"
    set_task_status(root, run_id, task_id, "running")
    set_agent_status(root, run_id, task_id, agent_id, "running")
    return run_id, task_id, agent_id


def stopped_backend(_root: Path, _run_id: str, target: str, task_id: str | None) -> dict:
    return {
        "status": "stopped",
        "pid": None,
        "backend": "codex",
        "target": target,
        "task_id": task_id,
        "last_pid": 1234,
        "stopped_at": "2026-05-31T00:00:00+00:00",
    }


class RunRecoveryTests(unittest.TestCase):
    def test_recovery_dry_run_reports_stale_running_agent_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id, task_id, agent_id = make_running_plan(root)

            result = run_stale_runtime_recovery(root, run_id, backend_status_provider=stopped_backend)
            task = task_snapshot(root, run_id, task_id)["task"]
            text = format_stale_runtime_recovery(result)

            self.assertTrue(result["dry_run"])
            self.assertEqual(result["candidates"][0]["task_id"], task_id)
            self.assertEqual(result["candidates"][0]["agent_id"], agent_id)
            self.assertEqual(task["status"], "running")
            self.assertEqual(task["agents"][0]["status"], "running")
            self.assertIn("AHA stale runtime recovery (dry-run)", text)

    def test_recovery_apply_requires_exact_task_and_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id, _task_id, _agent_id = make_running_plan(root)

            with self.assertRaises(RunRecoveryError) as error:
                run_stale_runtime_recovery(root, run_id, apply=True, backend_status_provider=stopped_backend)

            self.assertEqual(error.exception.reason, "target_required")

    def test_recovery_apply_marks_agent_interrupted_and_task_awaiting_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id, task_id, agent_id = make_running_plan(root)

            with mock.patch("aha_cli.web.status.backend_status", side_effect=stopped_backend):
                result = run_stale_runtime_recovery(
                    root,
                    run_id,
                    task_id=task_id,
                    agent_id=agent_id,
                    apply=True,
                    backend_status_provider=stopped_backend,
                )
            task = task_snapshot(root, run_id, task_id)["task"]
            event_log = event_path(root, run_id).read_text(encoding="utf-8")

            self.assertFalse(result["dry_run"])
            self.assertEqual(result["recovered_count"], 1)
            self.assertEqual(task["status"], "awaiting_user")
            self.assertEqual(task["agents"][0]["status"], "interrupted")
            self.assertIn("recovery_context", task["agents"][0])
            self.assertIn("agent_status_recovered", event_log)

    def test_recovery_apply_rechecks_candidate_before_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id, task_id, agent_id = make_running_plan(root)

            with mock.patch("aha_cli.web.status.backend_status", return_value={"status": "running", "pid": 4321}):
                with self.assertRaises(RunRecoveryError) as error:
                    run_stale_runtime_recovery(
                        root,
                        run_id,
                        task_id=task_id,
                        agent_id=agent_id,
                        apply=True,
                        backend_status_provider=stopped_backend,
                    )
            task = task_snapshot(root, run_id, task_id)["task"]

            self.assertEqual(error.exception.reason, "candidate_changed")
            self.assertEqual(task["status"], "running")
            self.assertEqual(task["agents"][0]["status"], "running")

    def test_recovery_dry_run_includes_restart_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id, task_id, agent_id = make_running_plan(root)

            result = run_stale_runtime_recovery(root, run_id, backend_status_provider=stopped_backend)

            self.assertEqual(result["restart_plans"][0]["task_id"], task_id)
            self.assertEqual(result["restart_plans"][0]["agent_id"], agent_id)
            self.assertTrue(result["restart_plans"][0]["restartable"])
            self.assertEqual(result["restart_plans"][0]["backend"], "codex")

    def test_recovery_apply_can_enqueue_resume_and_restart_backend(self) -> None:
        def fake_starter(_root: Path, _run_id: str, target: str, **kwargs: object) -> dict:
            return {
                "status": "running",
                "started": True,
                "target": target,
                "task_id": kwargs.get("task_id"),
                "backend": kwargs.get("backend"),
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id, task_id, agent_id = make_running_plan(root)

            with mock.patch("aha_cli.web.status.backend_status", side_effect=stopped_backend):
                result = run_stale_runtime_recovery(
                    root,
                    run_id,
                    task_id=task_id,
                    agent_id=agent_id,
                    apply=True,
                    restart_backend=True,
                    backend_status_provider=stopped_backend,
                    backend_starter=fake_starter,
                )
            task = task_snapshot(root, run_id, task_id)["task"]
            inbox = inbox_path(root, run_id, agent_id).read_text(encoding="utf-8")
            event_log = event_path(root, run_id).read_text(encoding="utf-8")

            self.assertEqual(result["recovered_count"], 1)
            self.assertEqual(result["restart_count"], 1)
            self.assertEqual(result["restarted"][0]["backend"]["status"], "running")
            self.assertEqual(task["status"], "running")
            self.assertEqual(task["agents"][0]["status"], "pending")
            self.assertIn("AHA recovery restart requested", inbox)
            self.assertIn("agent_recovery_restart_requested", event_log)


if __name__ == "__main__":
    unittest.main()
