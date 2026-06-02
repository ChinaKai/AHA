from __future__ import annotations

import datetime as dt
from pathlib import Path
import time

from aha_cli.constants import PLAN_FILE, RUNS_DIR
from aha_cli.domain.models import default_retention_policy_config, utc_now
from aha_cli.services.run_cleanup import DEFAULT_ACTIVE_HEARTBEAT_SECONDS, run_has_active_heartbeat
from aha_cli.store.io import write_json
from aha_cli.store.paths import aha_home_path, run_dir

DEFAULT_POLICY_LIMIT = 0
RETENTION_POLICY_REPORTS_DIR = "reports/retention-policy"


def _metric() -> dict:
    return {"files": 0, "bytes": 0}


def _metric_from_rows(rows: list[dict]) -> dict:
    metric = _metric()
    for row in rows:
        metric["files"] += 1
        metric["bytes"] += int(row["bytes"])
    return metric


def _add_metric(target: dict, source: dict) -> None:
    target["files"] += int(source.get("files") or 0)
    target["bytes"] += int(source.get("bytes") or 0)


def _iter_run_ids(root: Path) -> list[str]:
    runs_root = aha_home_path(root) / RUNS_DIR
    if not runs_root.is_dir():
        return []
    return sorted(path.parent.name for path in runs_root.glob(f"*/{PLAN_FILE}"))


def _apply_guard(
    run_path: Path,
    run_id: str,
    *,
    current_run_id: str | None,
    active_heartbeat_seconds: int,
    now: float,
) -> dict:
    if current_run_id and run_id == current_run_id:
        return {"action": "protect", "reason": "current_run"}
    if run_has_active_heartbeat(run_path, now=now, active_heartbeat_seconds=active_heartbeat_seconds):
        return {"action": "protect", "reason": "active_heartbeat"}
    return {"action": "allow", "reason": "inactive_non_current_run"}


def policy_thresholds(
    *,
    max_total_bytes: int = DEFAULT_POLICY_LIMIT,
    max_candidate_bytes: int = DEFAULT_POLICY_LIMIT,
    min_candidate_files: int = DEFAULT_POLICY_LIMIT,
) -> dict:
    return {
        "max_total_bytes": max(0, int(max_total_bytes or 0)),
        "max_candidate_bytes": max(0, int(max_candidate_bytes or 0)),
        "min_candidate_files": max(0, int(min_candidate_files or 0)),
    }


def policy_automation_report(
    rows: list[dict],
    candidates: list[dict],
    *,
    max_total_bytes: int = DEFAULT_POLICY_LIMIT,
    max_candidate_bytes: int = DEFAULT_POLICY_LIMIT,
    min_candidate_files: int = DEFAULT_POLICY_LIMIT,
) -> dict:
    thresholds = policy_thresholds(
        max_total_bytes=max_total_bytes,
        max_candidate_bytes=max_candidate_bytes,
        min_candidate_files=min_candidate_files,
    )
    total = _metric_from_rows(rows)
    candidate_total = _metric_from_rows(candidates)
    alerts: list[dict] = []
    if thresholds["max_total_bytes"] and total["bytes"] > thresholds["max_total_bytes"]:
        alerts.append(
            {
                "kind": "total_bytes_over_limit",
                "actual": total["bytes"],
                "limit": thresholds["max_total_bytes"],
            }
        )
    if thresholds["max_candidate_bytes"] and candidate_total["bytes"] > thresholds["max_candidate_bytes"]:
        alerts.append(
            {
                "kind": "candidate_bytes_over_limit",
                "actual": candidate_total["bytes"],
                "limit": thresholds["max_candidate_bytes"],
            }
        )
    if thresholds["min_candidate_files"] and candidate_total["files"] >= thresholds["min_candidate_files"]:
        alerts.append(
            {
                "kind": "candidate_files_at_threshold",
                "actual": candidate_total["files"],
                "limit": thresholds["min_candidate_files"],
            }
        )
    over_limit = bool(alerts)
    eligible_for_apply = candidate_total["files"] > 0
    return {
        "thresholds": thresholds,
        "alerts": alerts,
        "over_limit": over_limit,
        "eligible_for_apply": eligible_for_apply,
        "recommended_action": "apply_retention" if over_limit and eligible_for_apply else "review_policy" if over_limit else "none",
        "auto_applied": False,
    }


