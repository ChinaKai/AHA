(() => {
  function createPromptsController(elements = {}, deps = {}) {
    const apiUrl = deps.apiUrl || ((path, params) => path);
    const fetchWithTimeout = deps.fetchWithTimeout || ((url, opts) => fetch(url, opts));

    let loadedList = false;
    let selectedName = "";

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
      const panel = elements.promptsDialogEl;
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
      elements.ahaPromptsEl?.setAttribute("aria-expanded", String(isOpen));
    }

    function isOpen() {
      const panel = elements.promptsDialogEl;
      if (!panel) return false;
      if (isDialogElement(panel)) return Boolean(panel.open);
      return !panel.hidden;
    }

    function close() {
      setOpen(false);
    }

    function groupLabel(group) {
      const labels = {
        backend: "backend_",
        chat: "chat_",
        knowledge: "knowledge_",
        navigation: "navigation_",
        service_assistant: "service_assistant_",
        feishu_group: "feishu_group_",
        supervision: "supervision_",
        finalization: "finalization",
        subtask: "subtask",
        runner: "runner_",
        workflow: "workflow_guidance_",
        mode_instruction: "mode_instruction_",
        task: "task_",
        policy: "policy",
        action: "action_",
        hardware: "hardware",
        browser: "browser",
        memo: "memo_",
        other: "other"
      };
      return labels[group] || group;
    }

    function renderList(templates) {
      const groups = Object.keys(templates || {}).sort();
      const html = groups
        .map(group => {
          const items = (templates[group] || [])
            .map(item => {
              const name = String(item.name || "");
              const active = name === selectedName ? " active" : "";
              return `<li><button type="button" class="prompts-item${active}" data-prompt-name="${escapeHtml(name)}">${escapeHtml(name)}</button></li>`;
            })
            .join("");
          return `<div class="prompts-group"><h3 class="prompts-group-title">${escapeHtml(groupLabel(group))}</h3><ul class="prompts-list">${items}</ul></div>`;
        })
        .join("");
      const content = elements.promptsContentEl;
      if (!content) return;
      content.innerHTML = `<div class="prompts-layout">${html ? `<nav class="prompts-nav">${html}</nav>` : `<div class="prompts-empty">No prompt templates found.</div>`}<pre class="prompts-preview">${selectedName ? "Loading…" : "Select a template to preview."}</pre></div>`;
      if (html) bindNav();
    }

    function bindNav() {
      elements.promptsContentEl?.addEventListener("click", event => {
        const target = event.target instanceof Element ? event.target : null;
        const button = target?.closest("[data-prompt-name]");
        if (!button) return;
        const name = String(button.getAttribute("data-prompt-name") || "");
        if (name) void selectTemplate(name);
      });
    }

    async function selectTemplate(name) {
      selectedName = name;
      const preview = elements.promptsContentEl?.querySelector(".prompts-preview");
      if (preview) preview.textContent = "Loading…";
      const listItems = elements.promptsContentEl?.querySelectorAll(".prompts-item");
      listItems?.forEach(item => {
        item.classList.toggle("active", item.getAttribute("data-prompt-name") === name);
      });
      try {
        const url = apiUrl("/api/prompts", { name });
        const response = await fetchWithTimeout(url, { method: "GET", headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "failed to load prompt");
        const previewEl = elements.promptsContentEl?.querySelector(".prompts-preview");
        if (previewEl) {
          previewEl.textContent = payload.text || "";
          previewEl.classList.add("loaded");
        }
      } catch (error) {
        const previewEl = elements.promptsContentEl?.querySelector(".prompts-preview");
        if (previewEl) previewEl.textContent = `Failed to load prompt: ${error.message}`;
      }
    }

    async function loadList() {
      const content = elements.promptsContentEl;
      if (!content) return;
      if (!loadedList) {
        content.innerHTML = "<div class=\"prompts-empty\">Loading prompt templates…</div>";
      }
      try {
        const url = apiUrl("/api/prompts");
        const response = await fetchWithTimeout(url, { method: "GET", headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "failed to list prompts");
        loadedList = true;
        renderList(payload.templates || {});
      } catch (error) {
        content.innerHTML = `<div class="prompts-empty">Failed to load prompt templates: ${escapeHtml(error.message)}</div>`;
      }
    }

    async function open() {
      const panel = elements.promptsDialogEl;
      if (!panel) return;
      setOpen(true);
      await loadList();
    }

    function bind() {
      elements.ahaPromptsEl?.addEventListener("click", event => {
        event.stopPropagation();
        if (isOpen()) close();
        else void open();
      });
      elements.closePromptsEl?.addEventListener("click", close);
      elements.promptsDialogEl?.addEventListener("click", event => {
        if (isDialogElement(elements.promptsDialogEl) && event.target === elements.promptsDialogEl) {
          close();
        }
      });
    }

    return Object.freeze({
      bind,
      close,
      isOpen,
      open,
      renderContent: loadList
    });
  }

  window.AHAPromptsController = Object.freeze({ createPromptsController });
})();
