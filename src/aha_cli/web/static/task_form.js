(function () {
  function createTaskPayload(input = {}) {
    const proxyEnabled = Boolean(input.proxyEnabled);
    const backend = input.backend || "";
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
