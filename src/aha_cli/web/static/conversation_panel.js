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
      if (view.loading) return `<div class="empty loading">Loading conversation...</div>`;
      if (view.error) {
        return `<div class="empty">Conversation unavailable. Realtime updates will start from the latest event offset.<br><code>${escapeHtml(view.error)}</code></div>`;
      }
      if (!view.eventsHtml && !view.hasMore && !view.virtual) {
        const empty = `<div class="empty">No conversation for ${escapeHtml(view.target || "main")} yet.</div>`;
        return `<div class="conversation timeline">${empty}${view.timerHtml || ""}${view.metricsDockHtml || ""}</div>`;
      }
      const older = view.hasMore
        ? `<button class="load-older" type="button" data-load-older="true">${view.loadingOlder ? "Loading..." : "Load older"}</button>`
        : "";
      if (view.virtual) {
        // Virtualized host: the actual message rows are mounted by mountVirtualConversation.
        return `<div class="conversation timeline">${older}<div class="vl-host" data-vl-task="${escapeHtml(view.virtualTaskId || "")}" data-vl-target="${escapeHtml(view.virtualTarget || "")}"></div>${view.timerHtml || ""}${view.metricsDockHtml || ""}</div>`;
      }
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
      if (!state.endpoint && !state.device) return "";
      const bridge = state.bridge || {};
      const paused = Boolean(bridge.paused);
      const alive = Boolean(bridge.alive);
      const owner = bridge.device_owner || {};
      const occupied = Boolean(bridge.error && owner.pid);
      const canTakeover = Boolean(occupied && owner.can_terminate === true);
      // Reuse the task-status pill vocabulary so the bridge state reads like a status.
      const variant = state.readOnly ? "idle" : occupied ? "failed" : paused ? "awaiting_user" : alive ? "running" : "idle";
      const label = state.readOnly
        ? "read-only"
        : occupied
          ? "occupied"
          : paused ? "paused" : alive ? "live" : "connecting…";
      const toggle = state.readOnly
        ? ""
        : `<button type="button" class="hardware-bridge-toggle" data-hardware-bridge-action="${paused ? "resume" : "pause"}">${paused ? "Resume" : "Pause"}</button>`;
      const takeover = state.readOnly
        ? ""
        : `<button type="button" class="hardware-bridge-toggle danger" data-hardware-takeover="true" data-owner-pid="${escapeHtml(String(owner.pid || ""))}" data-owner-process="${escapeHtml(String(owner.process || "process"))}" ${canTakeover ? "" : "hidden"}>Take over</button>`;
      const clear = '<button type="button" class="hardware-bridge-toggle" data-hardware-terminal-action="clear">Clear</button>';
      const ownerIdentity = `${owner.process || "process"} (PID ${owner.pid || "unknown"}${owner.uid === null || owner.uid === undefined ? "" : `, UID ${owner.uid}`})`;
      const detail = occupied
        ? canTakeover
          ? `Serial device is in use by ${ownerIdentity}. Take over sends SIGTERM only.`
          : `Serial device is in use by ${ownerIdentity}. AHA has no permission to terminate it; close it manually.`
        : bridge.error ? String(bridge.error) : "";
      const transports = Array.isArray(state.transports) ? state.transports : [];
      const transportPicker = transports.length > 1
        ? `<span class="hardware-transport-picker">${transports.map(item => (
            `<button type="button" class="hardware-transport-btn ${item === state.transport ? "active" : ""}" data-hardware-transport="${escapeHtml(item)}">${escapeHtml(item === "serial" ? "Serial" : "Network")}</button>`
          )).join("")}</span>`
        : `<span class="hardware-transport-label">${escapeHtml(state.transport === "network" ? "Network" : "Serial")}</span>`;
      // Identity (transport + endpoint + status) stays left; local-screen and bridge
      // controls stay together on the right.
      return `
        <div class="hardware-bridge-bar">
          <span class="hardware-bridge-identity">
            ${transportPicker}
            <span class="hardware-bridge-device" title="${escapeHtml(String(state.endpoint || state.device))}">${escapeHtml(String(state.endpoint || state.device))}</span>
            <span class="status hardware-bridge-status ${variant}" data-hardware-terminal-status>${escapeHtml(label)}</span>
          </span>
          <span class="hardware-bridge-controls">${clear}${takeover}${toggle}</span>
          <div class="hardware-bridge-error" data-hardware-owner-detail ${detail ? "" : "hidden"}>${escapeHtml(detail)}</div>
        </div>
      `;
    }

    // A single scrollable accessory-key row below xterm. These buttons send raw bytes live
    // and cover keys mobile soft keyboards often omit (Esc, Tab, Ctrl, arrows, Home/End).
    function renderHardwareBottomBarHtml(state = {}) {
      if (!state.device || state.readOnly) return "";
      const keys = (window.AHATerminalUi?.terminalKeys?.() || [])
        .map(item => `<button type="button" class="hardware-key-btn" data-hardware-key="${escapeHtml(item.name)}" title="Send ${escapeHtml(item.name)}">${escapeHtml(item.label)}</button>`)
        .join("");
      return `
        <div class="hardware-keybar">
          <span class="hardware-keybar-keys hardware-accessory-keys">${keys}</span>
        </div>
      `;
    }

    function renderHardwareIoPanelHtml(state = {}) {
      if (!state.initialized && state.loading) return '<div class="empty loading">Loading hardware I/O...</div>';
      const toolbar = renderHardwareBridgeToolbarHtml(state);
      const bottomBar = renderHardwareBottomBarHtml(state);
      const key = `${String(state.taskId || "")}:${String(state.transport || "serial")}`;
      return `
        <div class="hardware-io-view" data-hardware-terminal-root data-hardware-terminal-key="${escapeHtml(key)}">
          ${toolbar}
          <div class="hardware-terminal-xterm aha-terminal-xterm" data-hardware-terminal-xterm tabindex="0" aria-label="Hardware terminal"></div>
          ${bottomBar}
        </div>
      `;
    }

    function renderBrowserPanelHtml(task = {}) {
      const policy = task?.browser_control && typeof task.browser_control === "object"
        ? task.browser_control
        : {};
      const nativeRequested = String(policy.display || "native") === "native";
      const userChrome = String(policy.runtime || "playwright") === "user_chrome";
      const deviceMode = String(policy.device_mode || "desktop") === "mobile" ? "mobile" : "desktop";
      const runtime = userChrome ? "user_chrome" : "playwright";
      const display = nativeRequested ? "native" : "embedded";
      const proxyMode = ["inherit", "custom"].includes(String(policy.proxy_mode || ""))
        ? String(policy.proxy_mode)
        : "direct";
      const downloads = String(policy.downloads || "deny") === "allow" ? "allow" : "deny";
      const uploads = String(policy.uploads || "deny") === "allow" ? "allow" : "deny";
      const selected = (value, expected) => value === expected ? " selected" : "";
      return `
        <div class="browser-session-view" data-browser-session-root data-browser-task-id="${escapeHtml(task?.id || "")}" data-browser-start-url="${escapeHtml(policy.start_url || "")}" data-browser-display="${nativeRequested ? "native" : "embedded"}" data-browser-runtime="${userChrome ? "user_chrome" : "playwright"}" data-browser-config-device-mode="${deviceMode}">
          <div class="browser-management-toolbar">
            <span class="browser-session-status idle" data-browser-status role="status" aria-label="Browser status: connecting" title="connecting"></span>
            <span class="browser-lifecycle-controls">
              <button class="browser-toolbar-icon-button" type="button" data-browser-lifecycle="start" title="Start" aria-label="Start" hidden><span aria-hidden="true">▶</span></button>
              <button class="browser-toolbar-icon-button" type="button" data-browser-lifecycle="close" title="Close" aria-label="Close"><span aria-hidden="true">■</span></button>
            </span>
          </div>
          <div class="browser-frame-area" data-browser-embedded-session>
            <div class="browser-frame-stage" data-browser-input-surface tabindex="0" aria-label="Shared browser viewport" aria-busy="true">
              <img data-browser-frame alt="Shared browser viewport" draggable="false" hidden>
              <div class="browser-frame-empty" data-browser-empty>Starting shared browser…</div>
              <textarea class="browser-page-keyboard-input" data-browser-keyboard-input rows="1" tabindex="-1" inputmode="text" enterkeyhint="enter" autocomplete="off" autocapitalize="none" spellcheck="false" aria-label="Browser page keyboard input"></textarea>
            </div>
          </div>
          <div class="browser-session-error" data-browser-error hidden></div>
        </div>
      `;
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
        return items.map(value => `<code title="${escapeHtml(value)}">${escapeHtml(localizeTimestampText(value))}</code>`).join("");
      }
      return items.map(value => `<span class="task-evidence-chip">${escapeHtml(localizeTimestampText(value))}</span>`).join("");
    }

    function renderEvidenceSuggestions(suggestions = [], { limit = 8 } = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const items = Array.isArray(suggestions)
        ? suggestions.filter(item => item && typeof item === "object").slice(0, limit)
        : [];
      if (!items.length) {
        return `<div class="task-evidence-empty">${escapeHtml(t("task.context_evidence_no_suggestions", "No KB maintenance actions needed."))}</div>`;
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

    function evidenceStatusText(status = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const state = status.state || "observing";
      const labels = {
        helped: t("task.context_evidence_status_helped", "KB helped"),
        growth_pending: t("task.context_evidence_status_growth_pending", "KB growth pending"),
        needs_repair: t("task.context_evidence_status_needs_repair", "Needs KB repair"),
        no_evidence: t("task.context_evidence_status_no_evidence", "No evidence yet"),
        observing: t("task.context_evidence_status_observing", "Observing"),
        stale: t("task.context_evidence_status_stale", "KB/nav stale")
      };
      return labels[state] || status.label || state;
    }

    function evidenceFeedbackModeText(mode) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      if (mode === "agent_feedback_plus_runtime") {
        return t("task.context_evidence_feedback_mode_agent", "Agent KB feedback plus AHA runtime inference.");
      }
      return t("task.context_evidence_feedback_mode", "AHA runtime inference from prompts, commands, and changed files.");
    }

    function evidenceLoopStateText(loop = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const labels = {
        closed_loop: t("task.context_evidence_loop_closed", "Task loop closed"),
        helped: t("task.context_evidence_loop_helped", "KB helped this task"),
        hit_only: t("task.context_evidence_loop_hit_only", "KB matched, impact unproven"),
        no_participation: t("task.context_evidence_loop_none", "No KB participation"),
        observing: t("task.context_evidence_loop_observing", "Collecting proof"),
        reuse_verified: t("task.context_evidence_loop_reused", "Cross-task reuse verified"),
        writeback_applied: t("task.context_evidence_loop_writeback", "Knowledge written back"),
        writeback_pending: t("task.context_evidence_loop_writeback_pending", "Knowledge write-back pending")
      };
      return labels[loop.state] || labels.observing;
    }

    function evidenceLoopDescription(loop = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const descriptions = {
        closed_loop: t("task.context_evidence_loop_closed_desc", "KB was used, the task completed, and verified knowledge was written back."),
        helped: t("task.context_evidence_loop_helped_desc", "The task adopted KB guidance, but completion or write-back is not yet proven."),
        hit_only: t("task.context_evidence_loop_hit_only_desc", "AHA routed KB context to the task, but there is no proof that the agent adopted it."),
        no_participation: t("task.context_evidence_loop_none_desc", "This task has not recorded KB routing or usage evidence."),
        observing: t("task.context_evidence_loop_observing_desc", "Evidence exists, but it does not yet prove KB impact."),
        reuse_verified: t("task.context_evidence_loop_reused_desc", "Knowledge written by an earlier task was explicitly reused and verified."),
        writeback_applied: t("task.context_evidence_loop_writeback_desc", "Verified task knowledge was written back; later-task reuse is still pending."),
        writeback_pending: t("task.context_evidence_loop_writeback_pending_desc", "The task found a KB gap, but the required project knowledge update is not complete.")
      };
      return descriptions[loop.state] || descriptions.observing;
    }

    function evidenceLoopStageText(stageId) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      return ({
        routed: t("task.context_evidence_stage_routed", "Matched"),
        used: t("task.context_evidence_stage_used", "Used"),
        solved: t("task.context_evidence_stage_solved", "Solved"),
        writeback: t("task.context_evidence_stage_writeback", "Written back"),
        reused: t("task.context_evidence_stage_reused", "Reused")
      })[stageId] || stageId;
    }

    function evidenceLoopStageStateText(state) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      return ({
        blocked: t("task.context_evidence_stage_blocked", "Waiting for prior proof"),
        complete: t("task.context_evidence_stage_complete", "Proven"),
        not_required: t("task.context_evidence_stage_not_required", "No write-back needed"),
        pending: t("task.context_evidence_stage_pending", "Not proven")
      })[state] || state;
    }

    function renderEvidenceLoop(payload = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const loop = payload.summary?.loop || { state: "no_participation", stages: [] };
      const stages = Array.isArray(loop.stages) ? loop.stages : [];
      const safeState = String(loop.state || "observing").replace(/[^a-z0-9_-]/gi, "");
      return `
        <section class="task-evidence-loop task-evidence-loop-${escapeHtml(safeState)}">
          <div class="task-evidence-loop-head">
            <div>
              <span>${escapeHtml(t("task.context_evidence_loop", "KB positive loop"))}</span>
              <strong>${escapeHtml(evidenceLoopStateText(loop))}</strong>
              <p>${escapeHtml(evidenceLoopDescription(loop))}</p>
            </div>
          </div>
          <div class="task-evidence-loop-track" role="list" aria-label="${escapeHtml(t("task.context_evidence_loop", "KB positive loop"))}">
            ${stages.map((stage, index) => {
              const state = String(stage?.state || "pending");
              const proof = Array.isArray(stage?.proof) ? stage.proof.filter(Boolean) : [];
              return `
                <div class="task-evidence-loop-stage task-evidence-loop-stage-${escapeHtml(state)}" role="listitem">
                  <span class="task-evidence-loop-marker" aria-hidden="true">${state === "complete" ? "✓" : String(index + 1)}</span>
                  <strong>${escapeHtml(evidenceLoopStageText(stage?.id || ""))}</strong>
                  <small>${escapeHtml(evidenceLoopStageStateText(state))}</small>
                  ${proof.length ? `<div>${evidenceListHtml(proof, { limit: 2, empty: "-", code: true })}</div>` : ""}
                </div>
              `;
            }).join("")}
          </div>
          <p class="task-evidence-loop-note">${escapeHtml(t("task.context_evidence_reuse_note", "Cross-task reuse remains unverified until a later task explicitly reports using this knowledge."))}</p>
        </section>
      `;
    }

    function renderAgentKbFeedback(feedback = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const sections = [
        ["helped", t("task.context_evidence_feedback_helped", "Helped")],
        ["reused", t("task.context_evidence_feedback_reused", "Reused")],
        ["stale", t("task.context_evidence_feedback_stale", "Stale")],
        ["missed", t("task.context_evidence_feedback_missed", "Missed")],
        ["updated", t("task.context_evidence_feedback_updated", "Updated")],
        ["pending", t("task.context_evidence_feedback_pending", "Pending")]
      ].filter(([key]) => Array.isArray(feedback[key]) && feedback[key].length);
      if (!sections.length) return "";
      return `
        <div class="task-evidence-block">
          <strong>${escapeHtml(t("task.context_evidence_agent_feedback", "Agent KB feedback"))}</strong>
          <div class="task-evidence-grid">
            ${sections.map(([key, label]) => `
              <div>
                <span>${escapeHtml(label)}</span>
                <div>${evidenceListHtml(feedback[key], { limit: 6, empty: "-", code: key !== "helped" })}</div>
              </div>
            `).join("")}
          </div>
        </div>
      `;
    }

    function renderKbGrowthState(state = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      if (!state || typeof state !== "object" || !state.status || state.status === "not_required") return "";
      const pending = Array.isArray(state.pending) ? state.pending.map(item => item?.target_path || item?.target || "").filter(Boolean) : [];
      const applied = Array.isArray(state.applied) ? state.applied.map(item => item?.target_path || item?.matched_ref || "").filter(Boolean) : [];
      return `
        <div class="task-evidence-block">
          <strong>${escapeHtml(t("task.context_evidence_growth", "KB growth"))}</strong>
          <div class="task-evidence-grid">
            <div>
              <span>${escapeHtml(t("task.context_evidence_growth_status", "Status"))}</span>
              <div>${evidenceListHtml([state.status], { limit: 1, empty: "-" })}</div>
            </div>
            <div>
              <span>${escapeHtml(t("task.context_evidence_growth_pending", "Pending write-back"))}</span>
              <div>${evidenceListHtml(pending, { limit: 6, empty: "-", code: true })}</div>
            </div>
            <div>
              <span>${escapeHtml(t("task.context_evidence_growth_applied", "Applied write-back"))}</span>
              <div>${evidenceListHtml(applied, { limit: 6, empty: "-", code: true })}</div>
            </div>
          </div>
        </div>
      `;
    }

    function renderEvidenceSummary(payload = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const summary = payload.summary || {};
      const status = summary.status || {};
      const nextAction = summary.next_action || {};
      const proof = summary.loop?.proof || {};
      const proofItems = [...(proof.helped || []), ...(proof.adopted_files || []), ...(proof.updated || [])];
      const statusState = String(status.state || "observing").replace(/[^a-z0-9_-]/gi, "");
      const latestUpdate = localizeTimestampText(summary.latest_record_created_at || "-");
      const targetPath = nextAction.target_path
        ? `<div class="task-evidence-line">${evidenceListHtml([nextAction.target_path], { limit: 1, empty: "-", code: true })}</div>`
        : "";
      return `
        <div class="task-evidence-summary task-evidence-summary-${escapeHtml(statusState)}">
          <div>
            <span>${escapeHtml(t("task.context_evidence_key_proof", "Key proof"))}</span>
            <strong>${escapeHtml(proofItems.length ? t("task.context_evidence_proof_available", "Verified observations") : t("task.context_evidence_proof_missing", "No causal proof yet"))}</strong>
            <div>${evidenceListHtml(proofItems, { limit: 5, empty: t("task.context_evidence_proof_missing_help", "A KB match alone is not counted as task impact."), code: true })}</div>
          </div>
          <div>
            <span>${escapeHtml(t("task.context_evidence_health", "KB health"))}</span>
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
            <span>${escapeHtml(t("task.context_evidence_latest", "Latest update"))}</span>
            <strong>${escapeHtml(latestUpdate)}</strong>
            <p>${escapeHtml(evidenceFeedbackModeText(summary.feedback_mode))}</p>
          </div>
        </div>
      `;
    }

    function renderEvidenceFacts(latest = {}, diagnostics = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      return `
        <div class="task-evidence-stack">
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
      `;
    }

    function renderEvidenceDiagnostics(payload = {}, diagnostics = {}, routingHealth = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const gapReasons = Array.isArray(diagnostics.gap_reasons)
        ? diagnostics.gap_reasons.map(item => {
            if (!item || typeof item !== "object") return String(item || "");
            const paths = Array.isArray(item.paths) ? item.paths.filter(Boolean).join(", ") : "";
            return [item.reason, paths].filter(Boolean).join(": ");
          }).filter(Boolean)
        : [];
      void payload;
      return `
        <div class="task-evidence-stack">
          <div class="task-evidence-block">
            <strong>${escapeHtml(t("task.context_evidence_routing", "Routing health"))}</strong>
            ${renderRoutingHealth(routingHealth)}
          </div>
          <div class="task-evidence-block">
            <strong>${escapeHtml(t("task.context_evidence_map", "Navigation diagnostics"))}</strong>
            <div class="task-evidence-line">${evidenceListHtml(gapReasons, { empty: t("task.context_evidence_none", "none") })}</div>
            <div class="task-evidence-line">${evidenceListHtml(diagnostics.missing_files || [], { limit: 8, empty: "-", code: true })}</div>
          </div>
        </div>
      `;
    }

    function renderContextEvidenceTabs({ payload = {}, latest = {}, diagnostics = {}, routingHealth = {}, maintenanceItems = [], kbGrowthState = {}, latestFeedback = {} } = {}) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      const growthHtml = renderKbGrowthState(kbGrowthState)
        || `<div class="task-evidence-empty">${escapeHtml(t("task.context_evidence_growth_not_required", "No KB growth state for this task yet."))}</div>`;
      const feedbackHtml = renderAgentKbFeedback(latestFeedback)
        || `<div class="task-evidence-empty">${escapeHtml(t("task.context_evidence_no_agent_feedback", "No agent KB feedback yet."))}</div>`;
      const tabs = [
        ["growth", t("task.context_evidence_tab_growth", "Growth")],
        ["feedback", t("task.context_evidence_tab_feedback", "Feedback")],
        ["evidence", t("task.context_evidence_tab_evidence", "Evidence")],
        ["diagnostics", t("task.context_evidence_tab_diagnostics", "Diagnostics")]
      ];
      const storedTab = window.__ahaContextEvidenceActiveTab;
      const activeTab = tabs.some(([key]) => key === storedTab) ? storedTab : "growth";
      return `
        <div class="task-evidence-tabs" role="tablist" aria-label="${escapeHtml(t("task.context_evidence_tabs", "Context evidence sections"))}">
          ${tabs.map(([key, label]) => {
            const active = key === activeTab;
            return `
              <button class="button-ghost task-evidence-tab ${active ? "active" : ""}" type="button" role="tab" aria-selected="${active ? "true" : "false"}" data-context-evidence-tab="${escapeHtml(key)}">${escapeHtml(label)}</button>
            `;
          }).join("")}
        </div>
        <div class="task-evidence-tab-panels">
          <section class="task-evidence-tab-panel ${activeTab === "growth" ? "active" : ""}" role="tabpanel" data-context-evidence-panel="growth">
            <div class="task-evidence-block">
              <strong>${escapeHtml(t("task.context_evidence_suggestions", "KB maintenance actions"))}</strong>
              ${renderEvidenceSuggestions(maintenanceItems)}
            </div>
            ${growthHtml}
          </section>
          <section class="task-evidence-tab-panel ${activeTab === "feedback" ? "active" : ""}" role="tabpanel" data-context-evidence-panel="feedback">
            ${feedbackHtml}
          </section>
          <section class="task-evidence-tab-panel ${activeTab === "evidence" ? "active" : ""}" role="tabpanel" data-context-evidence-panel="evidence">
            ${renderEvidenceFacts(latest, diagnostics)}
          </section>
          <section class="task-evidence-tab-panel ${activeTab === "diagnostics" ? "active" : ""}" role="tabpanel" data-context-evidence-panel="diagnostics">
            ${renderEvidenceDiagnostics(payload, diagnostics, routingHealth)}
          </section>
        </div>
      `;
    }

    function renderContextEvidencePanelHtml(detail = null) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      if (!detail) return `<div class="empty loading">${escapeHtml(t("task.context_evidence_loading", "Loading context evidence..."))}</div>`;
      if (detail.error) return `<div class="empty">${escapeHtml(detail.error)}</div>`;
      if (detail.loading) return `<div class="empty loading">${escapeHtml(t("task.context_evidence_loading", "Loading context evidence..."))}</div>`;
      const payload = detail.payload || { records: [], latest_result: null, maintenance_suggestions: [], maintenance_plan: [] };
      const latest = payload.latest_result || {};
      const diagnostics = latest.navigation_diagnostics || {};
      const routingHealth = payload.routing_health || latest.routing_health || {};
      const latestFeedback = payload.summary?.latest_agent_feedback || {};
      const kbGrowthState = payload.kb_growth_state || payload.summary?.kb_growth_state || {};
      const count = Number(payload.count || 0);
      const maintenanceItems = Array.isArray(payload.maintenance_plan) && payload.maintenance_plan.length
        ? payload.maintenance_plan
        : payload.maintenance_suggestions || [];
      if (!count) {
        return `
          <div class="context-evidence-view">
            <div class="task-evidence-head">
              <h3>${escapeHtml(t("task.context_evidence", "KB impact"))}</h3>
              <button type="button" data-context-evidence-refresh>${escapeHtml(t("common.refresh", "Refresh"))}</button>
            </div>
            ${renderEvidenceLoop(payload)}
          </div>
        `;
      }
      return `
        <div class="context-evidence-view">
          <div class="task-evidence-head">
            <div>
              <h3>${escapeHtml(t("task.context_evidence", "KB impact"))}</h3>
              <div class="meta">${escapeHtml(t("task.context_evidence_summary_hint", "Proof of whether KB helped this task and improved future work."))}</div>
            </div>
            <button type="button" data-context-evidence-refresh>${escapeHtml(t("common.refresh", "Refresh"))}</button>
          </div>
          ${renderEvidenceLoop(payload)}
          ${renderEvidenceSummary(payload)}
          <details class="task-evidence-details">
            <summary>${escapeHtml(t("task.context_evidence_technical_details", "Technical details"))} · ${escapeHtml(t("task.context_evidence_count", "{count} evidence records").replace("{count}", String(count)))}</summary>
            <div class="task-evidence-details-body">
              ${renderContextEvidenceTabs({ payload, latest, diagnostics, routingHealth, maintenanceItems, kbGrowthState, latestFeedback })}
            </div>
          </details>
        </div>
      `;
    }

    return Object.freeze({
      renderConversationFiltersHtml,
      renderConversationPanelHtml,
      renderFinalPanelHtml,
      renderHardwareIoPanelHtml,
      renderBrowserPanelHtml,
      renderLogsPanelHtml,
      renderContextPanelHtml,
      renderContextEvidencePanelHtml
    });
  }

  window.AHAConversationPanel = Object.freeze({ createConversationPanelHelpers });
})();
