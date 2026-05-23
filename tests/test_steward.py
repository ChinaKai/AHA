from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.steward import steward_decision_snapshot
from aha_cli.store.filesystem import (
    add_agent,
    append_message,
    event_path,
    inbox_path,
    iter_jsonl_from,
    mark_task_coordination,
    set_agent_status,
    set_task_status,
    status_snapshot,
    update_task_supervision_config,
)
from tests.helpers import fetch_ui_response, json_response_body


class StewardTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def create_run(self, root: Path) -> str:
        with mock.patch("pathlib.Path.cwd", return_value=root):
            self.run_cli("init", "--portable", "--backend", "codex")
            code, plan_output = self.run_cli("plan", "Steward test", "--agents", "1")
        self.assertEqual(code, 0)
        return plan_output.splitlines()[0].split(": ", 1)[1]

    def test_steward_hands_plan_like_main_reply_to_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            append_message(root, run_id, "main", "开始", sender="browser", task_id="task-001", role="main")
            append_message(
                root,
                run_id,
                "browser",
                "建议下一步先拆出 steward snapshot，然后补测试。",
                sender="main",
                task_id="task-001",
                role="main",
                from_agent="main",
                to_agent="browser",
            )

            snapshot = steward_decision_snapshot(root, run_id, "task-001")

        self.assertEqual(snapshot["decision"]["decision"], "semantic_review")
        self.assertIn("delegated browser-control semantic decision", snapshot["decision"]["reason"])
        self.assertEqual(snapshot["decision"]["prompt_to_main"], "")

    def test_steward_does_not_duplicate_existing_sub_coordination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
            set_agent_status(root, run_id, "task-001", sub["id"], "running")
            set_task_status(root, run_id, "task-001", "running")
            append_message(
                root,
                run_id,
                "browser",
                "我已分配子代理，等待完成。",
                sender="main",
                task_id="task-001",
                role="main",
                from_agent="main",
                to_agent="browser",
            )

            snapshot = steward_decision_snapshot(root, run_id, "task-001")

        self.assertEqual(snapshot["decision"]["decision"], "wait")
        self.assertIn("existing AHA coordination", snapshot["decision"]["reason"])

    def test_steward_respects_round_summary_already_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            mark_task_coordination(root, run_id, "task-001", round_summary_requested_at="2026-05-23T00:00:00+00:00")
            append_message(
                root,
                run_id,
                "browser",
                "子代理完成，等待 main 汇总。",
                sender="main",
                task_id="task-001",
                role="main",
                from_agent="main",
                to_agent="browser",
            )

            snapshot = steward_decision_snapshot(root, run_id, "task-001")

        self.assertEqual(snapshot["decision"]["decision"], "noop")
        self.assertIn("round summary is already requested", snapshot["decision"]["reason"])

    def test_steward_api_returns_read_only_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            append_message(
                root,
                run_id,
                "browser",
                "我准备 git reset --hard，需要你确认。",
                sender="main",
                task_id="task-001",
                role="main",
                from_agent="main",
                to_agent="browser",
            )

            response = asyncio.run(fetch_ui_response(root, run_id, "/api/task/task-001/steward"))
            body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(body["decision"]["decision"], "semantic_review")
        self.assertEqual(body["decision"]["source"], "rules")

    def test_steward_snapshot_exposes_boundary_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)

            snapshot = steward_decision_snapshot(root, run_id, "task-001")

        self.assertEqual(snapshot["boundary_rules"]["steward_allowed_apply_decisions"], [])
        self.assertEqual(snapshot["boundary_rules"]["semantic_handoff_decision"], "semantic_review")
        self.assertEqual(snapshot["boundary_rules"]["semantic_decision_owner"], "delegated_browser_control_host")
        self.assertEqual(
            snapshot["boundary_rules"]["status_channels"],
            ["main_backend", "host_backend", "steward_decision"],
        )

    def test_steward_apply_requests_semantic_review_for_plan_like_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            append_message(
                root,
                run_id,
                "browser",
                "建议下一步先拆出 steward apply，然后补测试。",
                sender="main",
                task_id="task-001",
                role="main",
                from_agent="main",
                to_agent="browser",
            )

            response = asyncio.run(
                fetch_ui_response(
                    root,
                    run_id,
                    "/api/task/task-001/steward/apply",
                    method="POST",
                    payload={"autostart": False},
                )
            )
            body = json_response_body(response)
            main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
            rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]

        self.assertFalse(body["steward"]["applied"])
        self.assertTrue(body["steward"]["semantic_review"])
        self.assertEqual(body["steward"]["semantic_host"]["reason"], "real supervision host is not configured")
        steward_messages = [row for row in main_messages if row.get("coordination") == "steward_continue"]
        self.assertEqual(steward_messages, [])
        self.assertTrue(any(row["type"] == "steward_semantic_review_skipped" for row in rows))

    def test_steward_apply_is_idempotent_for_same_main_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            update_task_supervision_config(
                root,
                run_id,
                "task-001",
                mode="assisted",
                host_backend="codex",
                real_agent_enabled=True,
            )
            append_message(
                root,
                run_id,
                "browser",
                "建议下一步先拆出 steward apply，然后补测试。",
                sender="main",
                task_id="task-001",
                role="main",
                from_agent="main",
                to_agent="browser",
            )

            with mock.patch("aha_cli.services.chat_supervision.start_backend", return_value={"status": "running"}):
                for _ in range(2):
                    response = asyncio.run(
                        fetch_ui_response(
                            root,
                            run_id,
                            "/api/task/task-001/steward/apply",
                            method="POST",
                            payload={"autostart": False},
                        )
                    )
                    self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
            host_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "host"), 0)
            rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]

        routed_messages = [row for row in host_messages if row.get("sender") == "main" and row.get("target") == "host"]
        self.assertEqual(len(routed_messages), 1)
        self.assertTrue(
            any(
                row["type"] == "steward_decision_skipped"
                and row["data"].get("reason") == "semantic_review already queued for latest main reply"
                for row in rows
            )
        )

    def test_steward_apply_does_not_route_unsafe_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            append_message(
                root,
                run_id,
                "browser",
                "我准备 git reset --hard，需要你确认。",
                sender="main",
                task_id="task-001",
                role="main",
                from_agent="main",
                to_agent="browser",
            )

            response = asyncio.run(
                fetch_ui_response(
                    root,
                    run_id,
                    "/api/task/task-001/steward/apply",
                    method="POST",
                    payload={"autostart": False},
                )
            )
            body = json_response_body(response)
            main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
            rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]

        self.assertFalse(body["steward"]["applied"])
        self.assertTrue(body["steward"]["semantic_review"])
        self.assertFalse(any(row.get("coordination") == "steward_continue" for row in main_messages))
        self.assertTrue(any(row["type"] == "steward_semantic_review_skipped" for row in rows))

    def test_steward_apply_skips_semantic_review_without_real_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            append_message(
                root,
                run_id,
                "browser",
                "我再看看。",
                sender="main",
                task_id="task-001",
                role="main",
                from_agent="main",
                to_agent="browser",
            )

            response = asyncio.run(
                fetch_ui_response(
                    root,
                    run_id,
                    "/api/task/task-001/steward/apply",
                    method="POST",
                    payload={"autostart": False},
                )
            )
            body = json_response_body(response)
            rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]

        self.assertFalse(body["steward"]["applied"])
        self.assertTrue(body["steward"]["semantic_review"])
        self.assertEqual(body["steward"]["semantic_host"]["reason"], "real supervision host is not configured")
        self.assertTrue(any(row["type"] == "steward_semantic_review_skipped" for row in rows))

    def test_steward_apply_routes_semantic_review_to_real_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = self.create_run(root)
            update_task_supervision_config(
                root,
                run_id,
                "task-001",
                mode="assisted",
                host_backend="codex",
                real_agent_enabled=True,
            )
            append_message(
                root,
                run_id,
                "browser",
                "我再看看。",
                sender="main",
                task_id="task-001",
                role="main",
                from_agent="main",
                to_agent="browser",
            )

            with mock.patch("aha_cli.services.chat_supervision.start_backend", return_value={"status": "running"}) as start_host:
                response = asyncio.run(
                    fetch_ui_response(
                        root,
                        run_id,
                        "/api/task/task-001/steward/apply",
                        method="POST",
                        payload={"autostart": False},
                    )
                )
            body = json_response_body(response)
            host_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "host"), 0)
            rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]

        self.assertTrue(body["steward"]["applied"])
        self.assertTrue(body["steward"]["semantic_review"])
        self.assertTrue(body["steward"]["semantic_host"]["routed_to_host"])
        start_host.assert_called_once()
        self.assertTrue(any(row.get("sender") == "main" and row.get("target") == "host" and row.get("message") == "我再看看。" for row in host_messages))
        self.assertTrue(any(row["type"] == "main_reported_to_host" for row in rows))
        self.assertTrue(any(row["type"] == "steward_semantic_review_routed" for row in rows))


if __name__ == "__main__":
    unittest.main()
