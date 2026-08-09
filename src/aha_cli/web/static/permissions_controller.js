(() => {
  function createPermissionsController(elements = {}, deps = {}) {
    const apiUrl = deps.apiUrl || ((path, params) => path);
    const fetchWithTimeout = deps.fetchWithTimeout || ((url, opts) => fetch(url, opts));
    const t = deps.t || ((key, fallback) => window.AHAI18n?.t?.(key, fallback) ?? fallback);

    let loaded = false;
    let saving = false;
    let candidates = [];

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

    function readPathOptions(candidates, selected) {
      const selectedPaths = new Set(selected || []);
      // Known candidates become checkboxes.
      const known = (candidates || [])
        .map(candidate => {
          const path = String(candidate.path || "");
          const checked = selectedPaths.has(path) ? " checked" : "";
          const label = escapeHtml(String(candidate.label || path));
          return `<label class="permissions-path-option"><input type="checkbox" name="read_path" value="${escapeHtml(path)}"${checked}> <span>${label}</span></label>`;
        })
        .join("");
      // Custom paths (selected but not a known candidate) become removable chips.
      const knownSet = new Set((candidates || []).map(c => String(c.path || "")));
      const custom = (selected || [])
        .filter(path => !knownSet.has(path))
        .map(path => `<span class="permissions-path-chip" data-custom-path="${escapeHtml(path)}">${escapeHtml(path)}<button type="button" class="permissions-path-chip-remove" data-remove-path="${escapeHtml(path)}" aria-label="remove">×</button></span>`)
        .join("");
      return `<div class="permissions-path-candidates">${known || '<em class="permissions-empty">No auto-detected paths.</em>'}</div>${custom ? `<div class="permissions-path-custom">${custom}</div>` : ""}`;
    }

    function formHtml(permissions, candidates) {
      const allowedTopics = escapeHtml((permissions.allowed_topics || []).join(", "));
      const handoffAlways = escapeHtml((permissions.handoff_always || []).join(", "));
      const readPaths = readPathOptions(candidates, permissions.read_paths || []);
      const allowCommon = Boolean(permissions.allow_common_knowledge);
      return `
<form class="permissions-form" data-permissions-form>
  <label class="permissions-field">
    <span>${escapeHtml(t("run.permissions_read_paths", "Readable paths"))}</span>
    <div class="permissions-read-paths">
      ${readPaths}
      <div class="permissions-path-add">
        <input type="text" name="read_path_custom" placeholder="${escapeHtml(t("run.permissions_read_paths_placeholder", "Add an absolute path…"))}">
        <button type="button" class="button-ghost" data-add-read-path>${escapeHtml(t("run.permissions_add_path", "Add"))}</button>
      </div>
    </div>
    <em>${escapeHtml(t("run.permissions_read_paths_hint", "Knowledge source of the digital human. All files under each path are readable."))}</em>
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

    function renderForm(permissions, candidates) {
      const content = elements.permissionsContentEl;
      if (!content) return;
      content.innerHTML = formHtml(permissions || {}, candidates || []);
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
        candidates = payload.candidates || [];
        renderForm(payload.permissions || {}, candidates);
      } catch (error) {
        content.innerHTML = `<div class="permissions-empty">${escapeHtml(t("run.permissions_load_error", "Failed to load permissions"))}: ${escapeHtml(error.message)}</div>`;
      }
    }

    function readForm(form) {
      const allowedTopics = String(form?.querySelector?.('[name="allowed_topics"]')?.value || "").trim();
      const handoffAlways = String(form?.querySelector?.('[name="handoff_always"]')?.value || "").trim();
      const allowCommon = Boolean(form?.querySelector?.('[name="allow_common_knowledge"]')?.checked);
      const readPaths = [];
      form?.querySelectorAll?.('[name="read_path"]:checked')?.forEach(input => {
        const value = String(input?.value || "").trim();
        if (value) readPaths.push(value);
      });
      // Custom paths are rendered as chips carrying data-path.
      form?.querySelectorAll?.('[data-custom-path]')?.forEach(chip => {
        const value = String(chip?.getAttribute?.("data-custom-path") || "").trim();
        if (value) readPaths.push(value);
      });
      return {
        read_paths: readPaths,
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
        const addBtn = event.target instanceof Element ? event.target.closest("[data-add-read-path]") : null;
        if (addBtn) {
          event.preventDefault();
          const form = addBtn.closest("[data-permissions-form]");
          const input = form?.querySelector?.('[name="read_path_custom"]');
          const value = String(input?.value || "").trim();
          if (value) {
            const current = collectReadPaths(form);
            if (!current.includes(value)) current.push(value);
            renderForm({ ...readForm(form), read_paths: current }, candidates);
          }
          return;
        }
        const removeBtn = event.target instanceof Element ? event.target.closest("[data-remove-path]") : null;
        if (removeBtn) {
          event.preventDefault();
          const form = removeBtn.closest("[data-permissions-form]");
          const value = String(removeBtn.getAttribute("data-remove-path") || "").trim();
          const current = collectReadPaths(form).filter(path => path !== value);
          renderForm({ ...readForm(form), read_paths: current }, candidates);
        }
      });
      elements.permissionsDialogEl?.addEventListener("submit", event => {
        const form = event.target instanceof Element ? event.target.closest("[data-permissions-form]") : null;
        if (!form) return;
        event.preventDefault();
        void save(form);
      });
    }

    function collectReadPaths(form) {
      const paths = [];
      form?.querySelectorAll?.('[name="read_path"]:checked')?.forEach(input => {
        const value = String(input?.value || "").trim();
        if (value) paths.push(value);
      });
      form?.querySelectorAll?.('[data-custom-path]')?.forEach(chip => {
        const value = String(chip?.getAttribute?.("data-custom-path") || "").trim();
        if (value) paths.push(value);
      });
      return paths;
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
