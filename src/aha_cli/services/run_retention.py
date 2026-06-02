from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tarfile
import time

from aha_cli.constants import PLAN_FILE
from aha_cli.services.run_cleanup import DEFAULT_ACTIVE_HEARTBEAT_SECONDS, run_has_active_heartbeat
from aha_cli.services.run_retention_policy import DEFAULT_POLICY_LIMIT, policy_automation_report
from aha_cli.store.paths import run_dir

RETENTION_MANIFEST = "aha-run-retention-manifest.json"
RETENTION_ARCHIVE_DIR = "run"
RETENTION_ARCHIVE_KIND = "aha.run.retention"
DEFAULT_RETENTION_GROUPS = ("logs", "prompts")
OPTIONAL_RETENTION_GROUPS = ("chat",)
EXCLUDED_RETENTION_GROUPS = {"results", "inbox", "runtime", "sessions", "tasks", "root", "retention"}
DEFAULT_MIN_AGE_SECONDS = 0
RETENTION_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".tar.bz2", ".tar.xz")

AGE_BUCKETS = (
    ("lt_1h", 0, 60 * 60),
    ("1h_1d", 60 * 60, 24 * 60 * 60),
    ("1d_7d", 24 * 60 * 60, 7 * 24 * 60 * 60),
    ("gte_7d", 7 * 24 * 60 * 60, None),
)

RETENTION_NOTES = {
    "logs": "append-only diagnostics; archive or trim old logs before deleting",
    "chat": "backend turn transcripts; archive older turns after final/results are preserved",
    "runtime": "runtime state and locks; only compact stale records with explicit recovery rules",
    "prompts": "prompt artifacts; safe to archive after related task/result snapshots are stable",
    "results": "task outputs and final artifacts; preserve by default",
    "inbox": "message inboxes; preserve while conversation/event replay depends on them",
    "tasks": "legacy task/session layout; preserve until migration rules exist",
    "sessions": "backend session metadata; preserve current session records",
    "root": "plan/events and top-level metadata; preserve by default",
}


class RunRetentionError(Exception):
    def __init__(self, message: str, *, reason: str, status_code: str = "400 Bad Request") -> None:
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


def _metric() -> dict:
    return {"files": 0, "bytes": 0}


def _add_metric(metric: dict, size: int) -> None:
    metric["files"] += 1
    metric["bytes"] += size


def _group_for(path: Path) -> str:
    parts = path.parts
    if len(parts) <= 1:
        return "root"
    return parts[0]


def _age_bucket(age_seconds: float) -> str:
    for name, minimum, maximum in AGE_BUCKETS:
        if age_seconds >= minimum and (maximum is None or age_seconds < maximum):
            return name
    return AGE_BUCKETS[-1][0]


def _sorted_metrics(metrics: dict[str, dict]) -> list[dict]:
    return [
        {"name": name, **value}
        for name, value in sorted(metrics.items(), key=lambda item: (-int(item[1]["bytes"]), item[0]))
    ]


def _validate_run_id(run_id: str) -> str:
    value = str(run_id or "").strip()
    if not value:
        raise ValueError("run id is required")
    if value in {".", ".."} or "/" in value or "\\" in value or Path(value).name != value:
        raise ValueError(f"invalid run id: {run_id}")
    return value


def _run_path(root: Path, run_id: str) -> tuple[str, Path]:
    selected_run_id = _validate_run_id(run_id)
    run_path = run_dir(root, selected_run_id)
    if not run_path.is_dir() or not (run_path / PLAN_FILE).exists():
        raise FileNotFoundError(f"Run not found: {selected_run_id}")
    return selected_run_id, run_path


