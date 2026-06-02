from __future__ import annotations

import json
import textwrap
import unittest

from aha_cli.services.action_payloads import (
    action_response_text,
    extract_action_payload,
    invalid_action_schema_reason,
)
from aha_cli.services.orchestrator import extract_action_payload as orchestrator_extract_action_payload


class ActionPayloadTests(unittest.TestCase):
    def test_extract_action_payload_accepts_plain_or_fenced_json_object(self) -> None:
        payload = {
            "actions": [{"type": "route_to_agent", "agent_id": "sub-001", "message": "go"}],
            "response": "sent",
        }

        self.assertEqual(extract_action_payload(json.dumps(payload)), payload)
        self.assertEqual(extract_action_payload(f"```json\n{json.dumps(payload)}\n```"), payload)

    def test_extract_action_payload_ignores_embedded_json_examples(self) -> None:
        reply = textwrap.dedent(
            """\
            Use this shape:

            ```json
            {"actions":[{"type":"route_to_agent","agent_id":"...","message":"..."}],"response":"..."}
            ```
            """
        ).strip()

        self.assertIsNone(extract_action_payload(reply))
        self.assertEqual(action_response_text(reply), reply)

    def test_invalid_action_schema_reason_rejects_legacy_top_level_actions(self) -> None:
        self.assertEqual(
            invalid_action_schema_reason({"action": "route_to_agent"}),
            "top-level action is not supported; use actions array",
        )
        self.assertEqual(
            invalid_action_schema_reason({"type": "route_to_agent"}),
            "top-level type is not supported; use actions array",
        )
        self.assertEqual(
            invalid_action_schema_reason({"actions": [{"type": "unknown"}]}),
            "unknown action type: unknown",
        )

    def test_action_response_text_keeps_orchestrator_re_export_compatible(self) -> None:
        reply = json.dumps({"actions": [], "response": "  done  "})

        self.assertEqual(action_response_text(reply), "done")
        self.assertEqual(orchestrator_extract_action_payload(reply), extract_action_payload(reply))


if __name__ == "__main__":
    unittest.main()
