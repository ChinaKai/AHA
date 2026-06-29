(() => {
  const TOKEN_USAGE_TIMEOUT_MS = 600000;
  const TOKEN_USAGE_POLL_MS = 1500;
  const DEFAULT_SINCE_DAYS = 0;
  const SINCE_STORAGE_KEY = "aha.tokenUsageSince.v2";

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

  function localTimezone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch (_err) {
      return "UTC";
    }
  }

  function dateInputValue(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function defaultSinceDate() {
    const date = new Date();
    date.setDate(date.getDate() - DEFAULT_SINCE_DAYS);
    return dateInputValue(date);
  }

  function todayDate() {
    return dateInputValue(new Date());
  }

  function normalizeDateInput(value) {
    const text = String(value || "").trim();
    return /^\d{4}-\d{2}-\d{2}$/.test(text) ? text : "";
  }

  function clampSinceDate(value) {
    const date = normalizeDateInput(value) || defaultSinceDate();
    const today = todayDate();
    return date > today ? today : date;
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

  function agentText(value) {
    const text = String(value || "unknown").trim();
    const known = {
      claude: "Claude",
      codex: "Codex",
      opencode: "OpenCode",
      ccusage: "ccusage"
    };
    return known[text.toLowerCase()] || text;
  }

  function modelListHtml(item = {}) {
    const models = Array.isArray(item.models)
      ? item.models
      : String(item.model || "").split(",").map(value => value.trim()).filter(Boolean);
    if (!models.length) return "";
    return models.map(model => `<div>- ${escapeHtml(model)}</div>`).join("");
  }

  function usageCells(usage = {}) {
    return `
      <td>${escapeHtml(numberText(usage.input_tokens))}</td>
      <td>${escapeHtml(numberText(usage.output_tokens))}</td>
      <td>${escapeHtml(numberText(usage.cache_creation_tokens))}</td>
      <td>${escapeHtml(numberText(usage.cache_read_tokens))}</td>
      <td>${escapeHtml(numberText(usage.total_tokens))}</td>
    `;
  }

  function dailyRowsHtml(days = [], totals = {}) {
    if (!days.length) return `<div class="token-usage-empty">${escapeHtml(t("token_usage.empty", "No usage records"))}</div>`;
    return `<div class="token-usage-table-wrap"><table class="token-usage-table">
      <thead>
        <tr>
          <th>${escapeHtml(t("token_usage.date", "Date"))}</th>
          <th>${escapeHtml(t("token_usage.agent", "Agent"))}</th>
          <th>${escapeHtml(t("token_usage.models", "Models"))}</th>
          <th>${escapeHtml(t("token_usage.input", "Input"))}</th>
          <th>${escapeHtml(t("token_usage.output", "Output"))}</th>
          <th>${escapeHtml(t("token_usage.cache_creation", "Cache create"))}</th>
          <th>${escapeHtml(t("token_usage.cache_read", "Cache read"))}</th>
          <th>${escapeHtml(t("token_usage.total_tokens", "Total"))}</th>
        </tr>
      </thead>
      <tbody>
        ${days.slice().reverse().map(day => {
          const detailRows = (Array.isArray(day.by_backend) ? day.by_backend : []).map(item => (
            `<tr class="token-usage-detail-row">
              <td></td>
              <td>- ${escapeHtml(agentText(item.backend))}</td>
              <td class="token-usage-models">${modelListHtml(item)}</td>
              ${usageCells(item)}
            </tr>`
          )).join("");
          return `<tr class="token-usage-all-row">
            <td><strong>${escapeHtml(day.date || "-")}</strong></td>
            <td>${escapeHtml(t("token_usage.all", "All"))}</td>
            <td></td>
            ${usageCells(day)}
          </tr>${detailRows}`;
        }).join("")}
      </tbody>
      <tfoot>
        <tr>
          <td><strong>${escapeHtml(t("token_usage.total_row", "Total"))}</strong></td>
          <td></td>
          <td></td>
          ${usageCells(totals)}
        </tr>
      </tfoot>
    </table></div>`;
  }

  function totalTrendHtml(days = []) {
    const points = days
      .filter(day => day?.date)
      .slice()
      .sort((a, b) => String(a.date).localeCompare(String(b.date)))
      .map(day => ({ date: String(day.date), total: Number(day.total_tokens || 0) }))
      .filter(point => Number.isFinite(point.total));
    if (!points.length) return "";
    const width = 720;
    const height = 180;
    const padLeft = 64;
    const padRight = 18;
    const padTop = 18;
    const padBottom = 30;
    const chartWidth = width - padLeft - padRight;
    const chartHeight = height - padTop - padBottom;
    const maxTotal = Math.max(1, ...points.map(point => point.total));
    const xStep = points.length > 1 ? chartWidth / (points.length - 1) : 0;
    const coords = points.map((point, index) => {
      const x = points.length > 1 ? padLeft + index * xStep : padLeft + chartWidth / 2;
      const y = padTop + chartHeight - (point.total / maxTotal) * chartHeight;
      return { ...point, x, y };
    });
    const gridTicks = [1, 0.75, 0.5, 0.25, 0];
    const gridHtml = gridTicks.map(ratio => {
      const y = padTop + chartHeight - ratio * chartHeight;
      return `<g>
        <line class="token-usage-chart-grid" x1="${padLeft}" y1="${y.toFixed(2)}" x2="${width - padRight}" y2="${y.toFixed(2)}"></line>
        <text class="token-usage-chart-label y" x="${padLeft - 8}" y="${(y + 4).toFixed(2)}">${escapeHtml(numberText(Math.round(maxTotal * ratio)))}</text>
      </g>`;
    }).join("");
    const polyline = coords.map(point => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(" ");
    const last = coords[coords.length - 1];
    return `<div class="token-usage-chart">
      <div class="token-usage-chart-head">
        <span>${escapeHtml(t("token_usage.all_total_trend", "All total trend"))}</span>
        <strong>${escapeHtml(numberText(last.total))}</strong>
      </div>
      <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(t("token_usage.all_total_trend", "All total trend"))}">
        ${gridHtml}
        <line class="token-usage-chart-grid axis" x1="${padLeft}" y1="${padTop}" x2="${padLeft}" y2="${height - padBottom}"></line>
        <polyline class="token-usage-chart-line" points="${polyline}"></polyline>
        ${coords.map(point => `<circle class="token-usage-chart-point" cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="3"><title>${escapeHtml(point.date)} ${escapeHtml(numberText(point.total))}</title></circle>`).join("")}
        <text class="token-usage-chart-label" x="${padLeft}" y="${height - 8}">${escapeHtml(points[0].date)}</text>
        <text class="token-usage-chart-label end" x="${width - padRight}" y="${height - 8}">${escapeHtml(points[points.length - 1].date)}</text>
      </svg>
    </div>`;
  }

  function unavailableHtml(reason = "") {
    const detail = reason || t("token_usage.configure_hint", "Install ccusage, make npx available to the AHA service, or set AHA_CCUSAGE_COMMAND.");
    return `<div class="token-usage-empty"><strong>${escapeHtml(t("token_usage.unavailable", "Usage integration unavailable"))}</strong><span>${escapeHtml(detail)}</span></div>`;
  }

  function noCacheHtml() {
    return `<div class="token-usage-empty"><strong>${escapeHtml(t("token_usage.no_cache", "No cached usage yet"))}</strong><span>${escapeHtml(t("token_usage.no_cache_hint", "Refresh to load usage in the background."))}</span></div>`;
  }

  function prefixedError(err, fallback) {
    const message = String(err?.message || err || fallback);
    return message.startsWith(`${fallback}:`) ? message : `${fallback}: ${message}`;
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
    let pollTimer = null;
    let bound = false;

    function readSinceDate() {
      try {
        return clampSinceDate(windowRef.localStorage?.getItem(SINCE_STORAGE_KEY));
      } catch (_err) {
        return defaultSinceDate();
      }
    }

    function storeSinceDate(value) {
      try {
        windowRef.localStorage?.setItem(SINCE_STORAGE_KEY, value);
      } catch (_err) {
        // localStorage can be unavailable in restricted browser modes.
      }
    }

    let sinceDate = readSinceDate();

    function currentRunId() {
      const value = String(deps.currentRunId?.() || runIdEl?.textContent || "").trim();
      return value && value !== "-" ? value : "";
    }

    function usageParams(timezone, since = sinceDate) {
      const params = { timezone };
      if (since) params.since = since;
      return params;
    }

    function usageApiUrl(timezone) {
      const params = { timezone };
      return deps.apiUrl?.("/api/usage/daily", params) || `/api/usage/daily?${new URLSearchParams(params).toString()}`;
    }

    function usageRefreshApiUrl(timezone) {
      const params = usageParams(timezone);
      return deps.apiUrl?.("/api/usage/daily/refresh", params) || `/api/usage/daily/refresh?${new URLSearchParams(params).toString()}`;
    }

    function usageStopApiUrl(timezone) {
      const params = usageParams(timezone, data?.filters?.since || sinceDate);
      return deps.apiUrl?.("/api/usage/daily/stop", params) || `/api/usage/daily/stop?${new URLSearchParams(params).toString()}`;
    }

    async function fetchUsageJson(url, fallback, options = {}) {
      if (deps.fetchWithTimeout && deps.readJsonResponse) {
        const response = await deps.fetchWithTimeout(url, options, TOKEN_USAGE_TIMEOUT_MS);
        return deps.readJsonResponse(response, fallback);
      }
      return deps.fetchJson?.(url, options, fallback) || {};
    }

    function clearPoll() {
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    }

    function refreshRunning() {
      return ["running", "stopping"].includes(data?.refresh?.status);
    }

    function schedulePoll() {
      clearPoll();
      if (!open || !refreshRunning()) return;
      pollTimer = setTimeout(() => {
        void loadTokenUsage({ force: true, silent: true });
      }, TOKEN_USAGE_POLL_MS);
    }

    function renderPopover() {
      if (!popover || !open) return;
      const timezone = data?.timezone || localTimezone();
      const totals = data?.totals || {};
      const unavailable = data?.available === false;
      const unavailableReason = data?.unavailable_reason || t("token_usage.configure_hint", "Install ccusage, make npx available to the AHA service, or set AHA_CCUSAGE_COMMAND.");
      const activeSince = data?.filters?.since || "-";
      const pendingSinceChanged = Boolean(!refreshRunning() && data?.cache?.status === "ready" && sinceDate && data?.filters?.since && sinceDate !== data.filters.since);
      const cacheMissing = data?.cache?.status === "missing";
      const refresh = data?.refresh || {};
      const refreshError = refresh.status === "failed" && refresh.error ? `${t("token_usage.refresh_failed", "Refresh failed")}: ${refresh.error}` : "";
      const readyState = `${t("token_usage.timezone", "Timezone")}: ${timezone} · ${t("token_usage.since", "Since")}: ${activeSince} · ${t("token_usage.days", "Days")}: ${numberText(data?.matched_events || 0)}${pendingSinceChanged ? ` · ${t("token_usage.pending_since", "Pending since")}: ${sinceDate}` : ""}`;
      const state = loading
        ? t("token_usage.loading_cache", "Loading cached usage...")
        : error || refreshError || (refresh.status === "stopping"
          ? t("token_usage.stopping", "Stopping refresh...")
          : refresh.status === "running"
          ? t("token_usage.refreshing", "Refreshing usage in background...")
          : unavailable
          ? `${t("token_usage.unavailable", "Usage integration unavailable")}: ${unavailableReason}`
          : cacheMissing
          ? t("token_usage.no_cache_hint", "Refresh to load usage in the background.")
          : readyState);
      popover.innerHTML = `<section class="token-usage-panel">
        <div class="token-usage-head">
          <div>
            <h3>${escapeHtml(t("token_usage.title", "Daily token usage"))}</h3>
            <div class="meta ${error ? "error" : ""}">${escapeHtml(state)}</div>
          </div>
          <div class="token-usage-actions">
            <label class="token-usage-filter"><span>${escapeHtml(t("token_usage.since", "Since"))}</span><input type="date" max="${escapeHtml(todayDate())}" data-token-usage-since value="${escapeHtml(sinceDate)}"></label>
            <button type="button" data-token-usage-refresh>${escapeHtml(refreshRunning() ? t("token_usage.stop_refresh", "Stop") : t("common.refresh", "Refresh"))}</button>
          </div>
        </div>
        <div class="token-usage-metrics">${usageSummaryCells(totals)}</div>
        ${loading || error ? "" : unavailable ? unavailableHtml(unavailableReason) : cacheMissing ? noCacheHtml() : `${totalTrendHtml(data?.days || [])}${dailyRowsHtml(data?.days || [], totals)}`}
      </section>`;
      schedulePoll();
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
        data = await fetchUsageJson(
          usageApiUrl(localTimezone()),
          t("token_usage.load_failed", "Failed to load usage")
        ) || {};
      } catch (err) {
        data = null;
        error = prefixedError(err, t("token_usage.load_failed", "Failed to load usage"));
      } finally {
        loading = false;
        if (open && loadedRunId === runId) renderPopover();
      }
    }

    async function refreshTokenUsage() {
      const runId = currentRunId();
      if (!runId) return;
      loading = true;
      error = "";
      renderPopover();
      loadedRunId = runId;
      try {
        data = await fetchUsageJson(
          usageRefreshApiUrl(localTimezone()),
          t("token_usage.refresh_failed", "Refresh failed"),
          { method: "POST" }
        ) || {};
      } catch (err) {
        error = prefixedError(err, t("token_usage.refresh_failed", "Refresh failed"));
      } finally {
        loading = false;
        if (open && loadedRunId === runId) renderPopover();
      }
    }

    async function stopTokenUsage() {
      const runId = currentRunId();
      if (!runId) return;
      error = "";
      try {
        data = await fetchUsageJson(
          usageStopApiUrl(localTimezone()),
          t("token_usage.stop_failed", "Stop failed"),
          { method: "POST" }
        ) || {};
      } catch (err) {
        error = prefixedError(err, t("token_usage.stop_failed", "Stop failed"));
      } finally {
        if (open && loadedRunId === runId) renderPopover();
      }
    }

    function setOpen(nextOpen) {
      open = Boolean(nextOpen && currentRunId() && popover);
      if (!popover) return;
      if (open) {
        deps.setRunMaintenanceConsoleOpen?.(false);
        deps.setHeadroomIntegrationOpen?.(false);
        deps.setWeixinConsoleOpen?.(false);
        deps.setPlayConsoleOpen?.(false);
        deps.setSkillsConsoleOpen?.(false);
      }
      sessionMenu?.classList?.toggle("token-usage-open", open);
      popover.hidden = !open;
      button?.setAttribute("aria-expanded", String(open));
      if (open) {
        renderPopover();
        void loadTokenUsage({ silent: Boolean(data && loadedRunId === currentRunId()) });
      } else {
        clearPoll();
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
          void (refreshRunning() ? stopTokenUsage() : refreshTokenUsage());
        }
      });
      popover?.addEventListener("change", event => {
        const target = event.target instanceof Element ? event.target : null;
        const input = target?.closest("[data-token-usage-since]");
        if (!input || !("value" in input)) return;
        sinceDate = clampSinceDate(input.value);
        input.value = sinceDate;
        storeSinceDate(sinceDate);
        renderPopover();
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
