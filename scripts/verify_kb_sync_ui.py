#!/usr/bin/env python3
"""Open the AHA knowledge console sync panel in a real browser and verify conflict UI.

Seeds a temp AHA home + git remote, creates a real sync conflict (agent edit on
the remote vs user edit locally), leaves the repo mid-rebase, starts the UI
server, then drives Chromium to the knowledge console:

1. asserts the sync status line reports the conflict and the "Resolve conflicts"
   button is visible,
2. clicks Resolve and polls /api/kb/sync-status until the maintenance job
   finishes (agent resolution or user-priority fallback),
3. asserts the repo is clean and the maintenance record says resolved,
4. captures screenshots of both the conflict and resolved states.

Run: PYTHONPATH=src python scripts/verify_kb_sync_ui.py [--headed]
"""
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
sys.path.insert(0, str(REPO_ROOT / "src"))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_command(argv: list[str], *, env: dict[str, str], cwd: Path, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, check=False, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise AssertionError("\n".join(["command failed", " ".join(argv), completed.stdout.strip(), completed.stderr.strip()]).strip())
    return completed


def start_server(argv: list[str], *, env: dict[str, str], cwd: Path, log_path: Path) -> subprocess.Popen[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(argv, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT, text=True)
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


def log_tail(path: Path, limit: int = 4000) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-limit:]


def wait_for_http_ready(url: str, process: subprocess.Popen[str], log_path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("server exited before becoming ready\n" + log_tail(log_path))
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status < 500:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise AssertionError(f"server did not become ready at {url}: {last_error}\n{log_tail(log_path)}")


def smoke_env(home: Path, tmp_root: Path) -> dict[str, str]:
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


def created_run_id(output: str) -> str:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Created run:"):
            return stripped.split(":", 1)[1].strip()
    raise AssertionError(f"could not parse run id from plan output: {output}")


def create_conflict(aha_home: Path, env: dict[str, str], cwd: Path) -> None:
    """Configure a KB with a git remote, seed an entry, then diverge local vs remote."""
    code = """
import json, subprocess, sys
from pathlib import Path
from aha_cli.domain.models import default_knowledge_config
from aha_cli.store.io import write_json
from aha_cli.store.paths import config_path
from aha_cli.store.knowledge import init_knowledge_base, knowledge_root, write_entry
from aha_cli.services import knowledge_git as kg

home = Path(sys.argv[1])
remote = Path(sys.argv[2])
subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
kb = default_knowledge_config()
kb["enabled"] = True
kb["git"].update({"enabled": True, "remote": str(remote), "auto_push": True})
kb["sync"]["resolve_conflicts"] = "agent"
cfg = {"knowledge": kb}
write_json(config_path(home), cfg)
init_knowledge_base(home, cfg)

def w(scope, kind, title, body, proj=None, meta=None, slug=None):
    write_entry(home, config=cfg, scope=scope, kind=kind, title=title,
                body=body, project_key_value=proj, meta=meta, slug=slug)

w("general", "wiki", "Concept", "ORIGINAL BODY\\n")
kg.commit_all(home, "seed", cfg)
kg.push(home, cfg)

# Remote diverges with an agent-distilled edit on the same entry.
other = home.parent / "other"
subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True)
entry = other / "general" / "wiki" / "concept.md"
entry.write_text("---\\ndistilled_by: heuristic\\n---\\nAGENT REMOTE EDIT\\n", encoding="utf-8")
subprocess.run(["git", "-C", str(other), "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], check=True)
subprocess.run(["git", "-C", str(other), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "remote agent edit"], check=True, capture_output=True)
subprocess.run(["git", "-C", str(other), "push", "origin", "main"], check=True, capture_output=True)

# Local diverges with a user edit on the same entry.
repo = knowledge_root(home, cfg)
local = repo / "general" / "wiki" / "concept.md"
local.write_text("USER LOCAL EDIT\\n", encoding="utf-8")
kg.commit_all(home, "local user edit", cfg)

# Agent-mode sync leaves the rebase in progress with a conflict.
result = kg.sync(home, cfg, message="manual sync")
assert result.get("conflict"), f"expected conflict, got {result}"
print("conflict-created")
"""
    run_command([sys.executable, "-c", code, str(aha_home), str(aha_home.parent / "remote.git")], env=env, cwd=cwd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the KB sync conflict UI in a real browser")
    parser.add_argument("--screenshots-dir", type=Path, default=REPO_ROOT / "graph-shots")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    args.screenshots_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        with tempfile.TemporaryDirectory(prefix="aha-sync-verify-") as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            workspace = tmp_path / "workspace"
            tmp_root = tmp_path / "tmp"
            aha_home = workspace / ".aha"
            home.mkdir(parents=True)
            workspace.mkdir(parents=True)
            tmp_root.mkdir(parents=True)
            env = smoke_env(home, tmp_root)

            run_command([sys.executable, "-m", "aha_cli", "--home", str(aha_home), "init", "--force"], env=env, cwd=workspace)
            plan = run_command(
                [sys.executable, "-m", "aha_cli", "--home", str(aha_home), "plan", "SYNC-VERIFY",
                 "--agents", "1", "--task", "SYNC-VERIFY primary"],
                env=env, cwd=workspace,
            )
            run_id = created_run_id(plan.stdout)
            create_conflict(aha_home, env, workspace)

            port = free_port()
            server_log = tmp_path / "aha-ui.log"
            server = start_server(
                [sys.executable, "-m", "aha_cli", "--home", str(aha_home), "ui", run_id,
                 "--host", "127.0.0.1", "--port", str(port)],
                env=env, cwd=workspace, log_path=server_log,
            )
            console_errors: list[str] = []
            screenshots: list[str] = []
            try:
                base_url = f"http://127.0.0.1:{port}"
                wait_for_http_ready(f"{base_url}/api/health", server, server_log)
                context = browser.new_context(viewport={"width": 1400, "height": 900})
                page = context.new_page()
                page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
                page.on("pageerror", lambda exc: console_errors.append(str(exc)))

                url = f"{base_url}/static/knowledge.html?run_id={run_id}"
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector("#kb-sync-status", state="visible", timeout=10000)
                page.wait_for_timeout(1200)

                # 1. Conflict state: status text mentions conflict, resolve button visible.
                status_text = page.locator("#kb-sync-status").inner_text(timeout=8000).strip()
                resolve_visible = page.locator("#kb-resolve").is_visible()
                print(json.dumps({"conflict_status": status_text, "resolve_visible": resolve_visible}, ensure_ascii=False))
                require("conflict" in status_text.lower(), f"sync status did not report conflict: {status_text!r}")
                require(resolve_visible, "resolve button should be visible during a conflict")

                path = args.screenshots_dir / "kb-sync-conflict.png"
                page.screenshot(path=str(path))
                screenshots.append(str(path))

                # 2. Click resolve and poll the maintenance record to completion.
                page.locator("#kb-resolve").click()
                page.wait_for_timeout(1500)
                deadline = time.monotonic() + 180
                resolved = None
                while time.monotonic() < deadline:
                    status = page.evaluate("() => fetch('/api/kb/sync-status').then(r => r.json())")
                    maintenance = status.get("maintenance") or {}
                    if maintenance.get("status") == "resolved":
                        resolved = maintenance
                        break
                    if maintenance.get("status") == "failed":
                        raise AssertionError(f"maintenance failed: {maintenance.get('error')}")
                    page.wait_for_timeout(2500)
                require(resolved is not None, "maintenance did not resolve within 180s")
                print(json.dumps({"maintenance": {"status": resolved["status"], "summary": resolved.get("summary")}}, ensure_ascii=False))

                # 3. Repo is clean, and the page's own poll has reflected it.
                repo_state = page.evaluate("() => fetch('/api/kb/sync-status').then(r => r.json())")
                require(repo_state.get("state") != "conflict", f"repo still in conflict: {repo_state.get('state')}")
                page.wait_for_function(
                    "() => !document.querySelector('#kb-sync-status')?.textContent.includes('Resolving')"
                    " && !document.querySelector('#kb-sync-status')?.textContent.includes('Checking')",
                    timeout=20000,
                )
                page.wait_for_timeout(300)
                resolved_text = page.locator("#kb-sync-status").inner_text(timeout=8000).strip()
                print(json.dumps({"resolved_status": resolved_text}, ensure_ascii=False))
                require("resolved" in resolved_text.lower() or "local change" in resolved_text.lower() or "up to date" in resolved_text.lower(),
                        f"status line did not reflect resolution: {resolved_text!r}")
                path = args.screenshots_dir / "kb-sync-resolved.png"
                page.screenshot(path=str(path))
                screenshots.append(str(path))
            finally:
                stop_server(server)

        require(not console_errors, "console errors:\n" + "\n".join(console_errors))
        print(json.dumps({"ok": True, "screenshots": screenshots}, ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
