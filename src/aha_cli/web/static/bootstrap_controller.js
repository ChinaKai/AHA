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

    function renderFirstRunState(force = false) {
      const error = deps.bootstrapError?.() || "";
      if (error) {
        renderBootstrapError(error);
        return;
      }
      if (!deps.bootstrapData?.()?.initialized) {
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
      configContext,
      configData,
      configMode,
      confirmConfigSave,
      createRunFromForm,
      formHtml,
      removeConfigRow,
      renderBootstrapConfigState,
      renderBootstrapError,
      renderFirstRunState,
      saveConfigForm,
      syncModelOptions
    });
  }

  window.AHABootstrapController = Object.freeze({ createBootstrapController });
})();
