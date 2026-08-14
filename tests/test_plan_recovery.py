from __future__ import annotations

import asyncio
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from aha_cli.domain.models import make_task
from aha_cli.services.run_archive import _should_skip
from aha_cli.store.events import append_event
from aha_cli.store.io import exclusive_sidecar_lock, json_backup_path, read_json, write_json
from aha_cli.store.runs import list_run_summaries, save_plan
from tests.helpers import (
    fetch_ui_response,
    increment_plan_counter,
    increment_text_counter_with_sidecar,
    json_response_body,
)


def plan_data(run_id: str, *, goal: str = "Recoverable run", counter: int = 0) -> dict:
    return {
        "id": run_id,
        "goal": goal,
        "mode": "research",
        "created_at": "2026-08-14T00:00:00+00:00",
        "updated_at": "2026-08-14T00:00:00+00:00",
        "write_scopes": [],
        "tasks": [],
        "counter": counter,
    }


class PlanRecoveryTests(unittest.TestCase):
    def test_verified_json_write_restores_previous_value_after_verification_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            previous = {"id": "run-1", "status": "previous"}
            write_json(path, previous)

            with mock.patch("aha_cli.store.io.read_json", return_value={"status": "mismatch"}):
                with self.assertRaises(OSError):
                    write_json(
                        path,
                        {"id": "run-1", "status": "next"},
                        backup=True,
                        verify=True,
                    )

            self.assertEqual(read_json(path), previous)
            self.assertEqual(read_json(json_backup_path(path)), previous)

    def test_save_plan_keeps_previous_valid_plan_as_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            path = root / "runs" / "run-1" / "plan.json"
            previous = plan_data("run-1", counter=1)
            updated = plan_data("run-1", counter=2)
            write_json(path, previous)

            save_plan(root, updated)

            self.assertEqual(read_json(path), updated)
            self.assertEqual(read_json(json_backup_path(path)), previous)

    def test_run_list_restores_missing_plan_from_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            path = root / "runs" / "run-1" / "plan.json"
            previous = plan_data("run-1", goal="Backup goal", counter=1)
            write_json(path, previous)
            save_plan(root, plan_data("run-1", goal="New goal", counter=2))
            path.unlink()

            summaries = list_run_summaries(root)
            recovered = read_json(path)

            self.assertEqual([summary["id"] for summary in summaries], ["run-1"])
            self.assertEqual(recovered["goal"], "Backup goal")
            self.assertEqual(recovered["recovery"]["source"], "plan_backup")
            self.assertIn('"type": "plan_recovered"', (path.parent / "events.jsonl").read_text(encoding="utf-8"))

    def test_run_list_reconstructs_plan_from_task_snapshots_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            run_id = "run-2"
            run_path = root / "runs" / run_id
            task = make_task(
                "task-001",
                "Recovered task",
                "2026-08-14T00:01:00+00:00",
                backend="codex",
                workspace_path="E:\\workspace",
            )
            task["status"] = "completed"
            task_path = run_path / "tasks" / task["id"] / "task.json"
            write_json(task_path, task)
            append_event(
                root,
                run_id,
                "plan_created",
                {"goal": "Original goal", "mode": "implementation", "proxy_enabled": True},
                ts="2026-08-14T00:00:00+00:00",
            )
            append_event(
                root,
                run_id,
                "run_renamed",
                {"name": "Recovered goal"},
                ts="2026-08-14T00:02:00+00:00",
            )
            append_event(
                root,
                run_id,
                "run_lifecycle_updated",
                {"previous_status": "active", "status": "hidden"},
                ts="2026-08-14T00:03:00+00:00",
            )
            append_event(
                root,
                run_id,
                "run_selected_task_updated",
                {"selected_task_id": "task-001"},
                ts="2026-08-14T00:04:00+00:00",
            )

            summaries = list_run_summaries(root)
            recovered = read_json(run_path / "plan.json")

            self.assertEqual([summary["id"] for summary in summaries], [run_id])
            self.assertEqual(recovered["goal"], "Recovered goal")
            self.assertEqual(recovered["mode"], "implementation")
            self.assertTrue(recovered["proxy"]["enabled"])
            self.assertEqual(recovered["tasks"], [task])
            self.assertEqual(recovered["ui"]["selected_task_id"], "task-001")
            self.assertEqual(summaries[0]["lifecycle_status"], "hidden")
            self.assertEqual(recovered["recovery"]["source"], "task_snapshots")
            artifacts = list((run_path / "recovery").glob("plan.reconstructed-*.json"))
            self.assertEqual(len(artifacts), 1)

    def test_runs_api_keeps_recoverable_run_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            run_id = "run-api"
            task = make_task(
                "task-001",
                "API recovery",
                "2026-08-14T00:01:00+00:00",
                backend="codex",
            )
            write_json(root / "runs" / run_id / "tasks" / "task-001" / "task.json", task)
            append_event(
                root,
                run_id,
                "plan_created",
                {"goal": "API recovered run", "mode": "research"},
                ts="2026-08-14T00:00:00+00:00",
            )

            response = asyncio.run(fetch_ui_response(root, run_id, "/api/runs", timeout=2.0))
            body = json_response_body(response)

            self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
            self.assertIn(run_id, {item["id"] for item in body["runs"]})
            self.assertTrue((root / "runs" / run_id / "plan.json").is_file())

    def test_empty_run_reconstructs_from_plan_created_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            run_id = "empty-run"
            append_event(
                root,
                run_id,
                "plan_created",
                {"goal": "Empty recovered run", "mode": "research", "tasks": 0},
                ts="2026-08-14T00:00:00+00:00",
            )

            summaries = list_run_summaries(root)
            recovered = read_json(root / "runs" / run_id / "plan.json")

            self.assertEqual([summary["id"] for summary in summaries], [run_id])
            self.assertEqual(recovered["goal"], "Empty recovered run")
            self.assertEqual(recovered["tasks"], [])
            self.assertEqual(recovered["recovery"]["source"], "durable_events")

    def test_corrupt_plan_is_preserved_before_backup_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            path = root / "runs" / "run-3" / "plan.json"
            previous = plan_data("run-3", goal="Backup copy")
            write_json(path, previous)
            save_plan(root, plan_data("run-3", goal="Current copy"))
            path.write_text("{not-json", encoding="utf-8")

            list_run_summaries(root)

            self.assertEqual(read_json(path)["goal"], "Backup copy")
            invalid = list((path.parent / "recovery").glob("plan.invalid-*.json"))
            self.assertEqual(len(invalid), 1)
            self.assertEqual(invalid[0].read_text(encoding="utf-8"), "{not-json")

    def test_plan_lock_serializes_multiprocess_read_modify_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            run_id = "run-4"
            path = root / "runs" / run_id / "plan.json"
            write_json(path, plan_data(run_id))
            workers = [
                multiprocessing.Process(
                    target=increment_plan_counter,
                    args=(str(root), run_id, 15, 0.002),
                )
                for _ in range(3)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=20)

            for worker in workers:
                self.assertFalse(worker.is_alive())
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(read_json(path)["counter"], 45)
            self.assertTrue(json_backup_path(path).is_file())
            self.assertFalse((path.parent / "runtime" / "plan.write.lock").exists())

    def test_sidecar_lock_serializes_independent_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / "shared.lock"
            counter = root / "counter.txt"
            counter.write_text("0", encoding="ascii")
            workers = [
                multiprocessing.Process(
                    target=increment_text_counter_with_sidecar,
                    args=(str(lock), str(counter), 20, 0.001),
                )
                for _index in range(3)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=20)

            for worker in workers:
                self.assertFalse(worker.is_alive())
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(counter.read_text(encoding="ascii"), "60")
            self.assertFalse(lock.exists())

    def test_sidecar_lock_reclaims_expired_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "expired.lock"
            lock.write_text("expired-owner\n", encoding="ascii")
            expired = time.time() - 120
            os.utime(lock, (expired, expired))

            with exclusive_sidecar_lock(lock, timeout=0.2, stale_seconds=60, retry_delay=0.001):
                self.assertTrue(lock.exists())

            self.assertFalse(lock.exists())

    def test_portable_archive_skips_plan_recovery_sidecars(self) -> None:
        self.assertTrue(_should_skip(Path("plan.json.bak"), include_logs=True))
        self.assertTrue(
            _should_skip(
                Path("recovery") / "plan.reconstructed-20260814T070042Z.json",
                include_logs=True,
            )
        )
        self.assertTrue(
            _should_skip(
                Path("recovery") / "plan.invalid-20260814T070042Z.json",
                include_logs=True,
            )
        )
        self.assertFalse(_should_skip(Path("recovery") / "notes.json", include_logs=True))


if __name__ == "__main__":
    unittest.main()
