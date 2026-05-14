from __future__ import annotations

import os
from pathlib import Path
import textwrap

from aha_cli.backends.codex import codex_sandbox, run_codex_exec
from aha_cli.store.filesystem import append_event_to_file, ensure_session, save_session


def run_codex_task(args) -> int:
    required = [
        "AHA_ROOT",
        "AHA_RUN_ID",
        "AHA_TASK_ID",
        "AHA_PROMPT_FILE",
        "AHA_OUTPUT_FILE",
        "AHA_EVENTS_FILE",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"codex-runner missing environment variables: {', '.join(missing)}")

    root = Path(os.environ["AHA_ROOT"]).resolve()
    run_id = os.environ["AHA_RUN_ID"]
    task_id = os.environ["AHA_TASK_ID"]
    mode = os.environ.get("AHA_MODE", "research")
    prompt_file = Path(os.environ["AHA_PROMPT_FILE"])
    output_file = Path(os.environ["AHA_OUTPUT_FILE"])
    inbox_file = Path(os.environ.get("AHA_INBOX_FILE", ""))
    events_file = Path(os.environ["AHA_EVENTS_FILE"])
    sandbox = codex_sandbox(mode, args.sandbox)
    session = ensure_session(root, run_id, task_id, "main", "codex")

    inbox_preview = ""
    if inbox_file.exists() and inbox_file.stat().st_size:
        inbox_preview = inbox_file.read_text(encoding="utf-8")[-8000:]
    prompt = textwrap.dedent(
        f"""\
        You are a backend Codex sub-agent running under AHA.

        Runtime context:
        - run_id: {run_id}
        - task_id: {task_id}
        - mode: {mode}
        - workspace: {root}
        - inbox_file: {inbox_file}
        - output_file: {output_file}

        Operational rules:
        - Complete the assigned task non-interactively.
        - If mode is research, inspect only and do not edit files.
        - If mode is implementation, keep edits inside the declared write scope from the prompt.
        - Write the final answer as concise Markdown matching the requested sections.
        - Treat the inbox preview as optional context, not as a blocking conversation loop.

        Inbox preview:
        {inbox_preview or "(empty)"}

        Assigned prompt:
        {prompt_file.read_text(encoding="utf-8")}
        """
    )
    append_event_to_file(events_file, run_id, "agent_started", {"source": "codex", "task_id": task_id, "sandbox": sandbox})
    exit_code, _, session = run_codex_exec(
        prompt,
        cwd=root,
        output_file=output_file,
        codex_bin=args.codex_bin,
        model=args.model,
        sandbox=sandbox,
        approval=args.approval,
        json_events=not args.no_json,
        extra_args=args.extra_arg or [],
        events_file=events_file,
        run_id=run_id,
        task_id=task_id,
        source="codex-runner",
        session=session,
    )
    if session:
        save_session(root, session)
    append_event_to_file(events_file, run_id, "agent_finished", {"source": "codex", "task_id": task_id, "exit_code": exit_code})
    return exit_code
