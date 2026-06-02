(() => {
  function createStatusStore(state = {}, deps = {}) {
    const noop = () => {};
    const runIdOf = deps.runIdOf || (run => String(run?.id || "").trim());
    const getCurrentRunId = state.getCurrentRunId || (() => "");
    const setCurrentRunId = state.setCurrentRunId || noop;
    const getDefaultRunId = state.getDefaultRunId || (() => "");
    const setDefaultRunId = state.setDefaultRunId || noop;
    const getRunsData = state.getRunsData || (() => []);
    const setRunsData = state.setRunsData || noop;
    const getStatusData = state.getStatusData || (() => null);
    const getSelectedTaskId = state.getSelectedTaskId || (() => null);
    const setSelectedTaskId = state.setSelectedTaskId || noop;
    const setBackendStatusData = state.setBackendStatusData || noop;
    const setConversationAutoFollow = state.setConversationAutoFollow || noop;
    const agentsRuntimeCache = state.agentsRuntimeCache || new Map();

    function applyRunListData(payload = {}) {
      const nextDefaultRunId = String(payload.default_run_id || getDefaultRunId() || "").trim();
      setDefaultRunId(nextDefaultRunId);
      const nextRunsData = Array.isArray(payload.runs) ? payload.runs : [];
      setRunsData(nextRunsData);
      const knownRunIds = new Set(nextRunsData.map(runIdOf).filter(Boolean));
      let nextCurrentRunId = getCurrentRunId();
      if (nextCurrentRunId && !knownRunIds.has(nextCurrentRunId)) {
        nextCurrentRunId = "";
        setCurrentRunId(nextCurrentRunId);
        deps.syncRunUrl?.();
      }
      const statusData = getStatusData();
      const preferred = nextCurrentRunId || nextDefaultRunId || runIdOf(nextRunsData[0]) || String(statusData?.run_id || "").trim();
      if (preferred && preferred !== nextCurrentRunId) {
        nextCurrentRunId = preferred;
        setCurrentRunId(nextCurrentRunId);
        deps.syncRunUrl?.();
      }
      if (!preferred && nextCurrentRunId) {
        nextCurrentRunId = "";
        setCurrentRunId(nextCurrentRunId);
        deps.syncRunUrl?.();
      }
      if (nextCurrentRunId) {
        if (!deps.initialSelectedTaskId) {
          setSelectedTaskId(deps.readStoredSelectedTaskId?.() || getSelectedTaskId());
        }
        deps.restoreEventCursorFromStorage?.();
      }
    }

    function resetRunScopedState() {
      deps.resetEventWebSocketReconnectState?.("run_scope_reset");
      setSelectedTaskId(deps.readStoredSelectedTaskId?.() || null);
      setBackendStatusData(null);
      deps.restoreEventCursorFromStorage?.();
      setConversationAutoFollow(true);
      state.allEvents?.splice(0);
      state.seenRealtimeEvents?.clear();
      state.conversationStates?.clear();
      state.expandedMessageKeys?.clear();
      state.finalDetails?.clear();
      state.contextDetails?.clear();
      state.logStates?.clear();
      deps.clearPendingState?.();
      deps.resetRunMaintenanceState?.();
    }

    function applyStatusData(options = {}) {
      deps.documentBody?.classList.remove("empty-run");
      const statusData = getStatusData();
      const statusRunId = String(statusData?.run_id || "").trim();
      if (statusRunId && statusRunId !== getCurrentRunId()) {
        deps.closeEventWebSocket?.();
        setCurrentRunId(statusRunId);
        deps.syncRunUrl?.();
        deps.restoreEventCursorFromStorage?.();
      }
      deps.renderAll?.(options);
    }

    function agentRuntimeCacheKey(taskId, agentId, runId = getCurrentRunId()) {
      return `${runId || ""}:${taskId || ""}:${agentId || "main"}`;
    }

    function normalizeBackendRuntimeState(runtimeState) {
      if (!runtimeState) return null;
      const taskId = String(runtimeState.task_id || getSelectedTaskId() || "");
      const target = String(runtimeState.target || runtimeState.id || deps.backendTarget?.() || "main");
      return { ...runtimeState, task_id: taskId, target, id: String(runtimeState.id || target) };
    }

    function refreshTaskActivityFromAgents(task) {
      const agents = task?.agents || [];
      if (agents.some(agent => deps.agentBackendProcessStatus?.(agent) === "busy")) {
        task.activity_status = "busy";
      } else if (deps.taskCurrentStatus?.(task) === "running" || agents.some(agent => deps.agentLifecycleStatus?.(agent) === "running")) {
        task.activity_status = "running";
      } else {
        task.activity_status = "idle";
      }
    }

    function applyBackendRuntimeStateToAgent(agent, runtimeState) {
      agent.backend_process_status = runtimeState.status || "stopped";
      agent.backend_process_pid = runtimeState.pid ?? null;
      agent.backend_process_last_reply_at = runtimeState.last_reply_at || "";
      agent.backend_resolved_model = runtimeState.resolved_model || agent.model || "";
      agent.backend_runtime_context_window = runtimeState.runtime_context_window ?? null;
      agent.backend_runtime_context_usage = runtimeState.runtime_context_usage || {};
      agent.backend_context_pressure = runtimeState.context_pressure || {};
      agent.backend_latest_usage = runtimeState.latest_usage || {};
      agent.backend_latest_prompt_metrics = runtimeState.latest_prompt_metrics || {};
    }

    function mergeBackendStatusIntoAgent(runtimeState) {
      const normalized = normalizeBackendRuntimeState(runtimeState);
      const statusData = getStatusData();
      if (!normalized || !statusData) return;
      const taskId = normalized.task_id;
      const target = normalized.target;
      agentsRuntimeCache.set(agentRuntimeCacheKey(taskId, target), normalized);
      const task = (statusData.tasks || []).find(item => String(item.id || "") === taskId);
      if (!task) return;
      const agent = (task.agents || []).find(item => String(item.id || "") === target);
      if (!agent) return;
      applyBackendRuntimeStateToAgent(agent, normalized);
      refreshTaskActivityFromAgents(task);
      if (taskId === getSelectedTaskId() && target === deps.backendTarget?.()) setBackendStatusData(normalized);
    }

    function applyCachedAgentsRuntime() {
      const statusData = getStatusData();
      if (!statusData) return;
      for (const task of statusData.tasks || []) {
        for (const agent of task.agents || []) {
          const cached = agentsRuntimeCache.get(agentRuntimeCacheKey(task.id, agent.id, statusData.run_id || getCurrentRunId()));
          if (cached) applyBackendRuntimeStateToAgent(agent, cached);
        }
        refreshTaskActivityFromAgents(task);
      }
    }

    function mergeAgentsRuntime(payload) {
      for (const runtimeState of payload?.agents || []) {
        mergeBackendStatusIntoAgent(runtimeState);
      }
      const selectedRuntime = (payload?.agents || []).find(runtimeState => String(runtimeState.target || runtimeState.id || "") === deps.backendTarget?.());
      if (selectedRuntime) setBackendStatusData(normalizeBackendRuntimeState(selectedRuntime));
    }

    return Object.freeze({
      agentRuntimeCacheKey,
      applyCachedAgentsRuntime,
      applyRunListData,
      applyStatusData,
      mergeAgentsRuntime,
      normalizeBackendRuntimeState,
      resetRunScopedState
    });
  }

  window.AHAStatusStore = Object.freeze({ createStatusStore });
})();
