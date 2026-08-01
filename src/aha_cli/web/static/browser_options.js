(function () {
  "use strict";

  // Filter the shared-browser picker to only the browsers actually installed on
  // the AHA host (detected via GET /api/browser/options), instead of a fixed list.
  // If nothing is detected, prompt to install rather than offering unusable options.
  function applyBrowserChannelOptions(data) {
    const anyAvailable = Boolean(data.chrome || data.msedge || data.chromium);
    const hint = "未检测到浏览器 — 请安装 Chrome 或 Edge（No browser detected — install Chrome or Edge）";
    document.querySelectorAll("[data-browser-mode]").forEach(select => {
      if (!select) return;
      const existingHint = select.querySelector('option[data-browser-missing]');
      if (anyAvailable) {
        if (existingHint) existingHint.remove();
        select.querySelectorAll("option").forEach(opt => {
          const value = String(opt.value || "");
          // "off" is always available; chrome/msedge/chromium depend on detection.
          if (value === "off") return;
          opt.hidden = !data[value];
        });
      } else {
        // Nothing installed: hide every browser option (keep "off") and show a
        // disabled install hint instead of a list that can't actually launch.
        select.querySelectorAll("option").forEach(opt => {
          opt.hidden = String(opt.value || "") !== "off";
        });
        if (!existingHint) {
          const option = document.createElement("option");
          option.value = "";
          option.disabled = true;
          option.setAttribute("data-browser-missing", "1");
          option.textContent = hint;
          select.appendChild(option);
        }
      }
    });
  }

  async function load() {
    try {
      const res = await fetch("/api/browser/options", { credentials: "include" });
      if (!res.ok) return;
      const data = await res.json();
      if (data && typeof data === "object") applyBrowserChannelOptions(data);
    } catch (_error) {
      // Detection unavailable: leave the full default list in place.
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load, { once: true });
  } else {
    load();
  }
})();
