from __future__ import annotations

import argparse
import asyncio
from importlib import resources
import json
import os
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
from aha_cli.services.onebin import build_onebin
from aha_cli.services.run_archive import RunArchiveError, export_run_archive, import_run_archive
from aha_cli.services.run_tasks import run_pending_tasks
from aha_cli.services.tasks import create_task_and_dispatch
from aha_cli.store.filesystem import (
    add_agent,
    add_task,
    add_workspace,
    AHA_HOME_ENV,
    append_message,
    config_path,
    create_plan,
    default_aha_home,
    event_path,
    find_aha_home,
    find_project_root,
    iter_jsonl_from,
    latest_run_id,
    list_sessions,
    list_workspaces,
    load_config,
    plan_path,
    read_json,
    require_plan,
    reopen_task,
    resolve_workspace_path,
    resolve_run_id,
    run_dir,
    save_session,
    status_snapshot,
    task_snapshot,
    update_agent_config,
    update_task_proxy_config,
    write_json,
)
from aha_cli.web.server import request_task_finalization_with_backend, run_ui_server
from aha_cli.websocket.server import run_ws_server

WATCH_EVENTS_LIMIT = 500
MAX_WATCH_EVENTS_LIMIT = 2000
COMMANDS = {
    "init",
    "plan",
    "run",
    "run-export",
    "run-import",
    "status",
    "collect",
    "merge",
    "list",
    "workspace",
    "watch",
    "send",
    "chat",
    "auto-reply",
    "commit",
    "commit-check",
    "package",
    "codex-runner",
    "codex-chat",
    "task",
    "agent",
    "session",
    "serve",
    "ui",
}


def visible_plan_tasks(plan: dict) -> list[dict]:
    return [task for task in plan["tasks"] if not task.get("deleted_at")]


def task_dashboard_html(run_id: str, poll_interval_ms: int) -> str:
    del run_id, poll_interval_ms
    return resources.files("aha_cli.web").joinpath("static", "index.html").read_text(encoding="utf-8")


def initialize_aha_home(root: Path, args: argparse.Namespace) -> int:
    root.mkdir(parents=True, exist_ok=True)
    cfg = config_path(root)
    if cfg.exists() and not args.force:
        print(f"AHA already initialized: {cfg}")
        return 0
    data = default_config()
    data["backend"] = args.backend or ("command" if args.runner_command else "stub")
    data["runner_command"] = args.runner_command
    data["default_parallel"] = args.parallel
    write_json(cfg, data)
    print(f"Initialized AHA home: {root}")
    return 0


def command_aha_home(args: argparse.Namespace) -> Path:
    return find_aha_home(explicit=getattr(args, "home", None))


def ensure_aha_home(root: Path) -> None:
    if not config_path(root).exists():
        initialize_aha_home(root, argparse.Namespace(force=False, backend=None, runner_command=None, parallel=4))


def cmd_init(args: argparse.Namespace) -> int:
    if args.portable or (args.path != "." and not getattr(args, "home", None) and not os.environ.get(AHA_HOME_ENV)):
        root = Path.cwd().resolve() if args.path == "." else Path(args.path).expanduser().resolve()
        return initialize_aha_home(root / ".aha", args)
    if getattr(args, "home", None) or os.environ.get(AHA_HOME_ENV):
        return initialize_aha_home(command_aha_home(args), args)
    return initialize_aha_home(default_aha_home(), args)


