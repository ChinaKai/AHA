from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping

from aha_cli.backends.registry import agent_backend_names, backend_names
from aha_cli.domain.run_lifecycle import RUN_LIFECYCLE_CHOICES
from aha_cli.domain.workflow_templates import workflow_template_ids
from aha_cli.services.app_version import aha_version
from aha_cli.services.commit_policy import CONVENTIONAL_TYPES
from aha_cli.services.prompt_templates import render_prompt_template

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
    "runs",
    "workspace",
    "watch",
    "send",
    "chat",
    "auto-reply",
    "commit",
    "commit-check",
    "package",
    "codex-runner",
    "claude-runner",
    "codex-chat",
    "claude-chat",
    "task",
    "agent",
    "session",
    "serve",
    "ui",
}


def add_codex_options(parser: argparse.ArgumentParser, prefix: str = "codex") -> None:
    parser.add_argument(f"--{prefix}-bin", default=None if prefix == "codex" else "codex")
    parser.add_argument("--model" if prefix != "codex" else "--codex-model", default=None)
    sandbox_flag = "--sandbox" if prefix != "codex" else "--codex-sandbox"
    approval_flag = "--approval" if prefix != "codex" else "--codex-approval"
    parser.add_argument(sandbox_flag, choices=["auto", "read-only", "workspace-write", "danger-full-access"], default=None if prefix == "codex" else "read-only")
    parser.add_argument(approval_flag, choices=["untrusted", "on-failure", "on-request", "never"], default=None if prefix == "codex" else "never")


