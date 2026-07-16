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
    const getSkillOptions = options.skillOptions || (() => []);
    const currentRunId = options.currentRunId || (() => "");
    const alertUser = options.alert || (message => window.alert(message));
    let createProxyOverride = false;
    let createHostProxyOverride = false;
    let selectedHostProxyOverride = false;
    let proxyEditingUntil = 0;
    let supervisionEditingUntil = 0;
    let contextEditingUntil = 0;
    let observeProxyEditingUntil = 0;
    let hardwareEditingUntil = 0;
    let taskSettingsEditorTaskId = "";

    function taskById(taskId) {
      const id = String(taskId || "");
      if (!id) return null;
      const tasks = getStatusData()?.tasks || [];
      return Array.isArray(tasks) ? tasks.find(task => String(task?.id || "") === id) || null : null;
    }

    function getTaskSettingsTask() {
      if (taskSettingsEditorTaskId) {
        const task = taskById(taskSettingsEditorTaskId);
        if (task) return task;
        taskSettingsEditorTaskId = "";
      }
      return getSelectedTask();
    }

    function t(key, fallback = "") {
      return window.AHAI18n?.t?.(key, fallback) || fallback;
    }

    function setTaskSettingsEditorTaskId(taskId) {
      taskSettingsEditorTaskId = String(taskId || "");
    }

    function resolveTaskEditorTask(task, explicit) {
      if (explicit) {
        setTaskSettingsEditorTaskId(task?.id || "");
        return task || null;
      }
      return getTaskSettingsTask();
    }

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

    function createProxyEnabledValue() {
      if (!els.taskProxyEnabledEl) return false;
      if (els.taskProxyEnabledEl.tagName === "SELECT") {
        return els.taskProxyEnabledEl.value === "on";
      }
      return Boolean(els.taskProxyEnabledEl.checked);
    }

    function setCreateProxyEnabled(value) {
      if (!els.taskProxyEnabledEl) return;
      if (els.taskProxyEnabledEl.tagName === "SELECT") {
        els.taskProxyEnabledEl.value = value ? "on" : "off";
      } else {
        els.taskProxyEnabledEl.checked = Boolean(value);
      }
    }

    function renderCreateProxyDefaultsPreview() {
      const previewEl = els.taskProxyDefaultsPreviewEl;
      if (!previewEl) return;
      if (!createProxyEnabledValue()) {
        previewEl.hidden = true;
        previewEl.innerHTML = "";
        return;
      }
      const config = backendProxyDefaultConfig();
      const rows = [
        ["HTTP", config.http_proxy || ""],
        ["HTTPS", config.https_proxy || ""],
        ["NO_PROXY", config.no_proxy || defaults.defaultNoProxy || ""]
      ];
      previewEl.innerHTML = rows.map(([label, value]) => `
        <div>
          <span>${escapeHtml(label)}</span>
          <code title="${escapeHtml(value || "-")}">${escapeHtml(value || "-")}</code>
        </div>
      `).join("");
      previewEl.hidden = false;
    }

    function syncCreateProxyDefaultForBackend(options = {}) {
      if (!els.taskProxyEnabledEl) return;
      if (options.force) createProxyOverride = false;
      if (createProxyOverride && !options.force) {
        renderCreateProxyDefaultsPreview();
        return;
      }
      setCreateProxyEnabled(Boolean(backendProxyDefaultConfig().enabled));
      renderCreateProxyDefaultsPreview();
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

    function renderTaskProxyEditor(taskArg) {
      const task = resolveTaskEditorTask(taskArg, arguments.length > 0);
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

    function supervisionHostBackendForMode(mode) {
      if (mode === "assisted_codex") return "codex";
      if (mode === "assisted_claude") return "claude";
      return "stub";
    }

    function syncSupervisionModeFields(
      modeEl,
      maxRoundsFieldEl,
      askUserFieldEl,
      hostModelFieldEl,
      hostModelEl,
      hostProxyFieldEl,
      hostProxyEnabledEl,
      syncOptions = {}
    ) {
      const manual = modeEl?.value === "manual";
      const hostBackend = supervisionHostBackendForMode(modeEl?.value || "manual");
      const realHost = hostBackend !== "stub";
      maxRoundsFieldEl?.classList.toggle("hidden", manual);
      if (maxRoundsFieldEl) maxRoundsFieldEl.hidden = manual;
      askUserFieldEl?.classList.toggle("hidden", manual);
      if (askUserFieldEl) askUserFieldEl.hidden = manual;
      hostModelFieldEl?.classList.toggle("hidden", !realHost);
      if (hostModelFieldEl) hostModelFieldEl.hidden = !realHost;
      hostProxyFieldEl?.classList.toggle("hidden", !realHost);
      if (hostProxyFieldEl) hostProxyFieldEl.hidden = !realHost;
      if (hostModelEl && realHost) {
        const requested = syncOptions.selectedModel !== undefined ? syncOptions.selectedModel : hostModelEl.value;
        helpers.fillModelSelect?.(hostModelEl, hostBackend, requested || helpers.defaultModelForBackend?.(hostBackend) || "");
      }
      if (hostProxyEnabledEl) {
        if (!realHost) {
          hostProxyEnabledEl.checked = false;
        } else if (syncOptions.selectedProxyEnabled !== undefined) {
          hostProxyEnabledEl.checked = Boolean(syncOptions.selectedProxyEnabled);
        } else if (syncOptions.applySelectedProxyDefault && !selectedHostProxyOverride) {
          hostProxyEnabledEl.checked = Boolean(backendProxyDefaultConfig(hostBackend).enabled);
        } else if (syncOptions.applyCreateProxyDefault && !createHostProxyOverride) {
          hostProxyEnabledEl.checked = Boolean(backendProxyDefaultConfig(hostBackend).enabled);
        }
      }
    }

    function syncTaskSupervisionModeFields(options = {}) {
      if (options.force) selectedHostProxyOverride = false;
      syncSupervisionModeFields(
        els.selectedTaskSupervisionModeEl,
        els.selectedTaskSupervisionMaxRoundsFieldEl,
        els.selectedTaskSupervisionAskUserFieldEl,
        els.selectedTaskSupervisionHostModelFieldEl,
        els.selectedTaskSupervisionHostModelEl,
        els.selectedTaskSupervisionHostProxyFieldEl,
        els.selectedTaskSupervisionHostProxyEnabledEl,
        { applySelectedProxyDefault: Boolean(options.applyProxyDefault) }
      );
    }

    function syncCreateTaskSupervisionModeFields(options = {}) {
      if (options.force) createHostProxyOverride = false;
      syncSupervisionModeFields(
        els.taskSupervisionModeEl,
        els.taskSupervisionMaxRoundsFieldEl,
        els.taskSupervisionAskUserFieldEl,
        els.taskSupervisionHostModelFieldEl,
        els.taskSupervisionHostModelEl,
        els.taskSupervisionHostProxyFieldEl,
        els.taskSupervisionHostProxyEnabledEl,
        { applyCreateProxyDefault: true }
      );
    }

    function renderTaskSupervisionEditor(taskArg) {
      const task = resolveTaskEditorTask(taskArg, arguments.length > 0);
      if (!els.taskSupervisionEditorEl || !els.taskSupervisionFormEl) return;
      const disabled = !task;
      const applyDisabledState = () => els.taskSupervisionFormEl.querySelectorAll("input, select, button").forEach(item => {
        item.disabled = disabled;
      });
      if (!task) {
        selectedHostProxyOverride = false;
        if (els.selectedTaskSupervisionModeEl) els.selectedTaskSupervisionModeEl.value = "manual";
        if (els.selectedTaskSupervisionMaxRoundsEl) els.selectedTaskSupervisionMaxRoundsEl.value = String(defaults.defaultTaskSupervisionMaxRounds);
        renderAskUserGateControls(els.selectedTaskSupervisionAskUserGatesEl, helpers.defaultAskUserGates());
        syncTaskSupervisionModeFields();
        if (els.selectedTaskSupervisionHostProxyEnabledEl) els.selectedTaskSupervisionHostProxyEnabledEl.checked = false;
        if (els.taskSupervisionStateEl) els.taskSupervisionStateEl.textContent = "Select a task to edit supervision.";
        applyDisabledState();
        return;
      }
      const policy = helpers.taskSupervisionPolicy(task);
      const selectedMode = helpers.taskSupervisionModeValue(policy);
      selectedHostProxyOverride = false;
      if (els.selectedTaskSupervisionModeEl) els.selectedTaskSupervisionModeEl.value = selectedMode;
      if (els.selectedTaskSupervisionMaxRoundsEl) els.selectedTaskSupervisionMaxRoundsEl.value = String(policy.max_rounds || defaults.defaultTaskSupervisionMaxRounds);
      renderAskUserGateControls(els.selectedTaskSupervisionAskUserGatesEl, policy.ask_user_gates);
      syncSupervisionModeFields(
        els.selectedTaskSupervisionModeEl,
        els.selectedTaskSupervisionMaxRoundsFieldEl,
        els.selectedTaskSupervisionAskUserFieldEl,
        els.selectedTaskSupervisionHostModelFieldEl,
        els.selectedTaskSupervisionHostModelEl,
        els.selectedTaskSupervisionHostProxyFieldEl,
        els.selectedTaskSupervisionHostProxyEnabledEl,
        {
          selectedModel: policy.host_model || helpers.defaultModelForBackend?.(supervisionHostBackendForMode(selectedMode)) || "",
          selectedProxyEnabled: policy.host_proxy_enabled
        }
      );
      if (els.taskSupervisionStateEl) els.taskSupervisionStateEl.textContent = helpers.taskSupervisionSummary(task);
      applyDisabledState();
    }

    function syncTaskContextFields() {
      els.selectedTaskContextThresholdFieldEl?.classList.add("hidden");
      if (els.selectedTaskContextThresholdFieldEl) els.selectedTaskContextThresholdFieldEl.hidden = true;
    }

    function renderTaskContextEditor(taskArg) {
      const task = resolveTaskEditorTask(taskArg, arguments.length > 0);
      if (!els.taskContextEditorEl || !els.taskContextFormEl) return;
      const disabled = !task;
      els.taskContextFormEl.querySelectorAll("input, button").forEach(item => {
        item.disabled = disabled;
      });
      if (!task) {
        if (els.selectedTaskContextAutoCompactEnabledEl) els.selectedTaskContextAutoCompactEnabledEl.checked = false;
        if (els.taskContextStateEl) els.taskContextStateEl.textContent = "Select a task to edit token saving.";
        syncTaskContextFields();
        renderTaskObserveProxyEditor(null, true);
        return;
      }
      const policy = helpers.taskTokenSavingPolicy?.(task) || helpers.taskContextManagementPolicy(task);
      if (els.selectedTaskContextAutoCompactEnabledEl) els.selectedTaskContextAutoCompactEnabledEl.checked = policy.enabled === true || policy.auto_compact_enabled === true;
      syncTaskContextFields();
      if (els.taskContextStateEl) els.taskContextStateEl.textContent = helpers.taskTokenSavingSummary?.(task) || helpers.taskContextSummary(task);
      renderTaskObserveProxyEditor(task, true);
    }

    function renderTaskObserveProxyEditor(taskArg, explicit = false) {
      const task = resolveTaskEditorTask(taskArg, explicit || arguments.length > 0);
      if (!els.taskObserveProxyEditorEl || !els.taskObserveProxyFormEl) return;
      const disabled = !task;
      els.taskObserveProxyFormEl.querySelectorAll("input, button").forEach(item => {
        item.disabled = disabled;
      });
      if (!task) {
        if (els.selectedTaskObserveProxyEnabledEl) els.selectedTaskObserveProxyEnabledEl.checked = false;
        if (els.taskObserveProxyStateEl) els.taskObserveProxyStateEl.textContent = "Select a task to edit observe proxy.";
        return;
      }
      const policy = helpers.taskObserveProxyPolicy?.(task) || {};
      if (els.selectedTaskObserveProxyEnabledEl) els.selectedTaskObserveProxyEnabledEl.checked = Boolean(policy.enabled);
      if (els.taskObserveProxyStateEl) els.taskObserveProxyStateEl.textContent = helpers.taskObserveProxySummary?.(task) || "off";
    }

    function selectedTaskSkillPaths(selectEl = els.selectedTaskSkillSelectEl) {
      if (!selectEl) return [];
      const checked = [...(selectEl.querySelectorAll?.("input[data-task-skill-path]:checked") || [])]
        .map(input => input.value)
        .filter(Boolean);
      if (checked.length || selectEl.tagName !== "SELECT") return checked;
      return [...(selectEl.selectedOptions || [])].map(option => option.value).filter(Boolean);
    }

    function renderSkillSelect(selectEl, selectedPaths = []) {
      if (!selectEl) return;
      const selected = new Set(selectedPaths);
      const options = Array.isArray(getSkillOptions()) ? getSkillOptions() : [];
      if (!options.length) {
        selectEl.innerHTML = `<div class="skill-option-empty">${escapeHtml(window.AHAI18n?.t?.("task.skills_empty", "No skills found") || "No skills found")}</div>`;
        return;
      }
      selectEl.innerHTML = options.map(option => {
        const path = String(option.path || "").trim();
        const label = String(option.label || option.id || path).trim();
        const source = String(option.source || "").trim();
        const text = source ? `${label} (${source})` : label;
        return `
          <label class="skill-option-chip">
            <input type="checkbox" data-task-skill-path value="${escapeHtml(path)}" ${selected.has(path) ? "checked" : ""}>
            <span>${escapeHtml(text)}</span>
          </label>
        `;
      }).join("");
    }

    function renderTaskSkillsEditor(taskArg) {
      const task = resolveTaskEditorTask(taskArg, arguments.length > 0);
      if (!els.taskSkillsEditorEl || !els.taskSkillsFormEl) return;
      const disabled = !task;
      const policy = helpers.taskSkillsPolicy?.(task) || { enabled_paths: [] };
      renderSkillSelect(els.selectedTaskSkillSelectEl, disabled ? [] : policy.enabled_paths);
      els.taskSkillsFormEl.querySelectorAll("input, button").forEach(item => {
        item.disabled = disabled;
      });
      if (els.taskSkillsStateEl) {
        els.taskSkillsStateEl.textContent = disabled ? "Select a task to edit skills." : (helpers.taskSkillsSummary?.(task) || "off");
      }
    }

    function syncTaskHardwareDebugFields(options = {}) {
      const disabled = Boolean(options.disabled);
      const form = els.taskHardwareFormEl;
      const modeEl = form?.querySelector("[data-hardware-mode]");
      if (modeEl) modeEl.disabled = disabled;
      const mode = String(modeEl?.value || "off");
      const enabled = mode !== "off";
      const serial = mode === "serial" || mode === "both";
      const network = mode === "network" || mode === "both";
      const settings = form?.querySelector("[data-hardware-settings]");
      if (settings) settings.hidden = !enabled;
      form?.querySelectorAll("[data-hardware-serial-settings]").forEach(item => { item.hidden = !serial; });
      form?.querySelectorAll("[data-hardware-network-settings]").forEach(item => { item.hidden = !network; });
      form?.querySelectorAll('[data-hardware-field^="serial."]').forEach(input => { input.disabled = disabled || !serial; });
      form?.querySelectorAll('[data-hardware-field^="network."]').forEach(input => { input.disabled = disabled || !network; });
      form?.querySelectorAll('[data-hardware-field^="credentials."]').forEach(input => { input.disabled = disabled || !enabled; });
      form?.querySelectorAll("[data-hardware-permission]").forEach(input => { input.disabled = disabled || !enabled; });
      const submit = els.taskHardwareFormEl?.querySelector('button[type="submit"]');
      if (submit) submit.disabled = disabled;
    }

    function resetTaskHardwareForm() {
      const form = els.taskHardwareFormEl;
      const mode = form?.querySelector("[data-hardware-mode]");
      if (mode) mode.value = "off";
      const access = form?.querySelector('[data-hardware-permission="access"]');
      if (access) access.value = "read_only";
      form?.querySelectorAll("[data-hardware-field]").forEach(input => {
        input.value = input.defaultValue || "";
      });
    }

    function setTaskHardwarePolicy(policy = {}) {
      const form = els.taskHardwareFormEl;
      const mode = form?.querySelector("[data-hardware-mode]");
      if (mode) mode.value = policy.mode || "off";
      const set = (key, value) => {
        const input = form?.querySelector(`[data-hardware-field="${key}"]`);
        if (input) input.value = value ?? input.defaultValue ?? "";
      };
      set("serial.device", policy.serial?.device || "");
      set("serial.baudrate", policy.serial?.baudrate || 115200);
      set("network.device_ip", policy.network?.device_ip || "");
      set("credentials.username", policy.credentials?.username || "");
      set("credentials.password", "");
      const password = form?.querySelector('[data-hardware-field="credentials.password"]');
      if (password) {
        password.placeholder = policy.credentials?.password_configured
          ? t("task.hardware_password_configured", "Configured — leave blank to keep")
          : "";
      }
      const access = form?.querySelector('[data-hardware-permission="access"]');
      if (access) access.value = policy.permissions?.access || "read_only";
    }

    function renderTaskHardwareEditor(taskArg) {
      const task = resolveTaskEditorTask(taskArg, arguments.length > 0);
      renderTaskSkillsEditor(task, true);
      if (!els.taskHardwareEditorEl || !els.taskHardwareFormEl) return;
      const disabled = !task;
      if (!task) {
        resetTaskHardwareForm();
        syncTaskHardwareDebugFields({ disabled });
        if (els.taskHardwareStateEl) els.taskHardwareStateEl.textContent = "Select a task to edit hardware debug.";
        return;
      }
      const policy = helpers.taskHardwareDebugPolicy?.(task) || {};
      setTaskHardwarePolicy(policy);
      syncTaskHardwareDebugFields({ disabled });
      if (els.taskHardwareStateEl) els.taskHardwareStateEl.textContent = helpers.taskHardwareDebugSummary?.(task) || "off";
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

    function markTaskObserveProxyEditing(durationMs = 10000) {
      observeProxyEditingUntil = Date.now() + durationMs;
    }

    function markTaskHardwareEditing(durationMs = 10000) {
      hardwareEditingUntil = Date.now() + durationMs;
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
      return Date.now() < contextEditingUntil
        || Date.now() < observeProxyEditingUntil
        || (active instanceof Element && Boolean(els.taskContextFormEl?.contains(active) || els.taskObserveProxyFormEl?.contains(active)));
    }

    function isTaskHardwareEditing() {
      const active = document.activeElement;
      return Date.now() < hardwareEditingUntil || (active instanceof Element && Boolean(els.taskHardwareFormEl?.contains(active)));
    }

    function resetEditing() {
      proxyEditingUntil = 0;
      supervisionEditingUntil = 0;
      contextEditingUntil = 0;
      observeProxyEditingUntil = 0;
      hardwareEditingUntil = 0;
    }

    async function saveTaskProxyConfig() {
      const task = getTaskSettingsTask();
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
      const task = getTaskSettingsTask();
      if (!task) return;
      const selectedMode = els.selectedTaskSupervisionModeEl?.value || "manual";
      const supervision = helpers.taskSupervisionPayloadFromMode(
        selectedMode,
        els.selectedTaskSupervisionMaxRoundsEl?.value || defaults.defaultTaskSupervisionMaxRounds,
        readAskUserGateControls(els.selectedTaskSupervisionAskUserGatesEl),
        {
          hostModel: els.selectedTaskSupervisionHostModelEl?.value || null,
          hostProxyEnabled: Boolean(els.selectedTaskSupervisionHostProxyEnabledEl?.checked)
        }
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
      const task = getTaskSettingsTask();
      if (!task) return;
      const enabled = Boolean(els.selectedTaskContextAutoCompactEnabledEl?.checked);
      await api.fetchJson(api.apiUrl(`/api/task/${encodeURIComponent(task.id)}/token-saving`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(api.runScopedPayload({
          enabled,
          provider: "nav"
        }))
      }, "Failed to update task token saving");
      contextEditingUntil = 0;
      await api.loadStatus({ forceTaskContext: true });
    }

    async function saveTaskObserveProxyConfig() {
      const task = getTaskSettingsTask();
      if (!task) return;
      const enabled = Boolean(els.selectedTaskObserveProxyEnabledEl?.checked);
      await api.fetchJson(api.apiUrl(`/api/task/${encodeURIComponent(task.id)}/observe-proxy`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(api.runScopedPayload({
          enabled
        }))
      }, "Failed to update task observe proxy");
      observeProxyEditingUntil = 0;
      await api.loadStatus({ forceTaskContext: true });
    }

    async function saveTaskSkillsConfig() {
      const task = getTaskSettingsTask();
      if (!task) return;
      await api.fetchJson(api.apiUrl(`/api/task/${encodeURIComponent(task.id)}/skills`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(api.runScopedPayload({
          enabled_paths: selectedTaskSkillPaths()
        }))
      }, "Failed to update task skills");
      hardwareEditingUntil = 0;
      await api.loadStatus({ forceTaskHardware: true });
    }

    async function saveTaskHardwareConfig() {
      const task = getTaskSettingsTask();
      if (!task) return;
      const form = els.taskHardwareFormEl;
      const value = key => String(form?.querySelector(`[data-hardware-field="${key}"]`)?.value || "").trim();
      const password = String(form?.querySelector('[data-hardware-field="credentials.password"]')?.value || "");
      const credentials = { username: value("credentials.username") };
      if (password) credentials.password = password;
      const hardwareDebug = {
        mode: String(form?.querySelector("[data-hardware-mode]")?.value || "off"),
        serial: { device: value("serial.device"), baudrate: Number(value("serial.baudrate") || "115200") || 115200 },
        network: { device_ip: value("network.device_ip") },
        credentials,
        permissions: {
          access: String(form?.querySelector('[data-hardware-permission="access"]')?.value || "read_only")
        }
      };
      await api.fetchJson(api.apiUrl(`/api/task/${encodeURIComponent(task.id)}/hardware-debug`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(api.runScopedPayload(hardwareDebug))
      }, "Failed to update task hardware debug");
      hardwareEditingUntil = 0;
      await api.loadStatus({ forceTaskHardware: true });
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
      els.taskObserveProxyFormEl?.addEventListener("pointerdown", () => markTaskObserveProxyEditing());
      els.taskObserveProxyFormEl?.addEventListener("focusin", () => markTaskObserveProxyEditing());
      els.taskObserveProxyFormEl?.addEventListener("input", () => markTaskObserveProxyEditing());
      els.taskObserveProxyFormEl?.addEventListener("change", () => markTaskObserveProxyEditing());
      els.taskObserveProxyFormEl?.addEventListener("submit", async event => {
        event.preventDefault();
        try {
          await saveTaskObserveProxyConfig();
        } catch (err) {
          alertUser(err?.message || String(err));
        }
      });
      els.taskSkillsFormEl?.addEventListener("pointerdown", () => markTaskHardwareEditing());
      els.taskSkillsFormEl?.addEventListener("focusin", () => markTaskHardwareEditing());
      els.taskSkillsFormEl?.addEventListener("change", () => markTaskHardwareEditing());
      els.selectedTaskSkillSelectEl?.addEventListener("focus", () => renderTaskSkillsEditor());
      els.taskSkillsFormEl?.addEventListener("submit", async event => {
        event.preventDefault();
        try {
          await saveTaskSkillsConfig();
        } catch (err) {
          alertUser(err?.message || String(err));
        }
      });
      els.taskHardwareFormEl?.addEventListener("pointerdown", () => markTaskHardwareEditing());
      els.taskHardwareFormEl?.addEventListener("focusin", () => markTaskHardwareEditing());
      els.taskHardwareFormEl?.addEventListener("input", () => markTaskHardwareEditing());
      els.taskHardwareFormEl?.addEventListener("change", event => {
        markTaskHardwareEditing();
        if (event.target instanceof Element && event.target.matches("[data-hardware-mode]")) {
          syncTaskHardwareDebugFields();
        }
      });
      els.taskHardwareFormEl?.addEventListener("submit", async event => {
        event.preventDefault();
        try {
          await saveTaskHardwareConfig();
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
        renderCreateProxyDefaultsPreview();
      });
      els.taskSupervisionHostProxyEnabledEl?.addEventListener("change", () => {
        createHostProxyOverride = true;
      });
      els.taskSupervisionModeEl?.addEventListener("change", () => {
        createHostProxyOverride = false;
        syncCreateTaskSupervisionModeFields();
      });
      els.selectedTaskSupervisionHostProxyEnabledEl?.addEventListener("change", () => {
        selectedHostProxyOverride = true;
      });
      els.selectedTaskSupervisionModeEl?.addEventListener("change", () => {
        selectedHostProxyOverride = false;
        syncTaskSupervisionModeFields({ applyProxyDefault: true });
      });
    }

    return Object.freeze({
      bind,
      fillSelectedTaskProxyDefaultFor,
      fillTaskCreateProxyDefaultFor,
      fillRunProxyDefaultFor,
      isTaskContextEditing,
      isTaskHardwareEditing,
      isTaskProxyEditing,
      isTaskSupervisionEditing,
      markTaskContextEditing,
      markTaskObserveProxyEditing,
      markTaskHardwareEditing,
      markTaskProxyEditing,
      markTaskSupervisionEditing,
      readAskUserGateControls,
      renderAskUserGateControls,
      renderRunProxyEditor,
      renderTaskContextEditor,
      renderTaskObserveProxyEditor,
      renderTaskHardwareEditor,
      renderTaskSkillsEditor,
      renderTaskProxyEditor,
      renderTaskSupervisionEditor,
      renderCreateProxyDefaultsPreview,
      resetEditing,
      saveTaskContextConfig,
      saveTaskObserveProxyConfig,
      saveTaskHardwareConfig,
      saveTaskSkillsConfig,
      saveRunProxyConfig,
      saveTaskProxyConfig,
      saveTaskSupervisionConfig,
      setCreateProxyDefaultsFromInputs,
      setTaskSettingsEditorTaskId,
      syncCreateProxyDefaultForBackend,
      syncCreateTaskSupervisionModeFields,
      syncTaskContextFields,
      syncTaskSupervisionModeFields
    });
  }

  window.AHATaskConfigController = Object.freeze({ createTaskConfigController });
})();