def _file_rows(run_path: Path, *, now: float) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(run_path.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        relative = path.relative_to(run_path)
        group = _group_for(relative)
        rows.append(
            {
                "path": str(relative),
                "bytes": int(stat.st_size),
                "mtime": stat.st_mtime,
                "group": group,
                "age_seconds": max(0.0, now - stat.st_mtime),
                "_absolute_path": path,
            }
        )
    return rows


def _public_row(row: dict, *, action: str | None = None) -> dict:
    public = {
        "path": row["path"],
        "bytes": row["bytes"],
        "mtime": row["mtime"],
        "group": row["group"],
    }
    if action:
        public["action"] = action
    return public


def _metric_from_rows(rows: list[dict]) -> dict:
    metric = _metric()
    for row in rows:
        _add_metric(metric, int(row["bytes"]))
    return metric


def _normalize_groups(groups: list[str] | tuple[str, ...] | None, *, include_chat: bool = False) -> tuple[str, ...]:
    selected = list(DEFAULT_RETENTION_GROUPS if groups is None else groups)
    if include_chat and "chat" not in selected:
        selected.append("chat")
    normalized: list[str] = []
    allowed = {*DEFAULT_RETENTION_GROUPS, *OPTIONAL_RETENTION_GROUPS}
    for group in selected:
        value = str(group or "").strip()
        if not value:
            continue
        if value not in allowed:
            raise ValueError(f"unsupported retention group: {value}")
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def _retention_candidates(
    rows: list[dict],
    *,
    groups: tuple[str, ...],
    min_age_seconds: int,
) -> list[dict]:
    return [
        row
        for row in rows
        if row["group"] in groups and row["age_seconds"] >= min_age_seconds
    ]


def _policy_dry_run_report(
    rows: list[dict],
    *,
    groups: tuple[str, ...],
    min_age_seconds: int,
    max_total_bytes: int = DEFAULT_POLICY_LIMIT,
    max_candidate_bytes: int = DEFAULT_POLICY_LIMIT,
    min_candidate_files: int = DEFAULT_POLICY_LIMIT,
) -> dict:
    group_names = sorted({str(row["group"]) for row in rows})
    actions: list[dict] = []
    candidates = _retention_candidates(rows, groups=groups, min_age_seconds=min_age_seconds)
    candidate_paths = {row["path"] for row in candidates}
    for group in group_names:
        group_rows = [row for row in rows if row["group"] == group]
        candidate_rows = [row for row in group_rows if row["path"] in candidate_paths]
        protected_rows = [row for row in group_rows if row["path"] not in candidate_paths]
        if group in groups:
            if candidate_rows:
                decision = "would_archive"
                reason = "selected_retention_group"
            else:
                decision = "preserve"
                reason = "below_min_age"
        elif group in OPTIONAL_RETENTION_GROUPS:
            decision = "preserve"
            reason = "optional_group_requires_include_chat"
        elif group in EXCLUDED_RETENTION_GROUPS:
            decision = "preserve"
            reason = "excluded_group"
        else:
            decision = "preserve"
            reason = "not_selected"
        actions.append(
            {
                "group": group,
                "selected": group in groups,
                "decision": decision,
                "reason": reason,
                "candidates": _metric_from_rows(candidate_rows),
                "protected": _metric_from_rows(protected_rows),
            }
        )
    return {
        "dry_run": True,
        "mode": "dry-run",
        "candidate_total": _metric_from_rows(candidates),
        "protected_total": _metric_from_rows([row for row in rows if row["path"] not in candidate_paths]),
        "actions": actions,
        "automation": policy_automation_report(
            rows,
            candidates,
            max_total_bytes=max_total_bytes,
            max_candidate_bytes=max_candidate_bytes,
            min_candidate_files=min_candidate_files,
        ),
    }


def _archive_path(run_path: Path, run_id: str, archive_dir: Path | None, *, now: float) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    destination = archive_dir.expanduser() if archive_dir is not None else run_path / "retention"
    return destination / f"{timestamp}-{run_id}-retention.tar.gz"


def _tar_add_json(archive: tarfile.TarFile, name: str, payload: dict) -> None:
    encoded = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(encoded)
    info.mtime = int(time.time())
    archive.addfile(info, io.BytesIO(encoded))


def _archive_candidates(
    archive_path: Path,
    *,
    run_id: str,
    candidates: list[dict],
    groups: tuple[str, ...],
    min_age_seconds: int,
    delete_after_archive: bool,
    now: float,
) -> dict:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": 1,
        "kind": RETENTION_ARCHIVE_KIND,
        "source_run_id": run_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "groups": list(groups),
        "min_age_seconds": min_age_seconds,
        "delete_after_archive": delete_after_archive,
        "files": [_public_row(row) for row in candidates],
    }
    with tarfile.open(archive_path, "w:gz") as archive:
        _tar_add_json(archive, RETENTION_MANIFEST, manifest)
        for row in candidates:
            archive.add(row["_absolute_path"], str(Path(RETENTION_ARCHIVE_DIR) / row["path"]))
    return {
        "path": str(archive_path),
        "bytes": archive_path.stat().st_size,
        "files": len(candidates),
    }