def cmd_plan(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    ensure_aha_home(root)
    cfg = load_config(root)
    try:
        workspace_path, workspace_id = resolve_workspace_path(
            root,
            workspace_id=args.workspace,
            workspace_path=args.workspace_path,
            default=Path.cwd(),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    plan = create_plan(
        root=root,
        goal=args.goal,
        agents=args.agents,
        mode=args.mode,
        task_titles=args.task or [],
        write_scopes=args.write_scope or [],
        backend=agent_backend_or_default(cfg.get("backend"), "stub"),
        workspace_path=workspace_path,
        workspace_id=workspace_id,
        proxy_enabled=args.proxy_enabled or bool(args.http_proxy or args.https_proxy),
        http_proxy=args.http_proxy,
        https_proxy=args.https_proxy,
        no_proxy=args.no_proxy,
    )
    print(f"Created run: {plan['id']}")
    print(f"Plan file: {plan_path(root, plan['id'])}")
    for task in visible_plan_tasks(plan):
        print(f"- {task['id']}: {task['title']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    return run_pending_tasks(root, run_id, args, codex_runner_command)


def cmd_run_export(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    output = Path(args.output or f"{run_id}.tar.gz")
    try:
        archive = export_run_archive(root, run_id, output, include_logs=not args.no_logs)
    except RunArchiveError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Exported run {run_id}: {archive}")
    return 0


def cmd_run_import(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    ensure_aha_home(root)
    try:
        source_run_id, imported_run_id = import_run_archive(
            root,
            Path(args.archive),
            target_run_id=args.run_id,
            preserve_id=args.preserve_id,
            force=args.force,
        )
    except RunArchiveError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Imported run {source_run_id} as {imported_run_id}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
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
    root = command_aha_home(args)
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
    root = command_aha_home(args)
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
    root = command_aha_home(args)
    runs = run_dir(root, "_").parent
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


def cmd_workspace(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    if args.workspace_cmd == "add":
        ensure_aha_home(root)
        try:
            workspace = add_workspace(root, args.path, name=args.name)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"{workspace['id']} {workspace['name']} {workspace['path']}")
    elif args.workspace_cmd == "list":
        workspaces = list_workspaces(root)
        if not workspaces:
            print("No workspaces found")
            return 0
        for workspace in workspaces:
            print(f"{workspace['id']} {workspace.get('name') or '-'} {workspace['path']}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
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
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    message = " ".join(args.message).strip()
    if not message:
        raise SystemExit("Message cannot be empty")
    payload = append_message(root, run_id, args.target, message, sender=args.sender)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
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
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    return auto_reply(root, run_id, args)


def cmd_codex_runner(args: argparse.Namespace) -> int:
    return run_codex_task(args)


def cmd_codex_chat(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    return codex_chat(root, run_id, args)


def cmd_task(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    if args.task_cmd == "add":
        try:
            workspace_path, workspace_id = resolve_workspace_path(
                root,
                workspace_id=args.workspace,
                workspace_path=args.workspace_path,
                default=Path.cwd(),
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        task = create_task_and_dispatch(
            root,
            run_id,
            args.title,
            backend=args.backend,
            model=args.model,
            workspace_path=workspace_path,
            workspace_id=workspace_id,
            sandbox=args.sandbox,
            approval=args.approval,
            proxy_enabled=args.proxy_enabled or bool(args.http_proxy or args.https_proxy),
            http_proxy=args.http_proxy,
            https_proxy=args.https_proxy,
            no_proxy=args.no_proxy,
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
    elif args.task_cmd == "final":
        payload = request_task_finalization_with_backend(
            root,
            run_id,
            args.task_id,
            f"aha task final {run_id} {args.task_id}",
            autostart_backend=not args.no_autostart,
        )
        print(payload["message"])
        backend = payload.get("backend")
        if backend:
            print(f"Backend: {backend.get('status')} pid={backend.get('pid') or '-'}")
    elif args.task_cmd == "reopen":
        reopen_task(root, run_id, args.task_id)
        print(f"{args.task_id} reopened. Follow-up messages are allowed again.")
    elif args.task_cmd == "proxy":
        fields = {}
        if args.proxy_enabled is not None:
            fields["proxy_enabled"] = args.proxy_enabled
        if args.http_proxy is not None:
            fields["http_proxy"] = args.http_proxy
        if args.https_proxy is not None:
            fields["https_proxy"] = args.https_proxy
        if args.no_proxy is not None:
            fields["no_proxy"] = args.no_proxy
        task = update_task_proxy_config(root, run_id, args.task_id, **fields)
        print(json.dumps(task, indent=2, ensure_ascii=False))
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    if args.agent_cmd == "add":
        agent = add_agent(
            root,
            run_id,
            args.task_id,
            backend=args.backend,
            role=args.role,
            model=args.model,
            sandbox=args.sandbox,
            approval=args.approval,
            proxy_enabled=args.proxy_enabled,
            created_by="debug-cli",
            created_reason="manual CLI add",
        )
        print(json.dumps(agent, indent=2, ensure_ascii=False))
    elif args.agent_cmd == "set":
        agent = update_agent_config(root, run_id, args.task_id, args.agent_id, sandbox=args.sandbox, approval=args.approval, proxy_enabled=args.proxy_enabled)
        print(json.dumps(agent, indent=2, ensure_ascii=False))
    elif args.agent_cmd == "list":
        task = task_snapshot(root, run_id, args.task_id)["task"]
        for agent in task.get("agents", []):
            print(f"{agent['id']} role={agent['role']} backend={agent['backend']} sandbox={agent.get('sandbox') or '-'} approval={agent.get('approval') or '-'} status={agent['status']}")
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
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
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    try:
        asyncio.run(run_ws_server(root, run_id, args.host, args.port, args.interval))
    except KeyboardInterrupt:
        return 130
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    ensure_aha_home(root)
    run_id = args.run_id or latest_run_id(root) or ""
    if run_id:
        require_plan(root, run_id)
    try:
        asyncio.run(run_ui_server(root, run_id, args.host, args.port, args.poll_interval))
    except KeyboardInterrupt:
        return 130
    return 0


def cmd_package(args: argparse.Namespace) -> int:
    if args.package_cmd == "onebin":
        try:
            artifact = build_onebin(
                Path(args.output),
                source_root=Path(args.source_root).expanduser().resolve() if args.source_root else None,
                interpreter=args.interpreter,
                compressed=not args.no_compress,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Built one-bin executable: {artifact}")
        return 0
    raise SystemExit(f"Unknown package command: {args.package_cmd}")


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
    parser.add_argument("--home", default=None, help="AHA_HOME data directory. Defaults to $AHA_HOME, a nearby .aha, or ~/.aha.")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="Initialize AHA metadata")
    init_p.add_argument("path", nargs="?", default=".", help="Project path for --portable initialization")
    init_p.add_argument("--portable", action="store_true", help="Initialize PATH/.aha instead of the default AHA_HOME")
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
    plan_p.add_argument("--workspace", default=None, help="Registered workspace id, such as ws-001")
    plan_p.add_argument("--workspace-path", default=None, help="Workspace path for the created tasks")
    plan_p.add_argument("--enable-proxy", dest="proxy_enabled", action="store_true", help="Enable task proxy for created agents")
    plan_p.add_argument("--http-proxy", default=None)
    plan_p.add_argument("--https-proxy", default=None)
    plan_p.add_argument("--no-proxy", default=None, help="NO_PROXY value for created tasks")
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

    run_export_p = sub.add_parser("run-export", help=argparse.SUPPRESS)
    run_export_p.add_argument("run_id", nargs="?")
    run_export_p.add_argument("--output", "-o", default=None)
    run_export_p.add_argument("--no-logs", action="store_true")
    run_export_p.set_defaults(func=cmd_run_export)

    run_import_p = sub.add_parser("run-import", help=argparse.SUPPRESS)
    run_import_p.add_argument("archive")
    run_import_p.add_argument("--run-id", default=None)
    run_import_p.add_argument("--preserve-id", action="store_true")
    run_import_p.add_argument("--force", action="store_true")
    run_import_p.set_defaults(func=cmd_run_import)

    for name, func in [("status", cmd_status), ("collect", cmd_collect), ("merge", cmd_merge)]:
        p = sub.add_parser(name)
        p.add_argument("run_id", nargs="?")
        if name == "merge":
            p.add_argument("--output", "-o")
        p.set_defaults(func=func)
    sub.add_parser("list", help="List runs").set_defaults(func=cmd_list)

    workspace_p = sub.add_parser("workspace", help="Manage registered workspaces")
    workspace_sub = workspace_p.add_subparsers(dest="workspace_cmd", required=True)
    workspace_add = workspace_sub.add_parser("add")
    workspace_add.add_argument("path")
    workspace_add.add_argument("--name", default=None)
    workspace_add.set_defaults(func=cmd_workspace)
    workspace_list = workspace_sub.add_parser("list")
    workspace_list.set_defaults(func=cmd_workspace)

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

    package_p = sub.add_parser("package", help="Build distributable artifacts")
    package_sub = package_p.add_subparsers(dest="package_cmd", required=True)
    onebin_p = package_sub.add_parser("onebin", help="Build a single-file executable zipapp")
    onebin_p.add_argument("--output", "-o", default="dist/aha", help="Output executable path")
    onebin_p.add_argument("--interpreter", default="/usr/bin/env python3", help="Shebang interpreter for the artifact")
    onebin_p.add_argument("--no-compress", action="store_true", help="Store files without ZIP compression")
    onebin_p.add_argument("--source-root", default=None, help=argparse.SUPPRESS)
    onebin_p.set_defaults(func=cmd_package)

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
    task_add.add_argument("--workspace", default=None, help="Registered workspace id, such as ws-001")
    task_add.add_argument("--workspace-path", default=None)
    task_add.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default=None)
    task_add.add_argument("--approval", choices=["untrusted", "on-failure", "on-request", "never"], default=None)
    task_add.add_argument("--enable-proxy", dest="proxy_enabled", action="store_true", help="Enable task proxy for created agents")
    task_add.add_argument("--http-proxy", default=None)
    task_add.add_argument("--https-proxy", default=None)
    task_add.add_argument("--no-proxy", default=None, help="NO_PROXY value for this task")
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
    task_final = task_sub.add_parser("final", help="Ask task-main to generate the Final and complete the task")
    task_final.add_argument("run_id")
    task_final.add_argument("task_id")
    task_final.add_argument("--no-autostart", action="store_true", help="Do not start a stopped task-main backend")
    task_final.set_defaults(func=cmd_task)
    task_reopen = task_sub.add_parser("reopen", help="Reopen a completed task for follow-up")
    task_reopen.add_argument("run_id")
    task_reopen.add_argument("task_id")
    task_reopen.set_defaults(func=cmd_task)
    task_proxy = task_sub.add_parser("proxy", help="Update task proxy defaults")
    task_proxy.add_argument("run_id")
    task_proxy.add_argument("task_id")
    proxy_group = task_proxy.add_mutually_exclusive_group()
    proxy_group.add_argument("--enable-proxy", dest="proxy_enabled", action="store_true")
    proxy_group.add_argument("--disable-proxy", dest="proxy_enabled", action="store_false")
    task_proxy.set_defaults(proxy_enabled=None)
    task_proxy.add_argument("--http-proxy", default=None)
    task_proxy.add_argument("--https-proxy", default=None)
    task_proxy.add_argument("--no-proxy", default=None, help="NO_PROXY value; pass an empty string to clear")
    task_proxy.set_defaults(func=cmd_task)

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
    add_proxy_group = agent_add.add_mutually_exclusive_group()
    add_proxy_group.add_argument("--enable-proxy", dest="proxy_enabled", action="store_true")
    add_proxy_group.add_argument("--disable-proxy", dest="proxy_enabled", action="store_false")
    agent_add.set_defaults(proxy_enabled=None)
    agent_add.set_defaults(func=cmd_agent)
    agent_set = agent_sub.add_parser("set")
    agent_set.add_argument("run_id")
    agent_set.add_argument("task_id")
    agent_set.add_argument("agent_id")
    agent_set.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default=None)
    agent_set.add_argument("--approval", choices=["untrusted", "on-failure", "on-request", "never"], default=None)
    set_proxy_group = agent_set.add_mutually_exclusive_group()
    set_proxy_group.add_argument("--enable-proxy", dest="proxy_enabled", action="store_true")
    set_proxy_group.add_argument("--disable-proxy", dest="proxy_enabled", action="store_false")
    agent_set.set_defaults(proxy_enabled=None)
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


def normalize_run_subcommand(argv: list[str]) -> list[str]:
    command_index = _first_command_index(argv)
    if command_index is None or argv[command_index] != "run" or command_index + 1 >= len(argv):
        return argv
    action = argv[command_index + 1]
    if action not in {"export", "import"}:
        return argv
    return [*argv[:command_index], f"run-{action}", *argv[command_index + 2 :]]


def _first_command_index(argv: list[str]) -> int | None:
    skip_next = False
    for index, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if arg == "--home":
            skip_next = True
            continue
        if arg.startswith("--home="):
            continue
        if arg.startswith("-"):
            continue
        return index
    return None


def with_default_command(argv: list[str]) -> list[str]:
    if not argv:
        return ["ui"]
    if any(arg in {"-h", "--help"} for arg in argv):
        return argv
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--home":
            skip_next = True
            continue
        if arg.startswith("--home="):
            continue
        if arg in COMMANDS:
            return argv
    return [*argv, "ui"]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    normalized = normalize_run_subcommand(list(sys.argv[1:] if argv is None else argv))
    args = parser.parse_args(with_default_command(normalized))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
