(() => {
  function createOptimisticEvents(state = {}, deps = {}) {
    function optimisticEventKey(event) {
      const data = deps.eventData?.(event) || {};
      const taskId = deps.eventTaskId?.(event) || "";
      if (event.type === "message") {
        const sender = deps.messageDisplaySender?.(data) || data.sender || "";
        const target = deps.messageDisplayTarget?.(data) || data.target || "";
        return `message:${taskId}:${sender}:${target}:${String(data.message || "").trim()}`;
      }
    if (event.type === "backend_start_queued") return `backend_start_queued:${taskId}:${data.target || "main"}`;
    if (event.type === "agent_started") return `agent_started:${taskId}:${data.target || "main"}`;
      if (event.type === "agent_status_changed") return `agent_status_changed:${taskId}:${data.agent_id || data.target || "main"}:${data.status || ""}`;
      if (event.type === "task_status_changed") return `task_status_changed:${taskId}:${data.status || ""}`;
      return "";
    }

    function optimisticEventContextKey(event) {
      const data = deps.eventData?.(event) || {};
      const taskId = deps.eventTaskId?.(event) || "";
      const target = data.target || data.agent_id || data.to_agent || deps.messageDisplayTarget?.(data) || "main";
      return `${taskId}::${target}`;
    }

    function clearOptimisticEventsForContext(taskId, target) {
      const contextKey = `${taskId}::${target || "main"}`;
      const matchesContext = event => event?._optimistic && optimisticEventContextKey(event) === contextKey;
      let removed = false;
      for (let index = state.allEvents.length - 1; index >= 0; index -= 1) {
        if (matchesContext(state.allEvents[index])) {
          state.allEvents.splice(index, 1);
          removed = true;
        }
      }
      for (const conversationState of state.conversationStates.values()) {
        const next = conversationState.events.filter(event => !matchesContext(event));
        if (next.length !== conversationState.events.length) {
          conversationState.events = next;
          removed = true;
        }
      }
      return removed;
    }

    function removeOptimisticEventsMatchedBy(events) {
      const keys = new Set(events.map(optimisticEventKey).filter(Boolean));
      const contextClearTypes = new Set(["agent_message", "agent_finished", "agent_error", "agent_status_changed"]);
      const contextKeys = new Set(events
        .filter(event => contextClearTypes.has(event.type))
        .map(optimisticEventContextKey)
        .filter(Boolean));
      if (!keys.size && !contextKeys.size) return false;
      let removed = false;
      const isMatched = event => event?._optimistic && (keys.has(optimisticEventKey(event)) || contextKeys.has(optimisticEventContextKey(event)));
      for (let index = state.allEvents.length - 1; index >= 0; index -= 1) {
        if (isMatched(state.allEvents[index])) {
          state.allEvents.splice(index, 1);
          removed = true;
        }
      }
      for (const conversationState of state.conversationStates.values()) {
        const next = conversationState.events.filter(event => !isMatched(event));
        if (next.length !== conversationState.events.length) {
          conversationState.events = next;
          removed = true;
        }
      }
      return removed;
    }

    function selectedBackendProcessActive(target) {
      const agent = deps.selectedAgent?.();
      const status = String(agent?.backend_process_status || deps.backendStatusData?.()?.status || "stopped").toLowerCase();
      return status === "running" || status === "busy";
    }

    function updateOptimisticAgentState(task, target, timestamp, waitingForBackendStart = false) {
      if (!task) return;
      task.current_status = "running";
      task.activity_status = "busy";
      const agent = (task.agents || []).find(item => item.id === target);
      if (agent) {
        agent.status = waitingForBackendStart ? "waiting" : "running";
        agent.waiting_reason = waitingForBackendStart ? "agent_start" : "";
        agent.status_started_at = timestamp;
        agent.backend_process_status = "busy";
        agent.backend_process_last_reply_at = "";
      }
      if (deps.selectedTaskId?.() === task.id && target === deps.backendTarget?.()) {
        deps.setBackendStatusData?.({
          ...(deps.backendStatusData?.() || {}),
          id: target,
          target,
          task_id: task.id,
          status: "busy"
        });
      }
    }

    function addOptimisticSendFeedback(task, target, message) {
      if (!task || !target || !message || deps.isAhaCommand?.(message)) return false;
      clearOptimisticEventsForContext(task.id, target);
      const ts = new Date().toISOString();
      const role = target === "main" ? "main" : "sub";
      const waitingForBackendStart = !selectedBackendProcessActive(target);
      const eventBase = () => {
        const seq = deps.nextOptimisticEventSeq?.() || 0;
        return {
          ts,
          event_id: `optimistic-${seq}`,
          _cursor: `optimistic-${seq}`,
          _optimistic: true,
          _uiKey: `optimistic-${seq}`
        };
      };
      const events = [
        {
          ...eventBase(),
          type: "message",
          data: { task_id: task.id, target, role, sender: "browser", from_agent: "browser", to_agent: target, message }
        },
        {
          ...eventBase(),
          type: "task_status_changed",
          data: { task_id: task.id, target, status: "running", exit_code: null }
        },
        waitingForBackendStart ? {
          ...eventBase(),
          type: "backend_start_queued",
          data: {
            task_id: task.id,
            target,
            backend: deps.selectedAgent?.()?.backend || task.preferred_backend || "-",
            model: deps.selectedAgent?.()?.model || task.preferred_model || "",
            queued: true
          }
        } : {
          ...eventBase(),
          type: "agent_started",
          data: {
            task_id: task.id,
            target,
            sender: "browser",
            sandbox: deps.selectedAgent?.()?.sandbox || task.preferred_sandbox || "-",
            approval: deps.selectedAgent?.()?.approval || task.preferred_approval || "-",
            proxy_enabled: Boolean(deps.selectedAgent?.()?.proxy_enabled)
          }
        },
        {
          ...eventBase(),
          type: "agent_status_changed",
          data: {
            task_id: task.id,
            agent_id: target,
            status: waitingForBackendStart ? "waiting" : "running",
            waiting_reason: waitingForBackendStart ? "agent_start" : "",
            exit_code: null
          }
        }
      ];
      state.allEvents.push(...events);
      deps.appendRealtimeConversationEvents?.(events);
      updateOptimisticAgentState(task, target, ts, waitingForBackendStart);
      deps.setConversationAutoFollow?.(true);
      deps.renderTaskList?.();
      deps.renderSelectedHeader?.();
      deps.renderSelectedAgentInfo?.();
      deps.renderPendingMessages?.();
      deps.renderPanelForRealtime?.();
      return true;
    }

    return Object.freeze({
      addOptimisticSendFeedback,
      clearOptimisticEventsForContext,
      optimisticEventContextKey,
      optimisticEventKey,
      removeOptimisticEventsMatchedBy
    });
  }

  window.AHAOptimisticEvents = Object.freeze({ createOptimisticEvents });
})();
