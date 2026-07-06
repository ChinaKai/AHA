from __future__ import annotations


_GROWTH_TARGETS = {"project_navigation", "project_solution"}


def kb_growth_state_for_plan(*, maintenance_plan: list[dict], prior_records: list[dict], dirty_paths: list[str]) -> dict:
    required = _growth_requirements(maintenance_plan)
    if not required:
        return {
            "status": "not_required",
            "required_count": 0,
            "applied_count": 0,
            "pending_count": 0,
            "required": [],
            "applied": [],
            "pending": [],
            "updated_refs": [],
            "dirty_refs": [],
        }
    updated_refs = _agent_updated_refs(prior_records)
    dirty_refs = _ordered_unique([str(path) for path in dirty_paths], limit=40)
    applied: list[dict] = []
    pending: list[dict] = []
    for item in required:
        match = _matching_ref(item, updated_refs=updated_refs, dirty_refs=dirty_refs)
        if match:
            applied.append({**item, **match})
        else:
            pending.append(item)
    status = "applied" if not pending else "pending"
    return {
        "status": status,
        "required_count": len(required),
        "applied_count": len(applied),
        "pending_count": len(pending),
        "required": required,
        "applied": applied,
        "pending": pending,
        "updated_refs": updated_refs[:20],
        "dirty_refs": dirty_refs[:20],
    }


def _growth_requirements(maintenance_plan: list[dict]) -> list[dict]:
    requirements: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in maintenance_plan:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "").strip()
        if target not in _GROWTH_TARGETS:
            continue
        target_path = str(item.get("target_path") or "").strip()
        action = str(item.get("action") or "").strip()
        reason = str(item.get("reason") or "").strip()
        write_policy = str(item.get("write_policy") or "").strip()
        execution = item.get("execution") if isinstance(item.get("execution"), dict) else {}
        severity = "required"
        key = (action, target, target_path, reason)
        if key in seen:
            continue
        seen.add(key)
        requirements.append({
            "action": action,
            "target": target,
            "target_path": target_path,
            "reason": reason,
            "write_policy": write_policy,
            "execution_state": str(execution.get("state") or ""),
            "severity": severity,
        })
        if len(requirements) >= 12:
            break
    return requirements


def _agent_updated_refs(records: list[dict]) -> list[str]:
    refs: list[str] = []
    for record in records:
        if record.get("type") != "agent_kb_feedback":
            continue
        feedback = record.get("feedback") if isinstance(record.get("feedback"), dict) else {}
        for value in feedback.get("updated") or []:
            refs.append(str(value))
    return _ordered_unique(refs, limit=40)


def _matching_ref(item: dict, *, updated_refs: list[str], dirty_refs: list[str]) -> dict:
    target_path = str(item.get("target_path") or "").strip()
    for ref in updated_refs:
        if _path_matches(target_path, ref):
            return {"source": "agent_kb_feedback", "matched_ref": ref}
    for ref in dirty_refs:
        if _path_matches(target_path, ref):
            return {"source": "dirty_path", "matched_ref": ref}
    return {}


def _path_matches(target_path: str, observed: str) -> bool:
    target = _normalize_path(target_path)
    candidate = _normalize_path(observed)
    if not target or not candidate:
        return False
    if target.endswith("/"):
        return candidate.startswith(target)
    return candidate == target or candidate.endswith(f"/{target}") or target.endswith(f"/{candidate}")


def _normalize_path(value: str) -> str:
    text = str(value or "").strip().strip("'\"`.,;:)").replace("\\", "/")
    for marker in ("/.aha/knowledge/", "knowledge/"):
        if marker in text:
            text = text.split(marker, 1)[1]
            break
    return text.strip("/")


def _ordered_unique(values: list[str], *, limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= limit:
            break
    return out
