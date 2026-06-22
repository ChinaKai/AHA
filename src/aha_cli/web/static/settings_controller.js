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
    }

    async function open() {
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
