from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from aha_cli.backends.codex import codex_runner_command
from aha_cli.backends.registry import agent_backend_names, agent_backend_or_default, backend_names
from aha_cli.domain.models import default_config
from aha_cli.services.chat import auto_reply, codex_chat
from aha_cli.services.commit_policy import CONVENTIONAL_TYPES, format_commit_message, validate_commit_message
from aha_cli.services.codex_runner import run_codex_task
from aha_cli.services.messages import format_event
from aha_cli.services.run_tasks import run_pending_tasks
from aha_cli.services.tasks import create_task_and_dispatch
from aha_cli.store.filesystem import (
    add_agent,
    add_task,
    append_message,
    config_path,
    create_plan,
    event_path,
    find_project_root,
    iter_jsonl_from,
    list_sessions,
    load_config,
    plan_path,
    read_json,
    require_plan,
    resolve_run_id,
    run_dir,
    save_session,
    status_snapshot,
    task_snapshot,
    update_agent_config,
    write_json,
)
from aha_cli.web.server import run_ui_server
from aha_cli.websocket.server import run_ws_server

WATCH_EVENTS_LIMIT = 500
MAX_WATCH_EVENTS_LIMIT = 2000


def visible_plan_tasks(plan: dict) -> list[dict]:
    return [task for task in plan["tasks"] if not task.get("deleted_at")]


def task_dashboard_html(run_id: str, poll_interval_ms: int) -> str:
    del run_id, poll_interval_ms
    return (Path(__file__).parent / "web" / "static" / "index.html").read_text(encoding="utf-8")


