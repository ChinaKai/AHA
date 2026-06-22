from __future__ import annotations

import uuid
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.store.io import read_json, write_json
from aha_cli.store.knowledge import NAV_DRAFTS_DIR, knowledge_root


def nav_drafts_dir(root: Path, config: dict | None = None) -> Path:
    return knowledge_root(root, config) / NAV_DRAFTS_DIR


def _draft_path(root: Path, config: dict | None, draft_id: str) -> Path:
    return nav_drafts_dir(root, config) / f"{draft_id}.json"


def create_draft(root: Path, config: dict | None, fields: dict) -> dict:
    now = utc_now()
    draft = {
        "id": fields.get("id") or f"navdraft_{uuid.uuid4().hex[:12]}",
        "status": fields.get("status") or "running",
        "created_at": fields.get("created_at") or now,
        "updated_at": fields.get("updated_at") or now,
        **{key: value for key, value in fields.items() if value is not None},
    }
    target = _draft_path(root, config, draft["id"])
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json(target, draft)
    return draft


def read_draft(root: Path, config: dict | None, draft_id: str) -> dict | None:
    path = _draft_path(root, config, draft_id)
    if not path.exists():
        return None
    draft = read_json(path)
    draft["_path"] = str(path)
    return draft


def update_draft(root: Path, config: dict | None, draft_id: str, **fields) -> dict:
    draft = read_draft(root, config, draft_id)
    if draft is None:
        raise FileNotFoundError(f"navigation draft not found: {draft_id}")
    draft.pop("_path", None)
    draft.update({key: value for key, value in fields.items() if value is not None})
    draft["updated_at"] = utc_now()
    write_json(_draft_path(root, config, draft_id), draft)
    return draft


def delete_draft(root: Path, config: dict | None, draft_id: str) -> bool:
    path = _draft_path(root, config, draft_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def list_drafts(root: Path, config: dict | None = None, project_key_value: str | None = None) -> list[dict]:
    target = nav_drafts_dir(root, config)
    if not target.is_dir():
        return []
    drafts: list[dict] = []
    for path in sorted(target.glob("*.json")):
        try:
            draft = read_json(path)
        except (OSError, ValueError):
            continue
        if project_key_value and draft.get("project_key") != project_key_value:
            continue
        draft["_path"] = str(path)
        drafts.append(draft)
    drafts.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return drafts
