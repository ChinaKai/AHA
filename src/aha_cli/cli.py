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
from aha_cli.domain.models import default_config, utc_now
from aha_cli.services.chat import auto_reply, claude_chat, codex_chat
from aha_cli.services.claude_runner import run_claude_task
from aha_cli.services.commit_policy import DEFAULT_GENERATED_BY, format_commit_message, generated_by_for_backend_model, validate_commit_message
from aha_cli.services.codex_runner import run_codex_task
from aha_cli.services.hardware_io import append_hardware_io_record
from aha_cli.services.hardware_bridge import (
    append_bridge_control,
    bridge_status,
    device_stream_page,
    ensure_bridge,
    task_devices,
)
from aha_cli.services.messages import format_event
from aha_cli.services.observe_proxy import run_observe_proxy_server
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
from aha_cli.services.knowledge_git import sync as knowledge_sync
from aha_cli.store.knowledge import (
    approve_candidate as knowledge_approve_candidate,
    enqueue_candidate as knowledge_enqueue_candidate,
    entry_exists as knowledge_entry_exists,
    entry_path_for as knowledge_entry_path_for,
    find_entry as knowledge_find_entry,
    future_iso as knowledge_future_iso,
    init_knowledge_base,
    iter_all_entries as knowledge_iter_all_entries,
    knowledge_status,
    list_pending as knowledge_list_pending,
    list_stale_entries as knowledge_list_stale,
    read_entry as knowledge_read_entry,
    remove_pending as knowledge_remove_pending,
    search_entries as knowledge_search_entries,
    slugify as knowledge_slugify,
    type_for_kind as knowledge_type_for_kind,
    write_entry as knowledge_write_entry,
)
from aha_cli.store.sessions import backend_session_usage_archive_fields
from aha_cli.services.knowledge_git import auto_commit_after_change as knowledge_auto_commit
from aha_cli.web.server import run_ui_server
from aha_cli.web.task_command_actions import complete_selected_task
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


