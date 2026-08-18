#!/usr/bin/env python3
"""Verify the skill CLI -> Web Skills tab flow in a real browser.

Seeds a temp AHA home, creates a personal skill via the `aha skill` CLI and a
system skill via frontmatter, starts the UI server, then drives Chromium to the
knowledge console Skills tab:

1. asserts the personal skill is visible with a "personal" source badge,
2. opens it and edits the SKILL.md through the Web editor (personal editable),
3. asserts the system skill is shown read-only (no delete button, textareas
   readonly),
4. captures screenshots.

Run: PYTHONPATH=src python scripts/verify_kb_skills.py [--headed]
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


def seed_skills(aha_home: Path, env: dict[str, str], cwd: Path) -> None:
    """Create one personal skill via CLI and one system skill via direct write."""
    run_command(
        [sys.executable, "-m", "aha_cli", "--home", str(aha_home), "skill", "create", "board-debug",
         "--title", "Board Debug", "--description", "UART board debugging workflow"],
        env=env, cwd=cwd,
    )
    # System skill: write directly into knowledge/skills with source: system.
    code = """
from pathlib import Path
import sys
home = Path(sys.argv[1])
d = home / "knowledge" / "skills" / "aha-hardware-debug"
d.mkdir(parents=True, exist_ok=True)
(d / "SKILL.md").write_text("---\\nname: aha-hardware-debug\\ndescription: system skill\\nsource: system\\n---\\n\\n# AHA Hardware Debug\\n", encoding="utf-8")
print("system seeded")
"""
    run_command([sys.executable, "-c", code, str(aha_home)], env=env, cwd=cwd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the skill CLI -> Web Skills tab flow")
    parser.add_argument("--screenshots-dir", type=Path, default=REPO_ROOT / "graph-shots")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    args.screenshots_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        with tempfile.TemporaryDirectory(prefix="aha-skills-verify-") as tmp:
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
                [sys.executable, "-m", "aha_cli", "--home", str(aha_home), "plan", "SKILLS-VERIFY",
                 "--agents", "1", "--task", "SKILLS-VERIFY primary"],
                env=env, cwd=workspace,
            )
            run_id = created_run_id(plan.stdout)
            seed_skills(aha_home, env, workspace)

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
                page.wait_for_selector("nav.kb-tabs", state="visible", timeout=10000)
                page.locator('nav.kb-tabs button[data-tab="skills"]').click()
                page.wait_for_selector("#skills-list", state="visible", timeout=10000)
                page.wait_for_timeout(1200)

                # 1. Both skills visible; personal badge present.
                list_text = page.locator("#skills-list").inner_text(timeout=8000)
                require("Board Debug" in list_text, f"board-debug missing from list: {list_text}")
                require("AHA Hardware Debug" in list_text, f"system skill missing from list: {list_text}")
                require("个人" in list_text or "personal" in list_text.lower(), f"personal badge missing: {list_text}")
                require("系统" in list_text or "system" in list_text.lower(), f"system badge missing: {list_text}")
                print(json.dumps({"skills_list": list_text.replace(chr(10), " | "), "step": "list"}, ensure_ascii=False))

                # 2. Open the system skill -> read-only editor.
                page.locator('[data-skill-id="aha-hardware-debug"]').click()
                page.wait_for_timeout(800)
                md_ro = page.evaluate("document.querySelector('#skill-md')?.readOnly")
                delete_btn = page.locator("#skill-delete").count()
                print(json.dumps({"system_readonly": md_ro, "system_delete_buttons": delete_btn}, ensure_ascii=False))
                require(md_ro is True, "system skill textarea should be readonly")
                require(delete_btn == 0, "system skill should not show a delete button")
                path = args.screenshots_dir / "kb-skills-system-readonly.png"
                page.screenshot(path=str(path))
                screenshots.append(str(path))

                # 3. Open the personal skill -> editable, then edit via the Web UI.
                page.locator('[data-skill-id="board-debug"]').click()
                page.wait_for_timeout(800)
                md_ro = page.evaluate("document.querySelector('#skill-md')?.readOnly")
                print(json.dumps({"personal_readonly": md_ro}, ensure_ascii=False))
                require(md_ro is False, "personal skill textarea should be editable")
                page.evaluate(
                    """() => {
                        const ta = document.querySelector('#skill-md');
                        ta.value = ta.value + '\\n## 工作流程\\n- Web edit verified\\n';
                    }"""
                )
                page.locator('#skills-form button[type="submit"]').click()
                page.wait_for_timeout(1200)
                toast_text = page.locator("#toast").inner_text(timeout=5000)
                print(json.dumps({"save_toast": toast_text}, ensure_ascii=False))
                # Verify the CLI sees the Web-edited body.
                env_copy = dict(env)
                show = run_command(
                    [sys.executable, "-m", "aha_cli", "--home", str(aha_home), "skill", "show", "board-debug", "--json"],
                    env=env_copy, cwd=workspace,
                )
                shown = json.loads(show.stdout)
                require("Web edit verified" in str(shown.get("skill_md") or ""), "Web edit not persisted to disk")
                path = args.screenshots_dir / "kb-skills-personal-edit.png"
                page.screenshot(path=str(path))
                screenshots.append(str(path))
            finally:
                stop_server(server)

        require(not console_errors, "console errors:\n" + "\n".join(console_errors))
        print(json.dumps({"ok": True, "screenshots": screenshots}, ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
