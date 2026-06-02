(() => {
  const timeFormat = window.AHATimeFormat;
  const {
    parseTimestamp,
    formatDuration
  } = timeFormat;
  const agentMetadata = window.AHAAgentMetadata;
  const {
    agentLifecycleStatus,
    agentWaitingReason
  } = agentMetadata;
  const taskListHelpers = window.AHATaskList;
  const {
    taskCurrentStatus,
    taskActivityStatus,
    taskDisplayStatus
  } = taskListHelpers;
  const conversationMetadata = window.AHAConversationMetadata;
  const {
    eventData,
    eventTimestamp
  } = conversationMetadata;
  const terminalTaskStatuses = new Set(["completed", "failed", "blocked"]);
  const terminalAgentStatuses = new Set(["completed", "failed", "blocked", "interrupted"]);

  function contextTaskEvents(context, taskId) {
    if (typeof context.taskEvents === "function") return context.taskEvents(taskId) || [];
    return Array.isArray(context.taskEvents) ? context.taskEvents : [];
  }

  function contextConversationEvents(context, taskId) {
    if (typeof context.conversationSourceEvents === "function") return context.conversationSourceEvents(taskId) || [];
    return Array.isArray(context.conversationSourceEvents) ? context.conversationSourceEvents : [];
  }

  function contextTasks(context) {
    if (typeof context.tasks === "function") return context.tasks() || [];
    return Array.isArray(context.tasks) ? context.tasks : [];
  }

  function contextTask(context, taskId) {
    if (typeof context.task === "function") return context.task(taskId);
    if (context.task) return context.task;
    return contextTasks(context).find(item => item.id === taskId);
  }

  function contextTarget(context) {
    if (typeof context.backendTarget === "function") return context.backendTarget();
    return context.target || "main";
  }

  function contextNow(context) {
    if (typeof context.now === "function") {
      const millis = Number(context.now());
      if (Number.isFinite(millis)) return millis;
    }
    return Date.now();
  }

  function taskTiming(taskId, task, context = {}) {
    const events = contextTaskEvents(context, taskId);
    const firstMatchingTime = predicate => {
      for (const event of events) {
        if (predicate(event)) {
          const millis = eventTimestamp(event);
          if (millis) return millis;
        }
      }
      return null;
    };
    const lastMatchingTime = predicate => {
      for (let index = events.length - 1; index >= 0; index -= 1) {
        const event = events[index];
        if (predicate(event)) {
          const millis = eventTimestamp(event);
          if (millis) return millis;
        }
      }
      return null;
    };
    const startedAt = parseTimestamp(task?.started_at) || firstMatchingTime(event => {
      const data = eventData(event);
      const hasStartedStatus = task?.status && task.status !== "pending";
      return (
        event.type === "task_started" ||
        event.type === "agent_started" ||
        (event.type === "task_dispatched" && hasStartedStatus) ||
        (event.type === "task_status_changed" && data.status === "running")
      );
    });
    const terminalStatus = terminalTaskStatuses.has(taskDisplayStatus(task));
    const finishedAt = parseTimestamp(task?.finished_at) || lastMatchingTime(event => {
      const data = eventData(event);
      return (
        event.type === "task_finished" ||
        event.type === "agent_finished" ||
        (event.type === "task_status_changed" && terminalTaskStatuses.has(data.status || ""))
      );
    });
    if (!startedAt) return null;
    const running = taskCurrentStatus(task) === "running" && taskActivityStatus(task) !== "idle";
    const endAt = running ? contextNow(context) : finishedAt || (terminalStatus ? lastMatchingTime(() => true) : null);
    if (!endAt) return { startedAt, finishedAt: null, elapsedMs: contextNow(context) - startedAt, running };
    return { startedAt, finishedAt: running ? null : endAt, elapsedMs: endAt - startedAt, running };
  }

  function subAgents(task) {
    return (task?.agents || []).filter(agent => agent.role === "sub");
  }

  function pendingSubAgents(task) {
    return subAgents(task).filter(agent => !terminalAgentStatuses.has(agent.status || ""));
  }

  function waitingSubagentTiming(task, context = {}) {
    const agents = subAgents(task);
    if (!agents.length) return null;
    const coordination = task?.coordination || {};
    const startedAt = parseTimestamp(coordination.followup_started_at);
    if (!startedAt) return null;
    const pending = pendingSubAgents(task);
    const finalRequestedAt = parseTimestamp(coordination.final_summary_requested_at);
    const finalCompletedAt = parseTimestamp(coordination.final_summary_completed_at);
    const running = taskCurrentStatus(task) === "running" && pending.length > 0 && !finalRequestedAt;
    const endAt = running ? contextNow(context) : finalRequestedAt || finalCompletedAt || contextNow(context);
    return {
      startedAt,
      finishedAt: running ? null : endAt,
      elapsedMs: endAt - startedAt,
      running,
      pending,
      completed: agents.filter(agent => terminalAgentStatuses.has(agent.status || ""))
    };
  }

  function taskTimingLabel(taskId, task, context = {}) {
    const timing = taskTiming(taskId, task, context);
    if (!timing) return "";
    return `${timing.running ? "elapsed" : "duration"} ${formatDuration(timing.elapsedMs)}`;
  }

  function taskMetaTiming(taskId, task, context = {}) {
    const parts = [];
    const taskLabel = taskTimingLabel(taskId, task, context);
    if (taskLabel) parts.push(`task ${taskLabel}`);
    const waiting = waitingSubagentTiming(task, context);
    if (waiting) parts.push(`waiting subagents ${formatDuration(waiting.elapsedMs)} (${waiting.completed.length}/${subAgents(task).length})`);
    return parts.join(" | ");
  }

  function latestTurnTiming(taskId, context = {}) {
    const matcher = typeof context.eventMatchesSelectedAgent === "function"
      ? context.eventMatchesSelectedAgent
      : () => true;
    const events = contextConversationEvents(context, taskId).filter(matcher);
    let startIndex = -1;
    for (let index = events.length - 1; index >= 0; index -= 1) {
      if (events[index].type === "agent_started") {
        startIndex = index;
        break;
      }
    }
    if (startIndex < 0) return null;
    const startedEvent = events[startIndex];
    const startedAt = eventTimestamp(startedEvent);
    if (!startedAt) return null;
    const task = contextTask(context, taskId);
    const target = contextTarget(context);
    const agent = (task?.agents || []).find(item => item.id === target);
    const coordination = task?.coordination || {};
    const followupStartedAt = parseTimestamp(coordination.followup_started_at);
    const finalCompletedAt = parseTimestamp(coordination.final_summary_completed_at);
    const followupCoversTurn =
      target === "main" &&
      followupStartedAt &&
      ((!finalCompletedAt && followupStartedAt >= startedAt) || (finalCompletedAt && finalCompletedAt >= startedAt));
    if (followupCoversTurn) {
      const waiting = waitingSubagentTiming(task, context);
      const agentStatus = agentLifecycleStatus(agent);
      const followupStillActive =
        taskCurrentStatus(task) === "running" ||
        waiting?.running ||
        agentStatus === "waiting";
      if (!finalCompletedAt && !followupStillActive) {
        return null;
      }
      let logicalStartedEvent = startedEvent;
      for (let index = startIndex; index >= 0; index -= 1) {
        if (events[index].type !== "agent_started") continue;
        const candidateStartedAt = eventTimestamp(events[index]);
        if (candidateStartedAt && candidateStartedAt <= followupStartedAt) {
          logicalStartedEvent = events[index];
          break;
        }
      }
      const logicalStartedAt = eventTimestamp(logicalStartedEvent) || followupStartedAt;
      const status = finalCompletedAt
        ? "completed"
        : waiting?.running || agentStatus === "waiting"
          ? "waiting"
          : agentStatus || "running";
      const waitingReason = status === "waiting" ? agentWaitingReason(agent) || (waiting?.running ? "subagents" : "") : "";
      const endAt = finalCompletedAt || contextNow(context);
      return {
        startedAt: logicalStartedAt,
        finishedAt: finalCompletedAt || null,
        elapsedMs: endAt - logicalStartedAt,
        running: !finalCompletedAt,
        status,
        waitingReason,
        target,
        sender: eventData(logicalStartedEvent).sender || "-"
      };
    }
    let latestStatusEvent = null;
    let terminalStatusEvent = null;
    let agentFinishedEvent = null;
    for (let index = startIndex + 1; index < events.length; index += 1) {
      const data = eventData(events[index]);
      if (events[index].type === "agent_status_changed") {
        latestStatusEvent = events[index];
        if (terminalAgentStatuses.has(data.status || "")) terminalStatusEvent = events[index];
      } else if (events[index].type === "agent_finished") {
        agentFinishedEvent = events[index];
      }
    }
    const latestStatus = eventData(latestStatusEvent || {}).status || agentLifecycleStatus(agent);
    let finishedAt = terminalStatusEvent ? eventTimestamp(terminalStatusEvent) : null;
    if (!finishedAt && !["running", "waiting"].includes(latestStatus) && agentFinishedEvent) {
      finishedAt = eventTimestamp(agentFinishedEvent);
    }
    if (!finishedAt && terminalAgentStatuses.has(agent?.status || "")) {
      finishedAt = parseTimestamp(agent.finished_at) || parseTimestamp(agent.last_active_at) || parseTimestamp(task?.finished_at) || startedAt;
    }
    const running = !finishedAt || latestStatus === "waiting";
    const endAt = running ? contextNow(context) : finishedAt;
    const finishedData = eventData(terminalStatusEvent || agentFinishedEvent || {});
    const exitCode = finishedData.exit_code;
    const status = running ? latestStatus || "running" : finishedData.status || agent?.status || (exitCode === 0 ? "completed" : "failed");
    const waitingReason = status === "waiting" ? (eventData(latestStatusEvent || {}).waiting_reason || agentWaitingReason(agent) || "") : "";
    return {
      startedAt,
      finishedAt: running ? null : finishedAt,
      elapsedMs: endAt - startedAt,
      running,
      status,
      waitingReason,
      target: eventData(startedEvent).target || "main",
      sender: eventData(startedEvent).sender || "-"
    };
  }

  window.AHATaskTiming = Object.freeze({
    taskTiming,
    subAgents,
    pendingSubAgents,
    waitingSubagentTiming,
    taskTimingLabel,
    taskMetaTiming,
    latestTurnTiming
  });
})();
