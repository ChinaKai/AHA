from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from aha_cli.services.skill_management import (
    SkillManagementError,
    create_managed_skill,
    delete_managed_skill,
    get_managed_skill,
    list_managed_skills,
    save_managed_skill,
    skills_root,
)
from aha_cli.web.http_utils import http_response, json_response, parse_json_body


def _head_or_response(method: str, response: bytes) -> bytes:
    return http_response("200 OK", b"", "application/json; charset=utf-8") if method == "HEAD" else response


def _skill_error(exc: SkillManagementError) -> bytes:
    return json_response({"ok": False, "error": str(exc)}, exc.status)


def _skills_payload(root: Path) -> dict:
    return {
        "ok": True,
        "skills_root": str(skills_root(root)),
        "skills": list_managed_skills(root),
    }


def _skill_id_from_path(path: str) -> str:
    route = path.removeprefix("/api/skills/").strip("/")
    return unquote(route)


def skill_route_response(root: Path, method: str, path: str, body: bytes) -> bytes | None:
    try:
        if method in {"GET", "HEAD"} and path == "/api/skills":
            return _head_or_response(method, json_response(_skills_payload(root)))
        if method == "POST" and path == "/api/skills":
            skill = create_managed_skill(root, parse_json_body(body))
            return json_response({"ok": True, "skills_root": str(skills_root(root)), "skill": skill}, "201 Created")
        if path.startswith("/api/skills/"):
            skill_id = _skill_id_from_path(path)
            if method in {"GET", "HEAD"}:
                return _head_or_response(
                    method,
                    json_response({"ok": True, "skills_root": str(skills_root(root)), "skill": get_managed_skill(root, skill_id)}),
                )
            if method in {"PUT", "PATCH"}:
                skill = save_managed_skill(root, skill_id, parse_json_body(body))
                return json_response({"ok": True, "skills_root": str(skills_root(root)), "skill": skill})
            if method == "DELETE":
                delete_managed_skill(root, skill_id)
                return json_response({**_skills_payload(root), "deleted": skill_id})
    except SkillManagementError as exc:
        return _skill_error(exc)
    return None


__all__ = ["skill_route_response"]
