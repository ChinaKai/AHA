from __future__ import annotations

import unittest

from aha_cli.services.routing import (
    ROUTE_TO_AGENT_SKIP_REASON,
    route_to_agent_request,
    route_to_agent_result,
    route_to_agent_routed_event,
    route_to_agent_skip_event,
)


class RoutingActionTests(unittest.TestCase):
    def test_route_to_agent_request_resolves_target_agent_and_payload_aliases(self) -> None:
        sub = {"id": "sub-001", "role": "sub", "backend": "codex"}
        task = {"agents": [{"id": "main", "role": "task-main"}, sub]}
        request = route_to_agent_request(
            task,
            {
                "type": "route_to_agent",
                "target": "sub-001",
                "prompt": "  check this  ",
                "reason": "parallel verification",
            },
        )

        self.assertTrue(request["ok"])
        self.assertEqual(request["target"], "sub-001")
        self.assertEqual(request["message"], "check this")
        self.assertIs(request["agent"], sub)
        self.assertEqual(
            route_to_agent_routed_event("task-001", request),
            {
                "task_id": "task-001",
                "target": "sub-001",
                "reason": "parallel verification",
                "chars": len("check this"),
            },
        )
        self.assertEqual(route_to_agent_result(request), {"type": "route_to_agent", "agent": sub})

    def test_route_to_agent_request_rejects_missing_target_message_or_main(self) -> None:
        task = {"agents": [{"id": "main", "role": "task-main"}, {"id": "sub-001", "role": "sub"}]}

        for action in (
            {"type": "route_to_agent", "agent_id": "sub-001", "message": ""},
            {"type": "route_to_agent", "agent_id": "missing", "message": "go"},
            {"type": "route_to_agent", "agent_id": "main", "message": "go"},
        ):
            request = route_to_agent_request(task, action)
            self.assertFalse(request["ok"])
            self.assertEqual(request["reason"], ROUTE_TO_AGENT_SKIP_REASON)
            self.assertEqual(route_to_agent_skip_event("task-001", request)["reason"], ROUTE_TO_AGENT_SKIP_REASON)


if __name__ == "__main__":
    unittest.main()
