(() => {
  function escapeFallback(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function createConversationPanelHelpers(options = {}) {
    const escapeHtml = options.escapeHtml || escapeFallback;
    const localizeTimestampText = options.localizeTimestampText || (value => String(value || ""));

    function renderConversationFiltersHtml({ active, filters, counts, filterOptions, open = false }) {
      if (!active) return "";
      const options = filterOptions || [];
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const label = t("conversation.filters", "Filters");
      return `
        <details id="conversation-filter-details" class="conversation-filter-popover" ${open ? "open" : ""}>
          <summary id="conversation-filter-toggle" class="conversation-filter-trigger" aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}">
            <svg class="conversation-filter-icon" aria-hidden="true" viewBox="0 0 24 24" focusable="false">
              <path d="M4 6h16l-6 7v5l-4 2v-7z"></path>
            </svg>
            <span class="sr-only">${escapeHtml(label)}</span>
          </summary>
          <div class="conversation-filter-menu">
            <div class="conversation-filter-chips" aria-label="${escapeHtml(label)}">
              ${options.map(item => `
                <label class="filter-chip ${filters?.[item.key] ? "active" : ""}">
                  <input type="checkbox" data-conversation-filter="${escapeHtml(item.key)}" ${filters?.[item.key] ? "checked" : ""}>
                  <span>${escapeHtml(t(`conversation.filter_${item.key}`, item.label))}</span>
                  <code>${escapeHtml(counts?.[item.key] ?? 0)}</code>
                </label>
              `).join("")}
            </div>
          </div>
        </details>
      `;
    }

    function renderConversationPanelHtml(view = {}) {
      if (view.loading) return `<div class="empty">Loading conversation...</div>`;
      if (view.error) {
        return `<div class="empty">Conversation unavailable. Realtime updates will start from the latest event offset.<br><code>${escapeHtml(view.error)}</code></div>`;
      }
      if (!view.eventsHtml && !view.hasMore) {
        const empty = `<div class="empty">No conversation for ${escapeHtml(view.target || "main")} yet.</div>`;
        return `<div class="conversation timeline">${empty}${view.timerHtml || ""}${view.metricsDockHtml || ""}</div>`;
      }
      const older = view.hasMore
        ? `<button class="load-older" type="button" data-load-older="true">${view.loadingOlder ? "Loading..." : "Load older"}</button>`
        : "";
      return `<div class="conversation timeline">${older}${view.eventsHtml || ""}${view.timerHtml || ""}${view.metricsDockHtml || ""}</div>`;
    }

    function renderFinalPanelHtml(detail) {
      if (!detail) return '<div class="empty">Loading final...</div>';
      return `<pre>${escapeHtml(detail.result || "No Final yet. Use /aha final to generate it.")}</pre>`;
    }

    function renderLogsPanelHtml(state = {}) {
      const older = state.hasMore
        ? `<button class="load-older" type="button" data-load-older-log="true">${state.loading ? "Loading..." : "Load older logs"}</button>`
        : "";
      const body = state.initialized ? localizeTimestampText(state.text || "No logs yet.") : "Loading logs...";
      return `<div class="log-view">${older}<pre>${escapeHtml(body)}</pre></div>`;
    }

    function renderContextPanelHtml({ rawPromptHtml = "", promptMetricsHtml = "" } = {}) {
      return `
        <div class="context-view">
          ${rawPromptHtml}
          ${promptMetricsHtml}
        </div>
      `;
    }

    return Object.freeze({
      renderConversationFiltersHtml,
      renderConversationPanelHtml,
      renderFinalPanelHtml,
      renderLogsPanelHtml,
      renderContextPanelHtml
    });
  }

  window.AHAConversationPanel = Object.freeze({ createConversationPanelHelpers });
})();
