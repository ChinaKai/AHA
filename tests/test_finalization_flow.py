from __future__ import annotations

import asyncio
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main, task_snapshot
from aha_cli.services.chat import chat_prompt_with_metrics
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.store.filesystem import (
    add_agent,
    append_event,
    complete_task,
    event_path,
    iter_jsonl_from,
    list_task_lifecycle_rounds,
    list_task_rounds,
    run_dir,
    reopen_task,
    set_agent_status,
    set_task_status,
    status_snapshot,
    task_final_snapshot,
    write_task_result,
)
from aha_cli.web.server import finalization_prompt, handle_slash_command
from tests.helpers import fetch_ui_response, json_response_body


class FinalizationFlowTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

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
