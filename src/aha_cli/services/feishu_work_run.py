from __future__ import annotations

from pathlib import Path

from aha_cli.domain.models import (
    is_feishu_group_run,
    is_service_assistant_run,
    normalize_feishu_integration_config,
)
from aha_cli.store.config import load_config
from aha_cli.store.runs import list_run_summaries, require_plan, run_summary


def _integration_config(root: Path) -> dict:
    integrations = load_config(root).get("integrations")
    raw = integrations.get("feishu") if isinstance(integrations, dict) else None
    return normalize_feishu_integration_config(raw)


def is_feishu_work_run(plan: object) -> bool:
    if not isinstance(plan, dict):
        return False
    if is_service_assistant_run(plan) or is_feishu_group_run(plan):
        return False
    return not bool(plan.get("system_managed"))


def validate_feishu_work_run_id(root: Path, run_id: str) -> str:
    identity = str(run_id or "").strip()
    if not identity:
        return ""
    try:
        plan = require_plan(root, identity)
    except SystemExit as exc:
        raise ValueError(f"飞书默认归属 Run 不存在：{identity}") from exc
    if not is_feishu_work_run(plan):
        raise ValueError("飞书默认归属 Run 不能选择系统 Run")
    return identity


def feishu_work_run_options(root: Path, *, limit: int = 100) -> list[dict]:
    options: list[dict] = []
    for summary in list_run_summaries(root):
        run_id = str(summary.get("id") or "").strip()
        if not run_id:
            continue
        try:
            plan = require_plan(root, run_id)
        except SystemExit:
            continue
        if not is_feishu_work_run(plan):
            continue
        options.append(
            {
                key: summary.get(key)
                for key in (
                    "id",
                    "goal",
                    "status",
                    "lifecycle_status",
                    "created_at",
                    "updated_at",
                    "task_count",
                    "completed_count",
                    "running_task_count",
                )
            }
        )
        if len(options) >= max(1, int(limit or 100)):
            break
    return options


def configured_feishu_work_run_id(root: Path) -> str:
    return str(_integration_config(root).get("default_run_id") or "").strip()


def resolve_feishu_work_run_id(root: Path, explicit_run_id: object = "") -> str:
    requested = str(explicit_run_id or "").strip()
    if requested:
        return validate_feishu_work_run_id(root, requested)
    configured = configured_feishu_work_run_id(root)
    if configured:
        return validate_feishu_work_run_id(root, configured)
    raise ValueError("未配置飞书默认归属 Run，请在飞书助手设置中选择绑定 Run，或在本次操作中指定 run_id")


def feishu_work_run_status(root: Path) -> dict:
    configured = configured_feishu_work_run_id(root)
    error = ""
    default_run: dict | None = None
    if configured:
        try:
            validate_feishu_work_run_id(root, configured)
            default_run = run_summary(root, configured)
        except (SystemExit, ValueError) as exc:
            error = str(exc)
    return {
        "default_run_id": configured if not error else "",
        "configured_default_run_id": configured,
        "default_run_available": bool(configured and not error),
        "default_run_error": error,
        "default_run": default_run,
        "work_run_options": feishu_work_run_options(root),
    }


__all__ = [
    "configured_feishu_work_run_id",
    "feishu_work_run_options",
    "feishu_work_run_status",
    "is_feishu_work_run",
    "resolve_feishu_work_run_id",
    "validate_feishu_work_run_id",
]
