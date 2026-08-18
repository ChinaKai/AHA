"""L3 watchdog: actively detect and recover stuck backend workers.

The stale-recovery path (``recover_stale_running_agents``) only fires when a
backend process has already exited (state status ``stopped``). A worker that
stays ``running`` in the state file but never consumes its inbox — e.g. a WSL
worker whose self-stop was rejected by a pid-namespace mismatch, or a cursor
that jumped past the pending messages — leaves every new message unanswered
until a user manually resets the session.

This module is the L3 self-healing layer: a periodic scan finds agents whose
backend is not consuming pending inbox messages with no recent activity, and
force-restarts them so the pending work is consumed. This includes a worker
that crashed before changing its lifecycle from ``pending`` to ``running``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.backend_runtime import backend_status, start_backend, stop_backend
from aha_cli.services.chat_offsets import chat_inbox_has_inflight_turn, chat_inbox_has_pending
from aha_cli.store.agents import update_agent_runtime
from aha_cli.store.filesystem import append_event, status_snapshot
from aha_cli.store.paths import inbox_path
from aha_cli.store.runs import list_run_summaries


def _coerce_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value

# A backend that has been idle (no agent activity) for this long while its
# inbox holds pending messages is considered stuck and force-restarted.
WATCHDOG_STUCK_SECONDS = 90.0
# Minimum interval between two forced restarts of the same agent. Guards
# against a restart loop when a worker keeps dying on the same message.
WATCHDOG_MIN_RESTART_INTERVAL_SECONDS = 30.0
# Restart repeatedly (with the min interval) at most this many times per scan
# sweep for one agent before giving up and leaving the message pending.
WATCHDOG_MAX_RESTARTS_PER_SCAN = 3


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _seconds_since(ts: datetime | None, *, now: datetime | None = None) -> float | None:
    if ts is None:
        return None
    reference = _coerce_aware(now) if now is not None else datetime.fromisoformat(utc_now())
    return (_coerce_aware(reference) - _coerce_aware(ts)).total_seconds()


def _pending_inbox_mtime(root: Path, run_id: str, task_id: str, target: str) -> datetime | None:
    inbox = inbox_path(root, run_id, target, task_id)
    try:
        return datetime.fromtimestamp(inbox.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _backend_last_activity(state: dict) -> datetime | None:
    activity = state.get("activity") if isinstance(state.get("activity"), dict) else state
    candidates = (
        activity.get("last_started_at"),
        activity.get("last_finished_at"),
        activity.get("last_reply_at"),
        activity.get("last_error_at"),
    )
    parsed = [_parse_ts(item) for item in candidates]
    parsed = [item for item in parsed if item is not None]
    return max(parsed) if parsed else None


def stuck_agent_reason(
    root: Path,
    run_id: str,
    task: dict,
    agent: dict,
    state: dict,
    *,
    now: datetime | None = None,
) -> str | None:
    """Return a recovery reason when ``agent``'s backend is stuck, else None.

    Stuck means the inbox has pending messages that have not been consumed for
    longer than ``WATCHDOG_STUCK_SECONDS`` and either a ``running`` lifecycle
    has an idle ``running`` backend, or a ``pending`` lifecycle has a stopped
    backend after a failed launch. A ``busy`` backend is left alone.
    """
    task_id = str(task.get("id") or "")
    agent_id = str(agent.get("id") or "main")
    if not task_id or not agent_id:
        return None
    if str(task.get("status") or "") != "running":
        return None
    agent_status = str(agent.get("status") or "")
    if agent_status not in {"pending", "running"}:
        return None
    backend_status_value = str(state.get("status") or "stopped").lower()
    if backend_status_value == "busy":
        return None
    if agent_status == "running" and backend_status_value not in {"running", "stopped"}:
        return None
    if agent_status == "pending" and backend_status_value != "stopped":
        return None
    if not chat_inbox_has_pending(root, run_id, agent_id, task_id):
        return None
    if (
        agent_status == "running"
        and backend_status_value == "stopped"
        and not chat_inbox_has_inflight_turn(root, run_id, agent_id, task_id)
    ):
        return None
    last_activity = _backend_last_activity(state)
    if last_activity is None and agent_status == "pending":
        last_activity = _pending_inbox_mtime(root, run_id, task_id, agent_id)
    idle = _seconds_since(last_activity, now=now)
    if idle is None or idle < WATCHDOG_STUCK_SECONDS:
        return None
    if agent_status == "pending":
        return "backend_stopped_with_pending_inbox"
    if backend_status_value == "stopped":
        return "backend_stopped_with_inflight_inbox"
    return "backend_running_but_not_consuming_inbox"


def recover_stuck_agent(
    root: Path,
    run_id: str,
    task: dict,
    agent: dict,
    *,
    reason: str,
) -> dict:
    """Force-stop and restart a stuck backend, leaving pending inbox work intact.

    Unlike ``record_agent_interrupt`` (which advances the chat cursor past every
    pending message), recovery here must NOT drop the pending messages — the
    user is still waiting on them. So the cursor is preserved and the backend is
    restarted to consume it.
    """
    task_id = str(task.get("id") or "")
    agent_id = str(agent.get("id") or "main")
    backend = str(agent.get("backend") or task.get("preferred_backend") or "claude")
    model = agent.get("model") or task.get("preferred_model")
    sandbox = agent.get("sandbox") or task.get("preferred_sandbox") or "danger-full-access"
    approval = agent.get("approval") or task.get("preferred_approval") or "never"

    stopped = stop_backend(root, run_id, agent_id, task_id=task_id, timeout=3.0)
    started = start_backend(
        root,
        run_id,
        agent_id,
        backend=backend,
        model=model,
        sandbox=sandbox,
        approval=approval,
        from_start=False,
        task_id=task_id,
    )
    update_agent_runtime(
        root,
        run_id,
        task_id,
        agent_id,
        recovery_context=f"backend watchdog restarted (stuck: {reason})",
        recovery_context_reason=reason,
        recovery_context_at=utc_now(),
        recovery_context_consumed_at="",
    )
    append_event(
        root,
        run_id,
        "agent_watchdog_recovered",
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "backend": backend,
            "reason": reason,
            "stopped_status": stopped.get("status"),
            "started_status": started.get("status"),
        },
    )
    return {"task_id": task_id, "agent_id": agent_id, "reason": reason}


def _recent_watchdog_recoveries(root: Path, run_id: str, *, limit: int = 200) -> dict[str, datetime]:
    """Map ``{task_id}:{agent_id}`` -> latest watchdog recovery ts in the event stream."""
    from aha_cli.store.filesystem import event_path, iter_jsonl_reverse

    path = event_path(root, run_id)
    if not path.exists():
        return {}
    latest: dict[str, datetime] = {}
    scanned = 0
    for _offset, event in iter_jsonl_reverse(path) or ():
        scanned += 1
        if scanned > limit:
            break
        if event.get("type") != "agent_watchdog_recovered":
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        key = f"{data.get('task_id')}:{data.get('agent_id')}"
        parsed = _parse_ts(event.get("ts"))
        if key and parsed and (key not in latest or parsed > latest[key]):
            latest[key] = parsed
    return latest


def scan_run(root: Path, run_id: str, *, now: datetime | None = None) -> dict:
    """Scan one run's active agents and recover stuck backends.

    Returns a summary with ``checked`` and ``recovered`` entries. ``now`` is
    injectable for tests; defaults to the current wall clock.
    """
    reference = _coerce_aware(now) if now is not None else datetime.fromisoformat(utc_now())
    snapshot = status_snapshot(root, run_id)
    recent = _recent_watchdog_recoveries(root, run_id)
    checked = 0
    recovered: list[dict] = []
    restarts_by_agent: dict[str, int] = {}
    for task in snapshot.get("tasks", []):
        task_id = str(task.get("id") or "")
        for agent in task.get("agents", []):
            agent_id = str(agent.get("id") or "main")
            if str(agent.get("status") or "") not in {"pending", "running"}:
                continue
            checked += 1
            state = backend_status(root, run_id, agent_id, task_id=task_id)
            reason = stuck_agent_reason(root, run_id, task, agent, state, now=reference)
            if not reason:
                continue
            key = f"{task_id}:{agent_id}"
            restarts = restarts_by_agent.get(key, 0)
            if restarts >= WATCHDOG_MAX_RESTARTS_PER_SCAN:
                continue
            last_recovery = recent.get(key)
            if last_recovery is not None:
                elapsed = _seconds_since(last_recovery, now=reference)
                if elapsed is not None and elapsed < WATCHDOG_MIN_RESTART_INTERVAL_SECONDS:
                    continue
            restarts_by_agent[key] = restarts + 1
            recovered.append(recover_stuck_agent(root, run_id, task, agent, reason=reason))
    return {"run_id": run_id, "checked": checked, "recovered": recovered}


def scan_all_runs(root: Path, *, now: datetime | None = None) -> dict:
    """Scan every run with running work and recover stuck backends."""
    summaries = list_run_summaries(root)
    active = [summary for summary in summaries if summary.get("has_running_work")]
    results: list[dict] = []
    for summary in active:
        try:
            results.append(scan_run(root, str(summary.get("id") or ""), now=now))
        except Exception as exc:  # noqa: BLE001 - one run must not break the sweep
            results.append({"run_id": summary.get("id"), "checked": 0, "recovered": [], "error": str(exc)})
    return {"runs": results, "checked": sum(int(r.get("checked") or 0) for r in results)}


__all__ = [
    "WATCHDOG_STUCK_SECONDS",
    "WATCHDOG_MIN_RESTART_INTERVAL_SECONDS",
    "WATCHDOG_MAX_RESTARTS_PER_SCAN",
    "scan_all_runs",
    "scan_run",
    "stuck_agent_reason",
    "recover_stuck_agent",
]
