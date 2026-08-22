(() => {
  function createBootstrapController(elements = {}, deps = {}) {
    const bootstrapConfigHelpers = deps.bootstrapConfigHelpers || {};

    function configData() {
      return deps.bootstrapData?.()?.config || {};
    }

    function configContext() {
      return {
        config: configData(),
        backendModels: deps.backendModels?.() || new Map(),
        modelOptionsForBackend: deps.modelOptionsForBackend || (() => []),
        reasoningEffortSelectOptions: deps.reasoningEffortSelectOptions || (() => "")
      };
    }

    function formHtml(options = {}) {
      return bootstrapConfigHelpers.bootstrapConfigFormHtml?.({
        ...options,
        ...configContext()
      }) || "";
    }

    function enterEmptyWorkspace(options = {}) {
      deps.resetEmptyRunState?.();
      deps.closeEventWebSocket?.();
      deps.renderEmptyWorkspace?.(options);
    }

    function renderBootstrapConfigState(force = false) {
      deps.clearBootstrapHomeViews?.();
      enterEmptyWorkspace({
        summaryText: "Initialize AHA",
        selectedTitle: "AHA config"
      });
      if (!force && elements.panelEl?.querySelector("[data-bootstrap-config-form]")) return;

      elements.panelEl.innerHTML = `
        <div class="bootstrap-panel bootstrap-config-panel">
          <div class="bootstrap-head">
            <h3>Initialize AHA</h3>
            <code>${deps.escapeHtml?.(deps.bootstrapData?.()?.aha_home || "")}/config.json</code>
          </div>
          ${formHtml({ mode: "init", submitLabel: "Save AHA Config" })}
        </div>
      `;
    }

    function renderBootstrapError(error) {
      enterEmptyWorkspace({
        summary: false,
        summaryText: "Bootstrap failed",
        selectedTitle: "Backend version mismatch"
      });
      elements.panelEl.innerHTML = `
        <div class="bootstrap-panel">
          <div class="bootstrap-head">
            <h3>Backend Not Ready</h3>
            <code>${deps.escapeHtml?.(deps.locationOrigin?.() || "")}</code>
          </div>
          <p class="meta">The frontend loaded, but this Web backend does not support the bootstrap API. Restart the backend or confirm the browser is connected to this AHA checkout.</p>
          <pre>${deps.escapeHtml?.(String(error || ""))}</pre>
        </div>
      `;
    }

    function renderBootstrapLoadingState() {
      if (elements.panelEl?.querySelector("[data-bootstrap-loading]")) return;
      elements.panelEl.innerHTML = `
        <div class="empty" data-bootstrap-loading>
          <span>Loading AHA...</span>
        </div>
      `;
    }

    function renderFirstRunState(force = false) {
      const error = deps.bootstrapError?.() || "";
      if (error) {
        renderBootstrapError(error);
        return;
      }
      const data = deps.bootstrapData?.();
      if (!data) {
        renderBootstrapLoadingState();
        return;
      }
      if (!data.initialized) {
        renderBootstrapConfigState(force);
        return;
      }

      enterEmptyWorkspace({
        summaryText: "No memo workspace yet",
        selectedTitle: "Create a run"
      });
      if (!force && elements.panelEl?.querySelector("[data-bootstrap-run-form]")) return;

      elements.panelEl.innerHTML = `
        <div class="bootstrap-panel">
          <div class="bootstrap-head">
            <h3>Memo Workspace</h3>
            <code>${deps.escapeHtml?.(deps.bootstrapData?.()?.aha_home || "")}</code>
          </div>
          <p class="meta">Create a run container for memos first. Tasks can be created later from selected memos.</p>
          <form class="bootstrap-form" data-bootstrap-run-form>
            <label class="field-label">
              <span>Run name</span>
              <input data-bootstrap-run-name placeholder="Name this run" autofocus>
            </label>
            <button type="submit">Create Run</button>
          </form>
        </div>
      `;
    }

    function configMode(form) {
      return bootstrapConfigHelpers.bootstrapConfigMode?.(form) || "init";
    }

    async function confirmConfigSave(mode) {
      if (mode !== "settings") return true;
      return await deps.confirmDialogAction?.({
        title: "Save AHA Settings?",
        message: "Write the current defaults to .aha/config.json.",
        confirmLabel: "Save Settings",
        details: [["Target", ".aha/config.json"]]
      });
    }

    function syncModelOptions(form) {
      bootstrapConfigHelpers.syncBootstrapModelOptions?.(form, configContext());
    }

    function addConfigRow(button) {
      bootstrapConfigHelpers.addBootstrapConfigRow?.(button, configContext());
    }

    function removeConfigRow(button) {
      bootstrapConfigHelpers.removeBootstrapConfigRow?.(button, configContext());
    }

    function addProvider(button) {
      const list = button?.closest?.("[data-bootstrap-provider-list]");
      if (!list) return;
      const index = list.querySelectorAll("[data-bootstrap-provider-row]").length;
      button.insertAdjacentHTML("beforebegin", bootstrapConfigHelpers.providerRowHtml?.({}, index, true) || "");
    }

    function removeProvider(button) {
      const row = button?.closest?.("[data-bootstrap-provider-row]");
      const list = row?.closest?.("[data-bootstrap-provider-list]");
      if (!row || !list) return;
      if (list.querySelectorAll("[data-bootstrap-provider-row]").length <= 1) {
        row.querySelectorAll("input").forEach(input => { input.value = ""; });
        return;
      }
      row.remove();
    }

    let detectModalState = null;

    function closeDetectModal() {
      if (!detectModalState) return;
      detectModalState.modalEl?.remove();
      detectModalState = null;
    }

    async function openDetectModal({ form, provider, models }) {
      closeDetectModal();
      const modalEl = document.createElement("div");
      modalEl.innerHTML = bootstrapConfigHelpers.detectModelsModalHtml?.(models, {
        provider_name: provider?.name,
        auth_style: provider?.auth_style
      }) || "";
      const rootEl = modalEl.firstElementChild;
      if (!rootEl) return;
      document.body.appendChild(rootEl);
      detectModalState = { form, provider, models, modalEl: rootEl };
      const selectedRows = () => [...rootEl.querySelectorAll("[data-bootstrap-detect-check]:checked")].map(input => input.closest("[data-bootstrap-detect-model-row]")).filter(Boolean);
      const updateSelectedLabel = () => {
        const count = selectedRows().reduce((total, row) => total + row.querySelectorAll("[data-bootstrap-bind-backend]:checked").length, 0);
        const btn = rootEl.querySelector("[data-bootstrap-detect-add-selected]");
        if (btn) btn.textContent = `Add selected bindings (${count})`;
      };
      rootEl.querySelector("[data-bootstrap-detect-filter]")?.addEventListener("input", event => {
        const query = String(event.target?.value || "").trim();
        let visible = 0;
        rootEl.querySelectorAll("[data-bootstrap-detect-model-row]").forEach(row => {
          const searchText = String(row.dataset.searchText || row.dataset.modelId || "");
          const matches = bootstrapConfigHelpers.fuzzyModelMatch?.(searchText, query) ?? searchText.toLowerCase().includes(query.toLowerCase());
          row.hidden = !matches;
          if (matches) visible += 1;
        });
        const count = rootEl.querySelector("[data-bootstrap-detect-filter-count]");
        if (count) count.textContent = query ? `${visible} of ${models.length} models` : `${models.length} models`;
        const empty = rootEl.querySelector("[data-bootstrap-detect-empty]");
        if (empty) empty.hidden = visible !== 0;
      });
      rootEl.querySelector("[data-bootstrap-detect-filter]")?.focus();
      rootEl.querySelectorAll("[data-bootstrap-detect-check]").forEach(checkbox => {
        checkbox.addEventListener("change", updateSelectedLabel);
      });
      rootEl.addEventListener("change", event => {
        if (event.target instanceof HTMLInputElement && event.target.matches("[data-bootstrap-bind-backend]")) updateSelectedLabel();
      });
      rootEl.querySelector("[data-bootstrap-detect-add-selected]")?.addEventListener("click", () => {
        const bindings = selectedRows().flatMap(row => {
          const modelId = String(row.dataset.modelId || "");
          const model = models.find(item => (typeof item === "string" ? item : String(item?.id || "").trim()) === modelId) || null;
          const contextWindow = Number(model?.max_input_tokens) > 0 ? Number(model.max_input_tokens) : "";
          const maxOutputTokens = Number(model?.max_output_tokens) > 0 ? Number(model.max_output_tokens) : "";
          return [...row.querySelectorAll("[data-bootstrap-bind-backend]:checked")].map(input => ({
            provider_id: provider.id,
            model_id: modelId,
            backend: String(input.dataset.bootstrapBindBackend || ""),
            wire_api: String(input.dataset.wireApi || ""),
            context_window: contextWindow,
            max_output_tokens: maxOutputTokens
          }));
        });
        if (bindings.length) bootstrapConfigHelpers.insertConfiguredModels?.(form, provider, bindings);
        closeDetectModal();
      });
      rootEl.querySelectorAll("[data-bootstrap-detect-close]").forEach(closeBtn => closeBtn.addEventListener("click", closeDetectModal));
      const applyDetectResults = results => {
        for (const result of Array.isArray(results) ? results : []) {
          const row = rootEl.querySelector(`[data-bootstrap-detect-model-row][data-model-id="${CSS.escape(String(result.model_id || ""))}"]`);
          if (!row) continue;
          const anthropicBase = String(result.anthropic_base_url || "").trim();
          if (anthropicBase && detectModalState?.provider) {
            detectModalState.provider = { ...detectModalState.provider, anthropic_base_url: anthropicBase };
            const providerRow = detectModalState.form?.querySelector?.(`[data-bootstrap-provider-row][data-provider-id="${CSS.escape(String(detectModalState.provider.id || ""))}"]`);
            const baseInput = providerRow?.querySelector?.('[data-bootstrap-provider-field="anthropic_base_url"]');
            if (baseInput instanceof HTMLInputElement) baseInput.value = anthropicBase;
          }
          for (const [wireApi, capability] of Object.entries(result.capabilities || {})) {
            const status = String(capability?.status || "inconclusive");
            const label = row.querySelector(`[data-bootstrap-capabilities] [data-wire-api="${wireApi}"]`);
            if (label) {
              label.dataset.status = status;
              const statusEl = label.querySelector("[data-capability-status]");
              if (statusEl) statusEl.textContent = status.replaceAll("_", " ");
            }
            const bindings = row.querySelectorAll(`[data-bootstrap-bind-backend][data-wire-api="${wireApi}"]`);
            for (const binding of bindings) {
              binding.disabled = status !== "supported";
              if (binding.disabled) binding.checked = false;
            }
          }
        }
        updateSelectedLabel();
      };

      const testModels = async (modelIds, button, doneLabel) => {
        if (!modelIds.length) return;
        button.disabled = true;
        button.textContent = "Testing...";
        try {
          const payload = await deps.fetchJson?.("/api/detect-models/test", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ provider_id: provider.id, auth_style: provider.auth_style, models: modelIds })
          }, "Interface test failed");
          applyDetectResults(payload?.results);
          button.textContent = doneLabel;
        } catch (err) {
          button.textContent = "Test failed";
          button.title = err?.message || String(err);
        } finally {
          button.disabled = false;
        }
      };

      rootEl.querySelector("[data-bootstrap-detect-test-selected]")?.addEventListener("click", event => {
        const testButton = event.currentTarget;
        const rows = selectedRows();
        const modelIds = rows.map(row => String(row.dataset.modelId || "")).filter(Boolean);
        void testModels(modelIds, testButton, "Test selected");
      });

      rootEl.querySelectorAll("[data-bootstrap-detect-model-test]").forEach(button => {
        button.addEventListener("click", () => {
          const row = button.closest("[data-bootstrap-detect-model-row]");
          const modelId = String(row?.dataset?.modelId || "");
          void testModels(modelId ? [modelId] : [], button, "⚡");
        });
      });
    }

    let modelEditorState = null;

    function closeModelEditor() {
      if (!modelEditorState) return;
      modelEditorState.modalEl?.remove();
      modelEditorState = null;
    }

    function upsertConfiguredModelBinding(form, originalKey, binding, providers) {
      const list = form?.querySelector?.("[data-bootstrap-binding-list]");
      if (!list) return;
      const names = new Map((bootstrapConfigHelpers.providerList?.(providers) || []).map(provider => [String(provider.id || ""), bootstrapConfigHelpers.configString?.(provider.name, provider.id) || provider.id]));
      const keyOf = item => [String(item.provider_id || ""), String(item.model_id || item.model || ""), String(item.backend || ""), String(item.wire_api || "")].join("\0");
      const rowValue = (row, field) => String(row.querySelector(`[data-bootstrap-binding-field="${field}"]`)?.value || "").trim();
      const rows = [...list.querySelectorAll("[data-bootstrap-model-binding]")];
      const existingRow = (originalKey && rows.find(row => [rowValue(row, "provider_id"), rowValue(row, "model"), rowValue(row, "backend"), rowValue(row, "wire_api")].join("\0") === originalKey))
        || rows.find(row => [rowValue(row, "provider_id"), rowValue(row, "model"), rowValue(row, "backend"), rowValue(row, "wire_api")].join("\0") === keyOf(binding));
      if (existingRow) {
        existingRow.outerHTML = bootstrapConfigHelpers.configuredModelRowHtml?.(binding, names.get(String(binding.provider_id || ""))) || existingRow.outerHTML;
      } else {
        bootstrapConfigHelpers.insertConfiguredModels?.(form, { id: binding.provider_id, name: names.get(String(binding.provider_id || "")) }, [binding]);
      }
      bootstrapConfigHelpers.refreshConfiguredModelGroups?.(list);
      syncModelOptions(form);
    }

    function openModelEditor({ form, binding, providers }) {
      closeModelEditor();
      const modalEl = document.createElement("div");
      modalEl.innerHTML = bootstrapConfigHelpers.configuredModelEditorHtml?.(binding || {}, providers) || "";
      const rootEl = modalEl.firstElementChild;
      if (!rootEl) return;
      document.body.appendChild(rootEl);
      const originalKey = binding && binding.provider_id && binding.model_id
        ? [binding.provider_id, binding.model_id, binding.backend, binding.wire_api].join("\0")
        : null;
      modelEditorState = { form, originalKey, modalEl: rootEl };
      rootEl.querySelectorAll("[data-bootstrap-model-editor-close]").forEach(btn => btn.addEventListener("click", closeModelEditor));
      rootEl.querySelector("[data-bootstrap-model-editor-save]")?.addEventListener("click", () => {
        const next = bootstrapConfigHelpers.readModelEditorFields?.(rootEl) || {};
        if (!next.provider_id || !next.model_id) {
          const modelEl = rootEl.querySelector('[data-bootstrap-model-field="model_id"]');
          if (modelEl && !next.model_id) modelEl.focus();
          return;
        }
        upsertConfiguredModelBinding(form, originalKey, next, providers);
        closeModelEditor();
      });
    }

    async function detectEnvModels(button) {
      const form = button?.closest?.("[data-bootstrap-config-form]");
      const block = button?.closest?.("[data-bootstrap-provider-detect]");
      if (!form || !block) return;
      const statusEl = block.querySelector("[data-bootstrap-detect-status]");
      const providerId = String(block.querySelector("[data-bootstrap-detect-provider]")?.value || "").trim();
      const provider = (bootstrapConfigHelpers.providerList?.(configData().providers) || []).find(item => String(item.id || "") === providerId)
        || { id: providerId, name: block.querySelector("[data-bootstrap-detect-provider] option:checked")?.textContent || providerId };
      if (!providerId || !provider) {
        if (statusEl) statusEl.textContent = "Save and select a Provider first.";
        return;
      }
      if (button instanceof HTMLButtonElement) button.disabled = true;
      if (statusEl) statusEl.textContent = "Detecting models...";
      try {
        const payload = await deps.fetchJson?.("/api/detect-models", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider_id: providerId })
        }, "Failed to detect models");
        const models = Array.isArray(payload?.models) ? payload.models : [];
        if (!models.length) {
          if (statusEl) statusEl.textContent = "No models found.";
          return;
        }
        const detectedAuthStyle = String(payload?.auth_style || provider.auth_style || "auto");
        const detectedProvider = { ...provider, auth_style: detectedAuthStyle };
        if (provider.auth_style === "auto" && detectedAuthStyle !== "auto") {
          const providerRow = form.querySelector(`[data-bootstrap-provider-row][data-provider-id="${CSS.escape(providerId)}"]`);
          const authSelect = providerRow?.querySelector('[data-bootstrap-provider-field="auth_style"]');
          if (authSelect instanceof HTMLSelectElement) authSelect.value = detectedAuthStyle;
        }
        if (statusEl) statusEl.textContent = `Detected ${models.length} models · Authentication: ${detectedAuthStyle}`;
        await openDetectModal({ form, provider: detectedProvider, models });
      } catch (err) {
        if (statusEl) statusEl.textContent = err?.message || String(err);
        else deps.alertError?.(err?.message || String(err));
      } finally {
        if (button instanceof HTMLButtonElement) button.disabled = false;
      }
    }

    function maskSavedBackendCredentials(form) {
      form.querySelectorAll("[data-bootstrap-provider-row] input[type='password'], [data-bootstrap-row='codex.env'] input[type='password'], [data-bootstrap-row='claude.env'] input[type='password']")
        .forEach(input => {
          if (!(input instanceof HTMLInputElement)) return;
          if (input.value.trim()) input.placeholder = "Configured; leave blank to keep";
          input.value = "";
        });
      form.querySelectorAll("[data-bootstrap-detect-key]").forEach(input => {
        if (input instanceof HTMLInputElement) input.value = "";
      });
    }

    async function saveConfigForm(form) {
      const submit = form.querySelector('button[type="submit"]');
      const state = form.querySelector("[data-bootstrap-config-state]");
      const mode = configMode(form);
      if (!await confirmConfigSave(mode)) return;
      if (submit) submit.disabled = true;
      if (state) state.textContent = "Saving...";
      try {
        const body = bootstrapConfigHelpers.bootstrapConfigPayload?.(form, configContext()) || {};
        const payload = await deps.fetchJson?.("/api/bootstrap", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        }, mode === "settings" ? "Failed to save settings" : "Failed to initialize AHA");
        deps.applyBootstrapPayload?.(payload);
        if (state) state.textContent = mode === "settings" ? "Saved." : "";
        if (mode === "settings") {
          maskSavedBackendCredentials(form);
          return;
        }
        if (deps.currentRunId?.()) {
          await deps.loadStatus?.({ forceAgents: true });
        } else {
          renderFirstRunState(true);
        }
      } catch (err) {
        const message = err?.message || String(err);
        if (state) state.textContent = message;
        else deps.alertError?.(message);
      } finally {
        if (submit) submit.disabled = false;
      }
    }

    async function createRunFromForm(form) {
      const goalEl = form.querySelector("[data-bootstrap-run-name]");
      const submit = form.querySelector('button[type="submit"]');
      const goal = String(goalEl?.value || "").trim();
      if (!goal) {
        goalEl?.focus();
        return;
      }
      if (submit) submit.disabled = true;
      try {
        const createdRunId = await deps.createRun?.(goal, "research", { createInitialTask: false });
        if (createdRunId) deps.openTaskMemoHome?.();
      } catch (err) {
        deps.alertError?.(err?.message || String(err));
      } finally {
        if (submit) submit.disabled = false;
      }
    }

    return Object.freeze({
      addConfigRow,
      addProvider,
      closeDetectModal,
      closeModelEditor,
      configContext,
      configData,
      configMode,
      confirmConfigSave,
      createRunFromForm,
      detectEnvModels,
      formHtml,
      openModelEditor,
      removeConfigRow,
      removeProvider,
      renderBootstrapConfigState,
      renderBootstrapError,
      renderBootstrapLoadingState,
      renderFirstRunState,
      saveConfigForm,
      syncModelOptions
    });
  }

  window.AHABootstrapController = Object.freeze({ createBootstrapController });
})();
