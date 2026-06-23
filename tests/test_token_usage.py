from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs

from aha_cli.services.token_usage import daily_token_usage_report, normalize_agent_usage_event
from aha_cli.store.events import append_event
from aha_cli.store.runs import save_plan
from aha_cli.web.system_routes import system_route_response
from tests.helpers import json_response_body


class TokenUsageTests(unittest.TestCase):
    def test_normalize_codex_usage_splits_cached_input(self) -> None:
        normalized = normalize_agent_usage_event(
            {
                "ts": "2026-06-23T01:00:00+00:00",
                "type": "agent_usage",
                "data": {
                    "source": "codex",
                    "task_id": "task-001",
                    "target": "main",
                    "usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 750,
                        "output_tokens": 120,
                        "reasoning_output_tokens": 30,
                        "total_tokens": 1150,
                    },
                },
            }
        )

        self.assertIsNotNone(normalized)
        usage = normalized["usage"] if normalized else {}
        self.assertEqual(usage["input_tokens"], 1000)
        self.assertEqual(usage["billable_input_tokens"], 250)
        self.assertEqual(usage["cache_read_tokens"], 750)
        self.assertEqual(usage["cache_creation_tokens"], 0)
        self.assertEqual(usage["total_tokens"], 1150)

    def test_daily_report_groups_by_timezone_and_backend(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-usage"
            save_plan(root, {"id": run_id, "goal": "Usage", "mode": "research", "tasks": [], "write_scopes": []})
            append_event(
                root,
                run_id,
                "agent_usage",
                {
                    "source": "codex",
                    "task_id": "task-001",
                    "target": "main",
                    "usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 700,
                        "output_tokens": 100,
                        "reasoning_output_tokens": 50,
                        "total_tokens": 1150,
                    },
                },
                ts="2026-06-22T16:30:00+00:00",
            )
            append_event(
                root,
                run_id,
                "agent_usage",
                {
                    "source": "claude",
                    "task_id": "task-002",
                    "target": "sub-001",
                    "usage": {
                        "input_tokens": 200,
                        "cache_read_input_tokens": 80,
                        "cache_creation_input_tokens": 40,
                        "output_tokens": 25,
                        "total_tokens": 999,
                        "total_cost_usd": 0.0123,
                    },
                },
                ts="2026-06-23T03:15:00+00:00",
            )

            report = daily_token_usage_report(root, run_id, timezone="Asia/Shanghai")

        self.assertEqual(report["matched_events"], 2)
        self.assertEqual([day["date"] for day in report["days"]], ["2026-06-23"])
        day = report["days"][0]
        self.assertEqual(day["event_count"], 2)
        self.assertEqual(day["input_tokens"], 1200)
        self.assertEqual(day["billable_input_tokens"], 500)
        self.assertEqual(day["cache_read_tokens"], 780)
        self.assertEqual(day["cache_creation_tokens"], 40)
        self.assertEqual(day["output_tokens"], 125)
        self.assertEqual(day["reasoning_output_tokens"], 50)
        self.assertEqual(day["total_tokens"], 1495)
        self.assertAlmostEqual(day["cost_usd"], 0.0123)
        self.assertEqual([item["backend"] for item in day["by_backend"]], ["codex", "claude"])
        self.assertEqual(report["totals"]["total_tokens"], 1495)

    def test_daily_route_supports_filters_and_head(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-usage"
            save_plan(root, {"id": run_id, "goal": "Usage", "mode": "research", "tasks": [], "write_scopes": []})
            append_event(
                root,
                run_id,
                "agent_usage",
                {"source": "codex", "task_id": "task-001", "target": "main", "usage": {"input_tokens": 10, "output_tokens": 5}},
                ts="2026-06-23T00:00:00+00:00",
            )
            append_event(
                root,
                run_id,
                "agent_usage",
                {"source": "claude", "task_id": "task-002", "target": "main", "usage": {"input_tokens": 20, "output_tokens": 5}},
                ts="2026-06-23T00:01:00+00:00",
            )

            response = system_route_response(
                root,
                run_id,
                "GET",
                "/api/usage/daily",
                parse_qs("timezone=UTC&backend=codex&since=2026-06-23&until=2026-06-23"),
            )
            head = system_route_response(root, run_id, "HEAD", "/api/usage/daily", parse_qs("timezone=UTC"))
            invalid = system_route_response(root, run_id, "GET", "/api/usage/daily", parse_qs("timezone=Nope/Nope"))

        self.assertTrue(response and response.startswith(b"HTTP/1.1 200 OK"))
        body = json_response_body(response)
        self.assertEqual(body["matched_events"], 1)
        self.assertEqual(body["totals"]["input_tokens"], 10)
        self.assertEqual(body["filters"]["backend"], "codex")
        self.assertTrue(head and head.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(head.split(b"\r\n\r\n", 1)[1], b"")
        self.assertTrue(invalid and invalid.startswith(b"HTTP/1.1 400 Bad Request"))
