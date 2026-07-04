(() => {
  function escapeFallback(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function createPromptMetricsView(options = {}) {
    const escapeHtml = options.escapeHtml || escapeFallback;
    const formatMetricNumber = options.formatMetricNumber || (value => String(value || 0));
    const formatMetricBytes = options.formatMetricBytes || (value => String(value || 0));
    const usageCacheReadTokens = options.usageCacheReadTokens || (() => 0);
    const usageCacheCreationTokens = options.usageCacheCreationTokens || (() => 0);
    const usageTokenBreakdown = options.usageTokenBreakdown || ((usage, { backend = "", source = "", contextPressure = null } = {}) => {
      const resolvedBackend = String(backend || contextPressure?.backend || source || "").toLowerCase().includes("claude") ? "claude" : "";
      const hasField = key => Object.prototype.hasOwnProperty.call(usage || {}, key) && usage?.[key] != null;
      const inputTokens = Number(usage?.input_tokens || 0);
      const cacheReadTokens = usageCacheReadTokens(usage || {});
      const cacheCreationTokens = usageCacheCreationTokens(usage || {});
      const outputTokens = Number(usage?.output_tokens || 0);
      const reasoningOutputTokens = Number(usage?.reasoning_output_tokens || 0);
      const totalTokens = inputTokens + outputTokens + (resolvedBackend === "claude" ? cacheReadTokens : 0);
      return {
        backend: resolvedBackend,
        cacheCreationTokens,
        cacheReadTokens,
        cachedTokens: cacheReadTokens + cacheCreationTokens,
        hasCacheCreationTokens: hasField("cache_creation_input_tokens"),
        hasCachedInputTokens: hasField("cached_input_tokens"),
        hasCacheReadTokens: hasField("cache_read_input_tokens") || hasField("cached_input_tokens"),
        hasInputTokens: hasField("input_tokens"),
        hasOutputTokens: hasField("output_tokens"),
        hasReasoningOutputTokens: hasField("reasoning_output_tokens"),
        inputTokens,
        isCodex: resolvedBackend === "codex",
        isClaude: resolvedBackend === "claude",
        outputTokens,
        reasoningOutputTokens,
        totalFormula: resolvedBackend === "claude" ? "input + cache read + output" : "input + output",
        totalTokens
      };
    });
    const contextPressurePercent = options.contextPressurePercent || (() => "");
    const metricMapRows = options.metricMapRows || (() => []);

    function renderSessionMapRows(rows, emptyLabel = "none") {
      if (!rows.length) {
        return `<div class="session-breakdown-empty">${escapeHtml(emptyLabel)}</div>`;
      }
      return rows.map(row => `
    <div class="session-breakdown-row">
      <span>${escapeHtml(row.name)}</span>
      <code>${escapeHtml(formatMetricNumber(row.count))}</code>
      ${row.chars == null ? "" : `<code>${escapeHtml(formatMetricNumber(row.chars))}</code>`}
    </div>
  `).join("");
    }

    function renderSessionBreakdown(analysis) {
      if (!analysis || !Object.keys(analysis).length) return "";
      if (analysis.error) {
        return `<div class="session-breakdown-error">${escapeHtml(analysis.error)}</div>`;
      }
      const totals = [
        `payload ${formatMetricNumber(analysis.total_payload_text_chars || 0)} chars`,
        `assistant ${formatMetricNumber(analysis.assistant_message_chars || 0)} chars`,
        `mirrors ${formatMetricNumber(analysis.event_msg_prompt_mirror_total_chars || 0)} chars`,
        `parse errors ${formatMetricNumber(analysis.parse_errors || 0)}`
      ];
      const latest = Array.isArray(analysis.latest_aha_prompts) ? analysis.latest_aha_prompts : [];
      return `
    <details class="metrics-breakdown session-breakdown" data-metrics-breakdown="session">
      <summary>Session breakdown</summary>
      <div class="session-breakdown-kpis">
        ${totals.map(item => `<code>${escapeHtml(item)}</code>`).join("")}
      </div>
      <div class="session-breakdown-group">
        <strong>AHA prompts</strong>
        ${renderSessionMapRows(metricMapRows(analysis.aha_prompt_counts, analysis.aha_prompt_chars))}
      </div>
      <div class="session-breakdown-group">
        <strong>Event mirrors</strong>
        ${renderSessionMapRows(metricMapRows(analysis.event_msg_prompt_mirror_counts, analysis.event_msg_prompt_mirror_chars))}
      </div>
      <div class="session-breakdown-group">
        <strong>Response items</strong>
        ${renderSessionMapRows(metricMapRows(analysis.response_item_counts, analysis.response_item_chars))}
      </div>
      <div class="session-breakdown-group">
        <strong>Record types</strong>
        ${renderSessionMapRows(metricMapRows(analysis.type_counts))}
      </div>
      <div class="session-breakdown-group">
        <strong>Latest prompts</strong>
        ${latest.length ? latest.map(item => `
          <div class="session-breakdown-row">
            <span>${escapeHtml(`${item.mode || "unknown"} @ line ${item.line || "-"}`)}</span>
            <code>${escapeHtml(formatMetricNumber(item.chars || 0))}</code>
          </div>
        `).join("") : `<div class="session-breakdown-empty">none</div>`}
      </div>
    </details>
  `;
    }

    function renderAhaInputBreakdown(data, rows) {
      const flags = [
        data.prompt_mode ? `mode ${data.prompt_mode}` : "",
        data.source ? `source ${data.source}` : "",
        data.target ? `target ${data.target}` : "",
        data.sender ? `sender ${data.sender}` : "",
        data.task_id ? `task ${data.task_id}` : "",
        data.is_finalization ? "finalization" : "",
        data.is_agent_command ? "agent command" : ""
      ].filter(Boolean);
      return `
    <details class="metrics-breakdown aha-breakdown" data-metrics-breakdown="aha">
      <summary>AHA input breakdown</summary>
      <div class="session-breakdown-kpis">
        ${(flags.length ? flags : ["no prompt metadata"]).map(item => `<code>${escapeHtml(item)}</code>`).join("")}
      </div>
      <div class="session-breakdown-group">
        <strong>Components</strong>
        ${rows.length ? rows.map(row => `
          <div class="session-breakdown-row metrics-breakdown-row-wide">
            <span>${escapeHtml(row.name)}</span>
            <code>${escapeHtml(formatMetricNumber(row.chars))}</code>
            <code>${escapeHtml(formatMetricBytes(row.bytes))}</code>
            <code>${escapeHtml(formatMetricNumber(row.lines))} lines</code>
          </div>
        `).join("") : `<div class="session-breakdown-empty">none</div>`}
      </div>
    </details>
  `;
    }

    function renderUsageBreakdown(usage, usageStatus, source, contextPressure = null) {
      const tokenBreakdown = usageTokenBreakdown(usage, { source, contextPressure });
      const inputTokens = tokenBreakdown.inputTokens;
      const cacheReadTokens = tokenBreakdown.cacheReadTokens;
      const cacheCreationTokens = tokenBreakdown.cacheCreationTokens;
      const outputTokens = tokenBreakdown.outputTokens;
      const reasoningTokens = Number(usage.reasoning_output_tokens || 0);
      const cacheRatioBase = inputTokens + cacheReadTokens + cacheCreationTokens;
      const cacheRatio = cacheRatioBase > 0 ? `${(cacheReadTokens / cacheRatioBase * 100).toFixed(1)}% cache read` : "";
      const rows = [];
      const addRow = (name, value, present) => {
        if (present) rows.push([name, value]);
      };
      addRow("input_tokens", inputTokens, tokenBreakdown.hasInputTokens);
      if (tokenBreakdown.isClaude) {
        addRow("cache_read_input_tokens", cacheReadTokens, tokenBreakdown.hasCacheReadTokens);
        addRow("cache_creation_input_tokens", cacheCreationTokens, tokenBreakdown.hasCacheCreationTokens);
      } else {
        addRow(tokenBreakdown.hasCachedInputTokens ? "cached_input_tokens" : "cache_read_input_tokens", cacheReadTokens, tokenBreakdown.hasCacheReadTokens);
        addRow("cache_creation_input_tokens", cacheCreationTokens, tokenBreakdown.hasCacheCreationTokens);
      }
      addRow("output_tokens", outputTokens, tokenBreakdown.hasOutputTokens);
      addRow("reasoning_output_tokens", reasoningTokens, tokenBreakdown.hasReasoningOutputTokens);
      if (rows.length) rows.push(["total_reported_tokens", tokenBreakdown.totalTokens]);
      const contextRows = [
        ["model", contextPressure?.model || "-"],
        ["input_tokens", contextPressure?.input_tokens ?? "-"],
        ["aha_prompt_tokens", contextPressure?.aha_prompt_tokens ?? "-"],
        ["backend_input_tokens", contextPressure?.backend_input_tokens ?? "-"],
        ["estimated_backend_history_tokens", contextPressure?.estimated_backend_history_tokens ?? "-"],
        ["aha_overhead_ratio", contextPressure?.aha_overhead_ratio ?? "-"],
        ["prompt_tokens", contextPressure?.prompt_tokens ?? "-"],
        ["runtime_input_tokens", contextPressure?.runtime_input_tokens ?? "-"],
        ["runtime_effective_input_tokens", contextPressure?.runtime_effective_input_tokens ?? "-"],
        ["runtime_cached_input_tokens", contextPressure?.runtime_cached_input_tokens ?? "-"],
        ["runtime_cache_creation_input_tokens", contextPressure?.runtime_cache_creation_input_tokens ?? "-"],
        ["runtime_total_tokens", contextPressure?.runtime_total_tokens ?? "-"],
        ["prompt_chars", contextPressure?.prompt_chars ?? "-"],
        ["prompt_bytes", contextPressure?.prompt_bytes ?? "-"],
        ["context_window", contextPressure?.context_window ?? "-"],
        ["context_percent", contextPressurePercent(contextPressure) || "-"],
        ["level", contextPressure?.level || "unknown"],
        ["window_source", contextPressure?.context_window_source || "unknown"],
        ["pressure_source", contextPressure?.pressure_source || "unknown"]
      ];
      const flags = [
        `status ${usageStatus.label}`,
        source ? `source ${source}` : "",
        tokenBreakdown.backend ? `backend ${tokenBreakdown.backend}` : "",
        `formula ${tokenBreakdown.totalFormula}`,
        cacheRatio,
        usage.total_cost_usd != null ? `cost $${Number(usage.total_cost_usd || 0).toFixed(6)}` : "",
        usage.duration_ms != null ? `duration ${formatMetricNumber(usage.duration_ms)}ms` : "",
        usage.num_turns != null ? `turns ${formatMetricNumber(usage.num_turns)}` : "",
        usage.subtype ? `subtype ${usage.subtype}` : ""
      ].filter(Boolean);
      return `
    <details class="metrics-breakdown usage-breakdown" data-metrics-breakdown="usage">
      <summary>Backend usage breakdown</summary>
      <div class="session-breakdown-kpis">
        ${flags.map(item => `<code>${escapeHtml(item)}</code>`).join("")}
      </div>
      <div class="session-breakdown-group">
        <strong>Tokens</strong>
        ${rows.map(([name, value]) => `
          <div class="session-breakdown-row">
            <span>${escapeHtml(name)}</span>
            <code>${escapeHtml(formatMetricNumber(value))}</code>
          </div>
        `).join("")}
      </div>
      <div class="session-breakdown-group">
        <strong>Context Pressure</strong>
        ${contextRows.map(([name, value]) => `
          <div class="session-breakdown-row">
            <span>${escapeHtml(name)}</span>
            <code>${escapeHtml(typeof value === "number" ? formatMetricNumber(value) : String(value))}</code>
          </div>
        `).join("")}
      </div>
    </details>
  `;
    }

    return Object.freeze({
      renderAhaInputBreakdown,
      renderSessionBreakdown,
      renderSessionMapRows,
      renderUsageBreakdown
    });
  }

  window.AHAPromptMetricsView = Object.freeze({ createPromptMetricsView });
})();
