#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from smoke_service_installers import run_smoke as run_service_installer_smoke


REPO_ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_command(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: Path = REPO_ROOT,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "\n".join(
                [
                    f"command failed ({completed.returncode}): {' '.join(argv)}",
                    completed.stdout.strip(),
                    completed.stderr.strip(),
                ]
            ).strip()
        )
    return completed


def isolated_env(home: Path, tmp_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("AHA_HOME", None)
    env.pop("AHA_RUN_ID", None)
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp_root)
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT / "src") if not existing else f"{REPO_ROOT / 'src'}{os.pathsep}{existing}"
    return env


def run_names(runs_dir: Path) -> set[str]:
    if not runs_dir.is_dir():
        return set()
    try:
        return {path.name for path in runs_dir.iterdir() if path.is_dir()}
    except OSError:
        return set()


def version_from_output(output: str) -> str:
    text = output.strip()
    require(text.startswith("aha "), f"unexpected version output: {text}")
    return text.split()[-1]


def check_source_version(env: dict[str, str]) -> str:
    result = run_command([sys.executable, "-m", "aha_cli", "--version"], env=env, timeout=10.0)
    return version_from_output(result.stdout)


def check_temp_onebin(env: dict[str, str], artifact: Path) -> dict:
    run_command(
        [sys.executable, str(REPO_ROOT / "scripts" / "build_onebin.py"), "--output", str(artifact)],
        env=env,
        timeout=45.0,
    )
    require(artifact.exists(), f"temporary onebin artifact was not created: {artifact}")
    version_result = run_command([str(artifact), "--version"], env=env, timeout=10.0)
    return {"built": True, "version": version_from_output(version_result.stdout)}


def run_preflight(*, skip_onebin_build: bool = False) -> dict:
    real_runs_dir = Path.home() / ".aha" / "runs"
    before_real_runs = run_names(real_runs_dir)
    with tempfile.TemporaryDirectory(prefix="aha-service-preflight-") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home"
        tmp_root = tmp_path / "tmp"
        bin_dir = tmp_path / "bin"
        home.mkdir(parents=True)
        tmp_root.mkdir(parents=True)
        bin_dir.mkdir(parents=True)
        env = isolated_env(home, tmp_root)

        source_version = check_source_version(env)
        if skip_onebin_build:
            onebin = {"built": False, "skipped": True, "reason": "--skip-onebin-build"}
        else:
            onebin = check_temp_onebin(env, bin_dir / "aha")
        installer_smoke = run_service_installer_smoke()

        isolated_home_runs = run_names(home / ".aha" / "runs")
        isolated_tmp_runs = sorted(path.name for path in tmp_path.rglob("runs/*") if path.is_dir())

    after_real_runs = run_names(real_runs_dir)
    require(before_real_runs == after_real_runs, "preflight changed the current user's ~/.aha/runs")
    require(not isolated_home_runs, "preflight created runs under its temporary HOME")

    checks = [
        "source entrypoint --version",
        "service installer dry-run no-write",
        "service auth token-file wiring",
        "service health URL plan",
        "real home run guard",
    ]
    if skip_onebin_build:
        checks.append("temporary onebin build skipped")
    else:
        checks.append("temporary onebin build --version")

    return {
        "status": "passed",
        "checks": checks,
        "source_version": source_version,
        "onebin": onebin,
        "installer_smoke_checks": installer_smoke["checks"],
        "real_home_runs": {
            "path": str(real_runs_dir),
            "before": len(before_real_runs),
            "after": len(after_real_runs),
            "unchanged": True,
        },
        "temporary_runs_seen": isolated_tmp_runs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run no-write preflight checks before upgrading AHA user services.")
    parser.add_argument("--skip-onebin-build", action="store_true", help="Skip the temporary onebin build/version check")
    parser.add_argument("--json", action="store_true", help="Print compact JSON only")
    args = parser.parse_args(argv)

    try:
        result = run_preflight(skip_onebin_build=args.skip_onebin_build)
    except Exception as exc:  # noqa: BLE001
        print(f"service upgrade preflight failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print("Service upgrade preflight passed")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
