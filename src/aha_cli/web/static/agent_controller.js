(() => {
  function createAgentController(elements = {}, deps = {}) {
    let agentsPanelEditingUntil = 0;

    function markAgentsPanelEditing(durationMs = 10000) {
      agentsPanelEditingUntil = Date.now() + durationMs;
    }

    function isAgentsPanelEditing() {
      return Date.now() < agentsPanelEditingUntil;
    }

    function selectedAgentId() {
      return elements.agentTargetEl?.value || "main";
    }

    function renderSelectedAgentInfo() {
      if (!elements.selectedAgentInfoEl) return;
      elements.selectedAgentInfoEl.textContent = "";
      elements.selectedAgentInfoEl.hidden = true;
    }

    function syncAgentCards() {
      [...(elements.agentsEl?.querySelectorAll(".agent-card") || [])].forEach(card => {
        card.classList.toggle("active", card.dataset.agentId === selectedAgentId());
      });
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
        const resolvedModel = agent.backend_resolved_model || deps.modelLabelForBackend?.(agent.backend, modelValue);
        const contextPressure = deps.agentContextPressureSummary?.(agent);
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
        card.className = `agent-card ${agentDisplay?.isHostAgent ? "host-agent" : ""} ${agent.id === previous ? "active" : ""}`;
        card.dataset.agentId = agent.id;
        card.title = agentDisplay?.title || "";
        card.innerHTML = `
          <div class="agent-card-head">
            <strong>${deps.escapeHtml?.(agent.id) || agent.id}</strong>
            <span class="agent-process ${deps.escapeHtml?.(processStatus) || processStatus}" title="backend process status">${deps.escapeHtml?.(deps.agentBackendProcessLabel?.(agent) || processStatus.toUpperCase()) || ""}</span>
          </div>
          <div class="meta truncate">${deps.escapeHtml?.(agentDisplay?.metaLines?.[0] || "") || ""}</div>
          <div class="meta truncate">${deps.escapeHtml?.(agentDisplay?.metaLines?.[1] || "") || ""}</div>
          <div class="meta truncate">${deps.escapeHtml?.(agentDisplay?.metaLines?.[2] || "") || ""}</div>
          <div class="meta truncate">${deps.escapeHtml?.(agentDisplay?.metaLines?.[3] || "") || ""}</div>
          ${deps.agentConfigController?.agentConfigEditorHtml(agent.id, currentConfig) || ""}
        `;
        card.addEventListener("click", event => {
          const clicked = event.target instanceof Element ? event.target : null;
          if (clicked?.closest("select") || clicked?.closest("input") || clicked?.closest("button")) return;
          agentTargetEl.value = agent.id;
          agentTargetEl.dispatchEvent(new Event("change"));
          deps.closeMobileSheets?.();
        });
        deps.agentConfigController?.bindAgentConfigEditor(card, agent, currentConfig);
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
    }

    function bind() {
      elements.agentsEl?.addEventListener("pointerdown", () => markAgentsPanelEditing());
      elements.agentsEl?.addEventListener("focusin", () => markAgentsPanelEditing());
      elements.agentsEl?.addEventListener("change", () => markAgentsPanelEditing(1500));
      elements.agentTargetEl?.addEventListener("change", async () => {
        syncAgentCards();
        renderSelectedAgentInfo();
        await deps.loadAgentsRuntime?.();
        deps.setConversationAutoFollow?.(true);
        deps.renderConversationFilters?.();
        await deps.ensureConversationLoaded?.();
        deps.renderPendingMessages?.();
        deps.renderPanel?.();
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