def build_parser(handlers: Mapping[str, Callable[[argparse.Namespace], int]]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aha", description="Agent-help-agent CLI prototype")
    parser.add_argument("--home", default=None, help="AHA_HOME data directory. Defaults to $AHA_HOME, a nearby .aha, or ~/.aha.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {aha_version() or 'unknown'}")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="Initialize AHA metadata")
    init_p.add_argument("path", nargs="?", default=".", help="Project path for --portable initialization")
    init_p.add_argument("--portable", action="store_true", help="Initialize PATH/.aha instead of the default AHA_HOME")
    init_p.add_argument("--force", action="store_true")
    init_p.add_argument("--backend", choices=backend_names(), default=None)
    init_p.add_argument("--runner-command", default=None)
    init_p.add_argument("--parallel", type=int, default=10)
    init_p.set_defaults(func=handlers["init"])

    plan_p = sub.add_parser("plan", help="Create a multi-agent task plan")
    plan_p.add_argument("goal")
    plan_p.add_argument("--agents", type=int, default=4)
    plan_p.add_argument("--mode", choices=["research", "implementation"], default="research")
    plan_p.add_argument("--collaboration-mode", choices=["auto", "solo", "pair", "team"], default="auto")
    plan_p.add_argument("--workflow-template", choices=workflow_template_ids(), default="auto")
    plan_p.add_argument("--task", action="append")
    plan_p.add_argument("--write-scope", action="append")
    plan_p.add_argument("--workspace", default=None, help="Registered workspace id, such as ws-001")
    plan_p.add_argument("--workspace-path", default=None, help="Workspace path for the created tasks")
    plan_p.add_argument("--enable-proxy", dest="proxy_enabled", action="store_true", help="Enable task proxy for created agents")
    plan_p.add_argument("--http-proxy", default=None)
    plan_p.add_argument("--https-proxy", default=None)
    plan_p.add_argument("--no-proxy", default=None, help="NO_PROXY value for created tasks")
    plan_p.set_defaults(func=handlers["plan"])

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
    run_p.add_argument("--claude-bin", default=None)
    run_p.add_argument("--claude-model", default=None)
    run_p.add_argument("--claude-sandbox", choices=["auto", "read-only", "workspace-write", "danger-full-access"], default=None)
    run_p.add_argument("--claude-permission-mode", choices=["default", "acceptEdits", "bypassPermissions", "plan"], default=None)
    run_p.add_argument("--claude-extra-arg", action="append", default=[])
    run_p.set_defaults(func=handlers["run"])

    run_export_p = sub.add_parser("run-export", help=argparse.SUPPRESS)
    run_export_p.add_argument("run_id", nargs="?")
    run_export_p.add_argument("--output", "-o", default=None)
    run_export_p.add_argument("--no-logs", action="store_true")
    run_export_p.set_defaults(func=handlers["run-export"])

    run_import_p = sub.add_parser("run-import", help=argparse.SUPPRESS)
    run_import_p.add_argument("archive")
    run_import_p.add_argument("--run-id", default=None)
    run_import_p.add_argument("--preserve-id", action="store_true")
    run_import_p.add_argument("--force", action="store_true")
    run_import_p.set_defaults(func=handlers["run-import"])

    for name in ("status", "collect", "merge"):
        p = sub.add_parser(name)
        p.add_argument("run_id", nargs="?")
        if name == "merge":
            p.add_argument("--output", "-o")
        p.set_defaults(func=handlers[name])
    sub.add_parser("list", help="List runs").set_defaults(func=handlers["list"])

    runs_p = sub.add_parser("runs", help="Manage runs")
    runs_sub = runs_p.add_subparsers(dest="runs_cmd", required=True)
    runs_cleanup = runs_sub.add_parser("cleanup", help="List or clean stale temporary run leftovers")
    runs_cleanup.add_argument("--current-run", default=None, help="Run id that must never be deleted; defaults to $AHA_RUN_ID")
    runs_cleanup.add_argument("--tmp-root", default="/tmp", help="Temporary root to scan for nested .aha homes")
    runs_cleanup.add_argument("--stale-seconds", type=int, default=3600, help="Minimum age before a temporary candidate is deletable")
    runs_cleanup.add_argument("--active-heartbeat-seconds", type=int, default=120, help="Fresh heartbeat window that protects a run")
    runs_cleanup.add_argument("--allow-non-temp-root", action="store_true", help="Allow scanning a tmp-root outside the system temporary directory")
    cleanup_mode = runs_cleanup.add_mutually_exclusive_group()
    cleanup_mode.add_argument("--apply", action="store_true", help="Actually delete candidates. Without this, cleanup is dry-run/list only.")
    cleanup_mode.add_argument("--dry-run", action="store_true", help="List candidates without deleting files. This is the default.")
    runs_cleanup.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    runs_cleanup.set_defaults(func=handlers["runs"])
    runs_diagnose = runs_sub.add_parser("diagnose", help="Diagnose runs and AHA service state without changing files")
    runs_diagnose.add_argument("--current-run", default=None, help="Run id to treat as current; defaults to $AHA_RUN_ID")
    runs_diagnose.add_argument("--stale-seconds", type=int, default=3600, help="Cleanup staleness window used for explanation")
    runs_diagnose.add_argument("--active-heartbeat-seconds", type=int, default=120, help="Fresh heartbeat window used for explanation")
    runs_diagnose.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    runs_diagnose.set_defaults(func=handlers["runs"])
    runs_lifecycle = runs_sub.add_parser("lifecycle", help="Set lifecycle status for an inactive run")
    runs_lifecycle.add_argument("run_id")
    runs_lifecycle.add_argument("status", choices=RUN_LIFECYCLE_CHOICES)
    runs_lifecycle.add_argument("--current-run", default=None, help="Run id that cannot be changed; defaults to $AHA_RUN_ID")
    runs_lifecycle.add_argument("--active-heartbeat-seconds", type=int, default=120, help="Fresh heartbeat window that protects a run")
    runs_lifecycle.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    runs_lifecycle.set_defaults(func=handlers["runs"])
    runs_delete = runs_sub.add_parser("delete", help="Delete a non-current run directory")
    runs_delete.add_argument("run_id")
    runs_delete.add_argument("--current-run", default=None, help="Run id that must never be deleted; defaults to $AHA_RUN_ID")
    runs_delete.add_argument("--active-heartbeat-seconds", type=int, default=120, help="Fresh heartbeat window that protects a run unless --force is used")
    runs_delete.add_argument("--force", action="store_true", help="Delete even when the run has an active browser heartbeat")
    runs_delete.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    runs_delete.set_defaults(func=handlers["runs"])
    runs_retention = runs_sub.add_parser("retention", help="Report or apply guarded run retention/compaction")
    runs_retention.add_argument("run_id")
    runs_retention.add_argument("--top", type=int, default=10, help="Number of largest files to include")
    runs_retention.add_argument("--current-run", default=None, help="Run id that cannot be compacted; defaults to $AHA_RUN_ID")
    runs_retention.add_argument("--active-heartbeat-seconds", type=int, default=120, help="Fresh heartbeat window that protects a run from apply")
    runs_retention.add_argument("--min-age-seconds", type=int, default=0, help="Only archive files at least this old")
    runs_retention.add_argument("--max-total-bytes", type=int, default=0, help="Policy alert when total run bytes exceed this limit")
    runs_retention.add_argument("--max-candidate-bytes", type=int, default=0, help="Policy alert when candidate bytes exceed this limit")
    runs_retention.add_argument("--min-candidate-files", type=int, default=0, help="Policy alert when at least this many files are candidates")
    runs_retention.add_argument("--archive-dir", default=None, help="Directory for generated retention archives; defaults to RUN/retention")
    runs_retention.add_argument("--include-chat", action="store_true", help="Also include chat transcripts in retention candidates")
    runs_retention.add_argument("--apply", action="store_true", help="Create a compressed retention archive. Without --force, originals are kept.")
    runs_retention.add_argument("--apply-if-over-limit", action="store_true", help="Create a retention archive only when policy thresholds alert")
    runs_retention.add_argument("--force", action="store_true", help="With --apply, delete archived candidates after the archive is written")
    runs_retention.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    runs_retention.set_defaults(func=handlers["runs"])
    runs_retention_policy = runs_sub.add_parser("retention-policy", help="Report or enforce retention thresholds across all runs")
    runs_retention_policy.add_argument("--top", type=int, default=10, help="Number of largest files to inspect per run")
    runs_retention_policy.add_argument("--current-run", default=None, help="Run id that cannot be compacted; defaults to $AHA_RUN_ID")
    runs_retention_policy.add_argument("--active-heartbeat-seconds", type=int, default=120, help="Fresh heartbeat window that protects runs from apply")
    runs_retention_policy.add_argument("--min-age-seconds", type=int, default=0, help="Only archive files at least this old")
    runs_retention_policy.add_argument("--max-total-bytes", type=int, default=0, help="Policy alert when total run bytes exceed this limit")
    runs_retention_policy.add_argument("--max-candidate-bytes", type=int, default=0, help="Policy alert when candidate bytes exceed this limit")
    runs_retention_policy.add_argument("--min-candidate-files", type=int, default=0, help="Policy alert when at least this many files are candidates")
    runs_retention_policy.add_argument("--archive-dir", default=None, help="Directory for generated retention archives; defaults to each RUN/retention")
    runs_retention_policy.add_argument("--report-dir", default=None, help="Directory for persisted retention-policy reports; defaults to AHA_HOME/reports/retention-policy")
    runs_retention_policy.add_argument("--include-chat", action="store_true", help="Also include chat transcripts in retention candidates")
    runs_retention_policy.add_argument("--apply-if-over-limit", action="store_true", help="Create retention archives for eligible runs only when thresholds alert")
    runs_retention_policy.add_argument("--write-report", action="store_true", help="Persist the retention-policy report for scheduled/long-term review")
    runs_retention_policy.add_argument("--force", action="store_true", help="With --apply-if-over-limit, delete archived candidates after the archive is written")
    runs_retention_policy.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    runs_retention_policy.set_defaults(func=handlers["runs"])
    runs_retention_archive = runs_sub.add_parser("retention-archive", help="List, inspect, or restore retention archives")
    retention_archive_sub = runs_retention_archive.add_subparsers(dest="retention_archive_cmd", required=True)
    retention_archive_list = retention_archive_sub.add_parser("list", help="List retention archives")
    retention_archive_list.add_argument("run_id", nargs="?", default=None, help="Run id whose default retention archive directory should be scanned")
    retention_archive_list.add_argument("--archive-dir", default=None, help="Directory to scan instead of RUN/retention")
    retention_archive_list.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    retention_archive_list.set_defaults(func=handlers["runs"])
    retention_archive_inspect = retention_archive_sub.add_parser("inspect", help="Inspect a retention archive manifest")
    retention_archive_inspect.add_argument("archive")
    retention_archive_inspect.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    retention_archive_inspect.set_defaults(func=handlers["runs"])
    retention_archive_restore = retention_archive_sub.add_parser("restore", help="Restore files from a retention archive")
    retention_archive_restore.add_argument("archive")
    retention_archive_restore.add_argument("--run-id", default=None, help="Run id to restore into; defaults to the archive source run")
    retention_archive_restore.add_argument("--current-run", default=None, help="Run id that cannot be changed; defaults to $AHA_RUN_ID")
    retention_archive_restore.add_argument("--active-heartbeat-seconds", type=int, default=120, help="Fresh heartbeat window that protects a run from restore")
    retention_archive_restore.add_argument("--force", action="store_true", help="Overwrite existing files during restore")
    retention_archive_restore.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    retention_archive_restore.set_defaults(func=handlers["runs"])
    runs_recover = runs_sub.add_parser("recover", help="Dry-run or apply stale runtime recovery")
    runs_recover.add_argument("run_id")
    runs_recover.add_argument("--task-id", default=None, help="Task id to recover; required with --apply")
    runs_recover.add_argument("--agent-id", default=None, help="Agent id to recover; required with --apply")
    recover_mode = runs_recover.add_mutually_exclusive_group()
    recover_mode.add_argument("--apply", action="store_true", help="Mark the exact stale running agent as interrupted")
    recover_mode.add_argument("--dry-run", action="store_true", help="List stale runtime candidates without changing state. This is the default.")
    runs_recover.add_argument("--restart-backend", action="store_true", help="With --apply, enqueue a recovery resume message and restart the agent backend")
    runs_recover.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    runs_recover.set_defaults(func=handlers["runs"])

    workspace_p = sub.add_parser("workspace", help="Manage registered workspaces")
    workspace_sub = workspace_p.add_subparsers(dest="workspace_cmd", required=True)
    workspace_add = workspace_sub.add_parser("add")
    workspace_add.add_argument("path")
    workspace_add.add_argument("--name", default=None)
    workspace_add.set_defaults(func=handlers["workspace"])
    workspace_list = workspace_sub.add_parser("list")
    workspace_list.set_defaults(func=handlers["workspace"])

    watch_p = sub.add_parser("watch")
    watch_p.add_argument("run_id", nargs="?")
    watch_p.add_argument("--interval", type=float, default=1.0)
    watch_p.add_argument("--once", action="store_true")
    watch_p.add_argument("--tail", action="store_true")
    watch_p.add_argument("--event-limit", type=int, default=WATCH_EVENTS_LIMIT)
    watch_p.set_defaults(func=handlers["watch"])

    send_p = sub.add_parser("send")
    send_p.add_argument("run_id")
    send_p.add_argument("target")
    send_p.add_argument("message", nargs="+")
    send_p.add_argument("--sender", default="main")
    send_p.set_defaults(func=handlers["send"])

    chat_p = sub.add_parser("chat")
    chat_p.add_argument("run_id")
    chat_p.add_argument("target")
    chat_p.add_argument("--sender", default="main")
    chat_p.set_defaults(func=handlers["chat"])

    auto_p = sub.add_parser("auto-reply")
    auto_p.add_argument("run_id", nargs="?")
    auto_p.add_argument("target", nargs="?", default="main")
    auto_p.add_argument("--sender", default="main")
    auto_p.add_argument("--reply-target", default=None)
    auto_p.add_argument("--template", default="收到：{message}")
    auto_p.add_argument("--interval", type=float, default=1.0)
    auto_p.add_argument("--from-start", action="store_true")
    auto_p.add_argument("--once", action="store_true")
    auto_p.set_defaults(func=handlers["auto-reply"])

    commit_p = sub.add_parser("commit", help="Commit staged or selected files with AHA Conventional Commit metadata")
    commit_p.add_argument("--type", choices=CONVENTIONAL_TYPES, required=True)
    commit_p.add_argument("--scope", default=None)
    commit_p.add_argument("--summary", required=True)
    commit_p.add_argument("--body", action="append", default=[], help="Commit body paragraph; repeatable")
    commit_p.add_argument("--body-file", default=None, help="Read commit body from a file")
    commit_p.add_argument("--generated-by", default=None)
    commit_p.add_argument("--task-id", default=None, help="Deprecated; task tracking stays in the AHA journal")
    commit_p.add_argument("--agent", default=None, help="Deprecated; agent tracking stays in the AHA journal")
    commit_p.add_argument("--aha-scope", default=None, help="Deprecated; scope tracking stays in the AHA journal")
    commit_p.add_argument("--add", nargs="+", action="append", default=[], help="Path(s) to stage before committing; repeatable")
    commit_p.add_argument("--dry-run", action="store_true", help="Print the generated commit message without committing")
    commit_p.set_defaults(func=handlers["commit"])

    commit_check_p = sub.add_parser("commit-check", help="Validate an AHA commit message file")
    commit_check_p.add_argument("--generated-by", default=None, help="Expected Generated-by trailer value")
    commit_check_p.add_argument("message_file", help="Commit message file path, or '-' for stdin")
    commit_check_p.set_defaults(func=handlers["commit-check"])

    package_p = sub.add_parser("package", help="Build distributable artifacts")
    package_sub = package_p.add_subparsers(dest="package_cmd", required=True)
    onebin_p = package_sub.add_parser("onebin", help="Build a single-file executable zipapp")
    onebin_p.add_argument("--output", "-o", default="dist/aha", help="Output executable path")
    onebin_p.add_argument("--interpreter", default="/usr/bin/env python3", help="Shebang interpreter for the artifact")
    onebin_p.add_argument("--no-compress", action="store_true", help="Store files without ZIP compression")
    onebin_p.add_argument("--source-root", default=None, help=argparse.SUPPRESS)
    onebin_p.set_defaults(func=handlers["package"])

    codex_runner_p = sub.add_parser("codex-runner")
    codex_runner_p.add_argument("--codex-bin", default="codex")
    codex_runner_p.add_argument("--model", default=None)
    codex_runner_p.add_argument("--sandbox", choices=["auto", "read-only", "workspace-write", "danger-full-access"], default="auto")
    codex_runner_p.add_argument("--approval", choices=["untrusted", "on-failure", "on-request", "never"], default="never")
    codex_runner_p.add_argument("--extra-arg", action="append", default=[])
    codex_runner_p.add_argument("--no-json", action="store_true")
    codex_runner_p.set_defaults(func=handlers["codex-runner"])

    claude_runner_p = sub.add_parser("claude-runner")
    claude_runner_p.add_argument("--claude-bin", default="claude")
    claude_runner_p.add_argument("--model", default=None)
    claude_runner_p.add_argument("--sandbox", choices=["auto", "read-only", "workspace-write", "danger-full-access"], default="auto")
    claude_runner_p.add_argument("--permission-mode", choices=["default", "acceptEdits", "bypassPermissions", "plan"], default=None)
    claude_runner_p.add_argument("--extra-arg", action="append", default=[])
    claude_runner_p.set_defaults(func=handlers["claude-runner"])

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
    codex_chat_p.add_argument("--requested-model", default=None)
    codex_chat_p.add_argument("--sandbox", choices=["auto", "read-only", "workspace-write", "danger-full-access"], default="workspace-write")
    codex_chat_p.add_argument("--approval", choices=["untrusted", "on-failure", "on-request", "never"], default="never")
    codex_chat_p.add_argument("--extra-arg", action="append", default=[])
    codex_chat_p.add_argument("--no-json", action="store_true")
    codex_chat_p.add_argument("--prompt-prefix", default=render_prompt_template("backend_prompt_prefix.md").strip())
    codex_chat_p.set_defaults(func=handlers["codex-chat"])

    claude_chat_p = sub.add_parser("claude-chat")
    claude_chat_p.add_argument("run_id", nargs="?")
    claude_chat_p.add_argument("target", nargs="?", default="main")
    claude_chat_p.add_argument("--task-id", default=None)
    claude_chat_p.add_argument("--sender", default="main")
    claude_chat_p.add_argument("--reply-target", default=None)
    claude_chat_p.add_argument("--interval", type=float, default=1.0)
    claude_chat_p.add_argument("--from-start", action="store_true")
    claude_chat_p.add_argument("--once", action="store_true")
    claude_chat_p.add_argument("--claude-bin", default="claude")
    claude_chat_p.add_argument("--model", default=None)
    claude_chat_p.add_argument("--sandbox", choices=["auto", "read-only", "workspace-write", "danger-full-access"], default="workspace-write")
    claude_chat_p.add_argument("--approval", choices=["untrusted", "on-failure", "on-request", "never"], default="never")
    claude_chat_p.add_argument("--extra-arg", action="append", default=[])
    claude_chat_p.add_argument("--prompt-prefix", default=render_prompt_template("backend_prompt_prefix.md").strip())
    claude_chat_p.set_defaults(func=handlers["claude-chat"])

    task_p = sub.add_parser("task")
    task_sub = task_p.add_subparsers(dest="task_cmd", required=True)
    task_add = task_sub.add_parser("add")
    task_add.add_argument("run_id")
    task_add.add_argument("title")
    task_add.add_argument("--description", default=None)
    task_add.add_argument("--backend", choices=agent_backend_names(), default="codex")
    task_add.add_argument("--model", default=None)
    task_add.add_argument("--workspace", default=None, help="Registered workspace id, such as ws-001")
    task_add.add_argument("--workspace-path", default=None)
    task_add.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default=None)
    task_add.add_argument("--approval", choices=["untrusted", "on-failure", "on-request", "never"], default=None)
    task_add.add_argument("--enable-proxy", dest="proxy_enabled", action="store_true", help="Enable task proxy for created agents")
    task_add.set_defaults(proxy_enabled=None)
    task_add.add_argument("--http-proxy", default=None)
    task_add.add_argument("--https-proxy", default=None)
    task_add.add_argument("--no-proxy", default=None, help="NO_PROXY value for this task")
    task_add.add_argument("--collaboration-mode", choices=["auto", "solo", "pair", "team"], default=None)
    task_add.add_argument("--workflow-template", choices=workflow_template_ids(), default=None)
    task_add.add_argument("--delegation-policy", choices=["auto", "disabled"], default=None)
    task_add.add_argument("--max-sub-agents", type=int, default=None)
    task_add.add_argument("--preferred-sub-backend", choices=agent_backend_names(), default=None)
    task_add.add_argument("--preferred-sub-model", default=None)
    task_add.add_argument("--no-dispatch", action="store_true")
    task_add.set_defaults(func=handlers["task"])
    task_list = task_sub.add_parser("list")
    task_list.add_argument("run_id", nargs="?")
    task_list.set_defaults(func=handlers["task"])
    task_show = task_sub.add_parser("show")
    task_show.add_argument("run_id")
    task_show.add_argument("task_id")
    task_show.set_defaults(func=handlers["task"])
    task_final = task_sub.add_parser("final", help="Ask task-main to generate the Final and complete the task")
    task_final.add_argument("run_id")
    task_final.add_argument("task_id")
    task_final.add_argument("--no-autostart", action="store_true", help="Do not start a stopped task-main backend")
    task_final.set_defaults(func=handlers["task"])
    task_reopen = task_sub.add_parser("reopen", help="Reopen a completed task for follow-up")
    task_reopen.add_argument("run_id")
    task_reopen.add_argument("task_id")
    task_reopen.set_defaults(func=handlers["task"])
    task_recover = task_sub.add_parser("recover", help="Recover stale stopped-backend agents for one task")
    task_recover.add_argument("run_id")
    task_recover.add_argument("task_id")
    task_recover.add_argument("--agent-id", default=None, help="Agent id to recover; when --apply omits it, a single candidate is selected automatically")
    task_recover_mode = task_recover.add_mutually_exclusive_group()
    task_recover_mode.add_argument("--apply", action="store_true", help="Mark stale stopped-backend agent(s) as interrupted")
    task_recover_mode.add_argument("--dry-run", action="store_true", help="List stale runtime candidates without changing state. This is the default.")
    task_recover.add_argument("--restart-backend", action="store_true", help="With --apply, enqueue a recovery resume message and restart the agent backend")
    task_recover.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    task_recover.set_defaults(func=handlers["task"])
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
    task_proxy.set_defaults(func=handlers["task"])

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
    agent_add.set_defaults(func=handlers["agent"])
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
    agent_set.set_defaults(func=handlers["agent"])
    agent_list = agent_sub.add_parser("list")
    agent_list.add_argument("run_id")
    agent_list.add_argument("task_id")
    agent_list.set_defaults(func=handlers["agent"])

    session_p = sub.add_parser("session")
    session_sub = session_p.add_subparsers(dest="session_cmd", required=True)
    session_list = session_sub.add_parser("list")
    session_list.add_argument("run_id")
    session_list.add_argument("--task-id", default=None)
    session_list.set_defaults(func=handlers["session"])
    session_reset = session_sub.add_parser("reset")
    session_reset.add_argument("run_id")
    session_reset.add_argument("agent_id")
    session_reset.add_argument("--task-id", default=None)
    session_reset.set_defaults(func=handlers["session"])
    session_compact_reset = session_sub.add_parser("compact-reset")
    session_compact_reset.add_argument("run_id")
    session_compact_reset.add_argument("agent_id")
    session_compact_reset.add_argument("--task-id", required=True)
    session_compact_reset.add_argument("--reason", default="manual", choices=["manual", "large", "overflow", "final-reopen", "recovery"])
    session_compact_reset.add_argument("--restart", action="store_true")
    session_compact_reset.add_argument("--dry-run", action="store_true")
    session_compact_reset.set_defaults(func=handlers["session"])

    serve_p = sub.add_parser("serve")
    serve_p.add_argument("run_id", nargs="?")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8765)
    serve_p.add_argument("--interval", type=float, default=1.0)
    serve_p.set_defaults(func=handlers["serve"])

    ui_p = sub.add_parser("ui")
    ui_p.add_argument("run_id", nargs="?")
    ui_p.add_argument("--host", default="127.0.0.1")
    ui_p.add_argument("--port", type=int, default=8766)
    ui_p.add_argument("--poll-interval", type=int, default=1000)
    ui_p.add_argument("--auth-token", default=None, help="Require this token for the Web UI and APIs except /api/health")
    ui_p.add_argument("--auth-token-file", default=None, help="Read the Web UI auth token from a file")
    ui_p.add_argument("--allow-unsafe-bind", action="store_true", help="Suppress the warning for network-visible UI binds without auth")
    ui_p.set_defaults(func=handlers["ui"])
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


__all__ = [
    "MAX_WATCH_EVENTS_LIMIT",
    "WATCH_EVENTS_LIMIT",
    "add_codex_options",
    "build_parser",
    "normalize_run_subcommand",
    "with_default_command",
]
