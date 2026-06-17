(() => {
  function createAgentController(elements = {}, deps = {}) {
    const documentRef = deps.documentRef || document;
    const windowRef = deps.windowRef || window;
    let agentsPanelEditingUntil = 0;
    let agentSettingsOpen = false;
    let agentSettingsAgentId = "";

    function markAgentsPanelEditing(durationMs = 10000) {
      agentsPanelEditingUntil = Date.now() + durationMs;
    }

    function isAgentsPanelEditing() {
      if (agentSettingsOpen) return true;
      return Date.now() < agentsPanelEditingUntil;
    }

    function selectedAgentId() {
      return elements.agentTargetEl?.value || "main";
    }

    function renderSelectedAgentInfo() {
      if (!elements.selectedAgentInfoEl) return;
      elements.selectedAgentInfoEl.hidden = true;
      elements.selectedAgentInfoEl.innerHTML = "";
    }

    function syncAgentCards() {
      [...(elements.agentsEl?.querySelectorAll(".agent-card") || [])].forEach(card => {
        card.classList.toggle("active", card.dataset.agentId === selectedAgentId());
      });
    }

    function currentTaskAgent(agentId) {
      const task = deps.selectedTask?.();
      if (!task || !agentId) return null;
      return (task.agents || []).find(agent => String(agent.id || "") === String(agentId)) || null;
    }

    function agentSettingsUseSheet() {
      return Boolean(windowRef.matchMedia?.("(max-width: 640px)")?.matches);
    }

    function agentSettingsTriggerFor(agentId) {
      const buttons = elements.agentsEl?.querySelectorAll("[data-agent-settings-trigger]") || [];
      return Array.from(buttons).find(button => button.getAttribute("data-agent-settings-trigger") === agentId) || null;
    }

    function ensureAgentSettingsPanel() {
      let panel = documentRef.getElementById?.("agent-settings-panel");
      if (!panel) {
        panel = documentRef.createElement("section");
        panel.id = "agent-settings-panel";
        panel.className = "agent-settings-panel hidden";
        panel.setAttribute("aria-labelledby", "agent-settings-title");
        panel.addEventListener("pointerdown", () => markAgentsPanelEditing());
        panel.addEventListener("focusin", () => markAgentsPanelEditing());
        panel.addEventListener("change", () => markAgentsPanelEditing());
        documentRef.body?.appendChild(panel);
      }
      return panel;
    }

    function clearAgentSettingsPosition(panel = ensureAgentSettingsPanel()) {
      panel.style.removeProperty("top");
      panel.style.removeProperty("left");
      panel.style.removeProperty("width");
    }

    function positionAgentSettingsPanel(agent, panel = ensureAgentSettingsPanel()) {
      if (!agent) return;
      clearAgentSettingsPosition(panel);
      const useSheet = agentSettingsUseSheet();
      panel.classList.toggle("agent-settings-sheet", useSheet);
      panel.classList.toggle("agent-settings-popover", !useSheet);
      if (useSheet) return;
      const trigger = agentSettingsTriggerFor(agent.id);
      const rect = trigger?.getBoundingClientRect?.();
      if (!rect) return;
      const margin = 12;
      const gap = 8;
      const width = Math.min(360, Math.max(280, windowRef.innerWidth - margin * 2));
      panel.style.width = `${width}px`;
      const height = Math.min(panel.offsetHeight || 420, windowRef.innerHeight - margin * 2);
      let left = rect.left - width - gap;
      if (left < margin) left = rect.right + gap;
      left = Math.max(margin, Math.min(left, windowRef.innerWidth - width - margin));
      const maxTop = Math.max(margin, windowRef.innerHeight - height - margin);
      const top = Math.max(margin, Math.min(rect.top, maxTop));
      panel.style.left = `${Math.round(left)}px`;
      panel.style.top = `${Math.round(top)}px`;
    }

    function closeAgentSettings(options = {}) {
      agentSettingsOpen = false;
      agentSettingsAgentId = "";
      const panel = ensureAgentSettingsPanel();
      panel.classList.add("hidden");
      panel.hidden = true;
      clearAgentSettingsPosition(panel);
      if (options.renderCards !== false) renderAgents();
    }

    function renderAgentSettingsPanel(agent, currentConfig, agentDisplay = {}) {
      const panel = ensureAgentSettingsPanel();
      const open = Boolean(agentSettingsOpen && agent && agent.id === agentSettingsAgentId);
      panel.classList.toggle("hidden", !open);
      panel.hidden = !open;
      if (!open) {
        clearAgentSettingsPosition(panel);
        return;
      }
      const title = window.AHAI18n?.t?.("agent.settings", "Agent settings") || "Agent settings";
      const closeLabel = window.AHAI18n?.t?.("common.close", "Close") || "Close";
      panel.innerHTML = `
        <div class="agent-settings-header">
          <div>
            <h3 id="agent-settings-title">${deps.escapeHtml?.(title) || title}</h3>
            <div class="meta">${deps.escapeHtml?.(agent.id) || agent.id}</div>
          </div>
          <button class="agent-settings-close" type="button">${deps.escapeHtml?.(closeLabel) || closeLabel}</button>
        </div>
        <div class="agent-settings-body">
          ${agentDisplay?.statusText ? `<div class="meta truncate">${deps.escapeHtml?.(agentDisplay.statusText) || ""}</div>` : ""}
          ${agentDisplay?.contextPressure ? `<div class="meta truncate">${deps.escapeHtml?.(agentDisplay.contextPressure) || ""}</div>` : ""}
          ${deps.agentConfigController?.agentConfigEditorHtml(agent.id, currentConfig) || ""}
        </div>
      `;
      panel.querySelector(".agent-settings-close")?.addEventListener("click", event => {
        event.preventDefault();
        event.stopPropagation();
        closeAgentSettings();
      });
      deps.agentConfigController?.bindAgentConfigEditor(panel, agent, currentConfig);
      positionAgentSettingsPanel(agent, panel);
    }

    function openAgentSettings(agentId) {
      if (!agentId) return;
      if (agentSettingsOpen && agentSettingsAgentId === agentId) {
        closeAgentSettings();
        return;
      }
      agentSettingsOpen = true;
      agentSettingsAgentId = agentId;
      markAgentsPanelEditing();
      renderAgents();
    }

    function renderAgents() {
      const task = deps.selectedTask?.();
      const agentsEl = elements.agentsEl;
      const agentTargetEl = elements.agentTargetEl;
      if (!agentsEl || !agentTargetEl) return;
      agentsEl.innerHTML = "";
      const previous = agentTargetEl.value;
      agentTargetEl.innerHTML = "";
      if (!task) {
        renderSelectedAgentInfo();
        return;
      }

      const allAgents = task.agents || [];
      const groupedAgents = deps.agentOptionGroups?.(allAgents) || [];
      for (const group of groupedAgents) {
        const optGroup = document.createElement("optgroup");
        optGroup.label = group.label;
        for (const agent of group.agents) {
          const opt = document.createElement("option");
          opt.value = agent.id;
          opt.textContent = deps.agentOptionLabel?.(agent) || agent.id;
          optGroup.appendChild(opt);
        }
        agentTargetEl.appendChild(optGroup);
      }

      const renderAgentCard = agent => {
        const runtimeDefaults = deps.agentRuntimeDefaults?.(agent, task) || {};
        const sandbox = runtimeDefaults.sandbox;
        const approval = runtimeDefaults.approval;
        const proxyEnabled = runtimeDefaults.proxyEnabled;
        const processStatus = deps.agentBackendProcessStatus?.(agent) || "stopped";
        const rawProcessStatus = agent.backend_process_status || processStatus;
        const lifecycleDisplay = deps.agentLifecycleDisplay?.(agent) || "";
        const lifecycleTiming = deps.agentStatusTiming?.(agent);
        const lifecycleTimingText = deps.agentStatusTimingText?.(agent) || "";
        const lastReply = deps.formatLocalTimestamp?.(agent.backend_process_last_reply_at, agent.backend_process_last_reply_at || "");
        const modelValue = deps.agentModelValue?.(agent, task);
        const currentConfig = deps.normalizeAgentConfig?.({
          backend: agent.backend || "codex",
          model: modelValue,
          sandbox,
          approval,
          proxyEnabled
        });
        const resolvedModel = deps.modelLabelForBackend?.(agent.backend, modelValue) || agent.backend_resolved_model || "";
        const contextPressure = deps.agentContextPressureSummary?.(agent);
        const settingsLabel = window.AHAI18n?.format?.("agent.settings_for", { agent: agent.id }, `Agent settings for ${agent.id}`) || `Agent settings for ${agent.id}`;
        const settingsOpen = agentSettingsOpen && agentSettingsAgentId === agent.id;
        const agentDisplay = deps.agentDisplayModel?.(agent, task, {
          processStatus,
          rawProcessStatus,
          lifecycleDisplay,
          statusText: lifecycleTimingText || lifecycleDisplay,
          resolvedModel,
          taskProxySummary: deps.taskProxySummary?.(task),
          contextPressure,
          lastReply,
          statusStarted: lifecycleTiming?.startedAt ? deps.formatClock?.(lifecycleTiming.startedAt) : "",
          statusFinished: lifecycleTiming?.finishedAt ? deps.formatClock?.(lifecycleTiming.finishedAt) : ""
        });
        const card = document.createElement("div");
        card.className = `agent-card ${agentDisplay?.isHostAgent ? "host-agent" : ""} ${agent.id === previous ? "active" : ""}${settingsOpen ? " settings-open" : ""}`;
        card.dataset.agentId = agent.id;
        card.title = agentDisplay?.title || "";
        card.innerHTML = `
          <div class="agent-card-head">
            <strong>${deps.escapeHtml?.(agent.id) || agent.id}</strong>
            <div class="agent-card-actions">
              <span class="agent-process ${deps.escapeHtml?.(processStatus) || processStatus}" title="backend process status">${deps.escapeHtml?.(deps.agentBackendProcessLabel?.(agent) || processStatus.toUpperCase()) || ""}</span>
              <button class="agent-settings-trigger" type="button" data-agent-settings-trigger="${deps.escapeHtml?.(agent.id) || agent.id}" aria-controls="agent-settings-panel" aria-expanded="${settingsOpen ? "true" : "false"}" aria-label="${deps.escapeHtml?.(settingsLabel) || settingsLabel}" title="${deps.escapeHtml?.(settingsLabel) || settingsLabel}"><span aria-hidden="true">⚙</span></button>
            </div>
          </div>
          <div class="agent-card-status-row">
            <code>${deps.escapeHtml?.(`${agent.backend || "-"} / ${resolvedModel}`) || ""}</code>
          </div>
        `;
        card.addEventListener("click", event => {
          const clicked = event.target instanceof Element ? event.target : null;
          if (clicked?.closest("select, input, button, [data-agent-config-editor]")) return;
          closeAgentSettings({ renderCards: false });
          agentTargetEl.value = agent.id;
          agentTargetEl.dispatchEvent(new Event("change"));
          deps.closeMobileSheets?.();
        });
        return card;
      };

      const appendAgentSection = group => {
        const section = document.createElement("section");
        section.className = `agent-section ${group.className}`;
        section.innerHTML = `
          <div class="agent-section-head">
            <h3>${deps.escapeHtml?.(group.label) || group.label}</h3>
            <span>${group.agents.length}</span>
          </div>
        `;
        for (const agent of group.agents) section.appendChild(renderAgentCard(agent));
        agentsEl.appendChild(section);
      };

      groupedAgents.forEach(appendAgentSection);

      if ([...agentTargetEl.options].some(item => item.value === previous)) agentTargetEl.value = previous;
      syncAgentCards();
      renderSelectedAgentInfo();
      if (agentSettingsOpen) {
        const agent = currentTaskAgent(agentSettingsAgentId);
        if (!agent) {
          closeAgentSettings({ renderCards: false });
        } else {
          const runtimeDefaults = deps.agentRuntimeDefaults?.(agent, task) || {};
          const modelValue = deps.agentModelValue?.(agent, task);
          const currentConfig = deps.normalizeAgentConfig?.({
            backend: agent.backend || "codex",
            model: modelValue,
            sandbox: runtimeDefaults.sandbox,
            approval: runtimeDefaults.approval,
            proxyEnabled: runtimeDefaults.proxyEnabled
          });
          const processStatus = deps.agentBackendProcessStatus?.(agent) || "stopped";
          const lifecycleTiming = deps.agentStatusTiming?.(agent);
          const contextPressure = deps.agentContextPressureSummary?.(agent);
          const resolvedModel = deps.modelLabelForBackend?.(agent.backend, modelValue) || agent.backend_resolved_model || "";
          const agentDisplay = deps.agentDisplayModel?.(agent, task, {
            processStatus,
            rawProcessStatus: agent.backend_process_status || processStatus,
            lifecycleDisplay: deps.agentLifecycleDisplay?.(agent) || "",
            statusText: deps.agentStatusTimingText?.(agent) || deps.agentLifecycleDisplay?.(agent) || "",
            resolvedModel,
            taskProxySummary: deps.taskProxySummary?.(task),
            contextPressure,
            lastReply: deps.formatLocalTimestamp?.(agent.backend_process_last_reply_at, agent.backend_process_last_reply_at || ""),
            statusStarted: lifecycleTiming?.startedAt ? deps.formatClock?.(lifecycleTiming.startedAt) : "",
            statusFinished: lifecycleTiming?.finishedAt ? deps.formatClock?.(lifecycleTiming.finishedAt) : ""
          });
          renderAgentSettingsPanel(agent, currentConfig, agentDisplay);
        }
      } else {
        renderAgentSettingsPanel(null, null);
      }
    }

    function bind() {
      elements.agentsEl?.addEventListener("pointerdown", () => markAgentsPanelEditing());
      elements.agentsEl?.addEventListener("focusin", () => markAgentsPanelEditing());
      elements.agentsEl?.addEventListener("change", () => markAgentsPanelEditing(1500));
      elements.agentTargetEl?.addEventListener("change", async () => {
        closeAgentSettings({ renderCards: false });
        syncAgentCards();
        renderSelectedAgentInfo();
        await deps.loadAgentsRuntime?.();
        deps.setConversationAutoFollow?.(true);
        deps.renderConversationFilters?.();
        await deps.ensureConversationLoaded?.();
        deps.renderPendingMessages?.();
        deps.renderPanel?.();
      });
      elements.agentsEl?.addEventListener("click", event => {
        const target = event.target instanceof Element ? event.target : null;
        const button = target?.closest("[data-agent-settings-trigger]");
        if (!button) return;
        event.preventDefault();
        event.stopPropagation();
        openAgentSettings(button.getAttribute("data-agent-settings-trigger"));
      });
      documentRef.addEventListener("pointerdown", event => {
        if (!agentSettingsOpen) return;
        const target = event.target instanceof Element ? event.target : null;
        if (!target) return;
        const panel = ensureAgentSettingsPanel();
        if (panel.contains(target)) return;
        if (target.closest("[data-agent-settings-trigger]")) return;
        if (target.closest("dialog, .confirm-dialog")) return;
        closeAgentSettings();
      });
      documentRef.addEventListener("keydown", event => {
        if (event.key !== "Escape" || !agentSettingsOpen) return;
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation?.();
        closeAgentSettings();
      });
      windowRef.addEventListener?.("resize", () => {
        if (!agentSettingsOpen) return;
        const agent = currentTaskAgent(agentSettingsAgentId);
        if (!agent) closeAgentSettings();
        else positionAgentSettingsPanel(agent);
      });
    }

    return Object.freeze({
      bind,
      isAgentsPanelEditing,
      markAgentsPanelEditing,
      renderAgents,
      renderSelectedAgentInfo,
      syncAgentCards
    });
  }

  window.AHAAgentController = Object.freeze({ createAgentController });
})();
