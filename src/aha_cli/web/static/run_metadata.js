(() => {
  const lifecycleFilterOptions = ["active", "hidden", "archived", "all"];

  function runIdOf(run) {
    return String(run?.id || run?.run_id || "").trim();
  }

  function runTitleOf(run) {
    const goal = String(run?.goal || "").trim();
    return goal || runIdOf(run) || "未命名 Run";
  }

  function runUpdatedAtOf(run) {
    return run?.updated_at || run?.created_at || "";
  }

  function runLifecycleStatus(run) {
    const lifecycle = run?.lifecycle || {};
    const raw = String(lifecycle.status || run?.lifecycle_status || "").trim().toLowerCase();
    if (raw === "archived" || run?.archived || run?.archived_at || lifecycle.archived || lifecycle.archived_at) return "archived";
    if (raw === "hidden" || run?.hidden || run?.hidden_at || lifecycle.hidden || lifecycle.hidden_at) return "hidden";
    if (raw === "active") return "active";
    return "active";
  }

  function runLifecycleLabel(run) {
    return runLifecycleStatus(run);
  }

  function runLifecycleClass(run) {
    return `run-lifecycle-${runLifecycleStatus(run)}`;
  }

  function runLifecycleTitle(run) {
    const lifecycle = run?.lifecycle || {};
    const status = runLifecycleStatus(run);
    const timestamp = status === "archived"
      ? (run?.archived_at || lifecycle.archived_at || "")
      : status === "hidden"
        ? (run?.hidden_at || lifecycle.hidden_at || "")
        : "";
    return timestamp ? `${status} since ${timestamp}` : status;
  }

  function sessionOptionLabel(run) {
    const title = runTitleOf(run);
    const lifecycle = ` · ${runLifecycleLabel(run)}`;
    const status = run?.status ? ` · ${run.status}` : "";
    const taskCount = Number.isFinite(run?.task_count) ? ` · ${run.task_count} 个任务` : "";
    return `${title}${lifecycle}${status}${taskCount}`;
  }

  function runLifecycleProtectionReason(run, currentRunId = "") {
    const id = runIdOf(run);
    if (id && id === String(currentRunId || "").trim()) return "current_run";
    if (run?.active_heartbeat) return "active_heartbeat";
    return "";
  }

  function runLifecycleReasonText(reason) {
    if (reason === "current_run") return "Current run is protected";
    if (reason === "active_heartbeat") return "Active heartbeat is protected";
    if (reason === "run_not_found") return "Run not found";
    return reason || "";
  }

  function runLifecycleActionItems(run, currentRunId = "") {
    const status = runLifecycleStatus(run);
    const reason = runLifecycleProtectionReason(run, currentRunId);
    const disabled = Boolean(reason);
    const actions = status === "archived"
      ? [{ status: "active", label: "Restore" }]
      : status === "hidden"
        ? [{ status: "active", label: "Restore" }, { status: "archived", label: "Archive" }]
        : [{ status: "hidden", label: "Hide" }, { status: "archived", label: "Archive" }];
    return actions.map(action => ({ ...action, disabled, reason }));
  }

  function runLifecycleFilterLabel(filter) {
    const value = String(filter || "").trim().toLowerCase();
    if (value === "hidden") return "Hidden";
    if (value === "archived") return "Archived";
    if (value === "all") return "All";
    return "Active";
  }

  function runMatchesLifecycleFilter(run, filter = "active") {
    const value = String(filter || "active").trim().toLowerCase();
    if (value === "all") return true;
    return runLifecycleStatus(run) === value;
  }

  function runLifecycleFilterCounts(runs = []) {
    const counts = { active: 0, hidden: 0, archived: 0, all: 0 };
    for (const run of Array.isArray(runs) ? runs : []) {
      const status = runLifecycleStatus(run);
      counts[status] = (counts[status] || 0) + 1;
      counts.all += 1;
    }
    return counts;
  }

  function runLifecycleFilterViewItems(runs = [], selectedFilter = "active") {
    const counts = runLifecycleFilterCounts(runs);
    return lifecycleFilterOptions.map(filter => ({
      filter,
      selected: filter === selectedFilter,
      count: counts[filter] || 0,
      label: `${runLifecycleFilterLabel(filter)} ${counts[filter] || 0}`
    }));
  }

  function runLifecycleActionsView(runs = [], selectedFilter = "active", currentRunId = "", actionInFlight = false) {
    const safeRuns = Array.isArray(runs) ? runs : [];
    const filteredRuns = safeRuns.filter(run => runMatchesLifecycleFilter(run, selectedFilter));
    const selectedLabel = runLifecycleFilterLabel(selectedFilter).toLowerCase();
    const emptyMessage = !safeRuns.length
      ? "No runs available"
      : !filteredRuns.length
        ? `No ${selectedLabel} runs in this filter`
        : "";
    const rows = filteredRuns.map(run => {
      const id = runIdOf(run);
      if (!id) return null;
      const reason = runLifecycleProtectionReason(run, currentRunId);
      const reasonText = runLifecycleReasonText(reason);
      const actions = runLifecycleActionItems(run, currentRunId).map(action => ({
        status: action.status,
        label: action.label,
        disabled: Boolean(action.disabled || actionInFlight),
        reason: action.reason || "",
        title: action.disabled ? reasonText : `${action.label} ${id}`
      }));
      return {
        id,
        title: runTitleOf(run),
        lifecycle: runLifecycleLabel(run),
        lifecycleClass: runLifecycleClass(run),
        reason,
        reasonText,
        actions
      };
    }).filter(Boolean);
    return {
      filters: runLifecycleFilterViewItems(safeRuns, selectedFilter),
      emptyMessage,
      rows
    };
  }

  window.AHARunMetadata = Object.freeze({
    lifecycleFilterOptions,
    runIdOf,
    runTitleOf,
    runUpdatedAtOf,
    runLifecycleStatus,
    runLifecycleLabel,
    runLifecycleClass,
    runLifecycleTitle,
    sessionOptionLabel,
    runLifecycleProtectionReason,
    runLifecycleReasonText,
    runLifecycleActionItems,
    runLifecycleFilterLabel,
    runMatchesLifecycleFilter,
    runLifecycleFilterCounts,
    runLifecycleFilterViewItems,
    runLifecycleActionsView
  });
})();
