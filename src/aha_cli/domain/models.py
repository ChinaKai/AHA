from __future__ import annotations

import datetime as dt
import textwrap
import uuid


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def new_run_id() -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def default_config() -> dict:
    return {
        "backend": "stub",
        "runner_command": None,
        "default_parallel": 4,
        "default_mode": "research",
        "codex": {
            "bin": "codex",
            "model": None,
            "sandbox": "auto",
            "approval": "never",
            "json": True,
            "session_policy": "sticky",
        },
        "claude": {
            "bin": "claude",
            "model": None,
            "sandbox": "auto",
            "permission_mode": None,
            "session_policy": "sticky",
            "env": {},
        },
    }


def default_tasks(goal: str, agents: int, mode: str) -> list[str]:
    research = [
        "Map the relevant files, concepts, and terminology for the goal.",
        "Trace the main execution flow and identify important data inputs and outputs.",
        "Analyze edge cases, risks, unclear assumptions, and missing context.",
        "Produce a concise module-level report with recommended next steps.",
    ]
    implementation = [
        "Inspect the current code and identify the minimal implementation scope.",
        "Implement a bounded change in an isolated write scope.",
        "Add or update focused verification for the changed behavior.",
        "Summarize changed files, verification results, and remaining risks.",
    ]
    base = implementation if mode == "implementation" else research
    tasks: list[str] = []
    for idx in range(max(1, agents)):
        tasks.append(base[idx] if idx < len(base) else f"Handle additional independent slice {idx + 1} for: {goal}")
    return tasks


def make_agent(
    agent_id: str,
    role: str,
    backend: str = "codex",
    status: str = "pending",
    model: str | None = None,
    workspace_path: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    proxy_enabled: bool = False,
    created_by: str = "system",
    created_reason: str = "",
) -> dict:
    return {
        "id": agent_id,
        "role": role,
        "backend": backend,
        "model": model,
        "sandbox": sandbox,
        "approval": approval,
        "proxy_enabled": bool(proxy_enabled),
        "status": status,
        "session_policy": "sticky",
        "session_id": None,
        "backend_session_id": None,
        "workspace_path": workspace_path,
        "created_by": created_by,
        "created_reason": created_reason,
        "status_started_at": None,
        "last_active_at": None,
        "last_usage": None,
    }


def make_task(
    task_id: str,
    title: str,
    created: str,
    backend: str = "codex",
    model: str | None = None,
    workspace_path: str | None = None,
    workspace_id: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
    proxy_enabled: bool = False,
    http_proxy: str | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
    delegation_policy: str = "auto",
    max_sub_agents: int = 3,
    preferred_sub_backend: str | None = None,
    preferred_sub_model: str | None = None,
) -> dict:
    return {
        "id": task_id,
        "title": title,
        "workspace_id": workspace_id,
        "workspace_path": workspace_path,
        "preferred_backend": backend,
        "preferred_model": model,
        "preferred_sandbox": sandbox,
        "preferred_approval": approval,
        "preferred_proxy_enabled": bool(proxy_enabled),
        "preferred_http_proxy": http_proxy,
        "preferred_https_proxy": https_proxy,
        "preferred_no_proxy": no_proxy,
        "preferred_sub_backend": preferred_sub_backend or backend,
        "preferred_sub_model": preferred_sub_model if preferred_sub_model is not None else model,
        "delegation_policy": delegation_policy,
        "max_sub_agents": max(0, max_sub_agents),
        "status": "pending",
        "prompt_file": f"prompts/{task_id}.md",
        "output_file": f"results/{task_id}.md",
        "log_file": f"logs/{task_id}.log",
        "inbox_file": f"inbox/{task_id}.jsonl",
        "created_at": created,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "current_round_id": "round-001",
        "round_sequence": 1,
        "last_final_round_id": None,
        "last_final_at": None,
        "hidden": False,
        "hidden_at": None,
        "deleted_at": None,
        "agents": [
            make_agent(
                "main",
                "task-main",
                backend,
                status="active",
                model=model,
                workspace_path=workspace_path,
                sandbox=sandbox,
                approval=approval,
                proxy_enabled=proxy_enabled,
                created_by="system",
                created_reason="task creation",
            )
        ],
    }


def make_task_round(
    task_id: str,
    sequence: int,
    started_at: str,
    reopened_from_round_id: str | None = None,
    status: str = "active",
) -> dict:
    round_id = f"round-{max(1, sequence):03d}"
    return {
        "task_id": task_id,
        "round_id": round_id,
        "sequence": max(1, sequence),
        "status": status,
        "started_at": started_at,
        "finalized_at": None,
        "final_path": None,
        "final_meta_path": None,
        "reopened_from_round_id": reopened_from_round_id,
    }


