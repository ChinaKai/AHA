from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main, task_snapshot
from aha_cli.services.chat import apply_supervision_host_decision, chat_offset_path, chat_prompt, chat_prompt_with_metrics, load_chat_offset, save_chat_offset
from aha_cli.services.messages import format_event
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.filesystem import (
    add_agent,
    append_event,
    complete_task,
    conversation_events_page,
    event_path,
    inbox_path,
    iter_jsonl_from,
    list_task_lifecycle_rounds,
    list_task_rounds,
    read_json,
    run_dir,
    reopen_task,
    set_agent_status,
    set_task_status,
    status_snapshot,
    task_final_snapshot,
    update_task_proxy_config,
    update_task_supervision_config,
    write_task_result,
)
from aha_cli.web.server import finalization_prompt, handle_slash_command
from tests.helpers import fetch_ui_response, json_response_body


class ChatFlowTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_chat_offset_persists_unprocessed_messages_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Offsets", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                inbox = inbox_path(root, run_id, "main")
                offset_file = chat_offset_path(run_dir(root, run_id), "main")
                initial_offset = load_chat_offset(inbox, offset_file, from_start=False)
                append_message(root, run_id, "main", "queued while stopped", sender="browser", task_id="task-001", role="main")
                save_chat_offset(offset_file, initial_offset)

                self.assertEqual(load_chat_offset(inbox, offset_file, from_start=False), initial_offset)
                self.assertGreater(inbox.stat().st_size, initial_offset)

    def test_task_scoped_chat_offsets_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)

            global_offset = chat_offset_path(run, "main")
            task_one_offset = chat_offset_path(run, "main", "task-001")
            task_two_offset = chat_offset_path(run, "main", "task-002")

        self.assertNotEqual(global_offset, task_one_offset)
        self.assertNotEqual(task_one_offset, task_two_offset)
        self.assertEqual(task_one_offset.name, "chat-offset-task-001-main.json")

    def test_task_scoped_codex_chat_skips_other_task_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task scoped workers", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                self.run_cli("task", "add", run_id, "Second task", "--no-dispatch")
                append_message(root, run_id, "main", "task two", sender="browser", task_id="task-002", role="main")
                append_message(root, run_id, "main", "task one", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "task one reply", None)) as run_agent:
                    code, output = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

                self.assertEqual(code, 0)
                self.assertEqual(run_agent.call_count, 1)
                self.assertIn("task-001", run_agent.call_args.args[0])
                self.assertNotIn("User message from browser", output)
                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)
                self.assertEqual(browser_messages[-1]["message"], "task one reply")
                self.assertEqual(browser_messages[-1]["task_id"], "task-001")
                scoped_offset = chat_offset_path(run_dir(root, run_id), "main", "task-001")
                self.assertTrue(scoped_offset.exists())
                self.assertFalse(chat_offset_path(run_dir(root, run_id), "main", "task-002").exists())

    def test_codex_chat_once_saves_offset_after_processed_message_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Task scoped offsets", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "first", sender="browser", task_id="task-001", role="main")
                append_message(root, run_id, "main", "second", sender="browser", task_id="task-001", role="main")

                with mock.patch(
                    "aha_cli.services.chat.run_codex_exec",
                    side_effect=[(0, "reply one", None), (0, "reply two", None)],
                ) as run_agent:
                    code, first_output = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")
                    self.assertEqual(code, 0)
                    code, second_output = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--once")
                    self.assertEqual(code, 0)

                browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)

        self.assertEqual(run_agent.call_count, 2)
        self.assertIn("main -> browser: reply one", first_output)
        self.assertIn("main -> browser: reply two", second_output)
        self.assertIn("User message from browser", run_agent.call_args_list[0].args[0])
        self.assertIn("first", run_agent.call_args_list[0].args[0])
        self.assertIn("second", run_agent.call_args_list[1].args[0])
        self.assertEqual([item["message"] for item in browser_messages[-2:]], ["reply one", "reply two"])

    def test_codex_chat_passes_latest_task_proxy_env_to_codex_exec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "Chat proxy env",
                    "--agents",
                    "1",
                    "--http-proxy",
                    "http://127.0.0.1:7890",
                    "--https-proxy",
                    "http://127.0.0.1:7890",
                    "--no-proxy",
                    "localhost,127.0.0.1",
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_proxy_config(root, run_id, "task-001", http_proxy="http://127.0.0.1:8888")
                append_message(root, run_id, "main", "use proxy", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "reply", None)) as run_agent:
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

        self.assertEqual(code, 0)
        proxy_env = run_agent.call_args.kwargs["proxy_env"]
        self.assertEqual(proxy_env["HTTP_PROXY"], "http://127.0.0.1:8888")
        self.assertEqual(proxy_env["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(proxy_env["NO_PROXY"], "localhost,127.0.0.1")
        self.assertEqual(proxy_env["http_proxy"], "http://127.0.0.1:8888")

    def test_chat_prompt_redacts_proxy_values_from_status_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli(
                    "plan",
                    "Prompt redaction",
                    "--agents",
                    "1",
                    "--http-proxy",
                    "http://user:secret@proxy.local:7890",
                    "--https-proxy",
                    "http://user:secret@proxy.local:7890",
                    "--no-proxy",
                    "internal.local",
                )
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                prompt = chat_prompt(
                    root,
                    run_id,
                    "main",
                    {
                        "sender": "browser",
                        "message": "hello",
                        "task_id": "task-001",
                        "role": "main",
                    },
                    "prefix",
                )

        self.assertNotIn("secret", prompt)
        self.assertNotIn("proxy.local:7890", prompt)
        self.assertNotIn("internal.local", prompt)
        self.assertIn("'preferred_http_proxy': '<set>'", prompt)

    def test_chat_prompt_with_metrics_reports_sizes_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Prompt metrics", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "old-event"})

                prompt, metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    {
                        "sender": "browser",
                        "message": "super-secret-user-text",
                        "task_id": "task-001",
                        "role": "main",
                    },
                    "prefix",
                )

        metrics_json = json.dumps(metrics, ensure_ascii=False)
        self.assertEqual(metrics["total"]["chars"], len(prompt))
        self.assertEqual(metrics["components"]["user_message"]["chars"], len("super-secret-user-text"))
        self.assertGreater(metrics["components"]["status_snapshot"]["chars"], 0)
        self.assertGreater(metrics["components"]["recent_events"]["chars"], 0)
        self.assertGreater(metrics["components"]["task_context"]["chars"], 0)
        self.assertNotIn("super-secret-user-text", metrics_json)
        self.assertNotIn("old-event", metrics_json)

    def test_chat_prompt_filters_private_host_notes_from_main_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Host note isolation", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(
                    root,
                    run_id,
                    "host",
                    "让 main 下一轮只回复数字2",
                    sender="browser",
                    task_id="task-001",
                    role="host",
                    from_agent="browser",
                    to_agent="host",
                )
                main_message = append_message(
                    root,
                    run_id,
                    "main",
                    "请直接回复数字1",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    from_agent="browser",
                    to_agent="main",
                )

                prompt = chat_prompt(root, run_id, "main", main_message, "")

        self.assertIn("请直接回复数字1", prompt)
        self.assertNotIn("让 main 下一轮只回复数字2", prompt)

    def test_chat_prompt_hides_supervision_host_from_main_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Host prompt isolation", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                update_task_supervision_config(
                    root,
                    run_id,
                    "task-001",
                    mode="assisted",
                    host_backend="claude",
                    real_agent_enabled=True,
                )
                task = status_snapshot(root, run_id)["tasks"][0]
                append_message(
                    root,
                    run_id,
                    "browser",
                    "Agents:\n- host role=host backend=claude assignment=task supervision host agent",
                    sender="AHA",
                    task_id="task-001",
                    role="aha",
                    from_agent="aha",
                    to_agent="browser",
                    agent_id="main",
                )
                append_event(
                    root,
                    run_id,
                    "agent_message",
                    {
                        "source": "claude-chat",
                        "task_id": "task-001",
                        "target": "main",
                        "item_type": "agent_message",
                        "text": "我处理 03 号，让host agent处理02号重启问题。",
                    },
                )
                main_message = append_message(
                    root,
                    run_id,
                    "main",
                    "你可以和 sub agent 一人一个问题",
                    sender="browser",
                    task_id="task-001",
                    role="main",
                    from_agent="browser",
                    to_agent="main",
                )

                prompt = chat_prompt(root, run_id, "main", main_message, "")

        self.assertTrue(any(agent["role"] == "host" for agent in task["agents"]))
        self.assertIn("你可以和 sub agent 一人一个问题", prompt)
        self.assertNotIn("host role=host", prompt)
        self.assertNotIn("'id': 'host'", prompt)
        self.assertNotIn("'role': 'host'", prompt)
        self.assertNotIn("让host agent处理02号", prompt)
        self.assertNotIn("task supervision host agent", prompt)

    def test_codex_chat_does_not_auto_write_final_or_complete_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Codex chat", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "你好", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "真实回复", None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("main -> browser: 真实回复", output)
                self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "awaiting_user")
                self.assertEqual(task_snapshot(root, run_id, "task-001")["result"], "")

                code, watch_output = self.run_cli("watch", run_id, "--once")
                self.assertEqual(code, 0)
                self.assertIn("message task=task-001 main -> browser: 真实回复", watch_output)
                self.assertIn("task_status_changed", watch_output)
                self.assertNotIn("task_result_written", watch_output)

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

    def test_codex_chat_records_prompt_metrics_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Codex prompt metrics", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "measure prompt", sender="browser", task_id="task-001", role="main")

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "reply", None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                rows = [json.loads(line) for line in event_path(root, run_id).read_text(encoding="utf-8").splitlines()]

        metrics_events = [row for row in rows if row["type"] == "agent_prompt_metrics"]
        self.assertEqual(code, 0)
        self.assertEqual(len(metrics_events), 1)
        metrics = metrics_events[0]["data"]
        self.assertEqual(metrics["source"], "codex-chat")
        self.assertEqual(metrics["task_id"], "task-001")
        self.assertGreater(metrics["total"]["chars"], 0)
        self.assertGreater(metrics["components"]["status_snapshot"]["chars"], 0)
        self.assertGreater(metrics["components"]["task_context"]["chars"], 0)

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

    def test_raw_result_file_without_final_metadata_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Stale result", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                path = run_dir(root, run_id) / "results/task-001.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("old auto result\n", encoding="utf-8")

                self.assertEqual(task_snapshot(root, run_id, "task-001")["result"], "")

    def test_finalize_policy_updates_existing_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Finalize chat", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                write_task_result(root, run_id, "task-001", "old final")
                set_task_status(root, run_id, "task-001", "completed", 0)
                append_message(
                    root,
                    run_id,
                    "main",
                    "Generate the Final",
                    sender="aha",
                    task_id="task-001",
                    role="main",
                    result_policy="finalize",
                    original_command="/aha finalize",
                )

                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "new final", None)):
                    code, output = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")
                self.assertEqual(code, 0)
                self.assertIn("main -> aha: new final", output)
                self.assertEqual(task_snapshot(root, run_id, "task-001")["result"].strip(), "new final")
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["status"], "completed")
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["exit_code"], 0)

    def test_final_driven_completion_reopen_preserves_round_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final loop", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                append_message(root, run_id, "main", "先做第一轮", sender="browser", task_id="task-001", role="main")
                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "第一轮已处理", None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--from-start", "--once")

                self.assertEqual(code, 0)
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["status"], "awaiting_user")
                self.assertEqual(task_snapshot(root, run_id, "task-001")["result"], "")
                events, _ = iter_jsonl_from(event_path(root, run_id), 0)
                self.assertTrue(any(event["type"] == "agent_finished" for event in events))
                self.assertFalse(any(event["type"] == "task_completed" for event in events))

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/aha final",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("Finalization requested", payload["message"]["message"])

                first_final = "第一轮 Final"
                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, first_final, None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--once")

                self.assertEqual(code, 0)
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "completed")
                self.assertEqual(detail["result"].strip(), first_final)
                first_rounds = list_task_lifecycle_rounds(root, run_id, "task-001")
                self.assertEqual(len(first_rounds), 1)
                self.assertEqual(first_rounds[0]["round_id"], "round-001")
                self.assertEqual(first_rounds[0]["status"], "finalized")
                self.assertEqual((run_dir(root, run_id) / first_rounds[0]["final_path"]).read_text(encoding="utf-8").strip(), first_final)

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/aha reopen",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("reopened", payload["message"]["message"])
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "awaiting_user")
                self.assertEqual(detail["result"].strip(), first_final)
                reopened_rounds = list_task_lifecycle_rounds(root, run_id, "task-001")
                self.assertEqual(len(reopened_rounds), 2)
                self.assertEqual(reopened_rounds[0]["status"], "finalized")
                self.assertEqual(reopened_rounds[1]["round_id"], "round-002")
                self.assertEqual(reopened_rounds[1]["status"], "active")
                self.assertEqual(reopened_rounds[1]["reopened_from_round_id"], "round-001")
                self.assertEqual((run_dir(root, run_id) / reopened_rounds[0]["final_path"]).read_text(encoding="utf-8").strip(), first_final)

                append_message(root, run_id, "main", "继续第二轮", sender="browser", task_id="task-001", role="main")
                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "第二轮已处理", None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--once")
                self.assertEqual(code, 0)
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["status"], "awaiting_user")

                handled, agent_message, payload = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main", "to_agent": "main"},
                    "/aha final",
                    "task-001",
                )
                self.assertTrue(handled)
                self.assertIsNone(agent_message)
                self.assertIn("Finalization requested", payload["message"]["message"])

                second_final = "第二轮 Final"
                with mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, second_final, None)):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--once")

                self.assertEqual(code, 0)
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "completed")
                self.assertEqual(detail["result"].strip(), second_final)
                final_rounds = list_task_lifecycle_rounds(root, run_id, "task-001")
                self.assertEqual(len(final_rounds), 2)
                self.assertEqual(final_rounds[0]["status"], "finalized")
                self.assertEqual(final_rounds[1]["status"], "finalized")
                self.assertEqual((run_dir(root, run_id) / final_rounds[0]["final_path"]).read_text(encoding="utf-8").strip(), first_final)
                self.assertEqual((run_dir(root, run_id) / final_rounds[1]["final_path"]).read_text(encoding="utf-8").strip(), second_final)

    def test_final_api_returns_task_overview_and_preserves_round_finals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final overview", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                task = task_snapshot(root, run_id, "task-001")["task"]
                first_final = "#### Final\n## 阶段总览\n第一轮 raw final 正文只应保留在 round 快照文件中"
                second_final = "#### Final\n## 阶段总览\n第二轮 raw final 正文只应保留在 round 快照文件中"

                write_task_result(root, run_id, "task-001", first_final)
                complete_task(root, run_id, "task-001", 0)
                first_round = list_task_lifecycle_rounds(root, run_id, "task-001")[0]
                first_round_final = run_dir(root, run_id) / first_round["final_path"]

                reopen_task(root, run_id, "task-001")
                write_task_result(root, run_id, "task-001", second_final)
                complete_task(root, run_id, "task-001", 0)
                second_round = list_task_lifecycle_rounds(root, run_id, "task-001")[1]
                second_round_final = run_dir(root, run_id) / second_round["final_path"]
                reopen_task(root, run_id, "task-001")

                snapshot = task_final_snapshot(root, run_id, "task-001")
                api_response = asyncio.run(fetch_ui_response(root, run_id, "/api/task/task-001/final"))
                api_body = json_response_body(api_response)
                overview_text = (run_dir(root, run_id) / "results/task-001.md").read_text(encoding="utf-8")

                self.assertTrue(api_response.startswith(b"HTTP/1.1 200 OK"))
                self.assertEqual(api_body["result"], overview_text)
                self.assertNotEqual(api_body["result"].strip(), second_final)
                self.assertIn("task-001", api_body["result"])
                self.assertIn(task["title"], api_body["result"])
                for expected in ("round-001", "round-002", "round-003"):
                    self.assertIn(expected, api_body["result"])
                self.assertIn("Raw final:", api_body["result"])
                self.assertNotIn(first_final, api_body["result"])
                self.assertNotIn(second_final, api_body["result"])
                self.assertNotIn("#### Final", api_body["result"])
                self.assertNotIn("## 阶段总览", api_body["result"])
                self.assertIn(first_round["final_path"], api_body["result"])
                self.assertIn(second_round["final_path"], api_body["result"])
                self.assertRegex(api_body["result"].lower(), r"reopen|reopened|复开|重开|重新打开|继续")
                self.assertEqual(first_round_final.read_text(encoding="utf-8").strip(), first_final)
                self.assertEqual(second_round_final.read_text(encoding="utf-8").strip(), second_final)
                self.assertEqual([item["round_id"] for item in snapshot["finals"]], ["round-001", "round-002"])

    def test_final_summary_stops_task_scoped_backends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final cleanup", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                add_agent(root, run_id, "task-001", backend="codex", role="sub")
                set_agent_status(root, run_id, "task-001", "sub-001", "completed", 0)
                set_task_status(root, run_id, "task-001", "running")
                append_message(
                    root,
                    run_id,
                    "main",
                    "Produce final summary",
                    sender="aha",
                    task_id="task-001",
                    role="main",
                    result_policy="finalize",
                    reply_target="browser",
                )

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "final summary", None)),
                    mock.patch("aha_cli.services.chat.stop_task_backends", return_value=[]) as stop_backends,
                    mock.patch("aha_cli.services.chat.mark_backend_stopped") as mark_stopped,
                ):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

                self.assertEqual(code, 0)
                stop_backends.assert_called_once()
                self.assertEqual(stop_backends.call_args.args[:3], (root / ".aha", run_id, "task-001"))
                self.assertIn("exclude_pid", stop_backends.call_args.kwargs)
                mark_stopped.assert_called_once()
                self.assertEqual(task_snapshot(root, run_id, "task-001")["task"]["status"], "completed")

    def test_task_scoped_main_backend_stops_after_round_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Round cleanup", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_agent_status(root, run_id, "task-001", sub["id"], "completed", 0)
                set_task_status(root, run_id, "task-001", "running")
                append_message(
                    root,
                    run_id,
                    "main",
                    render_prompt_template("task_round_summary.md", task_id="task-001"),
                    sender="aha",
                    task_id="task-001",
                    role="main",
                    reply_target="browser",
                    coordination="subagents_complete",
                )

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "round summary", None)),
                    mock.patch("aha_cli.services.chat.mark_backend_stopped") as mark_stopped,
                ):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

                self.assertEqual(code, 0)
                mark_stopped.assert_called_once()
                self.assertEqual(mark_stopped.call_args.args[:3], (root / ".aha", run_id, "main"))
                self.assertEqual(mark_stopped.call_args.kwargs["task_id"], "task-001")
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "awaiting_user")
                self.assertEqual(next(agent for agent in detail["task"]["agents"] if agent["id"] == "main")["status"], "completed")

    def test_task_scoped_main_backend_keeps_running_while_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Waiting backend", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                sub = add_agent(root, run_id, "task-001", backend="codex", role="sub", created_by="main")
                set_agent_status(root, run_id, "task-001", sub["id"], "running")
                set_task_status(root, run_id, "task-001", "running")
                append_message(root, run_id, "main", "继续", sender="browser", task_id="task-001", role="main")

                with (
                    mock.patch("aha_cli.services.chat.run_codex_exec", return_value=(0, "", None)),
                    mock.patch("aha_cli.services.chat.mark_backend_stopped") as mark_stopped,
                ):
                    code, _ = self.run_cli("codex-chat", run_id, "main", "--task-id", "task-001", "--from-start", "--once")

                self.assertEqual(code, 0)
                mark_stopped.assert_not_called()
                detail = task_snapshot(root, run_id, "task-001")
                self.assertEqual(detail["task"]["status"], "running")
                self.assertEqual(next(agent for agent in detail["task"]["agents"] if agent["id"] == "main")["status"], "waiting")

    def test_finalization_prompt_includes_task_journal(self) -> None:
        prompt = finalization_prompt(
            "task-001",
            "Journal task",
            [
                {
                    "round_id": "round-001",
                    "trigger": "main_turn",
                    "summary": "完成小修复",
                    "changed_files": ["src/app.py"],
                    "verification": ["unit tests"],
                    "risks": [],
                }
            ],
        )

        self.assertIn("Task journal (chronological ordered list):", prompt)
        self.assertIn("1. 完成小修复", prompt)
        self.assertIn("round-001", prompt)
        self.assertIn("完成小修复", prompt)
        self.assertIn("Use the Task journal as the primary source", prompt)
        self.assertIn("chronological ordered list", prompt)

    def test_aha_checkpoint_records_task_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Checkpoint task", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                handled, forwarded, response = handle_slash_command(
                    root,
                    run_id,
                    {"sender": "browser", "target": "main"},
                    "/aha checkpoint 完成第一轮清理",
                    "task-001",
                )

                self.assertTrue(handled)
                self.assertIsNone(forwarded)
                self.assertIn("Checkpoint recorded", response["message"]["message"])
                rounds = list_task_rounds(root, run_id, "task-001")
                self.assertEqual(rounds[0]["trigger"], "manual")
                self.assertEqual(rounds[0]["summary"], "完成第一轮清理")

    def test_chat_prompt_uses_recent_events_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Recent prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                for index in range(30):
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": f"prompt-event-{index}"})

                prompt = chat_prompt(root, run_id, "main", {"sender": "browser", "message": "status"}, "")

        self.assertIn("prompt-event-29", prompt)
        self.assertNotIn("prompt-event-0", prompt)

    def test_chat_prompt_scopes_status_and_events_to_current_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Prompt scoping", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                code, _ = self.run_cli("task", "add", run_id, "Foreign verbose task title that should stay out", "--no-dispatch")
                self.assertEqual(code, 0)
                code, _ = self.run_cli("task", "add", run_id, "Current compact prompt title", "--no-dispatch")
                self.assertEqual(code, 0)
                append_event(root, run_id, "agent_message", {"task_id": "task-002", "target": "main", "text": "foreign-event"})
                append_event(root, run_id, "agent_message", {"task_id": "task-003", "target": "main", "text": "current-event"})

                prompt = chat_prompt(
                    root,
                    run_id,
                    "main",
                    {"sender": "browser", "message": "status", "task_id": "task-003", "role": "main"},
                    "",
                )

        self.assertIn("Current compact prompt title", prompt)
        self.assertIn("current-event", prompt)
        self.assertIn("task_counts", prompt)
        self.assertNotIn("Foreign verbose task title that should stay out", prompt)
        self.assertNotIn("foreign-event", prompt)

    def test_chat_prompt_uses_delta_for_existing_sticky_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Sticky prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                session_file = run_dir(root, run_id) / "tasks" / "task-001" / "sessions" / "main.json"
                session = read_json(session_file)
                session["backend_session_id"] = "backend-session-1"
                session_file.write_text(json.dumps(session), encoding="utf-8")
                append_event(
                    root,
                    run_id,
                    "agent_message",
                    {"task_id": "task-001", "target": "main", "text": "already-in-backend-session"},
                )
                append_event(root, run_id, "agent_finished", {"task_id": "task-001", "target": "main", "exit_code": 0})
                append_event(root, run_id, "task_hidden", {"task_id": "task-001"})

                prompt, metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    {
                        "sender": "browser",
                        "message": "next request",
                        "task_id": "task-001",
                        "role": "main",
                        "ts": "2026-01-01T00:00:00+00:00",
                    },
                    "prefix",
                )

        self.assertEqual(metrics["prompt_mode"], "sticky_delta")
        self.assertIn("Current task delta:", prompt)
        self.assertIn("backend-session-1", prompt)
        self.assertIn("next request", prompt)
        self.assertIn("task_hidden", prompt)
        self.assertNotIn("Ownership and routing policy", prompt)
        self.assertNotIn("already-in-backend-session", prompt)
        self.assertIn("sticky_context", metrics["components"])
        self.assertIn("delta_status", metrics["components"])
        self.assertNotIn("task_context", metrics["components"])

    def test_finalization_prompt_omits_recent_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Final prompt", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(
                    root,
                    run_id,
                    "message",
                    {
                        "task_id": "task-001",
                        "sender": "browser",
                        "target": "main",
                        "message": "NOISY_RECENT_EVENT" * 1000,
                    },
                )

                prompt, metrics = chat_prompt_with_metrics(
                    root,
                    run_id,
                    "main",
                    {
                        "sender": "aha",
                        "message": finalization_prompt("task-001", "Final prompt", []),
                        "task_id": "task-001",
                        "role": "main",
                        "result_policy": "finalize",
                        "ts": "2026-01-01T00:00:00+00:00",
                    },
                    "prefix",
                )

        self.assertTrue(metrics["is_finalization"])
        self.assertEqual(metrics["event_limit"], 0)
        self.assertIn("omitted for finalization", prompt)
        self.assertNotIn("NOISY_RECENT_EVENT", prompt)
        self.assertLess(metrics["components"]["recent_events"]["chars"], 120)
