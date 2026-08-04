from __future__ import annotations

import hashlib
import datetime as dt
from pathlib import Path
import threading

from aha_cli.domain.models import (
    FEISHU_GROUP_PURPOSE,
    FEISHU_GROUP_TASK_KIND,
    SYSTEM_RUN_KIND,
    is_feishu_group_run,
    is_feishu_group_task,
    utc_now,
)
from aha_cli.store.config import load_config
from aha_cli.store.filesystem import create_plan
from aha_cli.store.io import write_json
from aha_cli.store.paths import aha_home_path, run_dir
from aha_cli.store.runs import list_run_summaries, locked_plan, require_plan, save_plan

FEISHU_GROUP_RUN_TITLE = "feishu-group"
FEISHU_GROUP_TASK_TITLE = "Feishu Digital Human"
FEISHU_GROUP_STATE_DIR = "feishu_group_state"
FEISHU_GROUP_HANDOFF_ACK = "您的问题已记录，我已转发给主人，有进展给您回复"
DEFAULT_CONTEXT_TOKEN_LIMIT = 2000
DEFAULT_TASK_RETENTION_DAYS = 30

_run_lock = threading.RLock()


def _session_short_hash(session_key: str) -> str:
    return hashlib.sha256(str(session_key or "").encode("utf-8")).hexdigest()[:6]


def _clean_display_name(value: object) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    return text[:40]


def feishu_group_state_dir(root: Path) -> Path:
    path = aha_home_path(root) / FEISHU_GROUP_STATE_DIR
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def feishu_group_user_session_key(*, tenant_key: str, open_id: str) -> str:
    tenant = str(tenant_key or "").strip()
    identity = str(open_id or "").strip()
    if not tenant:
        raise ValueError("tenant_key is required")
    if not identity:
        raise ValueError("open_id is required")
    return f"{tenant}:feishu-group-user:{identity}"


def session_task_title(session_key: str, *, display_name: str = "") -> str:
    short_id = _session_short_hash(session_key)
    clean_name = _clean_display_name(display_name)
    if clean_name:
        return f"{FEISHU_GROUP_TASK_TITLE} · {clean_name} · User · {short_id}"
    return f"{FEISHU_GROUP_TASK_TITLE} · User · {short_id}"


def _unique_task_title(plan: dict, title: str, *, exclude_task_id: str = "") -> str:
    titles = {
        str(task.get("title") or "")
        for task in plan.get("tasks", [])
        if isinstance(task, dict) and str(task.get("id") or "") != str(exclude_task_id or "")
    }
    if title not in titles:
        return title
    generation = 2
    while f"{title} #{generation}" in titles:
        generation += 1
    return f"{title} #{generation}"


def _sync_task_display_title(
    root: Path,
    run_id: str,
    task: dict,
    *,
    session_key: str,
    display_name: str = "",
) -> dict:
    clean_name = _clean_display_name(display_name) or _clean_display_name(task.get("feishu_display_name"))
    if not clean_name:
        return task
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        stored = next(
            (
                item
                for item in plan.get("tasks", [])
                if isinstance(item, dict) and str(item.get("id") or "") == str(task.get("id") or "")
            ),
            None,
        )
        if stored is None:
            return task
        desired = _unique_task_title(
            plan,
            session_task_title(session_key, display_name=clean_name),
            exclude_task_id=str(stored.get("id") or ""),
        )
        changed = False
        if str(stored.get("title") or "") != desired:
            stored["title"] = desired
            changed = True
        if clean_name and str(stored.get("feishu_display_name") or "") != clean_name:
            stored["feishu_display_name"] = clean_name
            changed = True
        if not changed:
            return dict(stored)
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / str(stored["id"]) / "task.json", stored)
        return dict(stored)


