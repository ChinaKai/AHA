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

    return Object.freeze({
      renderConversationFiltersHtml,
      renderConversationPanelHtml,
      renderFinalPanelHtml,
      renderHardwareIoPanelHtml,
      renderLogsPanelHtml,
      renderContextPanelHtml
    });
  }

  window.AHAConversationPanel = Object.freeze({ createConversationPanelHelpers });
})();
