from __future__ import annotations

import json
from pathlib import Path

from aha_cli.constants import PLAN_FILE
from aha_cli.domain.models import utc_now
from aha_cli.store.paths import run_dir


def realtime_debug_path(root: Path, run_id: str) -> Path | None:
    selected_run_id = str(run_id or "").strip()
    if not selected_run_id:
        return None
    run_path = run_dir(root, selected_run_id)
    if not (run_path / PLAN_FILE).is_file():
        return None
    return run_path / "logs" / "realtime-debug.log"


def realtime_debug_log(source: str, **fields: object) -> None:
    root = fields.pop("_root", None)
    run_id = str(fields.get("run_id") or "")
    payload = {"ts": utc_now(), "source": source, **fields}
    line = "[aha realtime] " + json.dumps(payload, ensure_ascii=False, default=str)
    print(line, flush=True)
    if isinstance(root, Path):
        log_path = realtime_debug_path(root, run_id)
        if log_path is None:
            return
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


__all__ = ["realtime_debug_log", "realtime_debug_path"]
