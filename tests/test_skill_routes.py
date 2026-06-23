from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from tests.helpers import fetch_ui_response, json_response_body


class SkillRoutesTests(unittest.TestCase):
    def test_skill_api_manages_aha_home_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()

            empty_response = asyncio.run(fetch_ui_response(root, "", "/api/skills"))
            empty_body = json_response_body(empty_response)
            self.assertTrue(empty_response.startswith(b"HTTP/1.1 200 OK"))
            self.assertEqual(empty_body["skills"], [])
            self.assertEqual(empty_body["skills_root"], str(root / "skills"))

            skill_md = "\n".join(
                [
                    "---",
                    "name: board-debug",
                    "description: Board debug workflow.",
                    "---",
                    "",
                    "# Board Debug",
                    "",
                    "Use UART safely.",
                    "",
                ]
            )
            openai_yaml = "\n".join(
                [
                    "interface:",
                    '  display_name: "Board Debug UI"',
                    '  short_description: "Board debug helpers"',
                    '  default_prompt: "Use $board-debug to inspect a board."',
                    "",
                ]
            )
            create_response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/skills",
                    method="POST",
                    payload={"id": "board-debug", "skill_md": skill_md, "openai_yaml": openai_yaml},
                )
            )
            create_body = json_response_body(create_response)
            self.assertTrue(create_response.startswith(b"HTTP/1.1 201 Created"))
            self.assertEqual(create_body["skill"]["id"], "board-debug")
            self.assertEqual(create_body["skill"]["label"], "Board Debug UI")
            self.assertEqual(create_body["skill"]["short_description"], "Board debug helpers")
            self.assertEqual((root / "skills" / "board-debug" / "SKILL.md").read_text(encoding="utf-8"), skill_md)
            self.assertEqual(
                (root / "skills" / "board-debug" / "agents" / "openai.yaml").read_text(encoding="utf-8"),
                openai_yaml,
            )

            list_response = asyncio.run(fetch_ui_response(root, "", "/api/skills"))
            list_body = json_response_body(list_response)
            self.assertEqual([item["id"] for item in list_body["skills"]], ["board-debug"])
            self.assertEqual(list_body["skills"][0]["description"], "Board debug workflow.")

            detail_response = asyncio.run(fetch_ui_response(root, "", "/api/skills/board-debug"))
            detail_body = json_response_body(detail_response)
            self.assertEqual(detail_body["skill"]["skill_md"], skill_md)
            self.assertEqual(detail_body["skill"]["openai_yaml"], openai_yaml)

            updated_md = skill_md.replace("Use UART safely.", "Use UART and Telnet safely.")
            update_response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/skills/board-debug",
                    method="PUT",
                    payload={"skill_md": updated_md, "openai_yaml": ""},
                )
            )
            update_body = json_response_body(update_response)
            self.assertTrue(update_response.startswith(b"HTTP/1.1 200 OK"))
            self.assertEqual(update_body["skill"]["skill_md"], updated_md)
            self.assertFalse((root / "skills" / "board-debug" / "agents" / "openai.yaml").exists())

            delete_response = asyncio.run(fetch_ui_response(root, "", "/api/skills/board-debug", method="DELETE"))
            delete_body = json_response_body(delete_response)
            self.assertTrue(delete_response.startswith(b"HTTP/1.1 200 OK"))
            self.assertEqual(delete_body["deleted"], "board-debug")
            self.assertFalse((root / "skills" / "board-debug").exists())

    def test_skill_api_rejects_invalid_skill_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()

            response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/skills",
                    method="POST",
                    payload={"id": "../escape", "skill_md": "# Bad\n"},
                )
            )
            body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertIn("skill id", body["error"])
