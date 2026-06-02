from __future__ import annotations

import unittest

from aha_cli.services.orchestrator import task_has_incomplete_sub_agents as orchestrator_task_has_incomplete_sub_agents
from aha_cli.services.subagent_state import (
    active_sub_agent_count,
    current_round_sub_agents,
    pending_current_round_sub_agents,
    pending_sub_agents,
    sub_agents,
    task_has_incomplete_sub_agents,
    waiting_for_subagents_message,
)


class SubagentStateTests(unittest.TestCase):
    def test_subagent_state_filters_to_current_round_activity(self) -> None:
        task = {
            "coordination": {"followup_started_at": "2026-05-30T10:00:00+00:00"},
            "agents": [
                {"id": "main", "role": "task-main", "status": "running"},
                {"id": "sub-old", "role": "sub", "status": "running", "last_active_at": "2026-05-30T09:59:00+00:00"},
                {"id": "sub-done", "role": "sub", "status": "completed", "finished_at": "2026-05-30T10:01:00+00:00"},
                {"id": "sub-active", "role": "sub", "status": "running", "status_started_at": "2026-05-30T10:02:00+00:00"},
            ],
        }

        self.assertEqual([agent["id"] for agent in sub_agents(task)], ["sub-old", "sub-done", "sub-active"])
        self.assertEqual([agent["id"] for agent in current_round_sub_agents(task)], ["sub-done", "sub-active"])
        self.assertEqual([agent["id"] for agent in pending_sub_agents(task)], ["sub-old", "sub-active"])
        self.assertEqual([agent["id"] for agent in pending_current_round_sub_agents(task)], ["sub-active"])
        self.assertEqual(active_sub_agent_count(task), 2)
        self.assertTrue(task_has_incomplete_sub_agents(task))
        self.assertEqual(
            waiting_for_subagents_message(task),
            "等待子 agent 完成：sub-active。当前进度 1/2。",
        )

    def test_completed_current_round_reports_waiting_for_main_summary(self) -> None:
        task = {
            "agents": [
                {"id": "sub-001", "role": "sub", "status": "completed"},
                {"id": "sub-002", "role": "sub", "status": "stopped"},
            ],
        }

        self.assertFalse(task_has_incomplete_sub_agents(task))
        self.assertFalse(orchestrator_task_has_incomplete_sub_agents(task))
        self.assertEqual(
            waiting_for_subagents_message(task),
            "所有子 agent 已完成，等待 task-main 做本轮汇总。",
        )


if __name__ == "__main__":
    unittest.main()
