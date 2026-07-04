from __future__ import annotations

import io
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main, task_snapshot
from aha_cli.store.filesystem import (
    add_agent,
    append_event,
    iter_jsonl_from,
    reopen_task,
    set_agent_status,
    set_task_status,
    status_snapshot,
)
from aha_cli.store.sessions import backend_session_usage_archive_fields, usage_token_summary
from tests.helpers import append_jsonl_records, write_plan_statuses


def run_cli(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    with mock.patch("sys.stdout", out):
        code = main(list(args))
    return code, out.getvalue()


class StoreStateTests(unittest.TestCase):
    def test_usage_token_summary_uses_backend_specific_total_formula(self) -> None:
        usage = {
            "input_tokens": 200,
            "cache_read_input_tokens": 25,
            "cache_creation_input_tokens": 40,
            "output_tokens": 75,
            "total_tokens": 999,
        }

        codex_summary = usage_token_summary(usage, backend="codex")
        claude_summary = usage_token_summary(usage, backend="claude")

        self.assertEqual(codex_summary["total_tokens"], 275)
        self.assertEqual(codex_summary["total_formula"], "input + output")
        self.assertEqual(codex_summary["cache_creation_tokens"], 40)
        self.assertEqual(claude_summary["total_tokens"], 300)
        self.assertEqual(claude_summary["total_formula"], "input + cache_read + output")
        self.assertEqual(claude_summary["cache_read_tokens"], 25)

    def test_backend_session_usage_archive_uses_current_session_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = run_cli("plan", "Repeat reset usage", "--agents", "0")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                history = [{"backend_session_id": "session-a", "token_summary": {"total_tokens": 150}}]
                append_event(
                    root,
                    run_id,
                    "agent_usage",
                    {
                        "task_id": "task-001",
                        "target": "main",
                        "backend_session_id": "session-a",
                        "usage": {"input_tokens": 120, "output_tokens": 30},
                    },
                )

                without_current_usage = backend_session_usage_archive_fields(
                    root,
                    run_id,
                    "task-001",
                    "main",
                    backend_session_id="session-b",
                    history=history,
                )
                append_event(
                    root,
                    run_id,
                    "backend_session_reset",
                    {"task_id": "task-001", "agent_id": "main", "old_backend_session_id": "session-a"},
                )
                append_event(
                    root,
                    run_id,
                    "agent_usage",
                    {
                        "task_id": "task-001",
                        "target": "main",
                        "usage": {"input_tokens": 40, "output_tokens": 8},
                    },
                )
                current_unscoped_usage = backend_session_usage_archive_fields(
                    root,
                    run_id,
                    "task-001",
                    "main",
                    backend_session_id="session-b",
                    backend="codex",
                    history=history,
                )

        self.assertEqual(without_current_usage, {})
        self.assertEqual(current_unscoped_usage["token_summary"]["total_tokens"], 48)

    def test_backend_session_usage_archive_sums_claude_turn_usage_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                run_cli("init", "--portable", "--backend", "claude")
                code, plan_output = run_cli("plan", "Claude usage", "--agents", "0")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                for usage in (
                    {"input_tokens": 10, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 7, "output_tokens": 3},
                    {"input_tokens": 5, "cache_read_input_tokens": 1, "cache_creation_input_tokens": 4, "output_tokens": 7},
                ):
                    append_event(
                        root,
                        run_id,
                        "agent_usage",
                        {
                            "task_id": "task-001",
                            "target": "main",
                            "backend_session_id": "claude-session",
                            "usage": usage,
                        },
                    )
                claude_usage = backend_session_usage_archive_fields(
                    root,
                    run_id,
                    "task-001",
                    "main",
                    backend_session_id="claude-session",
                    backend="claude",
                )
                codex_usage = backend_session_usage_archive_fields(
                    root,
                    run_id,
                    "task-001",
                    "main",
                    backend_session_id="claude-session",
                    backend="codex",
                )

        self.assertEqual(claude_usage["last_usage"]["input_tokens"], 15)
        self.assertEqual(claude_usage["last_usage"]["cache_read_input_tokens"], 3)
        self.assertEqual(claude_usage["last_usage"]["cache_creation_input_tokens"], 11)
        self.assertEqual(claude_usage["last_usage"]["output_tokens"], 10)
        self.assertEqual(claude_usage["token_summary"]["total_tokens"], 28)
        self.assertEqual(codex_usage["last_usage"]["input_tokens"], 5)
        self.assertEqual(codex_usage["token_summary"]["total_tokens"], 12)

    def test_running_status_keeps_original_task_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = run_cli("plan", "Timing", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                with mock.patch(
                    "aha_cli.store.filesystem.utc_now",
                    side_effect=[
                        "2026-05-15T00:00:00+00:00",
                        "2026-05-15T00:00:01+00:00",
                        "2026-05-15T00:05:00+00:00",
                        "2026-05-15T00:05:01+00:00",
                    ],
                ):
                    set_task_status(root, run_id, "task-001", "running")
                    set_task_status(root, run_id, "task-001", "running")

                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["started_at"], "2026-05-15T00:00:00+00:00")

    def test_running_status_does_not_reopen_terminal_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = run_cli("plan", "No reopen", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "completed", 0)
                completed = task_snapshot(root, run_id, "task-001")["task"]

                set_task_status(root, run_id, "task-001", "running")
                detail = task_snapshot(root, run_id, "task-001")

        self.assertEqual(detail["task"]["status"], "completed")
        self.assertEqual(detail["task"]["exit_code"], 0)
        self.assertEqual(detail["task"]["finished_at"], completed["finished_at"])

    def test_awaiting_user_status_does_not_reopen_terminal_task_without_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = run_cli("plan", "No implicit reopen", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "completed", 0)
                completed = task_snapshot(root, run_id, "task-001")["task"]

                set_task_status(root, run_id, "task-001", "awaiting_user")
                detail = task_snapshot(root, run_id, "task-001")

                reopened = reopen_task(root, run_id, "task-001")

        self.assertEqual(detail["task"]["status"], "completed")
        self.assertEqual(detail["task"]["exit_code"], 0)
        self.assertEqual(detail["task"]["finished_at"], completed["finished_at"])
        self.assertEqual(reopened["status"], "awaiting_user")

    def test_agent_status_started_at_tracks_status_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = run_cli("plan", "Agent timing", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                with mock.patch(
                    "aha_cli.store.filesystem.utc_now",
                    side_effect=[
                        "2026-05-15T00:00:00+00:00",
                        "2026-05-15T00:00:01+00:00",
                        "2026-05-15T00:00:10+00:00",
                        "2026-05-15T00:00:11+00:00",
                        "2026-05-15T00:01:00+00:00",
                        "2026-05-15T00:01:01+00:00",
                    ],
                ):
                    set_agent_status(root, run_id, "task-001", "main", "running")
                    set_agent_status(root, run_id, "task-001", "main", "running")
                    set_agent_status(root, run_id, "task-001", "main", "waiting")

                agent = task_snapshot(root, run_id, "task-001")["task"]["agents"][0]

        self.assertEqual(agent["status"], "waiting")
        self.assertEqual(agent["status_started_at"], "2026-05-15T00:01:00+00:00")
        self.assertEqual(agent["last_active_at"], "2026-05-15T00:01:00+00:00")
        self.assertEqual(agent["started_at"], "2026-05-15T00:00:10+00:00")

    def test_parallel_plan_writers_do_not_collide_on_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = run_cli("plan", "Parallel writers", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")

            workers = [
                multiprocessing.Process(
                    target=write_plan_statuses,
                    args=(str(root), run_id, "task-001", agent_id, 40),
                )
                for agent_id in ("main", "sub-001")
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)

            for worker in workers:
                self.assertFalse(worker.is_alive())
                self.assertEqual(worker.exitcode, 0)
            snapshot = status_snapshot(root, run_id)
            agents = {agent["id"]: agent["status"] for agent in snapshot["tasks"][0]["agents"]}
            self.assertEqual(agents["main"], "running")
            self.assertEqual(agents["sub-001"], "running")

    def test_jsonl_appends_are_valid_under_concurrent_writers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.jsonl")
            workers = [
                multiprocessing.Process(target=append_jsonl_records, args=(path, worker_id, 50))
                for worker_id in range(4)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)

            for worker in workers:
                self.assertFalse(worker.is_alive())
                self.assertEqual(worker.exitcode, 0)
            events, _ = iter_jsonl_from(Path(path), 0)

        self.assertEqual(len(events), 200)
        self.assertFalse(any(event.get("type") == "malformed_event" for event in events))
        self.assertEqual(len({(event["worker"], event["index"]) for event in events}), 200)
