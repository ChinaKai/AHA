from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.context_evidence import append_task_context_evidence
from aha_cli.web.task_routes import route_task_agent_request


class WebTaskRouteTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def route(
        self,
        root: Path,
        run_id: str,
        method: str,
        path: str,
        payload: dict | None = None,
        query: dict[str, list[str]] | None = None,
    ) -> dict:
        body = json.dumps(payload or {}).encode("utf-8")
        return route_task_agent_request(root, run_id, method, path, query or {}, body)

    def test_task_agent_routes_return_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Task routes", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                detail = self.route(root, run_id, "GET", "/api/task/task-001")
                created = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/tasks",
                    {"title": "Created through route", "backend": "stub", "dispatch": False},
                )
                agent = self.route(root, run_id, "POST", "/api/agents", {"task_id": "task-001", "backend": "stub"})
                agent_config = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/agent-config",
                    {"task_id": "task-001", "agent_id": agent["payload"]["agent"]["id"], "sandbox": "read-only"},
                )
                task_config = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/task-config",
                    {"task_id": "task-001", "proxy_enabled": True, "http_proxy": "http://proxy.local:8080"},
                )
                sent = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/send",
                    {"target": "main", "task_id": "task-001", "role": "main", "sender": "browser", "message": "hello"},
                )
                hidden = self.route(root, run_id, "POST", "/api/task/task-001/hide")

        self.assertEqual(detail["status"], "200 OK")
        self.assertEqual(detail["payload"]["task"]["id"], "task-001")
        self.assertEqual(created["payload"]["task"]["title"], "Created through route")
        self.assertEqual(agent["payload"]["agent"]["role"], "sub")
        self.assertEqual(agent_config["payload"]["agent"]["sandbox"], "read-only")
        self.assertTrue(task_config["payload"]["task"]["preferred_proxy_enabled"])
        self.assertEqual(sent["payload"]["message"]["message"], "hello")
        self.assertTrue(hidden["payload"]["task"]["hidden"])

    def test_task_context_evidence_route_returns_recent_records_and_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Context evidence route", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_task_context_evidence(
                    root,
                    run_id,
                    "task-001",
                    {"type": "context_pack", "agent_id": "main", "evidence": {"text_sha": "pack"}},
                )
                append_task_context_evidence(
                    root,
                    run_id,
                    "task-001",
                    {
                        "type": "context_evidence_result",
                        "agent_id": "main",
                        "signals": ["missing_nav"],
                        "maintenance_suggestions": [
                            {"action": "update", "target": "project_navigation", "reason": "missing_nav"}
                        ],
                        "maintenance_plan": [
                            {
                                "action": "update",
                                "target": "project_navigation",
                                "target_path": "navigation/index.md",
                                "reason": "missing_nav",
                                "write_policy": "direct_project_navigation_update",
                            }
                        ],
                    },
                )
                append_task_context_evidence(
                    root,
                    run_id,
                    "task-001",
                    {
                        "type": "context_evidence_result",
                        "agent_id": "main",
                        "signals": ["nav_stale"],
                        "routing_health": {
                            "status": "stale",
                            "downrank_paths": ["docs/old-guide.md"],
                            "prioritize_paths": ["docs/new-guide.md"],
                        },
                        "kb_scope_policy": {
                            "project_navigation": "direct_edit_approved_markdown_with_task_evidence",
                            "general_personal_wiki": "manual_candidate_review_only",
                        },
                        "maintenance_suggestions": [
                            {"action": "repair", "target": "project_navigation", "reason": "nav_stale"},
                            {"action": "update", "target": "project_navigation", "reason": "missing_nav"},
                        ],
                        "maintenance_plan": [
                            {
                                "action": "repair",
                                "target": "project_navigation",
                                "target_path": "navigation/index.md",
                                "reason": "nav_stale",
                                "write_policy": "direct_project_navigation_update",
                                "execution": {"state": "ready", "mode": "direct_edit"},
                            },
                            {
                                "action": "update",
                                "target": "project_navigation",
                                "target_path": "navigation/index.md",
                                "reason": "missing_nav",
                                "write_policy": "direct_project_navigation_update",
                                "execution": {"state": "ready", "mode": "direct_edit"},
                            },
                        ],
                    },
                )
                append_task_context_evidence(
                    root,
                    run_id,
                    "task-001",
                    {
                        "type": "agent_kb_feedback",
                        "agent_id": "main",
                        "feedback": {
                            "helped": ["navigation narrowed the route"],
                            "updated": ["navigation/index.md"],
                        },
                    },
                )
                response = self.route(root, run_id, "GET", "/api/task/task-001/context-evidence", query={"limit": ["2"]})
                result_only = self.route(
                    root,
                    run_id,
                    "GET",
                    "/api/task/task-001/context-evidence",
                    query={"type": ["context_evidence_result"]},
                )
                invalid_limit = self.route(
                    root,
                    run_id,
                    "GET",
                    "/api/task/task-001/context-evidence",
                    query={"limit": ["abc"]},
                )

        self.assertEqual(response["status"], "200 OK")
        payload = response["payload"]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["task_id"], "task-001")
        self.assertEqual(payload["count"], 4)
        self.assertEqual(payload["limit"], 2)
        self.assertEqual([record["type"] for record in payload["records"]], ["context_evidence_result", "agent_kb_feedback"])
        self.assertEqual(payload["latest_result"]["signals"], ["nav_stale"])
        self.assertEqual(
            [(item["action"], item["target"], item["reason"]) for item in payload["maintenance_suggestions"]],
            [
                ("repair", "project_navigation", "nav_stale"),
                ("update", "project_navigation", "missing_nav"),
            ],
        )
        self.assertEqual(
            [(item["action"], item["target"], item["target_path"], item["reason"]) for item in payload["maintenance_plan"]],
            [
                ("repair", "project_navigation", "navigation/index.md", "nav_stale"),
                ("update", "project_navigation", "navigation/index.md", "missing_nav"),
            ],
        )
        self.assertEqual(payload["routing_health"]["status"], "stale")
        self.assertEqual(payload["routing_health"]["downrank_paths"], ["docs/old-guide.md"])
        self.assertEqual(payload["kb_scope_policy"]["general_personal_wiki"], "manual_candidate_review_only")
        self.assertEqual(payload["summary"]["scope"], "task")
        self.assertEqual(payload["summary"]["generated_by"], "aha_runtime")
        self.assertEqual(payload["summary"]["feedback_mode"], "agent_feedback_plus_runtime")
        self.assertEqual(payload["summary"]["status"]["state"], "stale")
        self.assertEqual(payload["summary"]["next_action"]["label"], "Repair project navigation")
        self.assertEqual(payload["summary"]["next_action"]["target_path"], "navigation/index.md")
        self.assertEqual(payload["summary"]["record_type_counts"]["context_pack"], 1)
        self.assertEqual(payload["summary"]["record_type_counts"]["context_evidence_result"], 2)
        self.assertEqual(payload["summary"]["record_type_counts"]["agent_kb_feedback"], 1)
        self.assertEqual(payload["summary"]["agent_feedback_count"], 1)
        self.assertEqual(payload["summary"]["latest_agent_feedback"]["updated"], ["navigation/index.md"])
        self.assertIn("after_turn_runtime_distill", payload["summary"]["evidence_sources"])
        self.assertIn("agent_kb_feedback", payload["summary"]["evidence_sources"])
        self.assertEqual(result_only["payload"]["count"], 2)
        self.assertTrue(all(record["type"] == "context_evidence_result" for record in result_only["payload"]["records"]))
        self.assertEqual(invalid_limit["status"], "400 Bad Request")
        self.assertIn("limit must be an integer", invalid_limit["payload"]["error"])

    def test_task_context_evidence_route_surfaces_pending_kb_growth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Context evidence growth", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_task_context_evidence(
                    root,
                    run_id,
                    "task-001",
                    {
                        "type": "context_evidence_result",
                        "agent_id": "main",
                        "signals": ["missing_nav"],
                        "routing_health": {"status": "needs_repair"},
                        "maintenance_plan": [
                            {
                                "action": "update",
                                "target": "project_navigation",
                                "target_path": "navigation/index.md",
                                "reason": "missing_nav",
                                "write_policy": "direct_project_navigation_update",
                            }
                        ],
                        "kb_growth_state": {
                            "status": "pending",
                            "required_count": 1,
                            "applied_count": 0,
                            "pending_count": 1,
                            "pending": [
                                {
                                    "target": "project_navigation",
                                    "target_path": "navigation/index.md",
                                    "reason": "missing_nav",
                                }
                            ],
                            "applied": [],
                        },
                    },
                )
                response = self.route(root, run_id, "GET", "/api/task/task-001/context-evidence")

        payload = response["payload"]
        self.assertEqual(payload["summary"]["status"]["state"], "growth_pending")
        self.assertEqual(payload["summary"]["kb_growth_state"]["status"], "pending")
        self.assertEqual(payload["kb_growth_state"]["pending"][0]["target_path"], "navigation/index.md")

    def test_ui_server_runs_task_routes_off_event_loop(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "src" / "aha_cli" / "web" / "server.py").read_text(encoding="utf-8")

        self.assertIn("asyncio.to_thread(route_task_agent_request", source)


if __name__ == "__main__":
    unittest.main()
