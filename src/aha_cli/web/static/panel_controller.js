(() => {
  function createPanelController(elements = {}, deps = {}) {
    const panelEl = elements.panelEl;
    const documentRef = elements.documentRef || document;
    const activeTab = deps.activeTab || (() => "conversation");
    const setActiveTab = deps.setActiveTab || (() => {});
    const currentRunId = deps.currentRunId || (() => "");
    const selectedTask = deps.selectedTask || (() => null);
    const selectedTaskId = deps.selectedTaskId || (() => "");
    const runHasNoTasks = deps.runHasNoTasks || (() => false);
    const renderFirstRunState = deps.renderFirstRunState || (() => {});
    const renderConversationFilters = deps.renderConversationFilters || (() => {});
    const renderConversation = deps.renderConversation || (() => "");
    const renderFinalPanelHtml = deps.renderFinalPanelHtml || (() => "");
    const renderLogsPanelHtml = deps.renderLogsPanelHtml || (() => "");
    const renderHardwareIoPanelHtml = deps.renderHardwareIoPanelHtml || (() => "");
    const renderContextPanelHtml = deps.renderContextPanelHtml || (() => "");
    const renderContextEvidencePanelHtml = deps.renderContextEvidencePanelHtml || (() => "");
    const logState = deps.logState || (() => ({}));
    const hardwareIoState = deps.hardwareIoState || (() => ({}));
    const finalDetail = deps.finalDetail || (() => null);
    const contextDetail = deps.contextDetail || (() => null);
    const contextEvidenceDetail = deps.contextEvidenceDetail || (() => null);
    const promptMetricsState = deps.promptMetricsState || (() => ({}));
    const renderRawPromptSection = deps.renderRawPromptSection || (() => "");
    const renderPromptMetricsPanel = deps.renderPromptMetricsPanel || (() => "");
    const capturePromptMetricsPopoverState = deps.capturePromptMetricsPopoverState || (() => null);
    const restorePromptMetricsPopoverState = deps.restorePromptMetricsPopoverState || (() => {});
    const positionPromptMetricsPopover = deps.positionPromptMetricsPopover || (() => {});
    const captureContextScrollState = deps.captureContextScrollState || (() => null);
    const restoreContextScrollState = deps.restoreContextScrollState || (() => {});
    const syncExpandedMessageKeysFromDom = deps.syncExpandedMessageKeysFromDom || (() => {});
    const syncMobileActionPanel = deps.syncMobileActionPanel || (() => {});
    const ensureActiveTabData = deps.ensureActiveTabData || (async () => {});
    const conversationAutoFollow = deps.conversationAutoFollow || (() => true);
    const setConversationAutoFollow = deps.setConversationAutoFollow || (() => {});

    function isPanelNearBottom() {
      if (!panelEl) return true;
      return panelEl.scrollHeight - panelEl.scrollTop - panelEl.clientHeight < 80;
    }

    // True when the user has a live (non-collapsed) text selection inside `el`. Used to
    // hold off the 1s poll re-render so copying terminal output isn't wiped mid-drag.
    function hasSelectionWithin(el) {
      if (!el) return false;
      const view = documentRef.defaultView || (typeof window !== "undefined" ? window : null);
      const selection = view && view.getSelection ? view.getSelection() : null;
      if (!selection || selection.isCollapsed || !selection.rangeCount) return false;
      const node = selection.anchorNode;
      const target = node && node.nodeType === 1 ? node : node && node.parentNode;
      return Boolean(target && el.contains(target));
    }

    function renderPanel(options = {}) {
      renderConversationFilters();
      if (!currentRunId()) {
        renderFirstRunState();
        return;
      }
      const task = selectedTask();
      if (!task) {
        panelEl.innerHTML = runHasNoTasks()
          ? `
            <div class="empty first-task-empty">
              <strong>No tasks yet</strong>
              <span>Create the first task for this run.</span>
              <button type="button" data-open-first-task>Create first task</button>
            </div>
          `
          : '<div class="empty">No task selected.</div>';
        return;
      }
      // Keep the mobile action bar's Hardware entry in sync with the selected task,
      // not just on tab switches (selecting a task must re-evaluate visibility).
      syncMobileActionPanel();
      const tab = activeTab();
      if (tab === "conversation") {
        const previousTop = options.previousTop ?? panelEl.scrollTop;
        const previousHeight = options.previousHeight ?? panelEl.scrollHeight;
        const metricsPopoverState = capturePromptMetricsPopoverState();
        const metricsPopoverOpen = Boolean(metricsPopoverState);
        const shouldFollow = !metricsPopoverOpen && (conversationAutoFollow() || isPanelNearBottom());
        syncExpandedMessageKeysFromDom();
        panelEl.innerHTML = renderConversation(task.id);
        if (options.preserveScroll) {
          panelEl.scrollTop = panelEl.scrollHeight - previousHeight + previousTop;
        } else if (metricsPopoverOpen) {
          panelEl.scrollTop = previousTop;
        } else {
          panelEl.scrollTop = shouldFollow ? panelEl.scrollHeight : previousTop;
        }
        restorePromptMetricsPopoverState(metricsPopoverState);
        positionPromptMetricsPopover();
        return;
      }
      if (tab === "final") {
        panelEl.innerHTML = renderFinalPanelHtml(finalDetail(task.id));
      } else if (tab === "logs") {
        const state = logState(task.id);
        const previousTop = options.previousTop ?? panelEl.scrollTop;
        const previousHeight = options.previousHeight ?? panelEl.scrollHeight;
        const shouldFollow = state.autoFollow;
        panelEl.innerHTML = renderLogsPanelHtml(state);
        if (options.preserveScroll) {
          panelEl.scrollTop = panelEl.scrollHeight - previousHeight + previousTop;
        } else if (state.initialized) {
          panelEl.scrollTop = shouldFollow ? panelEl.scrollHeight : previousTop;
        }
      } else if (tab === "hardware") {
        const state = hardwareIoState(task.id);
        // Mirror the composer text into the terminal as a live "pending line" (line mode only;
        // raw mode sends each key live and keeps the box empty).
        const composerInput = documentRef.getElementById("message");
        state.pendingInput = state.rawMode ? "" : (composerInput ? composerInput.value : "");
        // The terminal is its own scroller (the toolbar stays pinned above it), so
        // auto-follow is decided from the terminal's own position: tail only while the
        // user is already at the bottom, otherwise leave their scroll-up untouched.
        const previousTerminal = panelEl.querySelector(".hardware-terminal");
        // A poll refresh replaces innerHTML, which would clear an in-progress selection.
        // Skip the DOM swap while the user is selecting terminal text (to copy); the next
        // tick re-renders with the accumulated state once the selection is released.
        if (previousTerminal && !options.preserveScroll && hasSelectionWithin(previousTerminal)) {
          return;
        }
        const wasAtBottom = !previousTerminal
          || previousTerminal.scrollHeight - previousTerminal.scrollTop - previousTerminal.clientHeight < 80;
        const previousTop = previousTerminal ? previousTerminal.scrollTop : 0;
        // The poll re-render rebuilds the key bar too, which would snap its horizontal scroll
        // back to 0 — so a user who scrolled to reach the right-hand keys could never tap them.
        // Preserve the key row's scrollLeft across the swap.
        const previousKeybar = panelEl.querySelector(".hardware-keybar-keys");
        const keybarScrollLeft = previousKeybar ? previousKeybar.scrollLeft : 0;
        panelEl.innerHTML = renderHardwareIoPanelHtml(state);
        const terminal = panelEl.querySelector(".hardware-terminal");
        if (terminal) {
          terminal.scrollTop = wasAtBottom ? terminal.scrollHeight : previousTop;
        }
        const keybar = panelEl.querySelector(".hardware-keybar-keys");
        if (keybar && keybarScrollLeft) keybar.scrollLeft = keybarScrollLeft;
      } else if (tab === "context") {
        const detail = contextDetail(task.id);
        if (!detail) {
          panelEl.innerHTML = '<div class="empty">Loading context...</div>';
          return;
        }
        const contextScrollState = (options.preserveContextScroll || panelEl.querySelector(".context-view"))
          ? captureContextScrollState()
          : null;
        const metrics = promptMetricsState(task.id);
        panelEl.innerHTML = renderContextPanelHtml({
          rawPromptHtml: renderRawPromptSection(metrics.data, metrics.total),
          promptMetricsHtml: renderPromptMetricsPanel(task.id)
        });
        restoreContextScrollState(contextScrollState);
      } else if (tab === "context-evidence") {
        panelEl.innerHTML = renderContextEvidencePanelHtml(contextEvidenceDetail(task.id));
      } else {
        panelEl.innerHTML = '<div class="empty">Unknown task view.</div>';
      }
    }

    async function activateTab(tab) {
      setActiveTab(tab || "conversation");
      if (activeTab() === "conversation") setConversationAutoFollow(true);
      if (activeTab() === "logs" && selectedTaskId()) logState(selectedTaskId()).autoFollow = true;
      if (activeTab() === "hardware" && selectedTaskId()) hardwareIoState(selectedTaskId()).autoFollow = true;
      documentRef.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item.dataset.tab === activeTab()));
      syncMobileActionPanel();
      await ensureActiveTabData();
      renderPanel();
    }

    return Object.freeze({
      renderPanel,
      isPanelNearBottom,
      activateTab
    });
  }

  window.AHAPanelController = Object.freeze({ createPanelController });
})();
