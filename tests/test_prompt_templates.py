from __future__ import annotations

from importlib import resources
from pathlib import Path
from string import Template
import unittest

from aha_cli.services.prompt_templates import render_prompt_template


REPO_ROOT = Path(__file__).resolve().parents[1]

SAMPLE_VALUES = {
    "AHA_EVENTS_FILE": "events.jsonl",
    "AHA_INBOX_FILE": "inbox.jsonl",
    "AHA_OUTPUT_FILE": "output.md",
    "action_retry_schema": '{"actions":[],"response":"ok"}',
    "action_contract": "AHA action output.",
    "agent_id": "main",
    "agent_context": "Agent context.",
    "agent_metadata": "- agent_id: main",
    "agents": '[{"id":"main"}]',
    "approval": "never",
    "ask_user_gate_policy": "- scope_change: host may decide",
    "assigned_prompt": "Do the task.",
    "attachment_dir": "attachments",
    "backend": "codex",
    "backend_session_id": "session-1",
    "browser_latest_request": "Please check this.",
    "browser_to_host_notes": "(none)",
    "channels": "- channel 1: type=uart",
    "collaboration_guidance": "Auto: choose the fastest execution path.",
    "collaboration_mode": "auto",
    "command": "/status",
    "commit_message_policy": "Use a Conventional Commit message.",
    "commit_policy": "Commit policy reminder.",
    "compact_summary": "Compact summary.",
    "coordination_policy": "Coordination policy.",
    "created_at": "2026-01-01T00:00:00+00:00",
    "current_agent": '{"id":"main"}',
    "current_round_id": "round-001",
    "delegated_contract": "Delegated browser control plane contract.",
    "delegation_policy": "auto",
    "enabled_channel_count": "1",
    "enabled_paths": "  - /tmp/SKILL.md",
    "error_text": "missing Generated-by",
    "expected_generated_by": "AHA Codex GPT-5",
    "final_context": "Final source range.",
    "generated_by": "AHA Codex GPT-5",
    "goal": "Build AHA",
    "hardware_debug_context": "Hardware debug context.",
    "image_manifest": "",
    "images": "- a.png (image/png, 10 bytes, path: a.png)",
    "inbox_file": "inbox.jsonl",
    "inbox_preview": "(empty)",
    "jsonl_exists": "True",
    "jsonl_path": "session.jsonl",
    "knowledge_context": "Known facts.",
    "last_final_round_id": "-",
    "latest_prompt_mode": "full",
    "latest_usage": "{}",
    "main_latest_reply": "Done.",
    "max_sub_agents": "2",
    "memo_completed_at": "2026-01-01T00:00:00+00:00",
    "memo_description": "memo body",
    "memo_id": "memo-001",
    "memo_status": "completed",
    "memo_title": "Memo",
    "message": "hello",
    "mode": "research",
    "mode_instruction": "Reply directly.",
    "model": "gpt-5",
    "model_family": "kimi",
    "mutability": "Read-only research: do not modify files.",
    "original_command": "/status",
    "original_request": "Original request.",
    "output_file": "output.md",
    "preferred_sub_backend": "codex",
    "preferred_sub_model": "default",
    "prefix": "AHA prompt prefix.",
    "project_key_value": "project-key",
    "raw_note": "raw note",
    "reason": "invalid schema",
    "recent_conversation": "(none)",
    "recent_events": "- event",
    "recent_messages": "- message",
    "recovery_context": "",
    "requested_at": "2026-01-01T00:00:00+00:00",
    "role": "main",
    "round_sequence": "1",
    "run_goal": "Run goal.",
    "run_id": "run-001",
    "sandbox": "workspace-write",
    "scan_json": "{}",
    "sender": "browser",
    "session_policy": "sticky",
    "size_bytes": "10",
    "scope_hint": "personal",
    "source": "browser_main_reply",
    "status": "running",
    "steward_handoffs": "(none)",
    "sticky_context": "Sticky context.",
    "target": "main",
    "task_context": "Task context.",
    "task_description": "Task description.",
    "task_id": "task-001",
    "task_journal": "Task journal.",
    "task_skills_context": "Task skills context.",
    "task_state": '{"id":"task-001"}',
    "task_title": "Task title",
    "title": "Task title",
    "ts": "2026-01-01T00:00:00+00:00",
    "workflow_guidance": "Use auto workflow.",
    "workflow_template": "auto",
    "workspace": "/workspace",
    "workspace_path": "/workspace",
    "write_scope": "- src/",
}


def _prompt_template_names() -> list[str]:
    return sorted(
        item.name
        for item in resources.files("aha_cli.prompts").iterdir()
        if item.name.endswith(".md")
    )


def _placeholders(text: str) -> set[str]:
    names: set[str] = set()
    invalid: list[str] = []
    for match in Template.pattern.finditer(text):
        name = match.group("named") or match.group("braced")
        if name:
            names.add(name)
        elif match.group("invalid") is not None:
            invalid.append(match.group(0))
    if invalid:
        raise AssertionError(f"invalid Template placeholders: {invalid}")
    return names


class PromptTemplateTests(unittest.TestCase):
    def test_all_bundled_prompt_templates_render_with_sample_values(self) -> None:
        prompts = resources.files("aha_cli.prompts")
        for name in _prompt_template_names():
            with self.subTest(template=name):
                text = prompts.joinpath(name).read_text(encoding="utf-8")
                missing = _placeholders(text) - set(SAMPLE_VALUES)
                self.assertEqual(missing, set())
                rendered = render_prompt_template(name, **SAMPLE_VALUES)
                self.assertTrue(rendered.strip())

    def test_migrated_prompt_bodies_are_not_hardcoded_in_python_sources(self) -> None:
        denylist = {
            "src/aha_cli/services/chat.py": [
                "AHA runtime rejected your previous reply before executing actions.",
                "Before normal completion, return exactly one AHA JSON object",
            ],
            "src/aha_cli/services/chat_prompt_context.py": [
                "Before another patch or answer, re-read the relevant code/logs/tests",
            ],
            "src/aha_cli/services/chat_supervision.py": [
                "AHA host instructions:",
                "Delegated browser control plane contract:",
                "Use your read-only project access",
                "AHA host sticky summary:",
            ],
            "src/aha_cli/services/hardware_debug.py": [
                "Hardware debug operating rules:",
                "A UART/serial port is a continuous stream",
            ],
            "src/aha_cli/services/knowledge_capture_distill.py": [
                "You are organizing a user's raw, messy note",
                "Reply with a short human summary, then exactly one machine-readable block",
            ],
            "src/aha_cli/services/knowledge_navigation.py": [
                "You are generating the initial AHA project navigation",
                "Return ONLY valid JSON. The top-level value must be an array of candidates.",
            ],
            "src/aha_cli/services/task_skills.py": [
                "Task skill operating rules:",
                "read its SKILL.md before acting",
            ],
        }
        for relpath, forbidden_phrases in denylist.items():
            with self.subTest(path=relpath):
                text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
                for phrase in forbidden_phrases:
                    self.assertNotIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
