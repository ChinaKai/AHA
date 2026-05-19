from __future__ import annotations

import io
import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from aha_cli.domain.models import new_run_id, utc_now
from aha_cli.store.filesystem import append_event, run_dir

ARCHIVE_MANIFEST = "aha-run-manifest.json"
ARCHIVE_RUN_DIR = "run"
ARCHIVE_SCHEMA = 1
REDACTED = "<redacted>"

EXCLUDED_TOP_LEVEL_DIRS = {"runtime"}
EXCLUDED_SUFFIXES = {".lock", ".pid", ".tmp"}
PROXY_SECRET_FIELDS = {
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "preferred_http_proxy",
    "preferred_https_proxy",
    "preferred_no_proxy",
}


class RunArchiveError(RuntimeError):
    pass


def export_run_archive(root: Path, run_id: str, output: Path, include_logs: bool = True) -> Path:
    source = run_dir(root, run_id)
    if not source.is_dir():
        raise RunArchiveError(f"Run not found: {run_id}")
    if output.suffix == ".zst" or output.name.endswith(".tar.zst"):
        raise RunArchiveError(".tar.zst export requires zstd support; use .tar.gz for now")
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": ARCHIVE_SCHEMA,
        "kind": "aha.run.export",
        "source_run_id": run_id,
        "exported_at": utc_now(),
        "include_logs": include_logs,
        "excluded": sorted(EXCLUDED_TOP_LEVEL_DIRS),
        "redacted_fields": sorted(PROXY_SECRET_FIELDS),
    }
    with tarfile.open(output, _write_tar_mode(output)) as archive:
        _add_bytes(archive, ARCHIVE_MANIFEST, _json_bytes(manifest))
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source)
            if _should_skip(relative, include_logs=include_logs):
                continue
            archive_name = str(Path(ARCHIVE_RUN_DIR) / relative)
            if _is_structured_text(relative):
                payload = _export_text(path, relative)
                _add_bytes(archive, archive_name, payload, source_path=path)
            else:
                archive.add(path, archive_name)
    return output


def import_run_archive(
    root: Path,
    archive_path: Path,
    *,
    target_run_id: str | None = None,
    preserve_id: bool = False,
    force: bool = False,
) -> tuple[str, str]:
    archive_path = archive_path.expanduser()
    if not archive_path.is_file():
        raise RunArchiveError(f"Archive not found: {archive_path}")
    with tempfile.TemporaryDirectory(prefix="aha-run-import-") as tmp:
        extract_root = Path(tmp)
        with tarfile.open(archive_path, "r:*") as archive:
            _safe_extract(archive, extract_root)
        manifest_path = extract_root / ARCHIVE_MANIFEST
        source_root = extract_root / ARCHIVE_RUN_DIR
        if not manifest_path.is_file() or not source_root.is_dir():
            raise RunArchiveError("Invalid AHA run archive")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("kind") != "aha.run.export":
            raise RunArchiveError("Invalid AHA run archive kind")
        source_run_id = str(manifest.get("source_run_id") or "")
        if not source_run_id:
            raise RunArchiveError("Archive is missing source_run_id")
        imported_run_id = target_run_id or (source_run_id if preserve_id else new_run_id())
        destination = run_dir(root, imported_run_id)
        if destination.exists():
            if not force:
                raise RunArchiveError(f"Run already exists: {imported_run_id}")
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_imported_run(source_root, destination, source_run_id, imported_run_id)
        append_event(
            root,
            imported_run_id,
            "run_imported",
            {
                "source_run_id": source_run_id,
                "archive": str(archive_path),
            },
        )
        return source_run_id, imported_run_id


