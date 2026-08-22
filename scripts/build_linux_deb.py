#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from aha_cli.services.debian_package import (  # noqa: E402
    build_linux_deb,
    debian_architecture,
    debian_version,
)
from aha_cli.services.service_upgrade import zipapp_build_version  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an AHA Debian package from the portable onebin.")
    parser.add_argument("--onebin", default="dist/aha", help="Portable AHA onebin")
    parser.add_argument("--output", default="", help="Output .deb path")
    parser.add_argument("--architecture", default="auto", help="Debian architecture (auto, amd64, arm64)")
    parser.add_argument("--version", default="", help="Package version; defaults to the onebin build version")
    args = parser.parse_args(argv)

    artifact = Path(args.onebin)
    raw_version = args.version or zipapp_build_version(artifact.expanduser().resolve())
    version = debian_version(raw_version)
    architecture = debian_architecture(args.architecture)
    output = Path(args.output or f"dist/aha_{version}_{architecture}.deb")
    try:
        built = build_linux_deb(
            onebin=artifact,
            output=output,
            architecture=architecture,
            version=version,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Built Debian package: {built}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
