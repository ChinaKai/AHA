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
    const agentConfigRequestTimeoutMs = Number(options.agentConfigRequestTimeoutMs) || 45000;

    function t(key, fallback = "") {
      return window.AHAI18n?.t?.(key, fallback) || fallback;
    }

    function formatText(key, values = {}, fallback = "") {
      return window.AHAI18n?.format?.(key, values, fallback) || fallback;
    }

    function agentConfigEditorHtml(agentId, currentConfig) {
      return `
        <div class="agent-config-editor" data-agent-config-editor data-agent-id="${escapeHtml(agentId)}">
          <div class="agent-config-summary">
            <span>${escapeHtml(t("agent.runtime_settings", "Runtime settings"))}</span>
            <code>${escapeHtml(agentConfigLabel(currentConfig))}</code>
          </div>
          <div class="agent-config-fields">
            <select data-agent-config-part="backend" title="Backend">${selectOptions(agentBackendOptions(), currentConfig.backend)}</select>
            <select data-agent-config-part="model" title="Model">${backendModelSelectOptions(currentConfig.backend, currentConfig.model)}</select>
            <select data-agent-config-part="sandbox" title="Sandbox">${selectOptions(sandboxOptions, currentConfig.sandbox)}</select>
            <select data-agent-config-part="approval" title="Approval">${selectOptions(approvalOptions, currentConfig.approval)}</select>
            <select data-agent-config-part="proxy_enabled" title="Proxy">${proxySelectOptions(currentConfig.proxyEnabled)}</select>
            <button type="button" data-agent-config-apply>${escapeHtml(t("agent.config_confirm", "Confirm"))}</button>
          </div>
        </div>
      `;
    }

    async function confirmAgentConfigChange(agent, previousConfig, nextConfig) {
      if (agentConfigValue(previousConfig) === agentConfigValue(nextConfig)) return true;
      const previousLabel = agentConfigLabel(previousConfig);
      const nextLabel = agentConfigLabel(nextConfig);
      return await confirmDialogAction({
        title: t("agent.confirm_backend_config_title", "Switch agent backend config?"),
        message: t("agent.confirm_backend_config_message", "AHA will stop the current backend, reset backend_session_id, and write handoff information for the new backend."),
        confirmLabel: t("agent.confirm_backend_config_confirm", "Switch config"),
        details: [
          ["Agent", agent.id],
          [t("agent.current", "Current"), previousLabel],
          [t("agent.target", "Target"), nextLabel]
        ]
      });
    }

    function syncAgentConfigEditorModel(card) {
      const backendSelect = card.querySelector('[data-agent-config-part="backend"]');
      const modelSelect = card.querySelector('[data-agent-config-part="model"]');
      fillModelSelect(modelSelect, backendSelect?.value || "codex", modelSelect?.value || "");
    }

    function agentRuntimeFieldLabel(field) {
      if (field === "agent_config") return t("agent.field_runtime_settings", "runtime settings");
      if (field === "sandbox") return t("agent.field_sandbox", "sandbox");
      if (field === "approval") return t("agent.field_approval", "approval");
      if (field === "proxy_enabled") return t("agent.field_proxy", "proxy");
      return field || t("agent.field_runtime_setting", "runtime setting");
    }

    function agentRuntimeChangeNeedsRestartChoice(agent, field) {
      if (!["agent_config", "sandbox", "approval", "proxy_enabled"].includes(field)) return false;
      return ["running", "busy"].includes(agentBackendProcessStatus(agent));
    }

    function replaceObjectContents(target, source) {
      if (!target || !source) return;
      for (const key of Object.keys(target)) delete target[key];
      Object.assign(target, source);
    }

    function applyAgentConfigResponse(currentTask, body = {}) {
      if (!currentTask || !body) return;
      const nextTask = body.task && String(body.task.id || "") === String(currentTask.id || "") ? body.task : null;
      if (nextTask) {
        replaceObjectContents(currentTask, nextTask);
        return;
      }
      const nextAgent = body.agent;
      if (!nextAgent || !Array.isArray(currentTask.agents)) return;
      const index = currentTask.agents.findIndex(agent => String(agent.id || "") === String(nextAgent.id || ""));
      if (index >= 0) currentTask.agents[index] = { ...currentTask.agents[index], ...nextAgent };
      else currentTask.agents.push(nextAgent);
    }

    function requestAgentRuntimeConfigAction(agent, field, value) {
      const label = agentRuntimeFieldLabel(field);
      const valueLabel = value === true
        ? t("agent.value_on", "on")
        : value === false
          ? t("agent.value_off", "off")
          : value;
      const message = [
        formatText("agent.runtime_change_question", { agent: agent.id, field: label, value: valueLabel }, `Change ${agent.id} ${label} to ${valueLabel}?`),
        "",
        t("agent.runtime_change_process_running", "The current backend process is already running."),
        t("agent.runtime_save_next_hint", "Save for next start keeps the process running."),
        t("agent.runtime_save_restart_hint", "Save & restart backend applies the change immediately.")
      ].join("\n");
      if (!agentRuntimeConfirmDialogEl || typeof agentRuntimeConfirmDialogEl.showModal !== "function") {
        if (windowRef.confirm(`${message}\n\n${t("agent.runtime_save_restart_ok", "OK = Save & restart backend")}`)) return Promise.resolve("restart");
        return Promise.resolve(windowRef.confirm(t("agent.runtime_save_next_question", "Save for next backend start instead?")) ? "next" : "cancel");
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
          resolve(windowRef.confirm(`${message}\n\n${t("agent.runtime_save_restart_ok", "OK = Save & restart backend")}`) ? "restart" : "cancel");
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
      try {
        const res = await fetchWithTimeout(apiUrl("/api/agent-config"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(runScopedPayload(payload))
        }, agentConfigRequestTimeoutMs);
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
          throw new Error(body.error || "Failed to update agent config");
        }
        await loadStatus({ forceAgents: true });
        applyAgentConfigResponse(selectedTask() || task, body);
        renderAgents();
        contextDetails.delete(task.id);
        await ensureActiveTabData();
        renderPanel();
      } catch (err) {
        alertUser(err?.message || "Failed to update agent config");
        return;
      }
    }

    function bindAgentConfigEditor(card, agent, currentConfig) {
      const editor = card.matches?.("[data-agent-config-editor]") ? card : card.querySelector("[data-agent-config-editor]");
      if (!editor) return;
      editor.addEventListener("change", event => {
        const target = event.target instanceof HTMLElement ? event.target : null;
        if (target?.dataset.agentConfigPart === "backend") syncAgentConfigEditorModel(editor);
      });
      editor.addEventListener("click", event => {
        const target = event.target instanceof HTMLElement ? event.target : null;
        if (!target?.matches("[data-agent-config-apply]")) return;
        const nextConfig = readAgentConfigEditor(editor);
        const backendModelChanged = agentBackendModelChanged(currentConfig, nextConfig);
        const runtimeChanged = agentRuntimeConfigChanged(currentConfig, nextConfig);
        if (!backendModelChanged && !runtimeChanged) return;
        void (async () => {
          target.disabled = true;
          let restartBackend = false;
          try {
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
          } finally {
            target.disabled = false;
          }
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
