(() => {
  function agentBackendProcessStatus(agent) {
    const raw = String(agent?.backend_process_status || "stopped").toLowerCase();
    if (raw === "running" || raw === "busy") return raw;
    return "stopped";
  }

  function agentBackendProcessLabel(agent) {
    return agentBackendProcessStatus(agent).toUpperCase();
  }

  function agentLifecycleStatus(agent) {
    return String(agent?.status || "pending").toLowerCase();
  }

  function agentWaitingReason(agent) {
    return agentLifecycleStatus(agent) === "waiting" ? String(agent?.waiting_reason || "").toLowerCase() : "";
  }

  function agentLifecycleDisplay(agent) {
    const status = agentLifecycleStatus(agent);
    const reason = agentWaitingReason(agent);
    return reason ? `${status}:${reason}` : status;
  }

  function agentLifecycleLabel(agent) {
    return agentLifecycleDisplay(agent).toUpperCase();
  }

  function isSupervisionAgent(agent) {
    const role = agent?.role || "";
    return role === "host" || role === "supervision-host";
  }

  function agentRoleLabel(agent) {
    return isSupervisionAgent(agent) ? "host / user proxy" : String(agent?.role || "");
  }

  function agentOptionLabel(agent) {
    return isSupervisionAgent(agent)
      ? `${agent?.id || ""} (host/${agent?.backend || "-"})`
      : `${agent?.id || ""} (${agent?.backend || "-"})`;
  }

  function agentOptionGroups(agents = []) {
    const items = Array.isArray(agents) ? agents : [];
    return [
      {
        label: "Main & sub agents",
        className: "execution-agents",
        agents: items.filter(agent => !isSupervisionAgent(agent))
      },
      {
        label: "Supervision",
        className: "supervision-agents",
        agents: items.filter(agent => isSupervisionAgent(agent))
      }
    ].filter(group => group.agents.length);
  }

  function agentRuntimeDefaults(agent, task = {}) {
    return {
      sandbox: agent?.sandbox || task?.preferred_sandbox || "workspace-write",
      approval: agent?.approval || task?.preferred_approval || "never",
      reasoningEffort: agent?.reasoning_effort || task?.preferred_reasoning_effort || "",
      proxyEnabled: Boolean(agent?.proxy_enabled)
    };
  }

  function agentSessionLabel(agent) {
    return agent?.backend_session_id || "-";
  }

  function agentWorkspaceLabel(agent, task = {}) {
    return agent?.workspace_path || task?.workspace_path || "-";
  }

  function agentProcessDetail(agent, details = {}) {
    return [
      `process=${details.rawProcessStatus || agentBackendProcessStatus(agent)}`,
      agent?.backend_process_pid ? `pid=${agent.backend_process_pid}` : "pid=-",
      agent?.backend_process_last_reply_at && details.lastReply ? `last_reply=${details.lastReply}` : "",
      details.contextPressure || ""
    ].filter(Boolean).join(" | ");
  }

  function agentDisplayModel(agent, task = {}, details = {}) {
    const runtime = agentRuntimeDefaults(agent, task);
    const roleLabel = agentRoleLabel(agent);
    const processStatus = details.processStatus || agentBackendProcessStatus(agent);
    const rawProcessStatus = details.rawProcessStatus || agent?.backend_process_status || processStatus;
    const lifecycleDisplay = details.lifecycleDisplay || agentLifecycleDisplay(agent);
    const statusText = details.statusText || lifecycleDisplay;
    const resolvedModel = details.resolvedModel || agent?.backend_resolved_model || agent?.model || "-";
    const taskProxySummary = details.taskProxySummary || "not configured";
    const contextPressure = details.contextPressure || "";
    const processDetail = agentProcessDetail(agent, {
      rawProcessStatus,
      lastReply: details.lastReply,
      contextPressure
    });
    const sessionLabel = agentSessionLabel(agent);
    const workspaceLabel = agentWorkspaceLabel(agent, task);
    return {
      isHostAgent: isSupervisionAgent(agent),
      roleLabel,
      sandbox: runtime.sandbox,
      approval: runtime.approval,
      reasoningEffort: runtime.reasoningEffort,
      proxyEnabled: runtime.proxyEnabled,
      processStatus,
      rawProcessStatus,
      lifecycleDisplay,
      statusText,
      resolvedModel,
      contextPressure,
      processDetail,
      sessionLabel,
      workspaceLabel,
      title: [
        `${agent?.id || ""} ${roleLabel}`,
        `backend=${agent?.backend || "-"}`,
        `model=${resolvedModel}`,
        `effort=${runtime.reasoningEffort || "default"}`,
        `sandbox=${runtime.sandbox}`,
        `approval=${runtime.approval}`,
        `proxy=${runtime.proxyEnabled ? "on" : "off"} (${taskProxySummary})`,
        `status=${statusText}`,
        details.statusStarted ? `status_started=${details.statusStarted}` : "",
        details.statusFinished ? `status_finished=${details.statusFinished}` : "",
        processDetail,
        `session=${sessionLabel}`,
        `workspace=${workspaceLabel}`
      ].filter(Boolean).join("\n"),
      metaLines: [
        `status=${statusText} | ${roleLabel} | ${agent?.backend || "-"} | ${resolvedModel}`,
        `sandbox=${runtime.sandbox} | approval=${runtime.approval} | effort=${runtime.reasoningEffort || "default"}`,
        `proxy=${runtime.proxyEnabled ? "on" : "off"} | task proxy=${taskProxySummary}`,
        `process=${rawProcessStatus} | ${contextPressure} | session=${sessionLabel}`
      ]
    };
  }

  window.AHAAgentMetadata = Object.freeze({
    agentBackendProcessStatus,
    agentBackendProcessLabel,
    agentLifecycleStatus,
    agentWaitingReason,
    agentLifecycleDisplay,
    agentLifecycleLabel,
    isSupervisionAgent,
    agentRoleLabel,
    agentOptionLabel,
    agentOptionGroups,
    agentRuntimeDefaults,
    agentSessionLabel,
    agentWorkspaceLabel,
    agentProcessDetail,
    agentDisplayModel
  });
})();
