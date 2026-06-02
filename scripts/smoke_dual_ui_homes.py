#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_command(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: Path = REPO_ROOT,
    timeout: float = 15.0,
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


def smoke_env(home: Path, tmp_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("AHA_HOME", None)
    env.pop("AHA_RUN_ID", None)
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp_root)
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    return env


def source_env(base: dict[str, str]) -> dict[str, str]:
    env = base.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT / "src") if not existing else f"{REPO_ROOT / 'src'}{os.pathsep}{existing}"
    return env


def build_artifact(artifact: Path, env: dict[str, str]) -> None:
    run_command(
        [sys.executable, str(REPO_ROOT / "scripts" / "build_onebin.py"), "--output", str(artifact)],
        env=env,
        timeout=30.0,
    )
    require(artifact.exists(), f"onebin artifact was not created: {artifact}")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def created_run_id(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("Created run:"):
            return line.split(": ", 1)[1]
    raise AssertionError(f"could not parse run id from output: {output}")


def start_server(argv: list[str], *, env: dict[str, str], cwd: Path, log_path: Path) -> subprocess.Popen[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
    finally:
        handle.close()


def stop_server(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def fetch_json(url: str, *, timeout: float = 10.0, token: str = "") -> dict:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            request = urllib.request.Request(url)
            if token:
                request.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(request, timeout=1.0) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"HTTP check failed for {url}: {last_error}")


def fetch_status(url: str, *, timeout: float = 10.0) -> int:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"HTTP status check failed for {url}: {last_error}")


def run_smoke(*, artifact: Path, build: bool) -> dict:
    artifact = artifact.resolve()
    with tempfile.TemporaryDirectory(prefix="aha-dual-ui-smoke-") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        tmp_root = tmp_path / "tmp"
        source_home = workspace / ".aha"
        onebin_home = home / ".aha"
        home.mkdir(parents=True)
        workspace.mkdir(parents=True)
        tmp_root.mkdir(parents=True)
        base_env = smoke_env(home, tmp_root)
        src_env = source_env(base_env)

        if build:
            build_artifact(artifact, base_env)
        require(artifact.exists(), f"onebin artifact is missing: {artifact}")

        run_command([sys.executable, "-m", "aha_cli", "--home", str(source_home), "init", "--force"], env=src_env, cwd=workspace)
        source_plan = run_command(
            [sys.executable, "-m", "aha_cli", "--home", str(source_home), "plan", "SRC-SMOKE", "--agents", "1"],
            env=src_env,
            cwd=workspace,
        )
        onebin_plan = run_command(
            [str(artifact), "--home", str(onebin_home), "plan", "ONEBIN-SMOKE", "--agents", "1"],
            env=base_env,
            cwd=workspace,
        )
        source_run_id = created_run_id(source_plan.stdout)
        onebin_run_id = created_run_id(onebin_plan.stdout)
        source_token = "source-smoke-token"
        onebin_token = "onebin-smoke-token"
        source_token_file = source_home / "web-token"
        onebin_token_file = onebin_home / "web-token"
        source_token_file.write_text(f"{source_token}\n", encoding="utf-8")
        onebin_token_file.write_text(f"{onebin_token}\n", encoding="utf-8")
        source_port = free_port()
        onebin_port = free_port()

        source_proc = start_server(
            [
                sys.executable,
                "-m",
                "aha_cli",
                "--home",
                str(source_home),
                "ui",
                source_run_id,
                "--host",
                "127.0.0.1",
                "--port",
                str(source_port),
                "--auth-token-file",
                str(source_token_file),
            ],
            env=src_env,
            cwd=workspace,
            log_path=tmp_path / "source-ui.log",
        )
        onebin_proc = start_server(
            [
                str(artifact),
                "--home",
                str(onebin_home),
                "ui",
                onebin_run_id,
                "--host",
                "127.0.0.1",
                "--port",
                str(onebin_port),
                "--auth-token-file",
                str(onebin_token_file),
            ],
            env=base_env,
            cwd=workspace,
            log_path=tmp_path / "onebin-ui.log",
        )
        try:
            source_unauth = fetch_status(f"http://127.0.0.1:{source_port}/api/bootstrap?run_id={source_run_id}")
            onebin_unauth = fetch_status(f"http://127.0.0.1:{onebin_port}/api/bootstrap?run_id={onebin_run_id}")
            source_bootstrap = fetch_json(f"http://127.0.0.1:{source_port}/api/bootstrap?run_id={source_run_id}", token=source_token)
            onebin_bootstrap = fetch_json(f"http://127.0.0.1:{onebin_port}/api/bootstrap?run_id={onebin_run_id}", token=onebin_token)
            source_health = fetch_json(f"http://127.0.0.1:{source_port}/api/health")
            onebin_health = fetch_json(f"http://127.0.0.1:{onebin_port}/api/health")
            require(source_unauth == 401, f"source UI did not reject unauthenticated bootstrap: {source_unauth}")
            require(onebin_unauth == 401, f"onebin UI did not reject unauthenticated bootstrap: {onebin_unauth}")
            require(source_bootstrap["aha_home"] == str(source_home), "source UI returned the wrong AHA home")
            require(onebin_bootstrap["aha_home"] == str(onebin_home), "onebin UI returned the wrong AHA home")
            require(source_health["ok"] is True, "source health endpoint did not report ok")
            require(onebin_health["ok"] is True, "onebin health endpoint did not report ok")
            require(source_health["auth_required"] is True, "source health did not report auth_required")
            require(onebin_health["auth_required"] is True, "onebin health did not report auth_required")
            require(source_health["aha_home"] == str(source_home), "source health returned the wrong AHA home")
            require(onebin_health["aha_home"] == str(onebin_home), "onebin health returned the wrong AHA home")
            require(str(source_health.get("bind_port")) == str(source_port), "source health returned the wrong bind port")
            require(str(onebin_health.get("bind_port")) == str(onebin_port), "onebin health returned the wrong bind port")
            require(source_health.get("bind_port") != onebin_health.get("bind_port"), "source and onebin bind ports should differ")
            require(source_bootstrap["default_run_id"] == source_run_id, "source UI returned the wrong default run")
            require(onebin_bootstrap["default_run_id"] == onebin_run_id, "onebin UI returned the wrong default run")
            source_goals = {item["goal"] for item in source_bootstrap["runs"]}
            onebin_goals = {item["goal"] for item in onebin_bootstrap["runs"]}
            require(source_goals == {"SRC-SMOKE"}, f"source UI runs leaked or missing goals: {source_goals}")
            require(onebin_goals == {"ONEBIN-SMOKE"}, f"onebin UI runs leaked or missing goals: {onebin_goals}")
        finally:
            stop_server(source_proc)
            stop_server(onebin_proc)

        source_delete = json.loads(
            run_command(
                [
                    sys.executable,
                    "-m",
                    "aha_cli",
                    "--home",
                    str(source_home),
                    "runs",
                    "delete",
                    source_run_id,
                    "--force",
                    "--json",
                ],
                env=src_env,
                cwd=workspace,
            ).stdout
        )
        onebin_delete = json.loads(
            run_command(
                [
                    str(artifact),
                    "--home",
                    str(onebin_home),
                    "runs",
                    "delete",
                    onebin_run_id,
                    "--force",
                    "--json",
                ],
                env=base_env,
                cwd=workspace,
            ).stdout
        )
        source_remaining_runs = sorted(path.name for path in (source_home / "runs").iterdir() if path.is_dir()) if (source_home / "runs").is_dir() else []
        onebin_remaining_runs = sorted(path.name for path in (onebin_home / "runs").iterdir() if path.is_dir()) if (onebin_home / "runs").is_dir() else []
        require(source_delete.get("ok") is True, "source smoke run deletion did not report ok")
        require(onebin_delete.get("ok") is True, "onebin smoke run deletion did not report ok")
        require(source_run_id not in source_remaining_runs, "source smoke run was left behind")
        require(onebin_run_id not in onebin_remaining_runs, "onebin smoke run was left behind")

        return {
            "artifact": str(artifact),
            "source_home": str(source_home),
            "source_run_id": source_run_id,
            "source_port": source_port,
            "source_auth": True,
            "onebin_home": str(onebin_home),
            "onebin_run_id": onebin_run_id,
            "onebin_port": onebin_port,
            "onebin_auth": True,
            "checks": [
                "source ui home",
                "onebin ui home",
                "service health endpoints",
                "bind port isolation",
                "run isolation",
                "token-protected ui",
                "smoke run cleanup",
            ],
            "remaining_runs": {
                "source": source_remaining_runs,
                "onebin": onebin_remaining_runs,
            },
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test source and onebin UIs with isolated AHA homes.")
    parser.add_argument("--artifact", default=str(REPO_ROOT / "dist" / "aha"), help="onebin artifact path")
    parser.add_argument("--skip-build", action="store_true", help="Use the existing artifact instead of building it first")
    parser.add_argument("--json", action="store_true", help="Print compact JSON only")
    args = parser.parse_args(argv)

    try:
        result = run_smoke(artifact=Path(args.artifact), build=not args.skip_build)
    except Exception as exc:  # noqa: BLE001
        print(f"dual UI smoke failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print("Dual UI home smoke passed")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
