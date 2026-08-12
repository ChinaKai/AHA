(function () {
  const backendOptions = ["codex", "claude"];
  const claudeEnvModelPrefix = "env:";
  const codexEnvGroupFields = ["OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY", "CODEX_WIRE_API", "CODEX_ENV_KEY"];
  const claudeEnvGroupFields = [
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS"
  ];
  const defaultBootstrapHttpProxy = "http://127.0.0.1:7890";
  const defaultBootstrapHttpsProxy = defaultBootstrapHttpProxy;
  const defaultBootstrapNoProxy = "localhost,127.0.0.1,::1";

  function formatContextWindow(tokens) {
    const value = Number(tokens);
    if (!Number.isFinite(value) || value <= 0) return "NA";
    return value >= 1000000 ? `${Math.round(value / 1000000)}M` : `${Math.round(value / 1000)}K`;
  }

  // Context window comes only from the gateway's own /v1/models response; no
  // hard-coded model map. Unknown models show NA.
  function modelContextWindow(model) {
    if (model && typeof model === "object") {
      const reported = Number(model.max_input_tokens);
      if (Number.isFinite(reported) && reported > 0) return reported;
    }
    return 0;
  }

  function detectModelsModalHtml(models = [], authStyle = "bearer") {
    const rows = models.map((model, index) => {
      const id = typeof model === "string" ? model : String(model?.id || "").trim();
      const windowValue = modelContextWindow(model);
      const windowLabel = formatContextWindow(windowValue);
      const outputLabel = formatContextWindow(model?.max_output_tokens);
      const modeLabel = String(model?.mode || "").trim() || "NA";
      const metaLabel = `${windowLabel}.${outputLabel}.${modeLabel}`;
      return `
        <div class="bootstrap-detect-model-row" data-bootstrap-detect-model-row data-model-id="${escapeHtml(id)}" data-model-index="${index}">
          <label class="bootstrap-detect-model-check">
            <input type="checkbox" data-bootstrap-detect-check>
            <span>${escapeHtml(id)}</span>
          </label>
          <span class="bootstrap-detect-model-meta" title="context.output.mode">${escapeHtml(metaLabel)}</span>
          <span class="bootstrap-detect-model-actions">
            <button class="bootstrap-add-row" type="button" data-bootstrap-detect-test>Test</button>
          </span>
        </div>
      `;
    }).join("");
    return `
      <div class="bootstrap-detect-modal-backdrop" data-bootstrap-detect-modal>
        <div class="bootstrap-detect-modal" role="dialog" aria-label="Detected models">
          <div class="bootstrap-detect-modal-head">
            <strong>Detected models</strong>
            <button class="bootstrap-icon-button" type="button" data-bootstrap-detect-close title="Close">x</button>
          </div>
          <label class="field-label">
            <span>Filter</span>
            <input data-bootstrap-detect-filter type="search" placeholder="Search models...">
          </label>
          <div class="bootstrap-detect-model-list" data-bootstrap-detect-list>
            ${rows}
          </div>
          <div class="bootstrap-detect-modal-foot">
            <button class="bootstrap-add-row" type="button" data-bootstrap-detect-add-selected>Add selected (0)</button>
            <button class="bootstrap-add-row" type="button" data-bootstrap-detect-close>Cancel</button>
          </div>
        </div>
      </div>
    `;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function selectOptions(options, current) {
    return options.map(value => `<option value="${escapeHtml(value)}" ${value === current ? "selected" : ""}>${escapeHtml(value)}</option>`).join("");
  }

  function configString(value, fallback = "") {
    if (value === null || value === undefined) return fallback;
    const text = String(value);
    return text || fallback;
  }

  function configListValues(value) {
    if (Array.isArray(value)) return value.map(item => String(item || "").trim()).filter(Boolean);
    if (typeof value === "string") return value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
    return [];
  }

  function bootstrapBackendOptions() {
    return [...backendOptions];
  }

  function bootstrapRootRows(value) {
    const values = configListValues(value);
    const rows = values.length ? values : [""];
    return rows.map(item => bootstrapConfigRowHtml("workspace_roots", { value: item })).join("");
  }

  function envGroupFieldsForBackend(backend) {
    return backend === "codex" ? codexEnvGroupFields : claudeEnvGroupFields;
  }

  function envGroupModelKey(backend) {
    return backend === "codex" ? "OPENAI_MODEL" : "ANTHROPIC_MODEL";
  }

  function envGroupSecretKeys(backend) {
    return backend === "codex" ? ["OPENAI_API_KEY"] : ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"];
  }

  function normalizeClaudeEnvGroup(value = {}, fallbackName = "") {
    return {
      name: value.name || fallbackName,
      ANTHROPIC_BASE_URL: value.ANTHROPIC_BASE_URL || value.base_url || "",
      ANTHROPIC_MODEL: value.ANTHROPIC_MODEL || value.model || "",
      ANTHROPIC_API_KEY: value.ANTHROPIC_API_KEY || value.api_key || "",
      ANTHROPIC_AUTH_TOKEN: value.ANTHROPIC_AUTH_TOKEN || value.auth_token || "",
      ANTHROPIC_DEFAULT_FABLE_MODEL: value.ANTHROPIC_DEFAULT_FABLE_MODEL || value.fable_model || "",
      ANTHROPIC_DEFAULT_OPUS_MODEL: value.ANTHROPIC_DEFAULT_OPUS_MODEL || value.opus_model || "",
      ANTHROPIC_DEFAULT_SONNET_MODEL: value.ANTHROPIC_DEFAULT_SONNET_MODEL || value.sonnet_model || "",
      ANTHROPIC_DEFAULT_HAIKU_MODEL: value.ANTHROPIC_DEFAULT_HAIKU_MODEL || value.ANTHROPIC_SMALL_FAST_MODEL || value.haiku_model || "",
      CLAUDE_CODE_MAX_CONTEXT_TOKENS: value.CLAUDE_CODE_MAX_CONTEXT_TOKENS || value.CLAUDE_CODE_AUTO_COMPACT_WINDOW || value.context_window || ""
    };
  }

  function bootstrapEnvGroups(value, backend = "claude") {
    if (Array.isArray(value)) {
      const groups = value.filter(item => item && typeof item === "object" && !Array.isArray(item));
      return backend === "claude" ? groups.map(item => normalizeClaudeEnvGroup(item)) : groups;
    }
    if (value && typeof value === "object") {
      if (backend === "codex") {
        return [{
          name: "default",
          OPENAI_BASE_URL: value.OPENAI_BASE_URL || value.ANTHROPIC_BASE_URL || value.base_url || "",
          OPENAI_MODEL: value.OPENAI_MODEL || value.ANTHROPIC_MODEL || value.model || "",
          OPENAI_API_KEY: value.OPENAI_API_KEY || value.ANTHROPIC_API_KEY || value.api_key || "",
          CODEX_WIRE_API: value.CODEX_WIRE_API || value.wire_api || "responses",
          CODEX_ENV_KEY: value.CODEX_ENV_KEY || value.env_key || "OPENAI_API_KEY"
        }];
      }
      return [normalizeClaudeEnvGroup(value, "default")];
    }
    return [];
  }

  function bootstrapEnvGroupName(item, index) {
    return configString(item?.name, `env-${index + 1}`);
  }

  function bootstrapEnvRows(value, _active = "", options = {}, backend = "claude") {
    const groups = bootstrapEnvGroups(value, backend);
    const rows = groups.length ? groups : [{ name: "" }];
    return rows.map((item, index) => bootstrapConfigRowHtml(`${backend}.env`, item, index, options)).join("");
  }

  function bootstrapEnvDetectHtml(backend) {
    return `
      <div class="bootstrap-env-detect" data-bootstrap-detect-backend="${escapeHtml(backend)}">
        <details class="bootstrap-env-advanced" open>
          <summary>Auto-detect models</summary>
          <div class="bootstrap-env-fields">
            <label class="field-label">
              <span>Base URL</span>
              <input data-bootstrap-detect-url placeholder="${backend === "codex" ? "https://api.openai.com/v1" : "https://api.anthropic.com"}" value="">
            </label>
            <label class="field-label">
              <span>API key / token</span>
              <input data-bootstrap-detect-key type="password" autocomplete="off" placeholder="sk-...">
            </label>
            <button class="bootstrap-add-row" type="button" data-bootstrap-detect-models>Fetch models</button>
            <div class="field-help" data-bootstrap-detect-status></div>
          </div>
        </details>
      </div>
    `;
  }

  function _setEnvRowFields(row, data, backend) {
    const nameEl = row.querySelector("[data-bootstrap-env-name]");
    if (nameEl) nameEl.value = configString(data.name);
    const fields = envGroupFieldsForBackend(backend);
    for (const key of fields) {
      const el = row.querySelector(`[data-bootstrap-env-field="${key}"]`);
      if (el && data[key] !== undefined) el.value = configString(data[key]);
    }
    if (backend === "claude") {
      const credEl = row.querySelector("[data-bootstrap-claude-credential]");
      if (credEl) credEl.value = configString(data.ANTHROPIC_AUTH_TOKEN);
    }
  }

  function insertDetectedEnvGroups(form, backend, models, baseUrl, credential, authStyle = "bearer", context = {}) {
    const kind = `${backend}.env`;
    const list = form.querySelector(`[data-bootstrap-config-list="${kind}"]`);
    const addButton = list?.querySelector(`[data-bootstrap-add-row="${kind}"]`);
    if (!list || !addButton) return 0;
    const groups = detectEnvGroupFromModels(backend, baseUrl, credential, authStyle, models);
    if (!groups.length) return 0;
    const existingRows = [...list.querySelectorAll("[data-bootstrap-row]")];
    let index = existingRows.length;
    let inserted = 0;
    let replaced = 0;
    for (const group of groups) {
      const existing = existingRows.find(row => configString(row.querySelector("[data-bootstrap-env-name]")?.value) === group.name);
      if (existing) {
        _setEnvRowFields(existing, group, backend);
        replaced += 1;
      } else {
        addButton.insertAdjacentHTML("beforebegin", bootstrapConfigRowHtml(kind, group, index + inserted));
        inserted += 1;
      }
    }
    syncBootstrapModelOptions(form, context);
    return { inserted, replaced };
  }

  function bootstrapBackendProxySwitchHtml(prefix, proxy = {}) {
    const label = prefix === "claude" ? "Claude" : "Codex";
    return `
      <label class="field-label checkbox-field">
        <span>${escapeHtml(label)} proxy default</span>
        <span class="checkbox-line">
          <input data-bootstrap-config-field="${escapeHtml(prefix)}.proxy.enabled" type="checkbox" ${proxy.enabled ? "checked" : ""}>
          <span>Enable by default for new ${escapeHtml(label)} tasks and agents</span>
        </span>
      </label>
    `;
  }

  function bootstrapSharedProxyFieldsHtml(proxy = {}) {
    return `
      <label class="field-label">
        <span>HTTP proxy</span>
        <input data-bootstrap-config-field="proxy.http_proxy" placeholder="${escapeHtml(defaultBootstrapHttpProxy)}" value="${escapeHtml(configString(proxy.http_proxy))}">
      </label>
      <label class="field-label">
        <span>HTTPS proxy</span>
        <input data-bootstrap-config-field="proxy.https_proxy" placeholder="${escapeHtml(defaultBootstrapHttpsProxy)}" value="${escapeHtml(configString(proxy.https_proxy))}">
      </label>
      <label class="field-label">
        <span>NO_PROXY</span>
        <input data-bootstrap-config-field="proxy.no_proxy" placeholder="${escapeHtml(defaultBootstrapNoProxy)}" value="${escapeHtml(configString(proxy.no_proxy))}">
      </label>
    `;
  }

  function bootstrapProxyFieldParts(input) {
    const field = String(input?.dataset?.bootstrapConfigField || "");
    const match = /^proxy\.(http_proxy|https_proxy|no_proxy)$/.exec(field);
    return match ? { name: match[1] } : null;
  }

  function bootstrapProxyField(form, name) {
    if (!form || !name) return null;
    return form.querySelector(`[data-bootstrap-config-field="proxy.${name}"]`);
  }

  function syncBootstrapProxyDefaultsForInput(input) {
    const parts = bootstrapProxyFieldParts(input);
    if (!parts || (parts.name !== "http_proxy" && parts.name !== "https_proxy")) return false;
    const form = input.closest("[data-bootstrap-config-form]");
    const httpProxy = bootstrapProxyField(form, "http_proxy");
    const httpsProxy = bootstrapProxyField(form, "https_proxy");
    const noProxy = bootstrapProxyField(form, "no_proxy");
    const configured = Boolean(httpProxy?.value.trim() || httpsProxy?.value.trim());
    if (configured && noProxy && !noProxy.value.trim()) {
      noProxy.value = defaultBootstrapNoProxy;
      return true;
    }
    return false;
  }

  function fillBootstrapProxyDefaultFor(input) {
    const parts = bootstrapProxyFieldParts(input);
    if (!parts) return false;
    const defaults = {
      http_proxy: defaultBootstrapHttpProxy,
      https_proxy: defaultBootstrapHttpsProxy,
      no_proxy: defaultBootstrapNoProxy
    };
    let changed = false;
    if (!input.value.trim()) {
      input.value = defaults[parts.name] || "";
      changed = true;
    }
    return syncBootstrapProxyDefaultsForInput(input) || changed;
  }

  function envModelValue(name) {
    const clean = configString(name).trim();
    return clean ? `${claudeEnvModelPrefix}${clean}` : "";
  }

  function envModelName(value) {
    const text = configString(value);
    return text.startsWith(claudeEnvModelPrefix) ? text.slice(claudeEnvModelPrefix.length).trim() : "";
  }

  function selectableBootstrapModelOptions(models) {
    const named = models.filter(model => configString(model?.name));
    return named.length ? named : models;
  }

  function selectedBootstrapModelValue(models, current) {
    const selected = configString(current);
    if (models.some(model => configString(model?.name) === selected)) return selected;
    return configString(models[0]?.name);
  }

  function backendModelOptions(backend, context = {}) {
    const modelOptionsForBackend = typeof context.modelOptionsForBackend === "function"
      ? context.modelOptionsForBackend
      : () => [];
    const models = modelOptionsForBackend(backend);
    return Array.isArray(models) ? models : [];
  }

  function selectedBackendModel(backend, current, context = {}) {
    return selectedBootstrapModelValue(
      selectableBootstrapModelOptions(backendModelOptions(backend, context)),
      current
    );
  }

  function backendModelSelectOptions(backend, current, context = {}) {
    const models = selectableBootstrapModelOptions(backendModelOptions(backend, context));
    const selected = selectedBootstrapModelValue(models, current);
    return models.map(model => {
      const name = configString(model.name);
      const label = configString(model.label, name || "default");
      return `<option value="${escapeHtml(name)}" ${name === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
    }).join("");
  }

  function backendReasoningEffortSelectOptions(backend, model, current, context = {}) {
    if (typeof context.reasoningEffortSelectOptions === "function") {
      return context.reasoningEffortSelectOptions(backend, model, current);
    }
    const selected = configString(current);
    return ["", "low", "medium", "high", "xhigh", "max"].map(value => {
      const label = value || "default";
      return `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
    }).join("");
  }

  function officialOptions(backend, context = {}) {
    const backendModels = context.backendModels instanceof Map ? context.backendModels : new Map();
    return (backendModels.get(backend) || []).map(model => ({
      name: configString(model.name),
      label: configString(model.label, model.name || "default")
    }));
  }

  function bootstrapFormModelOptions(form, backend, context = {}) {
    const envOptions = bootstrapConfigEnvGroups(form, backend, context)
      .map((group, index) => {
        const name = bootstrapEnvGroupName(group, index);
        const model = configString(group[envGroupModelKey(backend)], "not configured");
        return {
          name: envModelValue(name),
          label: `${model} (${name})`
        };
      })
      .filter(option => option.name);
    return [...officialOptions(backend, context), ...envOptions];
  }

  function bootstrapFormModelSelectOptions(form, backend, current, context = {}) {
    const models = selectableBootstrapModelOptions(bootstrapFormModelOptions(form, backend, context));
    const selected = selectedBootstrapModelValue(models, current);
    return models.map(model => {
      const name = configString(model.name);
      const label = configString(model.label, name || "default");
      return `<option value="${escapeHtml(name)}" ${name === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
    }).join("");
  }

  function bootstrapConfigRowHtml(kind, data = {}, index = 0, options = {}) {
    if (kind === "workspace_roots") {
      return `
        <div class="bootstrap-list-row" data-bootstrap-row="workspace_roots">
          <input data-bootstrap-root-value placeholder="/path/to/projects" value="${escapeHtml(configString(data.value))}">
          <button class="bootstrap-icon-button" type="button" data-bootstrap-remove-row title="Remove">x</button>
        </div>
      `;
    }
    if (kind === "codex.env" || kind === "claude.env") {
      const backend = kind.split(".", 1)[0] || "claude";
      const fields = envGroupFieldsForBackend(backend);
      const baseUrlKey = fields[0];
      const modelKey = envGroupModelKey(backend);
      const secretKeys = envGroupSecretKeys(backend);
      const name = configString(data.name);
      const namePlaceholder = index === 0 ? "default" : `env-${index + 1}`;
      const secretInput = (key, label) => {
        const value = options.maskSecrets ? "" : configString(data[key]);
        const placeholder = options.maskSecrets && configString(data[key]) ? "Configured; leave blank to keep" : "";
        return `
            <label class="field-label">
              <span>${escapeHtml(label)}</span>
              <input data-bootstrap-env-field="${escapeHtml(key)}" type="password" placeholder="${escapeHtml(placeholder)}" value="${escapeHtml(value)}">
            </label>`;
      };
      const claudeAuthToken = configString(data.ANTHROPIC_AUTH_TOKEN);
      const claudeApiKey = configString(data.ANTHROPIC_API_KEY);
      const credentialFields = backend === "codex" ? secretInput(secretKeys[0], "API key") : `
            <label class="field-label">
              <span>Credential</span>
              <input data-bootstrap-claude-credential type="password"
                placeholder="${escapeHtml(options.maskSecrets && claudeAuthToken ? "Configured; leave blank to keep" : "")}"
                value="${escapeHtml(options.maskSecrets ? "" : claudeAuthToken)}">
            </label>`;
      const codexExtraFields = backend === "codex" ? `
            <label class="field-label">
              <span>Wire API</span>
              <input data-bootstrap-env-field="CODEX_WIRE_API" placeholder="responses" value="${escapeHtml(configString(data.CODEX_WIRE_API, "responses"))}">
            </label>
            <label class="field-label">
              <span>Key env</span>
              <input data-bootstrap-env-field="CODEX_ENV_KEY" placeholder="OPENAI_API_KEY" value="${escapeHtml(configString(data.CODEX_ENV_KEY, "OPENAI_API_KEY"))}">
            </label>
      ` : "";
      const contextWindow = configString(data.CLAUDE_CODE_MAX_CONTEXT_TOKENS);
      const knownContextWindows = ["", "200000", "256000", "1000000"];
      const customContextOption = backend === "claude" && contextWindow && !knownContextWindows.includes(contextWindow)
        ? `<option value="${escapeHtml(contextWindow)}" selected>Custom (${escapeHtml(contextWindow)})</option>`
        : "";
      const claudeExtraFields = backend === "claude" ? `
            <label class="field-label">
              <span>Context window</span>
              <select data-bootstrap-env-field="CLAUDE_CODE_MAX_CONTEXT_TOKENS">
                <option value="" ${contextWindow ? "" : "selected"}>Auto-detect</option>
                <option value="200000" ${contextWindow === "200000" ? "selected" : ""}>200K</option>
                <option value="256000" ${contextWindow === "256000" ? "selected" : ""}>256K</option>
                <option value="1000000" ${contextWindow === "1000000" ? "selected" : ""}>1M</option>
                ${customContextOption}
              </select>
            </label>
            <details class="bootstrap-env-advanced">
              <summary>Advanced (optional)</summary>
              <div class="bootstrap-env-fields">
                ${backend === "codex" ? "" : secretInput("ANTHROPIC_API_KEY", "API key (x-api-key)")}
                <div class="field-help">Leave blank to use the Bearer credential above. Set only for x-api-key gateways.</div>
                <label class="field-label">
                  <span>Fable</span>
                  <input data-bootstrap-env-field="ANTHROPIC_DEFAULT_FABLE_MODEL" placeholder="use primary model" value="${escapeHtml(configString(data.ANTHROPIC_DEFAULT_FABLE_MODEL))}">
                </label>
                <label class="field-label">
                  <span>Opus</span>
                  <input data-bootstrap-env-field="ANTHROPIC_DEFAULT_OPUS_MODEL" placeholder="use primary model" value="${escapeHtml(configString(data.ANTHROPIC_DEFAULT_OPUS_MODEL))}">
                </label>
                <label class="field-label">
                  <span>Sonnet</span>
                  <input data-bootstrap-env-field="ANTHROPIC_DEFAULT_SONNET_MODEL" placeholder="use primary model" value="${escapeHtml(configString(data.ANTHROPIC_DEFAULT_SONNET_MODEL))}">
                </label>
                <label class="field-label">
                  <span>Haiku / fast</span>
                  <input data-bootstrap-env-field="ANTHROPIC_DEFAULT_HAIKU_MODEL" placeholder="use primary model" value="${escapeHtml(configString(data.ANTHROPIC_DEFAULT_HAIKU_MODEL))}">
                </label>
              </div>
              <div class="field-help">Leave all blank to use the primary model for every Claude Code role.</div>
            </details>
      ` : "";
      return `
        <div class="bootstrap-env-group" data-bootstrap-row="${escapeHtml(kind)}">
          <div class="bootstrap-env-group-head">
            <strong>${backend === "codex" ? "Provider group" : "Gateway group"}</strong>
            <button class="bootstrap-icon-button" type="button" data-bootstrap-remove-row title="Remove">x</button>
          </div>
          <div class="bootstrap-env-fields">
            <label class="field-label">
              <span>Name</span>
              <input data-bootstrap-env-name placeholder="${escapeHtml(namePlaceholder)}" value="${escapeHtml(name)}">
            </label>
            <label class="field-label">
              <span>Base URL</span>
              <input data-bootstrap-env-field="${escapeHtml(baseUrlKey)}" placeholder="${backend === "codex" ? "https://api.example.com/v1" : "https://api.anthropic.com"}" value="${escapeHtml(configString(data[baseUrlKey]))}">
            </label>
            <label class="field-label">
              <span>Model</span>
              <input data-bootstrap-env-field="${escapeHtml(modelKey)}" placeholder="${backend === "codex" ? "model-name" : "claude-sonnet-4-5"}" value="${escapeHtml(configString(data[modelKey]))}">
            </label>
            ${credentialFields}
            ${codexExtraFields}
            ${claudeExtraFields}
          </div>
        </div>
      `;
    }
    return "";
  }

  function bootstrapConfigFormHtml(options = {}) {
    const mode = configString(options.mode, "init");
    const submitLabel = configString(options.submitLabel, mode === "settings" ? "Save Settings" : "Save AHA Config");
    const cfg = options.config || {};
    const codex = cfg.codex || {};
    const claude = cfg.claude || {};
    const proxy = cfg.proxy || {};
    const codexProxy = codex.proxy || {};
    const claudeProxy = claude.proxy || {};
    const backend = backendOptions.includes(configString(cfg.backend)) ? configString(cfg.backend) : "codex";
    const maskSecrets = mode === "settings";
    const codexDetailsOpen = mode === "settings" ? "" : " open";
    const codexModel = selectedBackendModel("codex", codex.model || envModelValue(codex.env_active), options);
    const claudeModel = selectedBackendModel("claude", claude.model || envModelValue(claude.env_active), options);
    return `
      <form class="bootstrap-form" data-bootstrap-config-form data-bootstrap-config-mode="${escapeHtml(mode)}">
        <details class="bootstrap-config-section" open>
          <summary>Core Settings</summary>
          <div class="bootstrap-config-stack">
            <label class="field-label">
              <span>Default backend</span>
              <select data-bootstrap-config-field="backend">${selectOptions(backendOptions, backend)}</select>
              <div class="field-help">Backend used for new tasks when none is specified.</div>
            </label>
            <label class="field-label">
              <span>Task concurrency</span>
              <input data-bootstrap-config-field="default_parallel" type="number" min="1" step="1" value="${escapeHtml(configString(cfg.default_parallel, "10"))}">
              <div class="field-help">Default number of tasks AHA may run in parallel.</div>
            </label>
          </div>
        </details>
        <details class="bootstrap-config-section" open>
          <summary>Proxy Settings</summary>
          <div class="bootstrap-config-grid">
            ${bootstrapSharedProxyFieldsHtml(proxy)}
          </div>
          <div class="field-help">Shared by Codex, Claude, Web upgrades, and other AHA network operations.</div>
        </details>
        <details class="bootstrap-config-section" open>
          <summary>Workspaces</summary>
          <label class="field-label">
            <span>Workspace roots</span>
            <div class="bootstrap-config-list" data-bootstrap-config-list="workspace_roots">
              ${bootstrapRootRows(cfg.workspace_roots)}
              <button class="bootstrap-add-row" type="button" data-bootstrap-add-row="workspace_roots">Add root</button>
            </div>
            <div class="field-help">Project roots used for dashboard workspace discovery.</div>
          </label>
          <label class="field-label">
            <span>Webgame workspace</span>
            <input data-bootstrap-config-field="webgame_workspace" value="${escapeHtml(configString(cfg.webgame_workspace))}">
            <div class="field-help">Optional workspace for web game static assets.</div>
          </label>
        </details>
        <details class="bootstrap-config-section"${codexDetailsOpen}>
          <summary>Codex defaults</summary>
          <div class="bootstrap-config-grid">
            <label class="field-label">
              <span>Bin</span>
              <input data-bootstrap-config-field="codex.bin" value="${escapeHtml(configString(codex.bin, "codex"))}">
              <div class="field-help">Codex CLI executable name or path.</div>
            </label>
            <label class="field-label">
              <span>Model</span>
              <select data-bootstrap-config-field="codex.model">${backendModelSelectOptions("codex", codexModel, options)}</select>
              <div class="field-help">Official Codex model or custom OpenAI-compatible provider.</div>
            </label>
            <label class="field-label">
              <span>Reasoning effort</span>
              <select data-bootstrap-config-field="codex.reasoning_effort">${backendReasoningEffortSelectOptions("codex", codexModel, codex.reasoning_effort, options)}</select>
              <div class="field-help">Default Codex thinking depth for tasks and distill jobs.</div>
            </label>
          </div>
          <div class="bootstrap-config-grid">
            ${bootstrapBackendProxySwitchHtml("codex", codexProxy)}
          </div>
          <label class="field-label">
            <span>Provider groups</span>
            ${bootstrapEnvDetectHtml("codex")}
            <div class="bootstrap-config-list" data-bootstrap-config-list="codex.env">
              ${bootstrapEnvRows(codex.env, codex.env_active, { maskSecrets }, "codex")}
              <button class="bootstrap-add-row" type="button" data-bootstrap-add-row="codex.env">Add provider group</button>
            </div>
            <div class="field-help">Each group becomes a custom Codex model option using Codex provider override.</div>
          </label>
        </details>
        <details class="bootstrap-config-section">
          <summary>Claude defaults</summary>
          <div class="bootstrap-config-grid">
            <label class="field-label">
              <span>Bin</span>
              <input data-bootstrap-config-field="claude.bin" value="${escapeHtml(configString(claude.bin, "claude"))}">
              <div class="field-help">Claude CLI executable name or path.</div>
            </label>
            <label class="field-label">
              <span>Model</span>
              <select data-bootstrap-config-field="claude.model">${backendModelSelectOptions("claude", claudeModel, options)}</select>
              <div class="field-help">Official Claude model or custom gateway group.</div>
            </label>
            <label class="field-label">
              <span>Reasoning effort</span>
              <select data-bootstrap-config-field="claude.reasoning_effort">${backendReasoningEffortSelectOptions("claude", claudeModel, claude.reasoning_effort, options)}</select>
              <div class="field-help">Default Claude effort for tasks and distill jobs.</div>
            </label>
          </div>
          <div class="bootstrap-config-grid">
            ${bootstrapBackendProxySwitchHtml("claude", claudeProxy)}
          </div>
          <label class="field-label">
            <span>Gateway groups</span>
            ${bootstrapEnvDetectHtml("claude")}
            <div class="bootstrap-config-list" data-bootstrap-config-list="claude.env">
              ${bootstrapEnvRows(claude.env, claude.env_active, { maskSecrets })}
              <button class="bootstrap-add-row" type="button" data-bootstrap-add-row="claude.env">Add gateway group</button>
            </div>
            <div class="field-help">Each group defines one gateway connection. AHA derives Claude Code role, timeout, discovery, traffic and compaction variables at launch.</div>
          </label>
        </details>
        <div class="bootstrap-form-actions">
          <button type="submit">${escapeHtml(submitLabel)}</button>
          <div data-bootstrap-config-state class="meta"></div>
        </div>
      </form>
    `;
  }

  function bootstrapConfigField(form, name) {
    return form.querySelector(`[data-bootstrap-config-field="${name}"]`);
  }

  function bootstrapConfigText(form, name) {
    return String(bootstrapConfigField(form, name)?.value || "").trim();
  }

  function bootstrapConfigRoots(form) {
    return [...form.querySelectorAll("[data-bootstrap-root-value]")]
      .map(input => String(input.value || ""))
      .map(item => item.trim())
      .filter(Boolean);
  }

  function bootstrapConfigMode(form) {
    return String(form?.dataset?.bootstrapConfigMode || "init");
  }

  function previousBootstrapEnvGroup(index, name, backend = "claude", config = {}) {
    const groups = bootstrapEnvGroups(config?.[backend]?.env, backend);
    const named = groups.find(group => configString(group.name) === name);
    return named || groups[index] || {};
  }

  function bootstrapConfigEnvGroups(form, backend = "claude", context = {}) {
    const preserveSecrets = bootstrapConfigMode(form) === "settings";
    const fields = envGroupFieldsForBackend(backend);
    const secretKeys = envGroupSecretKeys(backend);
    const config = context.config || {};
    return [...form.querySelectorAll(`[data-bootstrap-row='${backend}.env']`)]
      .map((row, index) => {
        const rawName = String(row.querySelector("[data-bootstrap-env-name]")?.value || "").trim();
        const group = {
          name: rawName || `env-${index + 1}`
        };
        for (const key of fields) {
          group[key] = String(row.querySelector(`[data-bootstrap-env-field="${key}"]`)?.value || "").trim();
        }
        let selectedSecretKeys = secretKeys;
        if (backend === "claude") {
          const credential = String(row.querySelector("[data-bootstrap-claude-credential]")?.value || "").trim();
          // The main Credential input maps to the Bearer credential (ANTHROPIC_AUTH_TOKEN);
          // the advanced x-api-key field is read by the fields loop above. AUTH_TOKEN wins
          // when both are present, matching runtime priority.
          group.ANTHROPIC_AUTH_TOKEN = credential;
          if (credential) group.ANTHROPIC_API_KEY = "";
          selectedSecretKeys = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"];
        }
        const hasNonSecretValue = Boolean(rawName || fields.some(key => !secretKeys.includes(key) && group[key]));
        const previous = previousBootstrapEnvGroup(index, group.name, backend, config);
        const enteredSecret = selectedSecretKeys.some(key => group[key]);
        if (preserveSecrets && hasNonSecretValue && !enteredSecret) {
          for (const secretKey of selectedSecretKeys) group[secretKey] = configString(previous[secretKey]);
        }
        return {
          group,
          hasValue: Boolean(hasNonSecretValue || secretKeys.some(key => group[key]))
        };
      })
      .filter(item => item.hasValue)
      .map(item => item.group);
  }

  function bootstrapConfigEnvGroupNames(form, backend = "claude", context = {}) {
    return bootstrapConfigEnvGroups(form, backend, context).map((group, index) => bootstrapEnvGroupName(group, index));
  }

  function detectEnvGroupFromModel(backend, baseUrl, credential, authStyle = "bearer", model = {}) {
    const modelId = typeof model === "string" ? model : String(model?.id || model?.name || "").trim();
    if (!modelId) return null;
    if (backend === "codex") {
      return {
        name: modelId,
        OPENAI_BASE_URL: baseUrl,
        OPENAI_MODEL: modelId,
        OPENAI_API_KEY: credential,
        CODEX_WIRE_API: "responses",
        CODEX_ENV_KEY: "OPENAI_API_KEY"
      };
    }
    const group = {
      name: modelId,
      ANTHROPIC_BASE_URL: baseUrl,
      ANTHROPIC_MODEL: modelId
    };
    if (authStyle === "x-api-key") {
      group.ANTHROPIC_API_KEY = credential;
      group.ANTHROPIC_AUTH_TOKEN = "";
    } else {
      group.ANTHROPIC_AUTH_TOKEN = credential;
      group.ANTHROPIC_API_KEY = "";
    }
    const windowValue = modelContextWindow(model);
    if (windowValue > 0) group.CLAUDE_CODE_MAX_CONTEXT_TOKENS = String(windowValue);
    return group;
  }

  function detectEnvGroupFromModels(backend, baseUrl, credential, authStyle = "bearer", models = []) {
    return models
      .map(model => detectEnvGroupFromModel(backend, baseUrl, credential, authStyle, model))
      .filter(Boolean);
  }

  function bootstrapConfigCodexModel(form) {
    return bootstrapConfigText(form, "codex.model");
  }

  function bootstrapConfigClaudeModel(form) {
    return bootstrapConfigText(form, "claude.model");
  }

  function bootstrapConfigCodexActiveEnvGroup(form, context = {}) {
    const selected = bootstrapConfigCodexModel(form);
    const name = envModelName(selected);
    return bootstrapConfigEnvGroupNames(form, "codex", context).includes(name) ? name : "";
  }

  function bootstrapConfigClaudeActiveEnvGroup(form, context = {}) {
    const selected = bootstrapConfigClaudeModel(form);
    const name = envModelName(selected);
    return bootstrapConfigEnvGroupNames(form, "claude", context).includes(name) ? name : "";
  }

  function syncBootstrapModelOptions(form, context = {}) {
    for (const backend of backendOptions) {
      const select = bootstrapConfigField(form, `${backend}.model`);
      if (!select) continue;
      const previous = String(select.value || "");
      select.innerHTML = bootstrapFormModelSelectOptions(form, backend, previous, context);
      if ([...select.options].some(item => item.value === previous)) select.value = previous;
      const effortSelect = bootstrapConfigField(form, `${backend}.reasoning_effort`);
      if (!effortSelect) continue;
      const previousEffort = String(effortSelect.value || "");
      effortSelect.innerHTML = backendReasoningEffortSelectOptions(backend, select.value || "", previousEffort, context);
      if ([...effortSelect.options].some(item => item.value === previousEffort)) effortSelect.value = previousEffort;
    }
  }

  function addBootstrapConfigRow(button, context = {}) {
    const kind = button?.dataset?.bootstrapAddRow || "";
    if (!kind) return;
    const list = button.closest("[data-bootstrap-config-list]");
    const index = list ? list.querySelectorAll("[data-bootstrap-row]").length : 0;
    button.insertAdjacentHTML("beforebegin", bootstrapConfigRowHtml(kind, {}, index));
    syncBootstrapModelOptions(button.closest("[data-bootstrap-config-form]"), context);
  }

  function removeBootstrapConfigRow(button, context = {}) {
    const row = button.closest("[data-bootstrap-row]");
    const list = row?.closest("[data-bootstrap-config-list]");
    if (!row || !list) return;
    const rows = [...list.querySelectorAll("[data-bootstrap-row]")];
    if (rows.length <= 1) {
      row.querySelectorAll("input, select").forEach(input => {
        input.value = "";
      });
      syncBootstrapModelOptions(list.closest("[data-bootstrap-config-form]"), context);
      return;
    }
    row.remove();
    syncBootstrapModelOptions(list.closest("[data-bootstrap-config-form]"), context);
  }

  function bootstrapConfigPayload(form, context = {}) {
    const config = context.config || {};
    const body = {
      backend: bootstrapConfigText(form, "backend") || "codex",
      default_parallel: Number(bootstrapConfigText(form, "default_parallel") || 10),
      workspace_roots: bootstrapConfigRoots(form),
      webgame_workspace: bootstrapConfigText(form, "webgame_workspace"),
      proxy: {
        http_proxy: bootstrapConfigText(form, "proxy.http_proxy"),
        https_proxy: bootstrapConfigText(form, "proxy.https_proxy"),
        no_proxy: bootstrapConfigText(form, "proxy.no_proxy")
      },
      retention_policy: config.retention_policy || {},
      codex: {
        bin: bootstrapConfigText(form, "codex.bin") || "codex",
        model: bootstrapConfigCodexModel(form),
        reasoning_effort: bootstrapConfigText(form, "codex.reasoning_effort"),
        sandbox: "auto",
        approval: "never",
        json: true,
        session_policy: "sticky",
        env_active: bootstrapConfigCodexActiveEnvGroup(form, context),
        env: bootstrapConfigEnvGroups(form, "codex", context),
        proxy: {
          enabled: Boolean(bootstrapConfigField(form, "codex.proxy.enabled")?.checked)
        }
      },
      claude: {
        bin: bootstrapConfigText(form, "claude.bin") || "claude",
        model: bootstrapConfigClaudeModel(form),
        reasoning_effort: bootstrapConfigText(form, "claude.reasoning_effort"),
        sandbox: "auto",
        permission_mode: "",
        session_policy: "sticky",
        env_active: bootstrapConfigClaudeActiveEnvGroup(form, context),
        env: bootstrapConfigEnvGroups(form, "claude", context),
        proxy: {
          enabled: Boolean(bootstrapConfigField(form, "claude.proxy.enabled")?.checked)
        }
      },
      integrations: config.integrations || {}
    };
    body.integrations = {
      ...body.integrations,
      weixin: { enabled: false, visible: false }
    };
    if (bootstrapConfigMode(form) === "settings") body.force = true;
    return body;
  }

  window.AHABootstrapConfig = Object.freeze({
    codexEnvGroupFields,
    claudeEnvModelPrefix,
    claudeEnvGroupFields,
    configString,
    configListValues,
    bootstrapBackendOptions,
    bootstrapEnvGroups,
    bootstrapEnvGroupName,
    bootstrapEnvDetectHtml,
    bootstrapConfigFormHtml,
    bootstrapConfigMode,
    bootstrapConfigPayload,
    detectEnvGroupFromModel,
    detectEnvGroupFromModels,
    detectModelsModalHtml,
    formatContextWindow,
    modelContextWindow,
    syncBootstrapModelOptions,
    fillBootstrapProxyDefaultFor,
    syncBootstrapProxyDefaultsForInput,
    addBootstrapConfigRow,
    insertDetectedEnvGroups,
    removeBootstrapConfigRow
  });
}());
