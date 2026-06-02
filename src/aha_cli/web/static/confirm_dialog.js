(function () {
  function fallbackText(options = {}) {
    const lines = [
      options.title || "Confirm action?",
      options.message || ""
    ];
    for (const row of Array.isArray(options.details) ? options.details : []) {
      const label = String(row?.label ?? row?.[0] ?? "").trim();
      const value = String(row?.value ?? row?.[1] ?? "").trim();
      if (label || value) lines.push(`${label || "Detail"}: ${value || "-"}`);
    }
    return lines.filter(Boolean).join("\n");
  }

  function setDetails(detailsEl, details = []) {
    if (!detailsEl) return;
    detailsEl.replaceChildren();
    for (const row of Array.isArray(details) ? details : []) {
      const label = String(row?.label ?? row?.[0] ?? "").trim();
      const value = String(row?.value ?? row?.[1] ?? "").trim();
      const wrapper = document.createElement("div");
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = label || "Detail";
      dd.textContent = value || "-";
      wrapper.append(dt, dd);
      detailsEl.appendChild(wrapper);
    }
    detailsEl.hidden = !detailsEl.childElementCount;
  }

  function actionButton(action) {
    const button = document.createElement("button");
    button.type = "submit";
    button.value = action.value || "confirm";
    button.textContent = action.label || "Confirm";
    if (action.primary) button.classList.add("primary");
    if (action.danger) button.classList.add("danger");
    return button;
  }

  function defaultActions(options = {}) {
    return [
      { value: "cancel", label: options.cancelLabel || "Cancel" },
      {
        value: "confirm",
        label: options.confirmLabel || "Confirm",
        primary: true,
        danger: Boolean(options.danger)
      }
    ];
  }

  function defaultFallbackChoice(options, actions) {
    const confirmValue = options.confirmValue || "confirm";
    const cancelValue = options.cancelValue || "cancel";
    if (actions.length <= 2) {
      return Promise.resolve(window.confirm(fallbackText(options)) ? confirmValue : cancelValue);
    }
    return Promise.resolve(cancelValue);
  }

  function confirmChoice(options = {}) {
    const actions = Array.isArray(options.actions) && options.actions.length
      ? options.actions
      : defaultActions(options);
    const dialog = document.getElementById("action-confirm");
    const titleEl = document.getElementById("action-confirm-title");
    const messageEl = document.getElementById("action-confirm-message");
    const detailsEl = document.getElementById("action-confirm-details");
    const actionsEl = document.getElementById("action-confirm-actions");
    const fallback = () => {
      if (typeof options.fallback === "function") return Promise.resolve(options.fallback());
      return defaultFallbackChoice(options, actions);
    };
    if (!dialog || typeof dialog.showModal !== "function" || !actionsEl) return fallback();
    if (titleEl) titleEl.textContent = options.title || "Confirm action?";
    if (messageEl) {
      messageEl.textContent = options.message || "";
      messageEl.hidden = !messageEl.textContent;
    }
    setDetails(detailsEl, options.details);
    actionsEl.replaceChildren(...actions.map(actionButton));
    if (dialog.open) dialog.close("cancel");
    return new Promise(resolve => {
      const onClose = () => resolve(dialog.returnValue || "cancel");
      dialog.returnValue = "cancel";
      dialog.addEventListener("close", onClose, { once: true });
      try {
        dialog.showModal();
      } catch (_err) {
        dialog.removeEventListener("close", onClose);
        resolve(fallback());
      }
    });
  }

  async function confirmAction(options = {}) {
    return await confirmChoice(options) === (options.confirmValue || "confirm");
  }

  window.AHAConfirmDialog = Object.freeze({
    confirmAction,
    confirmChoice,
    fallbackText
  });
}());
