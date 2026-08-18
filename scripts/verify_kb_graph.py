#!/usr/bin/env python3
"""Open the AHA knowledge console Graph tab in a real browser and screenshot it.

Seeds a temp AHA home with several KB entries connected by Obsidian wikilinks,
starts the UI server, drives Chromium (Playwright) to the Graph tab, then
verifies the force-directed canvas actually rendered nodes and reports console
errors. Writes screenshots to --screenshots-dir (default: ./graph-shots).
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


def run_command(argv: list[str], *, env: dict[str, str], cwd: Path, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, check=False, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise AssertionError(
            "\n".join(["command failed", " ".join(argv), completed.stdout.strip(), completed.stderr.strip()]).strip()
        )
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


def seed_knowledge(aha_home: Path, env: dict[str, str], cwd: Path) -> None:
    """Enable the KB and write entries connected by Obsidian wikilinks."""
    code = """
import json, sys
from pathlib import Path
from aha_cli.domain.models import default_knowledge_config
from aha_cli.store.config import load_config
from aha_cli.store.io import write_json
from aha_cli.store.paths import config_path
from aha_cli.store.knowledge import init_knowledge_base, write_entry

home = Path(sys.argv[1])
kb = default_knowledge_config()
kb["enabled"] = True
kb.setdefault("curation", {})["gate"] = "agent-auto"
write_json(config_path(home), {"knowledge": kb})
init_knowledge_base(home, {"knowledge": kb})

def w(scope, kind, title, body, proj=None, meta=None, slug=None):
    write_entry(home, config={"knowledge": kb}, scope=scope, kind=kind, title=title,
                body=body, project_key_value=proj, meta=meta, slug=slug)

# A small connected graph: solutions reference wiki concepts via [[wikilinks]].
w("project", "solutions", "Cross OS", "WSL uses the distro backend see [[wsl-backend]]", proj="git-abc",
  meta={"tags": ["backend"]})
w("project", "solutions", "WSL backend", "in distro, paired with [[windows-web]]", proj="git-abc",
  meta={"tags": ["wsl"]})
w("project", "solutions", "Windows web", "Web service runs on Windows, resolves [[wsl-backend]] pid", proj="git-abc")
w("project", "navigation", "Project index", "## Project\\nlinks: [[cross-os]]", proj="git-abc",
  meta={"type": "navigation", "navigation_role": "index"}, slug="index")
w("project", "navigation", "WSL backend decision", "## Decisions\\nBackend runs in distro",
  proj="git-abc", meta={"type": "navigation", "navigation_role": "knowledge_decisions", "distilled_by": "sidecar"},
  slug="knowledge/decisions/use-wsl-backend")
w("project", "navigation", "WSL pitfalls", "## Pitfalls\\npython3 shim trap",
  proj="git-abc", meta={"type": "navigation", "navigation_role": "knowledge_pitfalls", "distilled_by": "sidecar"},
  slug="knowledge/pitfalls/wsl-python3-shim")
w("general", "wiki", "AHA orchestration", "agents coordinate via [[aha-run]] and [[wsl-backend]]")
w("general", "wiki", "AHA run", "a run owns agents, see [[aha-orchestration]]")
w("personal", "wiki", "WSL notes", "remember the python3 shim trap from [[wsl-backend]]")
print("seeded")
"""
    run_command(
        [sys.executable, "-c", code, str(aha_home)],
        env=env,
        cwd=cwd,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the KB Graph tab renders in a real browser")
    parser.add_argument("--screenshots-dir", type=Path, default=REPO_ROOT / "graph-shots")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    args.screenshots_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        with tempfile.TemporaryDirectory(prefix="aha-graph-verify-") as tmp:
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
                [sys.executable, "-m", "aha_cli", "--home", str(aha_home), "plan", "GRAPH-VERIFY",
                 "--agents", "1", "--task", "GRAPH-VERIFY primary"],
                env=env, cwd=workspace,
            )
            run_id = created_run_id(plan.stdout)
            seed_knowledge(aha_home, env, workspace)

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
                # Switch to the Graph tab.
                page.locator('nav.kb-tabs button[data-tab="graph"]').click()
                page.wait_for_selector("#graph-canvas", state="visible", timeout=10000)
                # Wait for the graph to settle a bit so nodes are laid out.
                page.wait_for_timeout(1800)

                # Confirm the status line reports entries (proves /api/kb/graph returned data).
                status = page.locator("#graph-status").inner_text(timeout=8000).strip()
                require("entries" in status or "truncated" in status or status.replace(" ", "").isdigit(),
                        f"graph status line did not report entries: {status!r}")

                # Confirm the canvas has painted non-blank pixels (nodes drawn).
                painted = page.evaluate(
                    """() => {
                        const c = document.querySelector('#graph-canvas');
                        if (!c) return false;
                        const ctx = c.getContext('2d');
                        const data = ctx.getImageData(0, 0, c.width, c.height).data;
                        let colored = 0;
                        for (let i = 0; i < data.length; i += 4) {
                            const r = data[i], g = data[i+1], b = data[i+2], a = data[i+3];
                            if (a > 0 && !(r === 0 && g === 0 && b === 0)) colored++;
                        }
                        return { colored, total: data.length / 4 };
                    }"""
                )
                require(painted["colored"] > 0, f"canvas is blank (colored pixels={painted['colored']})")
                print(json.dumps({"canvas_painted": painted, "graph_status": status}, ensure_ascii=False, indent=2))

                # Verify the graph data came from the API with expected edges.
                payload = page.evaluate(
                    "() => fetch('/api/kb/graph?max_nodes=400').then(r => r.json())"
                )
                node_ids = {n["id"] for n in payload["nodes"]}
                edge_types = {(l["source"], l["target"], l["type"]) for l in payload["links"]}
                require("cross-os" in node_ids and "wsl-backend" in node_ids, "expected seeded nodes missing")
                require(any(t == "wikilink" for _, _, t in edge_types), "no wikilink edges found")

                for name in ["graph-tab", "graph-hover"]:
                    path = args.screenshots_dir / f"kb-graph-{name}.png"
                    page.screenshot(path=str(path), full_page=False)
                    screenshots.append(str(path))

                # Hover a node to exercise highlight path (picks the largest node).
                hover_worked = page.evaluate(
                    """() => {
                        const c = document.querySelector('#graph-canvas');
                        const rect = c.getBoundingClientRect();
                        // Pick a node position from the internal state.
                        const g = window.__kbGraphState;
                        if (!g) return false;
                        let best = null, bestR = -1;
                        for (const n of g.nodes) {
                            const p = g.positions.get(n.id);
                            const r = n.backlink_count ? 6 + Number(n.backlink_count) * 1.6 : 6;
                            if (p && r > bestR) { bestR = r; best = p; }
                        }
                        if (!best) return false;
                        const x = rect.left + best.x * g.scale + g.panX;
                        const y = rect.top + best.y * g.scale + g.panY;
                        c.dispatchEvent(new PointerEvent('pointermove', { clientX: x, clientY: y, bubbles: true }));
                        return g.hover >= 0;
                    }"""
                )
                if hover_worked:
                    page.wait_for_timeout(300)
                    path = args.screenshots_dir / "kb-graph-hover.png"
                    page.screenshot(path=str(path), full_page=False)
                    screenshots.append(str(path))
            finally:
                stop_server(server)

        require(not console_errors, "console errors:\n" + "\n".join(console_errors))
        print(json.dumps({"ok": True, "screenshots": screenshots}, ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
