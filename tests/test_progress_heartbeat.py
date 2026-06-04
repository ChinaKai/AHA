from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from aha_cli.services.progress_heartbeat import AgentProgressHeartbeat
from aha_cli.store.filesystem import inbox_path, iter_jsonl_from


class ProgressHeartbeatTests(unittest.TestCase):
    def test_elapsed_threshold_can_emit_heartbeat_without_text_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            clock = [0.0]
            heartbeat = AgentProgressHeartbeat(
                root,
                run_id,
                task_id="task-001",
                agent_id="main",
                role="main",
                model_family="kimi",
                now=lambda: clock[0],
                seconds_threshold=60.0,
            )

            clock[0] = 61.0
            heartbeat.handle_event("agent_command_finished", {"output_tail": "ok"})
            browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)

        self.assertEqual(len(browser_messages), 1)
        self.assertEqual(browser_messages[0]["coordination"], "agent_progress_heartbeat")
        self.assertIn("本轮已持续较久", browser_messages[0]["message"])

    def test_commit_command_can_emit_commit_phase_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            heartbeat = AgentProgressHeartbeat(
                root,
                run_id,
                task_id="task-001",
                agent_id="main",
                role="main",
                model_family="minimax",
            )

            heartbeat.handle_event("agent_command_started", {"tool_name": "Bash", "command": "aha commit --type feat --scope ui"})
            browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)

        self.assertEqual(len(browser_messages), 1)
        self.assertEqual(browser_messages[0]["coordination"], "agent_progress_heartbeat")
        self.assertIn("正在进入提交阶段", browser_messages[0]["message"])
        self.assertIn("aha commit --type feat --scope ui", browser_messages[0]["message"])

    def test_session_tool_use_shape_can_emit_test_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-001"
            heartbeat = AgentProgressHeartbeat(
                root,
                run_id,
                task_id="task-001",
                agent_id="main",
                role="main",
                model_family="kimi",
            )

            heartbeat.handle_event(
                "tool_use",
                {"name": "Bash", "input": {"command": "python3 -m unittest tests.test_cli_core"}},
            )
            heartbeat.handle_event("toolUseResult", {"durationMs": 1200})
            browser_messages, _ = iter_jsonl_from(inbox_path(root, run_id, "browser"), 0)

        self.assertEqual(len(browser_messages), 1)
        self.assertEqual(browser_messages[0]["coordination"], "agent_progress_heartbeat")
        self.assertIn("正在进入测试/验证阶段", browser_messages[0]["message"])
        self.assertIn("python3 -m unittest tests.test_cli_core", browser_messages[0]["message"])
