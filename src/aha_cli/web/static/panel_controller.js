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
    const logState = deps.logState || (() => ({}));
    const hardwareIoState = deps.hardwareIoState || (() => ({}));
    const finalDetail = deps.finalDetail || (() => null);
    const contextDetail = deps.contextDetail || (() => null);
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
        const previousTop = options.previousTop ?? panelEl.scrollTop;
        const shouldFollow = state.autoFollow;
        panelEl.innerHTML = renderHardwareIoPanelHtml(state);
        if (state.initialized) {
          panelEl.scrollTop = shouldFollow ? panelEl.scrollHeight : previousTop;
        }
      } else {
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