def enforce_run_retention_policy(
    root: Path,
    run_id: str,
    *,
    apply: bool = False,
    current_run_id: str | None = None,
    active_heartbeat_seconds: int = DEFAULT_ACTIVE_HEARTBEAT_SECONDS,
    archive_dir: Path | None = None,
    force: bool = False,
    top: int = 10,
    now: float | None = None,
    groups: list[str] | tuple[str, ...] | None = None,
    include_chat: bool = False,
    min_age_seconds: int = 0,
    max_total_bytes: int = DEFAULT_POLICY_LIMIT,
    max_candidate_bytes: int = DEFAULT_POLICY_LIMIT,
    min_candidate_files: int = DEFAULT_POLICY_LIMIT,
) -> dict:
    from aha_cli.services.run_retention import apply_run_retention, run_retention_report

    report = run_retention_report(
        root,
        run_id,
        top=top,
        now=now,
        groups=groups,
        include_chat=include_chat,
        min_age_seconds=min_age_seconds,
        max_total_bytes=max_total_bytes,
        max_candidate_bytes=max_candidate_bytes,
        min_candidate_files=min_candidate_files,
    )
    report["policy_enforced"] = True
    report["apply_if_over_limit"] = bool(apply)
    if not apply:
        return report

    automation = (report.get("policy_report") or {}).get("automation") or {}
    if not automation.get("over_limit"):
        report["apply_skipped"] = True
        report["apply_skipped_reason"] = "threshold_not_exceeded"
        return report
    if not automation.get("eligible_for_apply"):
        report["apply_skipped"] = True
        report["apply_skipped_reason"] = "no_candidates"
        return report

    result = apply_run_retention(
        root,
        run_id,
        current_run_id=current_run_id,
        active_heartbeat_seconds=active_heartbeat_seconds,
        archive_dir=archive_dir,
        force=force,
        top=top,
        now=now,
        groups=groups,
        include_chat=include_chat,
        min_age_seconds=min_age_seconds,
        max_total_bytes=max_total_bytes,
        max_candidate_bytes=max_candidate_bytes,
        min_candidate_files=min_candidate_files,
    )
    result["policy_enforced"] = True
    result["apply_if_over_limit"] = True
    result["apply_skipped"] = False
    automation = (result.get("policy_report") or {}).get("automation")
    if isinstance(automation, dict):
        automation["auto_applied"] = True
    return result


def _retention_policy_run_summary(report: dict, *, guard: dict) -> dict:
    policy_report = report.get("policy_report") or {}
    automation = policy_report.get("automation") or {}
    candidate_total = policy_report.get("candidate_total") or _metric()
    over_limit = bool(automation.get("over_limit"))
    has_candidates = int(candidate_total.get("files") or 0) > 0
    guard_allows = guard.get("action") == "allow"
    if over_limit and has_candidates and guard_allows:
        recommended_action = "apply_retention"
    elif over_limit and has_candidates:
        recommended_action = f"protect_{guard.get('reason') or 'run'}"
    elif over_limit:
        recommended_action = "review_policy"
    else:
        recommended_action = "none"
    return {
        "run_id": report["run_id"],
        "path": report["path"],
        "dry_run": bool(report.get("dry_run", True)),
        "total": report.get("total") or _metric(),
        "candidate_total": candidate_total,
        "protected_total": policy_report.get("protected_total") or _metric(),
        "alerts": list(automation.get("alerts") or []),
        "over_limit": over_limit,
        "eligible_for_apply": bool(over_limit and has_candidates and guard_allows),
        "recommended_action": recommended_action,
        "guard": guard,
        "archive": report.get("archive"),
        "deleted_count": len(report.get("deleted") or []),
        "errors": list(report.get("errors") or []),
    }


