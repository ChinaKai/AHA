from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from aha_cli.services.run_cleanup import cleanup_temp_runs
from aha_cli.services.run_delete import RunDeleteError, delete_run
from aha_cli.store.io import write_json


def write_plan(run_path: Path, run_id: str, *, temporary: bool = False) -> None:
    data = {
        "id": run_id,
        "goal": "Cleanup test",
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


class RunCleanupTests(unittest.TestCase):
    def test_dry_run_lists_stale_temp_run_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            aha_home = Path(tmp) / ".aha"
            run_path = aha_home / "runs" / "temp-run"
            write_plan(run_path, "temp-run", temporary=True)
            (run_path / ".aha-temp-run").write_text("", encoding="utf-8")
            touch_tree(run_path, 1000)

            result = cleanup_temp_runs(aha_home, dry_run=True, tmp_root=None, stale_seconds=60, now=2000)

            self.assertTrue(run_path.exists())
            self.assertEqual(result["candidates"][0]["action"], "would_delete")
            self.assertEqual(result["deleted"][0]["action"], "would_delete")
            self.assertEqual(result["deleted"][0]["reason"], "stale_temporary_run")

    def test_apply_deletes_stale_temp_run_and_protects_user_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            aha_home = Path(tmp) / ".aha"
            temp_run = aha_home / "runs" / "temp-run"
            user_run = aha_home / "runs" / "user-run"
            write_plan(temp_run, "temp-run", temporary=True)
            (temp_run / ".aha-temp-run").write_text("", encoding="utf-8")
            write_plan(user_run, "user-run")
            touch_tree(temp_run, 1000)
            touch_tree(user_run, 1000)

            result = cleanup_temp_runs(aha_home, dry_run=False, tmp_root=None, stale_seconds=60, now=2000)

            self.assertFalse(temp_run.exists())
            self.assertTrue(user_run.exists())
            self.assertEqual(result["deleted"][0]["action"], "deleted")
            self.assertEqual(result["protected"][0]["reason"], "non_temporary_run")

    def test_current_and_active_heartbeat_runs_are_protected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            aha_home = Path(tmp) / ".aha"
            current_run = aha_home / "runs" / "current-run"
            active_run = aha_home / "runs" / "active-run"
            for run_path, run_id in ((current_run, "current-run"), (active_run, "active-run")):
                write_plan(run_path, run_id, temporary=True)
                (run_path / ".aha-temp-run").write_text("", encoding="utf-8")
                touch_tree(run_path, 1000)
            log = active_run / "logs" / "realtime-debug.log"
            log.parent.mkdir(parents=True)
            log.write_text('{"type":"heartbeat_sent"}\n', encoding="utf-8")
            os.utime(log, (1995, 1995))

            result = cleanup_temp_runs(
                aha_home,
                current_run_id="current-run",
                dry_run=False,
                tmp_root=None,
                stale_seconds=60,
                active_heartbeat_seconds=30,
                now=2000,
            )

            self.assertTrue(current_run.exists())
            self.assertTrue(active_run.exists())
            self.assertEqual({item["reason"] for item in result["protected"]}, {"current_run", "active_heartbeat"})

    def test_apply_removes_stale_tmp_aha_home_without_protected_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aha_home = root / "user-home" / ".aha"
            tmp_aha = root / "tmp-work" / ".aha"
            tmp_run = tmp_aha / "runs" / "tmp-run"
            write_plan(tmp_run, "tmp-run", temporary=True)
            (tmp_run / ".aha-temp-run").write_text("", encoding="utf-8")
            touch_tree(tmp_aha, 1000)

            result = cleanup_temp_runs(aha_home, dry_run=False, tmp_root=root, stale_seconds=60, now=2000)

            self.assertFalse(tmp_aha.exists())
            self.assertEqual(result["deleted"][0]["kind"], "tmp_aha_home")
            self.assertEqual(result["deleted"][0]["reason"], "stale_tmp_aha_home")

    def test_cleanup_refuses_non_temp_scan_root_without_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            aha_home = Path(tmp) / ".aha"

            result = cleanup_temp_runs(aha_home, dry_run=True, tmp_root=Path.cwd(), stale_seconds=60, now=2000)

            self.assertEqual(result["errors"][0]["reason"], "unsafe_tmp_root")

    def test_cleanup_protects_symlink_tmp_aha_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aha_home = root / "user-home" / ".aha"
            scan_root = root / "scan-root"
            target_aha = root / "target" / ".aha"
            tmp_run = target_aha / "runs" / "tmp-run"
            write_plan(tmp_run, "tmp-run", temporary=True)
            (tmp_run / ".aha-temp-run").write_text("", encoding="utf-8")
            touch_tree(target_aha, 1000)
            link_parent = scan_root / "linked-work"
            link_parent.mkdir(parents=True)
            (link_parent / ".aha").symlink_to(target_aha, target_is_directory=True)

            result = cleanup_temp_runs(aha_home, dry_run=False, tmp_root=scan_root, stale_seconds=60, now=2000)

            self.assertTrue(target_aha.exists())
            self.assertEqual(result["protected"][0]["reason"], "symlink_aha_home")

    def test_cleanup_protects_configured_tmp_aha_home_without_temp_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aha_home = root / "user-home" / ".aha"
            configured_aha = root / "portable-work" / ".aha"
            write_json(configured_aha / "config.json", {"backend": "stub"})
            touch_tree(configured_aha, 1000)

            result = cleanup_temp_runs(aha_home, dry_run=False, tmp_root=root, stale_seconds=60, now=2000)

            self.assertTrue(configured_aha.exists())
            self.assertEqual(result["protected"][0]["reason"], "non_temporary_aha_home")

    def test_delete_run_removes_requested_run_and_rejects_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            aha_home = Path(tmp) / ".aha"
            old_run = aha_home / "runs" / "old-run"
            current_run = aha_home / "runs" / "current-run"
            write_plan(old_run, "old-run")
            write_plan(current_run, "current-run")

            result = delete_run(aha_home, "old-run", current_run_id="current-run")

            self.assertEqual(result["action"], "deleted")
            self.assertTrue(result["had_plan"])
            self.assertFalse(old_run.exists())
            with self.assertRaises(RunDeleteError) as raised:
                delete_run(aha_home, "current-run", current_run_id="current-run", force=True)
            self.assertEqual(raised.exception.reason, "current_run")
            self.assertTrue(current_run.exists())

    def test_delete_run_requires_force_for_active_heartbeat_and_handles_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            aha_home = Path(tmp) / ".aha"
            active_run = aha_home / "runs" / "active-run"
            orphan_run = aha_home / "runs" / "orphan-run"
            write_plan(active_run, "active-run")
            log = active_run / "logs" / "realtime-debug.log"
            log.parent.mkdir(parents=True)
            log.write_text('{"phase":"heartbeat_sent"}\n', encoding="utf-8")
            orphan_log = orphan_run / "logs" / "realtime-debug.log"
            orphan_log.parent.mkdir(parents=True)
            orphan_log.write_text("orphan log\n", encoding="utf-8")

            with self.assertRaises(RunDeleteError) as raised:
                delete_run(aha_home, "active-run", current_run_id="current-run")
            self.assertEqual(raised.exception.reason, "active_heartbeat")

            forced = delete_run(aha_home, "active-run", current_run_id="current-run", force=True)
            orphan = delete_run(aha_home, "orphan-run", current_run_id="current-run")

            self.assertEqual(forced["reason"], "forced")
            self.assertFalse(active_run.exists())
            self.assertFalse(orphan["had_plan"])
            self.assertFalse(orphan_run.exists())


if __name__ == "__main__":
    unittest.main()
