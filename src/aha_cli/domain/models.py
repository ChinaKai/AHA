from __future__ import annotations

import datetime as dt
import uuid

from aha_cli.domain.workflow_templates import (
    WORKFLOW_TEMPLATE_GUIDANCE as TASK_WORKFLOW_TEMPLATE_GUIDANCE,
    WORKFLOW_TEMPLATE_IDS as TASK_WORKFLOW_TEMPLATES,
    normalize_workflow_template,
    workflow_template_guidance,
)
from aha_cli.services.prompt_templates import render_prompt_template

DEFAULT_RETENTION_POLICY_REPORT_INTERVAL_SECONDS = 6 * 60 * 60


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def new_run_id() -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def default_retention_policy_config() -> dict:
    return {
        "scheduled_report_enabled": True,
        "report_interval_seconds": DEFAULT_RETENTION_POLICY_REPORT_INTERVAL_SECONDS,
        "max_total_bytes": 0,
        "max_candidate_bytes": 0,
        "min_candidate_files": 0,
        "min_age_seconds": 0,
        "include_chat": False,
    }


def default_config() -> dict:
    return {
        "backend": "stub",
        "runner_command": None,
        "default_parallel": 10,
        "default_mode": "research",
        "workspace_roots": [],
        "webgame_workspace": None,
        "proxy": {
            "enabled": False,
            "http_proxy": None,
            "https_proxy": None,
            "no_proxy": None,
        },
        "context_windows": {},
        "retention_policy": default_retention_policy_config(),
        "codex": {
            "bin": "codex",
            "model": None,
            "sandbox": "auto",
            "approval": "never",
            "json": True,
            "session_policy": "sticky",
            "env_active": None,
            "env": [],
            "proxy": {
                "enabled": False,
                "http_proxy": None,
                "https_proxy": None,
                "no_proxy": None,
            },
        },
        "claude": {
            "bin": "claude",
            "model": None,
            "sandbox": "auto",
            "permission_mode": None,
            "session_policy": "sticky",
            "env_active": None,
            "env": [],
            "proxy": {
                "enabled": False,
                "http_proxy": None,
                "https_proxy": None,
                "no_proxy": None,
            },
        },
    }


TASK_SUPERVISION_MODES = {"manual", "assisted"}
TASK_SUPERVISION_HOST_BACKENDS = {"stub", "codex", "claude"}
TASK_SUPERVISION_ASK_USER_GATES = (
    "real_ui_validation",
    "scope_change",
    "commit_merge_delete",
    "destructive_or_high_risk",
    "permissions_or_external",
    "product_preference",
)
TASK_COLLABORATION_MODES = {"auto", "solo", "pair", "team"}
TASK_COLLABORATION_DEFAULTS = {
    "auto": ("auto", 3),
    "solo": ("disabled", 0),
    "pair": ("auto", 1),
    "team": ("auto", 2),
}
DEFAULT_TASK_SANDBOX = "danger-full-access"
DEFAULT_TASK_SUPERVISION_MAX_ROUNDS = 99
DEFAULT_TASK_CONTEXT_THRESHOLD_PERCENT = 75
TASK_HARDWARE_DEBUG_CHANNEL_TYPES = ("uart", "nfs", "telnet")
TASK_HARDWARE_DEBUG_PERMISSION_KEYS = ("read", "write")


def default_task_supervision_ask_user_gates() -> dict:
    return {key: False for key in TASK_SUPERVISION_ASK_USER_GATES}


def normalize_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


def normalize_delegation_policy(value: object, default: str = "auto") -> str:
    policy = str(value or default).strip().lower()
    return policy if policy in {"auto", "disabled"} else default


def normalize_collaboration_mode(value: object, default: str = "auto") -> str:
    mode = str(value or default).strip().lower()
    return mode if mode in TASK_COLLABORATION_MODES else default


def non_negative_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, default)


def infer_collaboration_mode(delegation_policy: object, max_sub_agents: object) -> str:
    policy = normalize_delegation_policy(delegation_policy)
    limit = non_negative_int(max_sub_agents)
    if policy == "disabled" or limit == 0:
        return "solo"
    if limit == 1:
        return "pair"
    if limit == 2:
        return "team"
    return "auto"