def _remove_empty_parents(path: Path, stop: Path) -> None:
    parent = path.parent
    while parent != stop and stop in [parent, *parent.parents]:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def _delete_candidates(run_path: Path, candidates: list[dict]) -> list[dict]:
    deleted: list[dict] = []
    for row in candidates:
        path = row["_absolute_path"]
        try:
            path.unlink()
            _remove_empty_parents(path, run_path)
            deleted.append(_public_row(row, action="deleted"))
        except FileNotFoundError:
            deleted.append({**_public_row(row, action="missing"), "error": "file was already removed"})
        except OSError as exc:
            deleted.append({**_public_row(row, action="delete_failed"), "error": str(exc)})
    return deleted


def _is_retention_archive_path(path: Path) -> bool:
    name = path.name
    return any(name.endswith(suffix) for suffix in RETENTION_ARCHIVE_SUFFIXES)


def _validate_archive_file(archive_path: Path) -> Path:
    selected = archive_path.expanduser()
    if not selected.is_file():
        raise RunRetentionError(
            f"Retention archive not found: {selected}",
            reason="archive_not_found",
            status_code="404 Not Found",
        )
    return selected


def _validate_archive_name(archive_name: str) -> str:
    value = str(archive_name or "").strip()
    if not value:
        raise RunRetentionError("Retention archive name is required", reason="archive_name_required")
    if value in {".", ".."} or "/" in value or "\\" in value or Path(value).name != value:
        raise RunRetentionError(f"Invalid retention archive name: {archive_name}", reason="invalid_archive_name")
    if not _is_retention_archive_path(Path(value)):
        raise RunRetentionError(f"Unsupported retention archive name: {archive_name}", reason="invalid_archive_name")
    return value


