from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.domain.run_lifecycle import run_lifecycle_projection
from aha_cli.services.run_cleanup import cleanup_temp_runs
from aha_cli.services.run_diagnostics import diagnose_runs
from aha_cli.services.run_lifecycle_actions import RunLifecycleActionError, set_run_lifecycle_status
from aha_cli.store.io import write_json
from aha_cli.store.runs import list_run_summaries, run_summary, update_run_lifecycle
from tests.helpers import fetch_ui_response, json_response_body


def write_plan(root: Path, run_id: str, **extra: object) -> None:
    write_json(
        root / "runs" / run_id / "plan.json",
        {
            "id": run_id,
            "goal": f"Run {run_id}",
            "mode": "research",
            "created_at": "2026-05-31T00:00:00+00:00",
            "updated_at": "2026-05-31T00:00:00+00:00",
            "write_scopes": [],
            "tasks": [],
            **extra,
        },
    )


class RunLifecycleTests(unittest.TestCase):
    def test_projection_defaults_legacy_run_to_active(self) -> None:
        lifecycle = run_lifecycle_projection({"id": "legacy"})

        self.assertEqual(
            lifecycle,
            {
                "status": "active",
                "hidden": False,
                "hidden_at": None,
                "archived": False,
                "archived_at": None,
            },
        )

    def test_projection_recognizes_future_hidden_and_archived_fields(self) -> None:
        hidden = run_lifecycle_projection({"hidden_at": "2026-05-31T01:00:00+00:00"})
        archived = run_lifecycle_projection({"run_lifecycle": {"state": "archived", "archived_at": "2026-05-31T02:00:00+00:00"}})

        self.assertEqual(hidden["status"], "hidden")
        self.assertTrue(hidden["hidden"])
        self.assertEqual(hidden["hidden_at"], "2026-05-31T01:00:00+00:00")
        self.assertEqual(archived["status"], "archived")
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["archived_at"], "2026-05-31T02:00:00+00:00")

    def test_run_summaries_project_lifecycle_without_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_plan(root, "legacy")
            write_plan(root, "hidden", hidden=True, hidden_at="2026-05-31T01:00:00+00:00")
            write_plan(root, "archived", lifecycle={"status": "archived", "archived_at": "2026-05-31T02:00:00+00:00"})

            legacy = run_summary(root, "legacy")
            summaries = {item["id"]: item for item in list_run_summaries(root)}

        self.assertEqual(legacy["lifecycle_status"], "active")
        self.assertEqual(legacy["lifecycle"]["status"], "active")
        self.assertEqual(set(summaries), {"legacy", "hidden", "archived"})
        self.assertEqual(summaries["hidden"]["lifecycle_status"], "hidden")
        self.assertTrue(summaries["hidden"]["hidden"])
        self.assertEqual(summaries["archived"]["lifecycle_status"], "archived")
        self.assertTrue(summaries["archived"]["archived"])

    def test_run_api_and_bootstrap_include_lifecycle_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            write_plan(root, "visible")
            write_plan(root, "hidden", lifecycle_status="hidden", hidden_at="2026-05-31T01:00:00+00:00")
            write_plan(root, "archived", archived=True, archived_at="2026-05-31T02:00:00+00:00")

            runs_response = asyncio.run(fetch_ui_response(root, "visible", "/api/runs"))
            bootstrap_response = asyncio.run(fetch_ui_response(root, "visible", "/api/bootstrap"))
            runs_body = json_response_body(runs_response)
            bootstrap_body = json_response_body(bootstrap_response)

        self.assertTrue(runs_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(bootstrap_response.startswith(b"HTTP/1.1 200 OK"))
        api_runs = {item["id"]: item for item in runs_body["runs"]}
        bootstrap_runs = {item["id"]: item for item in bootstrap_body["runs"]}
        self.assertEqual(set(api_runs), {"visible", "hidden", "archived"})
        self.assertEqual(api_runs["visible"]["lifecycle_status"], "active")
        self.assertEqual(api_runs["hidden"]["lifecycle"]["status"], "hidden")
        self.assertEqual(api_runs["hidden"]["hidden_at"], "2026-05-31T01:00:00+00:00")
        self.assertEqual(bootstrap_runs["archived"]["lifecycle_status"], "archived")
        self.assertEqual(bootstrap_runs["archived"]["archived_at"], "2026-05-31T02:00:00+00:00")

    def test_update_run_lifecycle_hides_archives_and_restores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_plan(root, "old-run")

            hidden = update_run_lifecycle(root, "old-run", "hidden")
            archived = update_run_lifecycle(root, "old-run", "archived")
            restored = update_run_lifecycle(root, "old-run", "active")
            plan = json_response_body(
                asyncio.run(fetch_ui_response(root, "other-run", "/api/runs"))
            )["runs"][0]

        self.assertEqual(hidden["lifecycle_status"], "hidden")
        self.assertTrue(hidden["hidden"])
        self.assertIsNotNone(hidden["hidden_at"])
        self.assertEqual(archived["lifecycle_status"], "archived")
        self.assertTrue(archived["archived"])
        self.assertIsNotNone(archived["archived_at"])
        self.assertEqual(restored["lifecycle_status"], "active")
        self.assertFalse(restored["hidden"])
        self.assertIsNone(restored["hidden_at"])
        self.assertFalse(restored["archived"])
        self.assertIsNone(restored["archived_at"])
        self.assertEqual(plan["lifecycle_status"], "active")

    def test_lifecycle_service_rejects_current_and_active_heartbeat_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_plan(root, "current-run")
            write_plan(root, "active-run")
            heartbeat = root / "runs" / "active-run" / "logs" / "realtime-debug.log"
            heartbeat.parent.mkdir(parents=True)
            heartbeat.write_text('{"type":"heartbeat_sent"}\n', encoding="utf-8")
            os.utime(heartbeat, (1995, 1995))

            with self.assertRaises(RunLifecycleActionError) as current_error:
                set_run_lifecycle_status(root, "current-run", "hidden", current_run_id="current-run")
            with self.assertRaises(RunLifecycleActionError) as heartbeat_error:
                set_run_lifecycle_status(
                    root,
                    "active-run",
                    "archived",
                    active_heartbeat_seconds=30,
                    now=2000,
                )

        self.assertEqual(current_error.exception.reason, "current_run")
        self.assertEqual(heartbeat_error.exception.reason, "active_heartbeat")

    def test_lifecycle_write_does_not_change_cleanup_or_diagnose_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_plan(root, "old-run")
            update_run_lifecycle(root, "old-run", "hidden")

            cleanup = cleanup_temp_runs(root, dry_run=True, tmp_root=None, stale_seconds=60, now=2000)
            diagnose = diagnose_runs(
                root,
                stale_seconds=60,
                now=2000,
                command_runner=lambda _argv: "",
            )

        self.assertEqual(cleanup["protected"][0]["reason"], "non_temporary_run")
        self.assertEqual(diagnose["runs"][0]["cleanup"]["reason"], "non_temporary_run")
        self.assertEqual(diagnose["runs"][0]["lifecycle_status"], "hidden")

    def test_run_api_can_hide_archive_and_restore_inactive_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            write_plan(root, "current-run")
            write_plan(root, "old-run")

            hidden_response = asyncio.run(
                fetch_ui_response(
                    root,
                    "current-run",
                    "/api/runs/old-run/lifecycle",
                    method="PATCH",
                    payload={"status": "hidden"},
                )
            )
            archived_response = asyncio.run(
                fetch_ui_response(
                    root,
                    "current-run",
                    "/api/runs/old-run/lifecycle",
                    method="POST",
                    payload={"status": "archived"},
                )
            )
            restored_response = asyncio.run(
                fetch_ui_response(
                    root,
                    "current-run",
                    "/api/runs/old-run/lifecycle",
                    method="PATCH",
                    payload={"status": "active"},
                )
            )
            hidden_body = json_response_body(hidden_response)
            archived_body = json_response_body(archived_response)
            restored_body = json_response_body(restored_response)

        self.assertTrue(hidden_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(hidden_body["run"]["lifecycle_status"], "hidden")
        self.assertEqual(archived_body["run"]["lifecycle_status"], "archived")
        self.assertEqual(restored_body["run"]["lifecycle_status"], "active")
        self.assertIn("old-run", {item["id"] for item in restored_body["runs"]})

    def test_run_api_rejects_missing_current_and_active_heartbeat_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            write_plan(root, "current-run")
            write_plan(root, "old-run")

            missing_response = asyncio.run(
                fetch_ui_response(
                    root,
                    "current-run",
                    "/api/runs/missing-run/lifecycle",
                    method="PATCH",
                    payload={"status": "hidden"},
                )
            )
            current_response = asyncio.run(
                fetch_ui_response(
                    root,
                    "current-run",
                    "/api/runs/current-run/lifecycle",
                    method="PATCH",
                    payload={"status": "hidden"},
                )
            )
            with mock.patch("aha_cli.services.run_lifecycle_actions.run_has_active_heartbeat", return_value=True):
                heartbeat_response = asyncio.run(
                    fetch_ui_response(
                        root,
                        "current-run",
                        "/api/runs/old-run/lifecycle",
                        method="PATCH",
                        payload={"status": "hidden"},
                    )
                )
            current_body = json_response_body(current_response)
            heartbeat_body = json_response_body(heartbeat_response)

        self.assertTrue(missing_response.startswith(b"HTTP/1.1 404 Not Found"))
        self.assertTrue(current_response.startswith(b"HTTP/1.1 409 Conflict"))
        self.assertEqual(current_body["reason"], "current_run")
        self.assertTrue(heartbeat_response.startswith(b"HTTP/1.1 409 Conflict"))
        self.assertEqual(heartbeat_body["reason"], "active_heartbeat")


if __name__ == "__main__":
    unittest.main()