def resolve_task_collaboration(
    collaboration_mode: object | None = None,
    delegation_policy: object | None = None,
    max_sub_agents: object | None = None,
) -> tuple[str, str, int]:
    explicit_mode = collaboration_mode is not None and str(collaboration_mode).strip() != ""
    if explicit_mode:
        mode = normalize_collaboration_mode(collaboration_mode)
        default_policy, default_limit = TASK_COLLABORATION_DEFAULTS[mode]
        if mode == "auto":
            policy = normalize_delegation_policy(delegation_policy, default_policy)
            if policy == "disabled":
                return "solo", "disabled", 0
            limit = non_negative_int(max_sub_agents, default_limit) if max_sub_agents is not None else default_limit
            return "auto", "auto", limit
        return mode, default_policy, default_limit

    policy = normalize_delegation_policy(delegation_policy)
    if policy == "disabled":
        return "solo", "disabled", 0
    limit = non_negative_int(max_sub_agents, 3) if max_sub_agents is not None else 3
    return infer_collaboration_mode(policy, limit), "auto", limit


def default_task_supervision() -> dict:
    return {
        "mode": "manual",
        "scope": "task",
        "host_backend": "stub",
        "host_model": None,
        "host_proxy_enabled": False,
        "host_agent_id": None,
        "real_agent_enabled": False,
        "channel": "main_only",
        "max_rounds": DEFAULT_TASK_SUPERVISION_MAX_ROUNDS,
        "ask_user_gates": default_task_supervision_ask_user_gates(),
    }


