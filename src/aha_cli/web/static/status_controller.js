(() => {
  function createStatusController(state = {}, deps = {}) {
    const escapeHtml = deps.escapeHtml || (value => String(value ?? ""));

    function t(key, fallback = "") {
      return window.AHAI18n?.t?.(key, fallback) || fallback;
    }

    function currentRunId() {
      return String(state.currentRunId?.() || "").trim();
    }

    function selectedTaskId() {
      return state.selectedTaskId?.() || null;
    }

    function statusData() {
      return state.statusData?.() || null;
    }

    function runsData() {
      return Array.isArray(state.runsData?.()) ? state.runsData() : [];
    }

    function bootstrapData() {
      return state.bootstrapData?.() || null;
    }

    function runScopedPayload(payload = {}) {
      const runId = currentRunId();
      return runId ? { ...payload, run_id: runId } : payload;
    }

    function applyRunListData(payload = {}) {
      deps.statusStore?.applyRunListData(payload);
    }

    function applyWorkspaceData(workspaces = []) {
      deps.runtimeOptions?.applyWorkspaceData(workspaces);
    }

    function applyBootstrapPayload(payload = {}) {
      deps.clearLoginState?.();
      state.setBootstrapError?.("");
      state.setBootstrapData?.(payload);
      applyRunListData(payload);
      applyWorkspaceData(payload.workspaces || deps.runtimeOptions?.workspaceData?.() || []);
      deps.applyBackendData?.(payload.backends || []);
      deps.applyWorkflowTemplateData?.(payload.workflow_templates);
      state.setRunsError?.("");
      state.setRunsLoaded?.(true);
      deps.renderSessionMenu?.();
    }

    async function loadBootstrap() {
      const payload = await deps.fetchJson?.("/api/bootstrap", {}, "Failed to bootstrap AHA");
      applyBootstrapPayload(payload);
      return payload;
    }

    function currentRunSummary() {
      const runId = currentRunId();
      return runsData().find(run => deps.runIdOf?.(run) === runId) || null;
    }

    function fallbackCurrentRun() {
      const data = statusData();
      const id = currentRunId() || data?.run_id || state.defaultRunId?.();
      if (!id) return null;
      return {
        id,
        goal: data?.goal || t("run.current_fallback", "Current run"),
        mode: data?.mode || "",
        status: data?.status || "",
        updated_at: data?.updated_at || ""
      };
    }

    function syncRunUrl() {
      const windowRef = deps.windowRef || window;
      if (!windowRef.history?.replaceState) return;
      const url = new URL(windowRef.location.href);
      const runId = currentRunId();
      if (runId) {
        url.searchParams.set("run_id", runId);
      } else {
        url.searchParams.delete("run_id");
      }
      url.searchParams.delete("run");
      windowRef.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    }

    function resetRunScopedState() {
      deps.statusStore?.resetRunScopedState();
      if (deps.panelEl) deps.panelEl.innerHTML = `<div class="empty">${escapeHtml(t("run.switching", "Switching run..."))}</div>`;
    }

    function currentAppVersion() {
      return String(statusData()?.aha_version || bootstrapData()?.aha_version || "").trim();
    }

    async function loadRuns(force = false) {
      if (state.runsLoaded?.() && !force) {
        deps.renderSessionMenu?.();
        return;
      }
      try {
        const payload = await deps.fetchJson?.("/api/runs", {}, "Failed to load runs");
        applyRunListData(payload);
        state.setRunsError?.("");
      } catch (err) {
        state.setRunsError?.(err?.message || String(err || t("run.list_unavailable", "Run list unavailable")));
        const fallback = fallbackCurrentRun();
        state.setRunsData?.(fallback ? [fallback] : []);
      } finally {
        state.setRunsLoaded?.(true);
        deps.renderSessionMenu?.();
      }
    }

    function applyStatusData(options = {}) {
      deps.statusStore?.applyStatusData(options);
    }

    async function loadStatus(options = {}) {
      const runId = currentRunId();
      if (!runId) {
        state.setStatusData?.(null);
        deps.renderFirstRunState?.();
        return null;
      }
      if (!deps.initialSelectedTaskId) {
        const remoteSelectedTaskId = await deps.ensureRemoteSelectedTaskId?.();
        if (remoteSelectedTaskId) state.setSelectedTaskId?.(remoteSelectedTaskId);
      }
      const params = { lite: "1" };
      const requestedSelectedTaskId = selectedTaskId();
      if (requestedSelectedTaskId) params.selected_task_id = requestedSelectedTaskId;
      const payload = await deps.fetchJson?.(deps.apiUrl?.("/api/tasks", params), {}, "Failed to load tasks");
      state.setStatusData?.(payload);
      deps.statusStore?.applyCachedAgentsRuntime();
      applyStatusData(options);
      const needsAgentDetails = deps.selectedTaskNeedsAgentDetails?.();
      if (options.ensureSelectedAgents !== false && !requestedSelectedTaskId && needsAgentDetails) {
        loadStatus({ ...options, forceAgents: true, ensureSelectedAgents: false })
          .then(() => deps.renderPanelForRealtime?.())
          .catch(err => console.warn("Failed to load selected task agent details", err));
      } else if (options.refreshRuntime !== false) {
        await loadAgentsRuntime();
      }
      return state.statusData?.();
    }

    async function loadAgentsRuntime(options = {}) {
      const runId = currentRunId();
      const taskId = selectedTaskId();
      if (!runId || !taskId || deps.selectedTaskNeedsAgentDetails?.()) {
        return null;
      }
      const payload = await deps.fetchJson?.(
        deps.apiUrl?.("/api/agents/runtime", { task_id: taskId }),
        {},
        "Failed to load agents runtime"
      );
      deps.statusStore?.mergeAgentsRuntime(payload);
      deps.renderBackendStatus?.();
      if (options.renderAgents !== false) {
        if (!deps.isAgentsPanelEditing?.()) {
          deps.renderAgents?.();
        } else {
          deps.renderSelectedAgentInfo?.();
        }
      }
      return payload;
    }

    return Object.freeze({
      applyBootstrapPayload,
      applyRunListData,
      applyStatusData,
      applyWorkspaceData,
      currentAppVersion,
      currentRunSummary,
      fallbackCurrentRun,
      loadAgentsRuntime,
      loadBootstrap,
      loadRuns,
      loadStatus,
      resetRunScopedState,
      runScopedPayload,
      syncRunUrl
    });
  }

  window.AHAStatusController = Object.freeze({
    createStatusController
  });
})();
