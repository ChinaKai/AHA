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
  const hardwareDebugChannelTypes = Object.freeze(["uart", "nfs", "telnet"]);
  const hardwareDebugPermissionKeys = Object.freeze(["read", "write"]);
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
    const tokenPolicy = taskTokenSavingPolicy(task);
    return {
      auto_compact_enabled: tokenPolicy.enabled,
      auto_compact_threshold_percent: defaultTaskContextThresholdPercent,
      enabled: tokenPolicy.enabled,
      provider: tokenPolicy.provider
    };
  }

  function taskTokenSavingPolicy(task) {
    const policy = task?.token_saving && typeof task.token_saving === "object" ? task.token_saving : {};
    const legacyPolicy = task?.context_management && typeof task.context_management === "object" ? task.context_management : {};
    const hasTokenPolicy = Boolean(task?.token_saving && typeof task.token_saving === "object");
    const provider = String(policy.provider || "headroom").trim().toLowerCase() || "headroom";
    return {
      enabled: typeof policy.enabled === "boolean" ? policy.enabled : (!hasTokenPolicy && legacyPolicy.auto_compact_enabled === true),
      provider
    };
  }

  function taskContextSummary(task) {
    return taskTokenSavingSummary(task);
  }

  function taskTokenSavingSummary(task) {
    const policy = taskTokenSavingPolicy(task);
    return policy.enabled ? `${policy.provider} on` : "off";
  }

  function defaultHardwareDebugPermissions() {
    return {
      read: true,
      write: false
    };
  }

  function normalizeHardwareDebugPermissions(value) {
    const permissions = defaultHardwareDebugPermissions();
    if (value && typeof value === "object") {
      if (Object.prototype.hasOwnProperty.call(value, "serial_read")) permissions.read = Boolean(value.serial_read);
      if (Object.prototype.hasOwnProperty.call(value, "serial_write")) permissions.write = Boolean(value.serial_write);
      hardwareDebugPermissionKeys.forEach(key => {
        if (Object.prototype.hasOwnProperty.call(value, key)) permissions[key] = Boolean(value[key]);
      });
    }
    return permissions;
  }

  function normalizeHardwareDebugChannel(value) {
    if (!value || typeof value !== "object") return null;
    const type = String(value.type || value.kind || "").trim().toLowerCase();
    if (!hardwareDebugChannelTypes.includes(type)) return null;
    return {
      type,
      settings: value.settings && typeof value.settings === "object" ? { ...value.settings } : {},
      permissions: normalizeHardwareDebugPermissions(value.permissions)
    };
  }

  function legacyHardwareDebugChannels(policy) {
    if (!policy || typeof policy !== "object" || !policy.enabled) return [];
    const devices = Array.isArray(policy.devices)
      ? policy.devices
      : (policy.devices && typeof policy.devices === "object" ? [policy.devices] : [{}]);
    return devices.map(device => normalizeHardwareDebugChannel({
      type: "uart",
      settings: {
        port: device?.port || device?.path || "",
        baudrate: device?.baudrate || device?.baud || 115200
      },
      permissions: policy.permissions || {}
    })).filter(Boolean);
  }

  function splitTaskSkillPaths(value) {
    if (Array.isArray(value)) return value.map(item => String(item || "").trim()).filter(Boolean);
    return String(value || "").split(/\r?\n|,/).map(item => item.trim()).filter(Boolean);
  }

  function taskSkillsPolicy(task) {
    const policy = task?.task_skills && typeof task.task_skills === "object" ? task.task_skills : {};
    return {
      enabled_paths: splitTaskSkillPaths(policy.enabled_paths || policy.skill_paths || policy.paths || policy.skills || [])
    };
  }

  function taskSkillsSummary(task) {
    const count = taskSkillsPolicy(task).enabled_paths.length;
    return count ? `${count} skill${count === 1 ? "" : "s"}` : "off";
  }

  function taskHardwareDebugPolicy(task) {
    const policy = task?.hardware_debug && typeof task.hardware_debug === "object" ? task.hardware_debug : {};
    const rawChannels = Array.isArray(policy.channels) ? policy.channels : [];
    const channels = rawChannels.length
      ? rawChannels.map(normalizeHardwareDebugChannel).filter(Boolean)
      : legacyHardwareDebugChannels(policy);
    const enabled = typeof policy.enabled === "boolean" ? policy.enabled : channels.length > 0;
    return {
      enabled,
      channels
    };
  }

  function taskHardwareDebugSummary(task) {
    const policy = taskHardwareDebugPolicy(task);
    if (!policy.enabled) return "off";
    if (!policy.channels.length) return "on | no channels";
    const types = policy.channels.map(channel => channel.type.toUpperCase()).join(", ");
    return `${policy.channels.length} channel${policy.channels.length === 1 ? "" : "s"} | ${types}`;
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
    hardwareDebugChannelTypes,
    hardwareDebugPermissionKeys,
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
    taskTokenSavingPolicy,
    taskTokenSavingSummary,
    defaultHardwareDebugPermissions,
    normalizeHardwareDebugPermissions,
    normalizeHardwareDebugChannel,
    splitTaskSkillPaths,
    taskSkillsPolicy,
    taskSkillsSummary,
    taskHardwareDebugPolicy,
    taskHardwareDebugSummary,
    taskSupervisionPayloadFromMode
  });
}());
