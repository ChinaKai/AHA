from __future__ import annotations

import re
from pathlib import Path


_PATH_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)+)(?![A-Za-z0-9_.-])")
_IGNORED_PATH_PREFIXES = (".aha/", "task_memo_assets/")
_IGNORED_COMMAND_PATHS = {
    "bin/bash",
    "bin/sh",
    "kb/map",
    "usr/bin/bash",
    "usr/bin/env",
    "usr/bin/python",
    "usr/bin/python3",
}
_IGNORED_SYSTEM_PATH_PREFIXES = (
    "bin/",
    "dev/",
    "etc/",
    "proc/",
    "sbin/",
    "sys/",
    "tmp/",
    "usr/bin/",
    "usr/local/bin/",
    "var/",
)


def sanitize_context_evidence_record(record: dict, *, root: Path, workspace: Path | None) -> dict:
    if record.get("type") != "context_evidence_result":
        return record
    payload = dict(record)
    knowledge_files: list[str] = []
    ignored_paths: list[str] = []

    def scrub(values: object, *, limit: int = 20) -> list[str]:
        buckets = _sanitize_evidence_paths(values, root=root, workspace=workspace)
        knowledge_files.extend(buckets["knowledge"])
        ignored_paths.extend(buckets["ignored"])
        return _ordered_unique(buckets["workspace"], limit=limit)

    payload["actual_files"] = scrub(payload.get("actual_files"), limit=20)
    if "referenced_files" in payload:
        payload["referenced_files"] = scrub(payload.get("referenced_files"), limit=20)
    if "stale_references" in payload:
        payload["stale_references"] = scrub(payload.get("stale_references"), limit=20)

    existing_knowledge = _sanitize_evidence_paths(payload.get("knowledge_files"), root=root, workspace=workspace)
    existing_ignored = _sanitize_evidence_paths(payload.get("ignored_command_paths"), root=root, workspace=workspace)
    payload["actual_files"] = _ordered_unique([*payload["actual_files"], *existing_knowledge["workspace"]], limit=20)
    knowledge_files.extend(existing_knowledge["knowledge"])
    knowledge_files.extend(existing_ignored["knowledge"])
    ignored_paths.extend([*existing_knowledge["ignored"], *existing_ignored["ignored"]])

    diagnostics_key = "navigation_diagnostics" if isinstance(payload.get("navigation_diagnostics"), dict) else "map_diagnostics"
    diagnostics = payload.get(diagnostics_key)
    if isinstance(diagnostics, dict):
        diagnostics = dict(diagnostics)
        for field in ("actual_files", "adopted_files", "missing_files"):
            if field in diagnostics:
                diagnostics[field] = scrub(diagnostics.get(field), limit=20)
        for field in ("referenced_files", "stale_path_hints", "stale_references"):
            if field in diagnostics:
                diagnostics[field] = scrub(diagnostics.get(field), limit=20)
        diag_knowledge = _sanitize_evidence_paths(diagnostics.get("knowledge_files"), root=root, workspace=workspace)
        diag_ignored = _sanitize_evidence_paths(diagnostics.get("ignored_command_paths"), root=root, workspace=workspace)
        knowledge_files.extend([*diag_knowledge["knowledge"], *diag_ignored["knowledge"]])
        ignored_paths.extend([*diag_knowledge["ignored"], *diag_ignored["ignored"]])
        diagnostics["gap_reasons"] = _sanitize_gap_reasons(
            diagnostics.get("gap_reasons"),
            root=root,
            workspace=workspace,
            knowledge_files=knowledge_files,
            ignored_paths=ignored_paths,
        )
        payload[diagnostics_key] = diagnostics

    routing_health = payload.get("routing_health")
    if isinstance(routing_health, dict):
        payload["routing_health"] = _sanitize_routing_health(
            routing_health,
            root=root,
            workspace=workspace,
            knowledge_files=knowledge_files,
            ignored_paths=ignored_paths,
        )

    payload["maintenance_suggestions"] = _sanitize_maintenance_items(
        payload.get("maintenance_suggestions"),
        root=root,
        workspace=workspace,
        knowledge_files=knowledge_files,
        ignored_paths=ignored_paths,
    )
    payload["maintenance_plan"] = _sanitize_maintenance_items(
        payload.get("maintenance_plan"),
        root=root,
        workspace=workspace,
        knowledge_files=knowledge_files,
        ignored_paths=ignored_paths,
    )

    payload["knowledge_files"] = _ordered_unique(knowledge_files, limit=20)
    payload["ignored_command_paths"] = _ordered_unique(ignored_paths, limit=20)
    if isinstance(payload.get(diagnostics_key), dict):
        payload[diagnostics_key]["knowledge_files"] = payload["knowledge_files"]
        payload[diagnostics_key]["ignored_command_paths"] = payload["ignored_command_paths"]
    return payload


