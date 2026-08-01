from __future__ import annotations

from pathlib import Path
import queue
import threading
import time

from aha_cli.domain.models import normalize_integrations_config, utc_now
from aha_cli.store.config import load_config
from aha_cli.store.events import append_event as append_raw_event

NOTIFICATION_QUEUE_LIMIT = 256


class _FlushMarker:
    def __init__(self) -> None:
        self.done = threading.Event()


_notification_queue: queue.Queue[tuple[Path, str, dict] | _FlushMarker | None] = queue.Queue(
    maxsize=NOTIFICATION_QUEUE_LIMIT
)
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def enabled_notification_channels(root: Path) -> list[str]:
    integrations = normalize_integrations_config(load_config(root).get("integrations"))
    channels: list[str] = []
    weixin = integrations.get("weixin") if isinstance(integrations.get("weixin"), dict) else {}
    feishu = integrations.get("feishu") if isinstance(integrations.get("feishu"), dict) else {}
    if weixin.get("enabled"):
        channels.append("weixin")
    # Feishu must remain active for direct assistant replies even when optional
    # task-status notifications are disabled.
    if feishu.get("enabled"):
        channels.append("feishu")
    return channels


def _record_delivery_error(root: Path, run_id: str, channel: str, event: dict, exc: Exception) -> None:
    append_raw_event(
        root,
        run_id,
        f"{channel}_notification_failed",
        {
            "source_event_type": event.get("type"),
            "source_event_id": event.get("event_id"),
            "error": str(exc),
        },
        ts=utc_now(),
    )


def deliver_notification_event(root: Path, run_id: str, event: dict, channels: list[str] | None = None) -> list[dict]:
    results: list[dict] = []
    for channel in channels if channels is not None else enabled_notification_channels(root):
        try:
            if channel == "weixin":
                from aha_cli.services.weixin_notifications import notify_event
            elif channel == "feishu":
                from aha_cli.services.feishu_notifications import notify_event
            else:
                continue
            result = notify_event(root, run_id, event)
            results.append({"channel": channel, **(result if isinstance(result, dict) else {})})
        except Exception as exc:  # noqa: BLE001 - channel failures cannot break AHA state writes.
            _record_delivery_error(root, run_id, channel, event, exc)
            results.append({"channel": channel, "ok": False, "error": str(exc)})
    return results


def _worker_loop() -> None:
    while True:
        item = _notification_queue.get()
        try:
            if item is None:
                return
            if isinstance(item, _FlushMarker):
                item.done.set()
                continue
            root, run_id, event = item
            deliver_notification_event(root, run_id, event)
        finally:
            _notification_queue.task_done()


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_loop, name="aha-channel-notifications", daemon=True)
        _worker.start()


def enqueue_notification_event(root: Path, run_id: str, event: dict) -> dict:
    channels = enabled_notification_channels(root)
    if not channels:
        return {"queued": False, "reason": "no_enabled_channels", "channels": []}
    _ensure_worker()
    try:
        _notification_queue.put_nowait((root, run_id, event))
    except queue.Full:
        append_raw_event(
            root,
            run_id,
            "channel_notification_dropped",
            {
                "source_event_type": event.get("type"),
                "source_event_id": event.get("event_id"),
                "channels": channels,
                "reason": "queue_full",
            },
            ts=utc_now(),
        )
        return {"queued": False, "reason": "queue_full", "channels": channels}
    return {"queued": True, "channels": channels}


def wait_for_notification_queue(timeout_seconds: float | None = None) -> bool:
    """Wait until all items queued before this call have been processed."""
    _ensure_worker()
    marker = _FlushMarker()
    if timeout_seconds is None:
        _notification_queue.put(marker)
        return marker.done.wait()
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    try:
        _notification_queue.put(marker, timeout=max(0.0, deadline - time.monotonic()))
    except queue.Full:
        return False
    return marker.done.wait(timeout=max(0.0, deadline - time.monotonic()))


__all__ = [
    "deliver_notification_event",
    "enabled_notification_channels",
    "enqueue_notification_event",
    "wait_for_notification_queue",
]