def _read_retention_manifest(archive: tarfile.TarFile) -> dict:
    try:
        handle = archive.extractfile(RETENTION_MANIFEST)
    except (KeyError, tarfile.TarError) as exc:
        raise RunRetentionError("Invalid retention archive: missing manifest", reason="invalid_archive") from exc
    if handle is None:
        raise RunRetentionError("Invalid retention archive: missing manifest", reason="invalid_archive")
    try:
        manifest = json.loads(handle.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunRetentionError("Invalid retention archive: unreadable manifest", reason="invalid_archive") from exc
    if manifest.get("kind") != RETENTION_ARCHIVE_KIND:
        raise RunRetentionError("Invalid retention archive kind", reason="invalid_archive")
    if int(manifest.get("schema") or 0) != 1:
        raise RunRetentionError("Unsupported retention archive schema", reason="unsupported_archive")
    return manifest


def _validate_archive_member_path(member_name: str) -> Path:
    member_path = Path(member_name)
    if member_name.startswith("/") or ".." in member_path.parts:
        raise RunRetentionError(f"Unsafe retention archive member: {member_name}", reason="unsafe_archive")
    if not member_path.parts or member_path.parts[0] != RETENTION_ARCHIVE_DIR:
        raise RunRetentionError(f"Unexpected retention archive member: {member_name}", reason="invalid_archive")
    relative = Path(*member_path.parts[1:])
    if not relative.parts:
        raise RunRetentionError(f"Unexpected retention archive member: {member_name}", reason="invalid_archive")
    if str(relative) in {".", ".."} or relative.is_absolute() or ".." in relative.parts:
        raise RunRetentionError(f"Unsafe retention archive member: {member_name}", reason="unsafe_archive")
    return relative


def _manifest_file_rows(manifest: dict) -> list[dict]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise RunRetentionError("Invalid retention archive manifest files", reason="invalid_archive")
    rows: list[dict] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise RunRetentionError("Invalid retention archive manifest file row", reason="invalid_archive")
        path_text = str(item.get("path") or "")
        if not path_text or path_text in seen:
            raise RunRetentionError("Invalid retention archive manifest path", reason="invalid_archive")
        relative = Path(path_text)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise RunRetentionError(f"Unsafe retention archive manifest path: {path_text}", reason="unsafe_archive")
        seen.add(path_text)
        rows.append(
            {
                "path": path_text,
                "bytes": int(item.get("bytes") or 0),
                "mtime": item.get("mtime"),
                "group": str(item.get("group") or _group_for(relative)),
            }
        )
    return rows


def _restore_target_for_member(run_path: Path, relative: Path) -> Path:
    target = run_path / relative
    try:
        root_resolved = run_path.resolve(strict=True)
        target_resolved = target.resolve(strict=False)
    except OSError as exc:
        raise RunRetentionError(f"Unsafe restore target: {relative}", reason="unsafe_restore_target") from exc
    if not target_resolved.is_relative_to(root_resolved):
        raise RunRetentionError(f"Unsafe restore target outside run: {relative}", reason="unsafe_restore_target")
    for candidate in (target, *target.parents):
        if candidate == run_path:
            break
        if candidate.exists() and candidate.is_symlink():
            raise RunRetentionError(f"Unsafe restore target uses symlink: {relative}", reason="unsafe_restore_target")
    return target


def inspect_retention_archive(archive_path: Path) -> dict:
    selected = _validate_archive_file(archive_path)
    try:
        with tarfile.open(selected, "r:*") as archive:
            manifest = _read_retention_manifest(archive)
            files = _manifest_file_rows(manifest)
            members = {member.name: member for member in archive.getmembers()}
            expected_members = {str(Path(RETENTION_ARCHIVE_DIR) / item["path"]) for item in files}
            stored_files: list[dict] = []
            missing_files: list[dict] = []
            unexpected_members: list[str] = []
            for item in files:
                member_name = str(Path(RETENTION_ARCHIVE_DIR) / item["path"])
                member = members.get(member_name)
                if member is None:
                    missing_files.append({**item, "member": member_name})
                    stored_files.append({**item, "member": member_name, "stored": False})
                    continue
                relative = _validate_archive_member_path(member.name)
                if str(relative) != item["path"] or not member.isfile():
                    raise RunRetentionError(f"Invalid retention archive member: {member.name}", reason="invalid_archive")
                stored_files.append({**item, "member": member_name, "stored": True, "stored_bytes": int(member.size)})
            for member_name, member in sorted(members.items()):
                if member_name == RETENTION_MANIFEST or member_name in expected_members:
                    continue
                _validate_archive_member_path(member_name)
                if member.isfile():
                    unexpected_members.append(member_name)
    except tarfile.TarError as exc:
        raise RunRetentionError("Invalid retention archive", reason="invalid_archive") from exc

    return {
        "archive": {"path": str(selected), "bytes": selected.stat().st_size},
        "valid": True,
        "schema": int(manifest.get("schema") or 0),
        "kind": manifest.get("kind"),
        "source_run_id": manifest.get("source_run_id"),
        "created_at": manifest.get("created_at"),
        "groups": list(manifest.get("groups") or []),
        "min_age_seconds": int(manifest.get("min_age_seconds") or 0),
        "delete_after_archive": bool(manifest.get("delete_after_archive")),
        "files": stored_files,
        "file_count": len(files),
        "stored_file_count": sum(1 for item in stored_files if item.get("stored")),
        "missing_files": missing_files,
        "unexpected_members": unexpected_members,
        "manifest": manifest,
    }


def retention_archive_path_for_run(root: Path, run_id: str, archive_name: str) -> tuple[str, Path]:
    selected_run_id, run_path = _run_path(root, run_id)
    selected_name = _validate_archive_name(archive_name)
    retention_dir = run_path / "retention"
    if retention_dir.exists() and retention_dir.is_symlink():
        raise RunRetentionError("Unsafe retention archive directory", reason="unsafe_archive")
    archive_path = retention_dir / selected_name
    if archive_path.exists() and archive_path.is_symlink():
        raise RunRetentionError("Unsafe retention archive path", reason="unsafe_archive")
    try:
        root_resolved = retention_dir.resolve(strict=False)
        path_resolved = archive_path.resolve(strict=False)
    except OSError as exc:
        raise RunRetentionError("Unsafe retention archive path", reason="unsafe_archive") from exc
    if not path_resolved.is_relative_to(root_resolved):
        raise RunRetentionError("Unsafe retention archive path outside run retention directory", reason="unsafe_archive")
    return selected_run_id, archive_path


def _ensure_archive_source_matches_run(run_id: str, inspected: dict) -> None:
    if str(inspected.get("source_run_id") or "") != run_id:
        raise RunRetentionError(
            "Retention archive source run does not match the requested run",
            reason="archive_source_mismatch",
            status_code="409 Conflict",
        )


def inspect_run_retention_archive(root: Path, run_id: str, archive_name: str) -> dict:
    selected_run_id, archive_path = retention_archive_path_for_run(root, run_id, archive_name)
    inspected = inspect_retention_archive(archive_path)
    _ensure_archive_source_matches_run(selected_run_id, inspected)
    return {**inspected, "run_id": selected_run_id, "archive_name": archive_path.name}


def _retention_archive_list_paths(root: Path, run_id: str | None, archive_dir: Path | None) -> tuple[str | None, list[Path]]:
    if archive_dir is not None:
        directory = archive_dir.expanduser()
        if not directory.is_dir():
            return (None, [])
        return (str(directory), sorted(path for path in directory.iterdir() if path.is_file() and _is_retention_archive_path(path)))
    if run_id:
        _selected_run_id, run_path = _run_path(root, run_id)
        directory = run_path / "retention"
        if not directory.is_dir():
            return (str(directory), [])
        return (str(directory), sorted(path for path in directory.iterdir() if path.is_file() and _is_retention_archive_path(path)))
    runs_root = run_dir(root, "_").parent
    if not runs_root.is_dir():
        return (str(runs_root), [])
    paths: list[Path] = []
    for directory in sorted(runs_root.glob("*/retention")):
        if directory.is_dir():
            paths.extend(path for path in directory.iterdir() if path.is_file() and _is_retention_archive_path(path))
    return (str(runs_root), sorted(paths))


def list_retention_archives(root: Path, run_id: str | None = None, *, archive_dir: Path | None = None) -> dict:
    selected_run_id = _validate_run_id(run_id) if run_id else None
    scanned, paths = _retention_archive_list_paths(root, selected_run_id, archive_dir)
    archives: list[dict] = []
    errors: list[dict] = []
    for path in paths:
        try:
            inspected = inspect_retention_archive(path)
            if selected_run_id and inspected.get("source_run_id") != selected_run_id:
                continue
            archives.append(
                {
                    "name": Path(inspected["archive"]["path"]).name,
                    "path": inspected["archive"]["path"],
                    "bytes": inspected["archive"]["bytes"],
                    "source_run_id": inspected["source_run_id"],
                    "created_at": inspected["created_at"],
                    "groups": inspected["groups"],
                    "files": inspected["file_count"],
                    "delete_after_archive": inspected["delete_after_archive"],
                }
            )
        except RunRetentionError as exc:
            errors.append({"path": str(path), "error": str(exc), "reason": exc.reason})
    return {
        "run_id": selected_run_id,
        "archive_dir": str(archive_dir.expanduser()) if archive_dir is not None else None,
        "scanned": scanned,
        "archives": archives,
        "errors": errors,
    }


def restore_retention_archive(
    root: Path,
    archive_path: Path,
    *,
    run_id: str | None = None,
    current_run_id: str | None = None,
    active_heartbeat_seconds: int = DEFAULT_ACTIVE_HEARTBEAT_SECONDS,
    force: bool = False,
    now: float | None = None,
) -> dict:
    now = time.time() if now is None else now
    selected_archive = _validate_archive_file(archive_path)
    inspected = inspect_retention_archive(selected_archive)
    target_run_id = _validate_run_id(run_id or str(inspected["source_run_id"] or ""))
    selected_run_id, run_path = _run_path(root, target_run_id)
    if current_run_id and selected_run_id == current_run_id:
        raise RunRetentionError(
            "Cannot restore retention archive to the current run",
            reason="current_run",
            status_code="409 Conflict",
        )
    if run_has_active_heartbeat(run_path, now=now, active_heartbeat_seconds=active_heartbeat_seconds):
        raise RunRetentionError(
            "Cannot restore retention archive to a run with active heartbeat",
            reason="active_heartbeat",
            status_code="409 Conflict",
        )

    manifest_files = _manifest_file_rows(inspected["manifest"])
    restored: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    try:
        with tarfile.open(selected_archive, "r:*") as archive:
            _read_retention_manifest(archive)
            for item in manifest_files:
                member_name = str(Path(RETENTION_ARCHIVE_DIR) / item["path"])
                try:
                    member = archive.getmember(member_name)
                    relative = _validate_archive_member_path(member.name)
                    if str(relative) != item["path"] or not member.isfile():
                        raise RunRetentionError(f"Invalid retention archive member: {member.name}", reason="invalid_archive")
                    target = _restore_target_for_member(run_path, relative)
                    if target.exists() and not force:
                        skipped.append({**item, "action": "exists"})
                        continue
                    handle = archive.extractfile(member)
                    if handle is None:
                        raise RunRetentionError(f"Unreadable retention archive member: {member.name}", reason="invalid_archive")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(handle.read())
                    if member.mtime:
                        os.utime(target, (member.mtime, member.mtime))
                    restored.append({**item, "action": "restored"})
                except KeyError:
                    errors.append({**item, "action": "missing", "error": "archive member is missing"})
                except RunRetentionError as exc:
                    if exc.reason in {"invalid_archive", "unsafe_archive", "unsafe_restore_target"}:
                        raise
                    errors.append({**item, "action": "restore_failed", "error": str(exc)})
                except OSError as exc:
                    errors.append({**item, "action": "restore_failed", "error": str(exc)})
    except tarfile.TarError as exc:
        raise RunRetentionError("Invalid retention archive", reason="invalid_archive") from exc

    return {
        "run_id": selected_run_id,
        "archive": inspected["archive"],
        "archive_name": selected_archive.name,
        "source_run_id": inspected["source_run_id"],
        "force": force,
        "restored": restored,
        "skipped": skipped,
        "errors": errors,
    }


def restore_run_retention_archive(
    root: Path,
    run_id: str,
    archive_name: str,
    *,
    current_run_id: str | None = None,
    active_heartbeat_seconds: int = DEFAULT_ACTIVE_HEARTBEAT_SECONDS,
    force: bool = False,
    now: float | None = None,
) -> dict:
    selected_run_id, archive_path = retention_archive_path_for_run(root, run_id, archive_name)
    inspected = inspect_retention_archive(archive_path)
    _ensure_archive_source_matches_run(selected_run_id, inspected)
    return restore_retention_archive(
        root,
        archive_path,
        run_id=selected_run_id,
        current_run_id=current_run_id,
        active_heartbeat_seconds=active_heartbeat_seconds,
        force=force,
        now=now,
    )


def run_retention_report(
    root: Path,
    run_id: str,
    *,
    top: int = 10,
    now: float | None = None,
    groups: list[str] | tuple[str, ...] | None = None,
    include_chat: bool = False,
    min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
    max_total_bytes: int = DEFAULT_POLICY_LIMIT,
    max_candidate_bytes: int = DEFAULT_POLICY_LIMIT,
    min_candidate_files: int = DEFAULT_POLICY_LIMIT,
) -> dict:
    selected_run_id, run_path = _run_path(root, run_id)

    now = time.time() if now is None else now
    total = _metric()
    group_metrics: dict[str, dict] = {}
    age_buckets = {name: _metric() for name, _minimum, _maximum in AGE_BUCKETS}
    largest_files: list[dict] = []
    selected_groups = _normalize_groups(groups, include_chat=include_chat)
    min_age_seconds = max(0, int(min_age_seconds))
    rows = _file_rows(run_path, now=now)

    for row in rows:
        size = int(row["bytes"])
        group = str(row["group"])
        _add_metric(total, size)
        _add_metric(group_metrics.setdefault(group, _metric()), size)
        _add_metric(age_buckets[_age_bucket(row["age_seconds"])], size)
        largest_files.append(_public_row(row))

    top = max(0, int(top))
    largest_files = sorted(largest_files, key=lambda item: (-int(item["bytes"]), str(item["path"])))[:top]
    candidates = _retention_candidates(rows, groups=selected_groups, min_age_seconds=min_age_seconds)
    notes = [
        {"group": item["name"], "note": RETENTION_NOTES.get(item["name"], "review before compacting")}
        for item in _sorted_metrics(group_metrics)
    ]
    return {
        "run_id": selected_run_id,
        "path": str(run_path),
        "total": total,
        "groups": _sorted_metrics(group_metrics),
        "age_buckets": [{"name": name, **value} for name, value in age_buckets.items()],
        "largest_files": largest_files,
        "policy": {
            "groups": list(selected_groups),
            "optional_groups": list(OPTIONAL_RETENTION_GROUPS),
            "excluded_groups": sorted(EXCLUDED_RETENTION_GROUPS),
            "min_age_seconds": min_age_seconds,
        },
        "policy_report": _policy_dry_run_report(
            rows,
            groups=selected_groups,
            min_age_seconds=min_age_seconds,
            max_total_bytes=max_total_bytes,
            max_candidate_bytes=max_candidate_bytes,
            min_candidate_files=min_candidate_files,
        ),
        "candidates": [_public_row(row, action="would_archive") for row in candidates],
        "retention_notes": notes,
        "dry_run": True,
    }


def apply_run_retention(
    root: Path,
    run_id: str,
    *,
    current_run_id: str | None = None,
    active_heartbeat_seconds: int = DEFAULT_ACTIVE_HEARTBEAT_SECONDS,
    archive_dir: Path | None = None,
    force: bool = False,
    top: int = 10,
    now: float | None = None,
    groups: list[str] | tuple[str, ...] | None = None,
    include_chat: bool = False,
    min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
    max_total_bytes: int = DEFAULT_POLICY_LIMIT,
    max_candidate_bytes: int = DEFAULT_POLICY_LIMIT,
    min_candidate_files: int = DEFAULT_POLICY_LIMIT,
) -> dict:
    now = time.time() if now is None else now
    selected_run_id, run_path = _run_path(root, run_id)
    if current_run_id and selected_run_id == current_run_id:
        raise RunRetentionError(
            "Cannot apply retention to the current run",
            reason="current_run",
            status_code="409 Conflict",
        )
    if run_has_active_heartbeat(run_path, now=now, active_heartbeat_seconds=active_heartbeat_seconds):
        raise RunRetentionError(
            "Cannot apply retention to a run with active heartbeat",
            reason="active_heartbeat",
            status_code="409 Conflict",
        )

    report = run_retention_report(
        root,
        selected_run_id,
        top=top,
        now=now,
        groups=groups,
        include_chat=include_chat,
        min_age_seconds=min_age_seconds,
        max_total_bytes=max_total_bytes,
        max_candidate_bytes=max_candidate_bytes,
        min_candidate_files=min_candidate_files,
    )
    selected_groups = tuple(report["policy"]["groups"])
    rows = _file_rows(run_path, now=now)
    candidates = _retention_candidates(
        rows,
        groups=selected_groups,
        min_age_seconds=int(report["policy"]["min_age_seconds"]),
    )
    result = {
        **report,
        "dry_run": False,
        "apply": True,
        "force": force,
        "archive": None,
        "deleted": [],
        "errors": [],
    }
    result["policy_report"] = {**result["policy_report"], "dry_run": False, "mode": "apply+force" if force else "apply"}
    if not candidates:
        result["candidates"] = []
        return result

    destination = _archive_path(run_path, selected_run_id, archive_dir, now=now)
    archive = _archive_candidates(
        destination,
        run_id=selected_run_id,
        candidates=candidates,
        groups=selected_groups,
        min_age_seconds=int(report["policy"]["min_age_seconds"]),
        delete_after_archive=force,
        now=now,
    )
    result["archive"] = archive
    if force:
        deleted = _delete_candidates(run_path, candidates)
        result["deleted"] = deleted
        result["errors"] = [item for item in deleted if item.get("error")]
        result["candidates"] = deleted
    else:
        result["candidates"] = [_public_row(row, action="archived") for row in candidates]
    return result


def format_retention_report(report: dict) -> str:
    mode = "dry-run"
    if report.get("apply") and report.get("force"):
        mode = "apply+force"
    elif report.get("apply"):
        mode = "apply"
    total = report["total"]
    lines = [
        f"AHA run retention report ({mode}): {report['run_id']}",
        f"path: {report['path']}",
        f"total: {total['files']} files, {total['bytes']} bytes",
        f"policy: groups={','.join(report['policy']['groups']) or '-'}, min_age_seconds={report['policy']['min_age_seconds']}",
        "groups:",
    ]
    for group in report["groups"]:
        lines.append(f"- {group['name']}: {group['files']} files, {group['bytes']} bytes")
    lines.append("candidates:")
    for item in report.get("candidates", []):
        lines.append(f"- {item['action']} {item['path']}: {item['bytes']} bytes")
    if not report.get("candidates"):
        lines.append("- none")
    if report.get("archive"):
        archive = report["archive"]
        lines.append(f"archive: {archive['path']} ({archive['files']} files, {archive['bytes']} bytes)")
    if report.get("deleted"):
        lines.append("deleted:")
        for item in report["deleted"]:
            lines.append(f"- {item['path']}: {item['action']}")
    if report.get("errors"):
        lines.append("Errors:")
        for item in report["errors"]:
            lines.append(f"- {item['path']}: {item['error']}")
    policy_report = report.get("policy_report") or {}
    if policy_report:
        candidate_total = policy_report.get("candidate_total") or {"files": 0, "bytes": 0}
        protected_total = policy_report.get("protected_total") or {"files": 0, "bytes": 0}
        lines.append("policy_dry_run:" if policy_report.get("dry_run") else "policy_report:")
        lines.append(f"- would_archive: {candidate_total['files']} files, {candidate_total['bytes']} bytes")
        lines.append(f"- preserve: {protected_total['files']} files, {protected_total['bytes']} bytes")
        automation = policy_report.get("automation") or {}
        alerts = automation.get("alerts") or []
        thresholds = automation.get("thresholds") or {}
        if any(thresholds.get(key) for key in ("max_total_bytes", "max_candidate_bytes", "min_candidate_files")):
            lines.append(
                f"- automation: over_limit={bool(automation.get('over_limit'))}, "
                f"recommended_action={automation.get('recommended_action') or 'none'}"
            )
            for alert in alerts:
                lines.append(f"  - alert {alert['kind']}: actual={alert['actual']} limit={alert['limit']}")
        for item in policy_report.get("actions", []):
            candidates = item.get("candidates") or {"files": 0, "bytes": 0}
            protected = item.get("protected") or {"files": 0, "bytes": 0}
            lines.append(
                f"- {item['group']}: {item['decision']} ({item['reason']}), "
                f"candidates={candidates['files']}/{candidates['bytes']} bytes, "
                f"preserve={protected['files']}/{protected['bytes']} bytes"
            )
    lines.append("largest_files:")
    for item in report["largest_files"]:
        lines.append(f"- {item['path']}: {item['bytes']} bytes")
    if not report["largest_files"]:
        lines.append("- none")
    lines.append("notes:")
    for item in report["retention_notes"]:
        lines.append(f"- {item['group']}: {item['note']}")
    return "\n".join(lines) + "\n"


def format_retention_archive_list(result: dict) -> str:
    lines = ["AHA retention archives"]
    if result.get("run_id"):
        lines.append(f"run_id: {result['run_id']}")
    if result.get("scanned"):
        lines.append(f"scanned: {result['scanned']}")
    lines.append("archives:")
    for item in result.get("archives", []):
        lines.append(
            f"- {item['path']}: source={item.get('source_run_id') or '-'}, "
            f"files={item['files']}, bytes={item['bytes']}, created_at={item.get('created_at') or '-'}"
        )
    if not result.get("archives"):
        lines.append("- none")
    if result.get("errors"):
        lines.append("Errors:")
        for item in result["errors"]:
            lines.append(f"- {item['path']}: {item['error']}")
    return "\n".join(lines) + "\n"


def format_retention_archive_inspect(result: dict) -> str:
    archive = result["archive"]
    lines = [
        "AHA retention archive inspect",
        f"path: {archive['path']}",
        f"bytes: {archive['bytes']}",
        f"source_run_id: {result.get('source_run_id') or '-'}",
        f"created_at: {result.get('created_at') or '-'}",
        f"policy: groups={','.join(result.get('groups') or []) or '-'}, min_age_seconds={result.get('min_age_seconds', 0)}",
        f"files: {result['stored_file_count']}/{result['file_count']}",
    ]
    if result.get("delete_after_archive"):
        lines.append("delete_after_archive: true")
    lines.append("members:")
    for item in result.get("files", []):
        status = "stored" if item.get("stored") else "missing"
        lines.append(f"- {status} {item['path']}: {item['bytes']} bytes")
    if not result.get("files"):
        lines.append("- none")
    if result.get("unexpected_members"):
        lines.append("unexpected_members:")
        for item in result["unexpected_members"]:
            lines.append(f"- {item}")
    if result.get("missing_files"):
        lines.append("missing_files:")
        for item in result["missing_files"]:
            lines.append(f"- {item['path']}")
    return "\n".join(lines) + "\n"


def format_retention_archive_restore(result: dict) -> str:
    lines = [
        "AHA retention archive restore",
        f"run_id: {result['run_id']}",
        f"archive: {result['archive']['path']}",
        f"restored: {len(result.get('restored') or [])}",
        f"skipped: {len(result.get('skipped') or [])}",
    ]
    if result.get("force"):
        lines.append("force: true")
    if result.get("restored"):
        lines.append("restored_files:")
        for item in result["restored"]:
            lines.append(f"- {item['path']}: {item['bytes']} bytes")
    if result.get("skipped"):
        lines.append("skipped_files:")
        for item in result["skipped"]:
            lines.append(f"- {item['path']}: {item['action']}")
    if result.get("errors"):
        lines.append("Errors:")
        for item in result["errors"]:
            lines.append(f"- {item['path']}: {item['error']}")
    return "\n".join(lines) + "\n"


__all__ = [
    "RunRetentionError",
    "apply_run_retention",
    "format_retention_archive_inspect",
    "format_retention_archive_list",
    "format_retention_archive_restore",
    "format_retention_report",
    "inspect_retention_archive",
    "inspect_run_retention_archive",
    "list_retention_archives",
    "restore_run_retention_archive",
    "restore_retention_archive",
    "retention_archive_path_for_run",
    "run_retention_report",
]
