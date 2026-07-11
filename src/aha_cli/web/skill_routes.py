from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from aha_cli.services.skill_management import (
    SkillManagementError,
    create_managed_skill,
    delete_managed_skill,
    get_managed_skill,
    legacy_skills_root,
    list_managed_skills,
    save_managed_skill,
    skills_root,
)
from aha_cli.web.http_utils import http_response, json_response, parse_json_body


def _head_or_response(method: str, response: bytes) -> bytes:
    return http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response


def _skill_error(exc: SkillManagementError) -> bytes:
    return json_response({"ok": False, "error": str(exc)}, exc.status)


def _payload_value(payload: dict | None, query: dict[str, list[str]] | None, key: str) -> str:
    if isinstance(payload, dict) and payload.get(key) is not None:
        return str(payload.get(key) or "")
    values = (query or {}).get(key) or []
    return str(values[0] or "") if values else ""


def _workspace_from_request(query: dict[str, list[str]] | None = None, payload: dict | None = None) -> Path | None:
    workspace = _payload_value(payload, query, "workspace_path") or _payload_value(payload, query, "workspace")
    return Path(workspace).expanduser() if workspace else None


def _skills_payload(root: Path, workspace: Path | None = None) -> dict:
    return {
        "ok": True,
        "skills_root": str(skills_root(root, workspace)),
        "legacy_skills_root": str(legacy_skills_root(root)),
        "skills": list_managed_skills(root, workspace),
    }


def _skill_id_from_path(path: str) -> str:
    route = path.removeprefix("/api/skills/").strip("/")
    return unquote(route)


def skill_route_response(
    root: Path,
    method: str,
    path: str,
    query: dict[str, list[str]] | None,
    body: bytes,
) -> bytes | None:
    try:
        if method in {"GET", "HEAD"} and path == "/api/skills":
            workspace = _workspace_from_request(query)
            return _head_or_response(method, json_response(_skills_payload(root, workspace)))
        if method == "POST" and path == "/api/skills":
            payload = parse_json_body(body)
            workspace = _workspace_from_request(query, payload)
            skill = create_managed_skill(root, payload, workspace)
            return json_response({**_skills_payload(root, workspace), "skill": skill}, "201 Created")
        if path.startswith("/api/skills/"):
            skill_id = _skill_id_from_path(path)
            workspace = _workspace_from_request(query)
            if method in {"GET", "HEAD"}:
                return _head_or_response(
                    method,
                    json_response({
                        **_skills_payload(root, workspace),
                        "skill": get_managed_skill(root, skill_id, workspace),
                    }),
                )
            if method in {"PUT", "PATCH"}:
                payload = parse_json_body(body)
                workspace = _workspace_from_request(query, payload)
                skill = save_managed_skill(root, skill_id, payload, workspace)
                return json_response({**_skills_payload(root, workspace), "skill": skill})
            if method == "DELETE":
                delete_managed_skill(root, skill_id, workspace)
                return json_response({**_skills_payload(root, workspace), "deleted": skill_id})
    except SkillManagementError as exc:
        return _skill_error(exc)
    return None


__all__ = ["skill_route_response"]
