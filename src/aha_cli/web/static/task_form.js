(function () {
  const defaultTaskContextThresholdPercent = 75;

  function normalizeTaskContextThreshold(value) {
    const rawThreshold = Number(value ?? defaultTaskContextThresholdPercent);
    return Number.isFinite(rawThreshold)
      ? Math.max(1, Math.min(99, Math.round(rawThreshold)))
      : defaultTaskContextThresholdPercent;
  }

  function taskTokenSavingConfirmLabel(payload) {
    const policy = payload.token_saving && typeof payload.token_saving === "object"
      ? payload.token_saving
      : {};
    const provider = String(policy.provider || "map");
    return policy.enabled === true ? `${provider} on` : "off";
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

  function observeProxyConfirmLabel(payload) {
    const policy = payload.observe_proxy && typeof payload.observe_proxy === "object"
      ? payload.observe_proxy
      : {};
    return policy.enabled === true ? "on" : "off";
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
    const tokenSavingEnabled = Boolean(input.tokenSavingEnabled);
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
      token_saving: {
        enabled: tokenSavingEnabled,
        provider: "map"
      },
      observe_proxy: {
        enabled: Boolean(input.observeProxyEnabled)
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
      ["Token saving", taskTokenSavingConfirmLabel(payload)],
      ["Observe proxy", observeProxyConfirmLabel(payload)],
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