def ensure_task_agents(task: dict, backend: str = "codex") -> list[dict]:
    agents = task.setdefault("agents", [])
    if not any(agent.get("id") == "main" for agent in agents):
        agents.insert(
            0,
            make_agent(
                "main",
                "task-main",
                task.get("preferred_backend") or backend,
                status="active",
                model=task.get("preferred_model"),
                workspace_path=task.get("workspace_path"),
                created_by="system",
                created_reason="compatibility upgrade",
            ),
        )
    for agent in agents:
        agent.setdefault("model", task.get("preferred_model"))
        agent.setdefault("sandbox", task.get("preferred_sandbox"))
        agent.setdefault("approval", task.get("preferred_approval"))
        agent.setdefault("proxy_enabled", bool(task.get("preferred_proxy_enabled")))
        agent.setdefault("backend_session_id", None)
        agent.setdefault("workspace_path", task.get("workspace_path"))
        agent.setdefault("created_by", "system")
        agent.setdefault("created_reason", "")
        agent.setdefault("status_started_at", None)
        agent.setdefault("last_active_at", None)
        agent.setdefault("last_usage", None)
    return agents


def next_task_id(tasks: list[dict]) -> str:
    nums = []
    for task in tasks:
        raw = str(task.get("id", ""))
        if raw.startswith("task-"):
            try:
                nums.append(int(raw.split("-", 1)[1]))
            except ValueError:
                pass
    return f"task-{(max(nums) if nums else 0) + 1:03d}"


def next_sub_id(task: dict) -> str:
    nums = []
    for agent in task.get("agents", []):
        raw = str(agent.get("id", ""))
        if raw.startswith("sub-"):
            try:
                nums.append(int(raw.split("-", 1)[1]))
            except ValueError:
                pass
    return f"sub-{(max(nums) if nums else 0) + 1:03d}"


def make_session(
    run_id: str,
    task_id: str | None,
    agent_id: str,
    backend: str,
    policy: str = "sticky",
    model: str | None = None,
    workspace_path: str | None = None,
) -> dict:
    scope = f"run:{run_id}:agent:{agent_id}" if task_id is None else f"run:{run_id}:task:{task_id}:agent:{agent_id}"
    return {
        "id": f"{task_id or 'run'}:{agent_id}",
        "run_id": run_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "backend": backend,
        "model": model,
        "policy": policy,
        "scope": scope,
        "backend_session_id": None,
        "workspace_path": workspace_path,
        "status": "active",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def task_prompt(goal: str, mode: str, task: dict, write_scopes: list[str]) -> str:
    scope_text = "\n".join(f"- {scope}" for scope in write_scopes) or "- none"
    mutability = (
        "You may edit only the declared write scope."
        if mode == "implementation"
        else "Read-only research: do not modify files."
    )
    return textwrap.dedent(
        f"""\
        # AHA Subtask

        Goal:
        {goal}

        Task:
        {task["title"]}

        Mode:
        {mode}

        Rules:
        - {mutability}
        - Do not revert user changes.
        - Report facts with file paths when possible.
        - Keep the result structured and concise.

        Write scope:
        {scope_text}

        Expected output sections:
        ## Summary
        ## Findings
        ## Files Read
        ## Files Changed
        ## Commands Run
        ## Risks
        ## Suggested Merge Notes

        Realtime protocol:
        - Read user/main-agent messages from $AHA_INBOX_FILE when your runner supports it.
        - Append JSON events to $AHA_EVENTS_FILE when your runner supports it.
        - Write final Markdown output to $AHA_OUTPUT_FILE.
        """
    )


def enrich_plan(plan: dict, backend: str = "codex") -> dict:
    for task in plan.get("tasks", []):
        task.setdefault("current_round_id", "round-001")
        task.setdefault("round_sequence", 1)
        task.setdefault("last_final_round_id", None)
        task.setdefault("last_final_at", None)
        task.setdefault("hidden", False)
        task.setdefault("hidden_at", None)
        task.setdefault("deleted_at", None)
        task.setdefault("preferred_sandbox", None)
        task.setdefault("preferred_approval", None)
        task.setdefault("preferred_proxy_enabled", False)
        task.setdefault("preferred_http_proxy", None)
        task.setdefault("preferred_https_proxy", None)
        task.setdefault("preferred_no_proxy", None)
        ensure_task_agents(task, backend)
    plan.setdefault("main_agent", make_agent("main", "run-main", backend, status="active"))
    return plan
