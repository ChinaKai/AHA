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

  function localTimezone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch (_err) {
      return "UTC";
    }
  }

  function usageSummaryCells(usage = {}) {
    return [
      ["token_usage.input", "Input", usage.input_tokens],
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
    if (!days.length) return `<div class="token-usage-empty">${escapeHtml(t("token_usage.empty", "No usage records"))}</div>`;
    return `<div class="token-usage-table-wrap"><table class="token-usage-table">
      <thead>
        <tr>
          <th>${escapeHtml(t("token_usage.date", "Date"))}</th>
          <th>${escapeHtml(t("token_usage.input", "Input"))}</th>
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
            <td>${escapeHtml(numberText(day.input_tokens))}</td>
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

  function createTokenUsageController(elements = {}, deps = {}) {
    const button = elements.tokenUsageEl;
    const popover = elements.tokenUsagePopoverEl;
    const sessionMenu = elements.sessionMenuEl;
    const runIdEl = elements.runIdEl;
    const windowRef = deps.windowRef || window;
    let open = false;
    let loading = false;
    let data = null;
    let error = "";
    let loadedRunId = "";
    let bound = false;

    function currentRunId() {
      const value = String(deps.currentRunId?.() || runIdEl?.textContent || "").trim();
      return value && value !== "-" ? value : "";
    }

    function usageApiUrl(timezone) {
      return deps.apiUrl?.("/api/usage/daily", { timezone }) || `/api/usage/daily?${new URLSearchParams({ timezone }).toString()}`;
    }

    function renderPopover() {
      if (!popover || !open) return;
      const timezone = data?.timezone || localTimezone();
      const totals = data?.totals || {};
      const state = loading
        ? t("token_usage.loading", "Loading usage...")
        : error || `${t("token_usage.timezone", "Timezone")}: ${timezone} · ${t("token_usage.days", "Days")}: ${numberText(data?.matched_events || 0)}`;
      popover.innerHTML = `<section class="token-usage-panel">
        <div class="token-usage-head">
          <div>
            <h3>${escapeHtml(t("token_usage.title", "Daily token usage"))}</h3>
            <div class="meta ${error ? "error" : ""}">${escapeHtml(state)}</div>
          </div>
          <div class="token-usage-actions">
            <button type="button" data-token-usage-refresh>${escapeHtml(t("common.refresh", "Refresh"))}</button>
          </div>
        </div>
        <div class="token-usage-metrics">${usageSummaryCells(totals)}</div>
        ${loading || error ? "" : dailyRowsHtml(data?.days || [])}
      </section>`;
    }

    async function loadTokenUsage(options = {}) {
      const runId = currentRunId();
      if (!runId) {
        data = null;
        error = t("run.none", "No run selected");
        renderPopover();
        return;
      }
      if (!options.force && data && loadedRunId === runId) {
        renderPopover();
        return;
      }
      if (!options.silent) {
        loading = true;
        error = "";
        renderPopover();
      }
      loadedRunId = runId;
      try {
        data = await deps.fetchJson?.(
          usageApiUrl(localTimezone()),
          {},
          t("token_usage.load_failed", "Failed to load usage")
        ) || {};
      } catch (err) {
        data = null;
        error = `${t("token_usage.load_failed", "Failed to load usage")}: ${err?.message || String(err)}`;
      } finally {
        loading = false;
        if (open && loadedRunId === runId) renderPopover();
      }
    }

    function setOpen(nextOpen) {
      open = Boolean(nextOpen && currentRunId() && popover);
      if (!popover) return;
      sessionMenu?.classList?.toggle("token-usage-open", open);
      popover.hidden = !open;
      button?.setAttribute("aria-expanded", String(open));
      if (open) {
        renderPopover();
        void loadTokenUsage({ silent: Boolean(data && loadedRunId === currentRunId()) });
      } else {
        popover.innerHTML = "";
      }
    }

    function bind() {
      if (bound) return;
      bound = true;
      popover?.addEventListener("click", event => {
        event.stopPropagation();
        const target = event.target instanceof Element ? event.target : null;
        if (target?.closest("[data-token-usage-refresh]")) {
          void loadTokenUsage({ force: true });
        }
      });
      windowRef.addEventListener?.("aha:languagechange", () => {
        if (open) renderPopover();
      });
    }

    return Object.freeze({
      bind,
      isOpen: () => open,
      loadTokenUsage,
      renderTokenUsagePopover: renderPopover,
      setTokenUsageOpen: setOpen
    });
  }

  window.AHATokenUsage = Object.freeze({ createTokenUsageController });
})();
