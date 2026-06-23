(() => {
  function t(key, fallback = "") {
    return window.AHAI18n?.t?.(key, fallback) || fallback;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function numberText(value) {
    const number = Number(value || 0);
    if (!Number.isFinite(number)) return "0";
    return new Intl.NumberFormat("en-US").format(number);
  }

  function costText(value) {
    const number = Number(value || 0);
    if (!Number.isFinite(number) || number <= 0) return "-";
    return `$${number.toFixed(number < 0.01 ? 6 : 4)}`;
  }

  function currentRunId() {
    const text = String(document.getElementById("run-id")?.textContent || "").trim();
    return text && text !== "-" ? text : "";
  }

  function localTimezone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch (_err) {
      return "UTC";
    }
  }

  function usageSummaryCells(usage = {}) {
    return [
      ["token_usage.billable_input", "Billable input", usage.billable_input_tokens],
      ["token_usage.cache_read", "Cache read", usage.cache_read_tokens],
      ["token_usage.cache_creation", "Cache create", usage.cache_creation_tokens],
      ["token_usage.output", "Output", usage.output_tokens],
      ["token_usage.reasoning", "Reasoning", usage.reasoning_output_tokens],
      ["token_usage.total_tokens", "Total", usage.total_tokens]
    ].map(([key, label, value]) => (
      `<div class="token-usage-metric"><span>${escapeHtml(t(key, label))}</span><strong>${escapeHtml(numberText(value))}</strong></div>`
    )).join("");
  }

  function backendLabel(item = {}) {
    return [item.backend || "unknown", item.model || ""].filter(Boolean).join(" / ");
  }

  function backendBreakdownHtml(day = {}) {
    const rows = Array.isArray(day.by_backend) ? day.by_backend : [];
    if (!rows.length) return "";
    return `<div class="token-usage-breakdown">${
      rows.slice(0, 4).map(item => (
        `<span title="${escapeHtml(backendLabel(item))}">${escapeHtml(backendLabel(item))}: ${escapeHtml(numberText(item.total_tokens))}</span>`
      )).join("")
    }</div>`;
  }

  function dailyRowsHtml(days = []) {
    if (!days.length) return `<div class="token-usage-empty">${escapeHtml(t("token_usage.empty", "No usage events"))}</div>`;
    return `<div class="token-usage-table-wrap"><table class="token-usage-table">
      <thead>
        <tr>
          <th>${escapeHtml(t("token_usage.date", "Date"))}</th>
          <th>${escapeHtml(t("token_usage.billable_input", "Billable input"))}</th>
          <th>${escapeHtml(t("token_usage.cache_read", "Cache read"))}</th>
          <th>${escapeHtml(t("token_usage.cache_creation", "Cache create"))}</th>
          <th>${escapeHtml(t("token_usage.output", "Output"))}</th>
          <th>${escapeHtml(t("token_usage.total_tokens", "Total"))}</th>
          <th>${escapeHtml(t("token_usage.cost", "Cost"))}</th>
        </tr>
      </thead>
      <tbody>
        ${days.slice().reverse().map(day => (
          `<tr>
            <td><strong>${escapeHtml(day.date || "-")}</strong>${backendBreakdownHtml(day)}</td>
            <td>${escapeHtml(numberText(day.billable_input_tokens))}</td>
            <td>${escapeHtml(numberText(day.cache_read_tokens))}</td>
            <td>${escapeHtml(numberText(day.cache_creation_tokens))}</td>
            <td>${escapeHtml(numberText(day.output_tokens))}</td>
            <td>${escapeHtml(numberText(day.total_tokens))}</td>
            <td>${escapeHtml(costText(day.cost_usd))}</td>
          </tr>`
        )).join("")}
      </tbody>
    </table></div>`;
  }

  function apiUrl(runId, timezone) {
    const query = new URLSearchParams({
      run_id: runId,
      timezone
    });
    return `/api/usage/daily?${query.toString()}`;
  }

  function bindTokenUsage() {
    const button = document.getElementById("token-usage");
    const popover = document.getElementById("token-usage-popover");
    const runIdEl = document.getElementById("run-id");
    const sessionMenu = document.getElementById("session-menu");
    if (!button || !popover) return;

    let open = false;
    let loading = false;
    let data = null;
    let error = "";
    let loadedRunId = "";

    function syncButton() {
      const disabled = !currentRunId();
      button.disabled = disabled;
      if (disabled && open) setOpen(false);
    }

    function render() {
      button.setAttribute("aria-expanded", String(open));
      popover.hidden = !open;
      sessionMenu?.classList?.toggle("token-usage-open", open);
      if (!open) return;
      const timezone = data?.timezone || localTimezone();
      const totals = data?.totals || {};
      const state = loading
        ? t("token_usage.loading", "Loading usage...")
        : error || `${t("token_usage.timezone", "Timezone")}: ${timezone} · ${t("token_usage.events", "Events")}: ${numberText(data?.matched_events || 0)}`;
      popover.innerHTML = `<section class="token-usage-panel">
        <div class="token-usage-head">
          <div>
            <h3>${escapeHtml(t("token_usage.title", "Daily token usage"))}</h3>
            <div class="meta ${error ? "error" : ""}">${escapeHtml(state)}</div>
          </div>
          <div class="token-usage-actions">
            <button type="button" data-token-usage-refresh>${escapeHtml(t("common.refresh", "Refresh"))}</button>
            <button type="button" data-token-usage-close>${escapeHtml(t("common.close", "Close"))}</button>
          </div>
        </div>
        <div class="token-usage-metrics">${usageSummaryCells(totals)}</div>
        ${loading || error ? "" : dailyRowsHtml(data?.days || [])}
      </section>`;
    }

    async function load(force = false) {
      const runId = currentRunId();
      if (!runId) {
        data = null;
        error = t("run.none", "No run selected");
        render();
        return;
      }
      if (!force && data && loadedRunId === runId) {
        render();
        return;
      }
      loading = true;
      error = "";
      loadedRunId = runId;
      render();
      try {
        const response = await fetch(apiUrl(runId, localTimezone()));
        const payload = await response.json().catch(() => null);
        if (!response.ok) throw new Error(payload?.error || `${response.status} ${response.statusText}`.trim());
        if (loadedRunId !== runId) return;
        data = payload || {};
      } catch (err) {
        if (loadedRunId !== runId) return;
        data = null;
        error = `${t("token_usage.load_failed", "Failed to load usage")}: ${err?.message || String(err)}`;
      } finally {
        if (loadedRunId === runId) {
          loading = false;
          render();
        }
      }
    }

    function setOpen(value) {
      open = Boolean(value && currentRunId());
      render();
      if (open) void load(false);
    }

    button.addEventListener("click", event => {
      event.stopPropagation();
      setOpen(!open);
    });
    popover.addEventListener("click", event => {
      event.stopPropagation();
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest("[data-token-usage-close]")) {
        setOpen(false);
        return;
      }
      if (target?.closest("[data-token-usage-refresh]")) {
        void load(true);
      }
    });
    document.addEventListener("click", event => {
      const target = event.target instanceof Element ? event.target : null;
      if (open && !popover.contains(target) && !button.contains(target)) setOpen(false);
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && open) setOpen(false);
    });
    window.addEventListener("aha:languagechange", render);
    if (runIdEl) {
      new MutationObserver(() => {
        syncButton();
        if (open && currentRunId() !== loadedRunId) void load(true);
      }).observe(runIdEl, { childList: true, characterData: true, subtree: true });
    }
    syncButton();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindTokenUsage, { once: true });
  } else {
    bindTokenUsage();
  }
})();
