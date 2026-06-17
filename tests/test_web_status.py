from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main, task_snapshot
from aha_cli.services.chat import chat_offset_path, save_chat_offset
from aha_cli.store.event_views import conversation_events_page
from aha_cli.store.filesystem import (
    add_agent,
    complete_task,
    event_path,
    inbox_path,
    iter_jsonl_from,
    mark_task_coordination,
    run_dir,
    set_agent_status,
    set_task_status,
    status_snapshot,
    update_task_context_management_config,
    update_agent_runtime,
)
from aha_cli.store.snapshots import status_snapshot_projection as raw_status_snapshot_projection
from aha_cli.store.sessions import ensure_session, save_session
from aha_cli.web import status as web_status_module
from aha_cli.web.server import handle_send_payload, recover_stale_running_agent, web_status_snapshot
from aha_cli.web.status import (
    cached_backend_status,
    recover_stale_running_agents,
    web_agents_runtime_snapshot,
    web_tasks_snapshot,
)
from tests.helpers import isolated_cli_environment


class WebStatusTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with isolated_cli_environment(), mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_status_exports_do_not_include_removed_auto_compact_hook(self) -> None:
        self.assertNotIn("auto_compact_agent_context_if_needed", web_status_module.__all__)

    def test_web_status_snapshot_includes_agent_backend_process_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Backend badges", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                mark_task_coordination(root, run_id, "task-001", followup_started_at="2026-05-15T00:00:00+00:00")

                def fake_backend_status(_root: Path, _run_id: str, target: str = "main", task_id: str | None = None) -> dict:
                    return {
                        "target": target,
                        "task_id": task_id,
                        "status": "running" if target == "main" else "stopped",
                        "pid": 1234 if target == "main" else None,
                        "last_reply_at": "2026-05-15T00:00:00+00:00" if target == "main" else None,
                        "resolved_model": "gpt-5.5" if target == "main" else None,
                        "runtime_context_window": 258400 if target == "main" else None,
                        "runtime_context_usage": {"input_tokens": 226853} if target == "main" else {},
                        "latest_usage": {"input_tokens": 735000} if target == "main" else {},
                        "latest_prompt_metrics": {"total": {"chars": 1234, "bytes": 1234}} if target == "main" else {},
                        "context_pressure": {"level": "watch", "percent": 70.0} if target == "main" else {},
                    }

                with mock.patch("aha_cli.web.status.backend_status", side_effect=fake_backend_status):
                    snapshot = web_status_snapshot(root, run_id)

        self.assertEqual(snapshot["tasks"][0]["coordination"]["followup_started_at"], "2026-05-15T00:00:00+00:00")
        agents = {agent["id"]: agent for agent in snapshot["tasks"][0]["agents"]}
        self.assertEqual(snapshot["tasks"][0]["activity_status"], "idle")
        self.assertEqual(agents["main"]["backend_process_status"], "running")
        self.assertEqual(agents["main"]["backend_process_pid"], 1234)
        self.assertEqual(agents["main"]["backend_resolved_model"], "gpt-5.5")
        self.assertEqual(agents["main"]["backend_runtime_context_window"], 258400)
        self.assertEqual(agents["main"]["backend_runtime_context_usage"]["input_tokens"], 226853)
        self.assertEqual(agents["main"]["backend_context_pressure"]["level"], "watch")
        self.assertEqual(agents["main"]["backend_latest_usage"]["input_tokens"], 735000)
        self.assertEqual(agents["main"]["backend_latest_prompt_metrics"]["total"]["chars"], 1234)
        self.assertEqual(agents["sub-001"]["backend_process_status"], "stopped")
        self.assertIsNone(agents["sub-001"]["backend_process_pid"])

    def test_web_status_snapshot_does_not_auto_compact_stopped_agent_above_task_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Auto context compact", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_context_management_config(
                    root,
                    run_id,
                    "task-001",
                    auto_compact_enabled=True,
                    auto_compact_threshold_percent=75,
                )
                session = ensure_session(root, run_id, "task-001", "main", "codex", model="gpt-5.5")
                session["backend_session_id"] = "codex-session-high-context"
                save_session(root, session)

                def fake_backend_status(_root: Path, _run_id: str, target: str = "main", task_id: str | None = None) -> dict:
                    return {"target": target, "task_id": task_id, "status": "stopped", "pid": None, "context_pressure": {"level": "watch", "percent": 80.0}}

                with mock.patch("aha_cli.web.status.backend_status", side_effect=fake_backend_status):
                    snapshot = web_status_snapshot(root, run_id)
                    conversation = conversation_events_page(root, run_id, "task-001", "main", categories={"chat"})

        agent = snapshot["tasks"][0]["agents"][0]
        self.assertEqual(agent["backend_process_status"], "stopped")
        self.assertEqual(agent["backend_context_pressure"]["level"], "watch")
        self.assertEqual(agent["backend_context_pressure"]["percent"], 80.0)
        messages = [event["data"]["message"] for event in conversation["events"] if event["type"] == "message"]
        self.assertFalse(any("AHA 已自动整理 `main` 的 agent context" in message for message in messages))

    def test_web_status_snapshot_lite_skips_nonselected_idle_backend_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Lite status", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_agent_status(root, run_id, "task-001", sub["id"], "completed", 0)

                with mock.patch("aha_cli.web.status.backend_status") as backend_status_mock:
                    selected_snapshot = web_status_snapshot(root, run_id, lite=True, selected_task_id="task-001")
                    snapshot = web_status_snapshot(root, run_id, lite=True, selected_task_id="task-other")
                    snapshot_without_selection = web_status_snapshot(root, run_id, lite=True)

        backend_status_mock.assert_not_called()
        selected_task = selected_snapshot["tasks"][0]
        self.assertEqual(selected_task["agent_count"], 2)
        self.assertEqual([agent["id"] for agent in selected_task["agents"]], ["main", sub["id"]])
        self.assertNotIn("backend_process_status", selected_task["agents"][0])
        task = snapshot["tasks"][0]
        self.assertEqual(task["agent_count"], 2)
        self.assertEqual(task["agents"], [])
        self.assertEqual(snapshot_without_selection["tasks"][0]["agent_count"], 2)
        self.assertEqual(snapshot_without_selection["tasks"][0]["agents"], [])

    def test_lite_status_projection_only_ensures_selected_task_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Lite projection", "--agents", "2")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                ensured: list[tuple[str | None, str]] = []

                def fake_ensure_session(
                    _root: Path,
                    _run_id: str,
                    task_id: str | None,
                    agent_id: str,
                    _backend: str,
                    **_kwargs: object,
                ) -> dict:
                    ensured.append((task_id, agent_id))
                    return {
                        "id": f"{task_id}-{agent_id}",
                        "backend_session_id": None,
                        "scope": "task",
                        "status": "active",
                        "updated_at": "2026-05-15T00:00:00+00:00",
                    }

                snapshot = raw_status_snapshot_projection(
                    root,
                    run_id,
                    lite=True,
                    selected_task_id="task-001",
                    ensure_session_func=fake_ensure_session,
                    event_stream_position_func=lambda _root, _run_id: 99,
                )

        tasks = {task["id"]: task for task in snapshot["tasks"]}
        self.assertEqual(snapshot["snapshot_event_id"], 99)
        self.assertEqual(ensured, [("task-001", "main")])
        self.assertEqual(tasks["task-001"]["agent_count"], 1)
        self.assertEqual(tasks["task-001"]["agents"][0]["session_id"], "task-001-main")
        self.assertEqual(tasks["task-002"]["agent_count"], 1)
        self.assertEqual(tasks["task-002"]["agents"], [])

    def test_web_tasks_snapshot_skips_backend_status_and_outcome_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Light task status", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")

                with (
                    mock.patch("aha_cli.web.status.backend_status") as backend_status_mock,
                    mock.patch("aha_cli.web.status.iter_jsonl_reverse") as reverse_events_mock,
                ):
                    snapshot = web_tasks_snapshot(root, run_id, lite=True, selected_task_id="task-001")

        backend_status_mock.assert_not_called()
        reverse_events_mock.assert_not_called()
        task = snapshot["tasks"][0]
        self.assertEqual(task["current_status"], "running")
        self.assertEqual(task["activity_status"], "running")
        self.assertEqual(task["display_status"], "running")
        self.assertNotIn("backend_process_status", task["agents"][0])

    def test_web_status_snapshot_does_not_recover_stale_running_agent_without_explicit_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "No implicit repair", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")

                with mock.patch("aha_cli.web.status.backend_status", return_value={"status": "stopped", "pid": None}):
                    snapshot = web_status_snapshot(root, run_id)
                persisted = task_snapshot(root, run_id, "task-001")["task"]
                event_log = event_path(root, run_id).read_text(encoding="utf-8")

        self.assertEqual(snapshot["tasks"][0]["agents"][0]["backend_process_status"], "stopped")
        self.assertEqual(persisted["status"], "running")
        self.assertEqual(persisted["agents"][0]["status"], "running")
        self.assertNotIn("agent_status_recovered", event_log)

    def test_web_agents_runtime_snapshot_returns_all_task_agents_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Batch runtime", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub")
                host = add_agent(root, run_id, "task-001", backend="claude", role="host")

                def fake_backend_status(_root: Path, _run_id: str, target: str = "main", task_id: str | None = None) -> dict:
                    return {
                        "target": target,
                        "task_id": task_id,
                        "status": "busy" if target == "main" else "running",
                        "pid": 1000 + len(target),
                        "resolved_model": "gpt-5.5" if target == "main" else "claude-sonnet",
                        "runtime_context_window": 200000,
                        "runtime_context_usage": {"input_tokens": len(target)},
                        "context_pressure": {"level": "ok"},
                        "latest_usage": {"input_tokens": 42},
                        "latest_prompt_metrics": {"total": {"chars": 99}},
                    }

                with (
                    mock.patch("aha_cli.web.status.backend_status", side_effect=fake_backend_status),
                    mock.patch("aha_cli.web.status.status_snapshot") as status_snapshot_mock,
                ):
                    runtime = web_agents_runtime_snapshot(root, run_id, "task-001")

        agents = {agent["id"]: agent for agent in runtime["agents"]}
        status_snapshot_mock.assert_not_called()
        self.assertEqual(runtime["agent_count"], 3)
        self.assertEqual(runtime["activity_status"], "busy")
        self.assertEqual(set(agents), {"main", sub["id"], host["id"]})
        self.assertEqual(agents["main"]["resolved_model"], "gpt-5.5")
        self.assertEqual(agents[sub["id"]]["status"], "running")
        self.assertEqual(agents[host["id"]]["context_pressure"]["level"], "ok")

    def test_explicit_recovery_marks_stale_running_agent_after_backend_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Interrupted service restart", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")

                with mock.patch("aha_cli.web.status.backend_status", return_value={"status": "stopped", "pid": None}):
                    recovery = recover_stale_running_agents(root, run_id)
                    snapshot = web_status_snapshot(root, run_id)
                persisted = task_snapshot(root, run_id, "task-001")["task"]
                event_log = event_path(root, run_id).read_text(encoding="utf-8")

        self.assertEqual(recovery["recovered_count"], 1)
        task = snapshot["tasks"][0]
        agent = task["agents"][0]
        self.assertEqual(task["status"], "awaiting_user")
        self.assertEqual(task["current_status"], "awaiting_user")
        self.assertEqual(task["display_status"], "awaiting_user")
        self.assertEqual(task["activity_status"], "idle")
        self.assertEqual(agent["status"], "interrupted")
        self.assertEqual(agent["backend_process_status"], "stopped")

        self.assertEqual(persisted["status"], "awaiting_user")
        self.assertEqual(persisted["agents"][0]["status"], "interrupted")
        self.assertIn("agent_status_recovered", event_log)

    def test_explicit_recovery_rechecks_stopped_cache_before_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Fresh backend race", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")

                with mock.patch("aha_cli.web.status.backend_status", return_value={"status": "stopped", "pid": None}):
                    cached_backend_status(root, run_id, "main", "task-001")
                with mock.patch("aha_cli.web.status.backend_status", return_value={"status": "running", "pid": 4321}):
                    recovery = recover_stale_running_agents(root, run_id)
                    snapshot = web_status_snapshot(root, run_id)
                persisted = task_snapshot(root, run_id, "task-001")["task"]
                event_log = event_path(root, run_id).read_text(encoding="utf-8")

        self.assertEqual(recovery["recovered_count"], 0)
        task = snapshot["tasks"][0]
        agent = task["agents"][0]
        self.assertEqual(task["status"], "running")
        self.assertEqual(agent["status"], "running")
        self.assertEqual(agent["backend_process_status"], "running")
        self.assertEqual(agent["backend_process_pid"], 4321)
        self.assertEqual(persisted["status"], "running")
        self.assertEqual(persisted["agents"][0]["status"], "running")
        self.assertNotIn("agent_status_recovered", event_log)

    def test_stale_recovery_does_not_reopen_terminal_task_from_old_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final race", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")
                stale_task = status_snapshot(root, run_id)["tasks"][0]
                stale_agent = stale_task["agents"][0]

                set_task_status(root, run_id, "task-001", "completed", 0)
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)
                recovered = recover_stale_running_agent(
                    root,
                    run_id,
                    stale_task,
                    stale_agent,
                    {"status": "stopped", "pid": None},
                )
                persisted = task_snapshot(root, run_id, "task-001")["task"]
                event_log = event_path(root, run_id).read_text(encoding="utf-8")

        self.assertFalse(recovered)
        self.assertEqual(persisted["status"], "completed")
        self.assertEqual(persisted["agents"][0]["status"], "completed")
        self.assertNotIn("agent_status_recovered", event_log)

    def test_recovered_agent_followup_includes_recovery_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recover context", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")

                with mock.patch("aha_cli.web.status.backend_status", return_value={"status": "stopped", "pid": None}):
                    recover_stale_running_agents(root, run_id)
                recovered = task_snapshot(root, run_id, "task-001")["task"]
                self.assertIn("工作异常中断", recovered["agents"][0]["recovery_context"])

                with (
                    mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "stopped", "pid": None}),
                    mock.patch("aha_cli.web.task_messaging.start_backend", return_value={"status": "running"}) as start_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "sender": "browser",
                            "message": "继续",
                        },
                    )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                sent_text = messages[-1]["message"]
                consumed = task_snapshot(root, run_id, "task-001")["task"]
                event_log = event_path(root, run_id).read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        start_backend.assert_called_once()
        self.assertEqual(sent_text, "继续")
        self.assertIn("工作异常中断", messages[-1]["recovery_context"])
        self.assertNotIn("用户当前发送的新消息", sent_text)
        self.assertEqual(consumed["agents"][0]["recovery_context"], "")
        self.assertIn("agent_recovery_context_consumed", event_log)

    def test_recovered_running_sub_agent_queues_notice_to_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sub recovery while main runs", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")
                set_agent_status(root, run_id, "task-001", sub["id"], "running")
                stale_task = task_snapshot(root, run_id, "task-001")["task"]
                stale_sub = next(agent for agent in stale_task["agents"] if agent["id"] == sub["id"])

                recovered = recover_stale_running_agent(
                    root,
                    run_id,
                    stale_task,
                    stale_sub,
                    {"status": "stopped", "pid": None},
                )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                detail = task_snapshot(root, run_id, "task-001")["task"]
                event_log = event_path(root, run_id).read_text(encoding="utf-8")
                main_page = conversation_events_page(root, run_id, "task-001", "main", limit=20, categories={"chat"})

        self.assertTrue(recovered)
        self.assertEqual(detail["status"], "running")
        self.assertEqual(next(agent for agent in detail["agents"] if agent["id"] == "main")["status"], "running")
        self.assertEqual(next(agent for agent in detail["agents"] if agent["id"] == sub["id"])["status"], "interrupted")
        self.assertEqual(messages[-1]["sender"], "aha")
        self.assertEqual(messages[-1]["coordination"], "agent_recovery_notice")
        self.assertIn(sub["id"], messages[-1]["message"])
        self.assertIn("不要假设它已经完成", messages[-1]["message"])
        self.assertIn("task_recovery_context_recorded", event_log)
        self.assertTrue(any(event.get("data", {}).get("message") == f"{sub['id']} backend 已停止。" for event in main_page["events"]))
        self.assertFalse(any(message.get("coordination") == "subagents_complete" for message in messages))

    def test_recovered_waited_sub_agent_requests_main_round_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sub recovery wakes main", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "waiting", waiting_reason="subagents")
                set_agent_status(root, run_id, "task-001", sub["id"], "running")
                stale_task = task_snapshot(root, run_id, "task-001")["task"]
                stale_sub = next(agent for agent in stale_task["agents"] if agent["id"] == sub["id"])

                with (
                    mock.patch("aha_cli.services.orchestrator.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_main,
                ):
                    recovered = recover_stale_running_agent(
                        root,
                        run_id,
                        stale_task,
                        stale_sub,
                        {"status": "stopped", "pid": None},
                    )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                main_page = conversation_events_page(root, run_id, "task-001", "main", limit=20, categories={"chat"})

        self.assertTrue(recovered)
        self.assertTrue(any(event.get("data", {}).get("message") == f"{sub['id']} backend 已停止。" for event in main_page["events"]))
        self.assertTrue(any(message.get("coordination") == "subagents_complete" for message in messages))
        start_main.assert_called_once()

    def test_recovered_idle_sub_agent_adds_context_to_next_main_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sub recovery before follow-up", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "waiting")
                set_agent_status(root, run_id, "task-001", sub["id"], "running")
                stale_task = task_snapshot(root, run_id, "task-001")["task"]
                stale_sub = next(agent for agent in stale_task["agents"] if agent["id"] == sub["id"])

                recover_stale_running_agent(
                    root,
                    run_id,
                    stale_task,
                    stale_sub,
                    {"status": "stopped", "pid": None},
                )
                context_task = task_snapshot(root, run_id, "task-001")["task"]
                main_agent = next(agent for agent in context_task["agents"] if agent["id"] == "main")
                self.assertIn(sub["id"], main_agent["recovery_context"])

                with (
                    mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "stopped", "pid": None}),
                    mock.patch("aha_cli.web.task_messaging.start_backend", return_value={"status": "running"}),
                ):
                    handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "sender": "browser",
                            "message": "继续",
                        },
                    )
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                consumed = task_snapshot(root, run_id, "task-001")["task"]

        self.assertEqual(messages[-1]["message"], "继续")
        self.assertIn(sub["id"], messages[-1]["recovery_context"])
        self.assertIn("不要假设它已经完成", messages[-1]["recovery_context"])
        self.assertNotIn("用户当前发送的新消息", messages[-1]["message"])
        self.assertEqual(next(agent for agent in consumed["agents"] if agent["id"] == "main")["recovery_context"], "")

    def test_web_status_snapshot_keeps_outcome_during_active_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Follow-up state", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "completed", 0)
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")

                with mock.patch("aha_cli.web.status.backend_status", return_value={"status": "busy", "pid": 1234}):
                    snapshot = web_status_snapshot(root, run_id)

        task = snapshot["tasks"][0]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["current_status"], "completed")
        self.assertEqual(task["outcome_status"], "completed")
        self.assertEqual(task["display_status"], "completed")
        self.assertEqual(task["activity_status"], "busy")

    def test_web_send_autostarts_stopped_task_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Web follow-up", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)
                append_message(root, run_id, "main", "old already-seen message", sender="browser", task_id="task-001", role="main")

                offset_file = chat_offset_path(run_dir(root, run_id), "main", "task-001")
                self.assertFalse(offset_file.exists())

                with (
                    mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.task_messaging.start_backend", return_value={"status": "running", "started": True}) as start_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "sender": "browser",
                            "message": "new follow-up",
                        },
                    )

                self.assertTrue(result["ok"])
                self.assertEqual(result["backend"]["status"], "running")
                start_backend.assert_called_once()
                self.assertFalse(start_backend.call_args.kwargs["from_start"])
                self.assertEqual(start_backend.call_args.kwargs["task_id"], "task-001")
                offset = json.loads(offset_file.read_text(encoding="utf-8"))["offset"]
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), offset)
                self.assertEqual([item["message"] for item in messages], ["new follow-up"])

    def test_web_send_autostarts_claude_task_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude web follow-up", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_agent_runtime(root, run_id, "task-001", "main", backend="claude")
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)

                with (
                    mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.task_messaging.start_backend", return_value={"status": "running", "started": True}) as start_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "sender": "browser",
                            "message": "new follow-up",
                        },
                    )

        self.assertTrue(result["ok"])
        start_backend.assert_called_once()
        self.assertEqual(start_backend.call_args.kwargs["backend"], "claude")

    def test_web_send_blocks_completed_task_until_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Locked task", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                complete_task(root, run_id, "task-001", 0)

                payload = {
                    "target": "main",
                    "role": "main",
                    "task_id": "task-001",
                    "from_agent": "browser",
                    "to_agent": "main",
                    "sender": "browser",
                    "message": "should be blocked",
                }
                with self.assertRaisesRegex(ValueError, "use /aha reopen"):
                    handle_send_payload(root, run_id, payload)

                reopened = handle_send_payload(root, run_id, {**payload, "message": "/aha reopen"})
                self.assertTrue(reopened["ok"])
                detail = task_snapshot(root, run_id, "task-001")["task"]
                self.assertEqual(detail["status"], "awaiting_user")
                self.assertEqual(detail["coordination"]["final_summary_requested_at"], "")

                with (
                    mock.patch("aha_cli.web.task_messaging.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.web.task_messaging.start_backend", return_value={"status": "running"}) as start_backend,
                ):
                    sent = handle_send_payload(root, run_id, {**payload, "message": "follow-up"})

                self.assertTrue(sent["ok"])
                start_backend.assert_called_once()

    def test_aha_interrupt_stops_backend_and_marks_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Interrupt turn", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "running")
                set_agent_status(root, run_id, "task-001", "main", "running")
                append_message(root, run_id, "main", "in-flight", sender="browser", task_id="task-001", role="main")
                offset_file = chat_offset_path(run_dir(root, run_id), "main", "task-001")
                save_chat_offset(offset_file, 0)

                with (
                    mock.patch("aha_cli.web.task_command_actions.backend_status", return_value={"status": "busy", "pid": 1234}),
                    mock.patch("aha_cli.web.task_command_actions.stop_backend", return_value={"status": "stopped", "pid": None, "target": "main"}) as stop_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "sender": "browser",
                            "message": "/aha interrupt",
                        },
                    )

                self.assertTrue(result["ok"])
                self.assertTrue(result["interrupt"]["interrupted"])
                stop_backend.assert_called_once()
                detail = task_snapshot(root, run_id, "task-001")["task"]
                self.assertEqual(detail["status"], "awaiting_user")
                self.assertEqual(detail["agents"][0]["status"], "interrupted")
                self.assertIsNotNone(detail["agents"][0]["finished_at"])
                self.assertEqual(json.loads(offset_file.read_text(encoding="utf-8"))["offset"], inbox_path(root, run_id, "main").stat().st_size)

    def test_aha_interrupt_stops_idle_running_backend_listener(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Idle listener", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                set_task_status(root, run_id, "task-001", "awaiting_user")
                set_agent_status(root, run_id, "task-001", "main", "completed", 0)

                with (
                    mock.patch("aha_cli.web.task_command_actions.backend_status", return_value={"status": "running", "pid": 1234}),
                    mock.patch("aha_cli.web.task_command_actions.stop_backend", return_value={"status": "stopped", "pid": None, "target": "main"}) as stop_backend,
                ):
                    result = handle_send_payload(
                        root,
                        run_id,
                        {
                            "target": "main",
                            "role": "main",
                            "task_id": "task-001",
                            "from_agent": "browser",
                            "to_agent": "main",
                            "sender": "browser",
                            "message": "/aha interrupt",
                        },
                    )

                self.assertTrue(result["ok"])
                self.assertTrue(result["interrupt"]["interrupted"])
                stop_backend.assert_called_once()
                detail = task_snapshot(root, run_id, "task-001")["task"]
                self.assertEqual(detail["status"], "awaiting_user")
                self.assertEqual(detail["agents"][0]["status"], "interrupted")
