from __future__ import annotations

from datetime import date
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import run_dir
from aha_cli.store.runs import require_plan

MEMO_STATUSES = {"todo", "doing", "done", "closed"}
MEMO_STATUS_ALIASES = {
    "open": "todo",
    "incomplete": "todo",
    "pending": "todo",
    "paused": "todo",
    "running": "doing",
    "blocked": "todo",
    "suspended": "todo",
    "complete": "done",
    "completed": "done",
    "archived": "closed",
}


def task_memos_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / "task_memos.json"


def normalize_memo_status(value: object) -> str:
    status = str(value or "todo").strip().lower().replace("-", "_")
    status = MEMO_STATUS_ALIASES.get(status, status)
    return status if status in MEMO_STATUSES else "todo"


def normalize_memo_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return ""


def normalize_memo_end_date(value: object, scheduled_date: str) -> str:
    end_date = normalize_memo_date(value)
    if not end_date or not scheduled_date or end_date < scheduled_date:
        return ""
    return end_date


def normalize_memo_terminal_timestamp(value: object, scheduled_date: str) -> str:
    text = str(value or "").strip()
    terminal_date = normalize_memo_date(text)
    if not terminal_date:
        return ""
    if scheduled_date and terminal_date < scheduled_date:
        return ""
    return text


def default_memo_terminal_timestamp(scheduled_date: str, now: str) -> str:
    now_date = normalize_memo_date(now)
    if scheduled_date and now_date and now_date < scheduled_date:
        return scheduled_date
    return now


