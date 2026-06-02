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


def skipped(reason: str) -> dict:
    return {"status": "skipped", "reason": reason, "checks": []}


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
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT / "src") if not existing else f"{REPO_ROOT / 'src'}{os.pathsep}{existing}"
    return env


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


def log_tail(path: Path, limit: int = 4000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-limit:]


def wait_for_http_ready(url: str, process: subprocess.Popen[str], log_path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                "\n".join(
                    [
                        f"server exited before becoming ready ({process.returncode})",
                        log_tail(log_path),
                    ]
                ).strip()
            )
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status < 500:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise AssertionError(
        "\n".join(
            [
                f"server did not become ready at {url}: {last_error}",
                log_tail(log_path),
            ]
        ).strip()
    )


def delete_run(aha_home: Path, run_id: str, env: dict[str, str], cwd: Path) -> bool:
    result = run_command(
        [
            sys.executable,
            "-m",
            "aha_cli",
            "--home",
            str(aha_home),
            "runs",
            "delete",
            run_id,
            "--force",
            "--json",
        ],
        env=env,
        cwd=cwd,
    )
    payload = json.loads(result.stdout)
    return bool(payload.get("ok"))


def run_smoke(*, require_browser: bool = False, headed: bool = False) -> dict:
    if os.environ.get("AHA_PLAYWRIGHT_SKIP") == "1":
        return skipped("AHA_PLAYWRIGHT_SKIP=1")

    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError as exc:
        if require_browser:
            raise AssertionError("python Playwright is not installed") from exc
        return skipped("python Playwright is not installed")

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=not headed)
        except Exception as exc:  # noqa: BLE001
            if require_browser:
                raise AssertionError(f"Playwright Chromium could not launch: {exc}") from exc
            return skipped(f"Playwright Chromium could not launch: {exc}")
        try:
            with tempfile.TemporaryDirectory(prefix="aha-playwright-smoke-") as tmp:
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
                    [
                        sys.executable,
                        "-m",
                        "aha_cli",
                        "--home",
                        str(aha_home),
                        "plan",
                        "PLAYWRIGHT-SMOKE",
                        "--agents",
                        "1",
                    ],
                    env=env,
                    cwd=workspace,
                )
                run_id = created_run_id(plan.stdout)
                token = "playwright-smoke-token"
                token_file = aha_home / "web-token"
                token_file.write_text(f"{token}\n", encoding="utf-8")
                port = free_port()
                server_log = tmp_path / "aha-ui.log"
                server = start_server(
                    [
                        sys.executable,
                        "-m",
                        "aha_cli",
                        "--home",
                        str(aha_home),
                        "ui",
                        run_id,
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--auth-token-file",
                        str(token_file),
                    ],
                    env=env,
                    cwd=workspace,
                    log_path=server_log,
                )
                console_errors: list[str] = []
                try:
                    wait_for_http_ready(f"http://127.0.0.1:{port}/api/health", server, server_log)
                    context = browser.new_context(viewport={"width": 1280, "height": 800})
                    try:
                        page = context.new_page()
                        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
                        page.goto(f"http://127.0.0.1:{port}/?run_id={run_id}&token={token}", wait_until="domcontentloaded")
                        page.wait_for_selector("#summary", state="visible", timeout=10000)
                        page.wait_for_function(
                            "document.querySelector('#summary')?.textContent.includes('PLAYWRIGHT-SMOKE')",
                            timeout=10000,
                        )
                        require(page.locator("#run-lifecycle").inner_text(timeout=5000).strip() == "active", "run lifecycle badge did not render active")
                        page.locator("#open-task-create").click()
                        page.wait_for_selector("#task-create-dialog[open]", timeout=5000)
                        page.keyboard.press("Escape")
                        page.locator("#session-toggle").click()
                        page.wait_for_selector("#run-select", state="visible", timeout=5000)
                        require(page.locator("#run-select").input_value(timeout=5000) == run_id, "run selector did not keep the smoke run selected")
                        page.locator('.tab[data-tab="logs"]').click()
                        page.wait_for_function(
                            "document.querySelector('.tab[data-tab=\"logs\"]')?.classList.contains('active')",
                            timeout=5000,
                        )
                        page.locator('.tab[data-tab="conversation"]').click()
                        page.wait_for_function(
                            "document.querySelector('.tab[data-tab=\"conversation\"]')?.classList.contains('active')",
                            timeout=5000,
                        )
                        page.locator('[data-task-visibility-filter="all"]').click()
                        page.wait_for_function(
                            "document.querySelector('[data-task-visibility-filter=\"all\"]')?.classList.contains('active')",
                            timeout=5000,
                        )
                        page.locator('[data-task-visibility-filter="active"]').click()
                        page.wait_for_function(
                            "document.querySelector('[data-task-visibility-filter=\"active\"]')?.classList.contains('active')",
                            timeout=5000,
                        )
                        page.locator('[data-task-id] [data-action="hide"]').first.click()
                        page.wait_for_selector("#action-confirm[open]", timeout=5000)
                        page.locator('#action-confirm button[value="confirm"]').click()
                        page.wait_for_selector(".empty.compact", timeout=10000)
                        page.locator('[data-task-visibility-filter="hidden"]').click()
                        page.wait_for_selector('[data-task-id] [data-action="restore"]', timeout=10000)
                        page.locator('[data-task-id] [data-action="restore"]').first.click()
                        page.wait_for_function(
                            "document.querySelector('[data-task-visibility-filter=\"hidden\"]')?.textContent.includes('0')",
                            timeout=10000,
                        )
                        page.locator('[data-task-visibility-filter="active"]').click()
                        page.wait_for_selector('[data-task-id] [data-action="hide"]', timeout=10000)
                        page.select_option("#agent-target", "main")
                        page.fill("#message", "/aha interrupt")
                        with page.expect_response(lambda response: "/api/send" in response.url and response.status == 200, timeout=10000):
                            page.locator('#send-form button.send').click()
                        page.wait_for_function(
                            "document.querySelector('#message')?.value === ''",
                            timeout=5000,
                        )
                        if not page.locator("#run-maintenance-console").is_visible():
                            page.locator("#session-toggle").click()
                            page.wait_for_selector("#run-maintenance-console", state="visible", timeout=5000)
                        page.locator("#run-maintenance-console").click()
                        page.wait_for_selector("#run-maintenance-popover:not([hidden])", timeout=5000)
                        page.locator("#run-maintenance-refresh").click()
                        page.wait_for_function(
                            "document.querySelector('#run-maintenance-summary')?.textContent.includes('容量')",
                            timeout=10000,
                        )
                        page.locator("#run-maintenance-close").click()
                        page.wait_for_selector("#run-maintenance-popover", state="hidden", timeout=5000)
                        if not page.locator("#weixin-console").is_visible():
                            page.locator("#session-toggle").click()
                            page.wait_for_selector("#weixin-console", state="visible", timeout=5000)
                        page.locator("#weixin-console").click()
                        page.wait_for_selector("#weixin-console-popover:not([hidden])", timeout=5000)
                        require("微信操作台" in page.locator("#weixin-console-popover").inner_text(timeout=5000), "weixin console did not render")
                        page.keyboard.press("Escape")
                        page.wait_for_selector("#weixin-console-popover", state="hidden", timeout=5000)
                        if not page.locator("#play-console").is_visible():
                            page.locator("#session-toggle").click()
                            page.wait_for_selector("#play-console", state="visible", timeout=5000)
                        page.locator("#play-console").click()
                        page.wait_for_selector("#play-console-popover:not([hidden])", timeout=5000)
                        require("玩了个玩" in page.locator("#play-console-popover").inner_text(timeout=5000), "play console did not render")
                        page.keyboard.press("Escape")
                        page.wait_for_selector("#play-console-popover", state="hidden", timeout=5000)
                        page.locator("#task-proxy-editor summary").click()
                        page.fill("#selected-task-http-proxy", "http://127.0.0.1:18080")
                        page.check("#selected-task-proxy-enabled")
                        page.locator('#task-proxy-form button[type="submit"]').click()
                        page.wait_for_function(
                            "document.querySelector('#task-proxy-state')?.textContent.includes('HTTP')",
                            timeout=10000,
                        )
                        page.locator("#task-supervision-editor summary").click()
                        page.select_option("#selected-task-supervision-mode", "assisted_stub")
                        page.fill("#selected-task-supervision-max-rounds", "7")
                        page.locator('#task-supervision-form button[type="submit"]').click()
                        page.wait_for_function(
                            "document.querySelector('#task-supervision-state')?.textContent.includes('assisted')",
                            timeout=10000,
                        )
                        page.locator("#task-context-editor summary").click()
                        page.check("#selected-task-context-auto-compact-enabled")
                        page.fill("#selected-task-context-threshold", "66")
                        page.locator('#task-context-form button[type="submit"]').click()
                        page.wait_for_function(
                            "document.querySelector('#task-context-state')?.textContent.includes('66')",
                            timeout=10000,
                        )
                        page.wait_for_selector("[data-agent-config-editor]", timeout=5000)
                        agent_editor = page.locator("[data-agent-config-editor]").first
                        agent_editor.locator('[data-agent-config-part="sandbox"]').select_option("read-only")
                        agent_editor.locator('[data-agent-config-part="approval"]').select_option("on-request")
                        agent_editor.locator('[data-agent-config-part="proxy_enabled"]').select_option("true")
                        with page.expect_response(lambda response: "/api/agent-config" in response.url and response.status == 200, timeout=10000):
                            agent_editor.locator("[data-agent-config-apply]").click()
                        require(
                            agent_editor.locator('[data-agent-config-part="sandbox"]').input_value(timeout=5000) == "read-only",
                            "agent sandbox config select did not keep the saved value",
                        )
                        require(
                            agent_editor.locator('[data-agent-config-part="approval"]').input_value(timeout=5000) == "on-request",
                            "agent approval config select did not keep the saved value",
                        )
                        require(
                            agent_editor.locator('[data-agent-config-part="proxy_enabled"]').input_value(timeout=5000) == "true",
                            "agent proxy config select did not keep the saved value",
                        )
                        if not page.locator("#aha-settings").is_visible():
                            page.locator("#session-toggle").click()
                            page.wait_for_selector("#aha-settings", state="visible", timeout=5000)
                        page.locator("#aha-settings").click()
                        page.wait_for_selector("#settings-dialog[open] [data-bootstrap-config-form]", timeout=5000)
                        require(
                            page.locator('#settings-dialog [data-bootstrap-config-field="backend"]').input_value(timeout=5000) in {"codex", "claude"},
                            "settings bootstrap backend select did not render",
                        )
                        page.locator('#settings-dialog [data-bootstrap-config-form] button[type="submit"]').click()
                        page.wait_for_selector("#action-confirm[open]", timeout=5000)
                        page.locator('#action-confirm button[value="confirm"]').click()
                        page.wait_for_function(
                            "document.querySelector('#settings-dialog [data-bootstrap-config-state]')?.textContent.includes('Saved')",
                            timeout=10000,
                        )
                        page.keyboard.press("Escape")
                        require(not console_errors, f"browser console errors: {console_errors[:3]}")
                    finally:
                        context.close()
                finally:
                    stop_server(server)

                deleted = delete_run(aha_home, run_id, env, workspace)
                remaining_runs = sorted(path.name for path in (aha_home / "runs").iterdir() if path.is_dir()) if (aha_home / "runs").is_dir() else []
                require(deleted, "smoke run deletion did not report ok")
                require(run_id not in remaining_runs, "smoke run was left behind")
                return {
                    "status": "passed",
                    "aha_home": str(aha_home),
                    "run_id": run_id,
                    "port": port,
                    "checks": [
                        "token login",
                        "bootstrap render",
                        "task-create dialog",
                        "run selector",
                        "tab switching",
                        "task filter switching",
                        "task hide restore action",
                        "agent target switching",
                        "interrupt command",
                        "maintenance refresh",
                        "task proxy config",
                        "task supervision config",
                        "task context config",
                        "agent runtime config",
                        "settings bootstrap form save",
                        "weixin console entry",
                        "play console entry",
                        "console errors",
                        "smoke run cleanup",
                    ],
                    "remaining_runs": remaining_runs,
                }
        finally:
            browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Optionally smoke test the AHA Web UI in a real browser with Playwright.")
    parser.add_argument("--json", action="store_true", help="Print compact JSON only")
    parser.add_argument("--require-playwright", action="store_true", help="Fail instead of skipping when Playwright or Chromium is unavailable")
    parser.add_argument("--headed", action="store_true", help="Run the browser headed when Playwright is available")
    args = parser.parse_args(argv)

    try:
        result = run_smoke(require_browser=args.require_playwright, headed=args.headed)
    except Exception as exc:  # noqa: BLE001
        print(f"Playwright UI smoke failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        if result.get("status") == "skipped":
            print(f"Playwright UI smoke skipped: {result.get('reason')}")
        else:
            print("Playwright UI smoke passed")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
