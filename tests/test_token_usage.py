from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
from urllib.parse import parse_qs

from aha_cli.services.token_usage import daily_token_usage_report
from aha_cli.store.runs import save_plan
from aha_cli.web.system_routes import system_route_response
from tests.helpers import json_response_body


class TokenUsageTests(unittest.TestCase):
    @mock.patch("aha_cli.services.token_usage._run_ccusage_json")
    def test_daily_report_adapts_ccusage_daily_json(self, run_ccusage: mock.Mock) -> None:
        run_ccusage.return_value = {
            "daily": [
                {
                    "date": "2026-06-23",
                    "inputTokens": 600,
                    "cacheReadTokens": 680,
                    "cacheCreationTokens": 40,
                    "outputTokens": 145,
                    "reasoningOutputTokens": 30,
                    "totalTokens": 1495,
                    "costUSD": 0.012345,
                    "modelBreakdowns": [
                        {
                            "modelName": "gpt-5.2",
                            "inputTokens": 500,
                            "cacheReadTokens": 600,
                            "outputTokens": 100,
                            "reasoningOutputTokens": 25,
                            "totalTokens": 1225,
                            "costUSD": 0.01,
                        }
                    ],
                }
            ],
            "totals": {
                "inputTokens": 600,
                "cacheReadTokens": 680,
                "cacheCreationTokens": 40,
                "outputTokens": 145,
                "reasoningOutputTokens": 30,
                "totalTokens": 1495,
                "costUSD": 0.012345,
            },
        }

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = daily_token_usage_report(
                root,
                "run-usage",
                timezone="Asia/Shanghai",
                since="2026-06-23",
                until="2026-06-23",
            )

        self.assertEqual(
            run_ccusage.call_args.args[0],
            [
                "daily",
                "--json",
                "--timezone",
                "Asia/Shanghai",
                "--since",
                "2026-06-23",
                "--until",
                "2026-06-23",
            ],
        )
        self.assertEqual(report["source"], "ccusage")
        self.assertEqual(report["timezone"], "Asia/Shanghai")
        self.assertEqual(report["matched_events"], 1)
        self.assertEqual(report["totals"]["input_tokens"], 600)
        self.assertEqual(report["totals"]["billable_input_tokens"], 600)
        self.assertEqual(report["totals"]["cache_read_tokens"], 680)
        self.assertEqual(report["totals"]["cache_creation_tokens"], 40)
        self.assertEqual(report["totals"]["output_tokens"], 145)
        self.assertEqual(report["totals"]["reasoning_output_tokens"], 30)
        self.assertEqual(report["totals"]["total_tokens"], 1495)
        self.assertAlmostEqual(report["totals"]["cost_usd"], 0.012345)
        day = report["days"][0]
        self.assertEqual(day["date"], "2026-06-23")
        self.assertEqual(day["by_backend"][0]["backend"], "ccusage")
        self.assertEqual(day["by_backend"][0]["model"], "gpt-5.2")
        self.assertEqual(day["by_backend"][0]["total_tokens"], 1225)
        self.assertEqual(day["by_task"], [])

    @mock.patch("aha_cli.services.token_usage._run_ccusage_json")
    def test_daily_report_uses_backend_specific_ccusage_command(self, run_ccusage: mock.Mock) -> None:
        run_ccusage.return_value = {
            "daily": [{"date": "2026-06-23", "inputTokens": 10, "outputTokens": 5, "totalTokens": 15}],
            "totals": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        }

        with TemporaryDirectory() as tmp:
            report = daily_token_usage_report(Path(tmp), "run-codex", backend="codex", offline=True)

        self.assertEqual(run_ccusage.call_args.args[0], ["codex", "daily", "--json", "--timezone", "UTC", "--offline"])
        self.assertEqual(report["filters"]["backend"], "codex")
        self.assertEqual(report["ccusage_args"][0:2], ["codex", "daily"])

    def test_daily_report_rejects_filters_ccusage_cannot_apply(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "task_id and target filters"):
                daily_token_usage_report(root, "run-usage", task_id="task-001")
            with self.assertRaisesRegex(ValueError, "unsupported ccusage backend"):
                daily_token_usage_report(root, "run-usage", backend="unknown-backend")

    @mock.patch("aha_cli.services.token_usage._run_ccusage_json")
    def test_daily_route_supports_ccusage_response_head_and_errors(self, run_ccusage: mock.Mock) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-usage"
            save_plan(root, {"id": run_id, "goal": "Usage", "mode": "research", "tasks": [], "write_scopes": []})
            run_ccusage.return_value = {
                "daily": [{"date": "2026-06-23", "inputTokens": 10, "outputTokens": 5, "totalTokens": 15}],
                "totals": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
            }

            response = system_route_response(
                root,
                run_id,
                "GET",
                "/api/usage/daily",
                parse_qs("timezone=UTC&backend=codex&since=2026-06-23&until=2026-06-23&offline=1"),
            )
            head = system_route_response(root, run_id, "HEAD", "/api/usage/daily", parse_qs("timezone=UTC"))
            invalid = system_route_response(root, run_id, "GET", "/api/usage/daily", parse_qs("timezone=Nope/Nope"))

            run_ccusage.side_effect = RuntimeError("ccusage unavailable")
            gateway = system_route_response(root, run_id, "GET", "/api/usage/daily", parse_qs("timezone=UTC"))

        self.assertTrue(response and response.startswith(b"HTTP/1.1 200 OK"))
        body = json_response_body(response)
        self.assertEqual(body["source"], "ccusage")
        self.assertEqual(body["filters"]["backend"], "codex")
        self.assertIn("--offline", body["ccusage_args"])
        self.assertEqual(body["totals"]["input_tokens"], 10)
        self.assertTrue(head and head.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(head.split(b"\r\n\r\n", 1)[1], b"")
        self.assertTrue(invalid and invalid.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertTrue(gateway and gateway.startswith(b"HTTP/1.1 502 Bad Gateway"))
