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
    "asset_dir": "task_memo_assets",
    "attachment_dir": "attachments",
    "attachment_output_guidance": "AHA conversation image output.",
    "backend": "codex",
    "backend_session_id": "session-1",
    "body": "note body",
    "browser_latest_request": "Please check this.",
    "browser_to_host_notes": "(none)",
    "channels": "- channel 1: type=uart",
    "chars": "123",
    "chain_count": "1",
    "chains": "Chain 1:\n- message",
    "collaboration_guidance": "Auto: choose the fastest execution path.",
    "collaboration_mode": "auto",
    "command": "/status",
    "commit_message_policy": "Use a Conventional Commit message.",
    "commit_policy": "Commit policy reminder.",
    "compact_summary": "Compact summary.",
    "context": "Recovered after backend restart.",
    "sections": "Updated AHA context.",
    "coordination_policy": "Coordination policy.",
    "created_at": "2026-01-01T00:00:00+00:00",
    "current_agent": '{"id":"main"}',
    "current_round_id": "round-001",
    "delegated_contract": "Delegated browser control plane contract.",
    "delegation_policy": "auto",
    "distill_mode_label": "整理",
    "distill_mode_rules": "- Do not add facts.",
    "distill_mode_summary": "Organize the note.",
    "enabled_channel_count": "1",
    "enabled_paths": "  - /tmp/SKILL.md",
    "error_text": "missing Generated-by",
    "expected_generated_by": "AHA Codex GPT-5",
    "field_name": "round_id",
    "final_context": "Final source range.",
    "from_at": "2026-01-01T00:00:00+00:00",
    "generated_by": "AHA Codex GPT-5",
    "goal": "Build AHA",
    "hardware_debug_context": "Hardware debug context.",
    "image_manifest": "",
    "image_refs": "- image: task_memo_assets/ab/shot.png",
    "images": "- a.png (image/png, 10 bytes, path: a.png)",
    "index": "1",
    "inbox_file": "inbox.jsonl",
    "inbox_preview": "(empty)",
    "instruction": "Save the Bluetooth provisioning flow to the knowledge base.",
    "items": "1. Round summary",
    "jsonl_exists": "True",
    "jsonl_path": "session.jsonl",
    "journal_count": "1",
    "journal_ids": "journal-001",
    "knowledge_context": "Known facts.",
    "knowledge_enabled": "true",
    "knowledge_feedback_context": "Knowledge/nav feedback context.",
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
    "messages": "- message",
    "metadata": "   - round_id: round-001",
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
    "project_nav_enabled": "true",
    "project_nav_index_exists": "true",
    "raw_note": "raw note",
    "reason": "invalid schema",
    "recent_conversation": "(none)",
    "recent_events": "- event",
    "recent_messages": "- message",
    "recipient": "main",
    "recovery_context": "",
    "relpath": "tasks/task-001/compacts/main.md",
    "requested_at": "2026-01-01T00:00:00+00:00",
    "role": "main",
    "round_id": "round-001",
    "round_ids": "round-001",
    "rounds": "- round-001 [main_turn] Done",
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
    "summary": "Round summary.",
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
    "to_at": "2026-01-01T00:00:00+00:00",
    "trigger": "main_turn",
    "ts": "2026-01-01T00:00:00+00:00",
    "value": "round-001",
    "visible_agents": '[{"id":"main"}]',
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
                '{"actions":[{"type":"record_task_update"',
            ],
            "src/aha_cli/services/chat_prompt_context.py": [
                "Before another patch or answer, re-read the relevant code/logs/tests",
                "AHA recovery context for this backend turn:",
                "Recent conversation chains",
                "Backend compact summary",
                "Current task context: none",
                "omitted for finalization",
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
            "src/aha_cli/domain/models.py": [
                "You may edit only the declared write scope.",
                "Read-only research: do not modify files.",
            ],
            "src/aha_cli/domain/workflow_templates.py": [
                "Fault debug: separate crash/log analysis",
                "Feature: keep main responsible for design",
            ],
            "src/aha_cli/services/action_payloads.py": [
                "Invalid AHA action schema:",
            ],
            "src/aha_cli/web/task_command_format.py": [
                "Task journal (chronological ordered list):",
                "Final source range:",
            ],
        }
        for relpath, forbidden_phrases in denylist.items():
            with self.subTest(path=relpath):
                text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
                for phrase in forbidden_phrases:
                    self.assertNotIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
