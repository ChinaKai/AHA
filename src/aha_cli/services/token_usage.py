from __future__ import annotations

import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aha_cli.domain.models import utc_now
from aha_cli.services.backend_paths import add_user_backend_paths
from aha_cli.store.io import read_json, write_json
from aha_cli.store.paths import run_dir

CCUSAGE_NPX_PACKAGE = "ccusage@20.0.14"
CCUSAGE_TIMEOUT_SECONDS = 10 * 60
USAGE_CACHE_SCHEMA_VERSION = 2

_usage_refresh_lock = threading.Lock()
_running_usage_refreshes: dict[str, dict[str, object]] = {}

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


class CcusageUnavailable(RuntimeError):
    """Raised when the optional ccusage integration cannot be launched."""


class CcusageCancelled(RuntimeError):
    """Raised when a background ccusage refresh is stopped by the user."""


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


def _ccusage_env() -> dict[str, str]:
    env = dict(os.environ)
    add_user_backend_paths(env, home=Path.home())
    env.setdefault("NO_COLOR", "1")
    return env


def _ccusage_unavailable_message() -> str:
    return (
        "ccusage command not found; install ccusage, make npx available to the AHA service, "
        "or set AHA_CCUSAGE_COMMAND"
    )


def _ccusage_base_command(env: dict[str, str] | None = None) -> list[str]:
    env = env or _ccusage_env()
    configured = str(os.environ.get("AHA_CCUSAGE_COMMAND") or "").strip()
    if configured:
        try:
            command = shlex.split(configured)
        except ValueError as exc:
            raise CcusageUnavailable(f"AHA_CCUSAGE_COMMAND is invalid: {exc}") from exc
        if not command:
            raise CcusageUnavailable(_ccusage_unavailable_message())
        return command
    path = env.get("PATH") or None
    ccusage = shutil.which("ccusage", path=path)
    if ccusage:
        return [ccusage]
    npx = shutil.which("npx", path=path)
    if npx:
        package = str(os.environ.get("AHA_CCUSAGE_NPX_PACKAGE") or CCUSAGE_NPX_PACKAGE).strip() or CCUSAGE_NPX_PACKAGE
        return [npx, "--yes", package]
    raise CcusageUnavailable(_ccusage_unavailable_message())


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


def _parse_ccusage_json(stdout: str) -> dict:
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("ccusage returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ccusage returned unexpected JSON")
    return payload


