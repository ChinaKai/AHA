from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import time

from aha_cli.constants import PLAN_FILE, RUNS_DIR
from aha_cli.store.io import read_json
from aha_cli.store.paths import aha_home_path

TEMP_RUN_MARKERS = (".aha-temp-run", ".temporary-run", "temporary-run")
TEMP_RUN_SOURCES = {"smoke", "test", "temporary", "temp"}
DEFAULT_STALE_SECONDS = 60 * 60
DEFAULT_ACTIVE_HEARTBEAT_SECONDS = 2 * 60


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _absolute(path: Path) -> Path:
    return path.expanduser().absolute()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _allowed_tmp_roots() -> list[Path]:
    candidates = {Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp")}
    return sorted((_resolved(path) for path in candidates), key=str)


def tmp_root_allowed(tmp_root: Path) -> bool:
    resolved = _resolved(tmp_root)
    return any(resolved == root or _is_relative_to(resolved, root) for root in _allowed_tmp_roots())


def _safe_stat_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _tree_mtime(path: Path) -> float:
    latest = _safe_stat_mtime(path)
    if not path.exists():
        return latest
    try:
        children = path.rglob("*")
    except OSError:
        return latest
    for child in children:
        latest = max(latest, _safe_stat_mtime(child))
    return latest


def _is_stale(path: Path, *, now: float, stale_seconds: int) -> bool:
    return now - _tree_mtime(path) >= stale_seconds


def _read_plan(run_path: Path) -> dict | None:
    plan = run_path / PLAN_FILE
    if not plan.exists():
        return None
    try:
        return read_json(plan)
    except (OSError, ValueError):
        return None


def _has_temp_marker(run_path: Path, plan: dict | None) -> bool:
    if any((run_path / marker).exists() for marker in TEMP_RUN_MARKERS):
        return True
    if not isinstance(plan, dict):
        return False
    metadata = plan.get("metadata")
    return (
        plan.get("temporary") is True
        or plan.get("temp") is True
        or (isinstance(metadata, dict) and metadata.get("temporary") is True)
        or str(plan.get("source") or "").strip().lower() in TEMP_RUN_SOURCES
    )


def _tail_text(path: Path, limit: int = 65536) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def run_has_active_heartbeat(
    run_path: Path,
    *,
    now: float | None = None,
    active_heartbeat_seconds: int = DEFAULT_ACTIVE_HEARTBEAT_SECONDS,
) -> bool:
    now = time.time() if now is None else now
    log_path = run_path / "logs" / "realtime-debug.log"
    if now - _safe_stat_mtime(log_path) > active_heartbeat_seconds:
        return False
    return "heartbeat" in _tail_text(log_path)


def classify_run_cleanup_candidate(
    run_path: Path,
    *,
    current_run_id: str | None = None,
    now: float | None = None,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    active_heartbeat_seconds: int = DEFAULT_ACTIVE_HEARTBEAT_SECONDS,
) -> dict:
    now = time.time() if now is None else now
    run_id = run_path.name
    base = {"kind": "run", "run_id": run_id, "path": str(run_path)}
    if run_path.is_symlink():
        return {**base, "action": "protect", "reason": "symlink_run"}
    if current_run_id and run_id == current_run_id:
        return {**base, "action": "protect", "reason": "current_run"}
    if run_has_active_heartbeat(run_path, now=now, active_heartbeat_seconds=active_heartbeat_seconds):
        return {**base, "action": "protect", "reason": "active_heartbeat"}

    plan = _read_plan(run_path)
    temp_marked = _has_temp_marker(run_path, plan)
    if plan and not temp_marked:
        return {**base, "action": "protect", "reason": "non_temporary_run"}

    if not _is_stale(run_path, now=now, stale_seconds=stale_seconds):
        return {**base, "action": "protect", "reason": "recent_temporary_run" if temp_marked else "recent_orphan_run"}
    return {**base, "action": "delete", "reason": "stale_temporary_run" if temp_marked else "stale_orphan_run"}


def _run_candidates(aha_home: Path) -> list[Path]:
    runs_dir = aha_home_path(aha_home) / RUNS_DIR
    if not runs_dir.is_dir():
        return []
    return sorted(path for path in runs_dir.iterdir() if path.is_dir())


def _tmp_aha_candidates(tmp_root: Path, aha_home: Path) -> list[Path]:
    tmp_root = _resolved(tmp_root)
    candidates: list[Path] = []
    direct = tmp_root / ".aha"
    try:
        direct_is_dir = direct.is_dir()
    except OSError:
        direct_is_dir = False
    if direct_is_dir:
        candidates.append(direct)
    if tmp_root.is_dir():
        try:
            children = sorted(tmp_root.iterdir())
        except OSError:
            children = []
        for child in children:
            nested = child / ".aha"
            try:
                is_nested_aha = child.is_dir() and nested.is_dir()
            except OSError:
                is_nested_aha = False
            if is_nested_aha:
                candidates.append(nested)
    aha_home = _resolved(aha_home)
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        resolved = _resolved(candidate)
        key = str(resolved)
        if key not in seen and resolved != aha_home:
            seen.add(key)
            unique.append(_absolute(candidate))
    return unique


def classify_tmp_aha_candidate(
    aha_path: Path,
    *,
    current_run_id: str | None = None,
    now: float | None = None,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    active_heartbeat_seconds: int = DEFAULT_ACTIVE_HEARTBEAT_SECONDS,
) -> dict:
    now = time.time() if now is None else now
    base = {"kind": "tmp_aha_home", "path": str(aha_path)}
    if aha_path.is_symlink():
        return {**base, "action": "protect", "reason": "symlink_aha_home"}
    protected_reasons: list[str] = []
    deletable_seen = False
    for run_path in _run_candidates(aha_path):
        candidate = classify_run_cleanup_candidate(
            run_path,
            current_run_id=current_run_id,
            now=now,
            stale_seconds=stale_seconds,
            active_heartbeat_seconds=active_heartbeat_seconds,
        )
        if candidate["action"] == "delete":
            deletable_seen = True
        else:
            protected_reasons.append(f"{candidate['run_id']}:{candidate['reason']}")
    if protected_reasons:
        return {**base, "action": "protect", "reason": "protected_runs_present", "details": protected_reasons}
    if not deletable_seen and (aha_path / "config.json").exists():
        return {**base, "action": "protect", "reason": "non_temporary_aha_home"}
    if not deletable_seen and not _is_stale(aha_path, now=now, stale_seconds=stale_seconds):
        return {**base, "action": "protect", "reason": "recent_tmp_aha_home"}
    return {**base, "action": "delete", "reason": "stale_tmp_aha_home"}


def cleanup_temp_runs(
    aha_home: Path,
    *,
    current_run_id: str | None = None,
    tmp_root: Path | None = None,
    dry_run: bool = True,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    active_heartbeat_seconds: int = DEFAULT_ACTIVE_HEARTBEAT_SECONDS,
    allow_non_temp_root: bool = False,
    now: float | None = None,
) -> dict:
    now = time.time() if now is None else now
    aha_home = aha_home_path(aha_home)
    result = {"dry_run": dry_run, "candidates": [], "deleted": [], "protected": [], "errors": []}

    candidates = [
        classify_run_cleanup_candidate(
            run_path,
            current_run_id=current_run_id,
            now=now,
            stale_seconds=stale_seconds,
            active_heartbeat_seconds=active_heartbeat_seconds,
        )
        for run_path in _run_candidates(aha_home)
    ]
    if tmp_root is not None and not allow_non_temp_root and not tmp_root_allowed(tmp_root):
        result["errors"].append(
            {
                "kind": "tmp_root",
                "path": str(Path(tmp_root).expanduser()),
                "reason": "unsafe_tmp_root",
                "error": "Refusing to scan tmp_root outside the system temporary directory without allow_non_temp_root",
            }
        )
    elif tmp_root is not None:
        candidates.extend(
            classify_tmp_aha_candidate(
                aha_path,
                current_run_id=current_run_id,
                now=now,
                stale_seconds=stale_seconds,
                active_heartbeat_seconds=active_heartbeat_seconds,
            )
            for aha_path in _tmp_aha_candidates(tmp_root, aha_home)
        )

    for candidate in candidates:
        if candidate["action"] != "delete":
            result["candidates"].append(candidate)
            result["protected"].append(candidate)
            continue
        if dry_run:
            candidate = {**candidate, "action": "would_delete"}
            result["candidates"].append(candidate)
            result["deleted"].append(candidate)
            continue
        try:
            shutil.rmtree(candidate["path"])
            candidate = {**candidate, "action": "deleted"}
            result["candidates"].append(candidate)
            result["deleted"].append(candidate)
        except OSError as exc:
            error = {**candidate, "error": str(exc)}
            result["candidates"].append(error)
            result["errors"].append(error)
    return result


def format_cleanup_summary(result: dict) -> str:
    mode = "apply" if not result["dry_run"] else "dry-run"
    lines = [f"AHA temp run cleanup ({mode})"]
    for candidate in result["candidates"]:
        label = candidate.get("run_id") or candidate["path"]
        lines.append(f"- {candidate['action']} {candidate['kind']} {label} ({candidate['reason']})")
    if not result["candidates"]:
        lines.append("- no candidates")
    if result["errors"]:
        lines.append("Errors:")
        for error in result["errors"]:
            lines.append(f"- {error['path']}: {error['error']}")
    return "\n".join(lines) + "\n"
