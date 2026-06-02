(() => {
  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function selectOptions(options = [], current = "") {
    return options.map(option => {
      const selected = option === current ? "selected" : "";
      return `<option value="${escapeHtml(option)}" ${selected}>${escapeHtml(option)}</option>`;
    }).join("");
  }

  function confirmDialogFallbackText(options = {}) {
    const lines = [options.title || "Confirm action?", options.message || ""];
    for (const row of Array.isArray(options.details) ? options.details : []) {
      const label = String(row?.label ?? row?.[0] ?? "").trim();
      const value = String(row?.value ?? row?.[1] ?? "").trim();
      if (label || value) lines.push(`${label || "Detail"}: ${value || "-"}`);
    }
    return lines.filter(Boolean).join("\n");
  }

  function confirmDialogChoice(options = {}) {
    if (window.AHAConfirmDialog?.confirmChoice) return window.AHAConfirmDialog.confirmChoice(options);
    if (typeof options.fallback === "function") return Promise.resolve(options.fallback());
    const confirmValue = options.confirmValue || "confirm";
    const cancelValue = options.cancelValue || "cancel";
    return Promise.resolve(window.confirm(confirmDialogFallbackText(options)) ? confirmValue : cancelValue);
  }

  async function confirmDialogAction(options = {}) {
    return await confirmDialogChoice(options) === (options.confirmValue || "confirm");
  }

  function createPanelUiHelpers(elements = {}, deps = {}) {
    const panelEl = elements.panelEl;
    const copyTextByKey = deps.copyTextByKey || new Map();
    const documentRef = deps.documentRef || document;
    const navigatorRef = deps.navigatorRef || navigator;
    const windowRef = deps.windowRef || window;

    function nodeInsidePanel(node) {
      if (!node || !panelEl) return false;
      const element = node instanceof Element ? node : node.parentElement;
      return Boolean(element && panelEl.contains(element));
    }

    function panelHasTextSelection() {
      const selection = windowRef.getSelection?.();
      if (!selection || selection.isCollapsed || !selection.toString().trim()) return false;
      return nodeInsidePanel(selection.anchorNode) || nodeInsidePanel(selection.focusNode);
    }

    function fallbackCopyText(text) {
      const textarea = documentRef.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.top = "-9999px";
      textarea.style.left = "-9999px";
      documentRef.body.appendChild(textarea);
      textarea.select();
      try {
        return documentRef.execCommand("copy");
      } finally {
        textarea.remove();
      }
    }

    async function copyTimelineMessage(button) {
      const key = button.dataset.copyMessageKey || "";
      const text = copyTextByKey.get(key) || "";
      if (!text) return;
      const originalLabel = button.getAttribute("aria-label") || "Copy message";
      const label = button.querySelector(".message-copy-label");
      const setState = (state, textLabel) => {
        button.dataset.copyState = state;
        button.setAttribute("aria-label", textLabel);
        button.title = textLabel;
        if (label) label.textContent = textLabel;
      };
      button.disabled = true;
      setState("copying", "Copying");
      try {
        if (navigatorRef.clipboard?.writeText) {
          await navigatorRef.clipboard.writeText(text);
        } else if (!fallbackCopyText(text)) {
          throw new Error("copy failed");
        }
        setState("copied", "Copied");
      } catch (_err) {
        const copied = fallbackCopyText(text);
        setState(copied ? "copied" : "failed", copied ? "Copied" : "Copy failed");
      } finally {
        windowRef.setTimeout(() => {
          button.disabled = false;
          setState("idle", originalLabel);
        }, 1200);
      }
    }

    return Object.freeze({
      copyTimelineMessage,
      panelHasTextSelection
    });
  }

  window.AHAAppHelpers = Object.freeze({
    confirmDialogAction,
    confirmDialogChoice,
    confirmDialogFallbackText,
    createPanelUiHelpers,
    escapeHtml,
    selectOptions
  });
})();
