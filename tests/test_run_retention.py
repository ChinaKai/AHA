from __future__ import annotations

import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest

from aha_cli.services.run_retention import (
    RunRetentionError,
    apply_run_retention,
    format_retention_report,
    inspect_retention_archive,
    inspect_run_retention_archive,
    list_retention_archives,
    restore_run_retention_archive,
    restore_retention_archive,
    run_retention_report,
)
from aha_cli.services.run_retention_policy import (
    enforce_all_run_retention_policy,
    enforce_run_retention_policy,
    format_all_run_retention_policy_report,
    retention_policy_report_due,
    scheduled_retention_policy_report,
)
from aha_cli.store.io import read_json, write_json


def write_plan(run_path: Path, run_id: str) -> None:
    write_json(
        run_path / "plan.json",
        {
            "id": run_id,
            "goal": "Retention test",
            "mode": "research",
            "created_at": "2026-05-31T00:00:00+00:00",
            "updated_at": "2026-05-31T00:00:00+00:00",
            "write_scopes": [],
            "tasks": [],
        },
    )


def write_file(path: Path, text: str, *, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


class RunRetentionTests(unittest.TestCase):
    def test_retention_report_groups_files_sizes_and_age_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            run_path = root / "runs" / "run-001"
            write_plan(run_path, "run-001")
            write_file(run_path / "logs" / "backend.log", "abc", mtime=99990)
            write_file(run_path / "chat" / "main.md", "12345", mtime=90000)
            write_file(run_path / "runtime" / "state.json", "{}", mtime=99999)

            report = run_retention_report(root, "run-001", top=2, now=100000)
            groups = {item["name"]: item for item in report["groups"]}
            buckets = {item["name"]: item for item in report["age_buckets"]}

            self.assertTrue(report["dry_run"])
            self.assertEqual(report["total"]["files"], 4)
            self.assertEqual(groups["chat"]["bytes"], 5)
            self.assertEqual(groups["logs"]["files"], 1)
            self.assertEqual(groups["root"]["files"], 1)
            self.assertEqual(buckets["lt_1h"]["files"], 3)
            self.assertEqual(buckets["1h_1d"]["files"], 1)
            self.assertEqual(report["largest_files"][0]["path"], "plan.json")
            self.assertEqual(report["largest_files"][1]["path"], "chat/main.md")
            policy_actions = {item["group"]: item for item in report["policy_report"]["actions"]}
            self.assertTrue(report["policy_report"]["dry_run"])
            self.assertEqual(report["policy_report"]["candidate_total"]["files"], 1)
            self.assertEqual(policy_actions["logs"]["decision"], "would_archive")
            self.assertEqual(policy_actions["chat"]["reason"], "optional_group_requires_include_chat")
            self.assertEqual(policy_actions["runtime"]["reason"], "excluded_group")

    def test_retention_report_text_output_is_human_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            run_path = root / "runs" / "run-001"
            write_plan(run_path, "run-001")
            write_file(run_path / "logs" / "backend.log", "abc", mtime=1990)

            text = format_retention_report(run_retention_report(root, "run-001", top=1, now=2000))

            self.assertIn("AHA run retention report (dry-run): run-001", text)
            self.assertIn("logs", text)
            self.assertIn("policy_dry_run", text)
            self.assertIn("largest_files", text)

    def test_retention_report_rejects_missing_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                run_retention_report(Path(tmp) / ".aha", "missing")

    def test_apply_retention_archives_default_groups_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            archive_dir = Path(tmp) / "archives"
            run_path = root / "runs" / "run-001"
            write_plan(run_path, "run-001")
            log = run_path / "logs" / "backend.log"
            prompt = run_path / "prompts" / "main.md"
            chat = run_path / "chat" / "main.md"
            write_file(log, "log", mtime=90000)
            write_file(prompt, "prompt", mtime=90000)
            write_file(chat, "chat", mtime=90000)

            result = apply_run_retention(root, "run-001", archive_dir=archive_dir, now=100000)
            archive_path = Path(result["archive"]["path"])

            self.assertFalse(result["dry_run"])
            self.assertFalse(result["force"])
            self.assertTrue(archive_path.exists())
            self.assertTrue(log.exists())
            self.assertTrue(prompt.exists())
            self.assertTrue(chat.exists())
            with tarfile.open(archive_path, "r:gz") as archive:
                names = set(archive.getnames())
            self.assertIn("aha-run-retention-manifest.json", names)
            self.assertIn("run/logs/backend.log", names)
            self.assertIn("run/prompts/main.md", names)
            self.assertNotIn("run/chat/main.md", names)

            archive_list = list_retention_archives(root, "run-001", archive_dir=archive_dir)
            inspected = inspect_retention_archive(archive_path)
            self.assertEqual(archive_list["archives"][0]["source_run_id"], "run-001")
            self.assertEqual(inspected["file_count"], 2)
            self.assertEqual({item["path"] for item in inspected["files"]}, {"logs/backend.log", "prompts/main.md"})

    def test_apply_retention_force_deletes_only_archived_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            archive_dir = Path(tmp) / "archives"
            run_path = root / "runs" / "run-001"
            write_plan(run_path, "run-001")
            log = run_path / "logs" / "backend.log"
            prompt = run_path / "prompts" / "main.md"
            chat = run_path / "chat" / "main.md"
            runtime = run_path / "runtime" / "state.json"
            write_file(log, "log", mtime=90000)
            write_file(prompt, "prompt", mtime=90000)
            write_file(chat, "chat", mtime=90000)
            write_file(runtime, "{}", mtime=90000)

            result = apply_run_retention(root, "run-001", archive_dir=archive_dir, force=True, now=100000)

            self.assertEqual({item["path"] for item in result["deleted"]}, {"logs/backend.log", "prompts/main.md"})
            self.assertFalse(log.exists())
            self.assertFalse(prompt.exists())
            self.assertTrue(chat.exists())
            self.assertTrue(runtime.exists())
            self.assertTrue((run_path / "plan.json").exists())

            restored = restore_retention_archive(root, Path(result["archive"]["path"]), now=100000)
            self.assertEqual({item["path"] for item in restored["restored"]}, {"logs/backend.log", "prompts/main.md"})
            self.assertTrue(log.exists())
            self.assertTrue(prompt.exists())

            skipped = restore_retention_archive(root, Path(result["archive"]["path"]), now=100000)
            self.assertEqual({item["path"] for item in skipped["skipped"]}, {"logs/backend.log", "prompts/main.md"})

    def test_restore_rejects_symlink_escape_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            archive_dir = Path(tmp) / "archives"
            outside = Path(tmp) / "outside"
            run_path = root / "runs" / "run-001"
            write_plan(run_path, "run-001")
            write_file(run_path / "logs" / "backend.log", "log", mtime=90000)
            result = apply_run_retention(root, "run-001", archive_dir=archive_dir, now=100000)
            archive_path = Path(result["archive"]["path"])
            shutil.rmtree(run_path / "logs")
            outside.mkdir()
            (run_path / "logs").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(RunRetentionError) as raised:
                restore_retention_archive(root, archive_path, now=100000, force=True)

            self.assertEqual(raised.exception.reason, "unsafe_restore_target")
            self.assertFalse((outside / "backend.log").exists())

    def test_apply_retention_can_include_chat_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            run_path = root / "runs" / "run-001"
            write_plan(run_path, "run-001")
            chat = run_path / "chat" / "main.md"
            write_file(chat, "chat", mtime=90000)

            result = apply_run_retention(root, "run-001", include_chat=True, force=True, now=100000)

            self.assertIn("chat", result["policy"]["groups"])
            self.assertFalse(chat.exists())

    def test_apply_retention_blocks_current_and_active_heartbeat_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            current_path = root / "runs" / "current-run"
            active_path = root / "runs" / "active-run"
            write_plan(current_path, "current-run")
            write_plan(active_path, "active-run")
            heartbeat = active_path / "logs" / "realtime-debug.log"
            write_file(heartbeat, '{"phase":"heartbeat_sent"}\n', mtime=99999)

            with self.assertRaises(RunRetentionError) as current_error:
                apply_run_retention(root, "current-run", current_run_id="current-run", now=100000)
            with self.assertRaises(RunRetentionError) as forced_current_error:
                apply_run_retention(root, "current-run", current_run_id="current-run", force=True, now=100000)
            with self.assertRaises(RunRetentionError) as heartbeat_error:
                apply_run_retention(root, "active-run", now=100000)
            with self.assertRaises(RunRetentionError) as forced_heartbeat_error:
                apply_run_retention(root, "active-run", force=True, now=100000)

            self.assertEqual(current_error.exception.reason, "current_run")
            self.assertEqual(forced_current_error.exception.reason, "current_run")
            self.assertEqual(heartbeat_error.exception.reason, "active_heartbeat")
            self.assertEqual(forced_heartbeat_error.exception.reason, "active_heartbeat")
            self.assertTrue(current_path.exists())
            self.assertTrue(active_path.exists())

    def test_retention_policy_thresholds_can_auto_apply_when_over_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            run_path = root / "runs" / "run-001"
            write_plan(run_path, "run-001")
            log = run_path / "logs" / "backend.log"
            write_file(log, "large-log", mtime=90000)

            preview = run_retention_report(root, "run-001", now=100000, max_candidate_bytes=1)
            result = enforce_run_retention_policy(
                root,
                "run-001",
                apply=True,
                force=True,
                now=100000,
                max_candidate_bytes=1,
            )

            automation = preview["policy_report"]["automation"]
            self.assertTrue(automation["over_limit"])
            self.assertEqual(automation["alerts"][0]["kind"], "candidate_bytes_over_limit")
            self.assertTrue(result["policy_enforced"])
            self.assertTrue(result["policy_report"]["automation"]["auto_applied"])
            self.assertFalse(log.exists())
            self.assertTrue(Path(result["archive"]["path"]).exists())

    def test_retention_policy_apply_skips_when_thresholds_are_not_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            run_path = root / "runs" / "run-001"
            write_plan(run_path, "run-001")
            log = run_path / "logs" / "backend.log"
            write_file(log, "small", mtime=90000)

            result = enforce_run_retention_policy(
                root,
                "run-001",
                apply=True,
                now=100000,
                max_candidate_bytes=999999,
            )

            self.assertTrue(result["dry_run"])
            self.assertTrue(result["apply_skipped"])
            self.assertEqual(result["apply_skipped_reason"], "threshold_not_exceeded")
            self.assertTrue(log.exists())

    def test_all_run_retention_policy_reports_alerts_and_protects_current_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            old_run = root / "runs" / "old-run"
            current_run = root / "runs" / "current-run"
            write_plan(old_run, "old-run")
            write_plan(current_run, "current-run")
            write_file(old_run / "logs" / "backend.log", "large-old-log", mtime=90000)
            write_file(current_run / "logs" / "backend.log", "large-current-log", mtime=90000)

            report = enforce_all_run_retention_policy(
                root,
                current_run_id="current-run",
                now=100000,
                max_candidate_bytes=1,
            )
            runs = {item["run_id"]: item for item in report["runs"]}
            text = format_all_run_retention_policy_report(report)

            self.assertTrue(report["dry_run"])
            self.assertEqual(report["summary"]["runs"], 2)
            self.assertEqual(report["summary"]["over_limit_runs"], 2)
            self.assertEqual(report["summary"]["eligible_runs"], 1)
            self.assertEqual(report["summary"]["protected_runs"], 1)
            self.assertEqual(runs["old-run"]["recommended_action"], "apply_retention")
            self.assertEqual(runs["current-run"]["recommended_action"], "protect_current_run")
            self.assertEqual(runs["current-run"]["guard"]["reason"], "current_run")
            self.assertEqual({item["run_id"] for item in report["alerts"]}, {"old-run", "current-run"})
            self.assertIn("AHA all-run retention policy (dry-run)", text)

    def test_all_run_retention_policy_apply_only_compacts_eligible_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            archive_dir = Path(tmp) / "archives"
            old_run = root / "runs" / "old-run"
            current_run = root / "runs" / "current-run"
            write_plan(old_run, "old-run")
            write_plan(current_run, "current-run")
            old_log = old_run / "logs" / "backend.log"
            current_log = current_run / "logs" / "backend.log"
            write_file(old_log, "large-old-log", mtime=90000)
            write_file(current_log, "large-current-log", mtime=90000)

            result = enforce_all_run_retention_policy(
                root,
                apply=True,
                current_run_id="current-run",
                archive_dir=archive_dir,
                force=True,
                now=100000,
                max_candidate_bytes=1,
            )
            runs = {item["run_id"]: item for item in result["runs"]}

            self.assertFalse(result["dry_run"])
            self.assertTrue(result["automation"]["auto_applied"])
            self.assertIsNotNone(runs["old-run"]["archive"])
            self.assertEqual(runs["old-run"]["deleted_count"], 1)
            self.assertFalse(old_log.exists())
            self.assertTrue(current_log.exists())
            self.assertIsNone(runs["current-run"]["archive"])
            self.assertEqual(runs["current-run"]["recommended_action"], "protect_current_run")
            self.assertTrue(Path(runs["old-run"]["archive"]["path"]).exists())

    def test_scheduled_retention_policy_report_persists_latest_read_only_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            run_path = root / "runs" / "run-001"
            write_plan(run_path, "run-001")
            write_file(run_path / "logs" / "backend.log", "large-log", mtime=90000)

            report = scheduled_retention_policy_report(
                root,
                current_run_id="current-run",
                config={"max_candidate_bytes": 1, "report_interval_seconds": 60},
                now=100000,
            )
            scheduled = report["scheduled_report"]
            latest = Path(scheduled["latest_path"])
            persisted = read_json(latest)

            self.assertTrue(report["dry_run"])
            self.assertTrue(scheduled["read_only"])
            self.assertEqual(report["summary"]["over_limit_runs"], 1)
            self.assertTrue(Path(scheduled["path"]).exists())
            self.assertEqual(persisted["scheduled_report"]["path"], scheduled["path"])
            self.assertFalse(retention_policy_report_due(root, interval_seconds=60, now=latest.stat().st_mtime + 10))
            self.assertTrue(retention_policy_report_due(root, interval_seconds=60, now=latest.stat().st_mtime + 61))

    def test_run_scoped_archive_inspect_and_restore_reject_unsafe_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            run_path = root / "runs" / "run-001"
            write_plan(run_path, "run-001")
            log = run_path / "logs" / "backend.log"
            write_file(log, "log", mtime=90000)
            result = apply_run_retention(root, "run-001", force=True, now=100000)
            archive_name = Path(result["archive"]["path"]).name

            inspected = inspect_run_retention_archive(root, "run-001", archive_name)
            restored = restore_run_retention_archive(root, "run-001", archive_name, now=100000)

            self.assertEqual(inspected["archive_name"], archive_name)
            self.assertEqual(restored["archive_name"], archive_name)
            self.assertTrue(log.exists())
            with self.assertRaises(RunRetentionError) as raised:
                inspect_run_retention_archive(root, "run-001", "../outside.tar.gz")
            self.assertEqual(raised.exception.reason, "invalid_archive_name")


if __name__ == "__main__":
    unittest.main()
