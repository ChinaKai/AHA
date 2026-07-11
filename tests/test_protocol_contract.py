from __future__ import annotations

import json
import re
from pathlib import Path
import unittest

from aha_cli.services.action_payloads import AHA_ACTION_TYPES, extract_action_payload, invalid_action_schema_reason

REPO_ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def json_blocks(markdown: str) -> list[dict]:
    blocks: list[dict] = []
    for match in re.finditer(r"```json\s*(.*?)\s*```", markdown, flags=re.DOTALL):
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            blocks.append(value)
    return blocks


def protocol_action_payload() -> dict:
    for block in json_blocks(read_repo_file("docs/protocol.md")):
        actions = block.get("actions")
        if isinstance(actions, list) and any(action.get("type") == "spawn_sub" for action in actions if isinstance(action, dict)):
            return block
    raise AssertionError("docs/protocol.md is missing the canonical action payload example")


class ProtocolContractTests(unittest.TestCase):
    def test_protocol_action_example_matches_supported_action_contract(self) -> None:
        payload = protocol_action_payload()
        actions = payload["actions"]
        action_types = {action["type"] for action in actions}

        self.assertEqual(action_types, AHA_ACTION_TYPES)
        self.assertIsNone(invalid_action_schema_reason(payload))
        self.assertEqual(extract_action_payload(json.dumps(payload)), payload)

        spawn = next(action for action in actions if action["type"] == "spawn_sub")
        for field in ("agent_id", "scope_id", "title", "backend", "model", "sandbox", "approval", "main_followup", "reason"):
            self.assertIn(field, spawn)
        self.assertIsNone(spawn["agent_id"])

        route = next(action for action in actions if action["type"] == "route_to_agent")
        for field in ("agent_id", "message", "main_followup", "reason"):
            self.assertIn(field, route)

        update = next(action for action in actions if action["type"] == "record_task_update")
        for field in ("summary", "changed_files", "verification", "risks"):
            self.assertIn(field, update)

    def test_prompt_templates_and_protocol_document_spawn_reassign_fields(self) -> None:
        for path in (
            "docs/protocol.md",
            "src/aha_cli/prompts/task_assignment.md",
            "src/aha_cli/prompts/backend_action_contract.md",
        ):
            text = read_repo_file(path)
            self.assertIn('"type": "spawn_sub"', text, path)
            self.assertIn("agent_id", text, path)
            self.assertIn("scope_id", text, path)
            self.assertIn("main_followup", text, path)
            self.assertIn("For a brand-new sub-agent", text, path)


if __name__ == "__main__":
    unittest.main()
