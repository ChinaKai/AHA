"""Scheduled knowledge sync loop for the AHA Web service.

Runs ``knowledge.sync`` on an interval (``knowledge.sync.interval_minutes``,
default 60) while the UI server is alive. Behavior:

- **Mode gate.** Only runs when ``knowledge.sync.mode == "auto"`` and the KB is
  enabled, so a manual-only deployment is left untouched.
- **Single flight.** A sidecar lock file prevents overlapping syncs across
  processes; the maintenance state record prevents starting a sync while a KB
  maintenance agent is already resolving a conflict.
- **Conflict handoff.** When the sync pull conflicts and ``resolve_conflicts ==
  "agent"``, the rebase is left in progress and a KB maintenance job is
  dispatched to resolve it.
- **Silent unless notable.** Clean syncs only update the persisted state; the
  UI surfaces the state, so nothing is spammed to the user.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from pathlib import Path

from aha_cli.domain.models import utc_now
from aha_cli.services.knowledge_git import sync as knowledge_sync
from aha_cli.services.knowledge_maintenance import (
    dispatch_maintenance_job,
    maintenance_record,
    read_sync_state,
    should_dispatch_sync_agent,
    write_sync_state,
)
from aha_cli.store.config import load_config
from aha_cli.store.paths import aha_home_path

_SYNC_LOCK_STALE_SECONDS = 300.0
_MIN_INTERVAL_MINUTES = 1.0


def _sync_lock_path(root: Path) -> Path:
    return aha_home_path(root) / "knowledge_sync.lock"


def _try_acquire_sync_lock(root: Path) -> bool:
    path = _sync_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        os.close(fd)
        return True
    except FileExistsError:
        try:
            stale = time.time() - path.stat().st_mtime > _SYNC_LOCK_STALE_SECONDS
        except OSError:
            return False
        if not stale:
            return False
        try:
            path.unlink()
        except OSError:
            return False
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
            os.close(fd)
            return True
        except FileExistsError:
            return False
    except OSError:
        return False


def _release_sync_lock(root: Path) -> None:
    try:
        _sync_lock_path(root).unlink()
    except OSError:
        pass


def _sync_config(root: Path) -> tuple[dict, float, bool]:
    cfg = load_config(root)
    knowledge_cfg = cfg.get("knowledge") if isinstance(cfg, dict) else {}
    if not isinstance(knowledge_cfg, dict):
        knowledge_cfg = {}
    sync_cfg = knowledge_cfg.get("sync") if isinstance(knowledge_cfg.get("sync"), dict) else {}
    try:
        interval = max(_MIN_INTERVAL_MINUTES, float(sync_cfg.get("interval_minutes", 60)))
    except (TypeError, ValueError):
        interval = 60.0
    enabled = bool(knowledge_cfg.get("enabled", True))
    return sync_cfg, interval, enabled


def _run_scheduled_sync(root: Path) -> None:
    sync_cfg, _, enabled = _sync_config(root)
    if not enabled or str(sync_cfg.get("mode", "auto")) != "auto":
        return
    if maintenance_record(root).get("status") == "running":
        return  # a maintenance agent is already resolving a conflict
    if not _try_acquire_sync_lock(root):
        return  # another sync (manual or scheduled) is in flight
    try:
        cfg = load_config(root)
        result = knowledge_sync(
            root,
            cfg,
            message=f"chore(knowledge): scheduled sync {utc_now()}",
            do_pull=True,
            do_push=True,
        )
        _record_loop_state(root, result, interval_minutes=float(sync_cfg.get("interval_minutes", 60)))
        if should_dispatch_sync_agent(result):
            parameters = inspect.signature(dispatch_maintenance_job).parameters.values()
            supports_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
            names = {parameter.name for parameter in parameters}
            if supports_kwargs or {"sync_result", "source"}.issubset(names):
                dispatch_maintenance_job(root, cfg, sync_result=result, source="scheduled")
            else:
                dispatch_maintenance_job(root, cfg)
    finally:
        _release_sync_lock(root)


def _record_loop_state(root: Path, result: dict, *, interval_minutes: float) -> None:
    state = read_sync_state(root)
    state["loop"] = {
        "enabled": True,
        "interval_minutes": interval_minutes,
        "last_sync_at": utc_now(),
        "last_sync_ok": bool(result.get("ok")),
        "last_sync_state": "conflict" if result.get("conflict") else ("ok" if result.get("ok") else "error"),
        "last_unmerged": result.get("unmerged") or [],
    }
    write_sync_state(root, state)


async def run_knowledge_sync_loop(root: Path, *, interval_minutes: float | None = None) -> None:
    """Periodically sync the knowledge base while the UI server is alive."""
    while True:
        _, configured_interval, _ = await asyncio.to_thread(_sync_config, root)
        interval = configured_interval if interval_minutes is None else max(_MIN_INTERVAL_MINUTES, float(interval_minutes))
        await asyncio.sleep(interval * 60)
        try:
            await asyncio.to_thread(_run_scheduled_sync, root)
        except Exception:  # noqa: BLE001 - a failed scheduled sync must not kill the server
            pass