def _write_tar_mode(path: Path) -> str:
    name = path.name
    if name.endswith((".tar.gz", ".tgz")):
        return "w:gz"
    if name.endswith(".tar.bz2"):
        return "w:bz2"
    if name.endswith(".tar.xz"):
        return "w:xz"
    if name.endswith(".tar"):
        return "w"
    return "w:gz"


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    base = destination.resolve()
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if base != target and base not in target.parents:
            raise RunArchiveError(f"Unsafe archive member: {member.name}")
        if member.name.startswith("/") or ".." in Path(member.name).parts:
            raise RunArchiveError(f"Unsafe archive member: {member.name}")
    archive.extractall(destination, filter="data")


def _should_skip(relative: Path, *, include_logs: bool) -> bool:
    if relative.parts and relative.parts[0] in EXCLUDED_TOP_LEVEL_DIRS:
        return True
    if not include_logs and relative.parts and relative.parts[0] == "logs":
        return True
    return relative.suffix in EXCLUDED_SUFFIXES or any(part.endswith(".tmp") for part in relative.parts)


def _is_structured_text(relative: Path) -> bool:
    return relative.suffix == ".json" or relative.name.endswith(".jsonl")


def _export_text(path: Path, relative: Path) -> bytes:
    if relative.suffix == ".json":
        return _json_bytes(_transform_export(json.loads(path.read_text(encoding="utf-8"))))
    records: list[bytes] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = _transform_export(json.loads(line))
            records.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        except json.JSONDecodeError:
            records.append(line.encode("utf-8") + b"\n")
    return b"".join(records)


def _copy_imported_run(source: Path, destination: Path, source_run_id: str, target_run_id: str) -> None:
    imported_at = utc_now()
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if _is_structured_text(relative):
            payload = _import_text(path, relative, source_run_id, target_run_id, imported_at)
            target.write_bytes(payload)
        else:
            shutil.copy2(path, target)


def _import_text(path: Path, relative: Path, source_run_id: str, target_run_id: str, imported_at: str) -> bytes:
    if relative.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        data = _transform_import(data, source_run_id, target_run_id)
        if relative == Path("plan.json"):
            data["id"] = target_run_id
        if _is_session_file(relative):
            data = _mark_session_imported(data, source_run_id, imported_at)
        return _json_bytes(data)
    records: list[bytes] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = _transform_import(json.loads(line), source_run_id, target_run_id)
            records.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        except json.JSONDecodeError:
            records.append(line.encode("utf-8") + b"\n")
    return b"".join(records)


def _is_session_file(relative: Path) -> bool:
    return relative.suffix == ".json" and "sessions" in relative.parts


def _transform_export(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in PROXY_SECRET_FIELDS:
                result[key] = REDACTED if item else item
                continue
            if key == "backend_session_id":
                if item:
                    result.setdefault("imported_backend_session_id", item)
                result[key] = None
                continue
            result[key] = _transform_export(item)
        return result
    if isinstance(value, list):
        return [_transform_export(item) for item in value]
    return value


def _transform_import(value: Any, source_run_id: str, target_run_id: str) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "run_id" and item == source_run_id:
                result[key] = target_run_id
            elif key == "scope" and isinstance(item, str):
                result[key] = item.replace(f"run:{source_run_id}", f"run:{target_run_id}")
            else:
                result[key] = _transform_import(item, source_run_id, target_run_id)
        return result
    if isinstance(value, list):
        return [_transform_import(item, source_run_id, target_run_id) for item in value]
    return value


def _mark_session_imported(session: dict, source_run_id: str, imported_at: str) -> dict:
    if session.get("backend_session_id") and not session.get("imported_backend_session_id"):
        session["imported_backend_session_id"] = session["backend_session_id"]
    session["backend_session_id"] = None
    session["status"] = "imported"
    session["imported_from_run_id"] = source_run_id
    session["imported_at"] = imported_at
    return session


def _json_bytes(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes, source_path: Path | None = None) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    if source_path is not None:
        stat = source_path.stat()
        info.mtime = int(stat.st_mtime)
        info.mode = stat.st_mode & 0o777
    else:
        info.mtime = 0
        info.mode = 0o644
    archive.addfile(info, io.BytesIO(payload))
