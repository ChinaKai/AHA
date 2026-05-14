from __future__ import annotations

import concurrent.futures
import os
from pathlib import Path
import shlex
import subprocess
import textwrap

from aha_cli.constants import EVENTS_FILE
from aha_cli.store.filesystem import (
    PLAN_LOCK,
    append_event,
    load_config,
    require_plan,
    run_dir,
    save_plan,
)
from aha_cli.domain.models import utc_now


def render_command(template: str, root: Path, run: Path, task: dict) -> str:
    prompt_file = run / task["prompt_file"]
    output_file = run / task["output_file"]
    log_file = run / task["log_file"]
    inbox_file = run / task["inbox_file"]
    events_file = run / EVENTS_FILE
    return template.format(
        root=str(root),
        run_id=run.name,
        run_dir=str(run),
        task_id=task["id"],
        prompt_file=str(prompt_file),
        output_file=str(output_file),
        log_file=str(log_file),
        inbox_file=str(inbox_file),
        events_file=str(events_file),
    )


def run_one_task(root: Path, plan: dict, task_id: str, command_template: str | None) -> tuple[str, int]:
    run = run_dir(root, plan["id"])
    with PLAN_LOCK:
        plan = require_plan(root, plan["id"])
        task = next(t for t in plan["tasks"] if t["id"] == task_id)
        task["status"] = "running"
        task["started_at"] = utc_now()
        task["exit_code"] = None
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
    append_event(root, plan["id"], "task_started", {"task_id": task_id, "title": task["title"]})

    output_file = run / task["output_file"]
    log_file = run / task["log_file"]
    inbox_file = run / task["inbox_file"]
    events_file = run / EVENTS_FILE
    output_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    inbox_file.parent.mkdir(parents=True, exist_ok=True)
    inbox_file.touch()

    if not command_template:
        output_file.write_text(
            textwrap.dedent(
                f"""\
                ## Summary
                No runner command was configured, so AHA created this stub result.

                ## Findings
                - Task: {task["title"]}
                - Prompt: {task["prompt_file"]}

                ## Files Read
                - none

                ## Files Changed
                - none

                ## Commands Run
                - none

                ## Risks
                - No external agent actually executed this task.

                ## Suggested Merge Notes
                - Configure a runner with `aha run {plan["id"]} --runner-command "..."`
                """
            ),
            encoding="utf-8",
        )
        log_file.write_text("stub runner completed\n", encoding="utf-8")
        append_event(root, plan["id"], "log", {"task_id": task_id, "line": "stub runner completed"})
        exit_code = 0
    else:
        command = render_command(command_template, root, run, task)
        env = os.environ.copy()
        env.update(
            {
                "AHA_ROOT": str(root),
                "AHA_RUN_ID": plan["id"],
                "AHA_RUN_DIR": str(run),
                "AHA_TASK_ID": task["id"],
                "AHA_GOAL": plan["goal"],
                "AHA_MODE": plan["mode"],
                "AHA_TASK_TITLE": task["title"],
                "AHA_PROMPT_FILE": str(run / task["prompt_file"]),
                "AHA_OUTPUT_FILE": str(output_file),
                "AHA_LOG_FILE": str(log_file),
                "AHA_INBOX_FILE": str(inbox_file),
                "AHA_EVENTS_FILE": str(events_file),
            }
        )
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        with log_file.open("w", encoding="utf-8") as log:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                log.flush()
                append_event(root, plan["id"], "log", {"task_id": task_id, "line": line.rstrip("\n")})
        exit_code = process.wait()
        if not output_file.exists():
            output_file.write_text(log_file.read_text(encoding="utf-8"), encoding="utf-8")

    with PLAN_LOCK:
        plan = require_plan(root, plan["id"])
        task = next(t for t in plan["tasks"] if t["id"] == task_id)
        task["status"] = "completed" if exit_code == 0 else "failed"
        task["finished_at"] = utc_now()
        task["exit_code"] = exit_code
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
    append_event(
        root,
        plan["id"],
        "task_finished",
        {"task_id": task_id, "status": "completed" if exit_code == 0 else "failed", "exit_code": exit_code},
    )
    return task_id, exit_code


def run_pending_tasks(root: Path, run_id: str, args, codex_command_builder) -> int:
    cfg = load_config(root)
    plan = require_plan(root, run_id)
    command = args.runner_command if args.runner_command is not None else cfg.get("runner_command")
    backend = args.backend or cfg.get("backend")
    if args.runner_command is not None:
        backend = "command"
    if backend is None:
        backend = "command" if command else "stub"
    if backend == "stub":
        command = None
    elif backend == "codex":
        command = codex_command_builder(args, cfg)
    elif backend == "command":
        if not command:
            raise SystemExit("backend=command requires --runner-command or config runner_command")
    else:
        raise SystemExit(f"Unknown backend: {backend}")
    parallel = args.parallel or int(cfg.get("default_parallel", 4))

    runnable_tasks = [task for task in plan["tasks"] if not task.get("deleted_at")]
    pending = [task["id"] for task in runnable_tasks if task["status"] in {"pending", "failed"}]
    if args.all:
        pending = [task["id"] for task in runnable_tasks]
    if not pending:
        print(f"No pending tasks for run: {run_id}")
        return 0

    if args.dry_run:
        print(f"Dry run for {run_id}:")
        for task in runnable_tasks:
            if task["id"] in pending:
                rendered = render_command(command or "<stub-runner>", root, run_dir(root, run_id), task)
                print(f"- {task['id']}: {rendered}")
        return 0

    print(f"Running {len(pending)} task(s) for {run_id} with parallel={parallel}")
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = [pool.submit(run_one_task, root, plan, task_id, command) for task_id in pending]
        for future in concurrent.futures.as_completed(futures):
            task_id, exit_code = future.result()
            status = "ok" if exit_code == 0 else f"failed:{exit_code}"
            print(f"- {task_id}: {status}")
            if exit_code != 0:
                failed += 1
    return 1 if failed else 0


def shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)
