#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from aha_cli.services.onebin import build_onebin  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the AHA single-file executable zipapp.")
    parser.add_argument("--output", "-o", default="dist/aha", help="Output executable path")
    parser.add_argument("--interpreter", default="/usr/bin/env python3", help="Shebang interpreter for the artifact")
    parser.add_argument("--no-compress", action="store_true", help="Store files without ZIP compression")
    args = parser.parse_args(argv)

    artifact = build_onebin(
        Path(args.output),
        source_root=SRC_ROOT,
        interpreter=args.interpreter,
        compressed=not args.no_compress,
    )
    print(f"Built one-bin executable: {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