def _epoch(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _mark_system_run(root: Path, run_id: str) -> dict:
    workspace = str(feishu_group_state_dir(root).resolve())
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        plan["kind"] = SYSTEM_RUN_KIND
        plan["system_managed"] = True
        plan["system_owner"] = "aha"
        plan["system_purpose"] = FEISHU_GROUP_PURPOSE
        plan["system_schema_version"] = 1
        main_agent = plan.get("main_agent") if isinstance(plan.get("main_agent"), dict) else {}
        main_agent.update(
            {
                "workspace_path": workspace,
                "sandbox": "read-only",
                "approval": "never",
            }
        )
        plan["main_agent"] = main_agent
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        return plan


def archive_inactive_feishu_group_tasks(
    root: Path,
    run_id: str,
    *,
    now: str | None = None,
    inactive_days: int = DEFAULT_TASK_RETENTION_DAYS,
) -> int:
    timestamp = str(now or utc_now())
    cutoff = _epoch(timestamp) - max(1, int(inactive_days)) * 24 * 60 * 60
    archived = 0
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        for task in plan.get("tasks", []):
            if not isinstance(task, dict) or not is_feishu_group_task(task) or task.get("deleted_at"):
                continue
            if task.get("feishu_group_archived_at"):
                continue
            last_interaction = _epoch(task.get("last_interaction_at") or task.get("updated_at") or task.get("created_at"))
            if not last_interaction or last_interaction > cutoff:
                continue
            task["hidden"] = True
            task["hidden_at"] = task.get("hidden_at") or timestamp
            task["status"] = "completed"
            task["finished_at"] = task.get("finished_at") or timestamp
            task["exit_code"] = 0 if task.get("exit_code") is None else task.get("exit_code")
            task["feishu_group_archived_at"] = timestamp
            write_json(run_dir(root, run_id) / "tasks" / str(task.get("id") or "") / "task.json", task)
            archived += 1
        if archived:
            plan["updated_at"] = timestamp
            save_plan(root, plan)
    return archived


def mark_feishu_group_task_interaction(root: Path, run_id: str, task_id: str, *, at: str | None = None) -> dict:
    timestamp = str(at or utc_now())
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        task = next(
            (
                item
                for item in plan.get("tasks", [])
                if isinstance(item, dict) and str(item.get("id") or "") == str(task_id or "")
            ),
            None,
        )
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        task["last_interaction_at"] = timestamp
        plan["updated_at"] = timestamp
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / str(task["id"]) / "task.json", task)
        return dict(task)


def ensure_feishu_group_run(root: Path, defaults: dict[str, object]) -> str:
    with _run_lock:
        for summary in list_run_summaries(root):
            run_id = str(summary.get("id") or "")
            if not run_id:
                continue
            try:
                plan = require_plan(root, run_id)
            except SystemExit:
                continue
            if is_feishu_group_run(plan):
                _mark_system_run(root, run_id)
                archive_inactive_feishu_group_tasks(root, run_id)
                return run_id
            if str(summary.get("goal") or "") == FEISHU_GROUP_RUN_TITLE:
                _mark_system_run(root, run_id)
                archive_inactive_feishu_group_tasks(root, run_id)
                return run_id

        config = load_config(root)
        backend = str(defaults.get("backend") or config.get("backend") or "codex")
        workspace = str(feishu_group_state_dir(root).resolve())
        plan = create_plan(
            root,
            FEISHU_GROUP_RUN_TITLE,
            1,
            "implementation",
            [],
            [],
            backend=backend,
            model=defaults.get("model"),
            reasoning_effort=defaults.get("reasoning_effort"),
            workspace_path=workspace,
            sandbox="read-only",
            approval="never",
            proxy_enabled=bool(defaults.get("proxy_enabled")),
            collaboration_mode="solo",
            workflow_template="auto",
            create_default_tasks=False,
        )
        run_id = str(plan.get("id") or "")
        _mark_system_run(root, run_id)
        return run_id