def cmd_init(args: argparse.Namespace) -> int:
    root = Path.cwd().resolve() if args.path == "." else Path(args.path).resolve()
    aha = root / ".aha"
    aha.mkdir(parents=True, exist_ok=True)
    cfg = config_path(root)
    if cfg.exists() and not args.force:
        print(f"AHA already initialized: {cfg}")
        return 0
    data = default_config()
    data["backend"] = args.backend or ("command" if args.runner_command else "stub")
    data["runner_command"] = args.runner_command
    data["default_parallel"] = args.parallel
    write_json(cfg, data)
    print(f"Initialized AHA project: {root}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    root = find_project_root()
    if not (root / ".aha").exists():
        cmd_init(argparse.Namespace(path=str(root), force=False, backend=None, runner_command=None, parallel=4))
    cfg = load_config(root)
    plan = create_plan(
        root=root,
        goal=args.goal,
        agents=args.agents,
        mode=args.mode,
        task_titles=args.task or [],
        write_scopes=args.write_scope or [],
        backend=agent_backend_or_default(cfg.get("backend"), "stub"),
    )
    print(f"Created run: {plan['id']}")
    print(f"Plan file: {plan_path(root, plan['id'])}")
    for task in visible_plan_tasks(plan):
        print(f"- {task['id']}: {task['title']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = find_project_root()
    run_id = resolve_run_id(root, args.run_id)
    return run_pending_tasks(root, run_id, args, codex_runner_command)


def cmd_status(args: argparse.Namespace) -> int:
    root = find_project_root()
    run_id = resolve_run_id(root, args.run_id)
    plan = require_plan(root, run_id)
    print(f"Run: {run_id}")
    print(f"Goal: {plan['goal']}")
    print(f"Mode: {plan['mode']}")
    for task in visible_plan_tasks(plan):
        agents = ",".join(agent["id"] for agent in task.get("agents", []))
        print(f"- {task['id']} [{task['status']}] exit={task['exit_code']} agents={agents} {task['title']}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    root = find_project_root()
    run_id = resolve_run_id(root, args.run_id)
    plan = require_plan(root, run_id)
    run = run_dir(root, run_id)
    print(f"# AHA Collection: {run_id}\n")
    print(f"Goal: {plan['goal']}\n")
    for task in visible_plan_tasks(plan):
        output = run / task["output_file"]
        print(f"## {task['id']} - {task['title']}")
        print(f"Status: {task['status']}")
        print()
        print(output.read_text(encoding="utf-8").rstrip() if output.exists() else "No output file.")
        print()
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    root = find_project_root()
    run_id = resolve_run_id(root, args.run_id)
    plan = require_plan(root, run_id)
    run = run_dir(root, run_id)
    lines = [f"# AHA Merged Report: {run_id}", "", f"Goal: {plan['goal']}", f"Mode: {plan['mode']}", "", "## Task Results", ""]
    for task in visible_plan_tasks(plan):
        output = run / task["output_file"]
        lines.extend([f"### {task['id']} - {task['title']}", "", f"Status: {task['status']}, exit_code: {task['exit_code']}", ""])
        lines.append(output.read_text(encoding="utf-8").rstrip() if output.exists() else "No output file.")
        lines.append("")
    path = Path(args.output) if args.output else run / "merged-report.md"
    if not path.is_absolute():
        path = root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote merged report: {path}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    root = find_project_root()
    runs = root / ".aha" / "runs"
    if not runs.is_dir():
        print("No runs found")
        return 0
    for plan_file in sorted(runs.glob("*/plan.json")):
        plan = read_json(plan_file)
        tasks = visible_plan_tasks(plan)
        done = sum(1 for task in tasks if task["status"] == "completed")
        total = len(tasks)
        print(f"{plan['id']} [{done}/{total}] {plan['goal']}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    root = find_project_root()
    run_id = resolve_run_id(root, args.run_id)
    print(json.dumps(status_snapshot(root, run_id), indent=2, ensure_ascii=False))
    events = event_path(root, run_id)
    offset = events.stat().st_size if args.tail and events.exists() else 0
    event_limit = max(1, min(args.event_limit, MAX_WATCH_EVENTS_LIMIT))
    try:
        while True:
            snapshot_offset = events.stat().st_size if events.exists() else 0
            new_events, offset = iter_jsonl_from(events, offset, before=snapshot_offset, limit=event_limit)
            for event in new_events:
                print(format_event(event), flush=True)
            if args.once:
                break
            import time
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    root = find_project_root()
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    message = " ".join(args.message).strip()
    if not message:
        raise SystemExit("Message cannot be empty")
    payload = append_message(root, run_id, args.target, message, sender=args.sender)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    root = find_project_root()
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    print(f"Chatting with {args.target} in run {run_id}. Ctrl-D or Ctrl-C to exit.")
    try:
        for line in sys.stdin:
            message = line.rstrip("\n")
            if message:
                append_message(root, run_id, args.target, message, sender=args.sender)
                print(f"sent -> {args.target}: {message}")
    except KeyboardInterrupt:
        return 130
    return 0


def cmd_auto_reply(args: argparse.Namespace) -> int:
    root = find_project_root()
    run_id = resolve_run_id(root, args.run_id)
    return auto_reply(root, run_id, args)


def cmd_codex_runner(args: argparse.Namespace) -> int:
    return run_codex_task(args)


def cmd_codex_chat(args: argparse.Namespace) -> int:
    root = find_project_root()
    run_id = resolve_run_id(root, args.run_id)
    return codex_chat(root, run_id, args)


def cmd_task(args: argparse.Namespace) -> int:
    root = find_project_root()
    run_id = resolve_run_id(root, args.run_id)
    if args.task_cmd == "add":
        task = create_task_and_dispatch(
            root,
            run_id,
            args.title,
            backend=args.backend,
            model=args.model,
            workspace_path=args.workspace_path,
            sandbox=args.sandbox,
            approval=args.approval,
            delegation_policy=args.delegation_policy,
            max_sub_agents=args.max_sub_agents,
            preferred_sub_backend=args.preferred_sub_backend,
            preferred_sub_model=args.preferred_sub_model,
            dispatch=not args.no_dispatch,
        )
        print(json.dumps(task, indent=2, ensure_ascii=False))
    elif args.task_cmd == "list":
        for task in visible_plan_tasks(require_plan(root, run_id)):
            print(f"{task['id']} [{task['status']}] agents={len(task.get('agents', []))} {task['title']}")
    elif args.task_cmd == "show":
        print(json.dumps(task_snapshot(root, run_id, args.task_id), indent=2, ensure_ascii=False))
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    root = find_project_root()
    run_id = resolve_run_id(root, args.run_id)
    if args.agent_cmd == "add":
        agent = add_agent(root, run_id, args.task_id, backend=args.backend, role=args.role, model=args.model, sandbox=args.sandbox, approval=args.approval, created_by="debug-cli", created_reason="manual CLI add")
        print(json.dumps(agent, indent=2, ensure_ascii=False))
    elif args.agent_cmd == "set":
        agent = update_agent_config(root, run_id, args.task_id, args.agent_id, sandbox=args.sandbox, approval=args.approval)
        print(json.dumps(agent, indent=2, ensure_ascii=False))
    elif args.agent_cmd == "list":
        task = task_snapshot(root, run_id, args.task_id)["task"]
        for agent in task.get("agents", []):
            print(f"{agent['id']} role={agent['role']} backend={agent['backend']} sandbox={agent.get('sandbox') or '-'} approval={agent.get('approval') or '-'} status={agent['status']}")
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    root = find_project_root()
    run_id = resolve_run_id(root, args.run_id)
    if args.session_cmd == "list":
        sessions = list_sessions(root, run_id, args.task_id)
        print(json.dumps(sessions, indent=2, ensure_ascii=False))
    elif args.session_cmd == "reset":
        sessions = list_sessions(root, run_id, args.task_id)
        for session in sessions:
            if session["agent_id"] == args.agent_id:
                session["backend_session_id"] = None
                session["status"] = "reset"
                save_session(root, session)
                print(json.dumps(session, indent=2, ensure_ascii=False))
                return 0
        raise SystemExit(f"Session not found: {args.agent_id}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    root = find_project_root()
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    try:
        asyncio.run(run_ws_server(root, run_id, args.host, args.port, args.interval))
    except KeyboardInterrupt:
        return 130
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    root = find_project_root()
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    try:
        asyncio.run(run_ui_server(root, run_id, args.host, args.port, args.poll_interval))
    except KeyboardInterrupt:
        return 130
    return 0


def cmd_commit(args: argparse.Namespace) -> int:
    try:
        message = format_commit_message(
            args.type,
            args.summary,
            args.task_id,
            args.agent,
            scope=args.scope,
            aha_scope=args.aha_scope,
        )
    except ValueError as exc:
        print(f"Commit message error: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(message, end="")
        return 0
    root = find_project_root()
    add_paths = [path for group in args.add for path in group]
    if add_paths:
        subprocess.run(["git", "add", "--", *add_paths], cwd=root, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root)
    if staged.returncode == 0:
        print("No staged changes to commit. Stage files first or pass --add <path>.", file=sys.stderr)
        return 1
    if staged.returncode != 1:
        print("Unable to inspect staged changes with git diff --cached.", file=sys.stderr)
        return staged.returncode
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(message)
        message_file = handle.name
    try:
        subprocess.run(["git", "commit", "-F", message_file], cwd=root, check=True)
    finally:
        Path(message_file).unlink(missing_ok=True)
    return 0


def cmd_commit_check(args: argparse.Namespace) -> int:
    if args.message_file == "-":
        message = sys.stdin.read()
    else:
        message = Path(args.message_file).read_text(encoding="utf-8")
    errors = validate_commit_message(message)
    if errors:
        for error in errors:
            print(f"commit message error: {error}", file=sys.stderr)
        return 1
    print("Commit message OK")
    return 0


def add_codex_options(parser: argparse.ArgumentParser, prefix: str = "codex") -> None:
    parser.add_argument(f"--{prefix}-bin", default=None if prefix == "codex" else "codex")
    parser.add_argument("--model" if prefix != "codex" else "--codex-model", default=None)
    sandbox_flag = "--sandbox" if prefix != "codex" else "--codex-sandbox"
    approval_flag = "--approval" if prefix != "codex" else "--codex-approval"
    parser.add_argument(sandbox_flag, choices=["auto", "read-only", "workspace-write", "danger-full-access"], default=None if prefix == "codex" else "read-only")
    parser.add_argument(approval_flag, choices=["untrusted", "on-failure", "on-request", "never"], default=None if prefix == "codex" else "never")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aha", description="Agent-help-agent CLI prototype")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="Initialize .aha metadata")
    init_p.add_argument("path", nargs="?", default=".")
    init_p.add_argument("--force", action="store_true")
    init_p.add_argument("--backend", choices=backend_names(), default=None)
    init_p.add_argument("--runner-command", default=None)
    init_p.add_argument("--parallel", type=int, default=4)
    init_p.set_defaults(func=cmd_init)

    plan_p = sub.add_parser("plan", help="Create a multi-agent task plan")
    plan_p.add_argument("goal")
    plan_p.add_argument("--agents", type=int, default=4)
    plan_p.add_argument("--mode", choices=["research", "implementation"], default="research")
    plan_p.add_argument("--task", action="append")
    plan_p.add_argument("--write-scope", action="append")
    plan_p.set_defaults(func=cmd_plan)

    run_p = sub.add_parser("run", help="Run pending tasks")
    run_p.add_argument("run_id", nargs="?")
    run_p.add_argument("--backend", choices=backend_names(), default=None)
    run_p.add_argument("--runner-command", default=None)
    run_p.add_argument("--parallel", type=int, default=None)
    run_p.add_argument("--all", action="store_true")
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--codex-bin", default=None)
    run_p.add_argument("--codex-model", default=None)
    run_p.add_argument("--codex-sandbox", choices=["auto", "read-only", "workspace-write", "danger-full-access"], default=None)
    run_p.add_argument("--codex-approval", choices=["untrusted", "on-failure", "on-request", "never"], default=None)
    run_p.add_argument("--codex-extra-arg", action="append", default=[])
    run_p.add_argument("--no-codex-json", action="store_true")
    run_p.set_defaults(func=cmd_run)

    for name, func in [("status", cmd_status), ("collect", cmd_collect), ("merge", cmd_merge)]:
        p = sub.add_parser(name)
        p.add_argument("run_id", nargs="?")
        if name == "merge":
            p.add_argument("--output", "-o")
        p.set_defaults(func=func)
    sub.add_parser("list", help="List runs").set_defaults(func=cmd_list)

    watch_p = sub.add_parser("watch")
    watch_p.add_argument("run_id", nargs="?")
    watch_p.add_argument("--interval", type=float, default=1.0)
    watch_p.add_argument("--once", action="store_true")
    watch_p.add_argument("--tail", action="store_true")
    watch_p.add_argument("--event-limit", type=int, default=WATCH_EVENTS_LIMIT)
    watch_p.set_defaults(func=cmd_watch)

    send_p = sub.add_parser("send")
    send_p.add_argument("run_id")
    send_p.add_argument("target")
    send_p.add_argument("message", nargs="+")
    send_p.add_argument("--sender", default="main")
    send_p.set_defaults(func=cmd_send)

    chat_p = sub.add_parser("chat")
    chat_p.add_argument("run_id")
    chat_p.add_argument("target")
    chat_p.add_argument("--sender", default="main")
    chat_p.set_defaults(func=cmd_chat)

    auto_p = sub.add_parser("auto-reply")
    auto_p.add_argument("run_id", nargs="?")
    auto_p.add_argument("target", nargs="?", default="main")
    auto_p.add_argument("--sender", default="main")
    auto_p.add_argument("--reply-target", default=None)
    auto_p.add_argument("--template", default="收到：{message}")
    auto_p.add_argument("--interval", type=float, default=1.0)
    auto_p.add_argument("--from-start", action="store_true")
    auto_p.add_argument("--once", action="store_true")
    auto_p.set_defaults(func=cmd_auto_reply)

    commit_p = sub.add_parser("commit", help="Commit staged or selected files with AHA Conventional Commit metadata")
    commit_p.add_argument("--type", choices=CONVENTIONAL_TYPES, required=True)
    commit_p.add_argument("--scope", default=None)
    commit_p.add_argument("--summary", required=True)
    commit_p.add_argument("--task-id", required=True)
    commit_p.add_argument("--agent", required=True)
    commit_p.add_argument("--aha-scope", default=None)
    commit_p.add_argument("--add", nargs="+", action="append", default=[], help="Path(s) to stage before committing; repeatable")
    commit_p.add_argument("--dry-run", action="store_true", help="Print the generated commit message without committing")
    commit_p.set_defaults(func=cmd_commit)

    commit_check_p = sub.add_parser("commit-check", help="Validate an AHA commit message file")
    commit_check_p.add_argument("message_file", help="Commit message file path, or '-' for stdin")
    commit_check_p.set_defaults(func=cmd_commit_check)

    codex_runner_p = sub.add_parser("codex-runner")
    codex_runner_p.add_argument("--codex-bin", default="codex")
    codex_runner_p.add_argument("--model", default=None)
    codex_runner_p.add_argument("--sandbox", choices=["auto", "read-only", "workspace-write", "danger-full-access"], default="auto")
    codex_runner_p.add_argument("--approval", choices=["untrusted", "on-failure", "on-request", "never"], default="never")
    codex_runner_p.add_argument("--extra-arg", action="append", default=[])
    codex_runner_p.add_argument("--no-json", action="store_true")
    codex_runner_p.set_defaults(func=cmd_codex_runner)

    codex_chat_p = sub.add_parser("codex-chat")
    codex_chat_p.add_argument("run_id", nargs="?")
    codex_chat_p.add_argument("target", nargs="?", default="main")
    codex_chat_p.add_argument("--task-id", default=None)
    codex_chat_p.add_argument("--sender", default="main")
    codex_chat_p.add_argument("--reply-target", default=None)
    codex_chat_p.add_argument("--interval", type=float, default=1.0)
    codex_chat_p.add_argument("--from-start", action="store_true")
    codex_chat_p.add_argument("--once", action="store_true")
    codex_chat_p.add_argument("--codex-bin", default="codex")
    codex_chat_p.add_argument("--model", default=None)
    codex_chat_p.add_argument("--sandbox", choices=["auto", "read-only", "workspace-write", "danger-full-access"], default="workspace-write")
    codex_chat_p.add_argument("--approval", choices=["untrusted", "on-failure", "on-request", "never"], default="never")
    codex_chat_p.add_argument("--extra-arg", action="append", default=[])
    codex_chat_p.add_argument("--no-json", action="store_true")
    codex_chat_p.add_argument("--prompt-prefix", default="You are connected to AHA as the real backend agent.")
    codex_chat_p.set_defaults(func=cmd_codex_chat)

    task_p = sub.add_parser("task")
    task_sub = task_p.add_subparsers(dest="task_cmd", required=True)
    task_add = task_sub.add_parser("add")
    task_add.add_argument("run_id")
    task_add.add_argument("title")
    task_add.add_argument("--backend", choices=agent_backend_names(), default="codex")
    task_add.add_argument("--model", default=None)
    task_add.add_argument("--workspace-path", default=None)
    task_add.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default=None)
    task_add.add_argument("--approval", choices=["untrusted", "on-failure", "on-request", "never"], default=None)
    task_add.add_argument("--delegation-policy", choices=["auto", "disabled"], default="auto")
    task_add.add_argument("--max-sub-agents", type=int, default=3)
    task_add.add_argument("--preferred-sub-backend", choices=agent_backend_names(), default=None)
    task_add.add_argument("--preferred-sub-model", default=None)
    task_add.add_argument("--no-dispatch", action="store_true")
    task_add.set_defaults(func=cmd_task)
    task_list = task_sub.add_parser("list")
    task_list.add_argument("run_id", nargs="?")
    task_list.set_defaults(func=cmd_task)
    task_show = task_sub.add_parser("show")
    task_show.add_argument("run_id")
    task_show.add_argument("task_id")
    task_show.set_defaults(func=cmd_task)

    agent_p = sub.add_parser("agent")
    agent_sub = agent_p.add_subparsers(dest="agent_cmd", required=True)
    agent_add = agent_sub.add_parser("add")
    agent_add.add_argument("run_id")
    agent_add.add_argument("task_id")
    agent_add.add_argument("--role", choices=["sub", "task-main"], default="sub")
    agent_add.add_argument("--backend", choices=agent_backend_names(), default="codex")
    agent_add.add_argument("--model", default=None)
    agent_add.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default=None)
    agent_add.add_argument("--approval", choices=["untrusted", "on-failure", "on-request", "never"], default=None)
    agent_add.set_defaults(func=cmd_agent)
    agent_set = agent_sub.add_parser("set")
    agent_set.add_argument("run_id")
    agent_set.add_argument("task_id")
    agent_set.add_argument("agent_id")
    agent_set.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default=None)
    agent_set.add_argument("--approval", choices=["untrusted", "on-failure", "on-request", "never"], default=None)
    agent_set.set_defaults(func=cmd_agent)
    agent_list = agent_sub.add_parser("list")
    agent_list.add_argument("run_id")
    agent_list.add_argument("task_id")
    agent_list.set_defaults(func=cmd_agent)

    session_p = sub.add_parser("session")
    session_sub = session_p.add_subparsers(dest="session_cmd", required=True)
    session_list = session_sub.add_parser("list")
    session_list.add_argument("run_id")
    session_list.add_argument("--task-id", default=None)
    session_list.set_defaults(func=cmd_session)
    session_reset = session_sub.add_parser("reset")
    session_reset.add_argument("run_id")
    session_reset.add_argument("agent_id")
    session_reset.add_argument("--task-id", default=None)
    session_reset.set_defaults(func=cmd_session)

    serve_p = sub.add_parser("serve")
    serve_p.add_argument("run_id", nargs="?")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8765)
    serve_p.add_argument("--interval", type=float, default=1.0)
    serve_p.set_defaults(func=cmd_serve)

    ui_p = sub.add_parser("ui")
    ui_p.add_argument("run_id", nargs="?")
    ui_p.add_argument("--host", default="0.0.0.0")
    ui_p.add_argument("--port", type=int, default=8766)
    ui_p.add_argument("--poll-interval", type=int, default=1000)
    ui_p.set_defaults(func=cmd_ui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
