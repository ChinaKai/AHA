from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest import mock

from aha_cli.cli import append_message, main, task_snapshot
from aha_cli.services.chat import chat_offset_path, load_chat_offset
from aha_cli.services.orchestrator import (
    action_response_text,
    execute_actions,
    extract_action_payload,
    monitor_task_coordination,
    record_sub_agent_report,
)
from aha_cli.store.filesystem import (
    add_agent,
    complete_task,
    ensure_session,
    event_path,
    inbox_path,
    iter_jsonl_from,
    list_sessions,
    list_task_rounds,
    mark_task_coordination,
    require_plan,
    reopen_task,
    run_dir,
    save_plan,
    save_session,
    set_agent_status,
    set_task_status,
    status_snapshot,
    task_final_snapshot,
    update_agent_runtime,
    write_task_result,
)
from aha_cli.web.server import finalization_prompt


class OrchestratorTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_record_task_update_uses_current_lifecycle_round_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Journal lifecycle", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                write_task_result(root, run_id, "task-001", "第一轮 Final")
                complete_task(root, run_id, "task-001", 0)
                reopen_task(root, run_id, "task-001")

                reply = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "record_task_update",
                                "summary": "第二轮修复 Round/Journal 关联",
                                "changed_files": ["src/aha_cli/store/filesystem.py"],
                            }
                        ],
                        "response": "已记录",
                    },
                    ensure_ascii=False,
                )

                executed = execute_actions(root, run_id, "task-001", reply)

                self.assertEqual(executed, [{"type": "record_task_update", "round_id": "round-002"}])
                journal = list_task_rounds(root, run_id, "task-001")
                self.assertEqual(journal[0]["round_id"], "round-002")
                self.assertEqual(journal[0]["round_sequence"], 2)
                self.assertEqual(journal[0]["sequence"], 2)
                self.assertEqual(journal[0]["journal_id"], "journal-001")
                self.assertEqual(journal[0]["journal_sequence"], 1)
                journal_text = (run_dir(root, run_id) / "results/task-001.md").read_text(encoding="utf-8")
                self.assertIn("轮次：`round-002`", journal_text)
                self.assertNotIn("轮次：`round-001`", journal_text)
                prompt = finalization_prompt("task-001", "Journal lifecycle", journal)
                self.assertIn("round-002", prompt)
                self.assertNotIn("round-001", prompt)
                final = task_final_snapshot(root, run_id, "task-001")
                self.assertIn("round-001", final["result"])
                self.assertEqual(final["finals"][0]["round_id"], "round-001")
                self.assertEqual((run_dir(root, run_id) / final["finals"][0]["final_path"]).read_text(encoding="utf-8").strip(), "第一轮 Final")

    def test_codex_chat_executes_spawn_sub_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Spawn sub", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "delegate this", sender="browser", task_id="task-001", role="main")
                reply = '{"complexity":"medium","actions":[{"type":"spawn_sub","title":"Inspect one slice","backend":"stub","reason":"parallel research"}],"response":"delegating"}'

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, reply, None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("delegating", output)

                code, agents = self.run_cli("agent", "list", run_id, "task-001")
                self.assertEqual(code, 0)
                self.assertIn("sub-001 role=sub backend=stub", agents)
                self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "running")
                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)
                self.assertIn("等待子 agent 完成", browser_messages[-1]["message"])

    def test_execute_actions_reuses_interrupted_sub_agent_at_max(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recover sub slot", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_two = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_three = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                append_message(root, run_id, sub["id"], "old assignment", sender="main", task_id="task-001", role="sub")
                set_agent_status(root, run_id, "task-001", sub["id"], "interrupted")
                set_agent_status(root, run_id, "task-001", sub_two["id"], "completed", 0)
                set_agent_status(root, run_id, "task-001", sub_three["id"], "completed", 0)
                update_agent_runtime(
                    root,
                    run_id,
                    "task-001",
                    sub["id"],
                    recovery_context="old failure",
                    session_id="old-session",
                    backend_session_id="old-backend-session",
                    last_usage={"input_tokens": 1},
                )
                session = ensure_session(root, run_id, "task-001", sub["id"], "codex", workspace_path=str(root))
                session["backend_session_id"] = "old-thread"
                session["status"] = "active"
                save_session(root, session)
                reply = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "spawn_sub",
                                "title": "Recover and inspect issue 02",
                                "backend": "codex",
                                "reason": "previous sub interrupted",
                            }
                        ],
                        "response": "请求 AHA 恢复 sub",
                    }
                )

                with mock.patch("aha_cli.services.orchestrator.start_backend") as start_backend_mock:
                    executed = execute_actions(root, run_id, "task-001", reply)

                detail = task_snapshot(root, run_id, "task-001")["task"]
                agent = next(item for item in detail["agents"] if item["id"] == sub["id"])
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, sub["id"]), 0)
                offset = load_chat_offset(
                    inbox_path(root, run_id, sub["id"]),
                    chat_offset_path(run_dir(root, run_id), sub["id"], "task-001"),
                    from_start=False,
                )
                new_messages, _ = iter_jsonl_from(inbox_path(root, run_id, sub["id"]), offset)
                rows, _ = iter_jsonl_from(event_path(root, run_id), 0)
                sessions = list_sessions(root, run_id, "task-001")

        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0]["type"], "spawn_sub")
        self.assertTrue(executed[0]["reused"])
        self.assertEqual(executed[0]["agent"]["id"], sub["id"])
        self.assertEqual(agent["status"], "pending")
        self.assertEqual(agent["assignment"], "Recover and inspect issue 02")
        self.assertEqual(agent["assignment_id"], f"{sub['id']}:gen-001")
        self.assertEqual(agent["scope_id"], f"{sub['id']}:gen-001")
        self.assertEqual(agent["generation"], 1)
        self.assertEqual(agent["recovery_context"], "")
        self.assertIsNone(agent["session_id"])
        self.assertIsNone(agent["backend_session_id"])
        self.assertIsNone(agent["last_usage"])
        sub_session = next(item for item in sessions if item["agent_id"] == sub["id"])
        self.assertIsNone(sub_session["backend_session_id"])
        self.assertEqual(sub_session["status"], "reset")
        self.assertEqual(sub_session["history_backend_sessions"][-1]["backend_session_id"], "old-thread")
        self.assertEqual(sub_session["history_backend_sessions"][-1]["assignment_id"], f"{sub['id']}:gen-001")
        self.assertEqual(sub_session["history_backend_sessions"][-1]["scope_id"], f"{sub['id']}:gen-001")
        self.assertEqual(len([item for item in detail["agents"] if item.get("role") == "sub"]), 3)
        self.assertEqual(messages[-1]["message"], "Recover and inspect issue 02")
        self.assertEqual([item["message"] for item in new_messages], ["Recover and inspect issue 02"])
        start_backend_mock.assert_called_once()
        self.assertFalse(any(row["type"] == "action_skipped" and row["data"].get("type") == "spawn_sub" for row in rows))
        self.assertTrue(any(row["type"] == "sub_agent_reused" for row in rows))
        self.assertTrue(
            any(
                row["type"] == "backend_session_reset"
                and row["data"].get("agent_id") == sub["id"]
                and row["data"].get("old_backend_session_id") == "old-thread"
                and row["data"].get("archived") is True
                for row in rows
            )
        )

    def test_execute_actions_reuses_distinct_sub_agents_in_spawn_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recover several sub slots", "--agents", "3")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_two = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_three = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_agent_status(root, run_id, "task-001", sub["id"], "completed", 0)
                set_agent_status(root, run_id, "task-001", sub_two["id"], "interrupted")
                set_agent_status(root, run_id, "task-001", sub_three["id"], "stopped")
                reply = json.dumps(
                    {
                        "actions": [
                            {"type": "spawn_sub", "title": "Inspect issue 02", "backend": "codex"},
                            {"type": "spawn_sub", "title": "Inspect issue 04", "backend": "codex"},
                            {"type": "spawn_sub", "title": "Inspect issue 03", "backend": "codex"},
                        ],
                        "response": "请求 AHA 分配三个 sub",
                    }
                )

                with mock.patch("aha_cli.services.orchestrator.start_backend") as start_backend_mock:
                    executed = execute_actions(root, run_id, "task-001", reply)

                detail = task_snapshot(root, run_id, "task-001")["task"]
                rows, _ = iter_jsonl_from(event_path(root, run_id), 0)
                sub_messages, _ = iter_jsonl_from(inbox_path(root, run_id, sub["id"]), 0)
                sub_two_messages, _ = iter_jsonl_from(inbox_path(root, run_id, sub_two["id"]), 0)
                sub_three_messages, _ = iter_jsonl_from(inbox_path(root, run_id, sub_three["id"]), 0)

        self.assertEqual([item["agent"]["id"] for item in executed], [sub_two["id"], sub["id"], sub_three["id"]])
        self.assertEqual([item["agent"]["assignment"] for item in executed], ["Inspect issue 02", "Inspect issue 04", "Inspect issue 03"])
        self.assertEqual(next(item for item in detail["agents"] if item["id"] == sub_two["id"])["assignment"], "Inspect issue 02")
        self.assertEqual(next(item for item in detail["agents"] if item["id"] == sub["id"])["assignment"], "Inspect issue 04")
        self.assertEqual(next(item for item in detail["agents"] if item["id"] == sub_three["id"])["assignment"], "Inspect issue 03")
        self.assertEqual(sub_two_messages[-1]["message"], "Inspect issue 02")
        self.assertEqual(sub_messages[-1]["message"], "Inspect issue 04")
        self.assertEqual(sub_three_messages[-1]["message"], "Inspect issue 03")
        self.assertEqual(start_backend_mock.call_count, 3)
        self.assertEqual(len([row for row in rows if row["type"] == "sub_agent_reused"]), 3)
        self.assertFalse(any(row["type"] == "action_skipped" and row["data"].get("type") == "spawn_sub" for row in rows))

    def test_execute_actions_spawn_sub_can_target_specific_existing_sub_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Retarget one sub slot", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_two = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_three = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_agent_status(root, run_id, "task-001", sub["id"], "completed", 0)
                set_agent_status(root, run_id, "task-001", sub_two["id"], "completed", 0)
                set_agent_status(root, run_id, "task-001", sub_three["id"], "completed", 0)
                reply = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "spawn_sub",
                                "agent_id": sub_three["id"],
                                "title": "Reassign issue 04",
                                "backend": "codex",
                            }
                        ],
                        "response": "指定 sub-003 重新分析",
                    }
                )

                with mock.patch("aha_cli.services.orchestrator.start_backend") as start_backend_mock:
                    executed = execute_actions(root, run_id, "task-001", reply)

                detail = task_snapshot(root, run_id, "task-001")["task"]
                rows, _ = iter_jsonl_from(event_path(root, run_id), 0)
                sub_three_messages, _ = iter_jsonl_from(inbox_path(root, run_id, sub_three["id"]), 0)

        agent = next(item for item in detail["agents"] if item["id"] == sub_three["id"])
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0]["agent"]["id"], sub_three["id"])
        self.assertEqual(executed[0]["requested_agent_id"], sub_three["id"])
        self.assertEqual(agent["status"], "pending")
        self.assertEqual(agent["assignment"], "Reassign issue 04")
        self.assertEqual(agent["assignment_id"], f"{sub_three['id']}:gen-001")
        self.assertEqual(agent["scope_id"], f"{sub_three['id']}:gen-001")
        self.assertEqual(sub_three_messages[-1]["message"], "Reassign issue 04")
        start_backend_mock.assert_called_once()
        self.assertFalse(any(row["type"] == "action_skipped" and row["data"].get("type") == "spawn_sub" for row in rows))
        self.assertTrue(
            any(
                row["type"] == "sub_agent_reused"
                and row["data"].get("agent_id") == sub_three["id"]
                and row["data"].get("reason") == "spawn_sub assigned to requested sub-agent"
                for row in rows
            )
        )

    def test_execute_actions_preserves_recovery_context_for_same_scope_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Resume scoped sub slot", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                update_agent_runtime(
                    root,
                    run_id,
                    "task-001",
                    sub["id"],
                    assignment="Old scoped work",
                    assignment_id=f"{sub['id']}:gen-001",
                    scope_id="chat-prompt-context",
                    scope_explicit=True,
                    generation=1,
                    recovery_context="resume from failing focused test",
                    recovery_context_reason="context_overflow",
                    recovery_context_at="2026-05-22T00:00:00+00:00",
                    recovery_context_consumed_at="",
                    session_id="sticky-session",
                    backend_session_id="backend-session",
                )
                session = ensure_session(root, run_id, "task-001", sub["id"], "codex", workspace_path=str(root))
                session["backend_session_id"] = "same-scope-thread"
                session["status"] = "active"
                save_session(root, session)
                set_agent_status(root, run_id, "task-001", sub["id"], "interrupted")
                reply = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "spawn_sub",
                                "agent_id": sub["id"],
                                "scope_id": "chat-prompt-context",
                                "title": "Continue scoped work",
                                "backend": "codex",
                            }
                        ],
                        "response": "继续同一 scope",
                    }
                )

                with mock.patch("aha_cli.services.orchestrator.start_backend"):
                    executed = execute_actions(root, run_id, "task-001", reply)

                detail = task_snapshot(root, run_id, "task-001")["task"]
                rows, _ = iter_jsonl_from(event_path(root, run_id), 0)
                sessions = list_sessions(root, run_id, "task-001")

        agent = next(item for item in detail["agents"] if item["id"] == sub["id"])
        sub_session = next(item for item in sessions if item["agent_id"] == sub["id"])
        self.assertEqual(len(executed), 1)
        self.assertEqual(agent["assignment"], "Continue scoped work")
        self.assertEqual(agent["assignment_id"], f"{sub['id']}:gen-002")
        self.assertEqual(agent["scope_id"], "chat-prompt-context")
        self.assertEqual(agent["generation"], 2)
        self.assertEqual(agent["recovery_context"], "resume from failing focused test")
        self.assertEqual(agent["backend_session_id"], "backend-session")
        self.assertEqual(sub_session["backend_session_id"], "same-scope-thread")
        self.assertEqual(sub_session["status"], "active")
        self.assertFalse(any(row["type"] == "backend_session_reset" for row in rows))
        self.assertTrue(
            any(
                row["type"] == "sub_agent_reused"
                and row["data"].get("agent_id") == sub["id"]
                and row["data"].get("same_scope") is True
                and row["data"].get("scope_id") == "chat-prompt-context"
                for row in rows
            )
        )

    def test_execute_actions_reports_spawn_sub_skipped_when_active_limit_reached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "No sub slot", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                plan = require_plan(root, run_id)
                plan["tasks"][0]["max_sub_agents"] = 1
                save_plan(root, plan)
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_two = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                sub_three = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_agent_status(root, run_id, "task-001", sub["id"], "running")
                set_agent_status(root, run_id, "task-001", sub_two["id"], "completed", 0)
                set_agent_status(root, run_id, "task-001", sub_three["id"], "completed", 0)
                reply = json.dumps(
                    {
                        "actions": [{"type": "spawn_sub", "title": "Extra sub", "backend": "codex"}],
                        "response": "请求 AHA 创建 sub",
                    }
                )

                with mock.patch("aha_cli.services.orchestrator.start_backend") as start_backend_mock:
                    executed = execute_actions(root, run_id, "task-001", reply)

                rows, _ = iter_jsonl_from(event_path(root, run_id), 0)
                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)
                detail = task_snapshot(root, run_id, "task-001")["task"]

        self.assertEqual(executed, [])
        start_backend_mock.assert_not_called()
        self.assertEqual(len([item for item in detail["agents"] if item.get("role") == "sub"]), 3)
        self.assertTrue(any(row["type"] == "action_skipped" and row["data"].get("reason") == "max_sub_agents reached" for row in rows))
        self.assertEqual(browser_messages[-1]["sender"], "aha")
        self.assertIn("没有创建新的 sub-agent", browser_messages[-1]["message"])
        self.assertIn("当前活跃 sub-agent 已达到", browser_messages[-1]["message"])

    def test_codex_chat_flags_subagent_claim_without_spawn_sub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sub claim mismatch", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "delegate this", sender="browser", task_id="task-001", role="main")
                reply = "3个sub agent已并行启动。我现在继续分析。"

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, reply, None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                detail = task_snapshot(root, run_id, "task-001")

        self.assertEqual(code, 0)
        self.assertFalse(any(agent["id"].startswith("sub-") for agent in detail["task"]["agents"]))
        mismatch_events = [event for event in events if event["type"] == "claimed_sub_without_aha_agent"]
        self.assertEqual(len(mismatch_events), 1)
        self.assertEqual(mismatch_events[0]["data"]["reason"], "reply_claim_without_spawn_sub_action")

    def test_action_parser_ignores_embedded_json_examples(self) -> None:
        reply = textwrap.dedent(
            """\
            我建议使用这个格式：

            ```json
            {"actions":[{"type":"route_to_agent","agent_id":"...","message":"..."}],"response":"..."}
            ```
            """
        ).strip()

        self.assertIsNone(extract_action_payload(reply))
        self.assertEqual(action_response_text(reply), reply)

    def test_invalid_top_level_action_schema_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Bad route schema", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                reply = json.dumps(
                    {
                        "action": "route_to_agent",
                        "agent_id": "sub-001",
                        "message": "This should not route",
                        "response": "sent",
                    }
                )

                with mock.patch("aha_cli.services.orchestrator.start_backend") as start_backend_mock:
                    executed = execute_actions(root, run_id, "task-001", reply)

                self.assertEqual(executed, [])
                self.assertIn("Invalid AHA action schema", action_response_text(reply))
                start_backend_mock.assert_not_called()
                sub_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "sub-001"), 0)
                self.assertEqual(sub_messages, [])
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                self.assertTrue(any(event["type"] == "invalid_action_schema" for event in events))

    def test_execute_actions_records_task_update_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Journal task", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                reply = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "record_task_update",
                                "summary": "修复 action schema 误解析",
                                "changed_files": ["src/aha_cli/services/orchestrator.py"],
                                "verification": ["tests OK"],
                                "risks": [],
                            }
                        ],
                        "response": "已记录",
                    },
                    ensure_ascii=False,
                )

                executed = execute_actions(root, run_id, "task-001", reply)

                self.assertEqual(executed, [{"type": "record_task_update", "round_id": "round-001"}])
                rounds = list_task_rounds(root, run_id, "task-001")
                self.assertEqual(rounds[0]["summary"], "修复 action schema 误解析")
                self.assertEqual(rounds[0]["changed_files"], ["src/aha_cli/services/orchestrator.py"])
                snapshot = task_final_snapshot(root, run_id, "task-001")
                self.assertEqual(snapshot["result_meta"]["policy"], "journal")
                self.assertIn("修复 action schema 误解析", snapshot["result"])
                self.assertIn("## 任务轮次", snapshot["result"])
                self.assertIn("1. `round-001` 修复 action schema 误解析", snapshot["result"])
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                self.assertTrue(any(event["type"] == "task_round_recorded" for event in events))

    def test_codex_chat_record_task_update_does_not_wait_for_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Record round", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "完成了一个小修复", sender="browser", task_id="task-001", role="main")
                reply = json.dumps(
                    {
                        "actions": [{"type": "record_task_update", "summary": "完成小修复"}],
                        "response": "完成",
                    },
                    ensure_ascii=False,
                )

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, reply, None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                self.assertIn("完成", output)
                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)
                self.assertNotIn("等待子 agent 完成", "\n".join(item["message"] for item in browser_messages))
                self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "awaiting_user")

    def test_codex_chat_autostarts_codex_sub_agent_from_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Spawn codex sub", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "delegate this", sender="browser", task_id="task-001", role="main")
                reply = '{"complexity":"complex","actions":[{"type":"spawn_sub","title":"Inspect one slice","backend":"codex","reason":"parallel research"}],"response":"delegating"}'

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, reply, None)),
                    mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_backend,
                ):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                start_backend.assert_called_once()
                self.assertEqual(start_backend.call_args.args[:3], (root / ".aha", run_id, "sub-001"))
                self.assertTrue(start_backend.call_args.kwargs["from_start"])
                messages, _ = iter_jsonl_from(inbox_path(root, run_id, "sub-001"), 0)
                self.assertEqual(messages[-1]["message"], "Inspect one slice")
                detail = task_snapshot(root, run_id, "task-001")
                sub_agent = next(agent for agent in detail["task"]["agents"] if agent["id"] == "sub-001")
                self.assertEqual(sub_agent["assignment"], "Inspect one slice")
                self.assertTrue(detail["task"]["coordination"]["followup_started_at"])

    def test_main_routes_followup_to_responsible_sub_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Owned follow-up", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_agent_status(root, run_id, "task-001", "sub-001", "completed", 0)
                mark_task_coordination(
                    root,
                    run_id,
                    "task-001",
                    final_summary_requested_at="2026-05-15T00:00:00+00:00",
                    final_summary_completed_at="2026-05-15T00:01:00+00:00",
                )
                set_task_status(root, run_id, "task-001", "completed", 0)
                reopen_task(root, run_id, "task-001")
                append_message(root, run_id, "main", "这个范围继续调整", sender="browser", task_id="task-001", role="main")
                reply = json.dumps(
                    {
                        "complexity": "simple",
                        "actions": [
                            {
                                "type": "route_to_agent",
                                "agent_id": "sub-001",
                                "message": "请继续调整你负责的范围",
                                "reason": "sub-001 owns this scope",
                            }
                        ],
                        "response": "已转给 sub-001 处理。",
                    }
                )

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, reply, None)),
                    mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_backend,
                ):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                self.assertIn("main -> browser: 已转给 sub-001 处理。", output)
                self.assertNotIn("route_to_agent", output)
                sub_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "sub-001"), 0)
                self.assertEqual(sub_messages[-1]["message"], "请继续调整你负责的范围")
                self.assertEqual(sub_messages[-1]["coordination"], "routed_by_main")
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "running")
                self.assertIsNone(detail["task"]["exit_code"])
                self.assertEqual(detail["task"]["coordination"]["final_summary_requested_at"], "")
                self.assertEqual(detail["task"]["coordination"]["final_summary_completed_at"], "")
                self.assertTrue(detail["task"]["coordination"]["followup_started_at"])
                start_backend.assert_called_once()

                report = record_sub_agent_report(root, run_id, "task-001", "sub-001", "sub follow-up done")
                self.assertTrue(report["round_summary_requested"])
                self.assertFalse(report["final_requested"])

    def test_sub_agent_skips_completed_task_even_with_old_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Terminal follow-up", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_agent_status(root, run_id, "task-001", "sub-001", "pending")
                set_task_status(root, run_id, "task-001", "completed", 0)
                mark_task_coordination(root, run_id, "task-001", followup_started_at="2026-05-15T00:00:00+00:00")
                append_message(root, run_id, "sub-001", "继续处理你负责的范围", sender="main", task_id="task-001", role="sub")

                with mock.patch("aha_cli.services.chat.run_codex_exec") as run_agent:
                    code, output = self.run_cli(
                        "codex-chat",
                        run_id,
                        "sub-001",
                        "--task-id",
                        "task-001",
                        "--sender",
                        "sub-001",
                        "--from-start",
                        "--once",
                )

                self.assertEqual(code, 0)
                self.assertNotIn("sub-001 -> main", output)
                run_agent.assert_not_called()
                detail = task_snapshot(root, run_id, "task-001")
                sub_agent = next(agent for agent in detail["task"]["agents"] if agent["id"] == "sub-001")
                self.assertEqual(detail["task"]["status"], "completed")
                self.assertEqual(sub_agent["status"], "pending")
                self.assertNotIn("final_summary_requested_at", detail["task"].get("coordination") or {})
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                self.assertTrue(any(event["type"] == "agent_message_skipped" for event in events))

    def test_sub_agent_reports_wait_then_request_main_round_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sub reports", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_task_status(root, run_id, "task-001", "running")

                first = record_sub_agent_report(root, run_id, "task-001", "sub-001", "sub-001 done")
                self.assertTrue(first["handled"])
                self.assertFalse(first.get("round_summary_requested"))
                detail = task_snapshot(root, run_id, "task-001")
                statuses = {agent["id"]: agent["status"] for agent in detail["task"]["agents"]}
                self.assertEqual(statuses["sub-001"], "completed")
                self.assertEqual(statuses["sub-002"], "pending")

                second = record_sub_agent_report(root, run_id, "task-001", "sub-002", "sub-002 done")
                self.assertTrue(second["round_summary_requested"])
                self.assertFalse(second["final_requested"])
                main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                self.assertEqual(main_messages[-1]["sender"], "aha")
                self.assertEqual(main_messages[-1]["coordination"], "subagents_complete")
                self.assertNotIn("result_policy", main_messages[-1])
                self.assertEqual(main_messages[-1]["reply_target"], "browser")
                self.assertIn("round summary", main_messages[-1]["message"])

    def test_round_summary_request_offsets_main_and_restarts_stopped_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Round summary restart", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                update_agent_runtime(
                    root,
                    run_id,
                    "task-001",
                    "main",
                    model="gpt-test",
                    sandbox="danger-full-access",
                    approval="never",
                )
                set_agent_status(root, run_id, "task-001", "main", "waiting", waiting_reason="subagents")
                set_task_status(root, run_id, "task-001", "running")
                append_message(root, run_id, "main", "old message", sender="browser", task_id="task-001", role="main")
                main_inbox = inbox_path(root, run_id, "main")
                baseline_offset = main_inbox.stat().st_size

                with (
                    mock.patch("aha_cli.services.orchestrator.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_backend,
                ):
                    first = record_sub_agent_report(root, run_id, "task-001", "sub-001", "sub-001 done")
                    second = record_sub_agent_report(root, run_id, "task-001", "sub-002", "sub-002 done")

                offset_file = chat_offset_path(run_dir(root, run_id), "main", "task-001")
                saved_offset = load_chat_offset(main_inbox, offset_file, from_start=False)
                queued_messages, _ = iter_jsonl_from(main_inbox, baseline_offset)

        self.assertFalse(first.get("round_summary_requested"))
        self.assertTrue(second["round_summary_requested"])
        self.assertEqual(saved_offset, baseline_offset)
        self.assertEqual(queued_messages[-1]["sender"], "aha")
        self.assertEqual(queued_messages[-1]["coordination"], "subagents_complete")
        start_backend.assert_called_once()
        self.assertEqual(start_backend.call_args.args[:3], (root, run_id, "main"))
        self.assertEqual(start_backend.call_args.kwargs["backend"], "codex")
        self.assertEqual(start_backend.call_args.kwargs["model"], "gpt-test")
        self.assertEqual(start_backend.call_args.kwargs["sandbox"], "danger-full-access")
        self.assertEqual(start_backend.call_args.kwargs["approval"], "never")
        self.assertEqual(start_backend.call_args.kwargs["task_id"], "task-001")
        self.assertFalse(start_backend.call_args.kwargs["from_start"])

    def test_monitor_task_coordination_recovers_requested_round_summary_main_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recover requested summary", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                update_agent_runtime(
                    root,
                    run_id,
                    "task-001",
                    "main",
                    model="gpt-test",
                    sandbox="danger-full-access",
                    approval="never",
                )
                set_agent_status(root, run_id, "task-001", "sub-001", "completed", 0)
                set_agent_status(root, run_id, "task-001", "main", "waiting", waiting_reason="subagents")
                set_task_status(root, run_id, "task-001", "running")
                mark_task_coordination(root, run_id, "task-001", round_summary_requested_at="2026-05-27T03:11:46+00:00")

                with (
                    mock.patch("aha_cli.services.orchestrator.backend_status", return_value={"status": "stopped"}),
                    mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_backend,
                ):
                    actions = monitor_task_coordination(root, run_id)

        self.assertIn({"type": "main_recovered", "task_id": "task-001"}, actions)
        start_backend.assert_called_once()
        self.assertEqual(start_backend.call_args.args[:3], (root, run_id, "main"))
        self.assertEqual(start_backend.call_args.kwargs["backend"], "codex")
        self.assertEqual(start_backend.call_args.kwargs["model"], "gpt-test")
        self.assertEqual(start_backend.call_args.kwargs["sandbox"], "danger-full-access")
        self.assertEqual(start_backend.call_args.kwargs["approval"], "never")
        self.assertEqual(start_backend.call_args.kwargs["task_id"], "task-001")
        self.assertFalse(start_backend.call_args.kwargs["from_start"])

    def test_monitor_task_coordination_ignores_stale_completed_sub_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Ignore stale sub-agents", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_agent_status(root, run_id, "task-001", "sub-001", "completed", 0)
                set_agent_status(root, run_id, "task-001", "sub-002", "completed", 0)
                set_task_status(root, run_id, "task-001", "running")
                mark_task_coordination(
                    root,
                    run_id,
                    "task-001",
                    final_summary_requested_at="",
                    final_summary_completed_at="",
                    round_summary_requested_at="",
                    round_summary_completed_at="",
                    followup_started_at="9999-01-01T00:00:00+00:00",
                )

                with mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_backend:
                    actions = monitor_task_coordination(root, run_id)

                self.assertNotIn({"type": "round_summary_requested", "task_id": "task-001"}, actions)
                start_backend.assert_not_called()
                main_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "main"), 0)
                self.assertFalse(any(item.get("coordination") == "subagents_complete" for item in main_messages))
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                self.assertFalse(any(event["type"] == "task_round_summary_requested" for event in events))

    def test_coordination_watchdog_recovers_stopped_pending_sub_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Watchdog", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_task_status(root, run_id, "task-001", "running")

                with mock.patch("aha_cli.services.orchestrator.start_backend", return_value={"status": "running"}) as start_backend:
                    actions = monitor_task_coordination(root, run_id)

                self.assertIn({"type": "agent_recovered", "task_id": "task-001", "agent_id": "sub-001"}, actions)
                start_backend.assert_called_once()
                self.assertFalse(start_backend.call_args.kwargs["from_start"])
                detail = task_snapshot(root, run_id, "task-001")
                sub_agent = next(agent for agent in detail["task"]["agents"] if agent["id"] == "sub-001")
                self.assertEqual(sub_agent["recovery_attempts"], 1)

    def test_main_does_not_reply_to_sub_agent_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "No loops", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                append_message(root, run_id, "main", "done", sender="sub-001", task_id="task-001", role="sub", from_agent="sub-001", to_agent="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec") as run_agent:
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                run_agent.assert_not_called()
                sub_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "sub-001"), 0)
                self.assertEqual(sub_messages, [])

    def test_sub_agent_skips_messages_after_final_summary_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Terminal sub", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                append_message(root, run_id, "sub-001", "no-op ack", sender="main", task_id="task-001", role="sub", from_agent="main", to_agent="sub-001")

                mark_task_coordination(root, run_id, "task-001", final_summary_requested_at="2026-05-15T00:00:00+00:00")

                with mock.patch("aha_cli.services.chat.run_codex_exec") as run_agent:
                    code, _ = self.run_cli("codex-chat", run_id, "sub-001", "--from-start", "--once")

                self.assertEqual(code, 0)
                run_agent.assert_not_called()
