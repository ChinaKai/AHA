from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from urllib.parse import quote

from aha_cli.services.task_skills import discover_task_skill_options
from tests.helpers import fetch_ui_response, json_response_body


def _make_git_workspace(path: Path, remote: str = "git@github.com:user/aha.git") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git_dir = path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        f'[core]\n[remote "origin"]\n\turl = {remote}\n',
        encoding="utf-8",
    )
    return path


class SkillRoutesTests(unittest.TestCase):
    def test_skill_api_manages_knowledge_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            workspace = _make_git_workspace(Path(tmp) / "workspace")
            target = f"?workspace_path={quote(str(workspace))}"
            skills_root = root / "knowledge" / "skills"

            empty_response = asyncio.run(fetch_ui_response(root, "", f"/api/skills{target}"))
            empty_body = json_response_body(empty_response)
            self.assertTrue(empty_response.startswith(b"HTTP/1.1 200 OK"))
            self.assertEqual(empty_body["skills"], [])
            self.assertEqual(empty_body["skills_root"], str(skills_root))
            self.assertEqual(empty_body["legacy_skills_root"], str(root / "skills"))

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
                    payload={
                        "id": "board-debug",
                        "skill_md": skill_md,
                        "openai_yaml": openai_yaml,
                        "workspace_path": str(workspace),
                    },
                )
            )
            create_body = json_response_body(create_response)
            self.assertTrue(create_response.startswith(b"HTTP/1.1 201 Created"))
            self.assertEqual(create_body["skill"]["id"], "board-debug")
            self.assertEqual(create_body["skill"]["label"], "Board Debug UI")
            self.assertEqual(create_body["skill"]["short_description"], "Board debug helpers")
            self.assertEqual(create_body["skill"]["source"], "knowledge")
            self.assertEqual((skills_root / "board-debug" / "SKILL.md").read_text(encoding="utf-8"), skill_md)
            self.assertEqual(
                (skills_root / "board-debug" / "agents" / "openai.yaml").read_text(encoding="utf-8"),
                openai_yaml,
            )

            list_response = asyncio.run(fetch_ui_response(root, "", f"/api/skills{target}"))
            list_body = json_response_body(list_response)
            self.assertEqual([item["id"] for item in list_body["skills"]], ["board-debug"])
            self.assertEqual(list_body["skills"][0]["description"], "Board debug workflow.")

            detail_response = asyncio.run(fetch_ui_response(root, "", f"/api/skills/board-debug{target}"))
            detail_body = json_response_body(detail_response)
            self.assertEqual(detail_body["skill"]["skill_md"], skill_md)
            self.assertEqual(detail_body["skill"]["openai_yaml"], openai_yaml)
            self.assertEqual(detail_body["skill"]["bundled_files"], [])

            updated_md = skill_md.replace("Use UART safely.", "Use UART and Telnet safely.")
            update_response = asyncio.run(
                fetch_ui_response(
                    root,
                    "",
                    "/api/skills/board-debug",
                    method="PUT",
                    payload={"skill_md": updated_md, "openai_yaml": "", "workspace_path": str(workspace)},
                )
            )
            update_body = json_response_body(update_response)
            self.assertTrue(update_response.startswith(b"HTTP/1.1 200 OK"))
            self.assertEqual(update_body["skill"]["skill_md"], updated_md)
            self.assertFalse((skills_root / "board-debug" / "agents" / "openai.yaml").exists())

            delete_response = asyncio.run(
                fetch_ui_response(root, "", f"/api/skills/board-debug{target}", method="DELETE")
            )
            delete_body = json_response_body(delete_response)
            self.assertTrue(delete_response.startswith(b"HTTP/1.1 200 OK"))
            self.assertEqual(delete_body["deleted"], "board-debug")
            self.assertFalse((skills_root / "board-debug").exists())

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

    def test_skill_api_migrates_legacy_home_skills_to_knowledge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            workspace = _make_git_workspace(Path(tmp) / "workspace")
            target = f"?workspace_path={quote(str(workspace))}"
            legacy_skill = root / "skills" / "board-debug"
            legacy_skill.mkdir(parents=True)
            (legacy_skill / "SKILL.md").write_text("# Legacy Board Debug\n\nUse UART safely.\n", encoding="utf-8")
            (legacy_skill / "references").mkdir()
            (legacy_skill / "references" / "checklist.md").write_text("- power\n", encoding="utf-8")

            response = asyncio.run(fetch_ui_response(root, "", f"/api/skills{target}"))
            body = json_response_body(response)
            migrated = root / "knowledge" / "skills" / "board-debug"
            migrated_skill_md = (migrated / "SKILL.md").read_text(encoding="utf-8")
            migrated_checklist = (migrated / "references" / "checklist.md").read_text(encoding="utf-8")

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual([item["id"] for item in body["skills"]], ["board-debug"])
        self.assertEqual(body["skills"][0]["source"], "knowledge")
        self.assertEqual(body["skills"][0]["path"], str(migrated / "SKILL.md"))
        self.assertEqual(migrated_skill_md, "# Legacy Board Debug\n\nUse UART safely.\n")
        self.assertEqual(migrated_checklist, "- power\n")

    def test_skill_detail_lists_bundled_tools_without_following_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            skill = root / "knowledge" / "skills" / "board-debug"
            (skill / "scripts").mkdir(parents=True)
            (skill / "SKILL.md").write_text("# Board Debug\n", encoding="utf-8")
            script = skill / "scripts" / "probe.py"
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            script.chmod(0o755)
            tool = skill / "relay_control"
            tool.write_bytes(b"tool")
            tool.chmod(0o755)
            (skill / "references").mkdir()
            (skill / "references" / "notes.md").write_text("notes\n", encoding="utf-8")
            outside = Path(tmp) / "secret.txt"
            outside.write_text("secret\n", encoding="utf-8")
            (skill / "leak").symlink_to(outside)

            response = asyncio.run(fetch_ui_response(root, "", "/api/skills/board-debug"))
            body = json_response_body(response)
            files = body["skill"]["bundled_files"]

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(
            [(item["path"], item["kind"], item["executable"]) for item in files],
            [
                ("references/notes.md", "reference", False),
                ("relay_control", "tool", True),
                ("scripts/probe.py", "script", True),
            ],
        )
        self.assertEqual(body["skill"]["bundled_file_count"], 3)
        self.assertEqual(body["skill"]["tool_count"], 2)

    def test_task_skill_options_use_knowledge_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            root.mkdir()
            workspace = _make_git_workspace(Path(tmp) / "workspace")
            legacy_skill = root / "skills" / "scope-debug"
            legacy_skill.mkdir(parents=True)
            (legacy_skill / "SKILL.md").write_text("# Scope Debug\n\nUse scope probes.\n", encoding="utf-8")

            options = discover_task_skill_options(root, workspace)
            migrated = root / "knowledge" / "skills" / "scope-debug"

        self.assertEqual(options, [
            {
                "id": "scope-debug",
                "label": "Scope Debug",
                "path": str(migrated / "SKILL.md"),
                "source": "knowledge",
            }
        ])
