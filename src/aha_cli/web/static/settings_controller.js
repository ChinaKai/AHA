(() => {
  function createSettingsController(elements = {}, deps = {}) {
    const bootstrapConfigFormHtml = deps.bootstrapConfigFormHtml || (() => "");
    const dispatchAction = deps.dispatchAction || (() => {});

    function isDialogElement(panel) {
      return typeof HTMLDialogElement !== "undefined" && panel instanceof HTMLDialogElement;
    }

    function setOpen(open) {
      const panel = elements.settingsDialogEl;
      if (!panel) return;
      const isOpen = Boolean(open);
      if (isDialogElement(panel)) {
        if (isOpen) {
          try {
            if (typeof panel.showModal === "function" && !panel.open) {
              panel.showModal();
            } else {
              panel.setAttribute("open", "");
            }
          } catch (_err) {
            panel.setAttribute("open", "");
          }
        } else if (typeof panel.close === "function" && panel.open) {
          panel.close();
        } else {
          panel.removeAttribute("open");
        }
      } else {
        panel.hidden = !isOpen;
        panel.toggleAttribute("open", isOpen);
      }
      elements.ahaSettingsEl?.setAttribute("aria-expanded", String(isOpen));
      elements.sessionMenuEl?.classList?.toggle("settings-open", isOpen);
    }

    function isOpen() {
      const panel = elements.settingsDialogEl;
      if (!panel) return false;
      if (isDialogElement(panel)) return Boolean(panel.open);
      return !panel.hidden;
    }

    function close() {
      setOpen(false);
    }

    function renderContent() {
      if (!elements.settingsContentEl) return;
      elements.settingsContentEl.innerHTML = bootstrapConfigFormHtml({ mode: "settings", submitLabel: "Save Settings" });
      window.AHAI18n?.apply(elements.settingsContentEl);
    }

    async function open(options = {}) {
      const panel = elements.settingsDialogEl;
      if (!panel) return;
      if (!deps.bootstrapData?.()) await deps.loadBootstrap?.();
      renderContent();
      deps.closeMobileSheets?.();
      deps.closeMobileActionPanel?.();
      setOpen(true);
    }

    function bind() {
      elements.ahaSettingsEl?.addEventListener("click", event => {
        event.stopPropagation();
        if (isOpen()) close();
        else void open();
      });
      elements.closeSettingsEl?.addEventListener("click", close);
      elements.settingsDialogEl?.addEventListener("click", event => {
        if (isDialogElement(elements.settingsDialogEl) && event.target === elements.settingsDialogEl) {
          close();
          return;
        }
        const proxyInput = event.target instanceof HTMLInputElement ? event.target : null;
        deps.fillBootstrapProxyDefaultFor?.(proxyInput);
        const addConfigRow = event.target instanceof Element ? event.target.closest("[data-bootstrap-add-row]") : null;
        if (addConfigRow) {
          event.preventDefault();
          deps.addBootstrapConfigRow?.(addConfigRow);
          return;
        }
        const removeConfigRow = event.target instanceof Element ? event.target.closest("[data-bootstrap-remove-row]") : null;
        if (removeConfigRow) {
          event.preventDefault();
          deps.removeBootstrapConfigRow?.(removeConfigRow);
          return;
        }
        const addProvider = event.target instanceof Element ? event.target.closest("[data-bootstrap-add-provider]") : null;
        if (addProvider) {
          event.preventDefault();
          deps.addBootstrapProvider?.(addProvider);
          return;
        }
        const removeProvider = event.target instanceof Element ? event.target.closest("[data-bootstrap-remove-provider]") : null;
        if (removeProvider) {
          event.preventDefault();
          deps.removeBootstrapProvider?.(removeProvider);
          return;
        }
        const removeBinding = event.target instanceof Element ? event.target.closest("[data-bootstrap-remove-binding]") : null;
        if (removeBinding) {
          event.preventDefault();
          const list = removeBinding.closest("[data-bootstrap-binding-list]");
          removeBinding.closest("[data-bootstrap-model-binding]")?.remove();
          deps.refreshConfiguredModelGroups?.(list);
          return;
        }
        const editBinding = event.target instanceof Element ? event.target.closest("[data-bootstrap-edit-binding]") : null;
        if (editBinding) {
          event.preventDefault();
          const row = editBinding.closest("[data-bootstrap-model-binding]");
          const form = editBinding.closest("[data-bootstrap-config-form]");
          if (!row || !form) return;
          const value = key => String(row.querySelector(`[data-bootstrap-binding-field="${key}"]`)?.value || "").trim();
          const binding = { provider_id: value("provider_id"), model_id: value("model"), backend: value("backend"), wire_api: value("wire_api") };
          const contextWindow = Number(value("context_window"));
          if (Number.isFinite(contextWindow) && contextWindow > 0) binding.context_window = contextWindow;
          const compactThreshold = Number(value("auto_compact_threshold_percent"));
          if (Number.isFinite(compactThreshold) && compactThreshold >= 1 && compactThreshold <= 99) binding.auto_compact_threshold_percent = Math.round(compactThreshold);
          for (const key of ["fable_model", "opus_model", "sonnet_model", "haiku_model"]) {
            const text = value(key);
            if (text) binding[key] = text;
          }
          deps.openModelEditor?.({ form, binding, providers: deps.providerList?.(deps.bootstrapData?.()?.config?.providers || []) || [] });
          return;
        }
        const addBinding = event.target instanceof Element ? event.target.closest("[data-bootstrap-add-binding]") : null;
        if (addBinding) {
          event.preventDefault();
          deps.openModelEditor?.({ form: addBinding.closest("[data-bootstrap-config-form]"), binding: null, providers: deps.providerList?.(deps.bootstrapData?.()?.config?.providers || []) || [] });
          return;
        }
        const detectButton = event.target instanceof Element ? event.target.closest("[data-bootstrap-detect-models]") : null;
        if (detectButton) {
          event.preventDefault();
          void deps.detectEnvModels?.(detectButton);
        }
      });
      elements.settingsDialogEl?.addEventListener("focusin", event => {
        const input = event.target instanceof HTMLInputElement ? event.target : null;
        deps.fillBootstrapProxyDefaultFor?.(input);
      });
      elements.settingsDialogEl?.addEventListener("submit", event => {
        const form = event.target instanceof Element ? event.target.closest("[data-bootstrap-config-form]") : null;
        if (!form) return;
        event.preventDefault();
        void dispatchAction("settings-save", { form });
      });
      elements.settingsDialogEl?.addEventListener("input", event => {
        const input = event.target instanceof HTMLInputElement ? event.target : null;
        deps.syncBootstrapProxyDefaultsForInput?.(input);
        if (!input?.matches("[data-bootstrap-env-name], [data-bootstrap-env-field]")) return;
        deps.syncBootstrapModelOptions?.(input.closest("[data-bootstrap-config-form]"));
      });
      elements.settingsDialogEl?.addEventListener("change", event => {
        const filter = event.target instanceof Element ? event.target.closest("[data-bootstrap-config-field$='.model_source']") : null;
        if (filter) deps.syncBootstrapModelOptions?.(filter.closest("[data-bootstrap-config-form]"));
      });
    }

    return Object.freeze({
      bind,
      close,
      open,
      renderContent
    });
  }

  window.AHASettingsController = Object.freeze({ createSettingsController });
})();
