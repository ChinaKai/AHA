(() => {
  function escapeFallback(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function skillOptionFromSummary(skill = {}) {
    return {
      id: String(skill.id || ""),
      label: String(skill.label || skill.name || skill.id || ""),
      path: String(skill.path || ""),
      source: String(skill.source || "aha_home")
    };
  }

  function createDefaultSkillMarkdown(id) {
    const safeId = String(id || "new-skill").trim() || "new-skill";
    const title = safeId.split("-").filter(Boolean).map(part => part.charAt(0).toUpperCase() + part.slice(1)).join(" ") || "New Skill";
    return [
      "---",
      `name: ${safeId}`,
      "description: Describe what this skill does and when Codex should use it.",
      "---",
      "",
      `# ${title}`,
      "",
      "## Workflow",
      "",
      "- Add the concrete steps this skill should guide.",
      ""
    ].join("\n");
  }

  function createSkillsConsoleController(elements = {}, deps = {}) {
    const escapeHtml = deps.escapeHtml || escapeFallback;
    const t = () => window.AHAI18n?.t || ((_, fallback) => fallback);
    const format = () => window.AHAI18n?.format || ((_, __, fallback) => fallback);
    let open = false;
    let bound = false;
    const state = {
      skills: [],
      detail: null,
      draftNew: false,
      loaded: false,
      loading: false,
      saving: false,
      deleting: false,
      error: "",
      notice: "",
      selectedId: "",
      skillsRoot: ""
    };

    function selectedSkill() {
      return state.skills.find(skill => skill.id === state.selectedId) || null;
    }

    function selectedDetail() {
      return state.detail || selectedSkill();
    }

    function skillOptions() {
      return state.skills.map(skillOptionFromSummary).filter(option => option.path);
    }

    function syncSkillOptions() {
      deps.onSkillsChanged?.(skillOptions());
    }

    function renderList() {
      const i18n = t();
      if (state.loading && !state.loaded) {
        return `<div class="skills-console-empty">${escapeHtml(i18n("skills.loading", "Loading skills..."))}</div>`;
      }
      const newActive = state.draftNew;
      const createButton = `
        <button class="skills-console-list-item ${newActive ? "active" : ""}" type="button" data-skills-action="new">
          <strong>${escapeHtml(i18n("skills.new", "New skill"))}</strong>
          <span>${escapeHtml(i18n("skills.new_hint", "Create a managed AHA skill"))}</span>
        </button>
      `;
      if (!state.skills.length) {
        return `${createButton}<div class="skills-console-empty">${escapeHtml(i18n("skills.empty", "No skills yet"))}</div>`;
      }
      return `${createButton}${state.skills.map(skill => {
        const active = !state.draftNew && skill.id === state.selectedId;
        const description = skill.short_description || skill.description || skill.path || "";
        return `
          <button class="skills-console-list-item ${active ? "active" : ""}" type="button" data-skill-id="${escapeHtml(skill.id)}">
            <strong>${escapeHtml(skill.label || skill.id)}</strong>
            <span>${escapeHtml(description)}</span>
          </button>
        `;
      }).join("")}`;
    }

    function renderEditor() {
      const i18n = t();
      const detail = selectedDetail();
      if (state.loading && !state.loaded) {
        return `<div class="skills-console-empty">${escapeHtml(i18n("skills.loading", "Loading skills..."))}</div>`;
      }
      if (!detail && !state.draftNew) {
        return `<div class="skills-console-empty">${escapeHtml(i18n("skills.select_empty", "Select or create a skill."))}</div>`;
      }
      const id = state.draftNew ? "" : String(detail?.id || "");
      const skillMd = state.draftNew ? createDefaultSkillMarkdown("new-skill") : String(detail?.skill_md || "");
      const openaiYaml = state.draftNew ? "" : String(detail?.openai_yaml || "");
      return `
        <form class="skills-console-form" data-skills-form>
          <label class="field-label">
            <span>${escapeHtml(i18n("skills.id", "Skill id"))}</span>
            <input data-skill-id-input value="${escapeHtml(id)}" ${state.draftNew ? "" : "readonly"} placeholder="board-debug">
          </label>
          <label class="field-label skills-console-editor">
            <span>SKILL.md</span>
            <textarea data-skill-md rows="18" spellcheck="false" wrap="soft">${escapeHtml(skillMd)}</textarea>
          </label>
          <label class="field-label skills-console-editor">
            <span>agents/openai.yaml</span>
            <textarea data-openai-yaml rows="7" spellcheck="false" wrap="soft">${escapeHtml(openaiYaml)}</textarea>
          </label>
          <div class="skills-console-form-actions">
            <button type="submit" ${state.saving ? "disabled" : ""}>${escapeHtml(state.saving ? i18n("skills.saving", "Saving...") : i18n("common.save", "Save"))}</button>
            ${state.draftNew ? "" : `<button class="danger" type="button" data-skills-action="delete" ${state.deleting ? "disabled" : ""}>${escapeHtml(i18n("skills.delete", "Delete"))}</button>`}
          </div>
        </form>
      `;
    }

    function renderPopover() {
      if (!elements.skillsConsolePopoverEl) return;
      const i18n = t();
      const rootLine = state.skillsRoot ? `<code>${escapeHtml(state.skillsRoot)}</code>` : "";
      const message = state.error
        ? `<div class="skills-console-message error">${escapeHtml(state.error)}</div>`
        : state.notice
          ? `<div class="skills-console-message success">${escapeHtml(state.notice)}</div>`
          : "";
      elements.skillsConsolePopoverEl.innerHTML = `
        <div class="skills-console">
          <div class="skills-console-head">
            <div>
              <h3>${escapeHtml(i18n("skills.title", "Skills"))}</h3>
              <p>${escapeHtml(i18n("skills.subtitle", "Manage skills discovered from this AHA home."))}</p>
              ${rootLine}
            </div>
            <button class="button-ghost" type="button" data-skills-action="refresh" ${state.loading ? "disabled" : ""}>${escapeHtml(i18n("common.refresh", "Refresh"))}</button>
          </div>
          ${message}
          <div class="skills-console-body">
            <div class="skills-console-list">${renderList()}</div>
            <div class="skills-console-detail">${renderEditor()}</div>
          </div>
        </div>
      `;
    }

    async function loadSkill(skillId) {
      const id = String(skillId || "").trim();
      if (!id) return;
      state.error = "";
      try {
        const payload = await deps.fetchJson?.(deps.apiUrl?.(`/api/skills/${encodeURIComponent(id)}`, {}, { runScoped: false }), {}, t()("skills.load_failed", "Failed to load skill"));
        state.detail = payload?.skill || null;
        state.selectedId = String(state.detail?.id || id);
        state.draftNew = false;
      } catch (err) {
        state.error = err?.message || String(err);
      } finally {
        renderPopover();
      }
    }

    async function loadSkills(options = {}) {
      if (state.loading) return;
      state.loading = true;
      state.error = "";
      if (!options.silent) renderPopover();
      try {
        const payload = await deps.fetchJson?.(deps.apiUrl?.("/api/skills", {}, { runScoped: false }), {}, t()("skills.load_failed", "Failed to load skills"));
        state.skills = Array.isArray(payload?.skills) ? payload.skills : [];
        state.skillsRoot = String(payload?.skills_root || "");
        state.loaded = true;
        syncSkillOptions();
        if (!state.draftNew && (!state.selectedId || !state.skills.some(skill => skill.id === state.selectedId))) {
          state.selectedId = String(state.skills[0]?.id || "");
          state.detail = null;
        }
      } catch (err) {
        state.error = err?.message || String(err);
      } finally {
        state.loading = false;
        renderPopover();
      }
      if (!state.draftNew && state.selectedId && (!state.detail || state.detail.id !== state.selectedId)) {
        await loadSkill(state.selectedId);
      }
    }

    function setNewDraft() {
      state.draftNew = true;
      state.selectedId = "";
      state.detail = null;
      state.notice = "";
      state.error = "";
      renderPopover();
      window.setTimeout(() => elements.skillsConsolePopoverEl?.querySelector("[data-skill-id-input]")?.focus(), 0);
    }

    async function saveFromForm(form) {
      const idInput = form?.querySelector("[data-skill-id-input]");
      const skillMdInput = form?.querySelector("[data-skill-md]");
      const openaiYamlInput = form?.querySelector("[data-openai-yaml]");
      const id = String(idInput?.value || state.selectedId || "").trim();
      if (!id) {
        idInput?.focus();
        return;
      }
      state.saving = true;
      state.error = "";
      state.notice = "";
      renderPopover();
      const payload = {
        id,
        skill_md: String(skillMdInput?.value || ""),
        openai_yaml: String(openaiYamlInput?.value || "")
      };
      try {
        const target = state.draftNew ? "/api/skills" : `/api/skills/${encodeURIComponent(id)}`;
        const method = state.draftNew ? "POST" : "PUT";
        const result = await deps.fetchJson?.(deps.apiUrl?.(target, {}, { runScoped: false }), {
          method,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        }, t()("skills.save_failed", "Failed to save skill"));
        state.detail = result?.skill || null;
        state.selectedId = String(state.detail?.id || id);
        state.draftNew = false;
        state.notice = t()("skills.saved", "Skill saved.");
        await loadSkills({ silent: true });
      } catch (err) {
        state.error = err?.message || String(err);
      } finally {
        state.saving = false;
        renderPopover();
      }
    }

    async function deleteSelected() {
      const id = String(state.selectedId || "").trim();
      if (!id || state.deleting) return;
      const ok = await deps.confirmDialogAction?.({
        title: format()("skills.delete_title", { id }, `Delete ${id}?`),
        message: t()("skills.delete_message", "Delete this skill directory from AHA home. This cannot be undone."),
        confirmLabel: t()("skills.delete", "Delete"),
        destructive: true,
        details: [["Skill", id]]
      });
      if (!ok) return;
      state.deleting = true;
      state.error = "";
      state.notice = "";
      renderPopover();
      try {
        await deps.fetchJson?.(deps.apiUrl?.(`/api/skills/${encodeURIComponent(id)}`, {}, { runScoped: false }), { method: "DELETE" }, t()("skills.delete_failed", "Failed to delete skill"));
        state.selectedId = "";
        state.detail = null;
        state.notice = t()("skills.deleted", "Skill deleted.");
        await loadSkills({ silent: true });
      } catch (err) {
        state.error = err?.message || String(err);
      } finally {
        state.deleting = false;
        renderPopover();
      }
    }

    function setOpen(nextOpen) {
      open = Boolean(nextOpen && elements.skillsConsolePopoverEl);
      if (!elements.skillsConsolePopoverEl) return;
      if (open) {
        deps.setRunMaintenanceConsoleOpen?.(false);
        deps.setWeixinConsoleOpen?.(false);
        deps.setPlayConsoleOpen?.(false);
      }
      elements.sessionMenuEl?.classList.toggle("skills-open", open);
      elements.skillsConsolePopoverEl.hidden = !open;
      elements.skillsConsoleEl?.setAttribute("aria-expanded", String(open));
      if (open) {
        renderPopover();
        void loadSkills({ silent: state.loaded });
      } else {
        elements.skillsConsolePopoverEl.innerHTML = "";
      }
    }

    function bind() {
      if (bound) return;
      bound = true;
      elements.skillsConsolePopoverEl?.addEventListener("click", event => {
        event.stopPropagation();
        const target = event.target instanceof Element ? event.target : null;
        const skillButton = target?.closest("[data-skill-id]");
        if (skillButton) {
          event.preventDefault();
          const id = skillButton.getAttribute("data-skill-id") || "";
          state.selectedId = id;
          state.detail = null;
          state.draftNew = false;
          renderPopover();
          void loadSkill(id);
          return;
        }
        const actionButton = target?.closest("[data-skills-action]");
        if (!actionButton) return;
        event.preventDefault();
        const action = actionButton.getAttribute("data-skills-action") || "";
        if (action === "refresh") void loadSkills({ silent: false });
        if (action === "new") setNewDraft();
        if (action === "delete") void deleteSelected();
      });
      elements.skillsConsolePopoverEl?.addEventListener("submit", event => {
        const form = event.target instanceof Element ? event.target.closest("[data-skills-form]") : null;
        if (!form) return;
        event.preventDefault();
        void saveFromForm(form);
      });
      elements.skillsConsolePopoverEl?.addEventListener("input", event => {
        const input = event.target instanceof HTMLInputElement ? event.target : null;
        if (!state.draftNew || !input?.matches("[data-skill-id-input]")) return;
        const textarea = elements.skillsConsolePopoverEl?.querySelector("[data-skill-md]");
        if (textarea instanceof HTMLTextAreaElement && textarea.value.includes("name: new-skill")) {
          textarea.value = createDefaultSkillMarkdown(input.value || "new-skill");
        }
      });
    }

    return Object.freeze({
      bind,
      isOpen: () => open,
      loadSkills,
      renderSkillsConsolePopover: renderPopover,
      setSkillsConsoleOpen: setOpen
    });
  }

  window.AHASkillsConsole = Object.freeze({ createSkillsConsoleController });
})();
