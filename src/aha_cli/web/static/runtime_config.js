(() => {
  function createRuntimeConfigHelpers(options = {}) {
    const configString = options.configString || ((value, fallback = "") => String(value || fallback || "").trim());
    const defaultModelForBackend = options.defaultModelForBackend || (() => "");
    const modelLabelForBackend = options.modelLabelForBackend || ((_backend, model) => model || "default");

    function agentModelValue(agent, task) {
      return configString(
        agent?.model ||
          (agent?.id === "main" ? task?.preferred_model : "") ||
          defaultModelForBackend(agent?.backend || task?.preferred_backend || "codex")
      );
    }

    function normalizeAgentConfig(config = {}) {
      return {
        backend: configString(config.backend, "codex"),
        model: configString(config.model),
        sandbox: configString(config.sandbox, "workspace-write"),
        approval: configString(config.approval, "never"),
        proxyEnabled: Boolean(config.proxyEnabled)
      };
    }

    function agentConfigValue(config = {}) {
      const normalized = normalizeAgentConfig(config);
      return JSON.stringify([
        normalized.backend,
        normalized.model,
        normalized.sandbox,
        normalized.approval,
        normalized.proxyEnabled
      ]);
    }

    function agentConfigLabel(config = {}) {
      const normalized = normalizeAgentConfig(config);
      return [
        normalized.backend,
        modelLabelForBackend(normalized.backend, normalized.model),
        normalized.sandbox,
        normalized.approval,
        `proxy ${normalized.proxyEnabled ? "on" : "off"}`
      ].join(" / ");
    }

    function agentBackendModelChanged(previousConfig, nextConfig) {
      const previous = normalizeAgentConfig(previousConfig);
      const next = normalizeAgentConfig(nextConfig);
      return previous.backend !== next.backend || previous.model !== next.model;
    }

    function agentRuntimeConfigChanged(previousConfig, nextConfig) {
      const previous = normalizeAgentConfig(previousConfig);
      const next = normalizeAgentConfig(nextConfig);
      return previous.sandbox !== next.sandbox || previous.approval !== next.approval || previous.proxyEnabled !== next.proxyEnabled;
    }

    function proxySelectOptions(current) {
      const selected = Boolean(current) ? "true" : "false";
      return [
        ["false", "proxy off"],
        ["true", "proxy on"]
      ].map(([value, label]) => (
        `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`
      )).join("");
    }

    function readAgentConfigEditor(card) {
      return normalizeAgentConfig({
        backend: card.querySelector('[data-agent-config-part="backend"]')?.value,
        model: card.querySelector('[data-agent-config-part="model"]')?.value,
        sandbox: card.querySelector('[data-agent-config-part="sandbox"]')?.value,
        approval: card.querySelector('[data-agent-config-part="approval"]')?.value,
        proxyEnabled: card.querySelector('[data-agent-config-part="proxy_enabled"]')?.value === "true"
      });
    }

    return Object.freeze({
      agentModelValue,
      normalizeAgentConfig,
      agentConfigValue,
      agentConfigLabel,
      agentBackendModelChanged,
      agentRuntimeConfigChanged,
      proxySelectOptions,
      readAgentConfigEditor
    });
  }

  function createRuntimeOptionsController(elements = {}, deps = {}) {
    const configString = deps.configString || ((value, fallback = "") => String(value || fallback || "").trim());
    const escapeHtml = deps.escapeHtml || (value => String(value ?? ""));
    const envModelPrefix = deps.claudeEnvModelPrefix || "env:";
    let backendModels = new Map();
    let backendCommands = new Map();
    let workspaceData = [];

    function envModelValue(name) {
      const clean = configString(name).trim();
      return clean ? `${envModelPrefix}${clean}` : "";
    }

    function isEnvModelValue(value) {
      return configString(value).startsWith(envModelPrefix);
    }

    function envModelName(value) {
      if (!isEnvModelValue(value)) return "";
      return configString(value).slice(envModelPrefix.length).trim();
    }

    function envModelLabel(backend, group, index) {
      const name = deps.bootstrapEnvGroupName?.(group, index) || "";
      const model = configString(backend === "codex" ? group?.OPENAI_MODEL : group?.ANTHROPIC_MODEL, "not configured");
      return `${model} (${name})`;
    }

    function envModelOptionsForBackend(backend) {
      const cfg = deps.bootstrapConfigData?.();
      const groups = deps.bootstrapEnvGroups?.(cfg?.[backend]?.env, backend) || [];
      return groups.map((group, index) => ({
        name: envModelValue(deps.bootstrapEnvGroupName?.(group, index)),
        label: envModelLabel(backend, group, index)
      })).filter(option => option.name);
    }

    function codexModelOptions() {
      const official = backendModels.get("codex") || [];
      const officialOptions = official.map(model => ({
        name: configString(model.name),
        label: configString(model.label, model.name || "default")
      }));
      return [...officialOptions, ...envModelOptionsForBackend("codex")];
    }

    function claudeModelOptions() {
      const official = backendModels.get("claude") || [];
      const officialOptions = official.map(model => ({
        name: configString(model.name),
        label: configString(model.label, model.name || "default")
      }));
      return [...officialOptions, ...envModelOptionsForBackend("claude")];
    }

    function modelOptionsForBackend(backend) {
      if (backend === "codex") return codexModelOptions();
      if (backend === "claude") return claudeModelOptions();
      return backendModels.get(backend) || [{ name: "", label: "default" }];
    }

    function backendModelSelectOptions(backend, current) {
      const selected = configString(current);
      return modelOptionsForBackend(backend).map(model => {
        const name = configString(model.name);
        const label = configString(model.label, name || "default");
        return `<option value="${escapeHtml(name)}" ${name === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
      }).join("");
    }

    function defaultModelForBackend(backend) {
      const cfg = deps.bootstrapConfigData?.();
      if (backend === "claude") {
        const configured = configString(cfg?.claude?.model);
        const legacyEnv = configString(cfg?.claude?.env_active);
        return configured || envModelValue(legacyEnv);
      }
      if (backend === "codex") {
        const configured = configString(cfg?.codex?.model);
        const legacyEnv = configString(cfg?.codex?.env_active);
        return configured || envModelValue(legacyEnv);
      }
      return "";
    }

    function modelLabelForBackend(backend, value) {
      const selected = configString(value);
      const option = modelOptionsForBackend(backend).find(item => configString(item.name) === selected);
      return configString(option?.label, selected || "default");
    }

    function fillModelSelect(select, backend, selected = "") {
      if (!select) return;
      const options = modelOptionsForBackend(backend);
      select.innerHTML = "";
      for (const model of options) {
        const opt = document.createElement("option");
        opt.value = configString(model.name);
        opt.textContent = configString(model.label, model.name || "default");
        select.appendChild(opt);
      }
      const values = [...select.options].map(item => item.value);
      const requested = configString(selected);
      const configured = defaultModelForBackend(backend);
      const fallback = values.includes(requested) ? requested : configured;
      if (values.includes(fallback)) select.value = fallback;
    }

    function renderModelOptions() {
      const previous = elements.taskModelEl?.value || "";
      if (elements.taskModelEl) elements.taskModelEl.disabled = false;
      fillModelSelect(elements.taskModelEl, elements.taskBackendEl?.value, previous);
    }

    function applyBackendData(backends = []) {
      backendModels = new Map();
      backendCommands = new Map();
      if (elements.taskBackendEl) elements.taskBackendEl.innerHTML = "";
      for (const backend of backends) {
        backendModels.set(backend.name, backend.models || [{ name: "", label: "default" }]);
        backendCommands.set(backend.name, backend.commands || []);
        if (!elements.taskBackendEl) continue;
        const opt = document.createElement("option");
        opt.value = backend.name;
        opt.textContent = backend.name;
        elements.taskBackendEl.appendChild(opt);
      }
      if ([...(elements.taskBackendEl?.options || [])].some(item => item.value === "codex")) {
        elements.taskBackendEl.value = "codex";
      }
      renderModelOptions();
    }

    async function loadBackends() {
      const payload = await deps.fetchJson?.("/api/backends", {}, "Failed to load backends");
      applyBackendData(payload?.backends || []);
      return payload;
    }

    function applyWorkspaceData(workspaces = []) {
      workspaceData = Array.isArray(workspaces) ? workspaces : [];
      renderWorkspaceSelect();
    }

    async function loadWorkspaces() {
      const payload = await deps.fetchJson?.("/api/workspaces", {}, "Failed to load workspaces");
      if (payload?.default_workspace_path) {
        deps.setDefaultWorkspacePath?.(payload.default_workspace_path);
      }
      applyWorkspaceData(payload?.workspaces || []);
    }

    function renderWorkspaceSelect() {
      if (!elements.workspaceSelectEl || !elements.workspaceCustomEl) return;
      const previous = elements.workspaceSelectEl.value;
      elements.workspaceSelectEl.innerHTML = "";
      for (const workspace of workspaceData) {
        const opt = document.createElement("option");
        opt.value = workspace.path;
        opt.dataset.workspaceId = workspace.id || "";
        opt.textContent = workspace.label || workspace.name;
        elements.workspaceSelectEl.appendChild(opt);
      }
      const custom = document.createElement("option");
      custom.value = "__custom__";
      custom.textContent = "Custom path...";
      elements.workspaceSelectEl.appendChild(custom);

      const preferred =
        workspaceData.find(item => item.path === previous) ||
        workspaceData.find(item => item.name === "fw_omni_builder") ||
        workspaceData[0];
      if (preferred) {
        elements.workspaceSelectEl.value = preferred.path;
      } else {
        elements.workspaceSelectEl.value = "__custom__";
        const fallbackPath = deps.bootstrapData?.()?.default_workspace_path;
        if (!elements.workspaceCustomEl.value && fallbackPath) elements.workspaceCustomEl.value = fallbackPath;
      }
      elements.workspaceCustomEl.classList.toggle("hidden", elements.workspaceSelectEl.value !== "__custom__");
    }

    function agentBackendOptions() {
      const names = [...backendModels.keys()].filter(Boolean);
      return names.length ? names : ["codex", "claude", "stub"];
    }

    return Object.freeze({
      agentBackendOptions,
      applyBackendData,
      applyWorkspaceData,
      backendCommandsFor: backend => backendCommands.get(backend) || [],
      backendModels: () => backendModels,
      backendModelSelectOptions,
      claudeEnvModelName: envModelName,
      claudeEnvModelValue: envModelValue,
      claudeModelOptions,
      isClaudeEnvModelValue: isEnvModelValue,
      codexModelOptions,
      defaultModelForBackend,
      fillModelSelect,
      loadBackends,
      loadWorkspaces,
      modelLabelForBackend,
      modelOptionsForBackend,
      renderModelOptions,
      renderWorkspaceSelect,
      workspaceData: () => workspaceData
    });
  }

  window.AHARuntimeConfig = Object.freeze({ createRuntimeConfigHelpers, createRuntimeOptionsController });
})();
