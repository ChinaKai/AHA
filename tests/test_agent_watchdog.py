from __future__ import annotations

import io
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.agent_watchdog import (
    WATCHDOG_MIN_RESTART_INTERVAL_SECONDS,
    recover_stuck_agent,
    scan_all_runs,
    scan_run,
    stuck_agent_reason,
)
from aha_cli.services.chat import chat_offset_path, save_chat_offset
from aha_cli.store.events import append_event
from aha_cli.store.filesystem import (
    append_jsonl,
    event_path,
    inbox_path,
    run_dir,
    set_agent_status,
    set_task_status,
    status_snapshot,
)
from aha_cli.store.io import read_json
from tests.helpers import isolated_cli_environment


class AgentWatchdogTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with isolated_cli_environment(), mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def _init_run(self, tmp: str) -> tuple[Path, str]:
        root = Path(tmp)
        with mock.patch("pathlib.Path.cwd", return_value=root):
            self.run_cli("init", "--portable", "--backend", "codex")
            code, plan_output = self.run_cli("plan", "Watchdog", "--agents", "1")
            self.assertEqual(code, 0)
            run_id = plan_output.splitlines()[0].split(": ", 1)[1]
        return root, run_id

    def _stuck_state(self, *, status: str = "running", last_started_at: str | None = None) -> dict:
        return {
            "status": status,
            "pid": 4242,
            "activity": {
                "busy": False,
                "last_started_at": last_started_at,
                "last_finished_at": None,
                "last_reply_at": None,
                "last_error_at": None,
            },
        }

    def _now(self) -> datetime:
        return datetime.now()

    def _prepare_pending(self, root: Path, run_id: str) -> Path:
        """Create a pending inbox message and pin the cursor to the start."""
        set_task_status(root, run_id, "task-001", "running")
        set_agent_status(root, run_id, "task-001", "main", "running")
        inbox = inbox_path(root, run_id, "main", "task-001")
        append_jsonl(inbox, {"sender": "browser", "message": "pending", "task_id": "task-001"})
        offset_file = chat_offset_path(run_dir(root, run_id), "main", "task-001")
        save_chat_offset(offset_file, 0)
        return inbox

    def test_stuck_agent_reason_detects_running_backend_with_pending_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)
            self._prepare_pending(root, run_id)
            old = (self._now() - timedelta(seconds=300)).isoformat()
            state = self._stuck_state(last_started_at=old)

            task = status_snapshot(root, run_id)["tasks"][0]
            agent = task["agents"][0]
            reason = stuck_agent_reason(root, run_id, task, agent, state, now=self._now())

        self.assertEqual(reason, "backend_running_but_not_consuming_inbox")

    def test_stuck_agent_reason_ignores_busy_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)
            self._prepare_pending(root, run_id)
            state = self._stuck_state(status="busy")

            task = status_snapshot(root, run_id)["tasks"][0]
            agent = task["agents"][0]
            reason = stuck_agent_reason(root, run_id, task, agent, state, now=self._now())

        self.assertIsNone(reason)

    def test_stuck_agent_reason_ignores_agent_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)
            set_agent_status(root, run_id, "task-001", "main", "completed")
            state = self._stuck_state(last_started_at=(self._now() - timedelta(seconds=300)).isoformat())

            task = status_snapshot(root, run_id)["tasks"][0]
            agent = task["agents"][0]
            reason = stuck_agent_reason(root, run_id, task, agent, state, now=self._now())

        self.assertIsNone(reason)

    def test_scan_run_recovers_stuck_backend_and_keeps_pending_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)
            inbox = self._prepare_pending(root, run_id)
            old = (self._now() - timedelta(seconds=300)).isoformat()
            state = self._stuck_state(last_started_at=old)
            stop_calls: list[tuple] = []
            start_calls: list[tuple] = []

            def fake_stop(*args: object, **kwargs: object) -> dict:
                stop_calls.append((args, kwargs))
                return {"status": "stopped"}

            def fake_start(*args: object, **kwargs: object) -> dict:
                start_calls.append((args, kwargs))
                return {"status": "running"}

            with (
                mock.patch("aha_cli.services.agent_watchdog.backend_status", return_value=state),
                mock.patch("aha_cli.services.agent_watchdog.stop_backend", side_effect=fake_stop),
                mock.patch("aha_cli.services.agent_watchdog.start_backend", side_effect=fake_start),
            ):
                result = scan_run(root, run_id, now=self._now())

            persisted_inbox_size = inbox.stat().st_size
            offset_file = chat_offset_path(run_dir(root, run_id), "main", "task-001")
            self.assertEqual(result["checked"], 1)
            self.assertEqual(len(result["recovered"]), 1)
            self.assertEqual(result["recovered"][0]["task_id"], "task-001")
            self.assertEqual(len(stop_calls), 1)
            self.assertEqual(len(start_calls), 1)
            # The pending message must survive: watchdog recovery preserves the cursor.
            self.assertGreater(persisted_inbox_size, 0)
            self.assertTrue(offset_file.exists())

    def test_scan_run_respects_min_restart_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)
            self._prepare_pending(root, run_id)
            old = (self._now() - timedelta(seconds=300)).isoformat()
            state = self._stuck_state(last_started_at=old)
            # A watchdog recovery happened seconds ago: must be suppressed.
            recent_recovery = (self._now() - timedelta(seconds=5)).isoformat()
            append_event(
                root,
                run_id,
                "agent_watchdog_recovered",
                {"task_id": "task-001", "agent_id": "main", "reason": "recent"},
                ts=recent_recovery,
            )

            with (
                mock.patch("aha_cli.services.agent_watchdog.backend_status", return_value=state),
                mock.patch("aha_cli.services.agent_watchdog.stop_backend", return_value={"status": "stopped"}) as stop_mock,
                mock.patch("aha_cli.services.agent_watchdog.start_backend", return_value={"status": "running"}),
            ):
                result = scan_run(root, run_id, now=self._now())

        self.assertEqual(len(result["recovered"]), 0)
        stop_mock.assert_not_called()

    def test_scan_all_runs_only_visits_active_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)
            set_task_status(root, run_id, "task-001", "running")
            set_agent_status(root, run_id, "task-001", "main", "running")

            with mock.patch("aha_cli.services.agent_watchdog.scan_run", return_value={"run_id": run_id, "checked": 1, "recovered": []}) as scan_mock:
                result = scan_all_runs(root)

        self.assertGreaterEqual(result["checked"], 1)
        scan_mock.assert_called_once()

    def test_recover_stuck_agent_preserves_cursor_and_records_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, run_id = self._init_run(tmp)
            set_task_status(root, run_id, "task-001", "running")
            set_agent_status(root, run_id, "task-001", "main", "running")
            inbox = inbox_path(root, run_id, "main", "task-001")
            append_jsonl(inbox, {"sender": "browser", "message": "pending", "task_id": "task-001"})
            offset_file = chat_offset_path(run_dir(root, run_id), "main", "task-001")
            save_chat_offset(offset_file, 0)

            with (
                mock.patch("aha_cli.services.agent_watchdog.stop_backend", return_value={"status": "stopped"}),
                mock.patch("aha_cli.services.agent_watchdog.start_backend", return_value={"status": "running"}),
            ):
                result = recover_stuck_agent(root, run_id, {"id": "task-001"}, {"id": "main"}, reason="stuck")

            persisted = status_snapshot(root, run_id)["tasks"][0]
            event_log = event_path(root, run_id).read_text(encoding="utf-8")

        self.assertEqual(result["reason"], "stuck")
        self.assertEqual(persisted["agents"][0]["id"], "main")
        self.assertIn("agent_watchdog_recovered", event_log)


if __name__ == "__main__":
    unittest.main()
