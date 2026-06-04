(() => {
  function createTimelineView(state = {}, deps = {}) {
    function eventTimeLabel(event) {
      const data = deps.eventData?.(event) || {};
      const value = event?.ts || data.ts;
      return deps.formatLocalTimestamp?.(value, value || "") || "";
    }

    function compactText(value, limit = 180) {
      const text = String(value ?? "").replace(/\s+/g, " ").trim();
      return text.length > limit ? `${text.slice(0, limit - 1)}...` : text;
    }

    function shouldCollapseMessage(value) {
      const text = String(value ?? "");
      return text.length > state.collapsedMessageCharLimit || text.split("\n").length > state.collapsedMessageLineLimit;
    }

    function expandedMessageStateKey(key, contextKey = deps.conversationKey?.()) {
      const messageKey = String(key || "");
      if (!messageKey) return "";
      return `${contextKey}::${messageKey}`;
    }

    function setExpandedMessageKey(key, open, contextKey = deps.conversationKey?.()) {
      const stateKey = expandedMessageStateKey(key, contextKey);
      if (!stateKey) return;
      if (open) {
        state.expandedMessageKeys?.add(stateKey);
      } else {
        state.expandedMessageKeys?.delete(stateKey);
      }
    }

    function syncExpandedMessageKeysFromDom(root) {
      root?.querySelectorAll(".collapsed-message[data-message-key]").forEach(details => {
        if (details instanceof HTMLDetailsElement) {
          setExpandedMessageKey(details.dataset.messageKey, details.open, details.dataset.messageContextKey || deps.conversationKey?.());
        }
      });
    }

    function renderMessageBody(body, key = "") {
      const text = String(body || "");
      if (!shouldCollapseMessage(text)) {
        return `<div class="message-body">${deps.escapeHtml?.(text)}</div>`;
      }
      const lines = text.split("\n").length;
      const summary = compactText(text, 220);
      const contextKey = deps.conversationKey?.() || "";
      const open = state.expandedMessageKeys?.has(expandedMessageStateKey(key, contextKey)) ? " open" : "";
      return `
        <details class="message-body collapsed-message" data-message-key="${deps.escapeHtml?.(key)}" data-message-context-key="${deps.escapeHtml?.(contextKey)}"${open}>
          <summary>
            <span>${deps.escapeHtml?.(summary || "(empty message)")}</span>
            <em>${deps.escapeHtml?.(`${text.length} chars | ${lines} lines`)}</em>
          </summary>
          <div class="message-body-full">${deps.escapeHtml?.(text)}</div>
        </details>
      `;
    }

    function renderTimelineCard(title, body, ts, cls, key = "") {
      const copyKey = String(key || "");
      if (copyKey) state.copyTextByKey?.set(copyKey, String(body || ""));
      const copyButton = copyKey
        ? `<button class="message-copy" type="button" data-copy-message-key="${deps.escapeHtml?.(copyKey)}" data-copy-state="idle" title="Copy message" aria-label="Copy message"><span class="message-copy-icon" aria-hidden="true"></span><span class="message-copy-label sr-only">Copy message</span></button>`
        : "";
      return `
        <div class="message ${cls}">
          <div class="message-head">
            <span class="message-title">${deps.escapeHtml?.(title)}</span>
            <span class="message-actions">
              <time>${deps.escapeHtml?.(ts || "")}</time>
              ${copyButton}
            </span>
          </div>
          ${renderMessageBody(body, key)}
        </div>
      `;
    }

    function renderTimelineStatus(title, body, status, ts = "") {
      return `
        <div class="timeline-status ${deps.escapeHtml?.(status || "")}">
          <span>${deps.escapeHtml?.(title)}</span>
          <code>${deps.escapeHtml?.(body || "")}</code>
          <time>${deps.escapeHtml?.(ts || "")}</time>
        </div>
      `;
    }

    function renderTimelineEvent(event) {
      const data = deps.eventData?.(event) || {};
      if (event.type === "message") {
        const { displaySender, displayTarget, className } = deps.messageTimelineDisplay?.(data) || {};
        return renderTimelineCard(`${displaySender} -> ${displayTarget}`, data.message || "", eventTimeLabel(event), className, event._uiKey);
      }
      if (event.type === "agent_message") return renderTimelineCard(deps.agentUpdateTitle?.(data), deps.agentUpdateBody?.(data), eventTimeLabel(event), "agent-update", event._uiKey);
      if (event.type === "agent_command_started") return renderTimelineCard(`running command (${data.target || "main"})`, data.command || "", eventTimeLabel(event), "agent-command", event._uiKey);
      if (event.type === "agent_command_finished") {
        const output = data.output_tail
          ? `\n\nOutput tail:\n${data.output_tail}`
          : data.output_tail_omitted
            ? `\n\nOutput tail omitted (${data.output_tail_chars || 0} chars).`
            : "";
        return renderTimelineCard(`command finished (${data.target || "main"}) exit=${data.exit_code ?? "-"}`, `${data.command || ""}${output}`, eventTimeLabel(event), data.exit_code === 0 ? "agent-command" : "event-error", event._uiKey);
      }
      if (event.type === "agent_error") return renderTimelineCard(`agent error (${data.target || "main"})`, data.message || JSON.stringify(data), eventTimeLabel(event), "event-error", event._uiKey);
      if (event.type === "agent_usage") {
        const usage = data.usage || {};
        return renderTimelineStatus("usage", `input=${usage.input_tokens ?? "-"} cached=${usage.cached_input_tokens ?? "-"} output=${usage.output_tokens ?? "-"} reasoning=${usage.reasoning_output_tokens ?? "-"}`, "usage", eventTimeLabel(event));
      }
      if (event.type === "agent_prompt_metrics") {
        const total = data.total || {};
        const rows = deps.componentMetricRows?.(data.components || {}, Number(total.chars || 0)) || [];
        const top = rows[0]?.name ? ` top=${rows[0].name}:${rows[0].chars}` : "";
        return renderTimelineStatus("prompt metrics", `chars=${total.chars ?? "-"} bytes=${total.bytes ?? "-"} lines=${total.lines ?? "-"}${top}`, "usage", eventTimeLabel(event));
      }
      if (event.type === "agent_context_overflow") return renderTimelineStatus("context overflow", data.message || data.reason || "context_window", "failed", eventTimeLabel(event));
      const ts = eventTimeLabel(event);
      if (event.type === "task_status_changed") return renderTimelineStatus(`task ${data.status}`, `exit=${data.exit_code ?? "-"}`, data.status, ts);
      if (event.type === "task_started") return renderTimelineStatus("task started", data.title || "", "running", ts);
      if (event.type === "task_finished") return renderTimelineStatus(`task ${data.status || "finished"}`, `exit=${data.exit_code ?? "-"}`, data.status || "completed", ts);
      if (event.type === "task_result_written") return renderTimelineStatus("final written", `${data.chars || 0} chars`, "completed", ts);
      if (event.type === "task_final_requested") {
        const isRoundSummary = data.policy === "round_summary";
        return renderTimelineStatus(isRoundSummary ? "round summary requested" : "final requested", `target=${data.target || "main"}`, "running", ts);
      }
      if (event.type === "task_round_summary_requested") return renderTimelineStatus("round summary requested", `target=${data.target || "main"}`, "running", ts);
      if (event.type === "task_reopened") return renderTimelineStatus("task reopened", data.task_id || "-", "awaiting_user", ts);
      if (event.type === "task_completed") return renderTimelineStatus("task completed", `exit=${data.exit_code ?? "-"}`, "completed", ts);
      if (event.type === "task_waiting_for_subagents") return renderTimelineStatus("waiting for sub-agents", `pending=${(data.pending || []).join(", ") || "-"}`, "running", ts);
      if (event.type === "agent_started") return renderTimelineStatus("agent started", `${data.target || "main"} from ${data.sender || "-"} sandbox=${data.sandbox || "-"} approval=${data.approval || "-"} proxy=${data.proxy_enabled ? "on" : "off"}`, "running", ts);
      if (event.type === "agent_interrupted") return renderTimelineStatus("agent interrupted", data.agent_id || data.target || "main", "interrupted", ts);
      if (event.type === "agent_status_changed") {
        const waitingReason = data.waiting_reason ? ` waiting=${data.waiting_reason}` : "";
        return renderTimelineStatus("agent status", `${data.agent_id || "-"} ${data.status || "-"}${waitingReason}`, data.status || "session", ts);
      }
      if (event.type === "agent_config_updated") return renderTimelineStatus("agent config updated", `${data.agent_id || "-"} backend=${data.backend || "-"} model=${data.model || "-"} sandbox=${data.sandbox || "-"} approval=${data.approval || "-"} proxy=${data.proxy_enabled ? "on" : "off"}`, "session", ts);
      if (event.type === "agent_backend_switched") return renderTimelineStatus("agent backend switched", `${data.agent_id || "-"} ${data.old_backend || "-"} -> ${data.new_backend || "-"} summary=${data.summary_path || "-"}`, "session", ts);
      if (event.type === "agent_backend_restarted") return renderTimelineStatus("agent backend restarted", `${data.agent_id || "-"} backend=${data.backend || "-"}`, "session", ts);
      if (event.type === "run_proxy_config_updated") return renderTimelineStatus("run proxy updated", `default=${data.proxy_enabled ? "on" : "off"} http=${data.http_proxy_configured ? "set" : "-"} https=${data.https_proxy_configured ? "set" : "-"} no_proxy=${data.no_proxy_configured ? "set" : "-"}`, "session", ts);
      if (event.type === "task_proxy_config_updated") return renderTimelineStatus("task proxy switch updated", `default=${data.proxy_enabled ? "on" : "off"}`, "session", ts);
      if (event.type === "task_supervision_config_updated") return renderTimelineStatus("task supervision updated", `${data.mode || "-"} via ${data.host_backend || "stub"} max_rounds=${data.max_rounds || "-"}`, "session", ts);
      if (event.type === "task_context_management_config_updated") return renderTimelineStatus("task context updated", `${data.auto_compact_enabled ? "auto on" : "auto off"} threshold=${data.auto_compact_threshold_percent || deps.defaultTaskContextThresholdPercent?.()}%`, "session", ts);
      if (event.type === "main_reported_to_host") return renderTimelineStatus("main reported to host", `${data.host_backend || "stub"} ${data.channel || "main_only"} reply=${data.reply_chars || 0} chars`, "session", ts);
      if (event.type === "host_decision") return renderTimelineStatus("host decision", data.decision || "-", "session", ts);
      if (event.type === "main_applied_decision") return renderTimelineStatus("main applied host decision", `${data.decision || "-"} effect=${data.effect || "noop"}`, data.applied ? "running" : "session", ts);
      if (event.type === "agent_thread") return renderTimelineStatus(`${data.source || "backend"} session`, data.thread_id || "-", "session", ts);
      if (event.type === "agent_finished") return renderTimelineStatus("agent finished", `exit=${data.exit_code ?? "-"}`, data.exit_code === 0 ? "completed" : "failed", ts);
      if (event.type === "task_dispatched") return renderTimelineStatus("task dispatched", `target=${data.target || "-"}`, "session", ts);
      if (event.type === "agent_created") return renderTimelineStatus("sub-agent created", `${data.agent_id || "-"} backend=${data.backend || "-"}`, "session", ts);
      if (event.type === "agent_delegated") return renderTimelineStatus("delegated", `${data.count || 0} action(s)`, "session", ts);
      if (event.type === "agent_message_routed") return renderTimelineStatus("routed to agent", `${data.target || "-"} ${data.reason || ""}`, "running", ts);
      if (event.type === "claimed_sub_without_aha_agent") return renderTimelineStatus("sub-agent claim mismatch", data.reason || "claimed without AHA spawn_sub", "failed", ts);
      if (event.type === "native_subagent_tool_used") return renderTimelineStatus("native subagent blocked", `${data.tool_name || "-"} ${data.reason || ""}`, "failed", ts);
      if (event.type === "sub_agent_reported") return renderTimelineStatus("sub-agent reported", `${data.agent_id || "-"} ${data.status || "-"}`, data.status || "session", ts);
      if (event.type === "sub_agent_report_ignored") return renderTimelineStatus("sub-agent report ignored", `${data.agent_id || "-"} ${data.reason || ""}`, "session", ts);
      if (event.type === "sub_agent_backend_recovered") return renderTimelineStatus("sub-agent backend recovered", `${data.agent_id || "-"} attempt=${data.attempt || "-"}`, "running", ts);
      if (event.type === "sub_agent_backend_failed") return renderTimelineStatus("sub-agent backend failed", `${data.agent_id || "-"} attempts=${data.attempts || "-"}`, "failed", ts);
      if (event.type === "workspace_missing") return renderTimelineStatus("workspace missing", data.workspace_path || "-", "blocked", ts);
      return renderTimelineStatus(event.type, JSON.stringify(data), "session", ts);
    }

    function renderTurnTimer(taskId) {
      const task = (deps.tasks?.() || []).find(item => item.id === taskId);
      const target = deps.backendTarget?.();
      const agent = (task?.agents || []).find(item => item.id === target);
      const timing = deps.latestTurnTiming?.(taskId) || {
        startedAt: null,
        finishedAt: null,
        elapsedMs: 0,
        running: false,
        status: "idle",
        waitingReason: "",
        target,
        sender: "-"
      };
      const title = timing.running
        ? (timing.status === "waiting"
          ? (timing.waitingReason === "host" ? "Agent is waiting for host" : "Agent is waiting")
          : "Agent is working")
        : timing.status === "idle"
          ? "Agent is idle"
          : `Agent turn ${timing.status}`;
      const label = timing.status === "idle" ? "" : (timing.running ? "elapsed" : "duration");
      const details = timing.status === "idle" && !timing.running
        ? ""
        : [
          label ? `${label} ${deps.formatDuration?.(timing.elapsedMs)}` : "",
          timing.waitingReason ? `waiting ${timing.waitingReason}` : "",
          timing.startedAt ? `started ${deps.formatClock?.(timing.startedAt)}` : "",
          timing.finishedAt ? `finished ${deps.formatClock?.(timing.finishedAt)}` : ""
        ].filter(Boolean).join(" | ");
      const visualStatus = timing.running && !["waiting", "busy"].includes(timing.status) ? "running" : timing.status;
      return `
        <div class="turn-timer ${deps.escapeHtml?.(visualStatus)}">
          <span class="activity-dot"></span>
          <strong>${deps.escapeHtml?.(title)}</strong>
          ${details ? `<code>${deps.escapeHtml?.(details)}</code>` : ""}
          ${deps.renderPromptMetricsPopover?.(taskId)}
        </div>
      `;
    }

    return Object.freeze({
      compactText,
      eventTimeLabel,
      expandedMessageStateKey,
      renderMessageBody,
      renderTimelineCard,
      renderTimelineEvent,
      renderTimelineStatus,
      renderTurnTimer,
      setExpandedMessageKey,
      shouldCollapseMessage,
      syncExpandedMessageKeysFromDom
    });
  }

  window.AHATimelineView = Object.freeze({ createTimelineView });
})();
