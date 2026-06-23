from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aha_cli.store.io import iter_jsonl_records_from
from aha_cli.store.paths import event_path

TOKEN_USAGE_ZERO = {
    "event_count": 0,
    "input_tokens": 0,
    "billable_input_tokens": 0,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 0,
    "cost_usd": 0.0,
}


def _non_negative_int(value: object) -> int:
    if value is None or value == "":
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            return max(0, int(float(str(value))))
        except (TypeError, ValueError):
            return 0


def _non_negative_float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _parse_datetime(value: object) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _parse_date(value: str | None, field: str) -> dt.date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def _timezone(name: str | None) -> ZoneInfo:
    tz_name = str(name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {tz_name}") from exc


def _empty_counts() -> dict:
    return dict(TOKEN_USAGE_ZERO)


def _usage_cache_creation_tokens(usage: dict) -> int:
    cache_creation = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else None
    if cache_creation:
        five_min = _non_negative_int(cache_creation.get("ephemeral_5m_input_tokens"))
        one_hour = _non_negative_int(cache_creation.get("ephemeral_1h_input_tokens"))
        if five_min or one_hour:
            return five_min + one_hour
    return _non_negative_int(usage.get("cache_creation_input_tokens"))


def normalize_agent_usage_event(event: dict) -> dict | None:
    if event.get("type") != "agent_usage":
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    if not usage:
        return None

    backend = str(data.get("backend") or data.get("source") or usage.get("backend") or "").strip().lower()
    model = str(data.get("model") or usage.get("model") or usage.get("model_name") or "").strip()
    raw_input_tokens = _non_negative_int(usage.get("input_tokens"))
    output_tokens = _non_negative_int(usage.get("output_tokens"))
    reasoning_output_tokens = _non_negative_int(usage.get("reasoning_output_tokens"))
    has_codex_cache = "cached_input_tokens" in usage
    is_codex = backend == "codex" or has_codex_cache
    if has_codex_cache:
        cache_read_tokens = min(_non_negative_int(usage.get("cached_input_tokens")), raw_input_tokens)
    else:
        cache_read_tokens = _non_negative_int(usage.get("cache_read_input_tokens"))
    cache_creation_tokens = 0 if is_codex else _usage_cache_creation_tokens(usage)
    billable_input_tokens = max(0, raw_input_tokens - cache_read_tokens) if is_codex else raw_input_tokens
    if is_codex:
        total_tokens = _non_negative_int(usage.get("total_tokens")) or raw_input_tokens + output_tokens + reasoning_output_tokens
    else:
        total_tokens = raw_input_tokens + cache_read_tokens + cache_creation_tokens + output_tokens

    if not any((raw_input_tokens, output_tokens, reasoning_output_tokens, cache_read_tokens, cache_creation_tokens, total_tokens)):
        return None

    return {
        "ts": event.get("ts"),
        "event_id": event.get("event_id"),
        "run_id": event.get("run_id"),
        "task_id": str(data.get("task_id") or "").strip(),
        "target": str(data.get("target") or "").strip(),
        "backend": backend or "unknown",
        "model": model,
        "usage": {
            "event_count": 1,
            "input_tokens": raw_input_tokens,
            "billable_input_tokens": billable_input_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_output_tokens,
            "total_tokens": total_tokens,
            "cost_usd": _non_negative_float(usage.get("total_cost_usd") or usage.get("cost_usd")),
        },
    }


def _add_counts(target: dict, usage: dict) -> None:
    for key in TOKEN_USAGE_ZERO:
        if key == "cost_usd":
            target[key] = round(float(target.get(key, 0.0)) + float(usage.get(key, 0.0)), 12)
        else:
            target[key] = int(target.get(key, 0)) + int(usage.get(key, 0))


def _breakdown_key(entry: dict, fields: tuple[str, ...]) -> str:
    return "\0".join(str(entry.get(field) or "") for field in fields)


def _add_breakdown(group: dict, name: str, fields: tuple[str, ...], entry: dict) -> None:
    key = _breakdown_key(entry, fields)
    items = group.setdefault(name, {})
    item = items.get(key)
    if item is None:
        item = {field: entry.get(field) or "" for field in fields}
        item.update(_empty_counts())
        items[key] = item
    _add_counts(item, entry["usage"])


def _finalize_breakdowns(items: dict[str, dict]) -> list[dict]:
    return sorted(
        items.values(),
        key=lambda item: (-int(item.get("total_tokens", 0)), str(item.get("backend") or ""), str(item.get("task_id") or "")),
    )


def daily_token_usage_report(
    root: Path,
    run_id: str,
    *,
    timezone: str | None = "UTC",
    since: str | None = None,
    until: str | None = None,
    task_id: str | None = None,
    target: str | None = None,
    backend: str | None = None,
    limit_days: int | None = None,
) -> dict:
    tz = _timezone(timezone)
    since_date = _parse_date(since, "since")
    until_date = _parse_date(until, "until")
    if since_date and until_date and since_date > until_date:
        raise ValueError("since must be before or equal to until")

    task_filter = str(task_id or "").strip()
    target_filter = str(target or "").strip()
    backend_filter = str(backend or "").strip().lower()
    day_groups: dict[str, dict] = {}
    totals = _empty_counts()
    path = event_path(root, run_id)
    records, last_event_id = iter_jsonl_records_from(path)
    scanned_events = 0
    matched_events = 0

    for event, line_end in records:
        scanned_events += 1
        normalized = normalize_agent_usage_event(event | {"event_id": event.get("event_id", line_end)})
        if not normalized:
            continue
        if task_filter and normalized["task_id"] != task_filter:
            continue
        if target_filter and normalized["target"] != target_filter:
            continue
        if backend_filter and normalized["backend"] != backend_filter:
            continue
        timestamp = _parse_datetime(normalized.get("ts"))
        if timestamp is None:
            continue
        day = timestamp.astimezone(tz).date()
        if since_date and day < since_date:
            continue
        if until_date and day > until_date:
            continue
        day_key = day.isoformat()
        group = day_groups.get(day_key)
        if group is None:
            group = {
                "date": day_key,
                **_empty_counts(),
                "_by_backend": {},
                "_by_task": {},
            }
            day_groups[day_key] = group
        matched_events += 1
        _add_counts(group, normalized["usage"])
        _add_counts(totals, normalized["usage"])
        _add_breakdown(group, "_by_backend", ("backend", "model"), normalized)
        _add_breakdown(group, "_by_task", ("task_id", "target", "backend"), normalized)

    days = []
    for group in sorted(day_groups.values(), key=lambda item: item["date"]):
        group["by_backend"] = _finalize_breakdowns(group.pop("_by_backend"))
        group["by_task"] = _finalize_breakdowns(group.pop("_by_task"))
        days.append(group)
    matched_events_before_limit = matched_events
    if limit_days is not None:
        days = days[-max(1, int(limit_days)) :]
        totals = _empty_counts()
        matched_events = 0
        for day in days:
            _add_counts(totals, day)
            matched_events += int(day.get("event_count", 0))

    return {
        "run_id": run_id,
        "timezone": str(timezone or "UTC").strip() or "UTC",
        "source": "agent_usage_events",
        "last_event_id": last_event_id,
        "scanned_events": scanned_events,
        "matched_events": matched_events,
        "matched_events_before_limit": matched_events_before_limit,
        "filters": {
            "since": since_date.isoformat() if since_date else "",
            "until": until_date.isoformat() if until_date else "",
            "task_id": task_filter,
            "target": target_filter,
            "backend": backend_filter,
        },
        "totals": totals,
        "days": days,
    }
