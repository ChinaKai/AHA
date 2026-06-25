from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import parse_qs

from aha_cli.services import token_usage
from aha_cli.services.token_usage import CcusageUnavailable, daily_token_usage_report
from aha_cli.store.runs import save_plan
from aha_cli.web.system_routes import system_route_response
from tests.helpers import json_response_body


class TokenUsageTests(unittest.TestCase):
    @mock.patch("aha_cli.services.token_usage._run_ccusage_json")
    def test_daily_report_adapts_ccusage_daily_json(self, run_ccusage: mock.Mock) -> None:
        run_ccusage.return_value = {
            "daily": [
                {
                    "period": "2026-06-23",
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
        self.assertTrue(report["available"])
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
    def test_daily_report_expands_detected_agent_breakdowns(self, run_ccusage: mock.Mock) -> None:
        run_ccusage.side_effect = [
            {
                "daily": [
                    {
                        "period": "2026-06-23",
                        "agent": "all",
                        "metadata": {"agents": ["claude", "codex"]},
                        "inputTokens": 150,
                        "cacheReadTokens": 300,
                        "cacheCreationTokens": 40,
                        "outputTokens": 60,
                        "totalTokens": 550,
                    }
                ],
                "totals": {
                    "inputTokens": 150,
                    "cacheReadTokens": 300,
                    "cacheCreationTokens": 40,
                    "outputTokens": 60,
                    "totalTokens": 550,
                },
            },
            {
                "daily": [
                    {
                        "date": "2026-06-23",
                        "modelsUsed": ["opus-4-8"],
                        "inputTokens": 20,
                        "cacheReadTokens": 50,
                        "cacheCreationTokens": 40,
                        "outputTokens": 10,
                        "totalTokens": 120,
                    }
                ],
                "totals": {"inputTokens": 20, "cacheReadTokens": 50, "cacheCreationTokens": 40, "outputTokens": 10, "totalTokens": 120},
            },
            {
                "daily": [
                    {
                        "date": "2026-06-23",
                        "models": ["gpt-5.5"],
                        "inputTokens": 130,
                        "cacheReadTokens": 250,
                        "cacheCreationTokens": 0,
                        "outputTokens": 50,
                        "totalTokens": 430,
                    }
                ],
                "totals": {"inputTokens": 130, "cacheReadTokens": 250, "outputTokens": 50, "totalTokens": 430},
            },
        ]

        with TemporaryDirectory() as tmp:
            report = daily_token_usage_report(
                Path(tmp),
                "run-usage",
                timezone="Asia/Shanghai",
                since="2026-06-23",
                until="2026-06-23",
            )

        self.assertEqual(
            [call.args[0][:2] for call in run_ccusage.call_args_list],
            [["daily", "--json"], ["claude", "daily"], ["codex", "daily"]],
        )
        self.assertEqual(report["matched_events"], 1)
        day = report["days"][0]
        self.assertEqual(day["date"], "2026-06-23")
        self.assertEqual(day["total_tokens"], 550)
        self.assertEqual(
            [(row["backend"], row["models"], row["total_tokens"]) for row in day["by_backend"]],
            [("claude", ["opus-4-8"], 120), ("codex", ["gpt-5.5"], 430)],
        )

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

    @mock.patch.dict("aha_cli.services.token_usage.os.environ", {"PATH": "/usr/bin"}, clear=True)
    @mock.patch("aha_cli.services.token_usage.subprocess.run")
    @mock.patch("aha_cli.services.token_usage.shutil.which")
    @mock.patch("aha_cli.services.token_usage.add_user_backend_paths")
    def test_ccusage_command_discovery_uses_user_backend_paths(
        self,
        add_paths: mock.Mock,
        which: mock.Mock,
        run: mock.Mock,
    ) -> None:
        nvm_bin = "/home/test/.nvm/versions/node/v24.15.0/bin"

        def add_user_paths(env: dict[str, str], *, home: Path | None = None) -> None:
            env["PATH"] = f"{nvm_bin}:/usr/bin"

        def which_command(name: str, path: str | None = None) -> str | None:
            if name == "npx" and path and nvm_bin in path:
                return f"{nvm_bin}/npx"
            return None

        add_paths.side_effect = add_user_paths
        which.side_effect = which_command
        run.return_value = SimpleNamespace(returncode=0, stdout="{}", stderr="")

        token_usage._run_ccusage_json(["daily", "--json"])

        command = run.call_args.args[0]
        self.assertEqual(command[:3], [f"{nvm_bin}/npx", "--yes", "ccusage@20.0.14"])
        self.assertIn(nvm_bin, run.call_args.kwargs["env"]["PATH"])

    @mock.patch("aha_cli.services.token_usage._run_ccusage_json")
    def test_daily_report_marks_missing_ccusage_unavailable(self, run_ccusage: mock.Mock) -> None:
        run_ccusage.side_effect = CcusageUnavailable("ccusage command not found; set AHA_CCUSAGE_COMMAND")

        with TemporaryDirectory() as tmp:
            report = daily_token_usage_report(Path(tmp), "run-usage", backend="codex")

        self.assertFalse(report["available"])
        self.assertEqual(report["source"], "ccusage")
        self.assertIn("AHA_CCUSAGE_COMMAND", report["unavailable_reason"])
        self.assertEqual(report["matched_events"], 0)
        self.assertEqual(report["totals"]["total_tokens"], 0)
        self.assertEqual(report["days"], [])
        self.assertEqual(report["ccusage_args"][0:2], ["codex", "daily"])

    def test_daily_report_rejects_filters_ccusage_cannot_apply(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "task_id and target filters"):
                daily_token_usage_report(root, "run-usage", task_id="task-001")
            with self.assertRaisesRegex(ValueError, "unsupported ccusage backend"):
                daily_token_usage_report(root, "run-usage", backend="unknown-backend")
            with self.assertRaisesRegex(ValueError, "since must be today or earlier"):
                daily_token_usage_report(root, "run-usage", since="2999-01-01")

    @mock.patch("aha_cli.services.token_usage._run_ccusage_json")
    def test_daily_route_reads_cache_and_refreshes_in_background(self, run_ccusage: mock.Mock) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-usage"
            save_plan(root, {"id": run_id, "goal": "Usage", "mode": "research", "tasks": [], "write_scopes": []})
            run_ccusage.return_value = {
                "daily": [{"date": "2026-06-23", "inputTokens": 10, "outputTokens": 5, "totalTokens": 15}],
                "totals": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
            }

            query = parse_qs("timezone=UTC&backend=codex&since=2026-06-23&until=2026-06-23&offline=1")
            cached_empty = system_route_response(
                root,
                run_id,
                "GET",
                "/api/usage/daily",
                query,
            )
            self.assertEqual(run_ccusage.call_count, 0)

            with mock.patch("aha_cli.services.token_usage.dispatch_daily_usage_refresh_job") as dispatch:
                dispatch.side_effect = (
                    lambda refresh_root, refresh_run_id, request: token_usage.run_daily_token_usage_refresh_job(
                        refresh_root,
                        refresh_run_id,
                        request,
                    )
                )
                refresh = system_route_response(root, run_id, "POST", "/api/usage/daily/refresh", query)

            cached = system_route_response(root, run_id, "GET", "/api/usage/daily", query)
            cached_other_since = system_route_response(
                root,
                run_id,
                "GET",
                "/api/usage/daily",
                parse_qs("timezone=UTC&backend=codex&since=2026-06-24"),
            )
            head = system_route_response(root, run_id, "HEAD", "/api/usage/daily", query)
            invalid = system_route_response(root, run_id, "GET", "/api/usage/daily", parse_qs("timezone=Nope/Nope"))
            invalid_future = system_route_response(root, run_id, "GET", "/api/usage/daily", parse_qs("since=2999-01-01"))
            stopped = system_route_response(root, run_id, "POST", "/api/usage/daily/stop", query)

            with mock.patch("aha_cli.services.token_usage.dispatch_daily_usage_refresh_job") as dispatch:
                dispatch.side_effect = (
                    lambda refresh_root, refresh_run_id, request: token_usage.run_daily_token_usage_refresh_job(
                        refresh_root,
                        refresh_run_id,
                        request,
                    )
                )
                run_ccusage.side_effect = CcusageUnavailable("ccusage command not found; set AHA_CCUSAGE_COMMAND")
                unavailable = system_route_response(root, run_id, "POST", "/api/usage/daily/refresh", parse_qs("timezone=UTC"))

                run_ccusage.side_effect = RuntimeError("ccusage unavailable")
                failed = system_route_response(root, run_id, "POST", "/api/usage/daily/refresh", parse_qs("timezone=UTC&backend=codex"))

        self.assertTrue(cached_empty and cached_empty.startswith(b"HTTP/1.1 200 OK"))
        empty_body = json_response_body(cached_empty)
        self.assertEqual(empty_body["cache"]["status"], "missing")
        self.assertIsNone(empty_body["available"])
        self.assertEqual(empty_body["matched_events"], 0)
        self.assertTrue(refresh and refresh.startswith(b"HTTP/1.1 200 OK"))
        refresh_body = json_response_body(refresh)
        self.assertTrue(refresh_body["ok"])
        self.assertEqual(refresh_body["refresh"]["status"], "succeeded")
        self.assertEqual(refresh_body["cache"]["status"], "ready")
        self.assertTrue(cached and cached.startswith(b"HTTP/1.1 200 OK"))
        body = json_response_body(cached)
        self.assertEqual(body["source"], "ccusage")
        self.assertTrue(body["available"])
        self.assertEqual(body["filters"]["backend"], "codex")
        other_since_body = json_response_body(cached_other_since)
        self.assertEqual(other_since_body["totals"]["input_tokens"], 10)
        self.assertEqual(other_since_body["filters"]["since"], "2026-06-23")
        self.assertIn("--offline", body["ccusage_args"])
        self.assertEqual(body["totals"]["input_tokens"], 10)
        self.assertTrue(head and head.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(head.split(b"\r\n\r\n", 1)[1], b"")
        self.assertTrue(invalid and invalid.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertTrue(invalid_future and invalid_future.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertTrue(stopped and stopped.startswith(b"HTTP/1.1 200 OK"))
        self.assertFalse(json_response_body(stopped)["stopped"])
        self.assertTrue(unavailable and unavailable.startswith(b"HTTP/1.1 200 OK"))
        unavailable_body = json_response_body(unavailable)
        self.assertFalse(unavailable_body["available"])
        self.assertIn("AHA_CCUSAGE_COMMAND", unavailable_body["unavailable_reason"])
        self.assertEqual(unavailable_body["refresh"]["status"], "succeeded")
        self.assertTrue(failed and failed.startswith(b"HTTP/1.1 200 OK"))
        failed_body = json_response_body(failed)
        self.assertEqual(failed_body["refresh"]["status"], "failed")
        self.assertIn("ccusage unavailable", failed_body["refresh"]["error"])
