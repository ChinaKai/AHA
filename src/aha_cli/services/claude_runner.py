from __future__ import annotations

import os
from pathlib import Path
import textwrap

from aha_cli.backends.claude import claude_permission_mode, run_claude_exec
from aha_cli.services.proxy import proxy_env_for_agent
from aha_cli.store.filesystem import append_event_to_file, ensure_session, load_config, save_session, task_snapshot


def run_claude_task(args) -> int:
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
        raise SystemExit(f"claude-runner missing environment variables: {', '.join(missing)}")

    root = Path(os.environ["AHA_ROOT"]).resolve()
    run_id = os.environ["AHA_RUN_ID"]
    task_id = os.environ["AHA_TASK_ID"]
    mode = os.environ.get("AHA_MODE", "research")
    prompt_file = Path(os.environ["AHA_PROMPT_FILE"])
    output_file = Path(os.environ["AHA_OUTPUT_FILE"])
    inbox_file = Path(os.environ.get("AHA_INBOX_FILE", ""))
    events_file = Path(os.environ["AHA_EVENTS_FILE"])
    permission_mode = args.permission_mode or claude_permission_mode(mode, args.sandbox)
    cfg = load_config(root)
    session = ensure_session(root, run_id, task_id, "main", "claude")
    task = task_snapshot(root, run_id, task_id)["task"]
    agent = next((item for item in task.get("agents", []) if item.get("id") == "main"), {})

    inbox_preview = ""
    if inbox_file.exists() and inbox_file.stat().st_size:
        inbox_preview = inbox_file.read_text(encoding="utf-8")[-8000:]
    prompt = textwrap.dedent(
        f"""\
        You are a backend Claude Code sub-agent running under AHA.

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
    append_event_to_file(events_file, run_id, "agent_started", {"source": "claude", "task_id": task_id, "permission_mode": permission_mode})
    exit_code, _, session = run_claude_exec(
        prompt,
        cwd=root,
        output_file=output_file,
        claude_bin=args.claude_bin,
        model=args.model,
        permission_mode=permission_mode,
        extra_args=args.extra_arg or [],
        events_file=events_file,
        run_id=run_id,
        task_id=task_id,
        source="claude-runner",
        session=session,
        proxy_env=proxy_env_for_agent(agent, task),
        claude_config=cfg.get("claude", {}),
    )
    if session:
        save_session(root, session)
    append_event_to_file(events_file, run_id, "agent_finished", {"source": "claude", "task_id": task_id, "exit_code": exit_code})
    return exit_code
