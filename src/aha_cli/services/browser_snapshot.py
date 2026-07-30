"""DOM snapshot script used by the shared browser bridge."""

from __future__ import annotations

SNAPSHOT_TEXT_LIMIT = 20000
SNAPSHOT_ELEMENT_LIMIT = 500

SNAPSHOT_SCRIPT = r"""
({ textLimit, elementLimit }) => {
  const root = document.documentElement;
  if (!root) return { text: "", elements: [] };
  let sequence = Number(root.getAttribute("data-aha-browser-ref-seq") || "0");
  const selector = [
    "a[href]", "button", "input", "textarea", "select", "summary",
    "[role]", "[contenteditable=true]", "[tabindex]"
  ].join(",");
  const visible = element => {
    const style = window.getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" &&
      box.width > 0 && box.height > 0;
  };
  const inferRole = element => {
    const explicit = element.getAttribute("role");
    if (explicit) return explicit;
    const tag = element.tagName.toLowerCase();
    if (tag === "a") return "link";
    if (tag === "button" || (tag === "input" && ["button", "submit", "reset"].includes(element.type))) return "button";
    if (tag === "input") return ["checkbox", "radio"].includes(element.type) ? element.type : "textbox";
    if (tag === "textarea") return "textbox";
    if (tag === "select") return "combobox";
    if (tag === "summary") return "button";
    return explicit || tag;
  };
  const nameFor = element => {
    const tag = element.tagName.toLowerCase();
    const type = String(element.getAttribute("type") || "").toLowerCase();
    const safeValue = type === "password" ? "" : String(element.value || "");
    return String(
      element.getAttribute("aria-label") ||
      element.getAttribute("alt") ||
      element.getAttribute("title") ||
      element.getAttribute("placeholder") ||
      element.innerText ||
      safeValue ||
      ""
    ).replace(/\s+/g, " ").trim().slice(0, 300);
  };
  const elements = [];
  for (const element of document.querySelectorAll(selector)) {
    if (elements.length >= elementLimit || !visible(element)) continue;
    let ref = element.getAttribute("data-aha-browser-ref");
    if (!ref) {
      sequence += 1;
      ref = `b${sequence}`;
      element.setAttribute("data-aha-browser-ref", ref);
    }
    const box = element.getBoundingClientRect();
    const type = String(element.getAttribute("type") || "").toLowerCase();
    elements.push({
      ref,
      role: inferRole(element),
      name: nameFor(element),
      type,
      disabled: Boolean(element.disabled) || element.getAttribute("aria-disabled") === "true",
      checked: typeof element.checked === "boolean" ? element.checked : undefined,
      value: type === "password" ? "" : String(element.value || "").slice(0, 500),
      box: {
        x: Math.round(box.x),
        y: Math.round(box.y),
        width: Math.round(box.width),
        height: Math.round(box.height)
      }
    });
  }
  root.setAttribute("data-aha-browser-ref-seq", String(sequence));
  const text = String(document.body?.innerText || "").replace(/\u0000/g, "").slice(0, textLimit);
  return { text, elements };
}
"""


__all__ = ["SNAPSHOT_ELEMENT_LIMIT", "SNAPSHOT_SCRIPT", "SNAPSHOT_TEXT_LIMIT"]