def _terminate_ccusage_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def _run_ccusage_json_with_job(
    command: list[str],
    *,
    env: dict[str, str],
    root: Path | None,
    refresh_job: dict[str, object],
) -> dict:
    try:
        process = subprocess.Popen(  # noqa: S603 - command is resolved without a shell.
            command,
            cwd=str(root) if root else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        executable = command[0] if command else "ccusage"
        raise CcusageUnavailable(
            f"ccusage command executable not found: {executable}; install ccusage, "
            "make npx available to the AHA service, or set AHA_CCUSAGE_COMMAND"
        ) from exc
    with _usage_refresh_lock:
        refresh_job["process"] = process
    cancel_event = refresh_job.get("cancel_event")
    deadline = time.monotonic() + CCUSAGE_TIMEOUT_SECONDS
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if hasattr(cancel_event, "is_set") and cancel_event.is_set():
                _terminate_ccusage_process(process)
                raise CcusageCancelled("ccusage refresh stopped")
            if time.monotonic() >= deadline:
                _terminate_ccusage_process(process)
                raise RuntimeError(f"ccusage timed out after {CCUSAGE_TIMEOUT_SECONDS}s")
    if hasattr(cancel_event, "is_set") and cancel_event.is_set():
        raise CcusageCancelled("ccusage refresh stopped")
    if process.returncode != 0:
        message = (stderr or stdout or "").strip()
        raise RuntimeError(f"ccusage failed with exit code {process.returncode}: {message[:500]}")
    return _parse_ccusage_json(stdout)


def _run_ccusage_json(args: list[str], *, root: Path | None = None, refresh_job: dict[str, object] | None = None) -> dict:
    env = _ccusage_env()
    command = _ccusage_base_command(env) + args
    if refresh_job is not None:
        return _run_ccusage_json_with_job(command, env=env, root=root, refresh_job=refresh_job)
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
    except FileNotFoundError as exc:
        executable = command[0] if command else "ccusage"
        raise CcusageUnavailable(
            f"ccusage command executable not found: {executable}; install ccusage, "
            "make npx available to the AHA service, or set AHA_CCUSAGE_COMMAND"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ccusage timed out after {CCUSAGE_TIMEOUT_SECONDS}s") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"ccusage failed with exit code {result.returncode}: {message[:500]}")
    return _parse_ccusage_json(result.stdout)


def _ccusage_date(data: dict) -> str:
    return str(data.get("date") or data.get("day") or data.get("period") or "").strip()[:10]


def _ccusage_models(data: dict) -> list[str]:
    raw_models = data.get("modelsUsed") or data.get("models_used") or data.get("models") or []
    models: list[str] = []
    if isinstance(raw_models, list):
        models = [str(value).strip() for value in raw_models if str(value or "").strip()]
    elif isinstance(raw_models, dict):
        models = [str(key).strip() for key in raw_models if str(key or "").strip()]
    else:
        text = str(raw_models or "").strip()
        if text:
            models = [value.strip() for value in text.split(",") if value.strip()]
    if models:
        return sorted(dict.fromkeys(models))

    breakdowns = data.get("modelBreakdowns") or data.get("model_breakdowns") or []
    if isinstance(breakdowns, list):
        for item in breakdowns:
            if not isinstance(item, dict):
                continue
            model = str(item.get("model") or item.get("modelName") or item.get("name") or "").strip()
            if model:
                models.append(model)
    return sorted(dict.fromkeys(models))


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
        date = _ccusage_date(item)
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


def _ccusage_backend_days(payload: dict, backend: str) -> list[dict]:
    raw_days = payload.get("daily") or payload.get("data") or []
    if not isinstance(raw_days, list):
        return []
    rows: list[dict] = []
    for item in raw_days:
        if not isinstance(item, dict):
            continue
        date = _ccusage_date(item)
        if not date:
            continue
        counts = _ccusage_counts(item)
        rows.append(
            {
                "date": date,
                **counts,
                "backend": backend,
                "models": _ccusage_models(item),
            }
        )
    return rows


def _detected_ccusage_agents(payload: dict) -> list[str]:
    raw_days = payload.get("daily") or payload.get("data") or []
    if not isinstance(raw_days, list):
        return []
    agents: list[str] = []
    supported_sources = set(CCUSAGE_SOURCE_COMMANDS.values())
    for item in raw_days:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        raw_agents = metadata.get("agents") or []
        if not isinstance(raw_agents, list):
            raw_agents = [raw_agents]
        explicit_agent = str(item.get("agent") or "").strip().lower()
        if explicit_agent and explicit_agent != "all":
            raw_agents = [explicit_agent, *raw_agents]
        for value in raw_agents:
            agent = str(value or "").strip().lower()
            if agent in supported_sources and agent not in agents:
                agents.append(agent)
    return agents


def _refresh_cancel_requested(refresh_job: dict[str, object] | None) -> bool:
    cancel_event = refresh_job.get("cancel_event") if refresh_job else None
    return bool(hasattr(cancel_event, "is_set") and cancel_event.is_set())


def _attach_ccusage_backend_breakdowns(
    days: list[dict],
    payload: dict,
    request: dict,
    *,
    root: Path,
    refresh_job: dict[str, object] | None,
) -> list[dict]:
    if not days:
        return days
    backend = str(request.get("backend") or "").strip().lower()
    if backend:
        source = CCUSAGE_SOURCE_COMMANDS.get(backend, backend)
        by_date: dict[str, list[dict]] = {}
        for row in _ccusage_backend_days(payload, source):
            by_date.setdefault(row["date"], []).append({key: value for key, value in row.items() if key != "date"})
    else:
        by_date = {}
        for agent in _detected_ccusage_agents(payload):
            if _refresh_cancel_requested(refresh_job):
                raise CcusageCancelled("ccusage refresh stopped")
            agent_args = _ccusage_daily_args(
                timezone=request["timezone"],
                since=request["since"],
                until=request["until"],
                backend=agent,
                offline=request["offline"],
            )
            agent_payload = _run_ccusage_json(agent_args, root=root, refresh_job=refresh_job)
            for row in _ccusage_backend_days(agent_payload, agent):
                by_date.setdefault(row["date"], []).append({key: value for key, value in row.items() if key != "date"})

    for day in days:
        rows = by_date.get(day["date"])
        if rows:
            day["by_backend"] = sorted(
                rows,
                key=lambda item: (str(item.get("backend") or ""), -int(item.get("total_tokens", 0))),
            )
    return days


def _ccusage_totals(payload: dict, days: list[dict]) -> dict:
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    if totals:
        return _ccusage_counts(totals)
    result = _empty_counts()
    for day in days:
        _add_counts(result, day)
    return result


def _daily_report_filters(since: dt.date | None, until: dt.date | None, backend: str) -> dict:
    return {
        "since": since.isoformat() if since else "",
        "until": until.isoformat() if until else "",
        "task_id": "",
        "target": "",
        "backend": backend,
    }


def _daily_usage_request(
    *,
    timezone: str | None = "UTC",
    since: str | None = None,
    until: str | None = None,
    task_id: str | None = None,
    target: str | None = None,
    backend: str | None = None,
    limit_days: int | None = None,
    offline: bool = False,
    refresh_job: dict[str, object] | None = None,
) -> dict:
    tz_name = _timezone_name(timezone)
    since_date = _parse_date(since, "since")
    until_date = _parse_date(until, "until")
    if since_date and until_date and since_date > until_date:
        raise ValueError("since must be before or equal to until")
    today = dt.datetime.now(ZoneInfo(tz_name)).date()
    if since_date and since_date > today:
        raise ValueError("since must be today or earlier")

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
    return {
        "timezone": tz_name,
        "since": since_date,
        "until": until_date,
        "backend": backend_filter,
        "limit_days": limit_days,
        "offline": bool(offline),
        "args": args,
        "filters": _daily_report_filters(since_date, until_date, backend_filter),
    }


def _daily_usage_request_payload(request: dict) -> dict:
    return {
        "timezone": request["timezone"],
        "since": request["filters"]["since"],
        "until": request["filters"]["until"],
        "backend": request["backend"],
        "limit_days": request["limit_days"],
        "offline": request["offline"],
        "ccusage_args": list(request["args"]),
    }


def _daily_usage_state_path(root: Path, run_id: str, request: dict) -> Path:
    return run_dir(root, run_id) / "runtime" / "usage" / "daily-latest.json"


def _daily_usage_job_key(run_id: str) -> str:
    return f"{run_id}:daily-latest"


def _read_daily_usage_state(root: Path, run_id: str, request: dict) -> dict:
    path = _daily_usage_state_path(root, run_id, request)
    try:
        state = read_json(path)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_daily_usage_state(root: Path, run_id: str, request: dict, state: dict) -> None:
    write_json(_daily_usage_state_path(root, run_id, request), state)


def _empty_cached_daily_report(run_id: str, request: dict) -> dict:
    return {
        "run_id": run_id,
        "timezone": request["timezone"],
        "source": "ccusage",
        "available": None,
        "unavailable_reason": "",
        "ccusage_args": list(request["args"]),
        "scanned_events": 0,
        "matched_events": 0,
        "matched_events_before_limit": 0,
        "filters": dict(request["filters"]),
        "totals": _empty_counts(),
        "days": [],
    }


def _daily_usage_refresh_view(state: dict) -> dict:
    return {
        "status": str(state.get("status") or "idle"),
        "started_at": str(state.get("started_at") or ""),
        "finished_at": str(state.get("finished_at") or ""),
        "error": str(state.get("error") or ""),
    }


def _daily_usage_cache_view(state: dict) -> dict:
    report = _daily_usage_state_report(state)
    return {
        "status": "ready" if report else "missing",
        "cached_at": str(state.get("cached_at") or state.get("finished_at") or ""),
    }


def _daily_usage_state_report(state: dict) -> dict | None:
    if int(state.get("schema_version") or 0) != USAGE_CACHE_SCHEMA_VERSION:
        return None
    report = state.get("report")
    return report if isinstance(report, dict) else None


def _daily_usage_cached_payload(run_id: str, request: dict, state: dict) -> dict:
    report = _daily_usage_state_report(state)
    payload = dict(report) if report else _empty_cached_daily_report(run_id, _daily_usage_state_request(state, request))
    payload["run_id"] = run_id
    payload["refresh"] = _daily_usage_refresh_view(state)
    payload["cache"] = _daily_usage_cache_view(state)
    return payload


def _daily_usage_state_request(state: dict, fallback: dict) -> dict:
    stored = state.get("request") if isinstance(state.get("request"), dict) else None
    if not stored:
        return fallback
    since = str(stored.get("since") or "")
    until = str(stored.get("until") or "")
    backend = str(stored.get("backend") or "")
    return {
        **fallback,
        "timezone": str(stored.get("timezone") or fallback["timezone"]),
        "backend": backend,
        "limit_days": stored.get("limit_days"),
        "offline": bool(stored.get("offline", fallback["offline"])),
        "args": list(stored.get("ccusage_args") or fallback["args"]),
        "filters": {
            "since": since,
            "until": until,
            "task_id": "",
            "target": "",
            "backend": backend,
        },
    }


def _unavailable_daily_report(
    run_id: str,
    *,
    timezone: str,
    since: dt.date | None,
    until: dt.date | None,
    backend: str,
    args: list[str],
    reason: str,
) -> dict:
    return {
        "run_id": run_id,
        "timezone": timezone,
        "source": "ccusage",
        "available": False,
        "unavailable_reason": reason,
        "ccusage_args": args,
        "scanned_events": 0,
        "matched_events": 0,
        "matched_events_before_limit": 0,
        "filters": _daily_report_filters(since, until, backend),
        "totals": _empty_counts(),
        "days": [],
    }


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
    refresh_job: dict[str, object] | None = None,
) -> dict:
    request = _daily_usage_request(
        timezone=timezone,
        since=since,
        until=until,
        task_id=task_id,
        target=target,
        backend=backend,
        limit_days=limit_days,
        offline=offline,
    )
    args = request["args"]
    try:
        payload = _run_ccusage_json(args, root=root, refresh_job=refresh_job)
    except CcusageUnavailable as exc:
        return _unavailable_daily_report(
            run_id,
            timezone=request["timezone"],
            since=request["since"],
            until=request["until"],
            backend=request["backend"],
            args=args,
            reason=str(exc),
        )
    source = CCUSAGE_SOURCE_COMMANDS.get(request["backend"], "ccusage")
    days = _ccusage_days(payload, source)
    days = _attach_ccusage_backend_breakdowns(days, payload, request, root=root, refresh_job=refresh_job)
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
        "timezone": request["timezone"],
        "source": "ccusage",
        "available": True,
        "unavailable_reason": "",
        "ccusage_args": args,
        "scanned_events": 0,
        "matched_events": len(days),
        "matched_events_before_limit": len(days_before_limit),
        "filters": dict(request["filters"]),
        "totals": totals,
        "days": days,
    }


