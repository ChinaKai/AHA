from __future__ import annotations

import unittest

from aha_cli.services.chat_supervision import (
    agents_visible_to_prompt,
    is_task_supervision_host_agent,
    parse_supervision_host_decision,
    prompt_event_visible_to_target,
    task_supervision_host_id,
)


class ChatSupervisionTests(unittest.TestCase):
    def test_supervision_host_identity_and_visible_agents(self) -> None:
        task = {
            "supervision": {"mode": "assisted", "host_backend": "codex", "real_agent_enabled": True, "host_agent_id": "host"},
            "agents": [
                {"id": "main", "role": "task-main"},
                {"id": "host", "role": "host", "created_by": "supervision"},
            ],
        }

        self.assertEqual(task_supervision_host_id(task), "host")
        self.assertTrue(is_task_supervision_host_agent(task, "host"))
        self.assertEqual([agent["id"] for agent in agents_visible_to_prompt(task, "main")], ["main"])
        self.assertEqual([agent["id"] for agent in agents_visible_to_prompt(task, "host")], ["main", "host"])

    def test_prompt_event_visibility_hides_host_in_main_prompt(self) -> None:
        task = {"supervision": {"mode": "assisted", "host_backend": "codex", "real_agent_enabled": True}}

        self.assertFalse(
            prompt_event_visible_to_target(
                {"type": "message", "data": {"sender": "host", "message": "supervision host note", "target": "main"}},
                "main",
                task,
            )
        )
        self.assertTrue(prompt_event_visible_to_target({"type": "message", "data": {"sender": "browser", "target": "main"}}, "host", task))

    def test_parse_supervision_host_decision_defaults_invalid_reply_to_user(self) -> None:
        invalid = parse_supervision_host_decision("plain reply")
        valid = parse_supervision_host_decision(
            '{"decision":"continue","reason":"next","response":"继续处理","actions":[]}'
        )

        self.assertEqual(invalid["decision"], "ask_user")
        self.assertEqual(invalid["response"], "plain reply")
        self.assertEqual(valid["decision"], "continue")
        self.assertEqual(valid["response"], "继续处理")


if __name__ == "__main__":
    unittest.main()
