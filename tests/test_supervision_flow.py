from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main, task_snapshot
from aha_cli.services.chat import apply_supervision_host_decision, chat_prompt
from aha_cli.services.messages import format_event
from aha_cli.store.filesystem import (
    add_agent,
    append_event,
    conversation_events_page,
    event_path,
    inbox_path,
    iter_jsonl_from,
    set_agent_status,
    set_task_status,
    status_snapshot,
    update_task_supervision_config,
)


class SupervisionFlowTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_codex_chat_records_supervision_stub_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Supervision stub", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    max_rounds=5,
                )
                append_message(root, run_id, "main", "托管测试", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "托管回复", None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task_status = status_snapshot(root, run_id)["tasks"][0]["status"]

        self.assertEqual(code, 0)
        self.assertIn("main -> browser: 托管回复", output)
        self.assertIn("main_reported_to_host", [row["type"] for row in rows])
        host_decisions = [row for row in rows if row["type"] == "host_decision"]
        self.assertEqual(host_decisions[-1]["data"]["decision"], "ask_user")
        self.assertNotIn("allowed", host_decisions[-1]["data"])
        applied = [row for row in rows if row["type"] == "main_applied_decision"]
        self.assertEqual(applied[-1]["data"]["effect"], "await_user")
        self.assertEqual(task_status, "awaiting_user")

    def test_codex_chat_records_claude_supervision_host_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude supervision host", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                    max_rounds=5,
                )
                append_message(
                    root,
                    run_id,
                    "host",
                    "后续如果要继续，优先用列表格式。",
                    sender="browser",
                    task_id="task-001",
                    role="host",
                    from_agent="browser",
                    to_agent="host",
                )
                append_message(root, run_id, "main", "托管测试", sender="browser", task_id="task-001", role="main")
                host_reply = '{"decision":"continue","reason":"needs priority","response":"先按阻塞项排优先级。","actions":[]}'

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "托管回复", None)),
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}) as start_host,
                ):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                with (
                    mock.patch("aha_cli.services.chat.run_claude_exec", return_value=(0, host_reply, None)) as host_run,
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}) as start_main,
                ):
                    host_code, _host_output = self.run_cli(
                        "claude-chat",
                        run_id,
                        "host",
                        "--sender",
                        "host",
                        "--sandbox",
                        "read-only",
                        "--task-id",
                        "task-001",
                        "--once",
                    )
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task = status_snapshot(root, run_id)["tasks"][0]
                main_page = conversation_events_page(root, run_id, "task-001", "main", limit=20)
                host_page = conversation_events_page(root, run_id, "task-001", "host", limit=20)
                host_inbox_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "host"), 0)

        self.assertEqual(code, 0)
        self.assertEqual(host_code, 0)
        self.assertIn("main -> browser: 托管回复", output)
        start_host.assert_called_once()
        self.assertEqual(start_host.call_args.args[2], "host")
        self.assertEqual(host_run.call_args.kwargs["permission_mode"], "plan")
        self.assertIn("后续如果要继续，优先用列表格式。", host_run.call_args.args[0])
        self.assertIn("托管回复", host_run.call_args.args[0])
        host = next(agent for agent in task["agents"] if agent["role"] == "host")
        self.assertEqual(host["backend"], "claude")
        self.assertEqual(host["sandbox"], "read-only")
        start_main.assert_called_once()
        self.assertTrue(any(row.get("message") == "托管回复" for row in host_inbox_messages))
        display_messages = [
            row["data"]
            for row in rows
            if row["type"] == "message" and row["data"].get("display_sender")
        ]
        self.assertEqual(
            [(row["display_sender"], row["display_target"], row["message"]) for row in display_messages],
            [("main", "host", "托管回复"), ("host", "main", "先按阻塞项排优先级。")],
        )
        self.assertFalse(any((row["data"].get("conversation") == "supervision") for row in rows if row["type"] == "message"))
        self.assertFalse(any("AHA 启动托管 host" in row["data"].get("message", "") for row in rows if row["type"] == "message"))
        self.assertFalse(any("host 正在判断" in row["data"].get("message", "") for row in rows if row["type"] == "message"))
        main_host_messages = [
            row["data"]
            for row in rows
            if row["type"] == "message"
            and row["data"].get("sender") == "main"
            and row["data"].get("target") == "host"
            and row["data"].get("message") == "托管回复"
        ]
        self.assertEqual(len(main_host_messages), 1)
        self.assertEqual(main_host_messages[0]["from_agent"], "main")
        self.assertEqual(main_host_messages[0]["to_agent"], "host")
        self.assertEqual(main_host_messages[0]["agent_id"], "host")
        routed_user_messages = [
            row["data"]
            for row in rows
            if row["type"] == "message"
            and row["data"].get("sender") == "browser"
            and row["data"].get("target") == "main"
            and row["data"].get("message") == "先按阻塞项排优先级。"
        ]
        self.assertEqual(len(routed_user_messages), 1)
        self.assertEqual(routed_user_messages[0]["from_agent"], "browser")
        self.assertEqual(routed_user_messages[0]["to_agent"], "main")
        self.assertEqual(routed_user_messages[0]["display_sender"], "host")
        self.assertEqual(routed_user_messages[0]["display_target"], "main")
        self.assertEqual(routed_user_messages[0]["agent_id"], "host")
        self.assertIn("browser -> main", format_event({"type": "message", "ts": "now", "data": routed_user_messages[0]}))
        self.assertTrue(any(event.get("data", {}).get("message") == "先按阻塞项排优先级。" for event in main_page["events"]))
        self.assertTrue(
            any(
                (event.get("data") or {}).get("display_sender") == "main"
                and (event.get("data") or {}).get("display_target") == "host"
                for event in main_page["events"]
            )
        )
        self.assertTrue(any((event.get("data") or {}).get("display_sender") == "main" and (event.get("data") or {}).get("display_target") == "host" for event in host_page["events"]))
        self.assertTrue(any((event.get("data") or {}).get("display_sender") == "host" and (event.get("data") or {}).get("display_target") == "main" for event in host_page["events"]))
        self.assertTrue(any(row["type"] == "main_reported_to_host" for row in rows))
        host_decisions = [row for row in rows if row["type"] == "host_decision"]
        self.assertEqual(host_decisions[-1]["data"]["host_backend"], "claude")
        self.assertEqual(host_decisions[-1]["data"]["decision"], "continue")
        self.assertEqual(host_decisions[-1]["data"]["executed_action_count"], 0)
        self.assertTrue(host_decisions[-1]["data"]["routed_to_main"])
        applied = [row for row in rows if row["type"] == "main_applied_decision"]
        self.assertEqual(applied[-1]["data"]["effect"], "routed_to_main")
        self.assertTrue(applied[-1]["data"]["routed_to_main"])
        self.assertEqual(task["status"], "running")

    def test_claude_supervision_wait_does_not_route_main_while_sub_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude supervision waits", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                    max_rounds=5,
                )
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_agent_status(root, run_id, "task-001", sub["id"], "running")
                set_task_status(root, run_id, "task-001", "running")

                with mock.patch("aha_cli.services.chat.start_backend") as start_main:
                    result = apply_supervision_host_decision(
                        root,
                        run_id,
                        "task-001",
                        host_agent_id="host",
                        host_reply='{"decision":"continue","reason":"waiting for sub","response":"好。","actions":[]}',
                        exit_code=0,
                    )

                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task = status_snapshot(root, run_id)["tasks"][0]

        self.assertTrue(result["waiting"])
        self.assertFalse(result["routed_to_main"])
        start_main.assert_not_called()
        self.assertEqual(task["status"], "running")
        self.assertEqual(next(agent for agent in task["agents"] if agent["id"] == "main")["status"], "waiting")
        self.assertFalse(
            any(
                row["type"] == "message"
                and row["data"].get("display_sender") == "host"
                and row["data"].get("display_target") == "main"
                for row in rows
            )
        )
        host_decisions = [row for row in rows if row["type"] == "host_decision"]
        self.assertEqual(host_decisions[-1]["data"]["decision"], "wait")
        self.assertTrue(host_decisions[-1]["data"]["waiting"])
        applied = [row for row in rows if row["type"] == "main_applied_decision"]
        self.assertEqual(applied[-1]["data"]["effect"], "waiting")

    def test_codex_chat_empty_main_reply_waits_when_sub_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Empty main reply with sub", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_agent_status(root, run_id, "task-001", sub["id"], "running")
                set_task_status(root, run_id, "task-001", "running")
                append_message(root, run_id, "main", "继续", sender="browser", task_id="task-001", role="main")

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "", None)),
                    mock.patch("aha_cli.services.chat.stop_task_backends") as stop_backends,
                ):
                    code, _output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task = status_snapshot(root, run_id)["tasks"][0]

        self.assertEqual(code, 0)
        stop_backends.assert_not_called()
        self.assertEqual(task["status"], "running")
        self.assertEqual(next(agent for agent in task["agents"] if agent["id"] == "main")["status"], "waiting")
        self.assertTrue(any(row["type"] == "agent_message_skipped" for row in rows))
        self.assertFalse(any(row["type"] == "agent_error" and row["data"].get("target") == "main" for row in rows))

    def test_codex_chat_routes_claude_supervision_ask_user_to_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude supervision asks user", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                    max_rounds=5,
                )
                append_message(root, run_id, "main", "托管测试", sender="browser", task_id="task-001", role="main")
                host_reply = '{"decision":"ask_user","reason":"needs real user","response":"你要继续改代码吗？","actions":[]}'

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "托管回复", None)),
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}) as start_host,
                ):
                    code, _output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                with (
                    mock.patch("aha_cli.services.chat.run_claude_exec", return_value=(0, host_reply, None)),
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}) as start_main,
                ):
                    host_code, _host_output = self.run_cli(
                        "claude-chat",
                        run_id,
                        "host",
                        "--sender",
                        "host",
                        "--sandbox",
                        "read-only",
                        "--task-id",
                        "task-001",
                        "--once",
                    )
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task = status_snapshot(root, run_id)["tasks"][0]
                main_page = conversation_events_page(root, run_id, "task-001", "main", limit=20)
                host_page = conversation_events_page(root, run_id, "task-001", "host", limit=20)

        self.assertEqual(code, 0)
        self.assertEqual(host_code, 0)
        start_host.assert_called_once()
        start_main.assert_not_called()
        main_host_messages = [
            row["data"]
            for row in rows
            if row["type"] == "message"
            and row["data"].get("sender") == "main"
            and row["data"].get("target") == "host"
            and row["data"].get("message") == "托管回复"
        ]
        self.assertEqual(len(main_host_messages), 1)
        self.assertEqual(main_host_messages[0]["display_sender"], "main")
        self.assertEqual(main_host_messages[0]["display_target"], "host")
        host_browser_messages = [
            row["data"]
            for row in rows
            if row["type"] == "message"
            and row["data"].get("sender") == "host"
            and row["data"].get("target") == "browser"
            and row["data"].get("message") == "你要继续改代码吗？"
        ]
        self.assertEqual(len(host_browser_messages), 1)
        self.assertEqual(host_browser_messages[0]["display_sender"], "host")
        self.assertEqual(host_browser_messages[0]["display_target"], "browser")
        self.assertEqual(host_browser_messages[0]["agent_id"], "main")
        self.assertTrue(any(event.get("data", {}).get("message") == "你要继续改代码吗？" for event in main_page["events"]))
        self.assertTrue(any(event.get("data", {}).get("message") == "你要继续改代码吗？" for event in host_page["events"]))
        host_decisions = [row for row in rows if row["type"] == "host_decision"]
        self.assertEqual(host_decisions[-1]["data"]["decision"], "ask_user")
        self.assertTrue(host_decisions[-1]["data"]["routed_to_browser"])
        applied = [row for row in rows if row["type"] == "main_applied_decision"]
        self.assertEqual(applied[-1]["data"]["effect"], "await_user")
        self.assertTrue(applied[-1]["data"]["routed_to_browser"])
        self.assertEqual(task["status"], "awaiting_user")

    def test_claude_supervision_max_rounds_are_per_user_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude supervision max rounds", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                    max_rounds=1,
                )
                for index in range(3):
                    append_event(
                        root,
                        run_id,
                        "host_decision",
                        {
                            "task_id": "task-001",
                            "host_agent_id": "host",
                            "decision": "continue",
                            "host_round": index + 1,
                        },
                    )
                append_message(root, run_id, "main", "新一轮测试", sender="browser", task_id="task-001", role="main")
                host_reply = '{"decision":"continue","reason":"new turn","response":"继续处理这一轮。","actions":[]}'

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "main 本轮回复", None)),
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}),
                ):
                    code, _output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                with (
                    mock.patch("aha_cli.services.chat.run_claude_exec", return_value=(0, host_reply, None)),
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}) as start_main,
                ):
                    host_code, _host_output = self.run_cli(
                        "claude-chat",
                        run_id,
                        "host",
                        "--sender",
                        "host",
                        "--sandbox",
                        "read-only",
                        "--task-id",
                        "task-001",
                        "--once",
                    )
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]

        self.assertEqual(code, 0)
        self.assertEqual(host_code, 0)
        start_main.assert_called_once()
        host_decisions = [row for row in rows if row["type"] == "host_decision"]
        self.assertEqual(host_decisions[-1]["data"]["host_round"], 1)
        self.assertTrue(host_decisions[-1]["data"]["routed_to_main"])

    def test_codex_chat_routes_claude_supervision_stop_to_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Claude supervision stops", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                    max_rounds=5,
                )
                append_message(root, run_id, "main", "托管测试", sender="browser", task_id="task-001", role="main")
                host_reply = '{"decision":"stop","reason":"done","response":"没问题，这轮可以结束。","actions":[]}'

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "托管回复", None)),
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}) as start_host,
                ):
                    code, _output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                with (
                    mock.patch("aha_cli.services.chat.run_claude_exec", return_value=(0, host_reply, None)),
                    mock.patch("aha_cli.services.chat.start_backend", return_value={"status": "running"}) as start_main,
                ):
                    host_code, _host_output = self.run_cli(
                        "claude-chat",
                        run_id,
                        "host",
                        "--sender",
                        "host",
                        "--sandbox",
                        "read-only",
                        "--task-id",
                        "task-001",
                        "--once",
                    )
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]
                task = status_snapshot(root, run_id)["tasks"][0]

        self.assertEqual(code, 0)
        self.assertEqual(host_code, 0)
        start_host.assert_called_once()
        start_main.assert_not_called()
        host_browser_messages = [
            row["data"]
            for row in rows
            if row["type"] == "message"
            and row["data"].get("sender") == "host"
            and row["data"].get("target") == "browser"
            and row["data"].get("message") == "没问题，这轮可以结束。"
        ]
        self.assertEqual(len(host_browser_messages), 1)
        self.assertEqual(host_browser_messages[0]["display_sender"], "host")
        self.assertEqual(host_browser_messages[0]["display_target"], "browser")
        host_main_messages = [
            row["data"]
            for row in rows
            if row["type"] == "message"
            and row["data"].get("display_sender") == "host"
            and row["data"].get("display_target") == "main"
        ]
        self.assertEqual(host_main_messages, [])
        host_decisions = [row for row in rows if row["type"] == "host_decision"]
        self.assertEqual(host_decisions[-1]["data"]["decision"], "stop")
        self.assertTrue(host_decisions[-1]["data"]["routed_to_browser"])
        applied = [row for row in rows if row["type"] == "main_applied_decision"]
        self.assertEqual(applied[-1]["data"]["effect"], "stopped")
        self.assertTrue(applied[-1]["data"]["routed_to_browser"])
        self.assertEqual(task["status"], "awaiting_user")

    def test_agent_command_does_not_write_task_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Agent command", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(
                    root,
                    run_id,
                    "main",
                    "/status",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    command_namespace="agent",
                    original_command="/agent status",
                )

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "状态：完成", None)) as run_agent:
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("main -> browser: 状态：完成", output)
                self.assertEqual(run_agent.call_args.kwargs["session"]["agent_id"], "main")
                self.assertIsNone(run_agent.call_args.kwargs["session"].get("backend_session_id"))

                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["result"], "")
                self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "pending")

    def test_agent_command_prompt_does_not_replay_task_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Agent command prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(
                    root,
                    run_id,
                    "browser",
                    "状态：已完成，旧任务结果",
                    sender="main",
                    task_id="task-001",
                    role="main",
                    from_agent="main",
                    to_agent="browser",
                )
                command = append_message(
                    root,
                    run_id,
                    "main",
                    "/status",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    command_namespace="agent",
                    original_command="/agent status",
                )

                prompt = chat_prompt(root, run_id, "main", command, "")
                self.assertIn("The user sent an agent command", prompt)
                self.assertIn("/status: report this agent's runtime/session metadata only", prompt)
                self.assertNotIn("Recent events:", prompt)
                self.assertNotIn("旧任务结果", prompt)



if __name__ == "__main__":
    unittest.main()