def daily_token_usage_cached(
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
    request = _daily_usage_request(
        timezone=timezone,
        since=since,
        until=until,
        task_id=task_id,
        target=target,
        backend=backend,
        limit_days=limit_days,
        offline=offline,
    )
    state = _read_daily_usage_state(root, run_id, request)
    return _daily_usage_cached_payload(run_id, request, state)


def run_daily_token_usage_refresh_job(root: Path, run_id: str, request: dict) -> None:
    job_key = _daily_usage_job_key(run_id)
    with _usage_refresh_lock:
        refresh_job = _running_usage_refreshes.get(job_key)
    try:
        report = daily_token_usage_report(
            root,
            run_id,
            timezone=request["timezone"],
            since=request["filters"]["since"],
            until=request["filters"]["until"],
            backend=request["backend"],
            limit_days=request["limit_days"],
            offline=request["offline"],
            refresh_job=refresh_job,
        )
    except CcusageCancelled as exc:
        now = utc_now()
        state = _read_daily_usage_state(root, run_id, request)
        state.update(
            {
                "schema_version": USAGE_CACHE_SCHEMA_VERSION,
                "status": "cancelled",
                "finished_at": now,
                "error": str(exc),
                "request": _daily_usage_request_payload(request),
            }
        )
        _write_daily_usage_state(root, run_id, request, state)
    except Exception as exc:  # noqa: BLE001 - background job errors must be pollable.
        now = utc_now()
        state = _read_daily_usage_state(root, run_id, request)
        state.update(
            {
                "schema_version": USAGE_CACHE_SCHEMA_VERSION,
                "status": "failed",
                "finished_at": now,
                "error": str(exc),
                "request": _daily_usage_request_payload(request),
            }
        )
        _write_daily_usage_state(root, run_id, request, state)
    else:
        now = utc_now()
        _write_daily_usage_state(
            root,
            run_id,
            request,
            {
                "schema_version": USAGE_CACHE_SCHEMA_VERSION,
                "status": "succeeded",
                "started_at": _read_daily_usage_state(root, run_id, request).get("started_at") or now,
                "finished_at": now,
                "cached_at": now,
                "error": "",
                "request": _daily_usage_request_payload(request),
                "report": report,
            },
        )
    finally:
        with _usage_refresh_lock:
            _running_usage_refreshes.pop(job_key, None)


def _default_dispatch_daily_usage_refresh_job(root: Path, run_id: str, request: dict) -> None:
    threading.Thread(
        target=run_daily_token_usage_refresh_job,
        args=(root, run_id, request),
        daemon=True,
    ).start()


dispatch_daily_usage_refresh_job = _default_dispatch_daily_usage_refresh_job


def start_daily_token_usage_refresh(
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
    request = _daily_usage_request(
        timezone=timezone,
        since=since,
        until=until,
        task_id=task_id,
        target=target,
        backend=backend,
        limit_days=limit_days,
        offline=offline,
    )
    now = utc_now()
    job_key = _daily_usage_job_key(run_id)
    with _usage_refresh_lock:
        state = _read_daily_usage_state(root, run_id, request)
        if job_key in _running_usage_refreshes:
            return _daily_usage_cached_payload(run_id, request, state)
        _running_usage_refreshes[job_key] = {"cancel_event": threading.Event(), "process": None}
        state.update(
            {
                "schema_version": USAGE_CACHE_SCHEMA_VERSION,
                "status": "running",
                "started_at": now,
                "finished_at": "",
                "error": "",
                "request": _daily_usage_request_payload(request),
            }
        )
        _write_daily_usage_state(root, run_id, request, state)
    try:
        dispatch_daily_usage_refresh_job(root, run_id, request)
    except Exception:
        with _usage_refresh_lock:
            _running_usage_refreshes.pop(job_key, None)
        raise
    return _daily_usage_cached_payload(run_id, request, _read_daily_usage_state(root, run_id, request))


def stop_daily_token_usage_refresh(
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
    request = _daily_usage_request(
        timezone=timezone,
        since=since,
        until=until,
        task_id=task_id,
        target=target,
        backend=backend,
        limit_days=limit_days,
        offline=offline,
    )
    job_key = _daily_usage_job_key(run_id)
    stopped = False
    with _usage_refresh_lock:
        refresh_job = _running_usage_refreshes.get(job_key)
        if refresh_job:
            cancel_event = refresh_job.get("cancel_event")
            if hasattr(cancel_event, "set"):
                cancel_event.set()
            process = refresh_job.get("process")
            if isinstance(process, subprocess.Popen) and process.poll() is None:
                process.terminate()
            stopped = True
    if stopped:
        state = _read_daily_usage_state(root, run_id, request)
        state.update(
            {
                "schema_version": USAGE_CACHE_SCHEMA_VERSION,
                "status": "stopping",
                "error": "",
                "request": _daily_usage_request_payload(request),
            }
        )
        _write_daily_usage_state(root, run_id, request, state)
        payload = _daily_usage_cached_payload(run_id, request, state)
    else:
        state = _read_daily_usage_state(root, run_id, request)
        if state.get("status") == "running":
            state.update(
                {
                    "schema_version": USAGE_CACHE_SCHEMA_VERSION,
                    "status": "cancelled",
                    "finished_at": utc_now(),
                    "error": "ccusage refresh stopped",
                    "request": _daily_usage_request_payload(request),
                }
            )
            _write_daily_usage_state(root, run_id, request, state)
        payload = _daily_usage_cached_payload(run_id, request, state)
    payload["stopped"] = stopped
    return payload
