(() => {
  function createAppSelectors(state = {}, deps = {}) {
    const selectedTaskFromStatus = deps.selectedTaskFromStatus || (() => null);
    const selectedAgentFromTask = deps.selectedAgentFromTask || (() => null);
    const selectedTaskRealtimeActiveFromState = deps.selectedTaskRealtimeActiveFromState || (() => false);
    const terminalAgentStatuses = deps.terminalAgentStatuses || new Set();

    function selectedTask() {
      return selectedTaskFromStatus(state.statusData?.(), state.selectedTaskId?.());
    }

    function selectedTaskNeedsAgentDetails(task = selectedTask()) {
      return Boolean(task && deps.taskAgentCount?.(task) > (task.agents || []).length);
    }

    function backendTarget() {
      return state.agentTargetValue?.() || "main";
    }

    function selectedAgent() {
      return selectedAgentFromTask(selectedTask(), backendTarget());
    }

    function isAhaCommand(message) {
      return /^\/aha(?:\s|$)/i.test(String(message || "").trim());
    }

    function isInterruptCommand(message) {
      return /^\/aha\s+interrupt(?:\s|$)/i.test(String(message || "").trim());
    }

    function selectedTaskRealtimeActive() {
      return selectedTaskRealtimeActiveFromState(
        selectedTask(),
        taskId => latestTurnTiming(taskId),
        deps.selectedAgentInputBlocked || (() => false),
        deps.taskActivityStatus || (() => "idle")
      );
    }

    function agentStatusTiming(agent) {
      const status = deps.agentLifecycleStatus?.(agent);
      const startedAt =
        deps.parseTimestamp?.(agent?.status_started_at) ||
        (status === "running" ? deps.parseTimestamp?.(agent?.started_at) : null) ||
        deps.parseTimestamp?.(agent?.last_active_at);
      if (!startedAt) return null;
      const terminal = terminalAgentStatuses.has(status);
      const finishedAt = terminal ? deps.parseTimestamp?.(agent?.finished_at) || deps.parseTimestamp?.(agent?.last_active_at) : null;
      const endAt = terminal ? (finishedAt || startedAt) : Date.now();
      return {
        status,
        waitingReason: deps.agentWaitingReason?.(agent),
        startedAt,
        finishedAt,
        elapsedMs: endAt - startedAt,
        running: !terminal
      };
    }

    function agentStatusTimingText(agent) {
      const timing = agentStatusTiming(agent);
      if (!timing) return "";
      const status = timing.waitingReason ? `${timing.status}:${timing.waitingReason}` : timing.status;
      return `${status} · ${deps.formatDuration?.(timing.elapsedMs)}`;
    }

    function taskTimingContext() {
      return {
        taskEvents: deps.taskEvents,
        conversationSourceEvents: deps.conversationSourceEvents,
        eventMatchesSelectedAgent: deps.eventMatchesSelectedAgent,
        tasks: () => state.statusData?.()?.tasks || [],
        backendTarget
      };
    }

    function taskTimingLabel(taskId, task) {
      return deps.taskTimingLabelForContext?.(taskId, task, taskTimingContext());
    }

    function taskMetaTiming(taskId, task) {
      return deps.taskMetaTimingForContext?.(taskId, task, taskTimingContext());
    }

    function latestTurnTiming(taskId) {
      return deps.latestTurnTimingForContext?.(taskId, taskTimingContext());
    }

    return Object.freeze({
      agentStatusTiming,
      agentStatusTimingText,
      backendTarget,
      isAhaCommand,
      isInterruptCommand,
      latestTurnTiming,
      selectedAgent,
      selectedTask,
      selectedTaskNeedsAgentDetails,
      selectedTaskRealtimeActive,
      taskMetaTiming,
      taskTimingContext,
      taskTimingLabel
    });
  }

  window.AHAAppSelectors = Object.freeze({ createAppSelectors });
})();
