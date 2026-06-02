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


def build_artifact(artifact: Path, env: dict[str, str]) -> None:
    run_command(
        [sys.executable, str(REPO_ROOT / "scripts" / "build_onebin.py"), "--output", str(artifact)],
        env=env,
        timeout=30.0,
    )
    require(artifact.exists(), f"onebin artifact was not created: {artifact}")


def run_smoke(*, artifact: Path, build: bool) -> dict:
    artifact = artifact.resolve()
    with tempfile.TemporaryDirectory(prefix="aha-onebin-smoke-") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        aha_home = workspace / ".aha"
        tmp_scan_root = tmp_path / "tmp"
        home.mkdir(parents=True)
        workspace.mkdir(parents=True)
        tmp_scan_root.mkdir(parents=True)
        env = smoke_env(home, tmp_scan_root)

        if build:
            build_artifact(artifact, env)
        require(artifact.exists(), f"onebin artifact is missing: {artifact}")

        help_result = run_command([str(artifact), "--help"], env=env)
        require("usage: aha" in help_result.stdout, "onebin help output missing usage")
        version_result = run_command([str(artifact), "--version"], env=env)
        require(version_result.stdout.strip().startswith("aha "), "onebin version output missing")

        init_result = run_command([str(artifact), "init", "--portable", "--force"], env=env, cwd=workspace)
        require(aha_home.exists(), "init did not create temporary AHA home")
        require("AHA initialized" in init_result.stdout or "Initialized" in init_result.stdout, "init output was unexpected")

        diagnose_result = run_command(
            [str(artifact), "--home", str(aha_home), "runs", "diagnose", "--json"],
            env=env,
        )
        diagnose = json.loads(diagnose_result.stdout)
        require(str(diagnose.get("aha_home") or "") == str(aha_home), "diagnose returned the wrong AHA home")
        for key in ("visible_runs", "active_heartbeat_runs", "stale_running_agents", "runs", "cleanup", "services"):
            require(key in diagnose, f"diagnose JSON missing {key}")

        cleanup_result = run_command(
            [
                str(artifact),
                "--home",
                str(aha_home),
                "runs",
                "cleanup",
                "--dry-run",
                "--json",
                "--tmp-root",
                str(tmp_scan_root),
            ],
            env=env,
        )
        cleanup = json.loads(cleanup_result.stdout)
        require(cleanup.get("dry_run") is True, "cleanup smoke must stay dry-run")
        for key in ("candidates", "deleted", "protected", "errors"):
            require(key in cleanup, f"cleanup JSON missing {key}")

        plan_result = run_command([str(artifact), "--home", str(aha_home), "plan", "Delete smoke", "--agents", "1"], env=env, cwd=workspace)
        run_id = next(line.split(": ", 1)[1] for line in plan_result.stdout.splitlines() if line.startswith("Created run:"))
        run_path = aha_home / "runs" / run_id
        smoke_log = run_path / "logs" / "retention-smoke.log"
        smoke_prompt = run_path / "prompts" / "retention-smoke.md"
        smoke_log.parent.mkdir(parents=True, exist_ok=True)
        smoke_prompt.parent.mkdir(parents=True, exist_ok=True)
        smoke_log.write_text("retention smoke log\n", encoding="utf-8")
        smoke_prompt.write_text("retention smoke prompt\n", encoding="utf-8")
        retention_result = run_command([str(artifact), "--home", str(aha_home), "runs", "retention", run_id, "--json"], env=env)
        retention = json.loads(retention_result.stdout)
        require(retention.get("run_id") == run_id, "runs retention returned the wrong run id")
        require(retention.get("dry_run") is True, "runs retention must be read-only/dry-run")
        require({item.get("path") for item in retention.get("candidates") or []} >= {"logs/retention-smoke.log", "prompts/retention-smoke.md"}, "runs retention did not find smoke candidates")
        retention_policy_result = run_command(
            [str(artifact), "--home", str(aha_home), "runs", "retention", run_id, "--max-candidate-bytes", "1", "--json"],
            env=env,
        )
        retention_policy = json.loads(retention_policy_result.stdout)
        automation = (retention_policy.get("policy_report") or {}).get("automation") or {}
        require(automation.get("over_limit") is True, "runs retention policy threshold did not alert")
        require(automation.get("recommended_action") == "apply_retention", "runs retention policy did not recommend apply")
        all_run_policy_result = run_command(
            [str(artifact), "--home", str(aha_home), "runs", "retention-policy", "--max-candidate-bytes", "1", "--json"],
            env=env,
        )
        all_run_policy = json.loads(all_run_policy_result.stdout)
        require(all_run_policy.get("dry_run") is True, "runs retention-policy must stay dry-run by default")
        require(all_run_policy.get("summary", {}).get("over_limit_runs", 0) >= 1, "runs retention-policy did not report threshold alerts")
        require(
            {item.get("run_id") for item in all_run_policy.get("runs") or []} >= {run_id},
            "runs retention-policy did not include the smoke run",
        )
        report_dir = tmp_path / "retention-policy-reports"
        policy_report_result = run_command(
            [
                str(artifact),
                "--home",
                str(aha_home),
                "runs",
                "retention-policy",
                "--max-candidate-bytes",
                "1",
                "--write-report",
                "--report-dir",
                str(report_dir),
                "--json",
            ],
            env=env,
        )
        policy_report = json.loads(policy_report_result.stdout)
        scheduled = policy_report.get("scheduled_report") or {}
        require(Path(str(scheduled.get("path") or "")).exists(), "runs retention-policy --write-report did not write report")
        require((report_dir / "latest.json").exists(), "runs retention-policy --write-report did not update latest.json")
        archive_dir = tmp_path / "archives"
        retention_apply_result = run_command(
            [
                str(artifact),
                "--home",
                str(aha_home),
                "runs",
                "retention",
                run_id,
                "--apply",
                "--json",
                "--archive-dir",
                str(archive_dir),
            ],
            env=env,
        )
        retention_apply = json.loads(retention_apply_result.stdout)
        require(retention_apply.get("dry_run") is False, "runs retention --apply did not leave dry-run mode")
        require(Path(retention_apply["archive"]["path"]).exists(), "runs retention --apply did not create an archive")
        require(smoke_log.exists() and smoke_prompt.exists(), "runs retention --apply must preserve originals without --force")
        retention_force_result = run_command(
            [
                str(artifact),
                "--home",
                str(aha_home),
                "runs",
                "retention",
                run_id,
                "--apply",
                "--force",
                "--json",
                "--archive-dir",
                str(archive_dir),
            ],
            env=env,
        )
        retention_force = json.loads(retention_force_result.stdout)
        require(retention_force.get("force") is True, "runs retention --apply --force did not report force")
        require(not smoke_log.exists() and not smoke_prompt.exists(), "runs retention --apply --force did not delete archived candidates")
        archive_path = Path(retention_apply["archive"]["path"])
        retention_archive_list_result = run_command(
            [
                str(artifact),
                "--home",
                str(aha_home),
                "runs",
                "retention-archive",
                "list",
                run_id,
                "--archive-dir",
                str(archive_dir),
                "--json",
            ],
            env=env,
        )
        retention_archives = json.loads(retention_archive_list_result.stdout)
        require(retention_archives.get("archives"), "runs retention-archive list did not find archives")
        retention_archive_inspect_result = run_command(
            [str(artifact), "--home", str(aha_home), "runs", "retention-archive", "inspect", str(archive_path), "--json"],
            env=env,
        )
        retention_archive_inspect = json.loads(retention_archive_inspect_result.stdout)
        require(retention_archive_inspect.get("source_run_id") == run_id, "runs retention-archive inspect returned the wrong source run")
        retention_archive_restore_result = run_command(
            [str(artifact), "--home", str(aha_home), "runs", "retention-archive", "restore", str(archive_path), "--json"],
            env=env,
        )
        retention_archive_restore = json.loads(retention_archive_restore_result.stdout)
        require(retention_archive_restore.get("restored"), "runs retention-archive restore did not restore files")
        require(smoke_log.exists() and smoke_prompt.exists(), "runs retention-archive restore did not recreate compacted files")
        recover_result = run_command([str(artifact), "--home", str(aha_home), "runs", "recover", run_id, "--json"], env=env)
        recover = json.loads(recover_result.stdout)
        require(recover.get("dry_run") is True, "runs recover must default to dry-run")
        require("candidates" in recover, "runs recover JSON missing candidates")
        delete_result = run_command([str(artifact), "--home", str(aha_home), "runs", "delete", run_id, "--json"], env=env)
        delete_payload = json.loads(delete_result.stdout)
        require(delete_payload.get("ok") is True, "runs delete JSON missing ok")
        require(not (aha_home / "runs" / run_id).exists(), "runs delete did not remove the run directory")
        remaining_runs = sorted(path.name for path in (aha_home / "runs").iterdir() if path.is_dir()) if (aha_home / "runs").is_dir() else []
        require(not remaining_runs, f"onebin smoke left run directories behind: {remaining_runs}")

        return {
            "artifact": str(artifact),
            "aha_home": str(aha_home),
            "home": str(home),
            "checks": [
                "help",
                "--version",
                "init --portable",
                "runs diagnose --json",
                "runs cleanup --dry-run --json",
                "runs retention --json",
                "runs retention policy threshold --json",
                "runs retention-policy --json",
                "runs retention-policy --write-report --json",
                "runs retention --apply --json",
                "runs retention --apply --force --json",
                "runs retention-archive list --json",
                "runs retention-archive inspect --json",
                "runs retention-archive restore --json",
                "runs recover --json",
                "runs delete --json",
            ],
            "diagnose": {
                "visible_runs": len(diagnose.get("visible_runs") or []),
                "active_heartbeat_runs": len(diagnose.get("active_heartbeat_runs") or []),
                "stale_running_agents": len(diagnose.get("stale_running_agents") or []),
            },
            "cleanup": {
                "dry_run": cleanup.get("dry_run"),
                "candidates": len(cleanup.get("candidates") or []),
                "protected": len(cleanup.get("protected") or []),
            },
            "remaining_runs": remaining_runs,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test the AHA onebin CLI in an isolated home.")
    parser.add_argument("--artifact", default=str(REPO_ROOT / "dist" / "aha"), help="onebin artifact path")
    parser.add_argument("--skip-build", action="store_true", help="Use the existing artifact instead of building it first")
    parser.add_argument("--json", action="store_true", help="Print compact JSON only")
    args = parser.parse_args(argv)

    try:
        result = run_smoke(artifact=Path(args.artifact), build=not args.skip_build)
    except Exception as exc:  # noqa: BLE001
        print(f"onebin smoke failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print("Onebin CLI smoke passed")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
