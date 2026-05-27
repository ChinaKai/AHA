from __future__ import annotations

import asyncio
import gzip
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from aha_cli.cli import append_message, main, task_snapshot
from aha_cli.services.backend_runtime import start_backend
from aha_cli.services.prompt_artifacts import save_prompt_artifact
from aha_cli.store.filesystem import (
    append_event,
    conversation_events_page,
    event_path,
    iter_jsonl_from,
    iter_jsonl_reverse,
    read_json,
    run_dir,
    session_path,
    set_task_status,
    status_snapshot,
    task_log_page,
    write_json,
)
from tests.helpers import fetch_ui_response, json_response_body


class WebEventsApiTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_ui_core_endpoints_return_without_full_event_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Fast UI endpoints", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                for index in range(3000):
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": f"event-{index}"})
                set_task_status(root, run_id, "task-001", "completed", exit_code=0)

                responses = {
                    target: asyncio.run(fetch_ui_response(root, run_id, target))
                    for target in ("/", "/static/app.js", "/api/status", "/api/events?offset=-1")
                }

        for response in responses.values():
            self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        events_body = json_response_body(responses["/api/events?offset=-1"])
        self.assertEqual(events_body["events"], [])
        self.assertGreater(events_body["offset"], 0)
        status_body = json_response_body(responses["/api/status"])
        self.assertEqual(status_body["tasks"][0]["display_status"], "completed")

    def test_ui_gzips_large_static_and_json_responses_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Gzip UI", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                script_response = asyncio.run(
                    fetch_ui_response(root, run_id, "/static/app.js", headers={"Accept-Encoding": "gzip"})
                )
                status_response = asyncio.run(
                    fetch_ui_response(root, run_id, "/api/status?lite=1", headers={"Accept-Encoding": "gzip"})
                )

        self.assertIn(b"Content-Encoding: gzip\r\n", script_response)
        self.assertIn(b"Content-Encoding: gzip\r\n", status_response)
        script_body = gzip.decompress(script_response.split(b"\r\n\r\n", 1)[1]).decode("utf-8")
        status_body = json.loads(gzip.decompress(status_response.split(b"\r\n\r\n", 1)[1]).decode("utf-8"))
        self.assertIn("conversationPageLimit", script_body)
        self.assertEqual(status_body["run_id"], run_id)

    def test_prompt_artifact_api_reads_raw_prompt_by_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Raw prompt artifact", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                prompt_ref = save_prompt_artifact(root, run_id, "task-001", "main", "raw assembled prompt\nline two")

                response = asyncio.run(fetch_ui_response(root, run_id, f"/api/prompt-artifact?ref={prompt_ref['path']}"))
                invalid_response = asyncio.run(fetch_ui_response(root, run_id, "/api/prompt-artifact?ref=../events.jsonl"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(body["prompt"], "raw assembled prompt\nline two")
        self.assertEqual(body["prompt_ref"]["path"], prompt_ref["path"])
        self.assertEqual(body["prompt_ref"]["chars"], len(body["prompt"]))
        self.assertTrue(invalid_response.startswith(b"HTTP/1.1 400 Bad Request"))

    def test_api_events_uses_snapshot_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Paged events", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                for index in range(10):
                    append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": f"event-{index}"})

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/events?offset=0&limit=3"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(len(body["events"]), 3)
        self.assertEqual(body["limit"], 3)
        self.assertTrue(body["has_more"])
        self.assertLess(body["offset"], body["snapshot_offset"])

    def test_conversation_events_page_filters_and_pages_by_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            append_event(root, run_id, "message", {"task_id": "task-001", "sender": "browser", "to_agent": "main", "message": "one"})
            append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "two"})
            append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "sub-001", "text": "sub"})
            append_event(root, run_id, "agent_message", {"task_id": "task-002", "target": "main", "text": "other task"})

            latest = conversation_events_page(root, run_id, "task-001", "main", limit=1)
            append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "new realtime"})
            realtime, _ = iter_jsonl_from(event_path(root, run_id), latest["after_offset"])
            older = conversation_events_page(root, run_id, "task-001", "main", limit=1, before=latest["next_before_offset"])

        self.assertEqual(latest["count"], 1)
        self.assertTrue(latest["has_more"])
        self.assertEqual(latest["events"][0]["data"]["text"], "two")
        self.assertEqual(realtime[0]["data"]["text"], "new realtime")
        self.assertFalse(older["has_more"])
        self.assertEqual(older["events"][0]["data"]["message"], "one")

    def test_conversation_events_page_includes_supervision_events_for_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            append_event(root, run_id, "main_reported_to_host", {"task_id": "task-001", "host_backend": "stub"})
            append_event(root, run_id, "host_decision", {"task_id": "task-001", "decision": "ask_user"})
            append_event(root, run_id, "main_applied_decision", {"task_id": "task-001", "decision": "ask_user", "applied": True})
            append_event(root, run_id, "host_decision", {"task_id": "task-002", "decision": "stop"})

            main_page = conversation_events_page(root, run_id, "task-001", "main", limit=10)
            sub_page = conversation_events_page(root, run_id, "task-001", "sub-001", limit=10)

        self.assertEqual(
            [event["type"] for event in main_page["events"]],
            ["main_reported_to_host", "host_decision", "main_applied_decision"],
        )
        self.assertEqual(sub_page["events"], [])

    def test_conversation_events_page_shares_host_forwarding_but_hides_aha_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            append_event(
                root,
                run_id,
                "message",
                {
                    "task_id": "task-001",
                    "sender": "main",
                    "target": "host",
                    "from_agent": "main",
                    "to_agent": "host",
                    "agent_id": "host",
                    "display_sender": "main",
                    "display_target": "host",
                    "message": "main reply",
                },
            )
            append_event(
                root,
                run_id,
                "message",
                {
                    "task_id": "task-001",
                    "sender": "AHA",
                    "target": "host",
                    "display_sender": "host",
                    "display_target": "host",
                    "message": "host 正在判断本轮下一步。",
                },
            )
            append_event(
                root,
                run_id,
                "message",
                {
                    "task_id": "task-001",
                    "sender": "browser",
                    "target": "main",
                    "agent_id": "host",
                    "display_sender": "host",
                    "display_target": "main",
                    "message": "next step",
                },
            )

            main_page = conversation_events_page(root, run_id, "task-001", "main", limit=10)
            host_page = conversation_events_page(root, run_id, "task-001", "host", limit=10)

        self.assertEqual([event["data"]["message"] for event in main_page["events"]], ["main reply", "next step"])
        self.assertEqual([event["data"]["message"] for event in host_page["events"]], ["main reply", "next step"])

    def test_conversation_events_page_dedupes_main_browser_mirror_before_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            append_event(
                root,
                run_id,
                "message",
                {
                    "task_id": "task-001",
                    "sender": "main",
                    "target": "host",
                    "from_agent": "main",
                    "to_agent": "host",
                    "display_sender": "main",
                    "display_target": "host",
                    "message": "same reply",
                },
            )
            append_event(
                root,
                run_id,
                "message",
                {"task_id": "task-001", "sender": "main", "target": "browser", "from_agent": "main", "to_agent": "browser", "message": "same reply"},
            )

            page = conversation_events_page(root, run_id, "task-001", "main", limit=1, categories={"chat"})

        self.assertEqual(page["count"], 1)
        self.assertEqual(page["events"][0]["data"]["display_target"], "host")
        self.assertEqual(page["events"][0]["data"]["message"], "same reply")

    def test_conversation_events_page_keeps_main_browser_without_host_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            append_event(
                root,
                run_id,
                "message",
                {"task_id": "task-001", "sender": "main", "target": "browser", "from_agent": "main", "to_agent": "browser", "message": "browser only"},
            )

            page = conversation_events_page(root, run_id, "task-001", "main", limit=1, categories={"chat"})

        self.assertEqual(page["count"], 1)
        self.assertEqual(page["events"][0]["data"]["target"], "browser")
        self.assertEqual(page["events"][0]["data"]["message"], "browser only")

    def test_conversation_events_page_keeps_main_browser_when_body_differs_from_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            append_event(
                root,
                run_id,
                "message",
                {
                    "task_id": "task-001",
                    "sender": "main",
                    "target": "host",
                    "from_agent": "main",
                    "to_agent": "host",
                    "display_sender": "main",
                    "display_target": "host",
                    "message": "host copy",
                },
            )
            append_event(
                root,
                run_id,
                "message",
                {"task_id": "task-001", "sender": "main", "target": "browser", "from_agent": "main", "to_agent": "browser", "message": "browser copy"},
            )

            page = conversation_events_page(root, run_id, "task-001", "main", limit=2, categories={"chat"})

        self.assertEqual([event["data"]["message"] for event in page["events"]], ["host copy", "browser copy"])

    def test_conversation_events_api_hides_action_envelope_agent_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Conversation action envelope", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                user_facing_response = "只展示投影后的 response"
                action_envelope = json.dumps(
                    {
                        "actions": [
                            {
                                "type": "record_task_update",
                                "summary": "raw envelope should stay out of timeline",
                                "changed_files": [],
                                "verification": [],
                                "risks": [],
                            }
                        ],
                        "response": user_facing_response,
                    },
                    ensure_ascii=False,
                )
                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": action_envelope})
                append_event(
                    root,
                    run_id,
                    "message",
                    {"task_id": "task-001", "sender": "main", "target": "browser", "message": user_facing_response},
                )

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20"))
                body = json_response_body(response)
                host_action_envelope = json.dumps(
                    {
                        "decision": "stop",
                        "reason": "host checked project state",
                        "actions": [],
                        "response": "host 视角应该保留这个决策摘要",
                    },
                    ensure_ascii=False,
                )
                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "host", "text": host_action_envelope})
                host_response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=host&limit=20"))
                host_body = json_response_body(host_response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        timeline_texts = [str(event["data"].get("text") or event["data"].get("message") or "") for event in body["events"]]
        self.assertEqual(timeline_texts, [user_facing_response])
        self.assertNotIn(action_envelope, timeline_texts)
        self.assertFalse(any('"actions"' in text and '"response"' in text for text in timeline_texts))
        self.assertTrue(host_response.startswith(b"HTTP/1.1 200 OK"))
        host_texts = [str(event["data"].get("text") or event["data"].get("message") or "") for event in host_body["events"]]
        self.assertIn(host_action_envelope, host_texts)

    def test_web_restart_api_schedules_source_ui_on_8766(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Restart web", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="scheduled\n", stderr="")
                with mock.patch("aha_cli.web.system_routes.subprocess.run", return_value=completed) as run_command:
                    response = asyncio.run(
                        fetch_ui_response(
                            root,
                            run_id,
                            "/api/web/restart",
                            method="POST",
                            payload={"host": "0.0.0.0", "port": 8766},
                        )
                    )
                body = json_response_body(response)
                rows, _ = iter_jsonl_from(event_path(root, run_id), 0)
                events = [row["type"] for row in rows]

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["host"], "0.0.0.0")
        self.assertEqual(body["port"], 8766)
        self.assertEqual(body["service_unit"], "aha-ui-source-8766.service")
        command = run_command.call_args.args[0]
        self.assertEqual(command[0], "systemd-run")
        self.assertIn("--on-active=1s", command)
        command_text = " ".join(command)
        self.assertIn(str(root), command_text)
        self.assertIn("0.0.0.0", command_text)
        self.assertIn("8766", command_text)
        self.assertIn("systemctl --user restart aha-ui-source-8766.service", command_text)
        self.assertIn("web_restart_requested", events)

    def test_conversation_events_api_restores_latest_turn_metrics_outside_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            with (
                mock.patch("pathlib.Path.cwd", return_value=root),
                mock.patch("pathlib.Path.home", return_value=home),
            ):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Conversation prompt metrics", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]

                class FakeProcess:
                    pid = 4242

                with (
                    mock.patch("aha_cli.services.backend_runtime.subprocess.Popen", return_value=FakeProcess()),
                    mock.patch("aha_cli.services.backend_runtime.pid_is_running", side_effect=lambda pid: bool(pid)),
                ):
                    start_backend(root, run_id, "main", task_id="task-001")
                session_file = session_path(root, run_id, "task-001", "main")
                session = read_json(session_file)
                session["backend_session_id"] = "codex-session-web"
                write_json(session_file, session)
                codex_session = home / ".codex" / "sessions" / "2026" / "05" / "24" / "rollout-codex-session-web.jsonl"
                codex_session.parent.mkdir(parents=True)
                codex_session.write_text(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "model_context_window": 258400,
                                    "last_token_usage": {"input_tokens": 226853, "cached_input_tokens": 226176},
                                },
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                append_event(root, run_id, "agent_started", {"task_id": "task-001", "target": "main", "sender": "browser"})
                append_event(
                    root,
                    run_id,
                    "agent_prompt_metrics",
                    {
                        "task_id": "task-001",
                        "target": "main",
                        "source": "codex-chat",
                        "total": {"tokens": 180600, "chars": 1234, "bytes": 1234, "lines": 12},
                        "components": {"status_snapshot": {"chars": 1000, "bytes": 1000, "lines": 1}},
                    },
                )
                append_event(root, run_id, "agent_thread", {"task_id": "task-001", "target": "main", "thread_id": "thread-1"})
                append_event(root, run_id, "agent_usage", {"task_id": "task-001", "target": "main", "usage": {"input_tokens": 99999999}})
                append_event(root, run_id, "agent_finished", {"task_id": "task-001", "target": "main", "exit_code": 0})
                for index in range(10):
                    append_event(
                        root,
                        run_id,
                        "agent_command_finished",
                        {"task_id": "task-001", "target": "main", "command": f"cmd-{index}", "exit_code": 0},
                    )

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=5"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertNotIn("agent_prompt_metrics", [event["type"] for event in body["events"]])
        turn_event_types = [event["type"] for event in body["turn_events"]]
        self.assertEqual(turn_event_types, ["agent_started", "agent_prompt_metrics", "agent_thread", "agent_usage", "agent_finished"])
        metrics = next(event for event in body["turn_events"] if event["type"] == "agent_prompt_metrics")
        self.assertEqual(metrics["data"]["total"]["chars"], 1234)
        pressure = body["backend_session"]["context_pressure"]
        self.assertEqual(pressure["context_window"], 258400)
        self.assertEqual(pressure["context_window_source"], "runtime")
        self.assertEqual(pressure["pressure_source"], "runtime.last_token_usage.input_tokens")
        self.assertEqual(pressure["level"], "high")
        self.assertEqual(pressure["percent"], round(226853 / 258400 * 100, 2))
        self.assertEqual(pressure["input_tokens"], 226853)
        self.assertEqual(pressure["prompt_tokens"], 180600)
        self.assertEqual(body["backend_session"]["runtime_context_usage"]["input_tokens"], 226853)
        self.assertEqual(body["backend_session"]["latest_usage"]["input_tokens"], 99999999)

    def test_conversation_events_api_filters_categories_server_side(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Conversation categories", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_message(root, run_id, "main", "hello", sender="browser", task_id="task-001", role="main")
                append_event(root, run_id, "agent_command_started", {"task_id": "task-001", "target": "main", "command": "pwd"})
                append_event(root, run_id, "agent_command_finished", {"task_id": "task-001", "target": "main", "command": "pwd", "exit_code": 0, "output_tail": "large output"})
                append_event(root, run_id, "agent_usage", {"task_id": "task-001", "target": "main", "usage": {"input_tokens": 10}})
                append_event(root, run_id, "task_status_changed", {"task_id": "task-001", "status": "running"})

                chat_response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20&categories=chat"))
                command_response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20&categories=chat,commands"))
                full_command_response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20&categories=commands&include_command_output=1"))
                none_response = asyncio.run(fetch_ui_response(root, run_id, "/api/conversation-events?task_id=task-001&target=main&limit=20&categories=none"))

        chat_body = json_response_body(chat_response)
        command_body = json_response_body(command_response)
        full_command_body = json_response_body(full_command_response)
        none_body = json_response_body(none_response)
        self.assertEqual([event["type"] for event in chat_body["events"]], ["message"])
        self.assertEqual(
            [event["type"] for event in command_body["events"]],
            ["message", "agent_command_started", "agent_command_finished"],
        )
        finished = command_body["events"][-1]["data"]
        self.assertNotIn("output_tail", finished)
        self.assertTrue(finished["output_tail_omitted"])
        self.assertEqual(finished["output_tail_chars"], len("large output"))
        self.assertEqual(full_command_body["events"][-1]["data"]["output_tail"], "large output")
        self.assertEqual(none_body["events"], [])

    def test_events_api_replays_from_saved_offset_after_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Replay events", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                initial = json_response_body(asyncio.run(fetch_ui_response(root, run_id, "/api/events?offset=-1")))
                last_event_id = initial["offset"]

                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "missed-1"})
                append_event(root, run_id, "agent_finished", {"task_id": "task-001", "target": "main", "exit_code": 0})

                first_page = json_response_body(
                    asyncio.run(fetch_ui_response(root, run_id, f"/api/events?offset={last_event_id}&limit=1"))
                )
                replay = json_response_body(asyncio.run(fetch_ui_response(root, run_id, f"/api/events?offset={last_event_id}&limit=10")))

            self.assertEqual(first_page["events"][0]["data"]["text"], "missed-1")
            self.assertTrue(first_page["has_more"])
            self.assertGreater(first_page["offset"], last_event_id)
            self.assertEqual([event["type"] for event in replay["events"]], ["agent_message", "agent_finished"])
            self.assertEqual(replay["events"][1]["data"]["exit_code"], 0)
            self.assertEqual(status_snapshot(root, run_id)["tasks"][0]["status"], "pending")

    def test_reverse_jsonl_reader_pages_by_byte_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            for index in range(5):
                append_event(root, run_id, "message", {"task_id": "task-001", "sender": "browser", "to_agent": "main", "message": f"line-{index}-" + ("x" * 40)})

            path = event_path(root, run_id)
            newest = list(iter_jsonl_reverse(path, chunk_size=32))
            older = list(iter_jsonl_reverse(path, before=newest[0][0], chunk_size=32))

        self.assertEqual(newest[0][1]["data"]["message"].split("-", 2)[:2], ["line", "4"])
        self.assertEqual(older[0][1]["data"]["message"].split("-", 2)[:2], ["line", "3"])
        self.assertGreater(newest[0][0], older[0][0])

    def test_task_log_page_tails_and_pages_by_byte_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Logs", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                task = task_snapshot(root, run_id, "task-001")["task"]
                log_path = run_dir(root, run_id) / task["log_file"]
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("\n".join(f"line-{index}" for index in range(5)) + "\n", encoding="utf-8")

                latest = task_log_page(root, run_id, "task-001", limit=2)
                older = task_log_page(root, run_id, "task-001", limit=2, before=latest["next_before_offset"])

        self.assertEqual(latest["text"], "line-3\nline-4")
        self.assertTrue(latest["has_more"])
        self.assertEqual(older["text"], "line-1\nline-2")
        self.assertTrue(older["has_more"])

    def test_task_log_page_falls_back_to_event_log_when_task_log_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Event logs", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                append_event(root, run_id, "agent_message", {"task_id": "task-001", "target": "main", "text": "first"})
                append_event(root, run_id, "agent_message", {"task_id": "task-002", "target": "main", "text": "other task"})
                append_event(root, run_id, "agent_command_finished", {"task_id": "task-001", "target": "main", "command": "pwd", "output_tail": "second"})

                latest = task_log_page(root, run_id, "task-001", limit=1)
                older = task_log_page(root, run_id, "task-001", limit=1, before=latest["next_before_offset"], source=latest["source"])

        self.assertEqual(latest["source"], "events")
        self.assertIn("agent_command_finished", latest["text"])
        self.assertIn("second", latest["text"])
        self.assertNotIn("other task", latest["text"])
        self.assertEqual(older["source"], "events")
        self.assertIn("first", older["text"])
