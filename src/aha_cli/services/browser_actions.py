from __future__ import annotations

import re
from urllib.parse import urlparse

from aha_cli.services.browser_runtime import BrowserBridgeError

_REF_RE = re.compile(r"^(?P<revision>[0-9]+):(?P<ref>b[0-9]+)$")
NATIVE_USER_ACTIVITY_SCRIPT = """
(() => {
  if (window.__ahaNativeActivityInstalled) return;
  Object.defineProperty(window, "__ahaNativeActivityInstalled", { value: true });
  for (const type of ["pointerdown", "keydown", "wheel", "touchstart"]) {
    window.addEventListener(type, event => {
      if (event.isTrusted && typeof window.__ahaNativeUserActivity === "function") {
        void window.__ahaNativeUserActivity(type);
      }
    }, { capture: true, passive: true });
  }
})();
"""


def validated_browser_ref(value: object, revision: int) -> str:
    match = _REF_RE.fullmatch(str(value or "").strip())
    if not match:
        raise BrowserBridgeError(
            "invalid_ref",
            "Element ref must come from the latest browser snapshot.",
        )
    ref_revision = int(match.group("revision"))
    if ref_revision != revision:
        raise BrowserBridgeError(
            "stale_ref",
            f"Element ref is stale (snapshot revision {ref_revision}, current revision {revision}).",
        )
    return match.group("ref")


def browser_url_allowed(url: str, config: dict) -> bool:
    parsed = urlparse(str(url or ""))
    if parsed.scheme == "about" and url == "about:blank":
        return True
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    allowed = config.get("allowed_hosts") or []
    if not allowed:
        return True
    hostname = parsed.hostname.lower()
    for item in allowed:
        pattern = str(item or "").lower()
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if hostname.endswith(suffix) and hostname != suffix[1:]:
                return True
        elif hostname == pattern:
            return True
    return False


async def browser_tabs(pages: dict[str, object], active_page_id: str) -> list[dict]:
    tabs: list[dict] = []
    for page_id, page in list(pages.items()):
        try:
            title = await page.title()
        except Exception:
            title = ""
        tabs.append(
            {
                "page_id": page_id,
                "title": title[:500],
                "url": str(page.url or "")[:2048],
                "active": page_id == active_page_id,
            }
        )
    return tabs


async def browser_mouse_action(
    page,
    args: dict,
    *,
    viewport_width: int,
    viewport_height: int,
) -> None:
    event_type = str(args.get("event") or "click").strip().lower()
    x = max(0.0, min(float(args.get("x") or 0), float(viewport_width)))
    y = max(0.0, min(float(args.get("y") or 0), float(viewport_height)))
    button = str(args.get("button") or "left")
    if button not in {"left", "middle", "right"}:
        button = "left"
    if event_type == "move":
        await page.mouse.move(x, y)
    elif event_type == "down":
        await page.mouse.move(x, y)
        await page.mouse.down(button=button)
    elif event_type == "up":
        await page.mouse.move(x, y)
        await page.mouse.up(button=button)
    elif event_type == "wheel":
        await page.mouse.move(x, y)
        await page.mouse.wheel(
            float(args.get("delta_x") or 0),
            float(args.get("delta_y") or 0),
        )
    else:
        await page.mouse.click(
            x,
            y,
            button=button,
            click_count=max(1, min(int(args.get("click_count") or 1), 3)),
        )


async def browser_page_accepts_text_input(page) -> bool:
    script = """
    () => {
      const element = document.activeElement;
      if (!element) return false;
      if (element.isContentEditable) return true;
      const tag = String(element.tagName || "").toLowerCase();
      if (tag === "textarea") return !element.disabled && !element.readOnly;
      if (tag !== "input" || element.disabled || element.readOnly) return false;
      return !new Set([
        "button", "checkbox", "color", "file", "hidden", "image", "radio",
        "range", "reset", "submit"
      ]).has(String(element.type || "text").toLowerCase());
    }
    """
    frames = list(getattr(page, "frames", []) or [])
    if not frames:
        frames = [page]
    for frame in frames:
        try:
            if await frame.evaluate(script):
                return True
        except Exception:
            continue
    return False


__all__ = [
    "NATIVE_USER_ACTIVITY_SCRIPT",
    "browser_mouse_action",
    "browser_page_accepts_text_input",
    "browser_tabs",
    "browser_url_allowed",
    "validated_browser_ref",
]
