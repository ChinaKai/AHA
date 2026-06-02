(() => {
  const agentMetadata = window.AHAAgentMetadata;
  const taskVisibilityFilterOptions = ["active", "closed", "hidden", "all"];
  const {
    agentBackendProcessStatus,
    agentBackendProcessLabel,
    agentLifecycleStatus,
    agentWaitingReason,
    agentLifecycleDisplay,
    agentLifecycleLabel
  } = agentMetadata;
  const terminalTaskStatuses = new Set(["completed", "failed", "blocked"]);

  function taskCurrentStatus(task) {
    return String(task?.current_status || task?.status || "pending").toLowerCase();
  }

  function taskOutcomeStatus(task) {
    const raw = task?.outcome_status || (terminalTaskStatuses.has(taskCurrentStatus(task)) ? taskCurrentStatus(task) : "");
    return raw ? String(raw).toLowerCase() : "";
  }

  function taskActivityStatus(task) {
    return String(task?.activity_status || (taskCurrentStatus(task) === "running" ? "running" : "idle")).toLowerCase();
  }

  function taskDisplayStatus(task) {
    return String(task?.display_status || taskOutcomeStatus(task) || taskCurrentStatus(task)).toLowerCase();
  }

  function taskProxyConfigured(task) {
    return Boolean(task?.run_proxy_configured || task?.preferred_http_proxy || task?.preferred_https_proxy);
  }

  function taskProxySummary(task, runProxy = null) {
    if (!task) return "";
    const runConfig = runProxy || {};
    const runConfigured = Boolean(task.run_proxy_configured || runConfig.http_proxy || runConfig.https_proxy);
    if (runConfigured) {
      return `${task.preferred_proxy_enabled ? "switch on" : "switch off"} · Core proxy`;
    }
    const parts = [];
    if (task.preferred_http_proxy) parts.push("HTTP");
    if (task.preferred_https_proxy) parts.push("HTTPS");
    if (task.preferred_no_proxy) parts.push("NO_PROXY");
    return parts.length ? `${task.preferred_proxy_enabled ? "default on" : "default off"} · ${parts.join(" · ")}` : "not configured";
  }

  function taskAgentCount(task) {
    const value = Number(task?.agent_count);
    if (Number.isFinite(value)) return value;
    return (task?.agents || []).length;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function normalizeTaskVisibilityFilter(filter = "active") {
    if (filter === true) return "all";
    if (filter === false || filter === null || filter === undefined) return "active";
    const value = String(filter || "active").trim().toLowerCase();
    return taskVisibilityFilterOptions.includes(value) ? value : "active";
  }

  function taskVisibilityStatus(task) {
    if (task?.hidden) return "hidden";
    return taskCurrentStatus(task) === "completed" ? "closed" : "active";
  }

  function taskVisibilityFilterLabel(filter) {
    const value = normalizeTaskVisibilityFilter(filter);
    if (value === "closed") return "Closed";
    if (value === "hidden") return "Hidden";
    if (value === "all") return "All";
    return "Active";
  }

  function taskMatchesVisibilityFilter(task, filter = "active") {
    const value = normalizeTaskVisibilityFilter(filter);
    if (value === "all") return true;
    return taskVisibilityStatus(task) === value;
  }

  function visibleTasks(tasks = [], filter = "active") {
    const items = Array.isArray(tasks) ? tasks : [];
    return items.filter(task => taskMatchesVisibilityFilter(task, filter));
  }

  function taskVisibilityFilterCounts(tasks = []) {
    const counts = { active: 0, closed: 0, hidden: 0, all: 0 };
    for (const task of Array.isArray(tasks) ? tasks : []) {
      const status = taskVisibilityStatus(task);
      counts[status] = (counts[status] || 0) + 1;
      counts.all += 1;
    }
    return counts;
  }

  function taskVisibilityFilterViewItems(tasks = [], selectedFilter = "active") {
    const selected = normalizeTaskVisibilityFilter(selectedFilter);
    const counts = taskVisibilityFilterCounts(tasks);
    return taskVisibilityFilterOptions.map(filter => ({
      filter,
      selected: filter === selected,
      count: counts[filter] || 0,
      label: `${taskVisibilityFilterLabel(filter)} ${counts[filter] || 0}`
    }));
  }

  function taskVisibilityFilterHtml(tasks = [], selectedFilter = "active") {
    return taskVisibilityFilterViewItems(tasks, selectedFilter).map(item => {
      return `<button class="task-list-filter${item.selected ? " active" : ""}" type="button" data-task-visibility-filter="${escapeHtml(item.filter)}" aria-pressed="${item.selected ? "true" : "false"}">${escapeHtml(item.label)}</button>`;
    }).join("");
  }

  function parseTimestamp(value) {
    if (!value) return null;
    const millis = Date.parse(value);
    return Number.isNaN(millis) ? null : millis;
  }

  function taskActivityMillis(task) {
    const candidates = [
      task?.started_at,
      task?.finished_at,
      task?.hidden_at,
      ...(task?.agents || []).flatMap(agent => [
        agent.last_active_at,
        agent.started_at,
        agent.finished_at,
        agent.session_updated_at
      ])
    ];
    return Math.max(0, ...candidates.map(parseTimestamp).filter(value => value !== null));
  }

  function defaultTaskId(tasks) {
    if (!tasks.length) return null;
    return tasks.reduce((latest, task) => (taskActivityMillis(task) >= taskActivityMillis(latest) ? task : latest), tasks[0]).id;
  }

  function pathName(path) {
    if (!path) return "-";
    const trimmed = String(path).replace(/\/+$/, "");
    return trimmed.split("/").filter(Boolean).pop() || trimmed || "-";
  }

  function taskStatusOrder(task) {
    const order = ["running", "awaiting_user", "pending", "blocked", "failed", "completed"];
    const status = taskDisplayStatus(task);
    const index = order.indexOf(status);
    return index >= 0 ? index : order.length;
  }

  function taskListMetaParts(task, summaries = {}) {
    return [
      `${taskAgentCount(task)} agent(s)`,
      summaries.workflow ? `workflow ${summaries.workflow}` : "",
      summaries.execution ? `execution ${summaries.execution}` : "",
      `default ${summaries.defaultBackend || task?.preferred_backend || "-"}`,
      summaries.proxy ? `proxy ${summaries.proxy}` : "",
      summaries.supervision ? `supervision ${summaries.supervision}` : "",
      summaries.context ? `context ${summaries.context}` : "",
      pathName(task?.workspace_path),
      summaries.timing || ""
    ].filter(Boolean);
  }

  function taskListTitle(task, summaries = {}) {
    return [
      `${task?.title || ""}${task?.description ? `\n\n${task.description}` : ""}`,
      `workflow=${summaries.workflow || "-"}`,
      `execution=${summaries.execution || "-"}`,
      `default backend=${summaries.defaultBackend || task?.preferred_backend || "-"}`,
      `proxy=${summaries.proxy || "not configured"}`,
      `supervision=${summaries.supervision || "-"}`,
      `context=${summaries.context || "-"}`,
      `workspace=${task?.workspace_path || "-"}`
    ].join("\n");
  }

  function taskListItemClass(task, selectedTaskId = "") {
    return `task ${task?.id === selectedTaskId ? "active" : ""} ${task?.hidden ? "hidden-task" : ""}`;
  }

  function taskListItemHtml(task, options = {}) {
    const summaries = options.summaries || {};
    const locked = terminalTaskStatuses.has(taskCurrentStatus(task));
    const completionAction = locked ? "reopen" : "final";
    const completionLabel = locked ? "Reopen" : "Final";
    const metaText = taskListMetaParts(task, summaries).join(" | ");
    const statusHtml = [
      options.statusBadgesHtml,
      options.proxyBadgeHtml,
      options.supervisionBadgeHtml,
      options.contextBadgeHtml
    ].filter(Boolean).join("");
    return `
      <div class="task-row">
        <strong>${escapeHtml(task?.id || "")}</strong>
        <span class="task-statuses">${statusHtml}</span>
      </div>
      <div class="task-title">${escapeHtml(task?.title || "")}</div>
      <div class="meta truncate">${escapeHtml(metaText)}</div>
      <div class="task-actions">
        <button class="task-action" type="button" data-action="${completionAction}">${completionLabel}</button>
        <button class="task-action" type="button" data-action="${task?.hidden ? "restore" : "hide"}">${task?.hidden ? "Restore" : "Hide"}</button>
        <button class="task-action danger" type="button" data-action="delete">Delete</button>
      </div>
    `;
  }

  window.AHATaskList = Object.freeze({
    agentBackendProcessStatus,
    agentBackendProcessLabel,
    agentLifecycleStatus,
    agentWaitingReason,
    agentLifecycleDisplay,
    agentLifecycleLabel,
    taskVisibilityFilterOptions,
    normalizeTaskVisibilityFilter,
    taskVisibilityStatus,
    taskVisibilityFilterLabel,
    taskMatchesVisibilityFilter,
    taskVisibilityFilterCounts,
    taskVisibilityFilterViewItems,
    taskVisibilityFilterHtml,
    taskCurrentStatus,
    taskOutcomeStatus,
    taskActivityStatus,
    taskDisplayStatus,
    taskProxyConfigured,
    taskProxySummary,
    taskAgentCount,
    visibleTasks,
    taskActivityMillis,
    defaultTaskId,
    pathName,
    taskStatusOrder,
    taskListMetaParts,
    taskListTitle,
    taskListItemClass,
    taskListItemHtml
  });
})();
