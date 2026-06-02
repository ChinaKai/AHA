(() => {
  function createTaskConfigController(options = {}) {
    const escapeHtml = options.escapeHtml || (value => String(value ?? ""));
    const els = options.els || {};
    const defaults = options.defaults || {};
    const helpers = options.helpers || {};
    const api = options.api || {};
    const getSelectedTask = options.selectedTask || (() => null);
    const getStatusData = options.statusData || (() => null);
    const getConfigData = options.configData || (() => ({}));
    const currentRunId = options.currentRunId || (() => "");
    const alertUser = options.alert || (message => window.alert(message));
    let createProxyOverride = false;
    let proxyEditingUntil = 0;
    let supervisionEditingUntil = 0;
    let contextEditingUntil = 0;

    function renderAskUserGateControls(containerEl, gates) {
      if (!containerEl) return;
      const normalized = helpers.normalizeAskUserGates(gates);
      containerEl.innerHTML = helpers.supervisionAskUserGateDefs.map(([key, label]) => `
        <label>
          <input type="checkbox" data-supervision-ask-user-gate="${escapeHtml(key)}" ${normalized[key] ? "checked" : ""}>
          <span>${escapeHtml(label)}</span>
        </label>
      `).join("");
    }

    function readAskUserGateControls(containerEl) {
      const gates = helpers.defaultAskUserGates();
      if (!containerEl) return gates;
      containerEl.querySelectorAll("[data-supervision-ask-user-gate]").forEach(input => {
        gates[input.dataset.supervisionAskUserGate] = Boolean(input.checked);
      });
      return gates;
    }

    function applyProxyDefaultValue(inputEl, defaultValue, enabledEl, options = {}) {
      let changed = false;
      if (inputEl && !inputEl.value.trim()) {
        inputEl.value = defaultValue;
        changed = true;
      }
      if (options.enableProxy && inputEl?.value.trim() && enabledEl && !enabledEl.checked) {
        enabledEl.checked = true;
        changed = true;
      }
      return changed;
    }

    function fillTaskCreateProxyDefaultFor(input) {
      return false;
    }

    function fillSelectedTaskProxyDefaultFor(input) {
      return false;
    }

    function fillRunProxyDefaultFor(input) {
      if (input === els.runHttpProxyEl) return applyProxyDefaultValue(els.runHttpProxyEl, defaults.defaultHttpProxy, els.runProxyEnabledEl, { enableProxy: true });
      if (input === els.runHttpsProxyEl) return applyProxyDefaultValue(els.runHttpsProxyEl, defaults.defaultHttpsProxy, els.runProxyEnabledEl, { enableProxy: true });
      if (input === els.runNoProxyEl) return applyProxyDefaultValue(els.runNoProxyEl, defaults.defaultNoProxy, els.runProxyEnabledEl);
      return false;
    }

    function setCreateProxyDefaultsFromInputs() {
      return false;
    }

    function runProxyConfig() {
      return getStatusData()?.proxy || {};
    }

    function backendProxyDefaultConfig(backend = els.taskBackendEl?.value) {
      const cfg = getConfigData() || {};
      const legacy = cfg.proxy || {};
      const backendKey = String(backend || cfg.backend || "").trim();
      const backendCfg = cfg[backendKey] || {};
      const proxy = backendCfg.proxy || {};
      const hasBackendProxy = Boolean(backendCfg.proxy && Object.keys(proxy).length);
      const hasLegacyProxy = Boolean(cfg.proxy && Object.keys(legacy).length);
      if (!hasBackendProxy && !hasLegacyProxy) return runProxyConfig();
      return {
        enabled: proxy.enabled !== undefined ? Boolean(proxy.enabled) : Boolean(legacy.enabled),
        http_proxy: proxy.http_proxy || legacy.http_proxy || null,
        https_proxy: proxy.https_proxy || legacy.https_proxy || null,
        no_proxy: proxy.no_proxy || legacy.no_proxy || null
      };
    }

    function syncCreateProxyDefaultForBackend(options = {}) {
      if (!els.taskProxyEnabledEl) return;
      if (options.force) createProxyOverride = false;
      if (createProxyOverride && !options.force) return;
      els.taskProxyEnabledEl.checked = Boolean(backendProxyDefaultConfig().enabled);
    }

    function runProxySummary() {
      const config = runProxyConfig();
      const parts = [];
      if (config.http_proxy) parts.push("HTTP");
      if (config.https_proxy) parts.push("HTTPS");
      if (config.no_proxy) parts.push("NO_PROXY");
      return parts.length ? `${config.enabled ? "default on" : "default off"} · ${parts.join(" · ")}` : "not configured";
    }

    function renderRunProxyEditor() {
      const runId = String(currentRunId() || "").trim();
      const disabled = !runId;
      const config = runProxyConfig();
      if (!disabled && els.taskProxyEnabledEl && !document.querySelector("#task-create-dialog")?.open) {
        syncCreateProxyDefaultForBackend();
      }
      if (!els.runProxyEditorEl || !els.runProxyFormEl) return;
      els.runProxyFormEl.querySelectorAll("input, button").forEach(item => {
        item.disabled = disabled;
      });
      if (els.runProxyEnabledEl) els.runProxyEnabledEl.checked = Boolean(config.enabled);
      if (els.runHttpProxyEl) els.runHttpProxyEl.value = config.http_proxy || "";
      if (els.runHttpsProxyEl) els.runHttpsProxyEl.value = config.https_proxy || "";
      if (els.runNoProxyEl) els.runNoProxyEl.value = config.no_proxy || defaults.defaultNoProxy;
      if (els.runProxyStateEl) els.runProxyStateEl.textContent = disabled ? "Select a run to edit proxy." : runProxySummary();
    }

    function renderTaskProxyEditor(task = getSelectedTask()) {
      if (!els.taskProxyEditorEl || !els.taskProxyFormEl) return;
      const disabled = !task;
      els.taskProxyFormEl.querySelectorAll("input, button").forEach(item => {
        item.disabled = disabled;
      });
      if (!task) {
        if (els.taskProxyStateEl) els.taskProxyStateEl.textContent = "Select a task to edit proxy.";
        if (els.selectedTaskProxyEnabledEl) els.selectedTaskProxyEnabledEl.checked = false;
        return;
      }
      if (els.selectedTaskProxyEnabledEl) els.selectedTaskProxyEnabledEl.checked = Boolean(task.preferred_proxy_enabled);
      if (els.taskProxyStateEl) els.taskProxyStateEl.textContent = helpers.taskProxySummary(task, runProxyConfig());
    }

    function syncSupervisionModeFields(modeEl, maxRoundsFieldEl, askUserFieldEl) {
      const manual = modeEl?.value === "manual";
      maxRoundsFieldEl?.classList.toggle("hidden", manual);
      if (maxRoundsFieldEl) maxRoundsFieldEl.hidden = manual;
      askUserFieldEl?.classList.toggle("hidden", manual);
      if (askUserFieldEl) askUserFieldEl.hidden = manual;
    }

    function syncTaskSupervisionModeFields() {
      syncSupervisionModeFields(els.selectedTaskSupervisionModeEl, els.selectedTaskSupervisionMaxRoundsFieldEl, els.selectedTaskSupervisionAskUserFieldEl);
    }

    function syncCreateTaskSupervisionModeFields() {
      syncSupervisionModeFields(els.taskSupervisionModeEl, els.taskSupervisionMaxRoundsFieldEl, els.taskSupervisionAskUserFieldEl);
    }

    function renderTaskSupervisionEditor(task = getSelectedTask()) {
      if (!els.taskSupervisionEditorEl || !els.taskSupervisionFormEl) return;
      const disabled = !task;
      const applyDisabledState = () => els.taskSupervisionFormEl.querySelectorAll("input, select, button").forEach(item => {
        item.disabled = disabled;
      });
      if (!task) {
        if (els.selectedTaskSupervisionModeEl) els.selectedTaskSupervisionModeEl.value = "manual";
        if (els.selectedTaskSupervisionMaxRoundsEl) els.selectedTaskSupervisionMaxRoundsEl.value = String(defaults.defaultTaskSupervisionMaxRounds);
        renderAskUserGateControls(els.selectedTaskSupervisionAskUserGatesEl, helpers.defaultAskUserGates());
        if (els.taskSupervisionStateEl) els.taskSupervisionStateEl.textContent = "Select a task to edit supervision.";
        syncTaskSupervisionModeFields();
        applyDisabledState();
        return;
      }
      const policy = helpers.taskSupervisionPolicy(task);
      if (els.selectedTaskSupervisionModeEl) els.selectedTaskSupervisionModeEl.value = helpers.taskSupervisionModeValue(policy);
      if (els.selectedTaskSupervisionMaxRoundsEl) els.selectedTaskSupervisionMaxRoundsEl.value = String(policy.max_rounds || defaults.defaultTaskSupervisionMaxRounds);
      renderAskUserGateControls(els.selectedTaskSupervisionAskUserGatesEl, policy.ask_user_gates);
      syncTaskSupervisionModeFields();
      if (els.taskSupervisionStateEl) els.taskSupervisionStateEl.textContent = helpers.taskSupervisionSummary(task);
      applyDisabledState();
    }

    function syncTaskContextFields() {
      const enabled = Boolean(els.selectedTaskContextAutoCompactEnabledEl?.checked);
      els.selectedTaskContextThresholdFieldEl?.classList.toggle("hidden", !enabled);
      if (els.selectedTaskContextThresholdFieldEl) els.selectedTaskContextThresholdFieldEl.hidden = !enabled;
    }

    function renderTaskContextEditor(task = getSelectedTask()) {
      if (!els.taskContextEditorEl || !els.taskContextFormEl) return;
      const disabled = !task;
      els.taskContextFormEl.querySelectorAll("input, button").forEach(item => {
        item.disabled = disabled;
      });
      if (!task) {
        if (els.selectedTaskContextAutoCompactEnabledEl) els.selectedTaskContextAutoCompactEnabledEl.checked = false;
        if (els.selectedTaskContextThresholdEl) els.selectedTaskContextThresholdEl.value = String(defaults.defaultTaskContextThresholdPercent);
        if (els.taskContextStateEl) els.taskContextStateEl.textContent = "Select a task to edit context management.";
        syncTaskContextFields();
        return;
      }
      const policy = helpers.taskContextManagementPolicy(task);
      if (els.selectedTaskContextAutoCompactEnabledEl) els.selectedTaskContextAutoCompactEnabledEl.checked = policy.auto_compact_enabled;
      if (els.selectedTaskContextThresholdEl) els.selectedTaskContextThresholdEl.value = String(policy.auto_compact_threshold_percent);
      syncTaskContextFields();
      if (els.taskContextStateEl) els.taskContextStateEl.textContent = helpers.taskContextSummary(task);
    }

    function markTaskProxyEditing(durationMs = 10000) {
      proxyEditingUntil = Date.now() + durationMs;
    }

    function markTaskSupervisionEditing(durationMs = 10000) {
      supervisionEditingUntil = Date.now() + durationMs;
    }

    function markTaskContextEditing(durationMs = 10000) {
      contextEditingUntil = Date.now() + durationMs;
    }

    function isTaskProxyEditing() {
      const active = document.activeElement;
      return Date.now() < proxyEditingUntil || (active instanceof Element && Boolean(els.taskProxyFormEl?.contains(active) || els.runProxyFormEl?.contains(active)));
    }

    function isTaskSupervisionEditing() {
      const active = document.activeElement;
      return Date.now() < supervisionEditingUntil || (active instanceof Element && Boolean(els.taskSupervisionFormEl?.contains(active)));
    }

    function isTaskContextEditing() {
      const active = document.activeElement;
      return Date.now() < contextEditingUntil || (active instanceof Element && Boolean(els.taskContextFormEl?.contains(active)));
    }

    function resetEditing() {
      proxyEditingUntil = 0;
      supervisionEditingUntil = 0;
      contextEditingUntil = 0;
    }

    async function saveTaskProxyConfig() {
      const task = getSelectedTask();
      if (!task) return;
      const proxyEnabled = Boolean(els.selectedTaskProxyEnabledEl?.checked);
      await api.fetchJson(api.apiUrl(`/api/task/${encodeURIComponent(task.id)}/proxy`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(api.runScopedPayload({
          proxy_enabled: proxyEnabled
        }))
      }, "Failed to update task proxy");
      proxyEditingUntil = 0;
      await api.loadStatus({ forceAgents: true, forceTaskProxy: true });
    }

    async function saveRunProxyConfig() {
      const runId = String(currentRunId() || "").trim();
      if (!runId) return;
      const httpProxy = els.runHttpProxyEl?.value.trim() || "";
      const httpsProxy = els.runHttpsProxyEl?.value.trim() || "";
      let noProxy = els.runNoProxyEl?.value.trim() || "";
      if ((httpProxy || httpsProxy) && !noProxy) {
        noProxy = defaults.defaultNoProxy;
        if (els.runNoProxyEl) els.runNoProxyEl.value = noProxy;
      }
      await api.fetchJson(api.apiUrl(`/api/runs/${encodeURIComponent(runId)}/proxy`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(api.runScopedPayload({
          proxy_enabled: Boolean(els.runProxyEnabledEl?.checked),
          http_proxy: httpProxy,
          https_proxy: httpsProxy,
          no_proxy: noProxy
        }))
      }, "Failed to update run proxy");
      proxyEditingUntil = 0;
      await api.loadStatus({ forceAgents: true, forceTaskProxy: true });
    }

    async function saveTaskSupervisionConfig() {
      const task = getSelectedTask();
      if (!task) return;
      const selectedMode = els.selectedTaskSupervisionModeEl?.value || "manual";
      const supervision = helpers.taskSupervisionPayloadFromMode(
        selectedMode,
        els.selectedTaskSupervisionMaxRoundsEl?.value || defaults.defaultTaskSupervisionMaxRounds,
        readAskUserGateControls(els.selectedTaskSupervisionAskUserGatesEl)
      );
      await api.fetchJson(api.apiUrl(`/api/task/${encodeURIComponent(task.id)}/supervision`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(api.runScopedPayload(supervision))
      }, "Failed to update task supervision");
      supervisionEditingUntil = 0;
      await api.loadStatus({ forceTaskSupervision: true });
    }

    async function saveTaskContextConfig() {
      const task = getSelectedTask();
      if (!task) return;
      const enabled = Boolean(els.selectedTaskContextAutoCompactEnabledEl?.checked);
      const threshold = helpers.normalizeTaskContextThreshold(els.selectedTaskContextThresholdEl?.value || defaults.defaultTaskContextThresholdPercent);
      if (els.selectedTaskContextThresholdEl) els.selectedTaskContextThresholdEl.value = String(threshold);
      await api.fetchJson(api.apiUrl(`/api/task/${encodeURIComponent(task.id)}/context-management`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(api.runScopedPayload({
          auto_compact_enabled: enabled,
          auto_compact_threshold_percent: threshold
        }))
      }, "Failed to update task context management");
      contextEditingUntil = 0;
      await api.loadStatus({ forceTaskContext: true });
    }

    function bind() {
      els.runProxyFormEl?.addEventListener("pointerdown", () => markTaskProxyEditing());
      els.runProxyFormEl?.addEventListener("focusin", () => markTaskProxyEditing());
      els.runProxyFormEl?.addEventListener("input", () => markTaskProxyEditing());
      els.runProxyFormEl?.addEventListener("change", () => markTaskProxyEditing());
      els.runProxyFormEl?.addEventListener("submit", async event => {
        event.preventDefault();
        try {
          await saveRunProxyConfig();
        } catch (err) {
          alertUser(err?.message || String(err));
        }
      });
      els.taskProxyFormEl?.addEventListener("pointerdown", () => markTaskProxyEditing());
      els.taskProxyFormEl?.addEventListener("focusin", () => markTaskProxyEditing());
      els.taskProxyFormEl?.addEventListener("input", () => markTaskProxyEditing());
      els.taskProxyFormEl?.addEventListener("change", () => markTaskProxyEditing());
      els.taskProxyFormEl?.addEventListener("submit", async event => {
        event.preventDefault();
        try {
          await saveTaskProxyConfig();
        } catch (err) {
          alertUser(err?.message || String(err));
        }
      });
      els.taskSupervisionFormEl?.addEventListener("pointerdown", () => markTaskSupervisionEditing());
      els.taskSupervisionFormEl?.addEventListener("focusin", () => markTaskSupervisionEditing());
      els.taskSupervisionFormEl?.addEventListener("input", () => markTaskSupervisionEditing());
      els.taskSupervisionFormEl?.addEventListener("change", () => {
        markTaskSupervisionEditing();
        syncTaskSupervisionModeFields();
      });
      els.taskSupervisionFormEl?.addEventListener("submit", async event => {
        event.preventDefault();
        try {
          await saveTaskSupervisionConfig();
        } catch (err) {
          alertUser(err?.message || String(err));
        }
      });
      els.taskContextFormEl?.addEventListener("pointerdown", () => markTaskContextEditing());
      els.taskContextFormEl?.addEventListener("focusin", () => markTaskContextEditing());
      els.taskContextFormEl?.addEventListener("input", () => markTaskContextEditing());
      els.taskContextFormEl?.addEventListener("change", () => {
        markTaskContextEditing();
        syncTaskContextFields();
      });
      els.taskContextFormEl?.addEventListener("submit", async event => {
        event.preventDefault();
        try {
          await saveTaskContextConfig();
        } catch (err) {
          alertUser(err?.message || String(err));
        }
      });
      [els.runHttpProxyEl, els.runHttpsProxyEl].forEach(input => {
        input?.addEventListener("input", () => {
          const configured = Boolean(els.runHttpProxyEl?.value.trim() || els.runHttpsProxyEl?.value.trim());
          if (configured && els.runProxyEnabledEl && !els.runProxyEnabledEl.checked) els.runProxyEnabledEl.checked = true;
          if (configured && els.runNoProxyEl && !els.runNoProxyEl.value.trim()) els.runNoProxyEl.value = defaults.defaultNoProxy;
        });
      });
      [els.runHttpProxyEl, els.runHttpsProxyEl, els.runNoProxyEl].forEach(input => {
        input?.addEventListener("focus", () => fillRunProxyDefaultFor(input));
        input?.addEventListener("click", () => fillRunProxyDefaultFor(input));
      });
      els.taskProxyEnabledEl?.addEventListener("change", () => {
        createProxyOverride = true;
      });
      els.taskSupervisionModeEl?.addEventListener("change", syncCreateTaskSupervisionModeFields);
    }

    return Object.freeze({
      bind,
      fillSelectedTaskProxyDefaultFor,
      fillTaskCreateProxyDefaultFor,
      fillRunProxyDefaultFor,
      isTaskContextEditing,
      isTaskProxyEditing,
      isTaskSupervisionEditing,
      markTaskContextEditing,
      markTaskProxyEditing,
      markTaskSupervisionEditing,
      readAskUserGateControls,
      renderAskUserGateControls,
      renderRunProxyEditor,
      renderTaskContextEditor,
      renderTaskProxyEditor,
      renderTaskSupervisionEditor,
      resetEditing,
      saveTaskContextConfig,
      saveRunProxyConfig,
      saveTaskProxyConfig,
      saveTaskSupervisionConfig,
      setCreateProxyDefaultsFromInputs,
      syncCreateProxyDefaultForBackend,
      syncCreateTaskSupervisionModeFields,
      syncTaskContextFields,
      syncTaskSupervisionModeFields
    });
  }

  window.AHATaskConfigController = Object.freeze({ createTaskConfigController });
})();
