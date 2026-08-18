#!/usr/bin/env python3
"""Verify the KB attachment upload + Web detail rendering in a real browser.

Seeds a temp AHA home with an entry carrying a text attachment, a PDF, and an
mp4 (local-only), starts the UI server, then drives Chromium to the knowledge
console entry detail and asserts the attachment area renders:
- text -> <pre> filled via /api/kb/attachment
- pdf -> <embed>
- mp4 -> <video controls>

Run: PYTHONPATH=src python scripts/verify_kb_attachments.py [--headed]
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


def seed_attachments(aha_home: Path, env: dict[str, str], cwd: Path) -> None:
    """Create an entry and attach text + pdf + mp4 (media local-only)."""
    code = r"""
import sys
from pathlib import Path
from aha_cli.domain.models import default_knowledge_config
from aha_cli.store.io import write_json
from aha_cli.store.paths import config_path
from aha_cli.store.knowledge import init_knowledge_base, write_entry
from aha_cli.store.knowledge_assets import add_entry_attachment

home = Path(sys.argv[1])
kb = default_knowledge_config()
kb["enabled"] = True
cfg = {"knowledge": kb}
write_json(config_path(home), cfg)
init_knowledge_base(home, cfg)
write_entry(home, config=cfg, scope="project", kind="solutions", project_key_value="git-abc",
            title="Attachment demo", body="## 结论\n附件展示测试。\n", slug="attachment-demo",
            meta={"type": "solution"})

def add(name, data):
    add_entry_attachment(home, cfg, "attachment-demo", data=data, filename=name)

add("notes.txt", "这是一个文本附件。\nsecond line\n".encode("utf-8"))
add("manual.pdf", b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF")
add("demo.mp4", b"FAKE-MP4-BYTES-FOR-BROWSER-TEST")
print("seeded")
"""
    run_command([sys.executable, "-c", code, str(aha_home)], env=env, cwd=cwd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the KB attachment upload + Web detail rendering")
    parser.add_argument("--screenshots-dir", type=Path, default=REPO_ROOT / "graph-shots")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    args.screenshots_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        with tempfile.TemporaryDirectory(prefix="aha-attach-verify-") as tmp:
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
                [sys.executable, "-m", "aha_cli", "--home", str(aha_home), "plan", "ATTACH-VERIFY",
                 "--agents", "1", "--task", "ATTACH-VERIFY primary"],
                env=env, cwd=workspace,
            )
            run_id = created_run_id(plan.stdout)
            seed_attachments(aha_home, env, workspace)

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
                # Open the entry detail directly via its slug.
                page.evaluate("() => openEntryReference('attachment-demo')")
                page.wait_for_selector("#entry-ref-modal", state="visible", timeout=10000)
                page.wait_for_timeout(1500)

                # Text attachment loaded into <pre>.
                text_pre = page.locator(".kb-attachment-text").first
                text_pre.wait_for(state="visible", timeout=8000)
                text_content = text_pre.inner_text(timeout=8000)
                print(json.dumps({"text_preview": text_content}, ensure_ascii=False))
                require("文本附件" in text_content, f"text attachment not rendered: {text_content!r}")

                # PDF embed present.
                pdf_embed = page.locator(".kb-attachment-embed")
                require(pdf_embed.count() == 1, "pdf embed missing")

                # Video element present with controls.
                video = page.locator(".kb-attachment-video")
                require(video.count() == 1, "video element missing")
                video_controls = video.evaluate("(el) => el.controls")
                require(video_controls is True, "video should have controls")

                # Download links for office/other attachments.
                download_links = page.locator('.kb-attachment-head a[download]')
                require(download_links.count() >= 3, "download links missing")

                path = args.screenshots_dir / "kb-attachments-detail.png"
                page.screenshot(path=str(path), full_page=True)
                screenshots.append(str(path))

                # Fetch one attachment via API directly to confirm serve + disposition.
                api_result = page.evaluate(
                    "() => fetch('/api/kb/attachment?id=attachment-demo&path=assets/attachment-demo/notes.txt').then(async r => ({ status: r.status, cd: r.headers.get('Content-Disposition'), ct: r.headers.get('Content-Type') }))"
                )
                print(json.dumps({"attachment_api": api_result}, ensure_ascii=False))
                require(api_result["status"] == 200, "attachment API failed")
                require("inline" in (api_result["cd"] or ""), "text should be inline")
            finally:
                stop_server(server)

        require(not console_errors, "console errors:\n" + "\n".join(console_errors))
        print(json.dumps({"ok": True, "screenshots": screenshots}, ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