def _summarize_policy_runs(runs: list[dict], errors: list[dict]) -> dict:
    total = _metric()
    candidate_total = _metric()
    archive_total = _metric()
    deleted_files = 0
    alerts = 0
    for item in runs:
        _add_metric(total, item.get("total") or {})
        _add_metric(candidate_total, item.get("candidate_total") or {})
        archive = item.get("archive") or {}
        if archive:
            archive_total["files"] += int(archive.get("files") or 0)
            archive_total["bytes"] += int(archive.get("bytes") or 0)
        deleted_files += int(item.get("deleted_count") or 0)
        alerts += len(item.get("alerts") or [])
    protected_runs = [item for item in runs if (item.get("guard") or {}).get("action") == "protect"]
    return {
        "runs": len(runs),
        "over_limit_runs": sum(1 for item in runs if item.get("over_limit")),
        "eligible_runs": sum(1 for item in runs if item.get("eligible_for_apply")),
        "protected_runs": len(protected_runs),
        "alerts": alerts,
        "total": total,
        "candidate_total": candidate_total,
        "archives": archive_total,
        "deleted_files": deleted_files,
        "errors": len(errors),
    }


def _policy_alerts(runs: list[dict]) -> list[dict]:
    alerts: list[dict] = []
    for item in runs:
        for alert in item.get("alerts") or []:
            alerts.append({"run_id": item["run_id"], **alert})
    return alerts


def _policy_groups(groups: list[str] | tuple[str, ...] | None, *, include_chat: bool) -> list[str]:
    selected = list(groups or ("logs", "prompts"))
    if include_chat and "chat" not in selected:
        selected.append("chat")
    return selected


def _non_negative_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, default)


