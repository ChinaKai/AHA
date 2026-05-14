from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys

from aha_cli.store.filesystem import append_event_to_file

OUTPUT_TAIL_LIMIT = 1200


def tail_text(value: str, limit: int = OUTPUT_TAIL_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def codex_sandbox(mode: str, requested: str) -> str:
    if requested != "auto":
        return requested
    return "read-only" if mode == "research" else "workspace-write"


def handle_codex_event(
    line: str,
    *,
    events_file: Path | None,
    run_id: str,
    task_id: str | None,
    source: str,
    session: dict | None = None,
) -> None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    raw_type = event.get("type")
    data: dict = {"source": source, "raw_type": raw_type}
    if task_id:
        data["task_id"] = task_id
    if raw_type == "thread.started":
        thread_id = event.get("thread_id")
        data["thread_id"] = thread_id
        if session is not None and thread_id and not session.get("backend_session_id"):
            session["backend_session_id"] = thread_id
        append_event_to_file(events_file, run_id, "agent_thread", data)
    elif raw_type == "error":
        data["message"] = event.get("message", "")
        append_event_to_file(events_file, run_id, "agent_error", data)
    elif raw_type == "turn.completed":
        data["usage"] = event.get("usage", {})
        append_event_to_file(events_file, run_id, "agent_usage", data)
    elif raw_type in {"item.started", "item.completed"}:
        item = event.get("item", {})
        data["item_type"] = item.get("type")
        if item.get("type") == "agent_message" and raw_type == "item.completed":
            data["text"] = item.get("text", "")
            append_event_to_file(events_file, run_id, "agent_message", data)
        elif item.get("type") == "command_execution":
            data["command"] = item.get("command", "")
            data["status"] = item.get("status", "")
            data["exit_code"] = item.get("exit_code")
            if raw_type == "item.completed":
                data["output_tail"] = tail_text(item.get("aggregated_output", ""))
            append_event_to_file(
                events_file,
                run_id,
                "agent_command_started" if raw_type == "item.started" else "agent_command_finished",
                data,
            )


def run_codex_exec(
    prompt: str,
    *,
    cwd: Path,
    output_file: Path,
    codex_bin: str = "codex",
    model: str | None = None,
    sandbox: str = "read-only",
    approval: str = "never",
    json_events: bool = True,
    extra_args: list[str] | None = None,
    events_file: Path | None = None,
    run_id: str = "",
    task_id: str | None = None,
    source: str = "codex",
    session: dict | None = None,
) -> tuple[int, str, dict | None]:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    session_id = session.get("backend_session_id") if session else None
    cmd = build_codex_exec_command(
        codex_bin=codex_bin,
        model=model,
        approval=approval,
        sandbox=sandbox,
        cwd=cwd,
        output_file=output_file,
        json_events=json_events,
        session_id=session_id,
    )
    for raw in extra_args or []:
        insert_at = -1 if cmd[-1] == "-" else len(cmd)
        for part in shlex.split(raw):
            cmd.insert(insert_at, part)
            insert_at += 1

    print(f"Running Codex backend: {' '.join(shlex.quote(part) for part in cmd[:-1])} -", flush=True)
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        process.stdin.write(prompt)
        process.stdin.close()
    except BrokenPipeError:
        pass
    for raw_line in process.stdout:
        print(raw_line, end="", flush=True)
        handle_codex_event(
            raw_line.strip(),
            events_file=events_file,
            run_id=run_id,
            task_id=task_id,
            source=source,
            session=session,
        )
    exit_code = process.wait()
    final_text = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
    return exit_code, final_text, session


def build_codex_exec_command(
    *,
    codex_bin: str,
    model: str | None,
    approval: str | None,
    sandbox: str,
    cwd: Path,
    output_file: Path,
    json_events: bool,
    session_id: str | None,
) -> list[str]:
    cmd = [codex_bin]
    if model:
        cmd.extend(["-m", model])
    if approval:
        cmd.extend(["-a", approval])

    cmd.extend(["exec", "--skip-git-repo-check", "--sandbox", sandbox, "-C", str(cwd)])
    if session_id:
        cmd.append("resume")
    if json_events:
        cmd.append("--json")
    cmd.extend(["-o", str(output_file)])
    if session_id:
        cmd.extend([session_id, "-"])
    else:
        cmd.append("-")
    return cmd


def codex_runner_command(args, cfg: dict) -> str:
    codex_cfg = cfg.get("codex", {})
    parts = [shlex.quote(sys.executable), "-m", "aha_cli", "codex-runner"]
    parts.extend(["--codex-bin", shlex.quote(args.codex_bin or codex_cfg.get("bin") or "codex")])
    model = args.codex_model if args.codex_model is not None else codex_cfg.get("model")
    if model:
        parts.extend(["--model", shlex.quote(model)])
    sandbox = args.codex_sandbox or codex_cfg.get("sandbox") or "auto"
    parts.extend(["--sandbox", shlex.quote(sandbox)])
    approval = args.codex_approval or codex_cfg.get("approval") or "never"
    parts.extend(["--approval", shlex.quote(approval)])
    if args.no_codex_json or not codex_cfg.get("json", True):
        parts.append("--no-json")
    for extra in args.codex_extra_arg or []:
        parts.extend(["--extra-arg", shlex.quote(extra)])
    return " ".join(parts)