def ensure_feishu_group_task(
    root: Path,
    run_id: str,
    session_key: str,
    defaults: dict[str, object],
    *,
    display_name: str = "",
) -> dict:
    session_hash = hashlib.sha256(str(session_key or "").encode("utf-8")).hexdigest()
    clean_name = _clean_display_name(display_name)
    plan = require_plan(root, run_id)
    existing = [
        task
        for task in plan.get("tasks", [])
        if isinstance(task, dict)
        and is_feishu_group_task(task)
        and str(task.get("session_key_hash") or "") == session_hash
        and not task.get("deleted_at")
        and not task.get("feishu_group_archived_at")
        and str(task.get("status") or "") not in {"completed", "failed", "blocked"}
    ]
    if existing:
        return _sync_task_display_title(
            root,
            run_id,
            existing[-1],
            session_key=session_key,
            display_name=clean_name,
        )
    base_title = session_task_title(session_key, display_name=clean_name)
    title = _unique_task_title(plan, base_title)
    workspace = str(feishu_group_state_dir(root).resolve())
    from aha_cli.services.tasks import create_task_and_dispatch

    task = create_task_and_dispatch(
        root,
        run_id,
        title,
        description="System-managed Feishu group digital-human memory for one Feishu user.",
        backend=str(defaults.get("backend") or "codex"),
        model=defaults.get("model"),
        reasoning_effort=defaults.get("reasoning_effort"),
        proxy_enabled=bool(defaults.get("proxy_enabled")),
        workspace_path=workspace,
        sandbox="read-only",
        approval="never",
        collaboration_mode="solo",
        workflow_template="auto",
        delegation_policy="disabled",
        max_sub_agents=0,
        context_management={"auto_compact_enabled": True},
        token_saving={"enabled": False, "provider": "nav"},
        dispatch=False,
    )
    with locked_plan(root, run_id):
        plan = require_plan(root, run_id)
        stored = next(item for item in plan.get("tasks", []) if str(item.get("id") or "") == str(task.get("id") or ""))
        stored.update(
            {
                "kind": FEISHU_GROUP_TASK_KIND,
                "system_managed": True,
                "system_owner": "aha",
                "channel": "feishu_group",
                "session_key_hash": session_hash,
                "system_schema_version": 1,
            }
        )
        if clean_name:
            stored["feishu_display_name"] = clean_name
        plan["updated_at"] = utc_now()
        save_plan(root, plan)
        write_json(run_dir(root, run_id) / "tasks" / str(stored["id"]) / "task.json", stored)
        task = dict(stored)
    return task


def group_agent_message(payload: dict, text: str, *, token_limit: int = DEFAULT_CONTEXT_TOKEN_LIMIT) -> str:
    del token_limit
    root_id = str(payload.get("root_id") or "").strip()
    parent_id = str(payload.get("parent_id") or "").strip()
    thread_id = str(payload.get("thread_id") or "").strip()
    lines = [
        "飞书群聊 @ 数字人请求",
        "",
        "请基于本次 @ 消息判断意图：能公开直接回答则直接答；执行类需求信息不清时先在群里简短追问；"
        "需求明确且涉及执行、承诺、权限、争议或私密内容时再触发转管家动作。",
        "",
        "当前 @ 消息：",
        str(text or "").strip(),
    ]
    attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
    if attachments:
        lines.extend(["", "飞书附件（仅为资源摘要；未下载或视觉分析，不要臆测内容）："])
        for index, item in enumerate(attachments[:8], start=1):
            if not isinstance(item, dict):
                continue
            name = str(item.get("file_name") or item.get("name") or "").strip()
            resource_type = str(item.get("type") or "attachment")
            key = str(item.get("image_key") or item.get("file_key") or item.get("media_key") or "").strip()
            label = f"{index}. {resource_type}"
            if name:
                label += f" {name}"
            if key:
                label += f" key={key[:6]}...{key[-4:]}" if len(key) > 12 else f" key={key}"
            lines.append(label)
    refs = [part for part in (f"root_id={root_id}" if root_id else "", f"parent_id={parent_id}" if parent_id else "", f"thread_id={thread_id}" if thread_id else "") if part]
    if refs:
        lines.extend(["", "飞书线程引用（仅作本次判断，不要在回复中暴露原始 ID）：", ", ".join(refs)])
    return "\n".join(lines).strip()


__all__ = [
    "DEFAULT_CONTEXT_TOKEN_LIMIT",
    "DEFAULT_TASK_RETENTION_DAYS",
    "FEISHU_GROUP_HANDOFF_ACK",
    "FEISHU_GROUP_RUN_TITLE",
    "FEISHU_GROUP_STATE_DIR",
    "FEISHU_GROUP_TASK_TITLE",
    "archive_inactive_feishu_group_tasks",
    "ensure_feishu_group_run",
    "ensure_feishu_group_task",
    "feishu_group_state_dir",
    "feishu_group_user_session_key",
    "group_agent_message",
    "mark_feishu_group_task_interaction",
    "session_task_title",
]
