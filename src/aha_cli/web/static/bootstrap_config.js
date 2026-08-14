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

  function fuzzyModelMatch(value, query) {
    const target = String(value || "").trim().toLowerCase();
    const needle = String(query || "").trim().toLowerCase();
    if (!needle) return true;
    if (target.includes(needle)) return true;
    const compactTarget = target.replace(/[^a-z0-9]+/g, "");
    const compactNeedle = needle.replace(/[^a-z0-9]+/g, "");
    if (compactNeedle && compactTarget.includes(compactNeedle)) return true;
    const terms = needle.split(/\s+/).filter(Boolean);
    return terms.every(term => {
      const compactTerm = term.replace(/[^a-z0-9]+/g, "");
      if (!compactTerm) return target.includes(term);
      let position = 0;
      for (const character of compactTarget) {
        if (character === compactTerm[position]) position += 1;
        if (position === compactTerm.length) return true;
      }
      return false;
    });
  }

  function detectModelsModalHtml(models = [], context = {}) {
    const providerName = configString(context?.provider_name || context?.provider?.name);
    const authStyle = configString(context?.auth_style, "auto");
    const rows = models.map((model, index) => {
      const id = typeof model === "string" ? model : String(model?.id || "").trim();
      const windowValue = modelContextWindow(model);
      const metaLabel = `${formatContextWindow(windowValue)}.${formatContextWindow(model?.max_output_tokens)}.${String(model?.mode || "NA")}`;
      return `
        <div class="bootstrap-detect-model-row" data-bootstrap-detect-model-row data-model-id="${escapeHtml(id)}" data-model-index="${index}" data-search-text="${escapeHtml(`${id} ${model?.mode || ""}`)}">
          <div class="bootstrap-detect-model-summary">
            <label class="bootstrap-detect-model-check"><input type="checkbox" data-bootstrap-detect-check><span>${escapeHtml(id)}</span></label>
            <span class="bootstrap-detect-model-meta" title="context.output.mode">${escapeHtml(metaLabel)}</span>
            <button class="bootstrap-icon-button bootstrap-detect-model-test-single" type="button" data-bootstrap-detect-model-test title="Test this model">&#9889;</button>
          </div>
          <div class="bootstrap-detect-model-details">
            <div>
              <span class="bootstrap-detect-section-label">Interface capability</span>
              <div class="bootstrap-capability-grid" data-bootstrap-capabilities>
                <span data-wire-api="responses">Responses <em data-capability-status>Untested</em></span>
                <span data-wire-api="chat_completions">Chat Completions <em data-capability-status>Untested</em></span>
                <span data-wire-api="anthropic_messages">Messages <em data-capability-status>Untested</em></span>
              </div>
            </div>
            <div>
              <span class="bootstrap-detect-section-label">Backend binding</span>
              <div class="bootstrap-binding-options">
                <label><input type="checkbox" data-bootstrap-bind-backend="codex" data-wire-api="responses" disabled> Codex · Responses</label>
                <label><input type="checkbox" data-bootstrap-bind-backend="claude" data-wire-api="chat_completions" disabled> Claude · Chat</label>
                <label><input type="checkbox" data-bootstrap-bind-backend="claude" data-wire-api="anthropic_messages" disabled> Claude · Messages</label>
              </div>
            </div>
          </div>
        </div>`;
    }).join("");
    return `
      <div class="bootstrap-detect-modal-backdrop" data-bootstrap-detect-modal>
        <div class="bootstrap-detect-modal" role="dialog" aria-label="Detected models">
          <div class="bootstrap-detect-modal-head">
            <div><strong>Detected Models</strong><div class="field-help">${escapeHtml(providerName || "Saved provider")} · Authentication: ${escapeHtml(authStyle)}</div></div>
            <button class="bootstrap-icon-button" type="button" data-bootstrap-detect-close title="Close">x</button>
          </div>
          <div class="bootstrap-detect-toolbar">
            <label class="field-label"><span>Search</span><input data-bootstrap-detect-filter type="search" placeholder="Fuzzy search model names..." autocomplete="off"></label>
            <span class="field-help" data-bootstrap-detect-filter-count>${models.length} models</span>
          </div>
          <div class="bootstrap-detect-model-list" data-bootstrap-detect-list>${rows}</div>
          <div class="field-help bootstrap-detect-empty" data-bootstrap-detect-empty hidden>No matching models.</div>
          <div class="bootstrap-detect-modal-foot">
            <button class="bootstrap-add-row" type="button" data-bootstrap-detect-test data-bootstrap-detect-test-selected>Test selected</button>
            <button class="bootstrap-add-row" type="button" data-bootstrap-detect-add-selected>Add selected bindings (0)</button>
            <button class="bootstrap-add-row" type="button" data-bootstrap-detect-close>Cancel</button>
          </div>
        </div>
      </div>`;
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

  function providerList(value) {
    return Array.isArray(value) ? value.filter(item => item && typeof item === "object") : [];
  }

  function configuredModelList(value) {
    return Array.isArray(value) ? value.filter(item => item && typeof item === "object") : [];
  }

  function providerRowHtml(provider = {}, index = 0, maskSecrets = false) {
    const id = configString(provider.id);
    const hasCredential = Boolean(provider.credential_configured || provider.api_key_configured || provider.api_key);
    return `
      <div class="bootstrap-provider-row" data-bootstrap-provider-row data-provider-id="${escapeHtml(id)}">
        <div class="bootstrap-provider-row-head">
          <strong>Provider</strong>
          <button class="bootstrap-icon-button" type="button" data-bootstrap-remove-provider title="Remove">x</button>
        </div>
        <div class="bootstrap-env-fields">
          <input type="hidden" data-bootstrap-provider-field="id" value="${escapeHtml(id)}">
          <label class="field-label"><span>Name</span><input data-bootstrap-provider-field="name" placeholder="OpenRouter" value="${escapeHtml(configString(provider.name))}"></label>
          <label class="field-label"><span>Base URL</span><input data-bootstrap-provider-field="base_url" placeholder="https://api.example.com/v1" value="${escapeHtml(configString(provider.base_url))}"></label>
          <label class="field-label"><span>Anthropic Base URL (optional)</span><input data-bootstrap-provider-field="anthropic_base_url" placeholder="https://api.example.com/anthropic" value="${escapeHtml(configString(provider.anthropic_base_url))}"><div class="field-help">Used by the Claude backend for Anthropic Messages. Auto-filled when detection finds it under /anthropic.</div></label>
          <label class="field-label"><span>API key</span><input data-bootstrap-provider-field="api_key" type="password" autocomplete="off" placeholder="${escapeHtml(maskSecrets && hasCredential ? "Configured; leave blank to keep" : "sk-...")}" value="${escapeHtml(maskSecrets ? "" : configString(provider.api_key))}"></label>
          <label class="field-label"><span>Authentication</span><select data-bootstrap-provider-field="auth_style">
            <option value="auto" ${configString(provider.auth_style, "auto") === "auto" ? "selected" : ""}>Auto detect</option>
            <option value="bearer" ${provider.auth_style === "bearer" ? "selected" : ""}>Bearer</option>
            <option value="x-api-key" ${provider.auth_style === "x-api-key" ? "selected" : ""}>x-api-key</option>
            <option value="none" ${provider.auth_style === "none" ? "selected" : ""}>None</option>
          </select><div class="field-help">Auto detects the authentication header when models are fetched.</div></label>
        </div>
      </div>`;
  }

  function providerOptionsHtml(providers, selected = "") {
    const rows = providerList(providers);
    if (!rows.length) return '<option value="">Save a provider first</option>';
    const selectedId = configString(selected);
    return rows.map(provider => {
      const id = configString(provider.id);
      return `<option value="${escapeHtml(id)}" ${id === selectedId ? "selected" : ""}>${escapeHtml(configString(provider.name, provider.id))}</option>`;
    }).join("");
  }

  function configuredModelRowHtml(binding, providerName = "") {
    const modelId = configString(binding.model_id || binding.model).trim();
    // A binding whose model_id is empty or whitespace (e.g. an Add Model left the
    // ID blank or an edit cleared it) must still render visibly so the user can
    // identify and edit it. Show a placeholder name; the hidden field keeps the
    // real (trimmed) value so saving won't persist the placeholder as a model id.
    const displayModelId = modelId || "(unnamed)";
    const contextWindow = Number(binding.context_window) > 0 ? Number(binding.context_window) : 0;
    const contextBadge = contextWindow > 0
      ? `<span class="bootstrap-model-binding-ctx" title="Context window">ctx ${escapeHtml(formatContextWindow(contextWindow))}</span>`
      : "";
    return `
      <div class="bootstrap-model-binding" data-bootstrap-model-binding>
        <input type="hidden" data-bootstrap-binding-field="provider_id" value="${escapeHtml(configString(binding.provider_id))}">
        <input type="hidden" data-bootstrap-binding-field="model" value="${escapeHtml(modelId)}">
        <input type="hidden" data-bootstrap-binding-field="backend" value="${escapeHtml(configString(binding.backend))}">
        <input type="hidden" data-bootstrap-binding-field="wire_api" value="${escapeHtml(configString(binding.wire_api))}">
        <input type="hidden" data-bootstrap-binding-field="context_window" value="${escapeHtml(contextWindow ? String(contextWindow) : "")}">
        <input type="hidden" data-bootstrap-binding-field="fable_model" value="${escapeHtml(configString(binding.fable_model))}">
        <input type="hidden" data-bootstrap-binding-field="opus_model" value="${escapeHtml(configString(binding.opus_model))}">
        <input type="hidden" data-bootstrap-binding-field="sonnet_model" value="${escapeHtml(configString(binding.sonnet_model))}">
        <input type="hidden" data-bootstrap-binding-field="haiku_model" value="${escapeHtml(configString(binding.haiku_model))}">
        <div><strong>${escapeHtml(displayModelId)}</strong>${contextBadge}<div class="field-help">${escapeHtml(providerName || binding.provider_id)} · ${escapeHtml(configString(binding.wire_api).replaceAll("_", " "))}</div></div>
        <div class="bootstrap-model-binding-actions">
          <button class="bootstrap-icon-button" type="button" data-bootstrap-edit-binding title="Edit model">&#9998;</button>
          <button class="bootstrap-icon-button" type="button" data-bootstrap-remove-binding title="Remove">x</button>
        </div>
      </div>`;
  }

  function configuredModelsHtml(models, providers) {
    const names = new Map(providerList(providers).map(provider => [configString(provider.id), configString(provider.name, provider.id)]));
    const rows = configuredModelList(models);
    if (!rows.length) return '<div class="field-help" data-bootstrap-configured-empty>No provider models configured yet.</div>';
    const backendLabels = { codex: "Codex", claude: "Claude" };
    return backendOptions.map(backend => {
      const backendRows = rows
        .filter(binding => configString(binding.backend).toLowerCase() === backend)
        .sort((left, right) => configString(left.model_id || left.model).localeCompare(configString(right.model_id || right.model)));
      if (!backendRows.length) return "";
      return `
        <section class="bootstrap-model-backend-group" data-bootstrap-model-backend-group="${backend}">
          <div class="bootstrap-model-backend-head"><strong>${backendLabels[backend]}</strong><span data-bootstrap-model-backend-count>${backendRows.length} model${backendRows.length === 1 ? "" : "s"}</span></div>
          <div class="bootstrap-model-backend-items" data-bootstrap-model-backend-items>
            ${backendRows.map(binding => configuredModelRowHtml(binding, names.get(configString(binding.provider_id)))).join("")}
          </div>
        </section>`;
    }).join("");
  }

  function configuredModelEditorHtml(binding = {}, providers = []) {
    binding = binding || {};
    const providerId = configString(binding.provider_id);
    const backend = configString(binding.backend, "claude");
    const wireApi = configString(binding.wire_api, "anthropic_messages");
    const roleLabels = { fable_model: "Fable", opus_model: "Opus", sonnet_model: "Sonnet", haiku_model: "Haiku / fast" };
    const roleRows = Object.keys(roleLabels).map(key => `
      <label class="field-label"><span>${escapeHtml(roleLabels[key])}</span><input data-bootstrap-model-field="${escapeHtml(key)}" placeholder="use primary model" value="${escapeHtml(configString(binding[key]))}"></label>
    `).join("");
    return `
      <div class="bootstrap-detect-modal-backdrop" data-bootstrap-model-editor>
        <div class="bootstrap-detect-modal" role="dialog" aria-label="Configured model">
          <div class="bootstrap-detect-modal-head">
            <div><strong>${binding.model_id ? "Edit Model" : "Add Model"}</strong><div class="field-help">Base URL and credential come from the selected Provider.</div></div>
            <button class="bootstrap-icon-button" type="button" data-bootstrap-model-editor-close title="Close">x</button>
          </div>
          <div class="bootstrap-env-fields">
            <label class="field-label"><span>Provider</span><select data-bootstrap-model-field="provider_id">${providerOptionsHtml(providers, providerId)}</select></label>
            <label class="field-label"><span>Model ID</span><input data-bootstrap-model-field="model_id" placeholder="model-name" value="${escapeHtml(configString(binding.model_id))}"></label>
            <label class="field-label"><span>Backend</span>
              <select data-bootstrap-model-field="backend">
                <option value="codex" ${backend === "codex" ? "selected" : ""}>Codex</option>
                <option value="claude" ${backend === "claude" ? "selected" : ""}>Claude</option>
              </select>
            </label>
            <label class="field-label"><span>Wire API</span>
              <select data-bootstrap-model-field="wire_api">
                <option value="responses" ${wireApi === "responses" ? "selected" : ""}>Responses</option>
                <option value="chat_completions" ${wireApi === "chat_completions" ? "selected" : ""}>Chat Completions</option>
                <option value="anthropic_messages" ${wireApi === "anthropic_messages" ? "selected" : ""}>Messages</option>
              </select>
            </label>
            <label class="field-label"><span>Context window</span><input data-bootstrap-model-field="context_window" type="number" min="0" placeholder="200000" value="${escapeHtml(configString(binding.context_window))}"></label>
          </div>
          <details class="bootstrap-env-advanced">
            <summary>Claude role models (optional)</summary>
            <div class="bootstrap-env-fields">${roleRows}<div class="field-help">Role overrides for Claude Code; leave blank to use the primary model.</div></div>
          </details>
          <div class="bootstrap-detect-modal-foot">
            <button class="bootstrap-add-row" type="button" data-bootstrap-model-editor-save>Save</button>
            <button class="bootstrap-add-row" type="button" data-bootstrap-model-editor-close>Cancel</button>
          </div>
        </div>
      </div>`;
  }

  function readModelEditorFields(rootEl) {
    const value = key => String(rootEl?.querySelector?.(`[data-bootstrap-model-field="${key}"]`)?.value || "").trim();
    const binding = {
      provider_id: value("provider_id"),
      model_id: value("model_id"),
      backend: value("backend") || "claude",
      wire_api: value("wire_api") || "anthropic_messages"
    };
    const contextWindow = Number(value("context_window"));
    if (Number.isFinite(contextWindow) && contextWindow > 0) binding.context_window = contextWindow;
    for (const key of ["fable_model", "opus_model", "sonnet_model", "haiku_model"]) {
      const text = value(key);
      if (text) binding[key] = text;
    }
    return binding;
  }

  function refreshConfiguredModelGroups(list) {
    if (!list) return;
    list.querySelector("[data-bootstrap-configured-empty]")?.remove();
    for (const group of list.querySelectorAll("[data-bootstrap-model-backend-group]")) {
      const items = group.querySelector("[data-bootstrap-model-backend-items]");
      const rows = [...(items?.querySelectorAll("[data-bootstrap-model-binding]") || [])];
      rows.sort((left, right) => String(left.querySelector('[data-bootstrap-binding-field="model"]')?.value || "").localeCompare(String(right.querySelector('[data-bootstrap-binding-field="model"]')?.value || "")));
      rows.forEach(row => items?.appendChild(row));
      if (!rows.length) {
        group.remove();
        continue;
      }
      const count = group.querySelector("[data-bootstrap-model-backend-count]");
      if (count) count.textContent = `${rows.length} model${rows.length === 1 ? "" : "s"}`;
    }
    backendOptions.forEach(backend => {
      const group = list.querySelector(`[data-bootstrap-model-backend-group="${backend}"]`);
      if (group) list.appendChild(group);
    });
    if (!list.querySelector("[data-bootstrap-model-binding]")) {
      list.insertAdjacentHTML("beforeend", '<div class="field-help" data-bootstrap-configured-empty>No provider models configured yet.</div>');
    }
  }

  function insertConfiguredModels(form, provider, bindings) {
    const list = form.querySelector("[data-bootstrap-binding-list]");
    if (!list) return 0;
    list.querySelector("[data-bootstrap-configured-empty]")?.remove();
    const existing = new Set([...list.querySelectorAll("[data-bootstrap-model-binding]")].map(row => {
      const value = key => String(row.querySelector(`[data-bootstrap-binding-field="${key}"]`)?.value || "").trim();
      return [value("provider_id"), value("model"), value("backend"), value("wire_api")].join("\0");
    }));
    let inserted = 0;
    for (const binding of configuredModelList(bindings)) {
      const item = { ...binding, provider_id: configString(binding.provider_id, provider?.id) };
      const key = [item.provider_id, item.model_id || item.model, item.backend, item.wire_api].join("\0");
      if (existing.has(key)) continue;
      const backend = configString(item.backend).toLowerCase();
      let group = list.querySelector(`[data-bootstrap-model-backend-group="${backend}"]`);
      if (!group) {
        const label = backend === "codex" ? "Codex" : "Claude";
        list.insertAdjacentHTML("beforeend", `<section class="bootstrap-model-backend-group" data-bootstrap-model-backend-group="${escapeHtml(backend)}"><div class="bootstrap-model-backend-head"><strong>${label}</strong><span data-bootstrap-model-backend-count>0 models</span></div><div class="bootstrap-model-backend-items" data-bootstrap-model-backend-items></div></section>`);
        group = list.querySelector(`[data-bootstrap-model-backend-group="${backend}"]`);
      }
      group?.querySelector("[data-bootstrap-model-backend-items]")?.insertAdjacentHTML("beforeend", configuredModelRowHtml(item, provider?.name));
      existing.add(key);
      inserted += 1;
    }
    refreshConfiguredModelGroups(list);
    return inserted;
  }

  function bootstrapEnvDetectHtml(providers) {
    return `
      <div class="bootstrap-env-detect" data-bootstrap-provider-detect>
        <div class="bootstrap-env-fields">
          <label class="field-label"><span>Provider</span><select data-bootstrap-detect-provider>${providerOptionsHtml(providers)}</select></label>
          <button class="bootstrap-add-row" type="button" data-bootstrap-detect-models>Fetch models</button>
          <div class="field-help" data-bootstrap-detect-status>Select a saved Provider. Interface tests send minimal inference requests and may incur a small charge.</div>
        </div>
      </div>`;
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

  function bootstrapModelFilterValue(form, backend) {
    const value = String(form?.querySelector?.(`[data-bootstrap-config-field="${backend}.model_source"]`)?.value || "both").trim();
    return value === "official" || value === "env" ? value : "both";
  }

  function filterBootstrapModelOptions(options, mode, selected = "") {
    const list = Array.isArray(options) ? options : [];
    let filtered = list;
    if (mode === "official") {
      filtered = list.filter(option => !configString(option?.name).startsWith(claudeEnvModelPrefix));
    } else if (mode === "env") {
      filtered = list.filter(option => configString(option?.name).startsWith(claudeEnvModelPrefix));
    }
    const selectedValue = configString(selected);
    if (selectedValue && !filtered.some(option => configString(option?.name) === selectedValue)) {
      const selectedOption = list.find(option => configString(option?.name) === selectedValue);
      if (selectedOption) filtered = [...filtered, selectedOption];
    }
    return filtered;
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
    const formGroups = bootstrapConfigEnvGroups(form, backend, context);
    const envGroups = formGroups.length ? formGroups : bootstrapEnvGroups(context.config?.[backend]?.env, backend);
    const envOptions = envGroups
      .map((group, index) => {
        const name = bootstrapEnvGroupName(group, index);
        const model = configString(group[envGroupModelKey(backend)], "not configured");
        return {
          name: envModelValue(name),
          label: `${model} (${name})`
        };
      })
      .filter(option => option.name);
    return filterBootstrapModelOptions(
      [...officialOptions(backend, context), ...envOptions],
      bootstrapModelFilterValue(form, backend),
      bootstrapConfigText(form, `${backend}.model`)
    );
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
    const sectionOpen = mode === "settings" ? "" : " open";
    const codexModel = selectedBackendModel("codex", codex.model || envModelValue(codex.env_active), options);
    const claudeModel = selectedBackendModel("claude", claude.model || envModelValue(claude.env_active), options);
    return `
      <form class="bootstrap-form" data-bootstrap-config-form data-bootstrap-config-mode="${escapeHtml(mode)}">
        <details class="bootstrap-config-section"${sectionOpen}>
          <summary>Core</summary>
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
        <details class="bootstrap-config-section"${sectionOpen}>
          <summary>Proxy</summary>
          <div class="bootstrap-config-grid">
            ${bootstrapSharedProxyFieldsHtml(proxy)}
          </div>
          <div class="field-help">Shared by Codex, Claude, Web upgrades, and other AHA network operations.</div>
        </details>
        <details class="bootstrap-config-section"${sectionOpen}>
          <summary>Workspace</summary>
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
        <details class="bootstrap-config-section bootstrap-backend-section"${sectionOpen}>
          <summary>Backend</summary>
          <div class="field-help bootstrap-section-intro">Configure the Codex and Claude runtimes, then connect models from API providers without changing the existing backend configuration format.</div>
          <div class="bootstrap-backend-runtime-grid">
            <section class="bootstrap-backend-card" data-bootstrap-backend-card="codex">
              <div class="bootstrap-backend-card-head">
                <div>
                  <strong>Codex</strong>
                  <div class="field-help">Codex CLI runtime and configured model.</div>
                </div>
                <span class="bootstrap-backend-kind">Responses</span>
              </div>
              <div class="bootstrap-config-grid">
                <label class="field-label">
                  <span>Bin</span>
                  <input data-bootstrap-config-field="codex.bin" value="${escapeHtml(configString(codex.bin, "codex"))}">
                  <div class="field-help">Codex CLI executable name or path.</div>
                </label>
                <label class="field-label">
                  <span>Model</span>
                  <select data-bootstrap-config-field="codex.model">${backendModelSelectOptions("codex", codexModel, options)}</select>
                  <div class="field-help">Official model or one of the provider connections below.</div>
                </label>
                <label class="field-label">
                  <span>Model source</span>
                  <select data-bootstrap-config-field="codex.model_source">
                    <option value="both"${configString(codex.model_source, "both") !== "official" && configString(codex.model_source, "both") !== "env" ? " selected" : ""}>BOTH</option>
                    <option value="official"${configString(codex.model_source, "both") === "official" ? " selected" : ""}>Official</option>
                    <option value="env"${configString(codex.model_source, "both") === "env" ? " selected" : ""}>ENV</option>
                  </select>
                  <div class="field-help">Model pickers for tasks, agents and KB distill show only these sources. Save to apply.</div>
                  ${configString(codex.model_source, "both") !== "official"
                    ? '<div class="field-help" data-bootstrap-model-source-hint="codex" data-i18n="settings.codex_model_source_hint">ENV 模型偏慢时可运行 <code>codex logout</code> 清理 ChatGPT 登录残留以提速。</div>'
                    : ""}
                </label>
                <label class="field-label">
                  <span>Reasoning effort</span>
                  <select data-bootstrap-config-field="codex.reasoning_effort">${backendReasoningEffortSelectOptions("codex", codexModel, codex.reasoning_effort, options)}</select>
                  <div class="field-help">Default Codex thinking depth for tasks and distill jobs.</div>
                </label>
                ${bootstrapBackendProxySwitchHtml("codex", codexProxy)}
              </div>
            </section>
            <section class="bootstrap-backend-card" data-bootstrap-backend-card="claude">
              <div class="bootstrap-backend-card-head">
                <div>
                  <strong>Claude</strong>
                  <div class="field-help">Claude Code CLI runtime and configured model.</div>
                </div>
                <span class="bootstrap-backend-kind">Messages</span>
              </div>
              <div class="bootstrap-config-grid">
                <label class="field-label">
                  <span>Bin</span>
                  <input data-bootstrap-config-field="claude.bin" value="${escapeHtml(configString(claude.bin, "claude"))}">
                  <div class="field-help">Claude CLI executable name or path.</div>
                </label>
                <label class="field-label">
                  <span>Model</span>
                  <select data-bootstrap-config-field="claude.model">${backendModelSelectOptions("claude", claudeModel, options)}</select>
                  <div class="field-help">Official model or one of the provider connections below.</div>
                </label>
                <label class="field-label">
                  <span>Model source</span>
                  <select data-bootstrap-config-field="claude.model_source">
                    <option value="both"${configString(claude.model_source, "both") !== "official" && configString(claude.model_source, "both") !== "env" ? " selected" : ""}>BOTH</option>
                    <option value="official"${configString(claude.model_source, "both") === "official" ? " selected" : ""}>Official</option>
                    <option value="env"${configString(claude.model_source, "both") === "env" ? " selected" : ""}>ENV</option>
                  </select>
                  <div class="field-help">Model pickers for tasks, agents and KB distill show only these sources. Save to apply.</div>
                </label>
                <label class="field-label">
                  <span>Reasoning effort</span>
                  <select data-bootstrap-config-field="claude.reasoning_effort">${backendReasoningEffortSelectOptions("claude", claudeModel, claude.reasoning_effort, options)}</select>
                  <div class="field-help">Default Claude effort for tasks and distill jobs.</div>
                </label>
                ${bootstrapBackendProxySwitchHtml("claude", claudeProxy)}
              </div>
            </section>
          </div>
          <div class="bootstrap-provider-heading">
            <div>
              <strong>Provider Connections</strong>
              <div class="field-help">Save each API endpoint and credential once, then reuse it for model discovery and multiple Backend bindings.</div>
            </div>
          </div>
          <div class="bootstrap-config-list bootstrap-provider-list" data-bootstrap-provider-list>
            ${(providerList(cfg.providers).length ? providerList(cfg.providers) : [{}]).map((provider, index) => providerRowHtml(provider, index, maskSecrets)).join("")}
            <button class="bootstrap-add-row" type="button" data-bootstrap-add-provider>Add Provider</button>
          </div>
          <div class="bootstrap-provider-heading">
            <div>
              <strong>Discover Models</strong>
              <div class="field-help">Fetch a saved Provider's models, test selected interface capabilities, then bind each model to one or more Backends.</div>
            </div>
          </div>
          ${bootstrapEnvDetectHtml(cfg.providers)}
          <div class="bootstrap-provider-heading">
            <div>
              <strong>Configured Models</strong>
              <div class="field-help">The same Provider model may be bound to both Codex and Claude with different wire APIs. Edit or add models manually when detection misses fields.</div>
            </div>
            <button class="bootstrap-add-row" type="button" data-bootstrap-add-binding>Add Model</button>
          </div>
          <div class="bootstrap-config-list bootstrap-model-binding-list" data-bootstrap-binding-list>
            ${configuredModelsHtml(cfg.configured_models, cfg.providers)}
          </div>
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

  function bootstrapProviders(form, context = {}) {
    const existing = new Map(providerList(context.config?.providers).map(provider => [configString(provider.id), provider]));
    return [...form.querySelectorAll("[data-bootstrap-provider-row]")].map((row, index) => {
      const value = key => String(row.querySelector(`[data-bootstrap-provider-field="${key}"]`)?.value || "").trim();
      const id = value("id");
      const provider = { id, name: value("name"), base_url: value("base_url"), anthropic_base_url: value("anthropic_base_url") || "", auth_style: value("auth_style") || "auto", credential: value("api_key") };
      if (!provider.credential && existing.get(id)?.credential_configured) provider.credential_configured = true;
      return provider;
    }).filter(provider => provider.name || provider.base_url);
  }

  function bootstrapConfiguredModels(form) {
    return [...form.querySelectorAll("[data-bootstrap-model-binding]")].map(row => {
      const value = key => String(row.querySelector(`[data-bootstrap-binding-field="${key}"]`)?.value || "").trim();
      const binding = { provider_id: value("provider_id"), model_id: value("model"), backend: value("backend"), wire_api: value("wire_api") };
      const contextWindow = Number(value("context_window"));
      if (Number.isFinite(contextWindow) && contextWindow > 0) binding.context_window = contextWindow;
      for (const key of ["fable_model", "opus_model", "sonnet_model", "haiku_model"]) {
        const text = value(key);
        if (text) binding[key] = text;
      }
      return binding;
    }).filter(binding => binding.provider_id && binding.model_id && binding.backend && binding.wire_api);
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
      providers: bootstrapProviders(form, context),
      configured_models: bootstrapConfiguredModels(form),
      codex: {
        bin: bootstrapConfigText(form, "codex.bin") || "codex",
        model: bootstrapConfigCodexModel(form),
        reasoning_effort: bootstrapConfigText(form, "codex.reasoning_effort"),
        sandbox: "auto",
        approval: "never",
        json: true,
        session_policy: "sticky",
        env_active: bootstrapConfigCodexActiveEnvGroup(form, context),
        model_source: bootstrapConfigText(form, "codex.model_source") || "both",
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
        model_source: bootstrapConfigText(form, "claude.model_source") || "both",
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
    providerList,
    configuredModelList,
    providerRowHtml,
    configuredModelsHtml,
    configuredModelRowHtml,
    insertConfiguredModels,
    bootstrapEnvDetectHtml,
    bootstrapConfigFormHtml,
    bootstrapConfigMode,
    bootstrapConfigPayload,
    bootstrapConfiguredModels,
    detectEnvGroupFromModel,
    detectEnvGroupFromModels,
    detectModelsModalHtml,
    fuzzyModelMatch,
    formatContextWindow,
    modelContextWindow,
    bootstrapModelFilterValue,
    filterBootstrapModelOptions,
    configuredModelEditorHtml,
    readModelEditorFields,
    refreshConfiguredModelGroups,
    syncBootstrapModelOptions,
    fillBootstrapProxyDefaultFor,
    syncBootstrapProxyDefaultsForInput,
    addBootstrapConfigRow,
    insertDetectedEnvGroups,
    removeBootstrapConfigRow
  });
}());
