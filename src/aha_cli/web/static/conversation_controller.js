(() => {
  function createConversationController(state = {}, deps = {}) {
    const allEvents = state.allEvents || [];
    const contextDetails = state.contextDetails || new Map();
    const conversationFilters = state.conversationFilters || {};
    const conversationStates = state.conversationStates || new Map();
    const conversationSessionRefreshes = state.conversationSessionRefreshes || new Map();
    const conversationSessionRefreshAt = state.conversationSessionRefreshAt || new Map();
    const copyTextByKey = state.copyTextByKey || new Map();
    const finalDetails = state.finalDetails || new Map();
    const logStates = state.logStates || new Map();
    const promptArtifactCache = state.promptArtifactCache || new Map();
    const compactResetStates = state.compactResetStates || new Map();
    const seenRealtimeEvents = state.seenRealtimeEvents || new Set();
    const panelEl = state.panelEl;
    const conversationFiltersEl = state.conversationFiltersEl;
    const logPageLimit = Number(state.logPageLimit || 200);
    const conversationPageLimit = Number(state.conversationPageLimit || 30);
    const sessionRefreshFallbackMs = Number(state.sessionRefreshFallbackMs || 5000);
    const getCurrentRunId = state.currentRunId || (() => "");
    const getSelectedTaskId = state.selectedTaskId || (() => "");
    const getActiveTab = state.activeTab || (() => "conversation");
    const backendTarget = state.backendTarget || (() => "main");
    const getOffset = state.offset || (() => -1);
    const setOffset = state.setOffset || (() => {});
    const getLastEventId = state.lastEventId || (() => "");
    const setLastEventId = state.setLastEventId || (() => {});
    const getEventTailInitialized = state.eventTailInitialized || (() => false);
    const setEventTailInitialized = state.setEventTailInitialized || (() => {});
    const getOpenPromptMetricsKey = state.openPromptMetricsKey || (() => "");
    const documentRef = state.documentRef || deps.documentRef || document;
    let conversationFiltersOpen = false;

    function promptMetricsKey(taskId, target = backendTarget()) {
      return `${taskId || ""}:${target || ""}`;
    }

    function conversationBackendSession(taskId, target = backendTarget()) {
      const stateValue = conversationStates.get(deps.conversationKey(taskId, target));
      return stateValue?.backendSession || null;
    }

    function promptMetricCandidateEvents(taskId, target = backendTarget()) {
      const candidates = [...deps.conversationSourceEvents(taskId, target), ...deps.taskEvents(taskId)];
      const seen = new Set();
      return candidates
        .filter(event => deps.isTaskEvent(event, taskId) && deps.eventMatchesAgent(event, target))
        .filter(event => {
          const id = deps.eventIdentity(event);
          if (seen.has(id)) return false;
          seen.add(id);
          return true;
        })
        .sort((left, right) => deps.conversationEventOrder(left) - deps.conversationEventOrder(right));
    }

    function latestTurnStartOrder(taskId, target = backendTarget()) {
      const events = deps.conversationSourceEvents(taskId, target).filter(event => deps.eventMatchesAgent(event, target));
      for (let index = events.length - 1; index >= 0; index -= 1) {
        if (events[index].type === "agent_started") return deps.conversationEventOrder(events[index]);
      }
      return null;
    }

    function latestTurnEvent(taskId, type, target = backendTarget()) {
      const startOrder = latestTurnStartOrder(taskId, target);
      const events = promptMetricCandidateEvents(taskId, target).filter(event => (
        event.type === type &&
        (startOrder == null || deps.conversationEventOrder(event) >= startOrder)
      ));
      return events.length ? events[events.length - 1] : null;
    }

    function latestPromptMetricsEvent(taskId, target = backendTarget()) {
      const events = promptMetricCandidateEvents(taskId, target).filter(event => event.type === "agent_prompt_metrics");
      return latestTurnEvent(taskId, "agent_prompt_metrics", target) || (events.length ? events[events.length - 1] : null);
    }

    function latestAgentUsageEvent(taskId, target = backendTarget()) {
      const events = promptMetricCandidateEvents(taskId, target).filter(event => event.type === "agent_usage");
      return latestTurnEvent(taskId, "agent_usage", target) || (events.length ? events[events.length - 1] : null);
    }

    function usageMetricsStatus(taskId, usageEvent, target = backendTarget()) {
      const startOrder = latestTurnStartOrder(taskId, target);
      if (!usageEvent) {
        return { label: startOrder == null ? "no usage" : "pending", className: "usage-pending" };
      }
      if (startOrder != null && deps.conversationEventOrder(usageEvent) >= startOrder) {
        return { label: "current turn", className: "usage-current" };
      }
      if (startOrder != null) {
        return { label: "previous turn", className: "usage-previous" };
      }
      return { label: "latest", className: "usage-current" };
    }

    function ahaInputMetricsStatus(taskId, metricsEvent, target = backendTarget()) {
      const startOrder = latestTurnStartOrder(taskId, target);
      if (!metricsEvent) return { label: "none", className: "prompt-none" };
      if (startOrder != null && deps.conversationEventOrder(metricsEvent) >= startOrder) {
        return { label: "current", className: "prompt-current" };
      }
      return { label: "latest", className: "prompt-latest" };
    }

    function latestContextOverflowEvent(taskId, target = backendTarget()) {
      const events = promptMetricCandidateEvents(taskId, target).filter(event => event.type === "agent_context_overflow");
      const latestTurnOverflow = latestTurnEvent(taskId, "agent_context_overflow", target);
      if (latestTurnOverflow) return latestTurnOverflow;
      return events.length ? events[events.length - 1] : null;
    }

    function backendSessionStatus(backendSession, overflow = false) {
      const analysis = backendSession?.analysis || {};
      const hasSessionId = Boolean(backendSession?.id);
      if (!backendSession?.exists) {
        return hasSessionId
          ? { label: "missing", className: "session-missing" }
          : { label: "none", className: "session-none" };
      }
      if (analysis.error) return { label: "error", className: "session-error" };
      if (overflow) return { label: "overflow", className: "session-overflow" };
      const sessionSize = Number(backendSession.size_bytes || 0);
      if (sessionSize >= deps.backendSessionCompactBytes) return { label: "large", className: "session-large" };
      if (sessionSize >= deps.backendSessionWatchBytes) return { label: "watch", className: "session-watch" };
      return { label: "ok", className: "session-ok" };
    }

    function compactResetState(taskId, target = backendTarget()) {
      return compactResetStates.get(promptMetricsKey(taskId, target)) || null;
    }

    function compactResetAdvice(sessionStatus) {
      const level = sessionStatus?.label || "none";
      if (level === "large" || level === "overflow") return "Compact reset recommended";
      if (level === "watch") return "Watch session size";
      if (level === "compacting" || level === "restarting" || level === "checking") return "Compact reset in progress";
      if (level === "done") return "Compact reset complete";
      if (level === "error") return "Check session file";
      if (level === "missing") return "Session file missing";
      if (level === "none") return "No backend session";
      return "No reset needed";
    }

    function promptArtifactCacheKey(ref) {
      return `${getCurrentRunId() || ""}:${ref}`;
    }

    async function ensurePromptArtifactLoaded(promptRef) {
      const ref = deps.promptRefPath(promptRef);
      if (!ref) return;
      const cacheKey = promptArtifactCacheKey(ref);
      const cached = promptArtifactCache.get(cacheKey);
      if (cached?.loading || cached?.loaded || cached?.error) return;
      promptArtifactCache.set(cacheKey, { loading: true, loaded: false, error: "", prompt: "", prompt_ref: promptRef });
      try {
        const payload = await deps.fetchJson(deps.apiUrl("/api/prompt-artifact", { ref }), {}, "Failed to load raw prompt");
        promptArtifactCache.set(cacheKey, {
          loading: false,
          loaded: true,
          error: "",
          prompt: String(payload.prompt || ""),
          prompt_ref: payload.prompt_ref || promptRef
        });
      } catch (err) {
        promptArtifactCache.set(cacheKey, {
          loading: false,
          loaded: false,
          error: err?.message || "Failed to load raw prompt",
          prompt: "",
          prompt_ref: promptRef
        });
      }
      deps.renderPanel?.({ preserveContextScroll: true });
    }

    function renderRawPromptSection(data = {}, total = {}) {
      const promptRef = data?.prompt_ref || null;
      const ref = deps.promptRefPath(promptRef);
      const meta = deps.promptArtifactMeta(promptRef, total);
      if (ref) ensurePromptArtifactLoaded(promptRef);
      const artifact = ref ? promptArtifactCache.get(promptArtifactCacheKey(ref)) : null;
      const prompt = artifact?.prompt || "";
      const copyKey = prompt ? `raw-prompt:${ref}` : "";
      if (copyKey) copyTextByKey.set(copyKey, prompt);
      const detailParts = [
        meta.chars != null ? `${deps.formatMetricNumber(meta.chars)} chars` : "",
        meta.bytes != null ? deps.formatMetricBytes(meta.bytes) : "",
        meta.lines != null ? `${deps.formatMetricNumber(meta.lines)} lines` : "",
        data.prompt_mode ? `mode ${data.prompt_mode}` : "",
        data.source ? `source ${data.source}` : ""
      ].filter(Boolean);
      const copyButton = copyKey
        ? `<button class="message-copy" type="button" data-copy-message-key="${deps.escapeHtml(copyKey)}" data-copy-state="idle" title="Copy raw prompt" aria-label="Copy raw prompt"><span class="message-copy-icon" aria-hidden="true"></span><span class="message-copy-label sr-only">Copy raw prompt</span></button>`
        : "";
      const status = !ref
        ? "No raw prompt artifact for this turn."
        : artifact?.error
          ? artifact.error
          : artifact?.loaded
            ? ref
            : "Loading raw prompt...";
      return `
        <section class="raw-prompt-section">
          <div class="prompt-metrics-head">
            <div>
              <span>Latest assembled prompt</span>
              <strong>${deps.escapeHtml(ref ? "Raw Prompt" : "No artifact")}</strong>
              <code>${deps.escapeHtml(status)}</code>
            </div>
            ${copyButton}
          </div>
          <div class="prompt-metric-kpis">
            ${(detailParts.length ? detailParts : ["waiting for prompt_ref"]).map(part => `<code>${deps.escapeHtml(part)}</code>`).join("")}
          </div>
          <pre class="raw-prompt-body">${deps.escapeHtml(prompt || status)}</pre>
        </section>
      `;
    }

    function captureContextScrollState() {
      const rawPrompt = panelEl.querySelector(".raw-prompt-body");
      const metricsDetails = panelEl.querySelector(".compact-metrics-details");
      const breakdownOpen = {};
      panelEl.querySelectorAll("[data-metrics-breakdown]").forEach(details => {
        if (details instanceof HTMLDetailsElement) breakdownOpen[details.dataset.metricsBreakdown || ""] = details.open;
      });
      return {
        breakdownOpen,
        hasContextView: Boolean(panelEl.querySelector(".context-view")),
        panelTop: panelEl.scrollTop,
        rawPromptTop: rawPrompt ? rawPrompt.scrollTop : 0,
        metricsOpen: metricsDetails instanceof HTMLDetailsElement ? metricsDetails.open : false
      };
    }

    function restoreContextScrollState(scrollState) {
      if (!scrollState?.hasContextView) return;
      const rawPrompt = panelEl.querySelector(".raw-prompt-body");
      const metricsDetails = panelEl.querySelector(".compact-metrics-details");
      const breakdownOpen = scrollState.breakdownOpen || {};
      const restoredBreakdowns = new Set();
      Object.entries(breakdownOpen).forEach(([key, open]) => {
        const breakdown = Array.from(panelEl.querySelectorAll("[data-metrics-breakdown]"))
          .find(item => item instanceof HTMLDetailsElement && item.dataset.metricsBreakdown === key);
        if (breakdown instanceof HTMLDetailsElement) {
          breakdown.open = Boolean(open);
          restoredBreakdowns.add(key);
        }
      });
      if (!restoredBreakdowns.has("compact") && metricsDetails instanceof HTMLDetailsElement) metricsDetails.open = scrollState.metricsOpen;
      panelEl.scrollTop = scrollState.panelTop;
      if (rawPrompt) rawPrompt.scrollTop = scrollState.rawPromptTop;
    }

    function promptMetricsState(taskId) {
      const target = backendTarget();
      const metricsEvent = latestPromptMetricsEvent(taskId, target);
      const usageEvent = latestAgentUsageEvent(taskId, target);
      const usageStatus = usageMetricsStatus(taskId, usageEvent, target);
      const ahaInputStatus = ahaInputMetricsStatus(taskId, metricsEvent, target);
      const overflowEvent = latestContextOverflowEvent(taskId, target);
      const data = deps.eventData(metricsEvent || {});
      const total = data.total || {};
      const totalChars = Number(total.chars || 0);
      const rows = deps.componentMetricRows(data.components || {}, totalChars);
      const largest = rows[0];
      const overflow = Boolean(overflowEvent && (!metricsEvent || deps.conversationEventOrder(overflowEvent) >= deps.conversationEventOrder(metricsEvent)));
      const backendSession = conversationBackendSession(taskId);
      const sessionStatus = backendSessionStatus(backendSession, overflow);
      const contextPressure = backendSession?.context_pressure || null;
      return { ahaInputStatus, backendSession, contextPressure, data, largest, metricsEvent, overflow, overflowEvent, rows, sessionStatus, total, totalChars, usageEvent, usageStatus };
    }

    function renderPromptMetricsPanel(taskId) {
      const metrics = promptMetricsState(taskId);
      const { backendSession, contextPressure, data, largest, metricsEvent, overflow, overflowEvent, rows, sessionStatus, total, totalChars, usageEvent, usageStatus } = metrics;
      const resetState = compactResetState(taskId);
      const displayedSessionStatus = resetState
        ? { label: resetState.label, className: resetState.className }
        : sessionStatus;
      const hasSessionHistory = Array.isArray(backendSession?.history) && backendSession.history.length > 0;
      const hasSessionInfo = Boolean(backendSession?.id || backendSession?.exists || hasSessionHistory || backendSession?.compact_summary);
      if (!metricsEvent && !usageEvent && !contextPressure && !overflowEvent && !hasSessionInfo) {
        return `
          <section class="prompt-metrics empty-metrics">
            <div>
              <span>Prompt Input</span>
              <strong>No metrics yet</strong>
            </div>
            <code>send a message after the metrics build is running</code>
          </section>
        `;
      }

      const source = data.source || deps.eventData(overflowEvent || {}).source || "backend";
      const usage = deps.eventData(usageEvent || {}).usage || {};
      const contextStatus = deps.contextPressureStatus(contextPressure);
      const contextPercent = deps.contextPressurePercent(contextPressure);
      const contextHeadline = contextPercent ? `${contextPercent} context` : "context unknown";
      const contextInputTokens = contextPressure?.input_tokens ?? contextPressure?.prompt_tokens;
      const contextWindowTokens = contextPressure?.context_window;
      const contextWindowUsedLabel = contextInputTokens != null ? `window used ${deps.formatMetricCompact(contextInputTokens)}` : "";
      const contextWindowTotalLabel = contextWindowTokens != null ? `window total ${deps.formatMetricCompact(contextWindowTokens)}` : "";
      const contextWindowUsedTotalLabel = contextInputTokens != null && contextWindowTokens != null
        ? `window used ${deps.formatMetricCompact(contextInputTokens)} / total ${deps.formatMetricCompact(contextWindowTokens)}`
        : contextInputTokens != null
          ? contextWindowUsedLabel
          : contextWindowTotalLabel;
      const sessionSize = Number(backendSession?.size_bytes);
      const sessionAnalysis = backendSession?.analysis || {};
      const sessionAhaCounts = sessionAnalysis.aha_prompt_counts || {};
      const sessionAhaChars = sessionAnalysis.aha_prompt_chars || {};
      const sessionFullCount = Number(sessionAhaCounts.full || 0);
      const sessionFullChars = Number(sessionAhaChars.full || 0);
      const sessionDeltaCount = Number(sessionAhaCounts.sticky_delta || 0);
      const sessionDeltaChars = Number(sessionAhaChars.sticky_delta || 0);
      const sessionMirrorChars = Number(sessionAnalysis.event_msg_prompt_mirror_total_chars || 0);
      const sessionToolChars = Number(sessionAnalysis.tool_output_chars || 0);
      const sessionLineCount = Number(sessionAnalysis.line_count || 0);
      const compactAdviceText = compactResetAdvice(displayedSessionStatus);
      const contextLabel = contextPressure
        ? [
            contextWindowUsedTotalLabel,
            contextPressure.model ? `model ${contextPressure.model}` : "",
            contextPressure.context_window_source ? `window source ${contextPressure.context_window_source}` : ""
          ].filter(Boolean).join(" · ")
        : "waiting for context pressure";
      const contextParts = [
        `level ${contextStatus.label}`,
        contextWindowUsedLabel,
        contextWindowTotalLabel,
        contextPressure?.pressure_source ? `source ${contextPressure.pressure_source}` : "",
        backendSession?.exists && Number.isFinite(sessionSize) ? `session ${deps.formatMetricBytes(sessionSize)}` : "",
        compactAdviceText
      ].filter(Boolean);
      const sessionActionButton = backendSession?.id
        ? `<button type="button" class="compact-reset-primary" data-session-action="compact-reset"${resetState ? " disabled" : ""}>${deps.escapeHtml(resetState?.buttonLabel || "Compact & Reset")}</button>`
        : "";
      const ahaParts = [
        `${deps.formatMetricNumber(totalChars)} chars`,
        `${deps.formatMetricBytes(total.bytes)} bytes`,
        `${deps.formatMetricNumber(total.lines)} lines`,
        data.event_limit ? `${deps.formatMetricNumber(data.event_limit)} events` : ""
      ].filter(Boolean);
      const usageParts = [
        usage.input_tokens != null ? `input ${deps.formatMetricNumber(usage.input_tokens)}` : "",
        (usage.cached_input_tokens != null || usage.cache_read_input_tokens != null) ? `cached ${deps.formatMetricNumber(deps.usageCacheReadTokens(usage))}` : "",
        usage.cache_creation_input_tokens != null ? `created ${deps.formatMetricNumber(deps.usageCacheCreationTokens(usage))}` : "",
        usage.output_tokens != null ? `output ${deps.formatMetricNumber(usage.output_tokens)}` : "",
        usage.reasoning_output_tokens != null ? `reasoning ${deps.formatMetricNumber(usage.reasoning_output_tokens)}` : "",
        usage.total_cost_usd != null ? `$${Number(usage.total_cost_usd || 0).toFixed(4)}` : "",
        usage.num_turns != null ? `${deps.formatMetricNumber(usage.num_turns)} turns` : "",
        deps.contextPressureSummary(contextPressure)
      ].filter(Boolean);
      const backendSummary = deps.contextPressurePercent(contextPressure)
        ? `${deps.formatMetricNumber(contextPressure.input_tokens ?? contextPressure.prompt_tokens ?? 0)} input tokens · ${deps.contextPressurePercent(contextPressure)} ctx`
        : usage.input_tokens != null ? `${deps.formatMetricNumber(usage.input_tokens)} input` : usageStatus.label;
      return `
        <section class="prompt-metrics session-compact-metrics ${overflow ? "has-overflow" : ""}">
          <div class="prompt-metrics-section session-metrics-section context-metrics-section session-compact-summary">
            <div class="prompt-metrics-head">
              <div>
                <span>Context Pressure</span>
                <strong>${deps.escapeHtml(contextHeadline)}</strong>
                <code>${deps.escapeHtml(contextLabel || "waiting for context pressure")}</code>
              </div>
              <div class="prompt-metrics-head-actions">
                <span class="status ${contextStatus.className}">${deps.escapeHtml(contextStatus.label)}</span>
                ${sessionActionButton}
              </div>
            </div>
            <div class="prompt-metric-kpis">
              ${(contextParts.length ? contextParts : ["context unknown"]).map(part => `<code>${deps.escapeHtml(part)}</code>`).join("")}
            </div>
          </div>
          <details class="metrics-breakdown compact-metrics-details" data-metrics-breakdown="compact">
            <summary>Metrics details</summary>
            <div class="compact-metrics-detail-grid">
              <div class="session-breakdown-group">
                <strong>AHA Input</strong>
                <div class="prompt-metric-kpis">
                  ${ahaParts.map(part => `<code>${deps.escapeHtml(part)}</code>`).join("")}
                </div>
                <div class="prompt-component-bars">
                  ${rows.map(row => `
                    <div class="prompt-component-row">
                      <span>${deps.escapeHtml(row.name)}</span>
                      <div class="prompt-component-track" aria-hidden="true">
                        <i style="width: ${row.percent.toFixed(2)}%"></i>
                      </div>
                      <code>${deps.escapeHtml(deps.formatMetricNumber(row.chars))}</code>
                    </div>
                  `).join("")}
                </div>
                ${deps.renderAhaInputBreakdown(data, rows)}
              </div>
              <div class="session-breakdown-group">
                <strong>Backend Usage</strong>
                <div class="prompt-metric-kpis">
                  ${(usageParts.length ? usageParts : [`usage ${usageStatus.label}`]).map(part => `<code>${deps.escapeHtml(part)}</code>`).join("")}
                </div>
                <div class="prompt-metric-kpis">
                  <code>${deps.escapeHtml(backendSummary)}</code>
                  <code>${deps.escapeHtml(source || "waiting for backend usage")}</code>
                  <code>${deps.escapeHtml(`ctx ${contextStatus.label}`)}</code>
                </div>
                ${deps.renderUsageBreakdown(usage, usageStatus, source, contextPressure)}
              </div>
              <div class="session-breakdown-group">
                <strong>Backend Session</strong>
                <div class="prompt-metric-kpis">
                  ${[
                    sessionLineCount ? `${deps.formatMetricNumber(sessionLineCount)} lines` : "",
                    deps.formatMetricCountChars(sessionFullCount, sessionFullChars, "full"),
                    deps.formatMetricCountChars(sessionDeltaCount, sessionDeltaChars, "delta"),
                    `mirrors ${deps.formatMetricNumber(sessionMirrorChars)} chars`,
                    `tools ${deps.formatMetricNumber(sessionToolChars)} chars`,
                    sessionAnalysis.parse_errors ? `${deps.formatMetricNumber(sessionAnalysis.parse_errors)} parse errors` : ""
                  ].filter(Boolean).map(part => `<code>${deps.escapeHtml(part)}</code>`).join("")}
                </div>
                ${backendSession?.exists ? deps.renderSessionBreakdown(sessionAnalysis) : ""}
              </div>
            </div>
          </details>
        </section>
      `;
    }

    function renderPromptMetricsPopover(taskId) {
      const metrics = promptMetricsState(taskId);
      const hasHistory = Array.isArray(metrics.backendSession?.history) && metrics.backendSession.history.length > 0;
      const hasMetrics = Boolean(metrics.metricsEvent || metrics.usageEvent || metrics.contextPressure || metrics.overflowEvent || metrics.backendSession?.id || metrics.backendSession?.exists || hasHistory || metrics.backendSession?.compact_summary);
      const sessionSize = Number(metrics.backendSession?.size_bytes);
      const contextPercent = deps.contextPressurePercent(metrics.contextPressure);
      const contextSummary = contextPercent || (metrics.contextPressure ? "ctx ?" : "");
      const sessionSummary = metrics.backendSession?.exists && Number.isFinite(sessionSize)
        ? deps.formatMetricBytes(sessionSize)
        : metrics.metricsEvent ? deps.formatMetricCompact(metrics.totalChars) : "--";
      const summary = contextSummary || "ctx ?";
      const top = metrics.largest?.name || "no components";
      const key = promptMetricsKey(taskId);
      const open = getOpenPromptMetricsKey() === key ? " open" : "";
      const triggerContextStatus = deps.contextPressureStatus(metrics.contextPressure);
      const classes = ["turn-metrics", metrics.overflow ? "has-overflow" : "", triggerContextStatus.className || "", hasMetrics ? "" : "is-empty"].filter(Boolean).join(" ");
      const sessionLabel = metrics.sessionStatus?.label || "none";
      const label = hasMetrics
        ? `Context ${summary}; Session ${sessionLabel}: ${sessionSummary}; AHA input ${deps.formatMetricNumber(metrics.totalChars)} chars, top ${top}`
        : "Prompt metrics unavailable";
      return `
        <details class="${classes}" data-turn-metrics-key="${deps.escapeHtml(key)}"${open}>
          <summary class="turn-metrics-trigger" title="${deps.escapeHtml(label)}" aria-label="${deps.escapeHtml(label)}">
            <span class="turn-metrics-dot" aria-hidden="true"></span>
            <code>${deps.escapeHtml(summary)}</code>
          </summary>
          <div class="turn-metrics-popover">
            ${renderPromptMetricsPanel(taskId)}
          </div>
        </details>
      `;
    }

    function renderPromptMetricsDock(taskId) {
      const metrics = promptMetricsState(taskId);
      const hasHistory = Array.isArray(metrics.backendSession?.history) && metrics.backendSession.history.length > 0;
      if (!metrics.metricsEvent && !metrics.usageEvent && !metrics.contextPressure && !metrics.overflowEvent && !metrics.backendSession?.id && !metrics.backendSession?.exists && !hasHistory && !metrics.backendSession?.compact_summary) return "";
      return `<div class="conversation-metrics-dock">${renderPromptMetricsPopover(taskId)}</div>`;
    }

    async function loadFinalDetail(taskId, force = false) {
      if (!taskId) return null;
      if (!force && finalDetails.has(taskId)) return finalDetails.get(taskId);
      const detail = await deps.fetchJson(deps.apiUrl(`/api/task/${encodeURIComponent(taskId)}/final`), {}, "Failed to load final");
      finalDetails.set(taskId, detail);
      return detail;
    }

    async function loadContextDetail(taskId, force = false) {
      if (!taskId) return null;
      if (!force && contextDetails.has(taskId)) return contextDetails.get(taskId);
      const detail = await deps.fetchJson(deps.apiUrl(`/api/task/${encodeURIComponent(taskId)}/context`), {}, "Failed to load context");
      contextDetails.set(taskId, detail);
      return detail;
    }

    function finalDetail(taskId) {
      return finalDetails.get(taskId);
    }

    function contextDetail(taskId) {
      return contextDetails.get(taskId);
    }

    function logState(taskId) {
      if (!logStates.has(taskId)) {
        logStates.set(taskId, { text: "", beforeOffset: null, hasMore: true, initialized: false, loading: false, source: "auto", autoFollow: true });
      }
      return logStates.get(taskId);
    }

    async function loadLogPage(taskId, older = false, force = false) {
      if (!taskId) return null;
      const stateValue = logState(taskId);
      if (stateValue.loading || (!force && !older && stateValue.initialized) || (older && !stateValue.hasMore)) return stateValue;
      stateValue.loading = true;
      try {
        const params = new URLSearchParams({ limit: String(logPageLimit) });
        if (older && stateValue.source) params.set("source", stateValue.source);
        if (older && stateValue.beforeOffset !== null && stateValue.beforeOffset !== undefined) params.set("before_offset", String(stateValue.beforeOffset));
        const payload = await deps.fetchJson(deps.apiUrl(`/api/task/${encodeURIComponent(taskId)}/logs`, params), {}, "Failed to load logs");
        const text = payload.text || "";
        stateValue.text = older ? [text, stateValue.text].filter(Boolean).join("\n") : text;
        stateValue.beforeOffset = payload.next_before_offset ?? payload.before ?? null;
        stateValue.hasMore = Boolean(payload.has_more);
        stateValue.source = payload.source || stateValue.source || "auto";
        stateValue.initialized = true;
        return stateValue;
      } finally {
        stateValue.loading = false;
      }
    }

    async function ensureActiveTabData() {
      const selectedTaskId = getSelectedTaskId();
      if (!selectedTaskId) return;
      const activeTab = getActiveTab();
      if (activeTab === "conversation") {
        await ensureConversationLoaded();
      } else if (activeTab === "logs") {
        await loadLogPage(selectedTaskId);
      } else if (activeTab === "final") {
        await loadFinalDetail(selectedTaskId, true);
      } else if (activeTab === "context") {
        await Promise.all([
          loadContextDetail(selectedTaskId),
          loadConversationPage(selectedTaskId, backendTarget())
        ]);
      }
    }

    async function loadOlderLogs() {
      const selectedTaskId = getSelectedTaskId();
      if (getActiveTab() !== "logs" || !selectedTaskId) return;
      const stateValue = logState(selectedTaskId);
      if (!stateValue.initialized || !stateValue.hasMore || stateValue.loading) return;
      const previousHeight = panelEl.scrollHeight;
      const previousTop = panelEl.scrollTop;
      await loadLogPage(selectedTaskId, true);
      deps.renderPanel?.({ preserveScroll: true, previousHeight, previousTop });
    }

    async function responseError(res, fallbackMessage = "Request failed") {
      try {
        await deps.readJsonResponse(res, fallbackMessage);
      } catch (err) {
        return err;
      }
      return new Error(fallbackMessage);
    }

    async function initializeEventTailOffset() {
      if (getEventTailInitialized()) return;
      deps.realtimeDebug("events.tail.request");
      const payload = await deps.fetchJson(deps.apiUrl("/api/events", { offset: "-1" }), {}, "Failed to initialize event stream");
      deps.rememberEventCursor(payload);
      setEventTailInitialized(true);
      deps.realtimeDebug("events.tail.response", {
        last_event_id: payload.last_event_id || "",
        offset: payload.offset,
        snapshot_event_id: payload.snapshot_event_id || payload.snapshot_offset || "",
        event_count: (payload.events || []).length
      });
    }

    async function prepareRealtimeCatchupBaseline() {
      if (getLastEventId() || getEventTailInitialized()) return;
      try {
        await initializeEventTailOffset();
      } catch (err) {
        deps.realtimeDebug("events.tail.error", { error: err?.message || String(err) });
      }
    }

    async function markConversationUnavailable(stateValue, err) {
      stateValue.events = [];
      stateValue.beforeOffset = null;
      stateValue.hasMore = false;
      stateValue.initialized = true;
      stateValue.error = err?.message || String(err || "Conversation unavailable");
      try {
        await initializeEventTailOffset();
      } catch (tailErr) {
        stateValue.error = `${stateValue.error}; ${tailErr?.message || tailErr}`;
      }
    }

    async function loadConversationPage(taskId = getSelectedTaskId(), target = backendTarget(), older = false, force = false) {
      if (!taskId) return null;
      const stateValue = deps.conversationState(taskId, target);
      const categoryKey = deps.activeConversationCategoryKey();
      deps.prepareConversationStateForLoad(stateValue, categoryKey, older);
      if (deps.shouldSkipConversationLoad(stateValue, older, force)) return stateValue;
      stateValue.loading = true;
      try {
        const params = new URLSearchParams({
          task_id: taskId,
          target,
          limit: String(conversationPageLimit),
          categories: categoryKey
        });
        if (older && stateValue.beforeOffset !== null && stateValue.beforeOffset !== undefined) params.set("before_offset", String(stateValue.beforeOffset));
        let res;
        try {
          res = await deps.fetchWithTimeout(deps.apiUrl("/api/conversation-events", params));
        } catch (err) {
          await markConversationUnavailable(stateValue, err);
          return stateValue;
        }
        if (!res.ok) {
          const error = await responseError(res, "Failed to load conversation");
          await markConversationUnavailable(stateValue, error);
          return stateValue;
        }
        const payload = await deps.readJsonResponse(res, "Failed to load conversation");
        const result = deps.applyConversationPagePayload(stateValue, payload, { older });
        conversationSessionRefreshAt.set(deps.conversationKey(taskId, target), Date.now());
        if (!older && getOffset() < 0 && Number.isFinite(result.afterOffset)) setOffset(result.afterOffset);
        return stateValue;
      } finally {
        stateValue.loading = false;
      }
    }

    async function ensureConversationLoaded() {
      if (getActiveTab() !== "conversation" || !getSelectedTaskId()) return;
      await loadConversationPage(getSelectedTaskId(), backendTarget(), false);
    }

    function isCurrentConversationTarget(taskId, target) {
      return getActiveTab() === "conversation" && getSelectedTaskId() === taskId && backendTarget() === target;
    }

    function refreshConversationBackendSession(taskId, target, options = {}) {
      if (!taskId || !target) return null;
      const key = deps.conversationKey(taskId, target);
      const existing = conversationSessionRefreshes.get(key);
      if (existing) {
        if (options.render) {
          existing.then(() => {
            if (isCurrentConversationTarget(taskId, target)) deps.renderPanelForRealtime?.();
          });
        }
        return existing;
      }
      const refresh = loadConversationPage(taskId, target, false, true)
        .then(stateValue => {
          conversationSessionRefreshAt.set(key, Date.now());
          if (options.render && isCurrentConversationTarget(taskId, target)) deps.renderPanelForRealtime?.();
          return stateValue;
        })
        .catch(err => {
          if (options.showError && options.render && isCurrentConversationTarget(taskId, target)) {
            panelEl.innerHTML = `<pre>${deps.escapeHtml(String(err))}</pre>`;
          } else {
            console.warn(`Failed to refresh conversation backend session (${options.reason || "refresh"})`, err);
          }
          return null;
        })
        .finally(() => {
          if (conversationSessionRefreshes.get(key) === refresh) conversationSessionRefreshes.delete(key);
        });
      conversationSessionRefreshes.set(key, refresh);
      return refresh;
    }

    function maybeRefreshConversationBackendSessionFallback() {
      const selectedTaskId = getSelectedTaskId();
      if (getActiveTab() !== "conversation" || !selectedTaskId) return null;
      const taskId = selectedTaskId;
      const target = backendTarget();
      const key = deps.conversationKey(taskId, target);
      const stateValue = conversationStates.get(key);
      if (!stateValue?.initialized || stateValue.loading) return null;
      const lastRefreshAt = conversationSessionRefreshAt.get(key) || 0;
      if (Date.now() - lastRefreshAt < sessionRefreshFallbackMs) return null;
      return refreshConversationBackendSession(taskId, target, { reason: "fallback" });
    }

    async function loadOlderConversation() {
      const selectedTaskId = getSelectedTaskId();
      if (getActiveTab() !== "conversation" || !selectedTaskId) return;
      const stateValue = deps.conversationState(selectedTaskId, backendTarget());
      if (!stateValue.initialized || !stateValue.hasMore || stateValue.loading) return;
      const previousHeight = panelEl.scrollHeight;
      const previousTop = panelEl.scrollTop;
      await loadConversationPage(selectedTaskId, backendTarget(), true);
      deps.renderPanel?.({ preserveScroll: true, previousHeight, previousTop });
    }

    function appendRealtimeConversationEvents(events) {
      if (!events.length) return;
      for (const [key, stateValue] of conversationStates.entries()) {
        if (!stateValue.initialized) continue;
        const { taskId, target } = deps.parseConversationKey(key);
        const matching = events.filter(event => (
          deps.isTaskEvent(event, taskId) &&
          deps.isTimelineEvent(event) &&
          deps.eventMatchesAgent(event, target) &&
          (conversationFilters[deps.conversationEventCategory(event)] || deps.turnEventTypes.has(event.type))
        ));
        if (matching.length) stateValue.events = deps.mergeConversationEvents(stateValue.events, matching, false);
      }
    }

    function invalidateConversationBackendSession(taskId, target) {
      const stateValue = conversationStates.get(deps.conversationKey(taskId, target));
      if (!stateValue) return false;
      stateValue.initialized = false;
      return true;
    }

    function queueConversationBackendSessionRefresh(sessionRefreshes, taskId, target, reason, options = {}) {
      if (!taskId || !target) return;
      const key = deps.conversationKey(taskId, target);
      if (options.invalidate) invalidateConversationBackendSession(taskId, target);
      if (isCurrentConversationTarget(taskId, target) || conversationStates.has(key)) {
        sessionRefreshes.set(key, { taskId, target, reason });
      }
    }

    function invalidateRealtimeTaskDetails(events) {
      const finalTaskIds = new Set();
      const sessionRefreshes = new Map();
      events.forEach(event => {
        const taskId = deps.eventTaskId(event);
        if (deps.finalDetailInvalidatingEvents.has(event.type) && taskId) finalTaskIds.add(taskId);
        if (deps.backendSessionRefreshEventTypes.has(event.type) && taskId) {
          const target = backendTarget();
          if (isCurrentConversationTarget(taskId, target) && deps.eventMatchesAgent(event, target)) {
            const reason = event.type === "agent_prompt_metrics" || event.type === "agent_usage" || event.type === "agent_context_overflow"
              ? "metrics-event"
              : "session-lifecycle";
            queueConversationBackendSessionRefresh(sessionRefreshes, taskId, target, reason);
          }
        }
        if (event.type === "backend_session_reset" && taskId) {
          const data = deps.eventData(event);
          const target = String(data.agent_id || data.target || "main");
          queueConversationBackendSessionRefresh(sessionRefreshes, taskId, target, "session-reset", { invalidate: true });
        }
        if (event.type === "backend_session_compact_reset" && taskId) {
          const data = deps.eventData(event);
          const target = String(data.agent_id || data.target || "main");
          queueConversationBackendSessionRefresh(sessionRefreshes, taskId, target, "compact-reset", { invalidate: true });
        }
      });
      finalTaskIds.forEach(taskId => finalDetails.delete(taskId));
      const selectedTaskId = getSelectedTaskId();
      if (getActiveTab() === "final" && selectedTaskId && finalTaskIds.has(selectedTaskId)) {
        loadFinalDetail(selectedTaskId, true)
          .then(() => deps.renderPanel?.())
          .catch(err => {
            panelEl.innerHTML = `<pre>${deps.escapeHtml(String(err))}</pre>`;
          });
      }
      for (const { taskId, target, reason } of sessionRefreshes.values()) {
        if (getActiveTab() !== "conversation" || getSelectedTaskId() !== taskId || backendTarget() !== target) continue;
        refreshConversationBackendSession(taskId, target, { render: true, showError: reason === "compact-reset", reason });
      }
    }

    function realtimeEventCursor(event, index = 0, startOffset = "") {
      return String(event?.event_id || event?._cursor || (startOffset !== "" ? `${startOffset}-${index}` : deps.eventIdentity(event))).trim();
    }

    function appendRealtimeEvents(events, startOffset = "") {
      const accepted = [];
      events.forEach((event, index) => {
        const cursor = realtimeEventCursor(event, index, startOffset);
        const dedupeKey = event?.event_id ? `event_id:${event.event_id}` : `event:${deps.eventIdentity(event)}`;
        if (seenRealtimeEvents.has(dedupeKey)) return;
        seenRealtimeEvents.add(dedupeKey);
        if (!event._uiKey) event._uiKey = `event-${cursor || index}-${event.type || "event"}`;
        deps.rememberEventCursorFromEvent(event);
        accepted.push(event);
      });
      if (!accepted.length) return accepted;
      allEvents.push(...accepted);
      appendRealtimeConversationEvents(accepted);
      deps.removeOptimisticEventsMatchedBy(accepted);
      invalidateRealtimeTaskDetails(accepted);
      if (accepted.some(event => event.type === "task_status_changed")) {
        void deps.refreshTaskMemosIfOpen?.().catch(err => console.warn("Failed to refresh task memos", err));
      }
      deps.realtimeDebug("events.accepted", {
        count: accepted.length,
        start_offset: startOffset,
        last_event_id: getLastEventId(),
        types: accepted.slice(0, 8).map(event => event.type || "")
      });
      return accepted;
    }

    async function pollEvents() {
      let res;
      const lastEventId = getLastEventId();
      const offset = getOffset();
      const params = lastEventId ? { last_event_id: lastEventId } : { offset: String(offset) };
      deps.realtimeDebug("poll.request", { params });
      try {
        res = await deps.fetchWithTimeout(deps.apiUrl("/api/events", params));
      } catch (err) {
        deps.realtimeDebug("poll.fetch_error", { params, error: err?.message || String(err) });
        if (!lastEventId && offset < 0) {
          await initializeEventTailOffset();
          return [];
        }
        throw err;
      }
      if (!res.ok) {
        deps.realtimeDebug("poll.http_error", { params, status: res.status, status_text: res.statusText });
        if (lastEventId) {
          setLastEventId("");
          setOffset(-1);
          setEventTailInitialized(false);
          deps.clearStoredLastEventId?.();
          await initializeEventTailOffset();
          return [];
        }
        if (offset < 0) await initializeEventTailOffset();
        return [];
      }
      const payload = await deps.readJsonResponse(res, "Failed to poll events");
      const startOffset = getOffset();
      deps.rememberEventCursor(payload);
      const accepted = appendRealtimeEvents(payload.events || [], startOffset);
      deps.realtimeDebug("poll.response", {
        params,
        event_count: (payload.events || []).length,
        accepted_count: accepted.length,
        response_last_event_id: payload.last_event_id || "",
        response_offset: payload.offset,
        snapshot_event_id: payload.snapshot_event_id || payload.snapshot_offset || "",
        has_more: Boolean(payload.has_more)
      });
      return accepted;
    }

    function latestKnownEventOrder() {
      const orders = allEvents.map(event => deps.conversationEventOrder(event)).filter(Number.isFinite);
      return orders.length ? Math.max(...orders) : -1;
    }

    function latestScopedTaskEvent(taskId, target, type, afterOrder = null) {
      const events = deps.taskEvents(taskId)
        .filter(event => event.type === type && deps.eventMatchesAgent(event, target))
        .filter(event => afterOrder == null || deps.conversationEventOrder(event) > afterOrder)
        .sort((left, right) => deps.conversationEventOrder(left) - deps.conversationEventOrder(right));
      return events.length ? events[events.length - 1] : null;
    }

    function compactResetLooksComplete(taskId, agentId, previousSessionId, afterOrder) {
      const compactEvent = latestScopedTaskEvent(taskId, agentId, "backend_session_compact_reset", afterOrder);
      const startedEvent = latestScopedTaskEvent(taskId, agentId, "backend_started", afterOrder);
      const backendSession = conversationBackendSession(taskId, agentId);
      const history = Array.isArray(backendSession?.history) ? backendSession.history : [];
      const archived = Boolean(previousSessionId && history.some(item => item.backend_session_id === previousSessionId));
      const currentSessionId = String(backendSession?.id || "");
      const hasNewConversationSession = Boolean(currentSessionId && currentSessionId !== previousSessionId);
      const agent = deps.agentStatusSession(taskId, agentId);
      const statusSessionId = String(agent?.backend_session_id || "");
      const hasNewStatusSession = Boolean(statusSessionId && statusSessionId !== previousSessionId && String(agent?.session_status || "").toLowerCase() === "active");
      return Boolean(compactEvent && (startedEvent || archived || hasNewConversationSession || hasNewStatusSession || backendSession?.compact_summary));
    }

    function renderConversationFilters() {
      if (!conversationFiltersEl) return;
      const active = getActiveTab() === "conversation";
      conversationFiltersEl.classList.toggle("hidden", !active);
      if (!active) return;
      const task = deps.selectedTask();
      const counts = task ? deps.conversationFilterCounts(task.id) : {};
      const previousDetails = conversationFiltersEl.querySelector?.("#conversation-filter-details");
      if (previousDetails instanceof HTMLDetailsElement) conversationFiltersOpen = previousDetails.open;
      const html = deps.renderConversationFiltersHtml({
        active: true,
        filters: conversationFilters,
        counts,
        filterOptions: deps.conversationFilterOptions,
        open: conversationFiltersOpen
      });
      conversationFiltersEl.innerHTML = html;
      conversationFiltersEl.classList.toggle("empty", !html.trim());
      const details = conversationFiltersEl.querySelector?.("#conversation-filter-details");
      details?.addEventListener("toggle", () => {
        conversationFiltersOpen = Boolean(details.open);
        if (conversationFiltersOpen) syncConversationFilterMenuOffset();
      });
      if (details?.open) syncConversationFilterMenuOffset();
    }

    function syncConversationFilterMenuOffset() {
      const composer = conversationFiltersEl?.closest?.(".composer");
      if (!composer || typeof composer.getBoundingClientRect !== "function") return;
      const rect = composer.getBoundingClientRect();
      const offset = Math.max(50, Math.ceil(rect.height) + 8);
      conversationFiltersEl.style.setProperty("--conversation-filter-menu-bottom", `${offset}px`);
    }

    function closeConversationFilters() {
      conversationFiltersOpen = false;
      const details = conversationFiltersEl?.querySelector?.("#conversation-filter-details");
      if (details instanceof HTMLDetailsElement) details.open = false;
    }

    documentRef.addEventListener?.("pointerdown", event => {
      if (!conversationFiltersOpen) return;
      const target = event.target instanceof Element ? event.target : null;
      if (target && conversationFiltersEl?.contains(target)) return;
      closeConversationFilters();
    });

    function renderConversation(taskId) {
      copyTextByKey.clear();
      const stateValue = deps.conversationState(taskId);
      if (!stateValue.initialized || stateValue.loading && !stateValue.events.length) {
        return deps.renderConversationPanelHtml({ loading: true });
      }
      if (stateValue.error && !stateValue.events.length) {
        return deps.renderConversationPanelHtml({ error: stateValue.error });
      }
      const events = deps.taskConversationEvents(taskId);
      if (!events.length && !stateValue.hasMore) {
        const timer = deps.renderTurnTimer(taskId);
        const metricsDock = timer ? "" : renderPromptMetricsDock(taskId);
        return deps.renderConversationPanelHtml({
          target: backendTarget(),
          timerHtml: timer,
          metricsDockHtml: metricsDock
        });
      }
      const timer = deps.renderTurnTimer(taskId);
      const metricsDock = timer ? "" : renderPromptMetricsDock(taskId);
      return deps.renderConversationPanelHtml({
        hasMore: stateValue.hasMore,
        loadingOlder: stateValue.loading,
        eventsHtml: events.map(deps.renderTimelineEvent).join(""),
        timerHtml: timer,
        metricsDockHtml: metricsDock
      });
    }

    return Object.freeze({
      appendRealtimeConversationEvents,
      appendRealtimeEvents,
      captureContextScrollState,
      compactResetLooksComplete,
      contextDetail,
      conversationBackendSession,
      ensureActiveTabData,
      ensureConversationLoaded,
      finalDetail,
      initializeEventTailOffset,
      latestKnownEventOrder,
      loadContextDetail,
      loadConversationPage,
      loadFinalDetail,
      loadLogPage,
      loadOlderConversation,
      loadOlderLogs,
      logState,
      maybeRefreshConversationBackendSessionFallback,
      pollEvents,
      prepareRealtimeCatchupBaseline,
      promptMetricsKey,
      promptMetricsState,
      refreshConversationBackendSession,
      renderConversation,
      renderConversationFilters,
      renderPromptMetricsDock,
      renderPromptMetricsPanel,
      renderPromptMetricsPopover,
      renderRawPromptSection,
      restoreContextScrollState
    });
  }

  window.AHAConversationController = Object.freeze({ createConversationController });
})();