def cmd_kb(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    cfg = load_config(root)
    if args.kb_cmd == "init":
        result = init_knowledge_base(root, cfg)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            verb = "Created" if result["created"] else "Verified"
            print(f"{verb} knowledge base at {result['path']} (schema v{result['schema_version']})")
        return 0
    if args.kb_cmd == "map":
        from aha_cli.services.knowledge_navigation import generate_navigation_candidate

        workspace = args.workspace or os.getcwd()
        result = generate_navigation_candidate(
            root, cfg,
            workspace_path=workspace,
            goal=getattr(args, "goal", None),
            project_key_value=getattr(args, "project", None),
        )
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif result.get("skipped"):
            print(f"skipped: {result['skipped']}")
        else:
            gate = result.get("gate", "manual")
            where = "pending review queue" if gate == "manual" else "knowledge base"
            print(
                f"project navigation candidates from '{result.get('title')}' -> "
                f"{where} ({result.get('project_key')}, count={result.get('candidates', 0)})"
            )
        return 0
    if args.kb_cmd == "tutorial":
        from aha_cli.services.knowledge_distill import distill_and_enqueue, general_tutorial_candidate

        body = args.body
        if getattr(args, "body_file", None):
            body = Path(args.body_file).read_text(encoding="utf-8")
        if not (args.title.strip() and body.strip()):
            print("tutorial requires a non-empty --title and body (--body/--body-file)", file=sys.stderr)
            return 2
        candidate = general_tutorial_candidate(
            title=args.title, body=body, kind=args.kind, tags=args.tag or [],
            source={"source_type": "manual_tutorial"},
        )
        result = distill_and_enqueue(root, cfg, {}, candidates=[candidate])
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif result.get("skipped"):
            print(f"skipped: {result['skipped']}")
        else:
            gate = result.get("gate", "manual")
            where = "pending review queue" if gate == "manual" else "knowledge base (general)"
            print(f"general tutorial '{args.title}' -> {where}")
        return 0
    if args.kb_cmd == "capture":
        return _cmd_kb_capture(root, cfg, args)
    if args.kb_cmd == "status":
        status = knowledge_status(root, cfg)
        if getattr(args, "json", False):
            print(json.dumps(status, indent=2, ensure_ascii=False))
        else:
            print(format_knowledge_status(status), end="")
        return 0
    if args.kb_cmd == "pending":
        pending = knowledge_list_pending(root, cfg)
        if getattr(args, "json", False):
            print(json.dumps(pending, indent=2, ensure_ascii=False))
        else:
            if not pending:
                print("No pending candidates")
            for cand in pending:
                meta = cand.get("meta", {})
                print(
                    f"  {cand.get('id')}  [{cand.get('scope')}/{cand.get('kind')}] "
                    f"{cand.get('title')}  (project={cand.get('project_key') or '-'}, "
                    f"confidence={meta.get('confidence', '-')}, "
                    f"created={cand.get('created_at') or '-'}, updated={cand.get('updated_at') or '-'})"
                )
            print(f"total pending: {len(pending)}")
        return 0
    if args.kb_cmd == "sync":
        message = args.message or f"chore(knowledge): manual sync {utc_now()}"
        result = knowledge_sync(
            root,
            cfg,
            message=message,
            do_pull=not args.no_pull,
            do_push=True if args.push else None,
        )
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            steps = result.get("steps", {})
            for name, step in steps.items():
                detail = step.get("error") or step.get("reason") or step.get("skipped") or "ok"
                print(f"  {name}: {'ok' if step.get('ok', True) else 'FAIL'} ({detail})")
            print(f"sync {'ok' if result['ok'] else 'FAILED'}")
        return 0 if result["ok"] else 1
    if args.kb_cmd == "list":
        entries = knowledge_iter_all_entries(root, cfg)
        entries = [e for e in entries if _kb_entry_matches(e, args)]
        if getattr(args, "json", False):
            print(json.dumps([_kb_entry_summary(e) for e in entries], indent=2, ensure_ascii=False))
        else:
            if not entries:
                print("No entries")
            for entry in entries:
                meta = entry.get("meta", {})
                print(
                    f"  {meta.get('id') or meta.get('slug')}  [{meta.get('scope')}/{meta.get('type')}] "
                    f"{meta.get('title')}  (project={meta.get('project_key') or '-'})"
                )
            print(f"total: {len(entries)}")
        return 0
    if args.kb_cmd == "show":
        entry = knowledge_find_entry(root, cfg, args.identifier)
        if entry is None:
            print(f"Entry not found: {args.identifier}", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            print(json.dumps(entry, indent=2, ensure_ascii=False))
        else:
            meta = entry.get("meta", {})
            print(f"# {meta.get('title')}  ({meta.get('id') or meta.get('slug')})")
            print(f"scope={meta.get('scope')} type={meta.get('type')} project={meta.get('project_key') or '-'}")
            print(f"path: {entry.get('path')}")
            print()
            print(entry.get("body", ""))
        return 0
    if args.kb_cmd == "search":
        hits = knowledge_search_entries(root, cfg, args.query)
        if getattr(args, "json", False):
            print(json.dumps([_kb_entry_summary(e) for e in hits], indent=2, ensure_ascii=False))
        else:
            if not hits:
                print("No matches")
            for entry in hits:
                meta = entry.get("meta", {})
                print(f"  {meta.get('id') or meta.get('slug')}  {meta.get('title')}")
            print(f"matches: {len(hits)}")
        return 0
    if args.kb_cmd == "approve":
        pending = {c.get("id"): c for c in knowledge_list_pending(root, cfg)}
        candidate = pending.get(args.candidate_id)
        if candidate is None:
            print(f"No pending candidate: {args.candidate_id}", file=sys.stderr)
            return 1
        # Dedup awareness scoped to the exact target identity (scope + kind +
        # project + slug) — a same-named entry in another scope/project is NOT
        # the same entry and must report "created".
        cand_scope = candidate.get("scope", "project")
        cand_kind = candidate.get("kind", "solutions")
        cand_project = candidate.get("project_key")
        cand_slug = candidate.get("slug") or knowledge_slugify(candidate.get("title", ""))
        existing = knowledge_entry_exists(root, cfg, cand_scope, cand_kind, cand_project, cand_slug)
        try:
            entry_path = knowledge_approve_candidate(root, cfg, args.candidate_id)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        git_result = knowledge_auto_commit(
            root, f"chore(knowledge): approve '{candidate.get('title', 'entry')}'", cfg
        )
        action = "updated" if existing else "created"
        result = {"ok": True, "action": action, "path": str(entry_path), "git": git_result}
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"{action} entry: {entry_path}")
            if not git_result.get("skipped"):
                print(f"  git: {'committed' if git_result.get('committed') else git_result.get('reason') or git_result.get('error')}")
        return 0
    if args.kb_cmd == "reject":
        removed = knowledge_remove_pending(root, cfg, args.candidate_id)
        if not removed:
            print(f"No pending candidate: {args.candidate_id}", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            print(json.dumps({"ok": True, "rejected": args.candidate_id}, indent=2, ensure_ascii=False))
        else:
            print(f"rejected: {args.candidate_id}")
        return 0
    if args.kb_cmd == "add":
        if args.scope == "project" and args.kind == "wiki":
            print("project wiki entries are not supported; use project navigation docs or project solutions", file=sys.stderr)
            return 2
        if args.scope != "project" and args.kind == "worklog":
            print("worklog entries are only supported for --scope project", file=sys.stderr)
            return 2
        if args.scope == "project" and not args.project:
            print("--project is required for --scope project", file=sys.stderr)
            return 2
        body = args.body
        if args.body_file:
            body = Path(args.body_file).read_text(encoding="utf-8")
        meta: dict = {}
        if args.tag:
            meta["tags"] = args.tag
        if args.review_days is not None:
            meta["review_after"] = knowledge_future_iso(args.review_days)
        if getattr(args, "confidence", None) is not None:
            meta["confidence"] = args.confidence
        if getattr(args, "pending", False):
            if args.append:
                print("--append is only supported when writing approved entries, not pending candidates", file=sys.stderr)
                return 2
            if not body.strip():
                print("pending candidate requires a non-empty body (--body/--body-file)", file=sys.stderr)
                return 2
            init_knowledge_base(root, cfg)
            source = {"source_type": args.source_type or "manual_cli"}
            if args.source_run:
                source["run_id"] = args.source_run
            if args.source_task:
                source["task_id"] = args.source_task
            if args.source_agent:
                source["agent_id"] = args.source_agent
            path = knowledge_enqueue_candidate(
                root,
                cfg,
                {
                    "kind": args.kind,
                    "scope": args.scope,
                    "project_key": args.project if args.scope == "project" else None,
                    "title": args.title,
                    "body": body,
                    "meta": meta,
                    "source": source,
                },
            )
            record = read_json(path)
            result = {"ok": True, "action": "pending", "path": str(path), "candidate_id": record.get("id")}
            if getattr(args, "json", False):
                print(json.dumps(result | {"candidate": record}, indent=2, ensure_ascii=False))
            else:
                print(f"pending candidate: {record.get('id')} {path}")
            return 0
        slug = knowledge_slugify(args.title)
        existing_path = knowledge_entry_path_for(root, cfg, args.scope, args.kind, args.project, slug)
        action = "created"
        if existing_path is not None:
            action = "appended" if args.append else "updated"
            existing = knowledge_read_entry(existing_path)
            # Preserve existing metadata on any update; only the params the user
            # actually passed (collected in `meta`) override.
            carried = {
                k: v for k, v in existing["meta"].items()
                if k in ("tags", "review_after", "outcome", "confidence", "related_files", "source_tasks")
            }
            carried.update(meta)
            meta = carried
            if args.append:
                body = (
                    existing["body"].rstrip()
                    + f"\n\n---\n_(追加于 {utc_now()})_\n\n"
                    + body.strip()
                    + "\n"
                )
        path = knowledge_write_entry(
            root, config=cfg, scope=args.scope, kind=args.kind,
            project_key_value=args.project, title=args.title, body=body, meta=meta,
        )
        git_result = knowledge_auto_commit(root, f"chore(knowledge): {action} '{args.title}'", cfg)
        result = {"ok": True, "action": action, "path": str(path), "git": git_result}
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"{action} entry: {path}")
        return 0
    if args.kb_cmd == "stale":
        stale = knowledge_list_stale(root, cfg)
        if getattr(args, "json", False):
            print(json.dumps([_kb_entry_summary(e) for e in stale], indent=2, ensure_ascii=False))
        else:
            if not stale:
                print("No stale entries")
            for entry in stale:
                meta = entry.get("meta", {})
                print(f"  {meta.get('id') or meta.get('slug')}  {meta.get('title')}  (review_after={meta.get('review_after')})")
            print(f"stale: {len(stale)}")
        return 0
    return 0


def _capture_text_arg(args: argparse.Namespace) -> str | None:
    """Resolve --text / --text-file ('-' = stdin) into note text, or None."""
    if getattr(args, "text_file", None):
        if args.text_file == "-":
            return sys.stdin.read()
        return Path(args.text_file).read_text(encoding="utf-8")
    text = getattr(args, "text", None)
    return text if text else None


def _cmd_kb_capture(root: Path, cfg: dict, args: argparse.Namespace) -> int:
    from aha_cli.store import knowledge_capture as cap

    is_json = getattr(args, "json", False)
    sub = args.kb_capture_cmd
    if sub == "add":
        text = _capture_text_arg(args)
        if not text or not text.strip():
            print("capture add requires --text or --text-file", file=sys.stderr)
            return 2
        note = cap.create_note(root, cfg, text=text, scope_hint=args.scope, title=args.title)
        if is_json:
            print(json.dumps(note, indent=2, ensure_ascii=False))
        else:
            print(f"captured note {note['id']} (scope_hint={note['scope_hint']}, status={note['status']})")
        return 0
    if sub == "list":
        notes = cap.list_notes(root, cfg)
        if is_json:
            print(json.dumps(notes, indent=2, ensure_ascii=False))
        else:
            if not notes:
                print("No capture notes")
            for n in notes:
                preview = " ".join((n.get("text") or "").split())[:60]
                print(f"  {n['id']}  [{n.get('scope_hint')}/{n.get('status')}]  {n.get('title') or preview}")
            print(f"capture notes: {len(notes)}")
        return 0
    if sub == "show":
        note = cap.read_note(root, cfg, args.note_id)
        if note is None:
            print(f"capture note not found: {args.note_id}", file=sys.stderr)
            return 1
        if is_json:
            print(json.dumps(note, indent=2, ensure_ascii=False))
        else:
            print(note.get("text") or "")
        return 0
    if sub == "edit":
        text = _capture_text_arg(args)
        try:
            note = cap.update_note(
                root, cfg, args.note_id,
                text=text, scope_hint=args.scope, title=args.title,
            )
        except FileNotFoundError:
            print(f"capture note not found: {args.note_id}", file=sys.stderr)
            return 1
        if is_json:
            print(json.dumps(note, indent=2, ensure_ascii=False))
        else:
            print(f"updated note {note['id']}")
        return 0
    if sub == "rm":
        removed = cap.delete_note(root, cfg, args.note_id)
        if is_json:
            print(json.dumps({"ok": removed, "id": args.note_id}, ensure_ascii=False))
        else:
            print(f"removed {args.note_id}" if removed else f"capture note not found: {args.note_id}")
        return 0 if removed else 1
    if sub == "distill":
        from aha_cli.services.knowledge_capture_distill import distill_note

        result = distill_note(root, cfg, args.note_id, backend=args.backend, model=args.model)
        if is_json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif not result.get("ok"):
            print(result.get("error", "distill failed"), file=sys.stderr)
        else:
            print(f"note {args.note_id} distilled -> {result['candidates']} candidate(s) in pending review queue")
        return 0 if result.get("ok") else 1
    return 0


def _kb_entry_matches(entry: dict, args: argparse.Namespace) -> bool:
    meta = entry.get("meta", {})
    if getattr(args, "scope", None) and meta.get("scope") != args.scope:
        return False
    if getattr(args, "kind", None) and meta.get("type") != knowledge_type_for_kind(args.kind):
        return False
    if getattr(args, "project", None) and meta.get("project_key") != args.project:
        return False
    return True


def _kb_entry_summary(entry: dict) -> dict:
    meta = entry.get("meta", {})
    return {
        "id": meta.get("id"),
        "slug": meta.get("slug"),
        "title": meta.get("title"),
        "scope": meta.get("scope"),
        "type": meta.get("type"),
        "project_key": meta.get("project_key"),
        "tags": meta.get("tags", []),
        "review_after": meta.get("review_after"),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "size_bytes": entry.get("size_bytes"),
        "path": entry.get("path"),
    }


def format_knowledge_status(status: dict) -> str:
    lines = [
        f"Knowledge base: {status['path']}",
        f"  initialized: {status['initialized']}  enabled: {status['enabled']}  schema: v{status['schema_version']}",
    ]
    git = status.get("git", {})
    lines.append(
        f"  git: repo={git.get('is_repo')} enabled={git.get('enabled')} "
        f"remote={git.get('remote') or '-'} branch={git.get('branch') or '-'}"
    )
    lines.append(f"  curation gate: {status.get('curation_gate')}")
    general = status.get("general", {})
    lines.append(
        f"  general: wiki={general.get('wiki', 0)} "
        f"solutions={general.get('solutions', 0)}"
    )
    personal = status.get("personal", {})
    lines.append(
        f"  personal: wiki={personal.get('wiki', 0)} "
        f"solutions={personal.get('solutions', 0)}"
    )
    projects = status.get("projects", [])
    if projects:
        lines.append(f"  projects ({len(projects)}):")
        for proj in projects:
            counts = proj["counts"]
            lines.append(
                f"    - {proj['project_key']}: wiki={counts.get('wiki', 0)} "
                f"solutions={counts.get('solutions', 0)} navigation={counts.get('navigation', 0)} "
                f"worklog={counts.get('worklog', 0)}"
            )
    else:
        lines.append("  projects: none")
    lines.append(f"  pending candidates: {status.get('pending', 0)}")
    lines.append(f"  stale (need review): {status.get('stale', 0)}")
    lines.append(f"  total entries: {status.get('total_entries', 0)}")
    return "\n".join(lines) + "\n"


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


def cmd_hardware_bridge(args: argparse.Namespace) -> int:
    from aha_cli.services.hardware_bridge import DeviceBridgeDaemon

    root = command_aha_home(args)
    daemon = DeviceBridgeDaemon(root, args.device, args.baudrate)
    try:
        daemon.run()
    except KeyboardInterrupt:
        daemon._running = False
    return 0


def _bridge_target(root, run_id: str, task_id: str) -> tuple[str | None, int]:
    """Resolve the (device, baudrate) a task's UART channel points at.

    The CLI helpers drive the machine-level device bridge, so the device is taken
    from the task's hardware-channel config rather than re-supplied per command.
    """

    from aha_cli.store.snapshots import task_lookup

    try:
        _plan, task, _run = task_lookup(root, run_id, task_id)
    except Exception:
        return None, 115200
    devices = task_devices(task)
    return devices[0] if devices else (None, 115200)


def _tail_device_stream(root, device: str) -> None:
    """Print the device bridge's RX/TX stream to stdout until interrupted."""

    import time

    after: int | None = None
    while True:
        page = device_stream_page(root, device, after=after, limit=1000)
        after = page.get("after_offset", after)
        for event in page.get("events") or []:
            direction = str(event.get("direction") or "system")
            data = str(event.get("data") or "")
            if direction == "rx":
                sys.stdout.write(data)
            elif direction == "tx":
                sys.stdout.write(data)
            else:
                sys.stdout.write(f"\n‹{direction} {event.get('source', '')}› {data}\n")
        sys.stdout.flush()
        time.sleep(0.2)


def cmd_hardware_attach(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    device = str(args.device or "").strip()
    baudrate = int(args.baudrate)
    if not device:
        device, baudrate = _bridge_target(root, run_id, args.task_id)
    if not device:
        print("hardware-attach requires --device (e.g. /dev/ttyUSB0) or a UART channel on the task", file=sys.stderr)
        return 2
    status = ensure_bridge(root, device, baudrate)
    print(
        f"Bridge owns {device}@{baudrate} (status={status.get('status')}). "
        "Streaming RX; Ctrl-C to stop watching (the bridge keeps holding the port). "
        "Use `aha hardware-stop` to release it."
    )
    try:
        _tail_device_stream(root, device)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_hardware_send(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    device, baudrate = _bridge_target(root, run_id, args.task_id)
    if not device:
        print(f"No UART device configured on task {args.task_id}.", file=sys.stderr)
        return 2
    ensure_bridge(root, device, baudrate)
    append_bridge_control(root, device, {"cmd": "send", "data": args.data, "source": "interactive"})
    print(f"Queued send on {device}: {args.data!r}")
    return 0


def cmd_hardware_arm(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    device, baudrate = _bridge_target(root, run_id, args.task_id)
    if not device:
        print(f"No UART device configured on task {args.task_id}.", file=sys.stderr)
        return 2
    ensure_bridge(root, device, baudrate)
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
    record = append_bridge_control(root, device, command)
    print(f"Queued arm on {device}: {json.dumps(record, ensure_ascii=False)}")
    return 0


def cmd_hardware_disarm(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    device, _baudrate = _bridge_target(root, run_id, args.task_id)
    if not device:
        print(f"No UART device configured on task {args.task_id}.", file=sys.stderr)
        return 2
    append_bridge_control(root, device, {"cmd": "disarm", "id": args.id})
    print(f"Queued disarm on {device}: rule {args.id}")
    return 0


def cmd_hardware_rules(args: argparse.Namespace) -> int:
    root = command_aha_home(args)
    run_id = resolve_run_id(root, args.run_id)
    require_plan(root, run_id)
    device, _baudrate = _bridge_target(root, run_id, args.task_id)
    state = bridge_status(root, device) if device else {"status": "stopped", "rules": []}
    if args.json:
        print(json.dumps(state, ensure_ascii=False))
        return 0
    print(f"device={device} status={state.get('status')} paused={state.get('paused', False)}")
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
    device, _baudrate = _bridge_target(root, run_id, args.task_id)
    if not device:
        print(f"No UART device configured on task {args.task_id}.", file=sys.stderr)
        return 2
    append_bridge_control(root, device, {"cmd": "stop"})
    print(f"Queued stop on {device} (bridge will release the port).")
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


def cmd_observe_proxy(args: argparse.Namespace) -> int:
    return run_observe_proxy_server(args)


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
    elif args.task_cmd == "complete":
        message, payload = complete_selected_task(root, run_id, args.task_id)
        print(message)
        if not payload.get("ok"):
            return 1
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
                old_backend_session_id = session.get("backend_session_id")
                if old_backend_session_id:
                    history = session.get("history_backend_sessions")
                    if not isinstance(history, list):
                        history = []
                    reset_at = utc_now()
                    history.append(
                        {
                            "backend_session_id": old_backend_session_id,
                            "backend": session.get("backend"),
                            "model": session.get("model"),
                            "started_at": session.get("created_at"),
                            "archived_at": reset_at,
                            "reason": "manual_reset",
                        }
                        | backend_session_usage_archive_fields(
                            root,
                            run_id,
                            session.get("task_id"),
                            args.agent_id,
                            backend_session_id=old_backend_session_id,
                            backend=session.get("backend"),
                            history=history,
                        )
                    )
                    session["history_backend_sessions"] = history
                    session["updated_at"] = reset_at
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
        "kb": cmd_kb,
        "watch": cmd_watch,
        "send": cmd_send,
        "hardware-io": cmd_hardware_io,
        "hardware-bridge": cmd_hardware_bridge,
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
        "observe-proxy": cmd_observe_proxy,
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
