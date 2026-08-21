from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import main
from aha_cli.services.context_evidence import append_task_context_evidence
from aha_cli.web.task_routes import _hardware_stream_payload, route_task_agent_request


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
        self.assertEqual(payload["summary"]["loop"]["state"], "writeback_applied")
        self.assertEqual(
            [(stage["id"], stage["state"]) for stage in payload["summary"]["loop"]["stages"]],
            [
                ("routed", "complete"),
                ("used", "complete"),
                ("solved", "pending"),
                ("writeback", "complete"),
                ("reused", "pending"),
            ],
        )
        self.assertEqual(payload["summary"]["loop"]["proof"]["helped"], ["navigation narrowed the route"])
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
        self.assertEqual(payload["summary"]["loop"]["state"], "writeback_pending")
        self.assertEqual(payload["summary"]["loop"]["stages"][3]["state"], "pending")
        self.assertEqual(payload["summary"]["kb_growth_state"]["status"], "pending")
        self.assertEqual(payload["kb_growth_state"]["pending"][0]["target_path"], "navigation/index.md")

    def test_ui_server_runs_task_routes_off_event_loop(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "src" / "aha_cli" / "web" / "server.py").read_text(encoding="utf-8")

        self.assertIn("asyncio.to_thread(route_task_agent_request", source)

    def test_browser_action_route_forwards_to_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Browser route", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                with mock.patch(
                    "aha_cli.web.task_routes.browser_bridge_request_sync",
                    return_value={"ok": True, "url": "https://example.com", "revision": 1},
                ) as bridge:
                    response = self.route(
                        root,
                        run_id,
                        "POST",
                        "/api/task/task-001/browser-action",
                        {"action": "navigate", "args": {"url": "https://example.com"}, "source": "agent", "agent_id": "main"},
                    )

        self.assertEqual(response["status"], "200 OK")
        self.assertTrue(response["payload"]["ok"])
        self.assertEqual(response["payload"]["url"], "https://example.com")
        bridge.assert_called_once_with(
            root,
            run_id,
            "task-001",
            "navigate",
            args={"url": "https://example.com"},
            source="agent",
            agent_id="main",
            timeout=30.0,
            parent_bound=True,
        )

    def test_hardware_arm_route_writes_bridge_control(self) -> None:
        from aha_cli.services.hardware_bridge import append_bridge_control

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Hw arm route", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

            device = "COM3"
            with (
                mock.patch("aha_cli.web.task_routes.task_devices", return_value=[(device, 115200)]),
                mock.patch("aha_cli.web.task_routes.task_hardware_debug_can_write", return_value=True),
                mock.patch("aha_cli.web.task_routes.append_bridge_control", side_effect=append_bridge_control) as control,
            ):
                response = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/task/task-001/hardware-arm",
                    {"channel": "serial", "pattern": "stop autoboot", "send": "\\r", "max_fires": 1},
                )

        self.assertEqual(response["status"], "200 OK")
        self.assertTrue(response["payload"]["ok"])
        self.assertEqual(response["payload"]["device"], device)
        control.assert_called_once()
        self.assertEqual(control.call_args.args[2]["cmd"], "arm")
        self.assertEqual(control.call_args.args[2]["pattern"], "stop autoboot")

    def test_hardware_stop_route_writes_bridge_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Hw stop route", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

            device = "COM3"
            with (
                mock.patch("aha_cli.web.task_routes.task_devices", return_value=[(device, 115200)]),
                mock.patch("aha_cli.web.task_routes.append_bridge_control") as control,
            ):
                response = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/task/task-001/hardware-stop",
                    {"channel": "serial"},
                )

        self.assertEqual(response["status"], "200 OK")
        self.assertTrue(response["payload"]["ok"])
        self.assertEqual(response["payload"]["command"], "stop")
        control.assert_called_once_with(root, device, {"cmd": "stop"})

    def test_hardware_attach_route_ensures_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Hw attach route", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

            device = "COM6"
            with (
                mock.patch("aha_cli.web.task_routes.task_devices", return_value=[(device, 115200)]),
                mock.patch("aha_cli.web.task_routes.ensure_bridge", return_value={"status": "running"}) as ensure,
            ):
                response = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/task/task-001/hardware-attach",
                    {"channel": "serial"},
                )

        self.assertEqual(response["status"], "200 OK")
        self.assertTrue(response["payload"]["ok"])
        self.assertEqual(response["payload"]["device"], device)
        self.assertEqual(response["payload"]["bridge"]["status"], "running")
        ensure.assert_called_once_with(root, device, 115200)

    def test_hardware_stream_defaults_to_first_group_id(self) -> None:
        task = {
            "status": "running",
            "hardware_debug": {
                "groups": [
                    {
                        "id": "console",
                        "mode": "serial",
                        "serial": {"device": "COM6", "baudrate": 115200},
                        "permissions": {"access": "read_write"},
                    }
                ]
            },
        }
        with (
            mock.patch("aha_cli.web.task_routes.task_snapshot", return_value={"task": task}),
            mock.patch("aha_cli.web.task_routes.ensure_bridge"),
            mock.patch(
                "aha_cli.web.task_routes.device_stream_page",
                return_value={"events": [], "after_offset": 0, "has_more": False},
            ),
            mock.patch("aha_cli.web.task_routes.bridge_status", return_value={"status": "running"}),
        ):
            payload = _hardware_stream_payload(
                Path("/tmp/aha-test"),
                "run-001",
                "task-001",
                after=None,
                limit=100,
            )

        self.assertEqual(payload["hardware"], "console")
        self.assertEqual(payload["transport"], "serial")
        self.assertEqual(payload["endpoint"], "COM6")

    def test_hardware_send_routes_named_hardware_group_to_its_serial_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Relay route", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

            with (
                mock.patch("aha_cli.web.task_routes.task_hardware_debug_can_write", return_value=True),
                mock.patch("aha_cli.web.task_routes.task_serial_group_target", return_value=("COM7", 9600)),
                mock.patch("aha_cli.web.task_routes.ensure_bridge", return_value={"status": "running"}) as ensure,
                mock.patch("aha_cli.web.task_routes.append_bridge_control", return_value={"id": 1}) as control,
            ):
                response = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/task/task-001/hardware-send",
                    {"hardware": "power", "data": "\\xA0\\x01\\x01\\xA2"},
                )

        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(response["payload"]["hardware"], "power")
        self.assertEqual(response["payload"]["device"], "COM7")
        ensure.assert_called_once_with(root, "COM7", 9600)
        control.assert_called_once_with(
            root,
            "COM7",
            {"cmd": "send", "data": "\\xA0\\x01\\x01\\xA2", "source": "web"},
        )

    def test_hardware_disarm_route_writes_bridge_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Hw disarm route", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

            device = "COM3"
            with (
                mock.patch("aha_cli.web.task_routes.task_devices", return_value=[(device, 115200)]),
                mock.patch("aha_cli.web.task_routes.append_bridge_control") as control,
            ):
                response = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/task/task-001/hardware-disarm",
                    {"channel": "serial", "id": "rule-1"},
                )

        self.assertEqual(response["status"], "200 OK")
        self.assertTrue(response["payload"]["ok"])
        self.assertEqual(response["payload"]["id"], "rule-1")
        control.assert_called_once_with(root, device, {"cmd": "disarm", "id": "rule-1"})

    def test_task_title_route_updates_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Rename task", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                response = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/task/task-001/title",
                    {"title": "新的任务标题"},
                )

        self.assertEqual(response["status"], "200 OK")
        self.assertTrue(response["payload"]["ok"])
        self.assertEqual(response["payload"]["title"], "新的任务标题")
        self.assertEqual(response["payload"]["task"]["title"], "新的任务标题")

    def test_task_title_route_rejects_empty_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "stub")
                code, plan_output = self.run_cli("plan", "Rename task", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                response = self.route(
                    root,
                    run_id,
                    "POST",
                    "/api/task/task-001/title",
                    {"title": "   "},
                )

        self.assertEqual(response["status"], "400 Bad Request")
        self.assertFalse(response["payload"]["ok"])


if __name__ == "__main__":
    unittest.main()
