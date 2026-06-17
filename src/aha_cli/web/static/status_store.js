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
      const lastSelectedRunId = String(payload.ui_state?.last_selected_run_id || "").trim();
      const preferredLastRunId = !deps.initialRunId && lastSelectedRunId && knownRunIds.has(lastSelectedRunId)
        ? lastSelectedRunId
        : "";
      const preferred = nextCurrentRunId || preferredLastRunId || nextDefaultRunId || runIdOf(nextRunsData[0]) || String(statusData?.run_id || "").trim();
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
        deps.writeSelectedRunId?.(nextCurrentRunId);
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

    function clearRuntimeCacheForAgent(taskId, agentId = "main") {
      const normalizedTaskId = String(taskId || "").trim();
      if (!normalizedTaskId) return false;
      const normalizedAgentId = String(agentId || "main").trim() || "main";
      const deleted = agentsRuntimeCache.delete(agentRuntimeCacheKey(normalizedTaskId, normalizedAgentId));
      if (deleted && normalizedTaskId === getSelectedTaskId() && normalizedAgentId === deps.backendTarget?.()) {
        setBackendStatusData(null);
      }
      return deleted;
    }

    function clearRuntimeCacheForTask(taskId) {
      const normalizedTaskId = String(taskId || "").trim();
      if (!normalizedTaskId) return false;
      const prefix = `${getCurrentRunId() || ""}:${normalizedTaskId}:`;
      let deleted = false;
      for (const key of Array.from(agentsRuntimeCache.keys())) {
        if (!String(key).startsWith(prefix)) continue;
        agentsRuntimeCache.delete(key);
        deleted = true;
      }
      if (deleted && normalizedTaskId === getSelectedTaskId()) setBackendStatusData(null);
      return deleted;
    }

    function clearRuntimeCacheForEvents(events = []) {
      let changed = false;
      for (const event of events || []) {
        const type = String(event?.type || "");
        const data = event?.data || {};
        const taskId = String(data.task_id || "").trim();
        if (!taskId) continue;
        if (type === "task_status_changed") {
          const status = String(data.status || "").toLowerCase();
          if (status && status !== "running") changed = clearRuntimeCacheForTask(taskId) || changed;
        } else if (type === "backend_stopped") {
          changed = clearRuntimeCacheForAgent(taskId, data.target || data.agent_id || "main") || changed;
        } else if (["agent_finished", "agent_error", "agent_interrupted"].includes(type)) {
          changed = clearRuntimeCacheForAgent(taskId, data.target || data.agent_id || "main") || changed;
        } else if (type === "agent_status_changed") {
          const status = String(data.status || "").toLowerCase();
          if (status && status !== "running") changed = clearRuntimeCacheForAgent(taskId, data.agent_id || data.target || "main") || changed;
        }
      }
      return changed;
    }

    function normalizeBackendRuntimeState(runtimeState) {
      if (!runtimeState) return null;
      const taskId = String(runtimeState.task_id || getSelectedTaskId() || "");
      const target = String(runtimeState.target || runtimeState.id || deps.backendTarget?.() || "main");
      return { ...runtimeState, task_id: taskId, target, id: String(runtimeState.id || target) };
    }

    function refreshTaskActivityFromAgents(task) {
      const agents = task?.agents || [];
      const currentStatus = deps.taskCurrentStatus?.(task);
      if (currentStatus !== "running") {
        task.activity_status = "idle";
        return;
      }
      if (agents.some(agent => deps.agentBackendProcessStatus?.(agent) === "busy")) {
        task.activity_status = "busy";
      } else if (agents.some(agent => deps.agentLifecycleStatus?.(agent) === "running") || currentStatus === "running") {
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
      clearRuntimeCacheForEvents,
      mergeAgentsRuntime,
      normalizeBackendRuntimeState,
      resetRunScopedState
    });
  }

  window.AHAStatusStore = Object.freeze({ createStatusStore });
})();
