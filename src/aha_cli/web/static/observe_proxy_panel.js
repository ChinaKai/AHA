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

  function bytesText(value) {
    const bytes = Number(value || 0);
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function localTimeText(value) {
    const text = String(value || "").trim();
    if (!text) return "";
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return text;
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
      timeZoneName: "short"
    }).format(date);
  }

  function formatJsonText(text) {
    const value = String(text || "").trim();
    if (!value) return "";
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch (_err) {
      return "";
    }
  }

  function formatSseText(text) {
    const lines = String(text || "").split(/\r?\n/);
    const events = [];
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) continue;
      const payload = trimmed.slice(5).trim();
      if (!payload) continue;
      if (payload === "[DONE]") {
        events.push("[DONE]");
        continue;
      }
      try {
        events.push(JSON.parse(payload));
      } catch (_err) {
        events.push(payload);
      }
    }
    return events.length ? JSON.stringify(events, null, 2) : "";
  }

  function formatObservedBody(text) {
    const value = String(text || "");
    return formatJsonText(value) || formatSseText(value) || value;
  }

  function previewHtml(label, artifact = {}, item = {}) {
    const preview = String(artifact.preview || "");
    const ref = String(artifact.ref || "");
    const requestId = String(item.request_id || "");
    const kind = String(label || "").toLowerCase();
    const suffix = artifact.truncated ? " ..." : "";
    if (!preview && !ref) return "";
    const bytes = Number(artifact.bytes || 0);
    const body = preview || (bytes > 0 ? `(${bytesText(bytes)} captured, preview unavailable)` : "(empty)");
    return `
      <div class="observe-proxy-preview">
        <button type="button" class="observe-proxy-preview-trigger" data-observe-proxy-detail="${escapeHtml(kind)}" data-observe-proxy-request-id="${escapeHtml(requestId)}">
          <span>${escapeHtml(label)}</span>
          <code>${escapeHtml(ref || "-")}</code>
          <em>${escapeHtml(t("observe_proxy.open_full", "Open full"))}</em>
        </button>
        <pre>${escapeHtml(body)}${escapeHtml(suffix)}</pre>
      </div>
    `;
  }

  function createObserveProxyController(elements = {}, deps = {}) {
    const button = elements.observeProxyEl;
    const popover = elements.observeProxyPopoverEl;
    const sessionMenu = elements.sessionMenuEl;
    let open = false;
    let loading = false;
    let error = "";
    let recentLoading = false;
    let recentError = "";
    let status = null;
    let selectedTaskId = "";
    let selectedForwardId = "";
    let bound = false;
    const taskRecentCache = new Map();
    const detailBodyCache = new Map();
    let detailEl = null;
    let detailState = {
      open: false,
      loading: false,
      error: "",
      title: "",
      subtitle: "",
      body: "",
      requestId: "",
      kind: ""
    };

    function statusLine() {
      if (loading) return t("observe_proxy.loading", "Loading Observe Proxy...");
      if (error) return error;
      if (!status) return t("observe_proxy.status_unknown", "Status unknown");
      if (status.running) return t("observe_proxy.running", "Runtime running");
      return t("observe_proxy.ready", "Ready");
    }

    function statusGridHtml() {
      const usage = status?.usage || {};
      const rows = [
        ["Runtime", status?.running ? "running" : "stopped"],
        ["Port", status?.port || "-"],
        ["Enabled tasks", numberText(usage.enabled_tasks)],
        ["Ready turns", numberText(usage.ready_turns)],
        ["Requests", numberText(usage.requests)],
        ["Responses", numberText(usage.responses)]
      ];
      return `<div class="observe-proxy-status-grid">${rows.map(([label, value]) => `
        <div><span>${escapeHtml(label)}</span><code>${escapeHtml(value)}</code></div>
      `).join("")}</div>`;
    }

    function usageByTaskHtml() {
      const usage = status?.usage || {};
      const tasks = Array.isArray(usage.tasks) ? usage.tasks : [];
      if (!tasks.length) {
        return `<div class="observe-proxy-usage-empty">${escapeHtml(t("observe_proxy.usage_empty", "No observed task turns recorded for this run."))}</div>`;
      }
      const hiddenCount = Math.max(0, Number(usage.task_count || 0) - tasks.length);
      const rows = tasks.map(task => {
        const taskId = String(task.task_id || "run");
        const selectedClass = taskId === selectedTaskId ? " selected" : "";
        const agents = Array.isArray(task.agents) ? task.agents : [];
        const agentSummary = agents.map(agent => `${agent.agent_id || "main"} ${numberText(agent.responses)} response(s)`).join(" · ");
        return `
          <button type="button" class="observe-proxy-usage-row${selectedClass}" data-observe-proxy-task="${escapeHtml(taskId)}" aria-pressed="${taskId === selectedTaskId}">
            <div>
              <strong>${escapeHtml(taskId)}</strong>
              <span>${escapeHtml(agentSummary || (task.enabled ? t("observe_proxy.enabled_no_turns", "enabled, no turns yet") : "-"))}</span>
            </div>
            <code>${escapeHtml(numberText(task.requests))} / ${escapeHtml(numberText(task.responses))}</code>
          </button>
        `;
      }).join("");
      return `
        <section class="observe-proxy-usage">
          <div class="observe-proxy-usage-head">
            <span>${escapeHtml(t("observe_proxy.by_task", "By task"))}</span>
            <code>${escapeHtml(t("observe_proxy.req_res", "request / response"))}</code>
          </div>
          ${rows}
          ${hiddenCount ? `<div class="observe-proxy-usage-more">${escapeHtml(t("observe_proxy.usage_more", "{count} more task(s)").replace("{count}", numberText(hiddenCount)))}</div>` : ""}
        </section>
      `;
    }

    function usageText(usage) {
      if (!usage || typeof usage !== "object") return "";
      const parts = [];
      for (const key of ["input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens", "reasoning_output_tokens"]) {
        if (usage[key] !== undefined && usage[key] !== null) parts.push(`${key}=${usage[key]}`);
      }
      return parts.join(" ");
    }

    function observeProxyUrl(taskId = "", options = {}) {
      const base = deps.apiUrl?.("/api/integrations/observe-proxy") || "/api/integrations/observe-proxy";
      const params = [];
      if (taskId) params.push(`task_id=${encodeURIComponent(taskId)}`);
      if (options.requestId) params.push(`request_id=${encodeURIComponent(options.requestId)}`);
      if (options.full) params.push("full=1");
      if (options.previewChars !== undefined) params.push(`preview_chars=${encodeURIComponent(options.previewChars)}`);
      if (!params.length) return base;
      const separator = base.includes("?") ? "&" : "?";
      return `${base}${separator}${params.join("&")}`;
    }

    function recentItems() {
      return Array.isArray(status?.usage?.recent) ? status.usage.recent : [];
    }

    function findRecentItem(requestId) {
      return recentItems().find(item => String(item?.request_id || "") === String(requestId || "")) || null;
    }

    function taskTotals(taskId) {
      const targetId = String(taskId || "");
      const task = (Array.isArray(status?.usage?.tasks) ? status.usage.tasks : [])
        .find(item => String(item?.task_id || "run") === targetId);
      if (!task) return null;
      return {
        requests: Number(task.requests || 0),
        responses: Number(task.responses || 0)
      };
    }

    function cachedRecentForTask(taskId) {
      const cache = taskRecentCache.get(String(taskId || ""));
      const totals = taskTotals(taskId);
      if (!cache || !totals) return null;
      if (cache.requests !== totals.requests || cache.responses !== totals.responses) return null;
      return cache.recent;
    }

    function setRecentItemsForTask(taskId, recent) {
      const targetId = String(taskId || "");
      if (!status) status = {};
      const usage = status.usage && typeof status.usage === "object" ? status.usage : {};
      const taskRecent = (Array.isArray(recent) ? recent : []).filter(item => String(item?.task_id || "run") === targetId);
      if (selectedForwardId && !taskRecent.some(item => String(item?.request_id || "") === selectedForwardId)) selectedForwardId = "";
      status = {
        ...status,
        usage: {
          ...usage,
          recent: taskRecent
        }
      };
    }

    function rememberRecentForTask(taskId) {
      const totals = taskTotals(taskId);
      if (!totals) return;
      const targetId = String(taskId || "");
      taskRecentCache.set(targetId, {
        requests: totals.requests,
        responses: totals.responses,
        recent: recentItems().filter(item => String(item?.task_id || "run") === targetId)
      });
    }

    function detailSubtitle(item, kind, artifact) {
      const parts = [
        item?.task_id || selectedTaskId || "run",
        item?.agent_id || "main",
        item?.backend || "-",
        item?.status ? `status ${item.status}` : "",
        artifact?.bytes !== undefined ? bytesText(artifact.bytes) : "",
        artifact?.ref || ""
      ].filter(Boolean);
      return `${kind} · ${parts.join(" · ")}`;
    }

    function ensureDetailEl() {
      if (detailEl) return detailEl;
      const doc = deps.documentRef || document;
      detailEl = doc.createElement("div");
      detailEl.className = "observe-proxy-detail-root";
      detailEl.hidden = true;
      doc.body.appendChild(detailEl);
      detailEl.addEventListener("click", event => {
        event.stopPropagation();
        const target = event.target instanceof Element ? event.target : null;
        if (target?.closest("[data-observe-proxy-detail-close]") || target?.classList?.contains("observe-proxy-detail-backdrop")) {
          closeDetail();
        }
      });
      doc.addEventListener("keydown", event => {
        if (event.key !== "Escape" || !detailState.open) return;
        event.preventDefault();
        event.stopPropagation();
        closeDetail();
      }, true);
      return detailEl;
    }

    function renderDetail() {
      const root = ensureDetailEl();
      root.hidden = !detailState.open;
      if (!detailState.open) {
        root.innerHTML = "";
        return;
      }
      const body = detailState.loading
        ? t("observe_proxy.detail_loading", "Loading full body...")
        : (detailState.error || detailState.body || "(empty)");
      root.innerHTML = `
        <div class="observe-proxy-detail-backdrop">
          <section class="observe-proxy-detail-dialog" role="dialog" aria-modal="true" aria-labelledby="observe-proxy-detail-title">
            <div class="observe-proxy-detail-head">
              <div>
                <h3 id="observe-proxy-detail-title">${escapeHtml(detailState.title || t("observe_proxy.detail_title", "Observed body"))}</h3>
                <span>${escapeHtml(detailState.subtitle || "")}</span>
              </div>
              <button type="button" data-observe-proxy-detail-close aria-label="${escapeHtml(t("common.close", "Close"))}">×</button>
            </div>
            <pre class="${detailState.error ? "error" : ""}">${escapeHtml(body)}</pre>
          </section>
        </div>
      `;
    }

    function closeDetail() {
      detailState = { ...detailState, open: false, loading: false };
      renderDetail();
    }

    async function openDetail(requestId, kind) {
      const item = findRecentItem(requestId);
      const normalizedKind = kind === "response" ? "response" : "request";
      const artifact = item?.[normalizedKind] || {};
      const taskId = String(item?.task_id || selectedTaskId || "").trim();
      const cacheKey = `${String(requestId || "")}:${normalizedKind}`;
      detailState = {
        open: true,
        loading: !detailBodyCache.has(cacheKey),
        error: "",
        title: normalizedKind === "response" ? t("observe_proxy.response_body", "Response body") : t("observe_proxy.request_body", "Request body"),
        subtitle: detailSubtitle(item, normalizedKind, artifact),
        body: detailBodyCache.get(cacheKey) || "",
        requestId: String(requestId || ""),
        kind: normalizedKind
      };
      renderDetail();
      if (detailBodyCache.has(cacheKey)) return;
      try {
        const payload = await deps.fetchJson?.(
          observeProxyUrl(taskId, { requestId: String(requestId || ""), full: true }),
          {},
          "Failed to load full observed body"
        );
        const fullItem = (Array.isArray(payload?.observe_proxy?.usage?.recent) ? payload.observe_proxy.usage.recent : [])
          .find(entry => String(entry?.request_id || "") === String(requestId || "")) || item;
        const fullArtifact = fullItem?.[normalizedKind] || artifact;
        detailState = {
          ...detailState,
          loading: false,
          subtitle: detailSubtitle(fullItem, normalizedKind, fullArtifact),
          body: formatObservedBody(fullArtifact?.preview || "")
        };
        detailBodyCache.set(cacheKey, detailState.body);
      } catch (err) {
        detailState = { ...detailState, loading: false, error: String(err?.message || err || "Failed to load full observed body") };
      }
      renderDetail();
    }

    function recentHtml() {
      if (!selectedTaskId) {
        return `<div class="observe-proxy-usage-empty">${escapeHtml(t("observe_proxy.select_task", "Select a task to view forwarded requests."))}</div>`;
      }
      if (recentLoading) {
        return `<div class="observe-proxy-usage-empty">${escapeHtml(t("observe_proxy.loading", "Loading Observe Proxy..."))}</div>`;
      }
      if (recentError) {
        return `<div class="observe-proxy-usage-empty error">${escapeHtml(recentError)}</div>`;
      }
      const recent = recentItems()
        .filter(item => String(item?.task_id || "run") === selectedTaskId);
      if (!recent.length) {
        return `<div class="observe-proxy-usage-empty">${escapeHtml(t("observe_proxy.recent_task_empty", "No forwarded requests for this task yet."))}</div>`;
      }
      return `
        <section class="observe-proxy-recent">
          <div class="observe-proxy-usage-head">
            <span>${escapeHtml(t("observe_proxy.recent", "Recent forwards"))}</span>
            <code>${escapeHtml(t("observe_proxy.recent_for_task", "For {task}").replace("{task}", selectedTaskId))}</code>
          </div>
          ${recent.map(item => {
            const requestId = String(item.request_id || "");
            const selected = requestId && requestId === selectedForwardId;
            const requestTime = localTimeText(item.request_ts || item.ts);
            const meta = [
              item.agent_id || "main",
              item.backend || "-",
              requestTime ? `${t("observe_proxy.request_time", "request")} ${requestTime}` : "",
              item.status ? `status ${item.status}` : "status -",
              item.duration_ms !== undefined && item.duration_ms !== null ? `${item.duration_ms}ms` : "",
              `${bytesText(item.request_bytes)} -> ${bytesText(item.response_bytes)}`
            ].filter(Boolean).join(" · ");
            return `
              <article class="observe-proxy-forward${selected ? " selected" : ""}">
                <button type="button" class="observe-proxy-forward-row" data-observe-proxy-forward="${escapeHtml(requestId)}" aria-expanded="${selected ? "true" : "false"}">
                  <strong>${escapeHtml(`${item.method || "-"} ${item.path || "-"}`)}</strong>
                  <span>${escapeHtml(meta)}</span>
                  ${usageText(item.usage) ? `<code>${escapeHtml(usageText(item.usage))}</code>` : ""}
                </button>
                ${selected ? `<div class="observe-proxy-forward-body">
                  ${previewHtml("Request", item.request, item)}
                  ${previewHtml("Response", item.response, item)}
                </div>` : ""}
              </article>
            `;
          }).join("")}
        </section>
      `;
    }

    function renderPopover() {
      if (!popover || !open) return;
      const stateClass = error ? "error" : "";
      popover.innerHTML = `<section class="observe-proxy-panel">
        <div class="observe-proxy-head">
          <div>
            <h3>${escapeHtml(t("observe_proxy.title", "Observe Proxy"))}</h3>
            <div class="meta ${stateClass}">${escapeHtml(statusLine())}</div>
          </div>
          <button type="button" data-observe-proxy-refresh ${loading ? "disabled" : ""}>${escapeHtml(t("common.refresh", "Refresh"))}</button>
        </div>
        ${statusGridHtml()}
        ${usageByTaskHtml()}
        ${recentHtml()}
        <div class="field-help">${escapeHtml(t("observe_proxy.task_hint", "Enable per task in Task settings. Captured bodies are stored under the run's network_io artifacts."))}</div>
      </section>`;
    }

    async function loadTaskRecent(taskId, options = {}) {
      const nextTaskId = String(taskId || "").trim();
      if (!nextTaskId) return;
      if (selectedTaskId !== nextTaskId) selectedForwardId = "";
      selectedTaskId = nextTaskId;
      const cachedRecent = options.force ? null : cachedRecentForTask(nextTaskId);
      if (cachedRecent) {
        recentError = "";
        recentLoading = false;
        setRecentItemsForTask(nextTaskId, cachedRecent);
        if (open) renderPopover();
        return;
      }
      recentLoading = true;
      recentError = "";
      if (!options.silent) renderPopover();
      try {
        const payload = await deps.fetchJson?.(observeProxyUrl(nextTaskId), {}, "Failed to load Observe Proxy task forwards");
        status = payload?.observe_proxy || status;
        rememberRecentForTask(nextTaskId);
        if (selectedForwardId && !recentItems().some(item => String(item?.request_id || "") === selectedForwardId)) selectedForwardId = "";
      } catch (err) {
        recentError = String(err?.message || err || "Failed to load Observe Proxy task forwards");
      } finally {
        recentLoading = false;
        if (open) renderPopover();
      }
    }

    async function loadObserveProxy(options = {}) {
      if (!options.silent) {
        loading = true;
        error = "";
        renderPopover();
      }
      let shouldLoadSelectedTask = false;
      try {
        const payload = await deps.fetchJson?.(observeProxyUrl(), {}, "Failed to load Observe Proxy status");
        status = payload?.observe_proxy || null;
        const tasks = Array.isArray(status?.usage?.tasks) ? status.usage.tasks : [];
        if (selectedTaskId && !tasks.some(task => String(task?.task_id || "run") === selectedTaskId)) {
          selectedTaskId = "";
          selectedForwardId = "";
          recentError = "";
        }
        if (selectedTaskId) {
          const cachedRecent = cachedRecentForTask(selectedTaskId);
          if (cachedRecent) {
            setRecentItemsForTask(selectedTaskId, cachedRecent);
          } else {
            shouldLoadSelectedTask = true;
          }
        }
      } catch (err) {
        error = String(err?.message || err || "Failed to load Observe Proxy");
      } finally {
        loading = false;
        if (open) renderPopover();
      }
      if (shouldLoadSelectedTask) await loadTaskRecent(selectedTaskId, { silent: options.silent });
    }

    function setOpen(value) {
      open = Boolean(value);
      button?.setAttribute("aria-expanded", String(open));
      sessionMenu?.classList?.toggle("observe-proxy-open", open);
      if (popover) popover.hidden = !open;
      if (open) {
        deps.setPlayConsoleOpen?.(false);
        deps.setRunMaintenanceConsoleOpen?.(false);
        deps.setSkillsConsoleOpen?.(false);
        deps.setTokenUsageOpen?.(false);
        deps.setWeixinConsoleOpen?.(false);
        void loadObserveProxy({ silent: Boolean(status) });
      }
      renderPopover();
    }

    function bind() {
      if (bound) return;
      bound = true;
      popover?.addEventListener("click", event => {
        const target = event.target instanceof Element ? event.target : null;
        const taskButton = target?.closest("[data-observe-proxy-task]");
        if (taskButton) {
          void loadTaskRecent(String(taskButton.getAttribute("data-observe-proxy-task") || ""));
          return;
        }
        const forwardButton = target?.closest("[data-observe-proxy-forward]");
        if (forwardButton) {
          const requestId = String(forwardButton.getAttribute("data-observe-proxy-forward") || "");
          selectedForwardId = selectedForwardId === requestId ? "" : requestId;
          renderPopover();
          return;
        }
        const detailButton = target?.closest("[data-observe-proxy-detail]");
        if (detailButton) {
          void openDetail(
            String(detailButton.getAttribute("data-observe-proxy-request-id") || ""),
            String(detailButton.getAttribute("data-observe-proxy-detail") || "")
          );
          return;
        }
        if (target?.closest("[data-observe-proxy-refresh]")) void loadObserveProxy();
      });
    }

    return Object.freeze({
      bind,
      isOpen: () => open,
      loadObserveProxy,
      renderObserveProxyPopover: renderPopover,
      setObserveProxyOpen: setOpen
    });
  }

  window.AHAObserveProxy = Object.freeze({ createObserveProxyController });
})();