def command_path_observations(commands: list[dict], *, workspace: Path, root: Path) -> dict:
    workspace_files: list[str] = []
    knowledge_files: list[str] = []
    ignored_paths: list[str] = []
    for item in commands:
        command = str(item.get("command") or "")
        for match in _PATH_TOKEN_RE.findall(command):
            clean = match.strip("'\"`.,;:)")
            kind, path = _classify_command_path(clean, workspace=workspace, root=root)
            if kind == "workspace" and path:
                workspace_files.append(path)
            elif kind == "knowledge" and path:
                knowledge_files.append(path)
            elif kind == "ignored" and path:
                ignored_paths.append(path)
    return {
        "workspace_files": _ordered_unique(workspace_files, limit=40),
        "knowledge_files": _ordered_unique(knowledge_files, limit=40),
        "ignored_paths": _ordered_unique(ignored_paths, limit=40),
    }


def ignored_path(path: str) -> bool:
    clean = str(path or "").strip()
    return not clean or clean.startswith(_IGNORED_PATH_PREFIXES)


def _sanitize_evidence_paths(values: object, *, root: Path, workspace: Path | None) -> dict[str, list[str]]:
    workspace_files: list[str] = []
    knowledge_files: list[str] = []
    ignored_paths: list[str] = []
    for value in _path_values(values):
        kind, path = _classify_evidence_path(value, root=root, workspace=workspace)
        if kind == "workspace" and path:
            workspace_files.append(path)
        elif kind == "knowledge" and path:
            knowledge_files.append(path)
        elif kind == "ignored" and path:
            ignored_paths.append(path)
    return {
        "workspace": _ordered_unique(workspace_files, limit=40),
        "knowledge": _ordered_unique(knowledge_files, limit=40),
        "ignored": _ordered_unique(ignored_paths, limit=40),
    }


def _path_values(values: object) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, int, float)):
        return [str(values)]
    if not isinstance(values, list):
        return [str(values)]
    out: list[str] = []
    for item in values:
        if isinstance(item, dict):
            text = item.get("path") or item.get("target") or item.get("file")
            if text:
                out.append(str(text))
        else:
            out.append(str(item))
    return out


def _classify_evidence_path(path: str, *, root: Path, workspace: Path | None) -> tuple[str, str]:
    raw = str(path or "").strip()
    had_leading_slash = raw.startswith("/")
    clean = raw.strip("'\"`.,;:)").replace("\\", "/").strip()
    clean = clean.strip("/")
    if not clean:
        return "ignored", ""
    lower = clean.lower()
    if _looks_like_knowledge_path(clean, root):
        return "knowledge", clean
    if ignored_path(clean) or lower in _IGNORED_COMMAND_PATHS:
        return "ignored", clean
    if workspace is not None:
        for prefix in _path_prefixes(workspace):
            if clean == prefix:
                return "ignored", clean
            if clean.startswith(prefix + "/"):
                rel = clean[len(prefix) + 1 :]
                if rel and not ignored_path(rel):
                    return "workspace", rel
                return "ignored", clean
    if lower.startswith(_IGNORED_SYSTEM_PATH_PREFIXES):
        return "ignored", clean
    if clean.startswith("~") or had_leading_slash:
        return "ignored", clean
    return "workspace", clean


def _sanitize_gap_reasons(
    values: object,
    *,
    root: Path,
    workspace: Path | None,
    knowledge_files: list[str],
    ignored_paths: list[str],
) -> list[dict]:
    if not isinstance(values, list):
        return []
    reasons: list[dict] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        clean = dict(item)
        buckets = _sanitize_evidence_paths(clean.get("paths"), root=root, workspace=workspace)
        clean["paths"] = buckets["workspace"][:8]
        knowledge_files.extend(buckets["knowledge"])
        ignored_paths.extend(buckets["ignored"])
        reasons.append(clean)
        if len(reasons) >= 8:
            break
    return reasons


