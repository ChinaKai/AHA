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

from aha_cli.backends.claude import claude_runner_command
from aha_cli.backends.codex import codex_runner_command
from aha_cli.backends.registry import agent_backend_or_default
from aha_cli.cli_parser import MAX_WATCH_EVENTS_LIMIT, build_parser as build_cli_parser, normalize_run_subcommand, with_default_command
from aha_cli.domain.models import default_config
from aha_cli.services.chat import auto_reply, claude_chat, codex_chat
from aha_cli.services.claude_runner import run_claude_task
from aha_cli.services.commit_policy import DEFAULT_GENERATED_BY, format_commit_message, generated_by_for_backend_model, validate_commit_message
from aha_cli.services.codex_runner import run_codex_task
from aha_cli.services.hardware_io import append_hardware_io_record
from aha_cli.services.hardware_session import (
    HardwareSessionDaemon,
    append_session_control,
    open_uart_transport,
    read_session_state,
)
from aha_cli.services.messages import format_event
from aha_cli.services.onebin import build_onebin
from aha_cli.services.run_archive import RunArchiveError, export_run_archive, import_run_archive
from aha_cli.services.run_cleanup import cleanup_temp_runs, format_cleanup_summary
from aha_cli.services.run_delete import RunDeleteError, delete_run
from aha_cli.services.run_diagnostics import diagnose_runs, format_run_diagnostics
from aha_cli.services.run_lifecycle_actions import RunLifecycleActionError, set_run_lifecycle_status
from aha_cli.services.run_recovery import RunRecoveryError, format_stale_runtime_recovery, run_stale_runtime_recovery
from aha_cli.services.run_retention import (
    RunRetentionError,
    apply_run_retention,
    format_retention_archive_inspect,
    format_retention_archive_list,
    format_retention_archive_restore,
    format_retention_report,
    inspect_retention_archive,
    list_retention_archives,
    restore_retention_archive,
    run_retention_report,
)
from aha_cli.services.run_retention_policy import (
    enforce_all_run_retention_policy,
    enforce_run_retention_policy,
    format_all_run_retention_policy_report,
    write_retention_policy_report,
)
from aha_cli.services.run_tasks import run_pending_tasks
from aha_cli.services.session_compact import compact_reset_backend_session
from aha_cli.services.tasks import create_task_and_dispatch
from aha_cli.web.auth import bind_host_exposes_network, resolve_auth_token
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
        initialize_aha_home(root, argparse.Namespace(force=False, backend=None, runner_command=None, parallel=10))


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
        proxy_enabled=(args.proxy_enabled or bool(args.http_proxy or args.https_proxy)) if (args.proxy_enabled or args.http_proxy or args.https_proxy or args.no_proxy) else None,
        http_proxy=args.http_proxy,
        https_proxy=args.https_proxy,
        no_proxy=args.no_proxy,
        collaboration_mode=args.collaboration_mode,
        workflow_template=args.workflow_template,
    )
    print(f"Created run: {plan['id']}")
    print(f"Plan file: {plan_path(root, plan['id'])}")
    for task in visible_plan_tasks(plan):
        print(f"- {task['id']}: {task['title']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    return run_pending_tasks(root, run_id, args, codex_runner_command, claude_runner_command)


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


def cmd_runs(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    if args.runs_cmd == "diagnose":
        result = diagnose_runs(
            root,
            current_run_id=args.current_run or os.environ.get("AHA_RUN_ID"),
            stale_seconds=args.stale_seconds,
            active_heartbeat_seconds=args.active_heartbeat_seconds,
        )
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(format_run_diagnostics(result), end="")
        return 0
    if args.runs_cmd == "lifecycle":
        try:
            run = set_run_lifecycle_status(
                root,
                args.run_id,
                args.status,
                current_run_id=args.current_run or os.environ.get("AHA_RUN_ID"),
                active_heartbeat_seconds=args.active_heartbeat_seconds,
            )
        except RunLifecycleActionError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        payload = {"ok": True, "run": run}
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"{run['id']} lifecycle={run['lifecycle_status']}")
        return 0
    if args.runs_cmd == "cleanup":
        result = cleanup_temp_runs(
            root,
            current_run_id=args.current_run or os.environ.get("AHA_RUN_ID"),
            tmp_root=Path(args.tmp_root).expanduser() if args.tmp_root else None,
            dry_run=not args.apply,
            stale_seconds=args.stale_seconds,
            active_heartbeat_seconds=args.active_heartbeat_seconds,
            allow_non_temp_root=args.allow_non_temp_root,
        )
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(format_cleanup_summary(result), end="")
        return 1 if result["errors"] else 0
    if args.runs_cmd == "delete":
        try:
            result = delete_run(
                root,
                args.run_id,
                current_run_id=args.current_run or os.environ.get("AHA_RUN_ID"),
                force=args.force,
                active_heartbeat_seconds=args.active_heartbeat_seconds,
            )
        except (RunDeleteError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        payload = {"ok": True, "deleted": result}
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            suffix = " (forced)" if args.force else ""
            print(f"Deleted run {result['run_id']}{suffix}")
        return 0
    if args.runs_cmd == "retention":
        if args.force and not (args.apply or args.apply_if_over_limit):
            print("--force requires --apply or --apply-if-over-limit", file=sys.stderr)
            return 2
        if args.apply and args.apply_if_over_limit:
            print("--apply and --apply-if-over-limit are mutually exclusive", file=sys.stderr)
            return 2
        if args.apply_if_over_limit and not any((args.max_total_bytes, args.max_candidate_bytes, args.min_candidate_files)):
            print("--apply-if-over-limit requires at least one policy threshold", file=sys.stderr)
            return 2
        try:
            if args.apply_if_over_limit:
                result = enforce_run_retention_policy(
                    root,
                    args.run_id,
                    apply=True,
                    current_run_id=args.current_run or os.environ.get("AHA_RUN_ID"),
                    active_heartbeat_seconds=args.active_heartbeat_seconds,
                    archive_dir=Path(args.archive_dir).expanduser() if args.archive_dir else None,
                    force=args.force,
                    top=args.top,
                    include_chat=args.include_chat,
                    min_age_seconds=args.min_age_seconds,
                    max_total_bytes=args.max_total_bytes,
                    max_candidate_bytes=args.max_candidate_bytes,
                    min_candidate_files=args.min_candidate_files,
                )
            elif args.apply:
                result = apply_run_retention(
                    root,
                    args.run_id,
                    current_run_id=args.current_run or os.environ.get("AHA_RUN_ID"),
                    active_heartbeat_seconds=args.active_heartbeat_seconds,
                    archive_dir=Path(args.archive_dir).expanduser() if args.archive_dir else None,
                    force=args.force,
                    top=args.top,
                    include_chat=args.include_chat,
                    min_age_seconds=args.min_age_seconds,
                    max_total_bytes=args.max_total_bytes,
                    max_candidate_bytes=args.max_candidate_bytes,
                    min_candidate_files=args.min_candidate_files,
                )
            else:
                result = run_retention_report(
                    root,
                    args.run_id,
                    top=args.top,
                    include_chat=args.include_chat,
                    min_age_seconds=args.min_age_seconds,
                    max_total_bytes=args.max_total_bytes,
                    max_candidate_bytes=args.max_candidate_bytes,
                    min_candidate_files=args.min_candidate_files,
                )
        except (FileNotFoundError, RunRetentionError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(format_retention_report(result), end="")
        return 1 if result.get("errors") else 0
    if args.runs_cmd == "retention-policy":
        if args.force and not args.apply_if_over_limit:
            print("--force requires --apply-if-over-limit", file=sys.stderr)
            return 2
        if args.apply_if_over_limit and not any((args.max_total_bytes, args.max_candidate_bytes, args.min_candidate_files)):
            print("--apply-if-over-limit requires at least one policy threshold", file=sys.stderr)
            return 2
        try:
            result = enforce_all_run_retention_policy(
                root,
                apply=args.apply_if_over_limit,
                current_run_id=args.current_run or os.environ.get("AHA_RUN_ID"),
                active_heartbeat_seconds=args.active_heartbeat_seconds,
                archive_dir=Path(args.archive_dir).expanduser() if args.archive_dir else None,
                force=args.force,
                top=args.top,
                include_chat=args.include_chat,
                min_age_seconds=args.min_age_seconds,
                max_total_bytes=args.max_total_bytes,
                max_candidate_bytes=args.max_candidate_bytes,
                min_candidate_files=args.min_candidate_files,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.write_report:
            result = write_retention_policy_report(
                root,
                result,
                report_dir=Path(args.report_dir).expanduser() if args.report_dir else None,
            )
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(format_all_run_retention_policy_report(result), end="")
        return 1 if result.get("errors") else 0
    if args.runs_cmd == "retention-archive":
        try:
            if args.retention_archive_cmd == "list":
                result = list_retention_archives(
                    root,
                    args.run_id,
                    archive_dir=Path(args.archive_dir).expanduser() if args.archive_dir else None,
                )
                formatter = format_retention_archive_list
            elif args.retention_archive_cmd == "inspect":
                result = inspect_retention_archive(Path(args.archive))
                formatter = format_retention_archive_inspect
            elif args.retention_archive_cmd == "restore":
                result = restore_retention_archive(
                    root,
                    Path(args.archive),
                    run_id=args.run_id,
                    current_run_id=args.current_run or os.environ.get("AHA_RUN_ID"),
                    active_heartbeat_seconds=args.active_heartbeat_seconds,
                    force=args.force,
                )
                formatter = format_retention_archive_restore
            else:
                raise SystemExit(f"Unknown retention archive command: {args.retention_archive_cmd}")
        except (FileNotFoundError, RunRetentionError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(formatter(result), end="")
        return 1 if result.get("errors") else 0
    if args.runs_cmd == "recover":
        if args.restart_backend and not args.apply:
            print("--restart-backend requires --apply", file=sys.stderr)
            return 2
        try:
            result = run_stale_runtime_recovery(
                root,
                args.run_id,
                task_id=args.task_id,
                agent_id=args.agent_id,
                apply=args.apply,
                restart_backend=args.restart_backend,
            )
        except RunRecoveryError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(format_stale_runtime_recovery(result), end="")
        return 0
    raise SystemExit(f"Unknown runs command: {args.runs_cmd}")


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


def cmd_hardware_io(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    result = append_hardware_io_record(
        root,
        run_id,
        args.task_id,
        {
            "agent_id": args.agent_id,
            "channel": args.channel,
            "endpoint": args.endpoint,
            "direction": args.direction,
            "encoding": args.encoding,
            "data": args.data,
        },
        default_agent_id=args.agent_id,
    )
    if args.json:
        print(json.dumps(result["record"], ensure_ascii=False))
    else:
        record = result["record"]
        print(f"{record['ts']} {record['task_id']} {record['agent_id']} {record['channel']} {record['direction']} {record['data']}")
    return 0


def cmd_hardware_attach(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    device = str(args.device or "").strip()
    if not device:
        print("hardware-attach requires --device (e.g. /dev/ttyUSB0)", file=sys.stderr)
        return 2
    try:
        transport = open_uart_transport(device, args.baudrate)
    except OSError as exc:
        print(f"Failed to open {device}: {exc}", file=sys.stderr)
        return 1
    endpoint = f"{device}@{args.baudrate}"
    daemon = HardwareSessionDaemon(
        root,
        run_id,
        args.task_id,
        args.channel,
        transport,
        endpoint=endpoint,
        agent_id=args.agent_id,
        idle_timeout=args.idle_timeout,
    )
    print(f"Attached to {endpoint} on channel {args.channel}. Ctrl-C or `aha hardware-stop` to detach.")
    try:
        daemon.run()
    except KeyboardInterrupt:
        daemon._running = False
    return 0


def cmd_hardware_send(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    append_session_control(root, run_id, args.task_id, args.channel, {"cmd": "send", "data": args.data})
    print(f"Queued send on {args.channel}: {args.data!r}")
    return 0


def cmd_hardware_arm(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    command = {
        "cmd": "arm",
        "id": args.id,
        "pattern": args.pattern,
        "regex": bool(args.regex),
        "send": args.send,
        "max_fires": args.max_fires,
        "ttl_seconds": args.ttl,
        "delay_seconds": args.delay,
        "interval_seconds": args.interval,
        "duration_seconds": args.duration,
    }
    record = append_session_control(root, run_id, args.task_id, args.channel, command)
    print(f"Queued arm on {args.channel}: {json.dumps(record, ensure_ascii=False)}")
    return 0


def cmd_hardware_disarm(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    append_session_control(root, run_id, args.task_id, args.channel, {"cmd": "disarm", "id": args.id})
    print(f"Queued disarm on {args.channel}: rule {args.id}")
    return 0


def cmd_hardware_rules(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    state = read_session_state(root, run_id, args.task_id, args.channel) or {"status": "detached", "rules": []}
    if args.json:
        print(json.dumps(state, ensure_ascii=False))
        return 0
    print(f"channel={args.channel} status={state.get('status')} endpoint={state.get('endpoint', '')}")
    rules = state.get("rules") or []
    if not rules:
        print("(no armed rules)")
    for rule in rules:
        print(
            f"- {rule.get('id')} [{rule.get('trigger')}] "
            f"pattern={rule.get('pattern')!r} send={rule.get('send_display')!r} "
            f"fires={rule.get('fires')}/{rule.get('max_fires') or '∞'}"
        )
    return 0


def cmd_hardware_stop(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    append_session_control(root, run_id, args.task_id, args.channel, {"cmd": "stop"})
    print(f"Queued stop on {args.channel}.")
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


def cmd_claude_runner(args: argparse.Namespace) -> int:
    return run_claude_task(args)


def cmd_codex_chat(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    return codex_chat(root, run_id, args)


def cmd_claude_chat(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    return claude_chat(root, run_id, args)


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
            proxy_enabled=args.proxy_enabled,
            http_proxy=args.http_proxy,
            https_proxy=args.https_proxy,
            no_proxy=args.no_proxy,
            collaboration_mode=args.collaboration_mode,
            workflow_template=args.workflow_template,
            delegation_policy=args.delegation_policy,
            max_sub_agents=args.max_sub_agents,
            preferred_sub_backend=args.preferred_sub_backend,
            preferred_sub_model=args.preferred_sub_model,
            description=args.description,
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
        recovery = run_stale_runtime_recovery(root, run_id, task_id=args.task_id, apply=False)
        recovered_count = 0
        for candidate in recovery.get("candidates") or []:
            try:
                applied = run_stale_runtime_recovery(
                    root,
                    run_id,
                    task_id=args.task_id,
                    agent_id=str(candidate.get("agent_id") or ""),
                    apply=True,
                )
            except RunRecoveryError:
                continue
            recovered_count += int(applied.get("recovered_count") or 0)
        suffix = f" Recovered {recovered_count} stale agent(s)." if recovered_count else ""
        print(f"{args.task_id} reopened. Follow-up messages are allowed again.{suffix}")
    elif args.task_cmd == "recover":
        if args.restart_backend and not args.apply:
            print("--restart-backend requires --apply", file=sys.stderr)
            return 2
        try:
            agent_id = args.agent_id
            if args.apply and not agent_id:
                dry = run_stale_runtime_recovery(root, run_id, task_id=args.task_id, apply=False)
                candidates = dry.get("candidates") or []
                if len(candidates) != 1:
                    print("Apply without --agent-id requires exactly one stale candidate", file=sys.stderr)
                    return 2
                agent_id = str(candidates[0].get("agent_id") or "")
            result = run_stale_runtime_recovery(
                root,
                run_id,
                task_id=args.task_id,
                agent_id=agent_id,
                apply=args.apply,
                restart_backend=args.restart_backend,
            )
        except RunRecoveryError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(format_stale_runtime_recovery(result), end="")
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
    elif args.session_cmd == "compact-reset":
        if not args.task_id:
            raise SystemExit("--task-id is required for compact-reset")
        payload = compact_reset_backend_session(
            root,
            run_id,
            args.task_id,
            args.agent_id,
            reason=args.reason,
            restart=args.restart,
            dry_run=args.dry_run,
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
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
    run_id = args.run_id or latest_run_id(root) or ""
    if run_id:
        require_plan(root, run_id)
    try:
        auth_token = resolve_auth_token(
            getattr(args, "auth_token", None) or os.environ.get("AHA_WEB_TOKEN"),
            getattr(args, "auth_token_file", None) or os.environ.get("AHA_WEB_TOKEN_FILE"),
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if bind_host_exposes_network(args.host) and not auth_token and not getattr(args, "allow_unsafe_bind", False):
        print(
            "warning: AHA Web UI is bound to a network-visible host without --auth-token or --auth-token-file; "
            "prefer --host 127.0.0.1 or enable token auth.",
            file=sys.stderr,
        )
    try:
        asyncio.run(run_ui_server(root, run_id, args.host, args.port, args.poll_interval, auth_token=auth_token))
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


def _generated_by_from_task_context(root: Path, run_id: str, task_id: str, agent_id: str) -> str | None:
    try:
        detail = task_snapshot(root, run_id, task_id)
    except (FileNotFoundError, KeyError, SystemExit):
        return None
    task = detail.get("task", {})
    agent = next((item for item in task.get("agents", []) if item.get("id") == agent_id), {})
    session = next((item for item in detail.get("sessions", []) if item.get("agent_id") == agent_id), {})
    backend = session.get("backend") or agent.get("backend") or task.get("preferred_backend")
    model = session.get("resolved_model") or session.get("model") or agent.get("model") or task.get("preferred_model")
    if not backend:
        return None
    return generated_by_for_backend_model(str(backend), str(model) if model is not None else None)


def _generated_by_from_runtime(args: argparse.Namespace) -> str | None:
    explicit = str(getattr(args, "generated_by", None) or "").strip()
    if explicit:
        return explicit
    env_generated_by = os.environ.get("AHA_GENERATED_BY", "").strip()
    if env_generated_by:
        return env_generated_by
    env_backend = os.environ.get("AHA_BACKEND", "").strip()
    if env_backend:
        return generated_by_for_backend_model(env_backend, os.environ.get("AHA_MODEL"))
    env_root = os.environ.get("AHA_ROOT", "").strip()
    run_id = os.environ.get("AHA_RUN_ID", "").strip()
    task_id = (str(getattr(args, "task_id", None) or "").strip() or os.environ.get("AHA_TASK_ID", "").strip())
    agent_id = (str(getattr(args, "agent", None) or "").strip() or os.environ.get("AHA_AGENT_ID", "").strip() or "main")
    if env_root and run_id and task_id:
        return _generated_by_from_task_context(Path(env_root).expanduser().resolve(), run_id, task_id, agent_id)
    return None


def _git_project_root(start: Path | None = None) -> Path | None:
    cwd = (start or Path.cwd()).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return Path(root).resolve() if root else None


def _commit_body_from_args(args: argparse.Namespace) -> str:
    parts = [str(item).strip() for item in getattr(args, "body", []) if str(item).strip()]
    body_file = str(getattr(args, "body_file", "") or "").strip()
    if body_file:
        parts.append(Path(body_file).expanduser().read_text(encoding="utf-8").strip())
    return "\n\n".join(part for part in parts if part)


def cmd_commit(args: argparse.Namespace) -> int:
    generated_by = _generated_by_from_runtime(args) or DEFAULT_GENERATED_BY
    try:
        body = _commit_body_from_args(args)
    except OSError as exc:
        print(f"Commit body error: {exc}", file=sys.stderr)
        return 2
    try:
        message = format_commit_message(
            args.type,
            args.summary,
            args.task_id,
            args.agent,
            scope=args.scope,
            aha_scope=args.aha_scope,
            body=body,
            generated_by=generated_by,
        )
    except ValueError as exc:
        print(f"Commit message error: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(message, end="")
        return 0
    root = _git_project_root()
    if root is None:
        print("Unable to locate a Git repository for aha commit.", file=sys.stderr)
        return 1
    add_paths = [path for group in args.add for path in group]
    if add_paths:
        subprocess.run(["git", "add", "--", *add_paths], cwd=root, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root, stderr=subprocess.PIPE, text=True)
    if staged.returncode == 0:
        print("No staged changes to commit. Stage files first or pass --add <path>.", file=sys.stderr)
        return 1
    if staged.returncode != 1:
        detail = f": {staged.stderr.strip()}" if staged.stderr.strip() else "."
        print(f"Unable to inspect staged changes with git diff --cached{detail}", file=sys.stderr)
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
    errors = validate_commit_message(message, expected_generated_by=_generated_by_from_runtime(args))
    if errors:
        for error in errors:
            print(f"commit message error: {error}", file=sys.stderr)
        return 1
    print("Commit message OK")
    return 0


def command_handlers() -> dict[str, object]:
    return {
        "init": cmd_init,
        "plan": cmd_plan,
        "run": cmd_run,
        "run-export": cmd_run_export,
        "run-import": cmd_run_import,
        "status": cmd_status,
        "collect": cmd_collect,
        "merge": cmd_merge,
        "list": cmd_list,
        "runs": cmd_runs,
        "workspace": cmd_workspace,
        "watch": cmd_watch,
        "send": cmd_send,
        "hardware-io": cmd_hardware_io,
        "hardware-attach": cmd_hardware_attach,
        "hardware-send": cmd_hardware_send,
        "hardware-arm": cmd_hardware_arm,
        "hardware-disarm": cmd_hardware_disarm,
        "hardware-rules": cmd_hardware_rules,
        "hardware-stop": cmd_hardware_stop,
        "chat": cmd_chat,
        "auto-reply": cmd_auto_reply,
        "commit": cmd_commit,
        "commit-check": cmd_commit_check,
        "package": cmd_package,
        "codex-runner": cmd_codex_runner,
        "claude-runner": cmd_claude_runner,
        "codex-chat": cmd_codex_chat,
        "claude-chat": cmd_claude_chat,
        "task": cmd_task,
        "agent": cmd_agent,
        "session": cmd_session,
        "serve": cmd_serve,
        "ui": cmd_ui,
    }


def build_parser() -> argparse.ArgumentParser:
    return build_cli_parser(command_handlers())


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    normalized = normalize_run_subcommand(list(sys.argv[1:] if argv is None else argv))
    args = parser.parse_args(with_default_command(normalized))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
