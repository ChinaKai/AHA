from __future__ import annotations

import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CCUSAGE_NPX_PACKAGE = "ccusage@20.0.14"
CCUSAGE_TIMEOUT_SECONDS = 45

CCUSAGE_SOURCE_COMMANDS = {
    "claude": "claude",
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "amp": "amp",
    "droid": "droid",
    "codebuff": "codebuff",
    "hermes": "hermes",
    "pi": "pi",
    "goose": "goose",
    "kilo": "kilo",
    "copilot": "copilot",
    "gemini": "gemini",
    "kimi": "kimi",
    "qwen": "qwen",
    "openclaw": "openclaw",
}

TOKEN_USAGE_ZERO = {
    "event_count": 0,
    "raw_input_tokens": 0,
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


def _parse_date(value: str | None, field: str) -> dt.date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def _timezone_name(name: str | None) -> str:
    tz_name = str(name or "UTC").strip() or "UTC"
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {tz_name}") from exc
    return tz_name


def _empty_counts() -> dict:
    return dict(TOKEN_USAGE_ZERO)


def _add_counts(target: dict, usage: dict) -> None:
    for key in TOKEN_USAGE_ZERO:
        if key == "cost_usd":
            target[key] = round(float(target.get(key, 0.0)) + float(usage.get(key, 0.0)), 12)
        else:
            target[key] = int(target.get(key, 0)) + int(usage.get(key, 0))


def _first_value(data: dict, names: tuple[str, ...]) -> object:
    for name in names:
        if name in data and data.get(name) not in (None, ""):
            return data.get(name)
    return None


def _ccusage_counts(data: dict) -> dict:
    input_tokens = _non_negative_int(_first_value(data, ("inputTokens", "totalInputTokens", "input_tokens")))
    cache_read_tokens = _non_negative_int(
        _first_value(
            data,
            (
                "cacheReadTokens",
                "cacheReadInputTokens",
                "cachedInputTokens",
                "totalCacheReadTokens",
                "cache_read_tokens",
            ),
        )
    )
    cache_creation_tokens = _non_negative_int(
        _first_value(
            data,
            (
                "cacheCreationTokens",
                "cacheCreationInputTokens",
                "totalCacheCreationTokens",
                "cache_creation_tokens",
            ),
        )
    )
    output_tokens = _non_negative_int(_first_value(data, ("outputTokens", "totalOutputTokens", "output_tokens")))
    reasoning_output_tokens = _non_negative_int(
        _first_value(data, ("reasoningOutputTokens", "totalReasoningOutputTokens", "reasoning_output_tokens"))
    )
    total_tokens = _non_negative_int(_first_value(data, ("totalTokens", "total_tokens")))
    if not total_tokens:
        total_tokens = input_tokens + cache_read_tokens + cache_creation_tokens + output_tokens + reasoning_output_tokens
    cost_usd = _non_negative_float(_first_value(data, ("totalCost", "costUSD", "totalCostUSD", "cost_usd", "cost")))
    return {
        "event_count": _non_negative_int(_first_value(data, ("entryCount", "entries", "eventCount", "count"))),
        "raw_input_tokens": input_tokens,
        "input_tokens": input_tokens,
        "billable_input_tokens": input_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
    }


def _ccusage_base_command() -> list[str]:
    configured = str(os.environ.get("AHA_CCUSAGE_COMMAND") or "").strip()
    if configured:
        return shlex.split(configured)
    ccusage = shutil.which("ccusage")
    if ccusage:
        return [ccusage]
    npx = shutil.which("npx")
    if npx:
        package = str(os.environ.get("AHA_CCUSAGE_NPX_PACKAGE") or CCUSAGE_NPX_PACKAGE).strip() or CCUSAGE_NPX_PACKAGE
        return [npx, "--yes", package]
    raise RuntimeError("ccusage command not found; install ccusage or set AHA_CCUSAGE_COMMAND")


def _ccusage_daily_args(
    *,
    timezone: str,
    since: dt.date | None,
    until: dt.date | None,
    backend: str,
    offline: bool,
) -> list[str]:
    source = ""
    if backend:
        source = CCUSAGE_SOURCE_COMMANDS.get(backend)
        if not source:
            supported = ", ".join(sorted(CCUSAGE_SOURCE_COMMANDS))
            raise ValueError(f"unsupported ccusage backend/source: {backend}; supported: {supported}")
    args = ([source, "daily"] if source else ["daily"]) + ["--json", "--timezone", timezone]
    if since:
        args += ["--since", since.isoformat()]
    if until:
        args += ["--until", until.isoformat()]
    if offline:
        args.append("--offline")
    return args


def _run_ccusage_json(args: list[str], *, root: Path | None = None) -> dict:
    command = _ccusage_base_command() + args
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    try:
        result = subprocess.run(
            command,
            cwd=str(root) if root else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=CCUSAGE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ccusage timed out after {CCUSAGE_TIMEOUT_SECONDS}s") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"ccusage failed with exit code {result.returncode}: {message[:500]}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("ccusage returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ccusage returned unexpected JSON")
    return payload


def _model_breakdowns(data: dict, source: str) -> list[dict]:
    breakdowns = data.get("modelBreakdowns") or data.get("model_breakdowns") or data.get("models") or []
    items: list[dict] = []
    if isinstance(breakdowns, dict):
        iterable = []
        for model, value in breakdowns.items():
            if isinstance(value, dict):
                iterable.append({"model": model, **value})
    elif isinstance(breakdowns, list):
        iterable = [item for item in breakdowns if isinstance(item, dict)]
    else:
        iterable = []
    for item in iterable:
        counts = _ccusage_counts(item)
        model = str(item.get("model") or item.get("modelName") or item.get("name") or "").strip()
        counts.update({"backend": str(item.get("source") or source or "ccusage").strip() or "ccusage", "model": model})
        items.append(counts)
    return sorted(items, key=lambda item: (-int(item.get("total_tokens", 0)), str(item.get("model") or "")))


def _ccusage_days(payload: dict, source: str) -> list[dict]:
    raw_days = payload.get("daily") or payload.get("data") or []
    if not isinstance(raw_days, list):
        return []
    days: list[dict] = []
    for item in raw_days:
        if not isinstance(item, dict):
            continue
        date = str(item.get("date") or item.get("day") or "").strip()
        if not date:
            continue
        counts = _ccusage_counts(item)
        days.append(
            {
                "date": date[:10],
                **counts,
                "by_backend": _model_breakdowns(item, source),
                "by_task": [],
            }
        )
    return sorted(days, key=lambda item: item["date"])


def _ccusage_totals(payload: dict, days: list[dict]) -> dict:
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    if totals:
        return _ccusage_counts(totals)
    result = _empty_counts()
    for day in days:
        _add_counts(result, day)
    return result


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
    offline: bool = False,
) -> dict:
    tz_name = _timezone_name(timezone)
    since_date = _parse_date(since, "since")
    until_date = _parse_date(until, "until")
    if since_date and until_date and since_date > until_date:
        raise ValueError("since must be before or equal to until")

    task_filter = str(task_id or "").strip()
    target_filter = str(target or "").strip()
    if task_filter or target_filter:
        raise ValueError("task_id and target filters are not supported by ccusage-backed daily usage")
    backend_filter = str(backend or "").strip().lower()

    args = _ccusage_daily_args(
        timezone=tz_name,
        since=since_date,
        until=until_date,
        backend=backend_filter,
        offline=offline,
    )
    payload = _run_ccusage_json(args, root=root)
    source = CCUSAGE_SOURCE_COMMANDS.get(backend_filter, "ccusage")
    days = _ccusage_days(payload, source)
    days_before_limit = list(days)
    if limit_days is not None:
        days = days[-max(1, int(limit_days)) :]
        totals = _empty_counts()
        for day in days:
            _add_counts(totals, day)
    else:
        totals = _ccusage_totals(payload, days)

    return {
        "run_id": run_id,
        "timezone": tz_name,
        "source": "ccusage",
        "ccusage_args": args,
        "scanned_events": 0,
        "matched_events": len(days),
        "matched_events_before_limit": len(days_before_limit),
        "filters": {
            "since": since_date.isoformat() if since_date else "",
            "until": until_date.isoformat() if until_date else "",
            "task_id": "",
            "target": "",
            "backend": backend_filter,
        },
        "totals": totals,
        "days": days,
    }