def _sanitize_routing_health(
    value: dict,
    *,
    root: Path,
    workspace: Path | None,
    knowledge_files: list[str],
    ignored_paths: list[str],
) -> dict:
    health = dict(value)
    for field in ("downrank_paths", "prioritize_paths", "adopted_files"):
        buckets = _sanitize_evidence_paths(health.get(field), root=root, workspace=workspace)
        health[field] = buckets["workspace"][:16]
        knowledge_files.extend(buckets["knowledge"])
        ignored_paths.extend(buckets["ignored"])
    adjustments: list[dict] = []
    for item in health.get("score_adjustments") or []:
        if not isinstance(item, dict):
            continue
        buckets = _sanitize_evidence_paths([item.get("path")], root=root, workspace=workspace)
        knowledge_files.extend(buckets["knowledge"])
        ignored_paths.extend(buckets["ignored"])
        if not buckets["workspace"]:
            continue
        clean = dict(item)
        clean["path"] = buckets["workspace"][0]
        adjustments.append(clean)
        if len(adjustments) >= 24:
            break
    if "score_adjustments" in health:
        health["score_adjustments"] = adjustments
    return health


def _sanitize_maintenance_items(
    values: object,
    *,
    root: Path,
    workspace: Path | None,
    knowledge_files: list[str],
    ignored_paths: list[str],
) -> list[dict]:
    if not isinstance(values, list):
        return []
    out: list[dict] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        clean = dict(item)
        for field in ("files", "source_files"):
            if field not in clean:
                continue
            buckets = _sanitize_evidence_paths(clean.get(field), root=root, workspace=workspace)
            clean[field] = buckets["workspace"][:12]
            knowledge_files.extend(buckets["knowledge"])
            ignored_paths.extend(buckets["ignored"])
        out.append(clean)
        if len(out) >= 20:
            break
    return out


def _classify_command_path(path: str, *, workspace: Path, root: Path) -> tuple[str, str]:
    clean = str(path or "").strip().strip("'\"`.,;:)").replace("\\", "/").strip("/")
    if not clean:
        return "ignored", ""
    if _looks_like_knowledge_path(clean, root):
        return "knowledge", clean
    if ignored_path(clean) or clean.lower() in _IGNORED_COMMAND_PATHS:
        return "ignored", clean
    workspace_prefixes = _path_prefixes(workspace)
    for prefix in workspace_prefixes:
        if clean == prefix:
            return "ignored", clean
        if clean.startswith(prefix + "/"):
            rel = clean[len(prefix) + 1 :]
            if rel and not ignored_path(rel):
                return "workspace", rel
            return "ignored", clean
    if clean.startswith(("dev/", "proc/", "sys/", "tmp/")):
        return "ignored", clean
    if clean.startswith("/"):
        try:
            absolute = Path(clean).expanduser()
            rel = absolute.relative_to(workspace.expanduser().resolve())
        except (OSError, ValueError):
            return "ignored", clean
        rel_text = rel.as_posix()
        if rel_text and not ignored_path(rel_text):
            return "workspace", rel_text
        return "ignored", clean
    if clean.startswith("~"):
        return "ignored", clean
    candidate = workspace / clean
    try:
        exists = candidate.exists()
    except OSError:
        exists = False
    if exists and not ignored_path(clean):
        return "workspace", clean
    return "ignored", clean


def _path_prefixes(path: Path) -> list[str]:
    prefixes: list[str] = []
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path.expanduser()
    for value in {str(path.expanduser()), str(resolved)}:
        clean = value.replace("\\", "/").strip("/")
        if clean:
            prefixes.append(clean)
    return _ordered_unique(prefixes, limit=4)


def _looks_like_knowledge_path(path: str, root: Path) -> bool:
    clean = str(path or "").replace("\\", "/").strip("/")
    if ".aha/knowledge/" in clean or clean.startswith("knowledge/"):
        return True
    if re.match(r"^(projects/[^/]+/(navigation|solutions)|general/(wiki|solutions)|personal/(wiki|solutions))(/|$)", clean):
        return True
    for prefix in _path_prefixes(root / "knowledge"):
        if clean == prefix or clean.startswith(prefix + "/"):
            return True
    return False


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
