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
      return `<pre>${escapeHtml(detail.result || "No saved result.")}</pre>`;
    }

    function renderLogsPanelHtml(state = {}) {
      const older = state.hasMore
        ? `<button class="load-older" type="button" data-load-older-log="true">${state.loading ? "Loading..." : "Load older logs"}</button>`
        : "";
      const body = state.initialized ? localizeTimestampText(state.text || "No logs yet.") : "Loading logs...";
      return `<div class="log-view">${older}<pre>${escapeHtml(body)}</pre></div>`;
    }

    function renderHardwareBridgeToolbarHtml(state = {}) {
      if (!state.device) return "";
      const bridge = state.bridge || {};
      const paused = Boolean(bridge.paused);
      const alive = Boolean(bridge.alive);
      // Reuse the task-status pill vocabulary so the bridge state reads like a status.
      const variant = state.readOnly ? "idle" : paused ? "awaiting_user" : alive ? "running" : "idle";
      const label = state.readOnly ? "read-only" : paused ? "paused" : alive ? "live" : "connecting…";
      const toggle = state.readOnly
        ? ""
        : `<button type="button" class="hardware-bridge-toggle" data-hardware-bridge-action="${paused ? "resume" : "pause"}">${paused ? "Resume" : "Pause"}</button>`;
      // Identity (device + status) sits together on the left; the only action up here is
      // Pause, kept apart on the right.
      return `
        <div class="hardware-bridge-bar">
          <span class="hardware-bridge-identity">
            <span class="hardware-bridge-device" title="${escapeHtml(String(state.device))}">${escapeHtml(String(state.device))}</span>
            <span class="status hardware-bridge-status ${variant}">${escapeHtml(label)}</span>
          </span>
          <span class="hardware-bridge-controls">${toggle}</span>
        </div>
      `;
    }

    // Sits directly above the composer (the input box). A single scrollable quick-key row.
    // Input is always line mode (type in the box, live preview in the terminal, Send = Enter),
    // identical on desktop and mobile. The raw per-keystroke toggle is hidden for now; the
    // keys below still send their bytes live (Ctrl-C interrupt, arrows for shell history, etc.).
    function renderHardwareBottomBarHtml(state = {}) {
      if (!state.device || state.readOnly) return "";
      const keys = [
        ["enter", "⏎"], ["esc", "Esc"], ["tab", "Tab"], ["ctrl-c", "^C"], ["ctrl-d", "^D"],
        ["ctrl-z", "^Z"], ["ctrl-l", "^L"], ["up", "↑"], ["down", "↓"], ["left", "←"],
        ["right", "→"], ["home", "Home"], ["end", "End"]
      ]
        .map(([key, glyph]) => `<button type="button" class="hardware-key-btn" data-hardware-key="${key}" title="Send ${escapeHtml(key)}">${escapeHtml(glyph)}</button>`)
        .join("");
      return `
        <div class="hardware-keybar">
          <span class="hardware-keybar-keys hardware-accessory-keys">${keys}</span>
        </div>
      `;
    }

    // The board speaks like a terminal: carriage returns overwrite the current line,
    // backspaces erase, and ANSI escape sequences colour/move the cursor. A <pre> would
    // render those raw (countdown frames pile up, escapes show as garbage), so collapse
    // them to the text a terminal would actually display.
    function decodeTerminalText(text) {
      let s = String(text || "");
      s = s.replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]/g, "");
      s = s.replace(/\x1b[@-Z\\\]^_]/g, "");
      s = s.replace(/\r\n/g, "\n");
      if (s.indexOf("\r") === -1 && s.indexOf("\b") === -1) return s;
      const lines = [];
      let line = "";
      let col = 0;
      for (let i = 0; i < s.length; i++) {
        const ch = s[i];
        if (ch === "\n") { lines.push(line); line = ""; col = 0; }
        else if (ch === "\r") { col = 0; }
        else if (ch === "\b") { col = Math.max(0, col - 1); }
        else { line = line.slice(0, col) + ch + line.slice(col + 1); col += 1; }
      }
      lines.push(line);
      return lines.join("\n");
    }

    function renderHardwareIoPanelHtml(state = {}) {
      if (!state.initialized && state.loading) return '<div class="empty">Loading hardware I/O...</div>';
      const toolbar = renderHardwareBridgeToolbarHtml(state);
      const bottomBar = renderHardwareBottomBarHtml(state);
      // Raw mode captures keystrokes on the composer textarea (persistent, flood-proof),
      // not on the terminal — so the <pre> is display-only in every mode.
      const raw = Boolean(state.rawMode) && !state.readOnly && Boolean(state.device);
      const viewClass = raw ? "hardware-io-view hardware-io-view-raw" : "hardware-io-view";
      // Line mode: mirror the composer's current text live at the prompt, so the terminal and
      // the input box stay in sync (you see what you're typing in context). The board echoes
      // the real line once you Send (= Enter), which replaces this preview.
      const pending = !raw ? String(state.pendingInput || "") : "";
      const pendingHtml = pending ? `<span class="hio-pending">${escapeHtml(pending)}</span>` : "";
      const events = Array.isArray(state.events) ? state.events : [];
      if (!events.length) {
        return `<div class="${viewClass}">${toolbar}<pre class="hardware-terminal">${pendingHtml}</pre>${bottomBar}</div>`;
      }
      const parts = [];
      let rxBuf = "";
      const flushRx = () => {
        if (!rxBuf) return;
        // Decode the RX run as a whole so carriage returns/backspaces that straddle chunk
        // boundaries still overwrite correctly.
        parts.push(`<span class="hio-rx">${escapeHtml(decodeTerminalText(rxBuf))}</span>`);
        rxBuf = "";
      };
      for (const item of events) {
        const direction = String(item.direction || "system").toLowerCase();
        const data = String(item.data || "");
        const truncated = item.truncated ? " …" : "";
        const ts = escapeHtml(localizeTimestampText(item.ts || ""));
        if (direction === "rx") {
          rxBuf += data + (item.truncated ? " …" : "");
          continue;
        }
        const rawSource = String(item.source || "");
        if (direction === "tx") {
          // The board echoes interactively-typed commands back over RX, so also rendering
          // our own local TX would show every command twice. Suppress the local echo for
          // user/agent sends and rely on the device echo; keep non-echoed sends visible
          // (e.g. armed-rule auto-reactions, which fire faster than any echo round-trip).
          if (rawSource === "web" || rawSource === "interactive") continue;
          flushRx();
          const source = escapeHtml(rawSource);
          const title = [ts, source].filter(Boolean).join(" · ");
          parts.push(`<span class="hio-tx" title="${title}">⮞ ${escapeHtml(decodeTerminalText(data))}${truncated}</span>`);
          continue;
        }
        flushRx();
        const source = rawSource ? escapeHtml(rawSource) : "";
        const tag = source ? `${direction} ${source}` : direction;
        parts.push(`<span class="hio-sys" title="${ts}">‹${escapeHtml(tag)}› ${escapeHtml(data)}${truncated}\n</span>`);
      }
      flushRx();
      return `<div class="${viewClass}">${toolbar}<pre class="hardware-terminal">${parts.join("")}${pendingHtml}</pre>${bottomBar}</div>`;
    }

    function renderContextPanelHtml({ rawPromptHtml = "", promptMetricsHtml = "" } = {}) {
      return `
        <div class="context-view">
          ${rawPromptHtml}
          ${promptMetricsHtml}
        </div>
      `;
    }

    function evidenceListHtml(values = [], { limit = 8, empty = "none", code = false } = {}) {
      const items = Array.isArray(values) ? values.filter(Boolean).slice(0, limit) : [];
      if (!items.length) return `<span class="task-evidence-muted">${escapeHtml(empty)}</span>`;
      if (code) {
        return items.map(value => `<code title="${escapeHtml(value)}">${escapeHtml(value)}</code>`).join("");
      }
      return items.map(value => `<span class="task-evidence-chip">${escapeHtml(value)}</span>`).join("");
    }

    function renderEvidenceSuggestions(suggestions = []) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const items = Array.isArray(suggestions) ? suggestions.filter(item => item && typeof item === "object").slice(0, 8) : [];
      if (!items.length) {
        return `<div class="task-evidence-empty">${escapeHtml(t("task.context_evidence_no_suggestions", "No maintenance suggestions."))}</div>`;
      }
      return items.map(item => {
        const label = [item.action, item.target, item.reason].filter(Boolean).join(" / ");
        const targetPath = item.target_path
          ? `<div class="task-evidence-line">${evidenceListHtml([item.target_path], { limit: 1, empty: "-", code: true })}</div>`
          : "";
        const policy = item.write_policy
          ? `<div class="task-evidence-line">${escapeHtml(item.write_policy)}</div>`
          : "";
        const validation = Array.isArray(item.validation) && item.validation.length
          ? `<div class="task-evidence-line">${evidenceListHtml(item.validation, { limit: 3, empty: "-", code: true })}</div>`
          : "";
        const execution = item.execution || {};
        const executionLine = execution.state || execution.next_step
          ? `<div class="task-evidence-line">${escapeHtml([execution.state, execution.next_step].filter(Boolean).join(" · "))}</div>`
          : "";
        return `
          <div class="task-evidence-suggestion">
            <strong>${escapeHtml(label || "-")}</strong>
            ${targetPath}
            ${policy}
            ${executionLine}
            <div>${evidenceListHtml(item.source_files || item.files || [], { limit: 6, empty: "-", code: true })}</div>
            ${validation}
          </div>
        `;
      }).join("");
    }

    function renderRoutingHealth(health = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      if (!health || typeof health !== "object" || !health.status) {
        return `<div class="task-evidence-empty">${escapeHtml(t("task.context_evidence_no_routing_health", "No routing health yet."))}</div>`;
      }
      return `
        <div class="task-evidence-line">${evidenceListHtml([health.status], { limit: 1, empty: "-" })}</div>
        <div class="task-evidence-line">${evidenceListHtml(health.downrank_paths || [], { limit: 6, empty: "-", code: true })}</div>
        <div class="task-evidence-line">${evidenceListHtml(health.prioritize_paths || [], { limit: 6, empty: "-", code: true })}</div>
      `;
    }

    function renderEvidenceQueries(payload = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const records = Array.isArray(payload.records) ? payload.records : [];
      const queries = records
        .filter(item => item?.type === "project_map_query")
        .slice(-6)
        .reverse();
      if (!queries.length) {
        return `<div class="task-evidence-empty">${escapeHtml(t("task.context_evidence_no_queries", "No map queries recorded."))}</div>`;
      }
      return queries.map(item => {
        const map = item.map || {};
        return `
          <div class="task-evidence-query">
            <strong>${escapeHtml(map.query || "-")}</strong>
            <span>${escapeHtml(String(map.total_matches ?? 0))} matches</span>
            <div>${evidenceListHtml(map.files || [], { limit: 6, empty: "-", code: true })}</div>
          </div>
        `;
      }).join("");
    }

    function evidenceStatusText(status = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const state = status.state || "observing";
      const labels = {
        helped: t("task.context_evidence_status_helped", "KB helped"),
        needs_repair: t("task.context_evidence_status_needs_repair", "Needs KB repair"),
        no_evidence: t("task.context_evidence_status_no_evidence", "No evidence yet"),
        observing: t("task.context_evidence_status_observing", "Observing"),
        stale: t("task.context_evidence_status_stale", "KB/map stale")
      };
      return labels[state] || status.label || state;
    }

    function evidenceSourceText(source) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const labels = {
        after_agent_turn: t("task.context_evidence_source_after_turn", "after agent turn"),
        after_turn_runtime_distill: t("task.context_evidence_source_after_turn", "after agent turn"),
        before_agent_prompt: t("task.context_evidence_source_before_prompt", "before agent prompt"),
        context_pack_before_prompt: t("task.context_evidence_source_context_pack", "context pack"),
        map_query_on_demand: t("task.context_evidence_source_map_query", "map query")
      };
      return labels[source] || source;
    }

    function renderEvidenceSummary(payload = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const summary = payload.summary || {};
      const status = summary.status || {};
      const nextAction = summary.next_action || {};
      const sources = Array.isArray(summary.evidence_sources) ? summary.evidence_sources : [];
      const generatedWhen = Array.isArray(summary.generated_when) ? summary.generated_when : [];
      const sourceText = sources.length
        ? sources.map(evidenceSourceText).join(" · ")
        : generatedWhen.map(evidenceSourceText).join(" · ");
      const statusState = String(status.state || "observing").replace(/[^a-z0-9_-]/gi, "");
      const targetPath = nextAction.target_path
        ? `<div class="task-evidence-line">${evidenceListHtml([nextAction.target_path], { limit: 1, empty: "-", code: true })}</div>`
        : "";
      return `
        <div class="task-evidence-summary task-evidence-summary-${escapeHtml(statusState)}">
          <div>
            <span>${escapeHtml(t("task.context_evidence_scope", "Scope"))}</span>
            <strong>${escapeHtml(t("task.context_evidence_scope_task", "This token-saving task"))}</strong>
            <p>${escapeHtml(t("task.context_evidence_feedback_mode", "AHA runtime inference from prompts, map queries, commands, and changed files."))}</p>
          </div>
          <div>
            <span>${escapeHtml(t("task.context_evidence_kb_effect", "KB effect"))}</span>
            <strong>${escapeHtml(evidenceStatusText(status))}</strong>
            <p>${escapeHtml(status.description || t("task.context_evidence_status_unknown", "No task-level KB impact summary yet."))}</p>
          </div>
          <div>
            <span>${escapeHtml(t("task.context_evidence_next_action", "Next action"))}</span>
            <strong>${escapeHtml(nextAction.label || t("task.context_evidence_no_action", "No maintenance action"))}</strong>
            <p>${escapeHtml([nextAction.state, nextAction.reason].filter(Boolean).join(" · ") || "-")}</p>
            ${targetPath}
          </div>
          <div>
            <span>${escapeHtml(t("task.context_evidence_sources", "Evidence sources"))}</span>
            <strong>${escapeHtml(sourceText || "-")}</strong>
            <p>${escapeHtml(t("task.context_evidence_latest", "Latest update"))}: ${escapeHtml(summary.latest_record_created_at || "-")}</p>
          </div>
        </div>
      `;
    }

    function renderContextEvidencePanelHtml(detail = null) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      if (!detail) return `<div class="empty">${escapeHtml(t("task.context_evidence_loading", "Loading context evidence..."))}</div>`;
      if (detail.error) return `<div class="empty">${escapeHtml(detail.error)}</div>`;
      if (detail.loading) return `<div class="empty">${escapeHtml(t("task.context_evidence_loading", "Loading context evidence..."))}</div>`;
      const payload = detail.payload || { records: [], latest_result: null, maintenance_suggestions: [], maintenance_plan: [] };
      const latest = payload.latest_result || {};
      const diagnostics = latest.map_diagnostics || {};
      const routingHealth = payload.routing_health || latest.routing_health || {};
      const count = Number(payload.count || 0);
      const maintenanceItems = Array.isArray(payload.maintenance_plan) && payload.maintenance_plan.length
        ? payload.maintenance_plan
        : payload.maintenance_suggestions || [];
      if (!count) {
        return `
          <div class="context-evidence-view">
            <div class="task-evidence-head">
              <h3>${escapeHtml(t("task.context_evidence", "Context evidence"))}</h3>
              <button type="button" data-context-evidence-refresh>${escapeHtml(t("common.refresh", "Refresh"))}</button>
            </div>
            <div class="task-evidence-empty">${escapeHtml(t("task.context_evidence_empty", "No context evidence yet."))}</div>
          </div>
        `;
      }
      return `
        <div class="context-evidence-view">
          <div class="task-evidence-head">
            <div>
              <h3>${escapeHtml(t("task.context_evidence", "Context evidence"))}</h3>
              <div class="meta">${escapeHtml(t("task.context_evidence_count", "{count} evidence records").replace("{count}", String(count)))}</div>
            </div>
            <button type="button" data-context-evidence-refresh>${escapeHtml(t("common.refresh", "Refresh"))}</button>
          </div>
          ${renderEvidenceSummary(payload)}
          <div class="task-evidence-grid">
            <div>
              <span>${escapeHtml(t("task.context_evidence_signals", "Signals"))}</span>
              <div>${evidenceListHtml(latest.signals || [], { empty: t("task.context_evidence_none", "none") })}</div>
            </div>
            <div>
              <span>${escapeHtml(t("task.context_evidence_actions", "Actions"))}</span>
              <div>${evidenceListHtml(latest.crud_actions || [], { empty: t("task.context_evidence_none", "none") })}</div>
            </div>
            <div>
              <span>${escapeHtml(t("task.context_evidence_actual", "Actual files"))}</span>
              <div>${evidenceListHtml(latest.actual_files || diagnostics.actual_files || [], { limit: 8, empty: "-", code: true })}</div>
            </div>
            <div>
              <span>${escapeHtml(t("task.context_evidence_referenced", "Referenced files"))}</span>
              <div>${evidenceListHtml(latest.referenced_files || diagnostics.referenced_files || [], { limit: 8, empty: "-", code: true })}</div>
            </div>
          </div>
          <div class="task-evidence-block">
            <strong>${escapeHtml(t("task.context_evidence_suggestions", "Maintenance suggestions"))}</strong>
            ${renderEvidenceSuggestions(maintenanceItems)}
          </div>
          <div class="task-evidence-section-title">
            <strong>${escapeHtml(t("task.context_evidence_diagnostics", "Diagnostic details"))}</strong>
          </div>
          <div class="task-evidence-block">
            <strong>${escapeHtml(t("task.context_evidence_routing", "Routing health"))}</strong>
            ${renderRoutingHealth(routingHealth)}
          </div>
          <div class="task-evidence-block">
            <strong>${escapeHtml(t("task.context_evidence_map", "Map diagnostics"))}</strong>
            <div class="task-evidence-line">${evidenceListHtml(diagnostics.gap_signals || [], { empty: t("task.context_evidence_none", "none") })}</div>
            <div class="task-evidence-line">${evidenceListHtml(diagnostics.missing_files || [], { limit: 8, empty: "-", code: true })}</div>
            <div class="task-evidence-line">${evidenceListHtml(diagnostics.stale_path_hints || [], { limit: 8, empty: "-", code: true })}</div>
          </div>
          <div class="task-evidence-block">
            <strong>${escapeHtml(t("task.context_evidence_queries", "Map queries"))}</strong>
            ${renderEvidenceQueries(payload)}
          </div>
        </div>
      `;
    }

    return Object.freeze({
      renderConversationFiltersHtml,
      renderConversationPanelHtml,
      renderFinalPanelHtml,
      renderHardwareIoPanelHtml,
      renderLogsPanelHtml,
      renderContextPanelHtml,
      renderContextEvidencePanelHtml
    });
  }

  window.AHAConversationPanel = Object.freeze({ createConversationPanelHelpers });
})();
