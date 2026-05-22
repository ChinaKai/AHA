from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from aha_cli.backends.claude import build_claude_exec_command, claude_permission_mode, handle_claude_event
from aha_cli.backends.codex import build_codex_exec_command, handle_codex_event, is_context_overflow_message, run_codex_exec
from aha_cli.cli import append_message, main, task_dashboard_html, task_snapshot
from aha_cli.services.commit_policy import format_commit_message, validate_commit_message
from aha_cli.services.chat import apply_supervision_host_decision, chat_offset_path, chat_prompt, chat_prompt_with_metrics, load_chat_offset, save_chat_offset
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.services.messages import format_event
from aha_cli.services.session_compact import compact_reset_backend_session
from aha_cli.services.orchestrator import task_assignment_prompt
from aha_cli.store.filesystem import (
    add_agent,
    append_jsonl,
    append_event,
    complete_task,
    conversation_events_page,
    delete_task,
    event_path,
    inbox_path,
    iter_jsonl_from,
    iter_jsonl_reverse,
    list_task_lifecycle_rounds,
    list_task_rounds,
    read_json,
    run_dir,
    mark_task_coordination,
    reopen_task,
    set_agent_status,
    set_task_hidden,
    set_task_status,
    status_snapshot,
    task_context_snapshot,
    task_final_snapshot,
    task_log_page,
    update_task_proxy_config,
    update_task_supervision_config,
    update_agent_config,
    update_agent_runtime,
    write_task_result,
)
from aha_cli.web.server import (
    backend_session_jsonl_info,
    finalization_prompt,
    format_agent_command,
    format_aha_command,
    handle_slash_command,
    workspace_options,
)
from tests.helpers import (
    fetch_ui_response,
    json_response_body,
)


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = main(list(args))
        return code, out.getvalue()

    def test_task_action_resume_alias_reopens_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.cwd", return_value=root):
                self.run_cli("init", "--portable", "--backend", "codex")
                code, plan_output = self.run_cli("plan", "Resume alias", "--agents", "1")
                self.assertEqual(code, 0)
                run_id = plan_output.splitlines()[0].split(": ", 1)[1]
                complete_task(root, run_id, "task-001", 0)

                response = asyncio.run(fetch_ui_response(root, run_id, "/api/task/task-001/resume", method="POST"))
                body = json_response_body(response)

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["task"]["status"], "awaiting_user")