def _bool_value(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


def retention_policy_schedule_config(value: object | None = None) -> dict:
    defaults = default_retention_policy_config()
    raw = value if isinstance(value, dict) else {}
    return {
        "scheduled_report_enabled": _bool_value(
            raw.get("scheduled_report_enabled", raw.get("enabled")),
            bool(defaults["scheduled_report_enabled"]),
        ),
        "report_interval_seconds": max(
            60,
            _non_negative_int(raw.get("report_interval_seconds", raw.get("interval_seconds")), int(defaults["report_interval_seconds"])),
        ),
        "max_total_bytes": _non_negative_int(raw.get("max_total_bytes"), int(defaults["max_total_bytes"])),
        "max_candidate_bytes": _non_negative_int(raw.get("max_candidate_bytes"), int(defaults["max_candidate_bytes"])),
        "min_candidate_files": _non_negative_int(raw.get("min_candidate_files"), int(defaults["min_candidate_files"])),
        "min_age_seconds": _non_negative_int(raw.get("min_age_seconds"), int(defaults["min_age_seconds"])),
        "include_chat": _bool_value(raw.get("include_chat"), bool(defaults["include_chat"])),
    }


def retention_policy_reports_dir(root: Path, report_dir: Path | None = None) -> Path:
    return report_dir.expanduser() if report_dir is not None else aha_home_path(root) / RETENTION_POLICY_REPORTS_DIR


def _report_stamp(now: dt.datetime | None = None) -> str:
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(dt.timezone.utc).replace(microsecond=0).strftime("%Y%m%dT%H%M%SZ")


def _latest_report_path(root: Path, report_dir: Path | None = None) -> Path:
    return retention_policy_reports_dir(root, report_dir) / "latest.json"


def retention_policy_report_due(
    root: Path,
    *,
    interval_seconds: int,
    report_dir: Path | None = None,
    now: float | None = None,
) -> bool:
    latest = _latest_report_path(root, report_dir)
    if not latest.exists():
        return True
    now = time.time() if now is None else now
    try:
        return now - latest.stat().st_mtime >= max(60, int(interval_seconds))
    except OSError:
        return True


def write_retention_policy_report(
    root: Path,
    report: dict,
    *,
    report_dir: Path | None = None,
    created_at: str | None = None,
) -> dict:
    created_at = created_at or utc_now()
    try:
        parsed = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        parsed = dt.datetime.now(dt.timezone.utc)
    directory = retention_policy_reports_dir(root, report_dir)
    report_path = directory / f"{_report_stamp(parsed)}-retention-policy.json"
    latest_path = directory / "latest.json"
    payload = {
        **report,
        "scheduled_report": {
            "created_at": created_at,
            "path": str(report_path),
            "latest_path": str(latest_path),
            "mode": "retention-policy",
            "read_only": bool(report.get("dry_run", True)),
        },
    }
    write_json(report_path, payload)
    write_json(latest_path, payload)
    return payload


def scheduled_retention_policy_report(
    root: Path,
    *,
    current_run_id: str | None = None,
    config: dict | None = None,
    report_dir: Path | None = None,
    now: float | None = None,
) -> dict:
    schedule = retention_policy_schedule_config(config)
    report = enforce_all_run_retention_policy(
        root,
        apply=False,
        current_run_id=current_run_id,
        now=now,
        include_chat=bool(schedule["include_chat"]),
        min_age_seconds=int(schedule["min_age_seconds"]),
        max_total_bytes=int(schedule["max_total_bytes"]),
        max_candidate_bytes=int(schedule["max_candidate_bytes"]),
        min_candidate_files=int(schedule["min_candidate_files"]),
    )
    return write_retention_policy_report(root, report, report_dir=report_dir)


def enforce_all_run_retention_policy(
    root: Path,
    *,
    apply: bool = False,
    current_run_id: str | None = None,
    active_heartbeat_seconds: int = DEFAULT_ACTIVE_HEARTBEAT_SECONDS,
    archive_dir: Path | None = None,
    force: bool = False,
    top: int = 10,
    now: float | None = None,
    groups: list[str] | tuple[str, ...] | None = None,
    include_chat: bool = False,
    min_age_seconds: int = 0,
    max_total_bytes: int = DEFAULT_POLICY_LIMIT,
    max_candidate_bytes: int = DEFAULT_POLICY_LIMIT,
    min_candidate_files: int = DEFAULT_POLICY_LIMIT,
) -> dict:
    from aha_cli.services.run_retention import RunRetentionError, run_retention_report

    now = time.time() if now is None else now
    run_ids = _iter_run_ids(root)
    runs: list[dict] = []
    errors: list[dict] = []
    for run_id in run_ids:
        run_path = run_dir(root, run_id)
        try:
            report = run_retention_report(
                root,
                run_id,
                top=top,
                now=now,
                groups=groups,
                include_chat=include_chat,
                min_age_seconds=min_age_seconds,
                max_total_bytes=max_total_bytes,
                max_candidate_bytes=max_candidate_bytes,
                min_candidate_files=min_candidate_files,
            )
            guard = _apply_guard(
                run_path,
                run_id,
                current_run_id=current_run_id,
                active_heartbeat_seconds=active_heartbeat_seconds,
                now=now,
            )
            summary = _retention_policy_run_summary(report, guard=guard)
            if apply and summary["eligible_for_apply"]:
                applied = enforce_run_retention_policy(
                    root,
                    run_id,
                    apply=True,
                    current_run_id=current_run_id,
                    active_heartbeat_seconds=active_heartbeat_seconds,
                    archive_dir=archive_dir,
                    force=force,
                    top=top,
                    now=now,
                    groups=groups,
                    include_chat=include_chat,
                    min_age_seconds=min_age_seconds,
                    max_total_bytes=max_total_bytes,
                    max_candidate_bytes=max_candidate_bytes,
                    min_candidate_files=min_candidate_files,
                )
                summary = _retention_policy_run_summary(applied, guard=guard)
            runs.append(summary)
        except (FileNotFoundError, RunRetentionError, ValueError) as exc:
            reason = getattr(exc, "reason", type(exc).__name__)
            errors.append({"run_id": run_id, "path": str(run_path), "error": str(exc), "reason": reason})

    result = {
        "aha_home": str(aha_home_path(root)),
        "dry_run": not apply,
        "policy_enforced": True,
        "apply_if_over_limit": bool(apply),
        "force": bool(force),
        "current_run_id": current_run_id,
        "active_heartbeat_seconds": int(active_heartbeat_seconds),
        "policy": {
            "groups": _policy_groups(groups, include_chat=include_chat),
            "include_chat": bool(include_chat),
            "min_age_seconds": max(0, int(min_age_seconds)),
            "thresholds": policy_thresholds(
                max_total_bytes=max_total_bytes,
                max_candidate_bytes=max_candidate_bytes,
                min_candidate_files=min_candidate_files,
            ),
        },
        "summary": _summarize_policy_runs(runs, errors),
        "alerts": _policy_alerts(runs),
        "runs": runs,
        "errors": errors,
    }
    automation = {
        "over_limit": bool(result["alerts"]),
        "recommended_action": "apply_retention" if any(item.get("eligible_for_apply") for item in runs) else "review_policy" if result["alerts"] else "none",
        "auto_applied": bool(apply and any(item.get("archive") for item in runs)),
    }
    result["automation"] = automation
    return result


def format_all_run_retention_policy_report(report: dict) -> str:
    mode = "apply-if-over-limit" if report.get("apply_if_over_limit") else "dry-run"
    summary = report.get("summary") or {}
    candidate_total = summary.get("candidate_total") or _metric()
    archive_total = summary.get("archives") or _metric()
    policy = report.get("policy") or {}
    thresholds = policy.get("thresholds") or {}
    lines = [
        f"AHA all-run retention policy ({mode})",
        f"home: {report.get('aha_home')}",
        (
            f"runs: {summary.get('runs', 0)}, over_limit={summary.get('over_limit_runs', 0)}, "
            f"eligible={summary.get('eligible_runs', 0)}, protected={summary.get('protected_runs', 0)}"
        ),
        f"candidates: {candidate_total.get('files', 0)} files, {candidate_total.get('bytes', 0)} bytes",
        f"policy: groups={','.join(policy.get('groups') or []) or '-'}, min_age_seconds={policy.get('min_age_seconds', 0)}",
        (
            "thresholds: "
            f"max_total_bytes={thresholds.get('max_total_bytes', 0)}, "
            f"max_candidate_bytes={thresholds.get('max_candidate_bytes', 0)}, "
            f"min_candidate_files={thresholds.get('min_candidate_files', 0)}"
        ),
    ]
    if archive_total.get("files") or archive_total.get("bytes"):
        lines.append(f"archives: {archive_total.get('files', 0)} files, {archive_total.get('bytes', 0)} bytes")
    lines.append("runs:")
    for item in report.get("runs") or []:
        candidate = item.get("candidate_total") or _metric()
        guard = item.get("guard") or {}
        alerts = ",".join(alert.get("kind", "alert") for alert in item.get("alerts") or []) or "-"
        suffix = ""
        if item.get("archive"):
            suffix = f", archive={item['archive']['path']}"
        lines.append(
            f"- {item['run_id']}: action={item.get('recommended_action')}, "
            f"guard={guard.get('reason')}, candidates={candidate.get('files', 0)}/{candidate.get('bytes', 0)} bytes, "
            f"alerts={alerts}{suffix}"
        )
    if not report.get("runs"):
        lines.append("- none")
    if report.get("errors"):
        lines.append("Errors:")
        for item in report["errors"]:
            lines.append(f"- {item.get('run_id') or '-'}: {item.get('error')}")
    scheduled = report.get("scheduled_report") or {}
    if scheduled.get("path"):
        lines.append(f"report: {scheduled['path']}")
    return "\n".join(lines) + "\n"


__all__ = [
    "DEFAULT_POLICY_LIMIT",
    "enforce_all_run_retention_policy",
    "enforce_run_retention_policy",
    "format_all_run_retention_policy_report",
    "policy_automation_report",
    "policy_thresholds",
    "retention_policy_report_due",
    "retention_policy_reports_dir",
    "retention_policy_schedule_config",
    "scheduled_retention_policy_report",
    "write_retention_policy_report",
]
