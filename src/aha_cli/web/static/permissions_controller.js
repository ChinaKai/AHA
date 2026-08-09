(() => {
  function createPermissionsController(elements = {}, deps = {}) {
    const apiUrl = deps.apiUrl || ((path, params) => path);
    const fetchWithTimeout = deps.fetchWithTimeout || ((url, opts) => fetch(url, opts));
    const t = deps.t || ((key, fallback) => window.AHAI18n?.t?.(key, fallback) ?? fallback);

    let loaded = false;
    let saving = false;

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function isDialogElement(panel) {
      return typeof HTMLDialogElement !== "undefined" && panel instanceof HTMLDialogElement;
    }

    function setOpen(open) {
      const panel = elements.permissionsDialogEl;
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
      elements.ahaPermissionsEl?.setAttribute("aria-expanded", String(isOpen));
    }

    function isOpen() {
      const panel = elements.permissionsDialogEl;
      if (!panel) return false;
      if (isDialogElement(panel)) return Boolean(panel.open);
      return !panel.hidden;
    }

    function close() {
      setOpen(false);
    }

    function formHtml(permissions) {
      const allowedTopics = escapeHtml((permissions.allowed_topics || []).join(", "));
      const handoffAlways = escapeHtml((permissions.handoff_always || []).join(", "));
      const readPaths = escapeHtml((permissions.read_paths || []).join("\n"));
      const allowCommon = Boolean(permissions.allow_common_knowledge);
      return `
<form class="permissions-form" data-permissions-form>
  <label class="permissions-field">
    <span>${escapeHtml(t("run.permissions_read_paths", "Readable paths"))}</span>
    <textarea name="read_paths" rows="4" placeholder="${escapeHtml(t("run.permissions_read_paths_placeholder", "One absolute path per line"))}">${readPaths}</textarea>
    <em>${escapeHtml(t("run.permissions_read_paths_hint", "Knowledge source of the digital human. All files under each path are readable. Empty = AHA KB + workspace roots + digital-human workspace."))}</em>
  </label>
  <label class="permissions-field permissions-check">
    <span>${escapeHtml(t("run.permissions_allow_common", "Allow common knowledge"))}</span>
    <input type="checkbox" name="allow_common_knowledge"${allowCommon ? " checked" : ""}>
    <em>${escapeHtml(t("run.permissions_allow_common_hint", "When off, answer only from the readable paths and hand off everything else. When on, casual small talk and generic facts may use common knowledge, but project answers must still come from the paths."))}</em>
  </label>
  <label class="permissions-field">
    <span>${escapeHtml(t("run.permissions_allowed_topics", "Directly answerable topics"))}</span>
    <textarea name="allowed_topics" rows="3" placeholder="${escapeHtml(t("run.permissions_topics_placeholder", "Comma-separated topics, e.g. deploy guide, release notes"))}">${allowedTopics}</textarea>
    <em>${escapeHtml(t("run.permissions_allowed_topics_hint", "Topics the digital human may answer directly without handoff. Empty = none."))}</em>
  </label>
  <label class="permissions-field">
    <span>${escapeHtml(t("run.permissions_handoff_always", "Always hand off topics"))}</span>
    <textarea name="handoff_always" rows="3" placeholder="${escapeHtml(t("run.permissions_handoff_placeholder", "Comma-separated topics"))}">${handoffAlways}</textarea>
    <em>${escapeHtml(t("run.permissions_handoff_always_hint", "Additional topics that must always hand off to the owner. Empty = baseline only (execution, commitment, permissions, private/secrets)."))}</em>
  </label>
  <div class="permissions-actions">
    <span id="permissions-state" class="meta" aria-live="polite"></span>
    <button type="submit" class="button-ghost">${escapeHtml(t("common.save", "Save"))}</button>
  </div>
</form>`;
    }

    function renderForm(permissions) {
      const content = elements.permissionsContentEl;
      if (!content) return;
      content.innerHTML = formHtml(permissions || {});
    }

    async function load() {
      const content = elements.permissionsContentEl;
      if (!content) return;
      if (!loaded) {
        content.innerHTML = `<div class="permissions-empty">${escapeHtml(t("run.permissions_loading", "Loading…"))}</div>`;
      }
      try {
        const url = apiUrl("/api/agents/group-digital-human/permissions");
        const response = await fetchWithTimeout(url, { method: "GET", headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "failed to load permissions");
        loaded = true;
        renderForm(payload.permissions || {});
      } catch (error) {
        content.innerHTML = `<div class="permissions-empty">${escapeHtml(t("run.permissions_load_error", "Failed to load permissions"))}: ${escapeHtml(error.message)}</div>`;
      }
    }

    function readForm(form) {
      const allowedTopics = String(form?.querySelector?.('[name="allowed_topics"]')?.value || "").trim();
      const handoffAlways = String(form?.querySelector?.('[name="handoff_always"]')?.value || "").trim();
      const readPaths = String(form?.querySelector?.('[name="read_paths"]')?.value || "").trim();
      const allowCommon = Boolean(form?.querySelector?.('[name="allow_common_knowledge"]')?.checked);
      return {
        read_paths: readPaths ? readPaths.split(/\r?\n/).map(item => item.trim()).filter(Boolean) : [],
        allow_common_knowledge: allowCommon,
        allowed_topics: allowedTopics ? allowedTopics.split(",").map(item => item.trim()).filter(Boolean) : [],
        handoff_always: handoffAlways ? handoffAlways.split(",").map(item => item.trim()).filter(Boolean) : [],
      };
    }

    async function save(form) {
      if (saving) return;
      saving = true;
      const stateEl = elements.permissionsContentEl?.querySelector("#permissions-state");
      if (stateEl) stateEl.textContent = t("run.permissions_saving", "Saving…");
      try {
        const url = apiUrl("/api/agents/group-digital-human/permissions");
        const response = await fetchWithTimeout(url, {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify(readForm(form)),
        });
        if (!response.ok) {
          let message = `HTTP ${response.status}`;
          try {
            const payload = await response.json();
            if (payload.error) message = payload.error;
          } catch (_err) { /* ignore */ }
          throw new Error(message);
        }
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "failed to save permissions");
        loaded = true;
        renderForm(payload.permissions || {});
        if (stateEl) stateEl.textContent = t("run.permissions_saved", "Saved");
      } catch (error) {
        if (stateEl) stateEl.textContent = `${t("run.permissions_save_error", "Failed to save")}: ${error.message}`;
      } finally {
        saving = false;
      }
    }

    async function open() {
      const panel = elements.permissionsDialogEl;
      if (!panel) return;
      deps.closeMobileSheets?.();
      deps.closeMobileActionPanel?.();
      setOpen(true);
      await load();
    }

    function bind() {
      elements.ahaPermissionsEl?.addEventListener("click", event => {
        event.stopPropagation();
        if (isOpen()) close();
        else void open();
      });
      elements.closePermissionsEl?.addEventListener("click", close);
      elements.permissionsDialogEl?.addEventListener("click", event => {
        if (isDialogElement(elements.permissionsDialogEl) && event.target === elements.permissionsDialogEl) {
          close();
        }
      });
      elements.permissionsDialogEl?.addEventListener("submit", event => {
        const form = event.target instanceof Element ? event.target.closest("[data-permissions-form]") : null;
        if (!form) return;
        event.preventDefault();
        void save(form);
      });
    }

    return Object.freeze({
      bind,
      close,
      isOpen,
      open,
      renderContent: load
    });
  }

  window.AHAPermissionsController = Object.freeze({ createPermissionsController });
})();
