(() => {
  function bindTabButtons(elements = {}, handlers = {}) {
    const documentRef = elements.documentRef || document;
    const activateTab = handlers.activateTab || (() => {});
    documentRef.addEventListener("click", event => {
      const target = event.target instanceof Element ? event.target : null;
      const button = target?.closest(".tab[data-tab]");
      if (!button) return;
      event.preventDefault();
      activateTab(button.dataset.tab);
    });
  }

  function bindTaskVisibilityActions(elements = {}, handlers = {}) {
    const tasksEl = elements.tasksEl;
    const updateTaskVisibility = handlers.updateTaskVisibility || (() => {});
    tasksEl?.addEventListener("pointerdown", event => {
      const target = event.target instanceof Element ? event.target : null;
      const button = target?.closest("[data-action]");
      if (!button) return;
      const taskEl = button.closest("[data-task-id]");
      if (!taskEl) return;
      event.preventDefault();
      event.stopPropagation();
      updateTaskVisibility(taskEl.dataset.taskId, button.dataset.action);
    });
  }

  function bindTaskFilter(elements = {}, handlers = {}) {
    const taskVisibilityFilterEl = elements.taskVisibilityFilterEl;
    taskVisibilityFilterEl?.addEventListener("click", event => {
      const target = event.target instanceof Element ? event.target : null;
      const button = target?.closest("[data-task-visibility-filter]");
      if (!button) return;
      const nextFilter = handlers.normalizeTaskVisibilityFilter(button.getAttribute("data-task-visibility-filter"));
      if (nextFilter === handlers.taskVisibilityFilter()) return;
      handlers.setTaskVisibilityFilter(nextFilter);
      const tasks = handlers.visibleTasks();
      if (!tasks.some(task => task.id === handlers.selectedTaskId())) {
        handlers.setSelectedTaskId(handlers.defaultTaskId(tasks));
      }
      handlers.writeStoredSelectedTaskId(handlers.selectedTaskId());
      handlers.renderTaskList();
      handlers.renderSelectedHeader();
      handlers.renderTaskProxyEditor();
      handlers.renderTaskSupervisionEditor();
      handlers.renderTaskContextEditor();
      handlers.renderTaskHardwareEditor();
      handlers.renderAgents();
      handlers.renderConversationFilters();
      handlers.renderPanel();
    });
  }

  function bindAgentTarget(elements = {}, handlers = {}) {
    elements.agentTargetEl?.addEventListener("change", async () => {
      handlers.syncAgentCards();
      handlers.renderSelectedAgentInfo();
      await handlers.loadAgentsRuntime();
      handlers.setConversationAutoFollow(true);
      handlers.renderConversationFilters();
      await handlers.ensureConversationLoaded();
      handlers.renderPendingMessages();
      handlers.renderPanel();
    });
  }

  function bindPanelEvents(elements = {}, handlers = {}) {
    const panelEl = elements.panelEl;
    const documentRef = elements.documentRef || document;
    const windowRef = elements.windowRef || window;
    // Tapping an on-screen hardware key must not blur the composer (or a mobile soft
    // keyboard would collapse), so swallow the focus-stealing pointerdown on those buttons.
    panelEl?.addEventListener("pointerdown", event => {
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest("[data-hardware-key]")) event.preventDefault();
    });
    panelEl?.addEventListener("scroll", () => {
      handlers.positionPromptMetricsPopover();
      if (handlers.activeTab() === "conversation") {
        handlers.setConversationAutoFollow(handlers.isPanelNearBottom());
      } else if (handlers.activeTab() === "logs") {
        if (handlers.selectedTaskId()) handlers.logState(handlers.selectedTaskId()).autoFollow = handlers.isPanelNearBottom();
        if (panelEl.scrollTop < 48) handlers.loadOlderLogs();
      } else if (handlers.activeTab() === "hardware") {
        if (handlers.selectedTaskId()) handlers.hardwareIoState(handlers.selectedTaskId()).autoFollow = handlers.isPanelNearBottom();
      }
    });
    panelEl?.addEventListener("submit", event => {
      const target = event.target instanceof Element ? event.target : null;
      const configForm = target?.closest("[data-bootstrap-config-form]");
      if (configForm) {
        event.preventDefault();
        handlers.saveBootstrapConfigForm(configForm);
        return;
      }
      const form = target?.closest("[data-bootstrap-run-form]");
      if (!form) return;
      event.preventDefault();
      handlers.createRunFromBootstrapForm(form);
    });
    panelEl?.addEventListener("focusin", event => {
      const input = event.target instanceof HTMLInputElement ? event.target : null;
      handlers.fillBootstrapProxyDefaultFor?.(input);
    });
    panelEl?.addEventListener("input", event => {
      const input = event.target instanceof HTMLInputElement ? event.target : null;
      handlers.syncBootstrapProxyDefaultsForInput?.(input);
      if (!input?.matches("[data-bootstrap-env-name], [data-bootstrap-env-field]")) return;
      handlers.syncBootstrapModelOptions(input.closest("[data-bootstrap-config-form]"));
    });
    panelEl?.addEventListener("click", event => {
      const target = event.target instanceof Element ? event.target : null;
      const proxyInput = event.target instanceof HTMLInputElement ? event.target : null;
      handlers.fillBootstrapProxyDefaultFor?.(proxyInput);
      const firstTaskButton = target?.closest("[data-open-first-task]");
      if (firstTaskButton) {
        event.preventDefault();
        handlers.openTaskCreateDialog();
        return;
      }
      const bridgeToggle = target?.closest("[data-hardware-bridge-action]");
      if (bridgeToggle) {
        event.preventDefault();
        handlers.hardwareBridgeControl?.(bridgeToggle.getAttribute("data-hardware-bridge-action"));
        return;
      }
      const hardwareKey = target?.closest("[data-hardware-key]");
      if (hardwareKey) {
        event.preventDefault();
        handlers.hardwareSendKey?.(hardwareKey.getAttribute("data-hardware-key"));
        return;
      }
      const rawModeToggle = target?.closest("[data-hardware-rawmode-toggle]");
      if (rawModeToggle) {
        event.preventDefault();
        handlers.hardwareToggleRawMode?.();
        return;
      }
      const addConfigRow = target?.closest("[data-bootstrap-add-row]");
      if (addConfigRow) {
        event.preventDefault();
        handlers.addBootstrapConfigRow(addConfigRow);
        return;
      }
      const removeConfigRow = target?.closest("[data-bootstrap-remove-row]");
      if (removeConfigRow) {
        event.preventDefault();
        handlers.removeBootstrapConfigRow(removeConfigRow);
        return;
      }
      const copyButton = target?.closest("[data-copy-message-key]");
      if (copyButton) {
        event.preventDefault();
        event.stopPropagation();
        handlers.copyTimelineMessage(copyButton);
        return;
      }
      const sessionButton = target?.closest("[data-session-action='compact-reset']");
      if (sessionButton instanceof HTMLButtonElement) {
        event.preventDefault();
        event.stopPropagation();
        handlers.compactResetSelectedSession(sessionButton);
        return;
      }
      const button = target?.closest("[data-load-older]");
      if (button) handlers.loadOlderConversation();
      const logButton = target?.closest("[data-load-older-log]");
      if (logButton) handlers.loadOlderLogs();
    });
    const syncMessageDetails = event => {
      const summary = event.target instanceof Element ? event.target.closest(".collapsed-message > summary") : null;
      const details = summary?.parentElement;
      if (details instanceof HTMLDetailsElement) {
        handlers.setExpandedMessageKey(details.dataset.messageKey, !details.open, details.dataset.messageContextKey);
      }
    };
    panelEl?.addEventListener("pointerdown", syncMessageDetails, true);
    panelEl?.addEventListener("keydown", event => {
      if (event.key !== "Enter" && event.key !== " ") return;
      syncMessageDetails(event);
    }, true);
    panelEl?.addEventListener("toggle", event => {
      const details = event.target instanceof HTMLDetailsElement ? event.target : null;
      const metricsKey = details?.dataset.turnMetricsKey;
      if (metricsKey) {
        const currentKey = handlers.openPromptMetricsKey();
        handlers.setOpenPromptMetricsKey(details.open ? metricsKey : currentKey === metricsKey ? "" : currentKey);
        if (!details.open) handlers.closePromptMetricsBreakdowns(details);
        if (details.open) windowRef.requestAnimationFrame(handlers.positionPromptMetricsPopover);
        return;
      }
      const key = details?.dataset.messageKey;
      if (!key) return;
      handlers.setExpandedMessageKey(key, details.open, details.dataset.messageContextKey);
    }, true);
    documentRef.addEventListener("pointerdown", handlers.closePromptMetricsPopoverForOutsideEvent, true);
    documentRef.addEventListener("focusin", handlers.closePromptMetricsPopoverForOutsideEvent, true);
    documentRef.addEventListener("keydown", event => {
      if (event.key === "Escape") handlers.closePromptMetricsPopover();
    });
    windowRef.addEventListener("resize", handlers.positionPromptMetricsPopover);
  }

  function bindRealtimeDocumentEvents(elements = {}, handlers = {}) {
    const documentRef = elements.documentRef || document;
    const windowRef = elements.windowRef || window;
    documentRef.addEventListener("visibilitychange", () => {
      handlers.realtimeDebug("document.visibilitychange", { state: documentRef.visibilityState });
      if (documentRef.visibilityState === "visible") handlers.requestRealtimeCatchup();
    });
    documentRef.addEventListener("selectionchange", handlers.flushDeferredPanelRender);
    windowRef.addEventListener("online", () => {
      handlers.realtimeDebug("window.online");
      handlers.requestRealtimeCatchup();
    });
  }

  window.AHAEventBindings = Object.freeze({
    bindAgentTarget,
    bindPanelEvents,
    bindRealtimeDocumentEvents,
    bindTabButtons,
    bindTaskFilter,
    bindTaskVisibilityActions
  });
})();
