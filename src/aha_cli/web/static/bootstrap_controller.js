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

    let detectModalState = null;

    function closeDetectModal() {
      if (!detectModalState) return;
      detectModalState.modalEl?.remove();
      detectModalState = null;
    }

    async function openDetectModal({ form, backend, models, authStyle, baseUrl, credential }) {
      closeDetectModal();
      const modalEl = document.createElement("div");
      modalEl.innerHTML = bootstrapConfigHelpers.detectModelsModalHtml?.(models, authStyle) || "";
      const rootEl = modalEl.firstElementChild;
      if (!rootEl) return;
      document.body.appendChild(rootEl);
      detectModalState = { form, backend, models, authStyle, baseUrl, credential, modalEl: rootEl };
      const selectedCount = () => [...rootEl.querySelectorAll("[data-bootstrap-detect-check]:checked")].length;
      const updateSelectedLabel = () => {
        const btn = rootEl.querySelector("[data-bootstrap-detect-add-selected]");
        if (btn) btn.textContent = `Add selected (${selectedCount()})`;
      };
      rootEl.querySelector("[data-bootstrap-detect-filter]")?.addEventListener("input", event => {
        const query = String(event.target?.value || "").trim().toLowerCase();
        rootEl.querySelectorAll("[data-bootstrap-detect-model-row]").forEach(row => {
          const id = String(row.dataset.modelId || "").toLowerCase();
          row.hidden = Boolean(query) && !id.includes(query);
        });
      });
      rootEl.querySelectorAll("[data-bootstrap-detect-check]").forEach(checkbox => {
        checkbox.addEventListener("change", updateSelectedLabel);
      });
      rootEl.querySelector("[data-bootstrap-detect-add-selected]")?.addEventListener("click", () => {
        const checked = [...rootEl.querySelectorAll("[data-bootstrap-detect-check]:checked")]
          .map(checkbox => checkbox.closest("[data-bootstrap-detect-model-row]")?.dataset?.modelIndex)
          .map(index => models[Number(index)])
          .filter(Boolean);
        if (checked.length) {
          bootstrapConfigHelpers.insertDetectedEnvGroups?.(form, backend, checked, baseUrl, credential, authStyle, configContext());
        }
        closeDetectModal();
      });
      rootEl.querySelectorAll("[data-bootstrap-detect-close]").forEach(closeBtn => {
        closeBtn.addEventListener("click", closeDetectModal);
      });
      rootEl.querySelectorAll("[data-bootstrap-detect-test]").forEach(testBtn => {
        testBtn.addEventListener("click", async () => {
          const row = testBtn.closest("[data-bootstrap-detect-model-row]");
          const model = models[Number(row?.dataset?.modelIndex)];
          const id = model && (typeof model === "string" ? model : model.id);
          if (!id) return;
          testBtn.disabled = true;
          testBtn.textContent = "Testing...";
          try {
            await deps.fetchJson?.("/api/detect-models/test", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ base_url: baseUrl, api_key: credential, model: id, auth_style: authStyle })
            }, "Test failed");
            testBtn.textContent = "OK";
            testBtn.classList.add("bootstrap-detect-ok");
          } catch (err) {
            testBtn.textContent = "Fail";
            testBtn.title = err?.message || String(err);
            testBtn.classList.add("bootstrap-detect-fail");
          } finally {
            testBtn.disabled = false;
          }
        });
      });
    }

    async function detectEnvModels(button) {
      const form = button?.closest?.("[data-bootstrap-config-form]");
      const block = button?.closest?.("[data-bootstrap-detect-backend]");
      if (!form || !block) return;
      const backend = String(block.dataset.bootstrapDetectBackend || "claude");
      const urlEl = block.querySelector("[data-bootstrap-detect-url]");
      const keyEl = block.querySelector("[data-bootstrap-detect-key]");
      const statusEl = block.querySelector("[data-bootstrap-detect-status]");
      const baseUrl = String(urlEl?.value || "").trim();
      const credential = String(keyEl?.value || "").trim();
      if (!baseUrl || !credential) {
        if (statusEl) statusEl.textContent = "Enter API base URL and key first.";
        return;
      }
      if (button instanceof HTMLButtonElement) button.disabled = true;
      if (statusEl) statusEl.textContent = "Detecting models...";
      try {
        const payload = await deps.fetchJson?.("/api/detect-models", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ base_url: baseUrl, api_key: credential, backend })
        }, "Failed to detect models");
        const models = Array.isArray(payload?.models) ? payload.models : [];
        if (!models.length) {
          if (statusEl) statusEl.textContent = "No models found.";
          return;
        }
        const authStyle = payload?.auth_style === "x-api-key" ? "x-api-key" : "bearer";
        if (statusEl) statusEl.textContent = "";
        await openDetectModal({ form, backend, models, authStyle, baseUrl, credential });
      } catch (err) {
        if (statusEl) statusEl.textContent = err?.message || String(err);
        else deps.alertError?.(err?.message || String(err));
      } finally {
        if (button instanceof HTMLButtonElement) button.disabled = false;
      }
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
        if (mode === "settings") return;
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
      closeDetectModal,
      configContext,
      configData,
      configMode,
      confirmConfigSave,
      createRunFromForm,
      detectEnvModels,
      formHtml,
      removeConfigRow,
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
