(() => {
  const lifecycleFilterOptions = ["active", "hidden", "archived", "all"];

  function t(key, fallback = "") {
    return window.AHAI18n?.t?.(key, fallback) || fallback;
  }

  function formatText(key, values = {}, fallback = "") {
    return window.AHAI18n?.format?.(key, values, fallback) || fallback;
  }

  function runIdOf(run) {
    return String(run?.id || run?.run_id || "").trim();
  }

  function runTitleOf(run) {
    const goal = String(run?.goal || "").trim();
    return goal || runIdOf(run) || t("run.unnamed", "Untitled run");
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
    return runLifecycleStatusLabel(runLifecycleStatus(run));
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
    const label = runLifecycleStatusLabel(status);
    return timestamp
      ? formatText("run.lifecycle_since", { status: label, timestamp }, `${label} since ${timestamp}`)
      : label;
  }

  function sessionOptionLabel(run) {
    return runTitleOf(run);
  }

  function runListMeta(run) {
    const status = run?.status ? String(run.status) : "";
    const taskCount = Number.isFinite(run?.task_count)
      ? formatText("run.task_count", { count: run.task_count }, `${run.task_count} tasks`)
      : "";
    return [status, taskCount].filter(Boolean).join(" | ");
  }

  function runHasRunningWork(run) {
    return Boolean(run?.has_running_work || Number(run?.running_task_count || 0) > 0 || Number(run?.running_agent_count || 0) > 0);
  }

  function runLifecycleProtectionReason(run, currentRunId = "") {
    if (runHasRunningWork(run)) return "running_work";
    return "";
  }

  function runDeleteProtectionReason(run, currentRunId = "") {
    const id = runIdOf(run);
    if (id && id === String(currentRunId || "").trim()) return "current_run";
    if (runHasRunningWork(run)) return "running_work";
    return "";
  }

  function runLifecycleReasonText(reason) {
    if (reason === "running_work") return t("run.lifecycle_protected_running", "Run has running tasks");
    if (reason === "current_run") return t("run.lifecycle_protected_current", "Current run is protected");
    if (reason === "active_heartbeat") return t("run.lifecycle_protected_heartbeat", "Active heartbeat is protected");
    if (reason === "run_not_found") return t("run.lifecycle_run_not_found", "Run not found");
    return reason || "";
  }

  function runLifecycleActionItems(run, currentRunId = "") {
    const status = runLifecycleStatus(run);
    const reason = runLifecycleProtectionReason(run, currentRunId);
    const disabled = Boolean(reason);
    const actions = status === "archived"
      ? [{ status: "active", label: t("run.lifecycle_restore", "Restore") }]
      : status === "hidden"
        ? [{ status: "active", label: t("run.lifecycle_restore", "Restore") }, { status: "archived", label: t("run.lifecycle_archive", "Archive") }]
        : [{ status: "hidden", label: t("run.lifecycle_hide", "Hide") }, { status: "archived", label: t("run.lifecycle_archive", "Archive") }];
    return actions.map(action => ({ ...action, disabled, reason }));
  }

  function runDeleteActionItem(run, currentRunId = "") {
    const reason = runDeleteProtectionReason(run, currentRunId);
    return {
      label: t("run.delete", "Delete"),
      disabled: Boolean(reason),
      reason,
      title: reason ? runLifecycleReasonText(reason) : `${t("run.delete", "Delete")} ${runIdOf(run)}`
    };
  }

  function runLifecycleStatusLabel(status) {
    const value = String(status || "").trim().toLowerCase();
    if (value === "hidden") return t("run.lifecycle_hidden", "Hidden");
    if (value === "archived") return t("run.lifecycle_archived", "Archived");
    if (value === "all") return t("run.lifecycle_all", "All");
    return t("run.lifecycle_active", "Active");
  }

  function runLifecycleFilterLabel(filter) {
    return runLifecycleStatusLabel(filter);
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
      ? t("run.lifecycle_no_runs", "No runs available")
      : !filteredRuns.length
        ? formatText("run.lifecycle_no_runs_filter", { filter: selectedLabel }, `No ${selectedLabel} runs in this filter`)
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
      const deleteAction = runDeleteActionItem(run, currentRunId);
      return {
        id,
        title: runTitleOf(run),
        meta: runListMeta(run),
        current: id === String(currentRunId || "").trim(),
        lifecycle: runLifecycleLabel(run),
        lifecycleClass: runLifecycleClass(run),
        reason,
        reasonText,
        actionInFlight,
        settingsTitle: formatText("run.settings_for", { run: id }, `Run settings for ${id}`),
        actions,
        deleteAction: { ...deleteAction, disabled: Boolean(deleteAction.disabled || actionInFlight) },
        maintenance: { label: t("run.maintenance", "Diagnostics"), title: `${t("run.maintenance", "Diagnostics")} ${id}` }
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
    runLifecycleStatusLabel,
    runLifecycleClass,
    runLifecycleTitle,
    sessionOptionLabel,
    runHasRunningWork,
    runLifecycleProtectionReason,
    runDeleteProtectionReason,
    runLifecycleReasonText,
    runLifecycleActionItems,
    runDeleteActionItem,
    runLifecycleFilterLabel,
    runMatchesLifecycleFilter,
    runLifecycleFilterCounts,
    runLifecycleFilterViewItems,
    runLifecycleActionsView
  });
})();
