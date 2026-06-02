from __future__ import annotations

import os
from pathlib import Path

from aha_cli.backends.claude import claude_cli_model, claude_config_for_model, claude_permission_mode, claude_resolved_model, run_claude_exec
from aha_cli.services.commit_policy import generated_by_for_backend_model
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.services.proxy import proxy_env_for_agent
from aha_cli.store.filesystem import append_event_to_file, ensure_session, load_config, require_plan, save_session, task_snapshot


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
    os.environ["AHA_AGENT_ID"] = "main"
    os.environ["AHA_BACKEND"] = "claude"
    cfg = load_config(root)
    configured_model = args.model or (cfg.get("claude", {}) or {}).get("model")
    claude_config = claude_config_for_model((cfg.get("claude", {}) or {}), configured_model)
    resolved_model = claude_resolved_model(claude_config, configured_model)
    os.environ["AHA_MODEL"] = resolved_model or ""
    os.environ["AHA_GENERATED_BY"] = generated_by_for_backend_model("claude", resolved_model)
    session = ensure_session(root, run_id, task_id, "main", "claude")
    plan = require_plan(root, run_id)
    task = task_snapshot(root, run_id, task_id)["task"]
    agent = next((item for item in task.get("agents", []) if item.get("id") == "main"), {})

    inbox_preview = ""
    if inbox_file.exists() and inbox_file.stat().st_size:
        inbox_preview = inbox_file.read_text(encoding="utf-8")[-8000:]
    prompt = render_prompt_template(
        "runner_claude.md",
        run_id=run_id,
        task_id=task_id,
        mode=mode,
        workspace=root,
        inbox_file=inbox_file,
        output_file=output_file,
        inbox_preview=inbox_preview or "(empty)",
        assigned_prompt=prompt_file.read_text(encoding="utf-8"),
    )
    append_event_to_file(events_file, run_id, "agent_started", {"source": "claude", "task_id": task_id, "permission_mode": permission_mode})
    exit_code, _, session = run_claude_exec(
        prompt,
        cwd=root,
        output_file=output_file,
        claude_bin=args.claude_bin,
        model=claude_cli_model(configured_model),
        permission_mode=permission_mode,
        extra_args=args.extra_arg or [],
        events_file=events_file,
        run_id=run_id,
        task_id=task_id,
        source="claude-runner",
        session=session,
        proxy_env=proxy_env_for_agent(agent, task, plan, cfg),
        claude_config=claude_config,
    )
    if session:
        save_session(root, session)
    append_event_to_file(events_file, run_id, "agent_finished", {"source": "claude", "task_id": task_id, "exit_code": exit_code})
    return exit_code