def normalize_task_supervision(value: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    supervision = default_task_supervision()
    mode = str(raw.get("mode") or supervision["mode"]).strip().lower()
    supervision["mode"] = mode if mode in TASK_SUPERVISION_MODES else "manual"
    host_backend = str(raw.get("host_backend") or supervision["host_backend"]).strip().lower()
    supervision["host_backend"] = host_backend if host_backend in TASK_SUPERVISION_HOST_BACKENDS else "stub"
    host_model = raw.get("host_model", raw.get("model"))
    supervision["host_model"] = str(host_model).strip() if host_model not in (None, "") else None
    supervision["host_proxy_enabled"] = normalize_bool(raw.get("host_proxy_enabled", raw.get("proxy_enabled")), default=False)
    host_agent_id = raw.get("host_agent_id")
    supervision["host_agent_id"] = str(host_agent_id).strip() if host_agent_id else None
    if "real_agent_enabled" in raw:
        supervision["real_agent_enabled"] = normalize_bool(raw.get("real_agent_enabled"))
    raw_gates = raw.get("ask_user_gates") if isinstance(raw.get("ask_user_gates"), dict) else raw.get("ask_user")
    if isinstance(raw_gates, dict):
        gates = default_task_supervision_ask_user_gates()
        for key in TASK_SUPERVISION_ASK_USER_GATES:
            if key in raw_gates:
                gates[key] = normalize_bool(raw_gates.get(key), default=False)
        supervision["ask_user_gates"] = gates
    try:
        supervision["max_rounds"] = max(1, min(100, int(raw.get("max_rounds") or supervision["max_rounds"])))
    except (TypeError, ValueError):
        pass

    if supervision["mode"] == "manual":
        supervision["host_backend"] = "stub"
        supervision["host_agent_id"] = None
        supervision["real_agent_enabled"] = False
    elif supervision["host_backend"] == "stub":
        supervision["host_agent_id"] = None
        supervision["real_agent_enabled"] = False
    else:
        supervision["real_agent_enabled"] = True
    return supervision


def default_task_context_management() -> dict:
    return {
        "auto_compact_enabled": False,
        "auto_compact_threshold_percent": DEFAULT_TASK_CONTEXT_THRESHOLD_PERCENT,
    }


def normalize_task_context_management(value: object | None = None, *, default_enabled: bool = False) -> dict:
    raw = value if isinstance(value, dict) else {}
    context = default_task_context_management()
    if default_enabled:
        context["auto_compact_enabled"] = True
    if "auto_compact_enabled" in raw:
        context["auto_compact_enabled"] = normalize_bool(raw.get("auto_compact_enabled"))
    elif "enabled" in raw:
        context["auto_compact_enabled"] = normalize_bool(raw.get("enabled"))
    raw_threshold = raw.get("auto_compact_threshold_percent", raw.get("threshold_percent"))
    if raw_threshold is not None:
        try:
            context["auto_compact_threshold_percent"] = max(1, min(99, int(raw_threshold)))
        except (TypeError, ValueError):
            pass
    return context


def default_task_hardware_debug_permissions() -> dict:
    return {
        "read": True,
        "write": False,
    }


def default_task_hardware_debug() -> dict:
    return {
        "enabled": False,
        "channels": [],
    }


def default_task_skills() -> dict:
    return {
        "enabled_paths": [],
    }


def _normalize_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace("\r\n", "\n").replace(",", "\n").split("\n")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


def normalize_task_hardware_debug_permissions(value: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    permissions = default_task_hardware_debug_permissions()
    legacy_map = {
        "serial_read": "read",
        "serial_write": "write",
    }
    for old_key, new_key in legacy_map.items():
        if old_key in raw:
            permissions[new_key] = normalize_bool(raw.get(old_key), default=permissions[new_key])
    for key in TASK_HARDWARE_DEBUG_PERMISSION_KEYS:
        if key in raw:
            permissions[key] = normalize_bool(raw.get(key), default=permissions[key])
    return permissions


def normalize_task_hardware_debug_uart_settings(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    port = str(raw.get("port") or raw.get("path") or "").strip()
    baudrate_raw = raw.get("baudrate", raw.get("baud"))
    try:
        baudrate = int(baudrate_raw) if baudrate_raw not in (None, "") else 115200
    except (TypeError, ValueError):
        baudrate = 115200
    return {
        "port": port,
        "baudrate": max(1, baudrate),
    }


def normalize_task_hardware_debug_nfs_settings(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    return {
        "server": str(raw.get("server") or raw.get("host") or "").strip(),
        "remote_path": str(raw.get("remote_path") or raw.get("export_path") or raw.get("path") or "").strip(),
        "mount_path": str(raw.get("mount_path") or raw.get("target_path") or "").strip(),
    }


def normalize_task_hardware_debug_telnet_settings(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    port_raw = raw.get("port")
    try:
        port = int(port_raw) if port_raw not in (None, "") else 23
    except (TypeError, ValueError):
        port = 23
    return {
        "host": str(raw.get("host") or raw.get("server") or "").strip(),
        "port": max(1, port),
        "username": str(raw.get("username") or raw.get("user") or "").strip(),
    }


def normalize_task_hardware_debug_channel(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    channel_type = str(value.get("type") or value.get("kind") or "").strip().lower()
    if channel_type in {"", "none"}:
        return None
    if channel_type not in TASK_HARDWARE_DEBUG_CHANNEL_TYPES:
        return None
    settings_raw = value.get("settings") if isinstance(value.get("settings"), dict) else value
    if channel_type == "uart":
        settings = normalize_task_hardware_debug_uart_settings(settings_raw)
    elif channel_type == "nfs":
        settings = normalize_task_hardware_debug_nfs_settings(settings_raw)
    else:
        settings = normalize_task_hardware_debug_telnet_settings(settings_raw)
    return {
        "type": channel_type,
        "settings": settings,
        "operation_skill_path": str(value.get("operation_skill_path") or value.get("operation_skill") or "").strip(),
        "permissions": normalize_task_hardware_debug_permissions(value.get("permissions")),
    }


def normalize_task_hardware_debug(value: object | None = None) -> dict:
    raw = value if isinstance(value, dict) else {}
    hardware_debug = default_task_hardware_debug()
    channels_raw = raw.get("channels")
    channels: list[dict] = []
    if isinstance(channels_raw, list):
        channels = [channel for item in channels_raw if (channel := normalize_task_hardware_debug_channel(item))]
    elif isinstance(channels_raw, dict):
        channel = normalize_task_hardware_debug_channel(channels_raw)
        channels = [channel] if channel else []
    else:
        enabled = normalize_bool(raw.get("enabled", raw.get("hardware_debug_enabled")), default=False)
        devices_raw = raw.get("devices")
        legacy_devices = devices_raw if isinstance(devices_raw, list) else ([devices_raw] if isinstance(devices_raw, dict) else [])
        if enabled:
            if legacy_devices:
                for device in legacy_devices:
                    channel = normalize_task_hardware_debug_channel(
                        {
                            "type": "uart",
                            "settings": device,
                            "operation_skill_path": raw.get("operation_skill_path", raw.get("operation_skill")),
                            "permissions": raw.get("permissions"),
                        }
                    )
                    if channel:
                        channels.append(channel)
            else:
                channel = normalize_task_hardware_debug_channel(
                    {
                        "type": "uart",
                        "settings": {},
                        "operation_skill_path": raw.get("operation_skill_path", raw.get("operation_skill")),
                        "permissions": raw.get("permissions"),
                    }
                )
                if channel:
                    channels.append(channel)
    hardware_debug["channels"] = channels
    if "enabled" in raw:
        hardware_debug["enabled"] = normalize_bool(raw.get("enabled"), default=bool(channels))
    else:
        # Legacy configs have no master switch: treat any configured channel as enabled.
        hardware_debug["enabled"] = bool(channels)
    return hardware_debug


def normalize_task_skills(value: object | None = None) -> dict:
    if isinstance(value, dict):
        raw_paths = value.get("enabled_paths", value.get("skill_paths", value.get("paths", value.get("skills"))))
    else:
        raw_paths = value
    task_skills = default_task_skills()
    task_skills["enabled_paths"] = _normalize_string_list(raw_paths)
    return task_skills


def task_metadata_projection(task: dict, default_backend: str = "codex") -> dict:
    preferred_backend = task.get("preferred_backend") or default_backend
    preferred_model = task.get("preferred_model")
    collaboration_mode, delegation_policy, max_sub_agents = resolve_task_collaboration(
        task.get("collaboration_mode"),
        task.get("delegation_policy"),
        task.get("max_sub_agents"),
    )
    preferred_sub_model = task.get("preferred_sub_model")
    if preferred_sub_model is None:
        preferred_sub_model = preferred_model
    return {
        "workspace_id": task.get("workspace_id"),
        "workspace_path": task.get("workspace_path"),
        "preferred_backend": preferred_backend,
        "preferred_model": preferred_model,
        "preferred_sandbox": task.get("preferred_sandbox"),
        "preferred_approval": task.get("preferred_approval"),
        "preferred_proxy_enabled": bool(task.get("preferred_proxy_enabled")),
        "preferred_http_proxy": task.get("preferred_http_proxy"),
        "preferred_https_proxy": task.get("preferred_https_proxy"),
        "preferred_no_proxy": task.get("preferred_no_proxy"),
        "preferred_sub_backend": task.get("preferred_sub_backend") or preferred_backend,
        "preferred_sub_model": preferred_sub_model,
        "collaboration_mode": collaboration_mode,
        "workflow_template": normalize_workflow_template(task.get("workflow_template")),
        "delegation_policy": delegation_policy,
        "max_sub_agents": max_sub_agents,
        "supervision": normalize_task_supervision(task.get("supervision")),
        "context_management": normalize_task_context_management(task.get("context_management")),
        "task_skills": normalize_task_skills(task.get("task_skills")),
        "hardware_debug": normalize_task_hardware_debug(task.get("hardware_debug")),
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
        "assignment_id": None,
        "scope_id": None,
        "scope_explicit": False,
        "generation": 0,
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
    collaboration_mode: str | None = None,
    workflow_template: str | None = None,
    delegation_policy: str | None = "auto",
    max_sub_agents: int | None = 3,
    preferred_sub_backend: str | None = None,
    preferred_sub_model: str | None = None,
    description: str | None = None,
    supervision: dict | None = None,
    context_management: dict | None = None,
    task_skills: dict | None = None,
    hardware_debug: dict | None = None,
) -> dict:
    resolved_collaboration_mode, resolved_delegation_policy, resolved_max_sub_agents = resolve_task_collaboration(
        collaboration_mode,
        delegation_policy,
        max_sub_agents,
    )
    return {
        "id": task_id,
        "title": title,
        "description": description or "",
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
        "collaboration_mode": resolved_collaboration_mode,
        "workflow_template": normalize_workflow_template(workflow_template),
        "delegation_policy": resolved_delegation_policy,
        "max_sub_agents": resolved_max_sub_agents,
        "supervision": normalize_task_supervision(supervision),
        "context_management": normalize_task_context_management(context_management),
        "task_skills": normalize_task_skills(task_skills),
        "hardware_debug": normalize_task_hardware_debug(hardware_debug),
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
        "history_backend_sessions": [],
        "compact_summary": None,
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
    return render_prompt_template(
        "subtask.md",
        goal=goal,
        task_title=task["title"],
        task_description=task.get("description", ""),
        mode=mode,
        mutability=mutability,
        write_scope=scope_text,
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
        task.update(task_metadata_projection(task, backend))
        ensure_task_agents(task, backend)
    plan.setdefault("main_agent", make_agent("main", "run-main", backend, status="active"))
    return plan
