(function () {
  const defaultTaskContextThresholdPercent = 75;

  function normalizeTaskContextThreshold(value) {
    const rawThreshold = Number(value ?? defaultTaskContextThresholdPercent);
    return Number.isFinite(rawThreshold)
      ? Math.max(1, Math.min(99, Math.round(rawThreshold)))
      : defaultTaskContextThresholdPercent;
  }

  function taskContextConfirmLabel(payload) {
    const policy = payload.context_management && typeof payload.context_management === "object"
      ? payload.context_management
      : {};
    const enabled = policy.auto_compact_enabled === true;
    const threshold = normalizeTaskContextThreshold(policy.auto_compact_threshold_percent);
    return enabled ? `auto at ${threshold}%` : "auto off";
  }

  function hardwareDebugConfirmLabel(payload) {
    const policy = payload.hardware_debug && typeof payload.hardware_debug === "object"
      ? payload.hardware_debug
      : {};
    const channels = Array.isArray(policy.channels) ? policy.channels : [];
    if (!channels.length) return "off";
    const types = channels.map(channel => String(channel?.type || "").toUpperCase()).filter(Boolean).join(", ");
    return `${channels.length} channel${channels.length === 1 ? "" : "s"}${types ? ` (${types})` : ""}`;
  }

  function taskSkillsConfirmLabel(payload) {
    const policy = payload.task_skills && typeof payload.task_skills === "object"
      ? payload.task_skills
      : {};
    const skills = Array.isArray(policy.enabled_paths) ? policy.enabled_paths.length : 0;
    return skills ? `${skills} skill${skills === 1 ? "" : "s"}` : "off";
  }

  function createTaskPayload(input = {}) {
    const proxyEnabled = Boolean(input.proxyEnabled);
    const backend = input.backend || "";
    const contextAutoCompactEnabled = Boolean(input.contextAutoCompactEnabled);
    const contextThreshold = normalizeTaskContextThreshold(input.contextThreshold);
    return {
      title: String(input.title || "").trim(),
      description: String(input.description || "").trim(),
      backend,
      sandbox: input.sandbox || "",
      approval: input.approval || "",
      proxy_enabled: proxyEnabled,
      workspace_id: input.workspaceId || "",
      workspace_path: input.workspacePath || "",
      collaboration_mode: input.collaborationMode || "auto",
      workflow_template: input.workflowTemplate || "auto",
      delegation_policy: input.delegationPolicy || "auto",
      max_sub_agents: Number(input.maxSubAgents ?? 3),
      preferred_sub_backend: input.preferredSubBackend || backend,
      supervision: input.supervision || {},
      context_management: {
        auto_compact_enabled: contextAutoCompactEnabled,
        auto_compact_threshold_percent: contextThreshold
      },
      task_skills: input.taskSkills || {},
      hardware_debug: input.hardwareDebug || {},
      dispatch: input.dispatch !== false,
      model: input.model || null
    };
  }

  function taskProxyConfirmLabel(payload) {
    return payload.proxy_enabled ? "on" : "off";
  }

  function createTaskConfirmRows(payload, context = {}) {
    const supervision = payload.supervision || {};
    const hostModel = context.hostModelLabel || supervision.host_model || "default";
    const hostProxy = supervision.host_proxy_enabled ? "on" : "off";
    return [
      ["Run", context.runId || "-"],
      ["Title", payload.title],
      ["Description", payload.description || "-"],
      ["Workspace", context.workspaceLabel || payload.workspace_path || payload.workspace_id || "-"],
      ["Backend", context.backendLabel || payload.backend || "default"],
      ["Sandbox", payload.sandbox || "-"],
      ["Approval", payload.approval || "-"],
      ["Execution", `${payload.collaboration_mode || "auto"} (${payload.max_sub_agents || 0})`],
      ["Workflow", payload.workflow_template || "auto"],
      ["Supervision", context.supervisionSummary || "manual"],
      ["Host model", supervision.real_agent_enabled ? `${supervision.host_backend || "stub"} / ${hostModel}` : "-"],
      ["Host proxy", supervision.real_agent_enabled ? hostProxy : "-"],
      ["Context", taskContextConfirmLabel(payload)],
      ["Skills", taskSkillsConfirmLabel(payload)],
      ["Hardware", hardwareDebugConfirmLabel(payload)],
      ["Proxy", taskProxyConfirmLabel(payload)]
    ];
  }

  function createTaskFallbackConfirmText(payload, context = {}) {
    return [
      `Create task "${payload.title}"?`,
      payload.description ? `Description: ${payload.description}` : "",
      `Run: ${context.runId || "-"}`,
      `Workspace: ${context.workspaceLabel || payload.workspace_path || payload.workspace_id || "-"}`
    ].filter(Boolean).join("\n");
  }

  window.AHATaskForm = Object.freeze({
    createTaskPayload,
    createTaskConfirmRows,
    createTaskFallbackConfirmText
  });
}());
