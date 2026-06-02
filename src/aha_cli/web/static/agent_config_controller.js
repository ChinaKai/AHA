(() => {
  function createAgentConfigController(options = {}) {
    const escapeHtml = options.escapeHtml || (value => String(value ?? ""));
    const selectOptions = options.selectOptions || (() => "");
    const backendModelSelectOptions = options.backendModelSelectOptions || (() => "");
    const agentBackendOptions = options.agentBackendOptions || (() => []);
    const sandboxOptions = options.sandboxOptions || [];
    const approvalOptions = options.approvalOptions || [];
    const proxySelectOptions = options.proxySelectOptions || (() => "");
    const readAgentConfigEditor = options.readAgentConfigEditor || (() => ({}));
    const normalizeAgentConfig = options.normalizeAgentConfig || (config => config || {});
    const agentConfigValue = options.agentConfigValue || (config => JSON.stringify(config || {}));
    const agentConfigLabel = options.agentConfigLabel || (config => JSON.stringify(config || {}));
    const agentBackendModelChanged = options.agentBackendModelChanged || (() => false);
    const agentRuntimeConfigChanged = options.agentRuntimeConfigChanged || (() => false);
    const fillModelSelect = options.fillModelSelect || (() => {});
    const confirmDialogAction = options.confirmDialogAction || (() => Promise.resolve(true));
    const agentBackendProcessStatus = options.agentBackendProcessStatus || (() => "");
    const agentRuntimeConfirmDialogEl = options.agentRuntimeConfirmDialogEl || null;
    const agentRuntimeConfirmMessageEl = options.agentRuntimeConfirmMessageEl || null;
    const windowRef = options.windowRef || window;
    const selectedTask = options.selectedTask || (() => null);
    const loadStatus = options.loadStatus || (() => Promise.resolve());
    const ensureActiveTabData = options.ensureActiveTabData || (() => Promise.resolve());
    const renderPanel = options.renderPanel || (() => {});
    const renderAgents = options.renderAgents || (() => {});
    const contextDetails = options.contextDetails || { delete: () => {} };
    const fetchWithTimeout = options.fetchWithTimeout || window.fetch.bind(window);
    const apiUrl = options.apiUrl || (path => path);
    const runScopedPayload = options.runScopedPayload || (payload => payload);
    const alertUser = options.alert || (message => window.alert(message));

    function agentConfigEditorHtml(agentId, currentConfig) {
      return `
        <div class="agent-config-editor" data-agent-config-editor data-agent-id="${escapeHtml(agentId)}">
          <select data-agent-config-part="backend" title="Backend">${selectOptions(agentBackendOptions(), currentConfig.backend)}</select>
          <select data-agent-config-part="model" title="Model">${backendModelSelectOptions(currentConfig.backend, currentConfig.model)}</select>
          <select data-agent-config-part="sandbox" title="Sandbox">${selectOptions(sandboxOptions, currentConfig.sandbox)}</select>
          <select data-agent-config-part="approval" title="Approval">${selectOptions(approvalOptions, currentConfig.approval)}</select>
          <select data-agent-config-part="proxy_enabled" title="Proxy">${proxySelectOptions(currentConfig.proxyEnabled)}</select>
          <button type="button" data-agent-config-apply>Confirm</button>
        </div>
      `;
    }

    async function confirmAgentConfigChange(agent, previousConfig, nextConfig) {
      if (agentConfigValue(previousConfig) === agentConfigValue(nextConfig)) return true;
      const previousLabel = agentConfigLabel(previousConfig);
      const nextLabel = agentConfigLabel(nextConfig);
      return await confirmDialogAction({
        title: "切换 agent backend config？",
        message: "AHA 会停止当前 backend、重置 backend_session_id，并给新 backend 写入交接信息。",
        confirmLabel: "切换 config",
        details: [
          ["Agent", agent.id],
          ["当前", previousLabel],
          ["目标", nextLabel]
        ]
      });
    }

    function syncAgentConfigEditorModel(card) {
      const backendSelect = card.querySelector('[data-agent-config-part="backend"]');
      const modelSelect = card.querySelector('[data-agent-config-part="model"]');
      fillModelSelect(modelSelect, backendSelect?.value || "codex", modelSelect?.value || "");
    }

    function agentRuntimeFieldLabel(field) {
      if (field === "agent_config") return "runtime settings";
      if (field === "sandbox") return "sandbox";
      if (field === "approval") return "approval";
      if (field === "proxy_enabled") return "proxy";
      return field || "runtime setting";
    }

    function agentRuntimeChangeNeedsRestartChoice(agent, field) {
      if (!["agent_config", "sandbox", "approval", "proxy_enabled"].includes(field)) return false;
      return ["running", "busy"].includes(agentBackendProcessStatus(agent));
    }

    function requestAgentRuntimeConfigAction(agent, field, value) {
      const label = agentRuntimeFieldLabel(field);
      const message = [
        `Change ${agent.id} ${label} to ${value === true ? "on" : value === false ? "off" : value}?`,
        "",
        "The current backend process is already running.",
        "Save for next start keeps the process running.",
        "Save & restart backend applies the change immediately."
      ].join("\n");
      if (!agentRuntimeConfirmDialogEl || typeof agentRuntimeConfirmDialogEl.showModal !== "function") {
        if (windowRef.confirm(`${message}\n\nOK = Save & restart backend`)) return Promise.resolve("restart");
        return Promise.resolve(windowRef.confirm("Save for next backend start instead?") ? "next" : "cancel");
      }
      if (agentRuntimeConfirmMessageEl) {
        agentRuntimeConfirmMessageEl.textContent = message;
      }
      if (agentRuntimeConfirmDialogEl.open) agentRuntimeConfirmDialogEl.close("cancel");
      return new Promise(resolve => {
        const onClose = () => resolve(agentRuntimeConfirmDialogEl.returnValue || "cancel");
        agentRuntimeConfirmDialogEl.returnValue = "cancel";
        agentRuntimeConfirmDialogEl.addEventListener("close", onClose, { once: true });
        try {
          agentRuntimeConfirmDialogEl.showModal();
        } catch (_err) {
          agentRuntimeConfirmDialogEl.removeEventListener("close", onClose);
          resolve(windowRef.confirm(`${message}\n\nOK = Save & restart backend`) ? "restart" : "cancel");
        }
      });
    }

    async function updateAgentConfig(agentId, config, options = {}) {
      const task = selectedTask();
      if (!task || !agentId) return;
      const normalized = normalizeAgentConfig(config);
      const payload = { task_id: task.id, agent_id: agentId };
      payload.sandbox = normalized.sandbox;
      payload.approval = normalized.approval;
      payload.proxy_enabled = normalized.proxyEnabled;
      if (options.includeBackendModel) {
        payload.backend = normalized.backend;
        payload.model = normalized.model || null;
      }
      if (options.restartBackend) payload.restart_backend = true;
      const res = await fetchWithTimeout(apiUrl("/api/agent-config"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(runScopedPayload(payload))
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        alertUser(body.error || "Failed to update agent config");
        return;
      }
      await loadStatus({ forceAgents: true });
      contextDetails.delete(task.id);
      await ensureActiveTabData();
      renderPanel();
    }

    function bindAgentConfigEditor(card, agent, currentConfig) {
      card.addEventListener("change", event => {
        const target = event.target instanceof HTMLElement ? event.target : null;
        if (target?.dataset.agentConfigPart === "backend") syncAgentConfigEditorModel(card);
      });
      card.addEventListener("click", event => {
        const target = event.target instanceof HTMLElement ? event.target : null;
        if (!target?.matches("[data-agent-config-apply]")) return;
        const nextConfig = readAgentConfigEditor(card);
        const backendModelChanged = agentBackendModelChanged(currentConfig, nextConfig);
        const runtimeChanged = agentRuntimeConfigChanged(currentConfig, nextConfig);
        if (!backendModelChanged && !runtimeChanged) return;
        void (async () => {
          let restartBackend = false;
          if (backendModelChanged) {
            const confirmed = await confirmAgentConfigChange(agent, currentConfig, nextConfig);
            if (!confirmed) {
              renderAgents();
              return;
            }
          } else if (runtimeChanged && agentRuntimeChangeNeedsRestartChoice(agent, "agent_config")) {
            const action = await requestAgentRuntimeConfigAction(agent, "agent_config", agentConfigLabel(nextConfig));
            if (action === "cancel") {
              renderAgents();
              return;
            }
            restartBackend = action === "restart";
          }
          await updateAgentConfig(agent.id, nextConfig, { includeBackendModel: backendModelChanged, restartBackend });
        })();
      });
    }

    return Object.freeze({
      agentConfigEditorHtml,
      bindAgentConfigEditor,
      confirmAgentConfigChange,
      requestAgentRuntimeConfigAction,
      syncAgentConfigEditorModel,
      updateAgentConfig
    });
  }

  window.AHAAgentConfigController = Object.freeze({ createAgentConfigController });
})();
