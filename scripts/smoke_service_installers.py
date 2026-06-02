#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_ONEBIN = REPO_ROOT / "scripts" / "install_user_service.sh"
INSTALL_SOURCE = REPO_ROOT / "scripts" / "install_source_user_service.sh"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def smoke_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("AHA_HOME", None)
    env.pop("AHA_RUN_ID", None)
    env["HOME"] = str(tmp_path / "home")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["USER"] = "aha-smoke"
    return env


def run_command(argv: list[str], *, env: dict[str, str] | None = None, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        cwd=REPO_ROOT,
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


def check_shell_syntax() -> None:
    run_command(["bash", "-n", str(INSTALL_ONEBIN)], timeout=5.0)
    run_command(["bash", "-n", str(INSTALL_SOURCE)], timeout=5.0)


def check_onebin_dry_run(tmp_path: Path) -> dict:
    env = smoke_env(tmp_path)
    home_aha = Path(env["HOME"]) / ".aha"
    bin_path = tmp_path / "bin" / "aha"
    aha_home = tmp_path / "onebin-home"
    token_file = aha_home / "web-token"
    service_path = tmp_path / "config" / "systemd" / "user" / "aha-smoke.service"
    result = run_command(
        [
            "bash",
            str(INSTALL_ONEBIN),
            "--dry-run",
            "--bin",
            str(bin_path),
            "--aha-home",
            str(aha_home),
            "--port",
            "18788",
            "--run-id",
            "smoke-run",
            "--service-name",
            "aha-smoke",
            "--no-start",
            "--no-linger",
        ],
        env=env,
    )
    require("Dry-run: no files written, no executable built, no services changed" in result.stdout, "onebin dry-run banner missing")
    require(f"Service path: {service_path}" in result.stdout, "onebin dry-run service path mismatch")
    require(f'Environment="AHA_HOME={aha_home}"' in result.stdout, "onebin dry-run AHA_HOME missing")
    require("Health URL: http://127.0.0.1:18788/api/health" in result.stdout, "onebin dry-run health URL missing")
    require("Upgrade validation: 1" in result.stdout, "onebin dry-run upgrade validation missing")
    require("Auth required: 1" in result.stdout, "onebin dry-run auth requirement missing")
    require(f"Auth token file: {token_file}" in result.stdout, "onebin dry-run auth token file missing")
    require(
        f'ExecStart="{bin_path}" --home "{aha_home}" ui "smoke-run" --host "127.0.0.1" --port 18788 --auth-token-file "{token_file}"' in result.stdout,
        "onebin dry-run ExecStart mismatch",
    )
    require(not bin_path.exists(), "onebin dry-run unexpectedly built the executable")
    require(not aha_home.exists(), "onebin dry-run unexpectedly created AHA_HOME")
    require(not home_aha.exists(), "onebin dry-run unexpectedly created HOME/.aha")
    require(not service_path.exists(), "onebin dry-run unexpectedly wrote the service file")
    require(not token_file.exists(), "onebin dry-run unexpectedly wrote the auth token")
    return {
        "service_path": str(service_path),
        "auth_token_file": str(token_file),
        "built_executable": bin_path.exists(),
        "created_aha_home": aha_home.exists(),
        "created_home_aha": home_aha.exists(),
        "wrote_service": service_path.exists(),
        "wrote_token": token_file.exists(),
    }


def check_source_dry_run(tmp_path: Path) -> dict:
    env = smoke_env(tmp_path)
    home_aha = Path(env["HOME"]) / ".aha"
    aha_home = tmp_path / "source-home"
    token_file = aha_home / "web-token"
    service_path = tmp_path / "config" / "systemd" / "user" / "aha-src-smoke.service"
    python_bin = os.path.abspath(os.path.expanduser(sys.executable))
    result = run_command(
        [
            "bash",
            str(INSTALL_SOURCE),
            "--dry-run",
            "--aha-home",
            str(aha_home),
            "--port",
            "18766",
            "--run-id",
            "source-smoke-run",
            "--python",
            python_bin,
            "--service-name",
            "aha-src-smoke",
            "--no-start",
            "--no-enable",
        ],
        env=env,
    )
    require("Dry-run: no files written, no services changed" in result.stdout, "source dry-run banner missing")
    require(f"Service path: {service_path}" in result.stdout, "source dry-run service path mismatch")
    require(f"WorkingDirectory={REPO_ROOT}" in result.stdout, "source dry-run WorkingDirectory mismatch")
    require(f'Environment="PYTHONPATH={REPO_ROOT}/src"' in result.stdout, "source dry-run PYTHONPATH missing")
    require(f'Environment="AHA_HOME={aha_home}"' in result.stdout, "source dry-run AHA_HOME missing")
    require("Health URL: http://127.0.0.1:18766/api/health" in result.stdout, "source dry-run health URL missing")
    require("Version validation: 1" in result.stdout, "source dry-run version validation missing")
    require("Auth required: 1" in result.stdout, "source dry-run auth requirement missing")
    require(f"Auth token file: {token_file}" in result.stdout, "source dry-run auth token file missing")
    require(
        f'ExecStart="{python_bin}" -m aha_cli --home "{aha_home}" ui "source-smoke-run" --host "127.0.0.1" --port 18766 --auth-token-file "{token_file}"'
        in result.stdout,
        "source dry-run ExecStart mismatch",
    )
    require(not aha_home.exists(), "source dry-run unexpectedly created AHA_HOME")
    require(not home_aha.exists(), "source dry-run unexpectedly created HOME/.aha")
    require(not service_path.exists(), "source dry-run unexpectedly wrote the service file")
    require(not token_file.exists(), "source dry-run unexpectedly wrote the auth token")
    return {
        "service_path": str(service_path),
        "auth_token_file": str(token_file),
        "created_aha_home": aha_home.exists(),
        "created_home_aha": home_aha.exists(),
        "wrote_service": service_path.exists(),
        "wrote_token": token_file.exists(),
    }


def run_smoke() -> dict:
    check_shell_syntax()
    with tempfile.TemporaryDirectory(prefix="aha-service-installers-") as tmp:
        tmp_path = Path(tmp)
        onebin = check_onebin_dry_run(tmp_path / "onebin")
        source = check_source_dry_run(tmp_path / "source")
        return {
            "checks": [
                "bash -n install_user_service.sh",
                "bash -n install_source_user_service.sh",
                "onebin dry-run unit no-write",
                "source dry-run unit no-write",
                "service health check URL",
                "entrypoint version validation",
                "service auth token file wiring",
                "installer dry-run AHA home no-write",
            ],
            "onebin": onebin,
            "source": source,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test service installer dry-run unit generation.")
    parser.add_argument("--json", action="store_true", help="Print compact JSON only")
    args = parser.parse_args(argv)

    try:
        result = run_smoke()
    except Exception as exc:  # noqa: BLE001
        print(f"service installer smoke failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print("Service installer smoke passed")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
