(function () {
  const collaborationModeOptions = Object.freeze(["auto", "solo", "pair", "team"]);
  const collaborationModeDescriptions = Object.freeze({
    auto: "AHA 自动选择最快执行方式；只有能节省时间时才启用 sub-agent。",
    solo: "单人模式：main 自己完成全部工作，适合小任务和快速修改。",
    pair: "双人模式：最多 1 个 sub-agent 并行处理实现、调研或 review。",
    team: "团队模式：最多 2 个 sub-agent 处理可拆分责任区，main 负责协调和合并。"
  });
  const workflowTemplateOptions = Object.freeze(["auto", "bugfix", "feature", "review", "embedded-driver", "fault-debug", "hil-regression", "release"]);
  const workflowTemplateDescriptions = Object.freeze({
    auto: "自动识别任务类型和最高效执行策略。",
    bugfix: "面向定位、修复、回归验证的效率策略。",
    feature: "面向设计、实现、测试/文档的效率策略。",
    review: "面向独立风险审查、测试审查和代码审查。",
    "embedded-driver": "面向 datasheet/register 分析、驱动实现和边界测试。",
    "fault-debug": "面向 crash/log 分析、最近改动审查和复现验证。",
    "hil-regression": "面向 HIL 测试矩阵、自动化日志和回归风险。",
    release: "面向 changelog/docs、build/package 和最终风险审查。"
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

  function collaborationModeDescription(mode) {
    return collaborationModeDescriptions[mode] || collaborationModeDescriptions.auto;
  }

  function workflowTemplateDescription(template) {
    return workflowTemplateDescriptions[template] || workflowTemplateDescriptions.auto;
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
    return `${policy.mode}${policy.mode === "assisted" ? ` via ${policy.host_backend}` : ""} | max rounds ${policy.max_rounds} | ask user ${askCount}/${supervisionAskUserGateDefs.length}`;
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

  function taskSupervisionPayloadFromMode(selectedMode, maxRoundsValue, askUserGates) {
    const assisted = selectedMode !== "manual";
    const codexHost = selectedMode === "assisted_codex";
    const claudeHost = selectedMode === "assisted_claude";
    return {
      mode: assisted ? "assisted" : "manual",
      host_backend: codexHost ? "codex" : (claudeHost ? "claude" : "stub"),
      real_agent_enabled: codexHost || claudeHost,
      max_rounds: Number(maxRoundsValue || defaultTaskSupervisionMaxRounds),
      ask_user_gates: normalizeAskUserGates(askUserGates)
    };
  }

  window.AHATaskMetadata = Object.freeze({
    collaborationModeOptions,
    collaborationModeDescriptions,
    workflowTemplateOptions,
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