def normalize_memo_max_sub_agents(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return max(1, min(int(value or 3), 16))
    except (TypeError, ValueError):
        return None


def normalize_memo(raw: dict | None = None) -> dict:
    source = raw or {}
    now = utc_now()
    scheduled_date = normalize_memo_date(source.get("scheduled_date") or source.get("date"))
    status = normalize_memo_status(source.get("status"))
    return {
        "id": str(source.get("id") or "").strip(),
        "title": str(source.get("title") or "").strip(),
        "description": str(source.get("description") or "").strip(),
        "status": status,
        "scheduled_date": scheduled_date,
        "end_date": normalize_memo_end_date(source.get("end_date"), scheduled_date),
        "workspace_id": str(source.get("workspace_id") or "").strip(),
        "workspace_path": str(source.get("workspace_path") or "").strip(),
        "backend": str(source.get("backend") or "").strip(),
        "model": str(source.get("model") or "").strip(),
        "sandbox": str(source.get("sandbox") or "").strip(),
        "approval": str(source.get("approval") or "").strip(),
        "proxy_enabled": bool(source.get("proxy_enabled")) if "proxy_enabled" in source else None,
        "collaboration_mode": str(source.get("collaboration_mode") or "auto").strip() or "auto",
        "workflow_template": str(source.get("workflow_template") or "auto").strip() or "auto",
        "delegation_policy": str(source.get("delegation_policy") or "auto").strip() or "auto",
        "max_sub_agents": normalize_memo_max_sub_agents(source.get("max_sub_agents")),
        "preferred_sub_backend": str(source.get("preferred_sub_backend") or source.get("backend") or "").strip(),
        "created_task_id": str(source.get("created_task_id") or "").strip(),
        "converted_at": str(source.get("converted_at") or "").strip(),
        "completed_at": normalize_memo_terminal_timestamp(source.get("completed_at"), scheduled_date) if status == "done" else "",
        "closed_at": normalize_memo_terminal_timestamp(source.get("closed_at"), scheduled_date) if status == "closed" else "",
        "created_at": str(source.get("created_at") or now).strip(),
        "updated_at": str(source.get("updated_at") or now).strip(),
    }


def read_task_memos(root: Path, run_id: str) -> list[dict]:
    require_plan(root, run_id)
    path = task_memos_path(root, run_id)
    if not path.exists():
        return []
    data = read_json(path)
    raw_items = data.get("memos", []) if isinstance(data, dict) else []
    if not isinstance(raw_items, list):
        return []
    return [memo for memo in (normalize_memo(item if isinstance(item, dict) else {}) for item in raw_items) if memo.get("id")]


def write_task_memos(root: Path, run_id: str, memos: list[dict]) -> list[dict]:
    require_plan(root, run_id)
    normalized = [normalize_memo(memo) for memo in memos if memo.get("id")]
    normalized.sort(key=lambda item: (item.get("scheduled_date") or "9999-99-99", item.get("updated_at") or ""), reverse=True)
    write_json(task_memos_path(root, run_id), {"memos": normalized, "updated_at": utc_now()})
    return normalized


def next_memo_id(memos: list[dict]) -> str:
    max_index = 0
    for memo in memos:
        memo_id = str(memo.get("id") or "")
        if memo_id.startswith("memo-"):
            try:
                max_index = max(max_index, int(memo_id.removeprefix("memo-")))
            except ValueError:
                continue
    return f"memo-{max_index + 1:03d}"


def memo_fields_from_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    source = payload.get("memo") if isinstance(payload.get("memo"), dict) else payload
    return {key: value for key, value in source.items() if key != "id"}


def create_task_memo(root: Path, run_id: str, payload: dict) -> dict:
    memos = read_task_memos(root, run_id)
    now = utc_now()
    fields = memo_fields_from_payload(payload)
    status = normalize_memo_status(fields.get("status"))
    scheduled_date = normalize_memo_date(fields.get("scheduled_date") or fields.get("date"))
    provided_completed_at = normalize_memo_terminal_timestamp(fields.get("completed_at"), scheduled_date)
    provided_closed_at = normalize_memo_terminal_timestamp(fields.get("closed_at"), scheduled_date)
    default_terminal = default_memo_terminal_timestamp(scheduled_date, now)
    timestamps = {
        "completed_at": (provided_completed_at or default_terminal) if status == "done" else "",
        "closed_at": (provided_closed_at or default_terminal) if status == "closed" else "",
    }
    memo = normalize_memo(
        {
            **fields,
            **timestamps,
            "id": next_memo_id(memos),
            "created_at": now,
            "updated_at": now,
        }
    )
    if not memo["title"] and not memo["description"]:
        raise ValueError("memo title or description is required")
    write_task_memos(root, run_id, [memo, *memos])
    return memo


def update_task_memo(root: Path, run_id: str, memo_id: str, payload: dict) -> dict:
    memos = read_task_memos(root, run_id)
    fields = memo_fields_from_payload(payload)
    now = utc_now()
    updated: dict | None = None
    next_items = []
    for memo in memos:
        if memo.get("id") == memo_id:
            current_status = normalize_memo_status(memo.get("status"))
            next_status = normalize_memo_status(
                fields.get("status") if "status" in fields else memo.get("status")
            )
            next_scheduled_date = normalize_memo_date(
                fields.get("scheduled_date") if "scheduled_date" in fields else memo.get("scheduled_date")
            )
            raw_created_task_id = (
                fields.get("created_task_id")
                if "created_task_id" in fields
                else memo.get("created_task_id")
            )
            next_created_task_id = str(raw_created_task_id or "").strip()
            converted_at = memo.get("converted_at") or ""
            if "created_task_id" in fields and not next_created_task_id:
                converted_at = ""
            elif next_created_task_id and not converted_at:
                converted_at = now
            completed_at = memo.get("completed_at") or ""
            closed_at = memo.get("closed_at") or ""
            if next_status == "done":
                if "completed_at" in fields:
                    completed_at = (
                        normalize_memo_terminal_timestamp(fields.get("completed_at"), next_scheduled_date)
                        or completed_at
                        or default_memo_terminal_timestamp(next_scheduled_date, now)
                    )
                elif "status" in fields and current_status != "done":
                    completed_at = default_memo_terminal_timestamp(next_scheduled_date, now)
                closed_at = ""
            elif next_status == "closed":
                if "closed_at" in fields:
                    closed_at = (
                        normalize_memo_terminal_timestamp(fields.get("closed_at"), next_scheduled_date)
                        or closed_at
                        or default_memo_terminal_timestamp(next_scheduled_date, now)
                    )
                elif "status" in fields and current_status != "closed":
                    closed_at = default_memo_terminal_timestamp(next_scheduled_date, now)
                completed_at = ""
            else:
                completed_at = ""
                closed_at = ""
            updated = normalize_memo(
                {
                    **memo,
                    **fields,
                    "converted_at": converted_at,
                    "completed_at": completed_at,
                    "closed_at": closed_at,
                    "updated_at": now,
                }
            )
            next_items.append(updated)
        else:
            next_items.append(memo)
    if updated is None:
        raise KeyError(f"memo not found: {memo_id}")
    write_task_memos(root, run_id, next_items)
    return updated


def delete_task_memo(root: Path, run_id: str, memo_id: str) -> dict:
    memos = read_task_memos(root, run_id)
    next_items = [memo for memo in memos if memo.get("id") != memo_id]
    if len(next_items) == len(memos):
        raise KeyError(f"memo not found: {memo_id}")
    write_task_memos(root, run_id, next_items)
    return {"id": memo_id, "deleted": True}
