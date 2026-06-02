(() => {
  function createSettingsController(elements = {}, deps = {}) {
    const bootstrapConfigFormHtml = deps.bootstrapConfigFormHtml || (() => "");
    const dispatchAction = deps.dispatchAction || (() => {});

    function close() {
      const dialog = elements.settingsDialogEl;
      if (!dialog) return;
      if (typeof dialog.close === "function" && dialog.open) {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    }

    function renderContent() {
      if (!elements.settingsContentEl) return;
      elements.settingsContentEl.innerHTML = bootstrapConfigFormHtml({ mode: "settings", submitLabel: "Save Settings" });
    }

    async function open() {
      const dialog = elements.settingsDialogEl;
      if (!dialog) return;
      if (!deps.bootstrapData?.()) await deps.loadBootstrap?.();
      renderContent();
      deps.setSessionMenu?.(false);
      deps.closeMobileSheets?.();
      deps.closeMobileActionPanel?.();
      try {
        if (typeof dialog.showModal === "function") {
          if (!dialog.open) dialog.showModal();
        } else {
          dialog.setAttribute("open", "");
        }
      } catch (_err) {
        dialog.setAttribute("open", "");
      }
    }

    function bind() {
      elements.ahaSettingsEl?.addEventListener("click", event => {
        event.stopPropagation();
        void open();
      });
      elements.closeSettingsEl?.addEventListener("click", close);
      elements.settingsDialogEl?.addEventListener("click", event => {
        if (event.target === elements.settingsDialogEl) {
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
