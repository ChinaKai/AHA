from __future__ import annotations

import os
from pathlib import Path

from aha_cli.backends.codex import codex_cli_model, codex_config_for_model, codex_resolved_model, codex_sandbox, run_codex_exec
from aha_cli.services.commit_policy import generated_by_for_backend_model
from aha_cli.services.prompt_templates import render_prompt_template
from aha_cli.services.proxy import proxy_env_for_agent
from aha_cli.store.filesystem import append_event_to_file, ensure_session, load_config, require_plan, save_session, task_snapshot


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
    cfg = load_config(root)
    configured_model = args.model or (cfg.get("codex", {}) or {}).get("model")
    codex_config = codex_config_for_model((cfg.get("codex", {}) or {}), configured_model)
    command_model = codex_cli_model(codex_config, configured_model)
    resolved_model = codex_resolved_model(codex_config, configured_model)
    os.environ["AHA_AGENT_ID"] = "main"
    os.environ["AHA_BACKEND"] = "codex"
    os.environ["AHA_MODEL"] = resolved_model or ""
    os.environ["AHA_GENERATED_BY"] = generated_by_for_backend_model("codex", resolved_model)
    session = ensure_session(root, run_id, task_id, "main", "codex")
    plan = require_plan(root, run_id)
    task = task_snapshot(root, run_id, task_id)["task"]
    agent = next((item for item in task.get("agents", []) if item.get("id") == "main"), {})

    inbox_preview = ""
    if inbox_file.exists() and inbox_file.stat().st_size:
        inbox_preview = inbox_file.read_text(encoding="utf-8")[-8000:]
    prompt = render_prompt_template(
        "runner_codex.md",
        run_id=run_id,
        task_id=task_id,
        mode=mode,
        workspace=root,
        inbox_file=inbox_file,
        output_file=output_file,
        inbox_preview=inbox_preview or "(empty)",
        assigned_prompt=prompt_file.read_text(encoding="utf-8"),
    )
    append_event_to_file(events_file, run_id, "agent_started", {"source": "codex", "task_id": task_id, "sandbox": sandbox})
    exit_code, _, session = run_codex_exec(
        prompt,
        cwd=root,
        output_file=output_file,
        codex_bin=args.codex_bin,
        model=configured_model,
        sandbox=sandbox,
        approval=args.approval,
        json_events=not args.no_json,
        reasoning_effort=args.reasoning_effort,
        extra_args=args.extra_arg or [],
        events_file=events_file,
        run_id=run_id,
        task_id=task_id,
        source="codex-runner",
        session=session,
        proxy_env=proxy_env_for_agent(agent, task, plan, cfg),
        codex_config=codex_config,
    )
    if session:
        save_session(root, session)
    append_event_to_file(events_file, run_id, "agent_finished", {"source": "codex", "task_id": task_id, "exit_code": exit_code})
    return exit_code
