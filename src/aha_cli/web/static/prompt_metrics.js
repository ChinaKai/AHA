(() => {
  function contextPressureHasPercent(pressure) {
    if (pressure?.percent == null || pressure?.percent === "") return false;
    return Number.isFinite(Number(pressure.percent));
  }

  function formatMetricNumber(value) {
    const number = Number(value || 0);
    if (!Number.isFinite(number)) return "0";
    return new Intl.NumberFormat("en-US").format(number);
  }

  function formatMetricCompact(value) {
    const number = Number(value || 0);
    if (!Number.isFinite(number) || number <= 0) return "0";
    if (number < 1000) return String(Math.round(number));
    if (number < 1000000) {
      const valueInK = number / 1000;
      return `${valueInK < 10 ? valueInK.toFixed(1) : Math.round(valueInK)}k`;
    }
    return `${(number / 1000000).toFixed(1)}m`;
  }

  function formatMetricBytes(value) {
    const number = Number(value || 0);
    if (!Number.isFinite(number) || number <= 0) return "0 B";
    if (number < 1024) return `${formatMetricNumber(number)} B`;
    if (number < 1024 * 1024) return `${(number / 1024).toFixed(1)} KB`;
    return `${(number / 1024 / 1024).toFixed(2)} MB`;
  }

  function contextPressureStatus(pressure) {
    const level = String(pressure?.level || "unknown");
    if (level === "high") return { label: "high", className: "context-high" };
    if (level === "watch") return { label: "watch", className: "context-watch" };
    if (level === "ok") return { label: "ok", className: "context-ok" };
    return { label: "unknown", className: "context-unknown" };
  }

  function contextPressurePercent(pressure) {
    if (pressure?.percent == null || pressure?.percent === "") return "";
    const percent = Number(pressure.percent);
    return Number.isFinite(percent) ? `${percent.toFixed(percent >= 10 ? 1 : 2)}%` : "";
  }

  function contextPressureSummary(pressure) {
    const percent = contextPressurePercent(pressure);
    if (!pressure) return "context unknown";
    if (!percent) {
      const promptChars = pressure.prompt_chars != null ? formatMetricCompact(pressure.prompt_chars) : "";
      return promptChars ? `context unknown (${promptChars} chars)` : "context unknown";
    }
    const inputTokens = pressure.input_tokens ?? pressure.prompt_tokens;
    const input = inputTokens != null ? formatMetricCompact(inputTokens) : "-";
    const window = pressure.context_window != null ? formatMetricCompact(pressure.context_window) : "-";
    return `${percent} context (${input}/${window})`;
  }

  function agentContextPressureSummary(agent) {
    return contextPressureSummary(agent?.backend_context_pressure);
  }

  function formatMetricCountChars(count, chars, noun) {
    const safeCount = Number(count || 0);
    const safeChars = Number(chars || 0);
    return `${formatMetricNumber(safeCount)} ${noun} · ${formatMetricNumber(safeChars)} chars`;
  }

  function metricMapRows(counts, chars = null) {
    const keys = new Set([...Object.keys(counts || {}), ...Object.keys(chars || {})]);
    return Array.from(keys).map(name => ({
      name,
      count: Number((counts || {})[name] || 0),
      chars: chars ? Number(chars[name] || 0) : null
    })).sort((left, right) => {
      const leftValue = left.chars == null ? left.count : left.chars;
      const rightValue = right.chars == null ? right.count : right.chars;
      return rightValue - leftValue || left.name.localeCompare(right.name);
    });
  }

  function usageCacheReadTokens(usage) {
    return Number(usage.cached_input_tokens ?? usage.cache_read_input_tokens ?? 0);
  }

  function usageCacheCreationTokens(usage) {
    return Number(usage.cache_creation_input_tokens ?? 0);
  }

  function metricNumberValue(value) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? number : 0;
  }

  function usageTotalTokens(usage = {}) {
    const inputTokens = metricNumberValue(usage?.input_tokens);
    const outputTokens = metricNumberValue(usage?.output_tokens);
    return inputTokens + outputTokens || metricNumberValue(usage?.total_tokens);
  }

  function historySessionTokenTotal(history = []) {
    if (!Array.isArray(history)) return 0;
    return history.reduce((sum, session) => {
      const summary = session?.token_summary || {};
      const summaryTotal = metricNumberValue(summary.total_tokens ?? summary.totalTokens);
      if (summaryTotal) return sum + summaryTotal;
      return sum + usageTotalTokens(session?.last_usage || session?.usage || {});
    }, 0);
  }

  function tokenLedgerFromMetrics({ usage = {}, contextPressure = null, total = {}, backendSession = null } = {}) {
    const ahaPromptTokens = metricNumberValue(
      contextPressure?.aha_prompt_tokens ??
      contextPressure?.prompt_estimate_tokens ??
      total?.tokens
    );
    const historySessionTokens = historySessionTokenTotal(backendSession?.history);
    const usageIsCurrent = !historySessionTokens || !backendSession || Boolean(backendSession?.id || backendSession?.exists);
    const backendInputTokens = usageIsCurrent ? metricNumberValue(usage?.input_tokens) : 0;
    const estimatedHistoryTokens = metricNumberValue(
      contextPressure?.estimated_backend_history_tokens ??
      (backendInputTokens && ahaPromptTokens ? Math.max(0, backendInputTokens - ahaPromptTokens) : 0)
    );
    const reasoningOutputTokens = usageIsCurrent ? metricNumberValue(usage?.reasoning_output_tokens) : 0;
    const cacheReadTokens = usageIsCurrent ? usageCacheReadTokens(usage) : 0;
    const cacheCreationTokens = usageIsCurrent ? usageCacheCreationTokens(usage) : 0;
    const cachedTokens = cacheReadTokens + cacheCreationTokens;
    const sessionBytes = metricNumberValue(backendSession?.size_bytes);
    const contextPercent = contextPressurePercent(contextPressure);
    const outputTokens = usageIsCurrent ? metricNumberValue(usage?.output_tokens) : 0;
    const currentTotalTokens = backendInputTokens + outputTokens;
    const totalTokens = historySessionTokens + currentTotalTokens;
    const trackedTokens = totalTokens;
    const rows = [
      { key: "backend_input", label: "Backend input", value: backendInputTokens, unit: "tok", className: "input" },
      { key: "history", label: "History", value: estimatedHistoryTokens, unit: "tok", className: estimatedHistoryTokens > ahaPromptTokens ? "dominant" : "neutral" },
      { key: "aha_prompt", label: "AHA prompt", value: ahaPromptTokens, unit: "tok", className: "neutral" },
      { key: "output", label: "Output", value: outputTokens, unit: "tok", className: outputTokens > backendInputTokens ? "dominant" : "neutral" },
      { key: "cache", label: "Cache", value: cachedTokens, unit: "tok", className: "cache" }
    ];
    const comparable = rows.filter(row => row.key !== "backend_input" && row.value > 0);
    const largest = comparable.sort((left, right) => right.value - left.value)[0] || null;
    return {
      ahaPromptTokens,
      backendInputTokens,
      cacheCreationTokens,
      cacheReadTokens,
      cachedTokens,
      contextPercent,
      currentTotalTokens,
      estimatedHistoryTokens,
      historySessionTokens,
      hasData: Boolean(totalTokens || backendInputTokens || outputTokens || ahaPromptTokens || sessionBytes),
      largest,
      outputTokens,
      reasoningOutputTokens,
      rows,
      sessionBytes,
      totalTokens,
      trackedTokens
    };
  }

  function tokenLedgerVerdict(ledger) {
    if (!ledger?.hasData) {
      return {
        label: "Waiting for usage",
        detail: "No backend token data has arrived for this turn.",
        className: "unknown"
      };
    }
    const history = ledger.estimatedHistoryTokens || 0;
    const aha = ledger.ahaPromptTokens || 0;
    const output = ledger.outputTokens || 0;
    const input = ledger.backendInputTokens || 0;
    if (history > Math.max(aha * 2, output, 2000)) {
      return {
        label: "Backend history dominates",
        detail: `${formatMetricNumber(history)} history tokens vs ${formatMetricNumber(aha)} AHA prompt`,
        className: "history"
      };
    }
    if (output > Math.max(input * 0.75, aha * 2, 2000)) {
      return {
        label: "Output dominates",
        detail: `${formatMetricNumber(output)} output tokens will become future history`,
        className: "output"
      };
    }
    if (aha > Math.max(history, output, 2000)) {
      return {
        label: "AHA prompt dominates",
        detail: `${formatMetricNumber(aha)} AHA prompt tokens are the largest visible source`,
        className: "aha"
      };
    }
    if (input > 0) {
      return {
        label: "Input tracked",
        detail: `${formatMetricNumber(input)} backend input · ${ledger.contextPercent || "context unknown"}`,
        className: "input"
      };
    }
    return {
      label: "Prompt tracked",
      detail: `${formatMetricNumber(aha)} AHA prompt tokens`,
      className: "aha"
    };
  }

  function componentMetricRows(components, totalChars) {
    return Object.entries(components || {})
      .map(([name, metric]) => ({
        name,
        chars: Number(metric?.chars || 0),
        bytes: Number(metric?.bytes || 0),
        lines: Number(metric?.lines || 0)
      }))
      .sort((left, right) => right.chars - left.chars)
      .map(item => {
        const percent = totalChars > 0 ? Math.min(100, Math.max(0, item.chars / totalChars * 100)) : 0;
        return { ...item, percent };
      });
  }

  function promptRefPath(promptRef) {
    if (!promptRef) return "";
    if (typeof promptRef === "string") return promptRef.trim();
    return String(promptRef.path || promptRef.ref || "").trim();
  }

  function promptArtifactMeta(promptRef, total = {}) {
    const ref = promptRef && typeof promptRef === "object" ? promptRef : {};
    return {
      chars: ref.chars ?? total.chars,
      bytes: ref.bytes ?? total.bytes,
      lines: ref.lines ?? total.lines,
      created_at: ref.created_at || ""
    };
  }

  window.AHAPromptMetrics = Object.freeze({
    contextPressureHasPercent,
    formatMetricNumber,
    formatMetricCompact,
    formatMetricBytes,
    contextPressureStatus,
    contextPressurePercent,
    contextPressureSummary,
    agentContextPressureSummary,
    formatMetricCountChars,
    metricMapRows,
    usageCacheReadTokens,
    usageCacheCreationTokens,
    tokenLedgerFromMetrics,
    tokenLedgerVerdict,
    componentMetricRows,
    promptRefPath,
    promptArtifactMeta
  });
})();
