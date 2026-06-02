#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if SRC_DIR.exists():
    sys.path.insert(0, str(SRC_DIR))

from aha_cli.services.run_cleanup import cleanup_temp_runs, format_cleanup_summary  # noqa: E402
from aha_cli.store.paths import find_aha_home  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List or clean stale temporary AHA run leftovers.")
    parser.add_argument("--home", default=None, help="AHA home to inspect; defaults to normal AHA discovery.")
    parser.add_argument("--current-run", default=os.environ.get("AHA_RUN_ID"), help="Run id that must never be deleted.")
    parser.add_argument("--tmp-root", default="/tmp", help="Temporary root to scan for nested .aha homes.")
    parser.add_argument("--stale-seconds", type=int, default=3600, help="Minimum age before a temporary candidate is deletable.")
    parser.add_argument("--active-heartbeat-seconds", type=int, default=120, help="Fresh heartbeat window that protects a run.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Actually delete candidates. Without this, the script is dry-run/list only.")
    mode.add_argument("--dry-run", action="store_true", help="List candidates without deleting files. This is the default.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    aha_home = Path(args.home).expanduser() if args.home else find_aha_home()
    result = cleanup_temp_runs(
        aha_home,
        current_run_id=args.current_run,
        tmp_root=Path(args.tmp_root).expanduser() if args.tmp_root else None,
        dry_run=not args.apply,
        stale_seconds=args.stale_seconds,
        active_heartbeat_seconds=args.active_heartbeat_seconds,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(format_cleanup_summary(result), end="")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
