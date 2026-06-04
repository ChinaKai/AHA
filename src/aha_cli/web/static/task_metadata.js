(function () {
  const collaborationModeOptions = Object.freeze(["auto", "solo", "pair", "team"]);
  const collaborationModeDescriptionKeys = Object.freeze({
    auto: "task.collaboration_auto_desc",
    solo: "task.collaboration_solo_desc",
    pair: "task.collaboration_pair_desc",
    team: "task.collaboration_team_desc"
  });
  const collaborationModeDescriptions = Object.freeze({
    auto: "AHA automatically chooses the fastest execution path and uses sub-agents only when it saves time.",
    solo: "Solo mode: main completes all work, best for small tasks and quick edits.",
    pair: "Pair mode: up to 1 sub-agent handles implementation, research, or review in parallel.",
    team: "Team mode: up to 2 sub-agents handle separable scopes while main coordinates and integrates."
  });
  const workflowTemplateOptions = Object.freeze(["auto", "bugfix", "feature", "review", "embedded-driver", "fault-debug", "hil-regression", "release"]);
  const workflowTemplateDescriptionKeys = Object.freeze({
    auto: "task.workflow_auto_desc",
    bugfix: "task.workflow_bugfix_desc",
    feature: "task.workflow_feature_desc",
    review: "task.workflow_review_desc",
    "embedded-driver": "task.workflow_embedded_driver_desc",
    "fault-debug": "task.workflow_fault_debug_desc",
    "hil-regression": "task.workflow_hil_regression_desc",
    release: "task.workflow_release_desc"
  });
  const workflowTemplateDescriptions = Object.freeze({
    auto: "Automatically detect task type and the most efficient execution strategy.",
    bugfix: "Efficiency strategy for investigation, fixes, and regression checks.",
    feature: "Efficiency strategy for design, implementation, tests, and documentation.",
    review: "For independent risk review, test review, and code review.",
    "embedded-driver": "For datasheet/register analysis, driver implementation, and boundary testing.",
    "fault-debug": "For crash/log analysis, recent-change review, and reproduction validation.",
    "hil-regression": "For HIL test matrices, automated logs, and regression risk.",
    release: "For changelogs/docs, build/package work, and final risk review."
  });
  const defaultTaskSupervisionMaxRounds = 99;
  const defaultTaskContextThresholdPercent = 75;
  const supervisionAskUserGateDefs = Object.freeze([
    Object.freeze(["real_ui_validation", "Real UI/device"]),
    Object.freeze(["scope_change", "Scope change"]),
    Object.freeze(["commit_merge_delete", "Commit/merge/delete"]),
    Object.freeze(["destructive_or_high_risk", "High risk"]),
    Object.freeze(["permissions_or_external", "Permissions/external"]),
    Object.freeze(["product_preference", "Product preference"])
  ]);

  function t(key, fallback = "") {
    return window.AHAI18n?.t?.(key, fallback) || fallback;
  }

  function collaborationModeDescription(mode) {
    const value = collaborationModeOptions.includes(mode) ? mode : "auto";
    return t(collaborationModeDescriptionKeys[value], collaborationModeDescriptions[value]);
  }

  function workflowTemplateDescription(template) {
    const value = workflowTemplateOptions.includes(template) ? template : "auto";
    return t(workflowTemplateDescriptionKeys[value], workflowTemplateDescriptions[value]);
  }

  function collaborationModeMaxSubAgents(mode, fallback = 3) {
    if (mode === "solo") return 0;
    if (mode === "pair") return 1;
    if (mode === "team") return 2;
    return Number(fallback || "3");
  }

  function collaborationModeDelegationPolicy(mode) {
    return mode === "solo" ? "disabled" : "auto";
  }

  function inferredTaskCollaborationMode(task) {
    const mode = String(task?.collaboration_mode || "").toLowerCase();
    if (collaborationModeOptions.includes(mode)) return mode;
    if (task?.delegation_policy === "disabled") return "solo";
    const rawLimit = task?.max_sub_agents;
    const limit = rawLimit === undefined || rawLimit === null ? 3 : Number(rawLimit || 0);
    if (limit === 0) return "solo";
    if (limit === 1) return "pair";
    if (limit === 2) return "team";
    return "auto";
  }

  function taskCollaborationSummary(task) {
    const mode = inferredTaskCollaborationMode(task);
    const rawLimit = task?.max_sub_agents;
    const limit = rawLimit === undefined || rawLimit === null ? 3 : Number(rawLimit || 0);
    if (mode === "auto") return `auto (${limit})`;
    if (mode === "solo") return "solo";
    if (mode === "pair") return "pair (1)";
    if (mode === "team") return "team (2)";
    return mode;
  }

  function taskWorkflowSummary(task) {
    const template = String(task?.workflow_template || "auto").toLowerCase();
    return workflowTemplateOptions.includes(template) ? template : "auto";
  }

  function defaultAskUserGates() {
    return Object.fromEntries(supervisionAskUserGateDefs.map(([key]) => [key, false]));
  }

  function normalizeAskUserGates(value) {
    const gates = defaultAskUserGates();
    if (value && typeof value === "object") {
      supervisionAskUserGateDefs.forEach(([key]) => {
        if (Object.prototype.hasOwnProperty.call(value, key)) gates[key] = Boolean(value[key]);
      });
    }
    return gates;
  }

  function taskSupervisionPolicy(task) {
    const policy = task?.supervision && typeof task.supervision === "object" ? task.supervision : {};
    return {
      mode: policy.mode === "assisted" ? "assisted" : "manual",
      host_backend: policy.host_backend || "stub",
      host_model: policy.host_model || "",
      host_proxy_enabled: Boolean(policy.host_proxy_enabled),
      real_agent_enabled: Boolean(policy.real_agent_enabled),
      max_rounds: Number(policy.max_rounds || defaultTaskSupervisionMaxRounds),
      ask_user_gates: normalizeAskUserGates(policy.ask_user_gates)
    };
  }

  function taskSupervisionModeValue(policy) {
    if (policy.mode !== "assisted") return "manual";
    if (policy.host_backend === "codex" && policy.real_agent_enabled) return "assisted_codex";
    if (policy.host_backend === "claude" && policy.real_agent_enabled) return "assisted_claude";
    return "assisted_stub";
  }

  function taskSupervisionSummary(task) {
    const policy = taskSupervisionPolicy(task);
    if (policy.mode === "manual") return "manual";
    const askCount = Object.values(policy.ask_user_gates).filter(Boolean).length;
    const hostParts = policy.host_backend === "stub"
      ? ["stub"]
      : [policy.host_backend, policy.host_model || "default", `proxy ${policy.host_proxy_enabled ? "on" : "off"}`];
    return `${policy.mode} via ${hostParts.join(" / ")} | max rounds ${policy.max_rounds} | ask user ${askCount}/${supervisionAskUserGateDefs.length}`;
  }

  function normalizeTaskContextThreshold(value) {
    const rawThreshold = Number(value ?? defaultTaskContextThresholdPercent);
    return Number.isFinite(rawThreshold)
      ? Math.max(1, Math.min(99, Math.round(rawThreshold)))
      : defaultTaskContextThresholdPercent;
  }

  function taskContextManagementPolicy(task) {
    const policy = task?.context_management && typeof task.context_management === "object" ? task.context_management : {};
    return {
      auto_compact_enabled: Boolean(policy.auto_compact_enabled),
      auto_compact_threshold_percent: normalizeTaskContextThreshold(policy.auto_compact_threshold_percent)
    };
  }

  function taskContextSummary(task) {
    const policy = taskContextManagementPolicy(task);
    return policy.auto_compact_enabled ? `auto at ${policy.auto_compact_threshold_percent}%` : "auto off";
  }

  function taskSupervisionPayloadFromMode(selectedMode, maxRoundsValue, askUserGates, hostOptions = {}) {
    const assisted = selectedMode !== "manual";
    const codexHost = selectedMode === "assisted_codex";
    const claudeHost = selectedMode === "assisted_claude";
    const realHost = codexHost || claudeHost;
    return {
      mode: assisted ? "assisted" : "manual",
      host_backend: codexHost ? "codex" : (claudeHost ? "claude" : "stub"),
      host_model: realHost ? (hostOptions.hostModel || null) : null,
      host_proxy_enabled: realHost ? Boolean(hostOptions.hostProxyEnabled) : false,
      real_agent_enabled: realHost,
      max_rounds: Number(maxRoundsValue || defaultTaskSupervisionMaxRounds),
      ask_user_gates: normalizeAskUserGates(askUserGates)
    };
  }

  window.AHATaskMetadata = Object.freeze({
    collaborationModeOptions,
    collaborationModeDescriptionKeys,
    collaborationModeDescriptions,
    workflowTemplateOptions,
    workflowTemplateDescriptionKeys,
    workflowTemplateDescriptions,
    defaultTaskSupervisionMaxRounds,
    defaultTaskContextThresholdPercent,
    supervisionAskUserGateDefs,
    collaborationModeDescription,
    workflowTemplateDescription,
    collaborationModeMaxSubAgents,
    collaborationModeDelegationPolicy,
    inferredTaskCollaborationMode,
    taskCollaborationSummary,
    taskWorkflowSummary,
    defaultAskUserGates,
    normalizeAskUserGates,
    taskSupervisionPolicy,
    taskSupervisionModeValue,
    taskSupervisionSummary,
    normalizeTaskContextThreshold,
    taskContextManagementPolicy,
    taskContextSummary,
    taskSupervisionPayloadFromMode
  });
}());
