(() => {
  function createRenderOrchestrator(deps = {}) {
    const noop = () => {};
    const renderSessionSummary = deps.renderSessionSummary || noop;
    const renderTaskList = deps.renderTaskList || noop;
    const renderSelectedHeader = deps.renderSelectedHeader || noop;
    const renderRunProxyEditor = deps.renderRunProxyEditor || noop;
    const renderTaskProxyEditor = deps.renderTaskProxyEditor || noop;
    const renderTaskSupervisionEditor = deps.renderTaskSupervisionEditor || noop;
    const renderTaskContextEditor = deps.renderTaskContextEditor || noop;
    const renderTaskHardwareEditor = deps.renderTaskHardwareEditor || noop;
    const renderAgents = deps.renderAgents || noop;
    const renderSelectedAgentInfo = deps.renderSelectedAgentInfo || noop;
    const renderPendingMessages = deps.renderPendingMessages || noop;
    const renderBackendStatus = deps.renderBackendStatus || noop;
    const renderSessionMenu = deps.renderSessionMenu || noop;
    const renderPanel = deps.renderPanel || noop;
    const renderPanelForRealtime = deps.renderPanelForRealtime || renderPanel;
    const visibleTasks = deps.visibleTasks || (() => []);
    const selectedTaskId = deps.selectedTaskId || (() => null);
    const setSelectedTaskId = deps.setSelectedTaskId || noop;
    const readStoredSelectedTaskId = deps.readStoredSelectedTaskId || (() => null);
    const defaultTaskId = deps.defaultTaskId || (() => null);
    const writeStoredSelectedTaskId = deps.writeStoredSelectedTaskId || noop;
    const isTaskProxyEditing = deps.isTaskProxyEditing || (() => false);
    const isTaskSupervisionEditing = deps.isTaskSupervisionEditing || (() => false);
    const isTaskContextEditing = deps.isTaskContextEditing || (() => false);
    const isTaskHardwareEditing = deps.isTaskHardwareEditing || (() => false);
    const isAgentsPanelEditing = deps.isAgentsPanelEditing || (() => false);
    const setSummaryText = deps.setSummaryText || noop;
    const setSelectedTitle = deps.setSelectedTitle || noop;
    const hideConversationFilters = deps.hideConversationFilters || noop;
    const panelHasTextSelection = deps.panelHasTextSelection || (() => false);
    const windowRef = deps.windowRef || window;
    const statusGoal = deps.statusGoal || (() => "");
    let deferredPanelRender = false;
    let deferredPanelRenderTimer = 0;

    function normalizeSelectedTask() {
      const tasks = visibleTasks();
      let nextTaskId = selectedTaskId();
      if (!nextTaskId) nextTaskId = readStoredSelectedTaskId() || null;
      if (!nextTaskId || !tasks.some(task => task.id === nextTaskId)) nextTaskId = defaultTaskId(tasks);
      setSelectedTaskId(nextTaskId);
      writeStoredSelectedTaskId(nextTaskId);
      return nextTaskId;
    }

    function renderTaskWorkspace(options = {}) {
      renderTaskList();
      renderSelectedHeader();
      if (options.forceTaskProxy || !isTaskProxyEditing()) renderRunProxyEditor();
      if (options.forceTaskProxy || !isTaskProxyEditing()) renderTaskProxyEditor();
      if (options.forceTaskSupervision || !isTaskSupervisionEditing()) renderTaskSupervisionEditor();
      if (options.forceTaskContext || !isTaskContextEditing()) renderTaskContextEditor();
      if (options.forceTaskHardware || !isTaskHardwareEditing()) renderTaskHardwareEditor();
      if (options.forceAgents || !isAgentsPanelEditing()) {
        renderAgents();
      } else {
        renderSelectedAgentInfo();
      }
      renderPendingMessages();
    }

    function renderAll(options = {}) {
      renderSessionSummary();
      setSummaryText(statusGoal());
      normalizeSelectedTask();
      renderTaskWorkspace(options);
    }

    function renderSelectionShell() {
      renderTaskWorkspace({ forceTaskProxy: true, forceTaskSupervision: true, forceTaskContext: true, forceTaskHardware: true, forceAgents: true });
    }

    function renderSelectionPanel(options = {}) {
      renderPendingMessages();
      renderPanel(options);
    }

    function renderEmptyWorkspace(options = {}) {
      renderSessionMenu();
      if (options.summary !== false) renderSessionSummary();
      setSummaryText(options.summaryText || "");
      renderTaskList();
      renderSelectedHeader();
      if (options.selectedTitle) setSelectedTitle(options.selectedTitle);
      renderAgents();
      renderBackendStatus();
      renderPendingMessages();
      hideConversationFilters();
    }

    function renderForEvent(options = {}) {
      if (panelHasTextSelection()) {
        deferredPanelRender = true;
        return false;
      }
      if (deferredPanelRenderTimer) {
        windowRef.clearTimeout(deferredPanelRenderTimer);
        deferredPanelRenderTimer = 0;
      }
      deferredPanelRender = false;
      renderPanelForRealtime(options);
      return true;
    }

    function flushDeferredPanelRender() {
      if (!deferredPanelRender || panelHasTextSelection()) return;
      if (deferredPanelRenderTimer) return;
      deferredPanelRenderTimer = windowRef.setTimeout(() => {
        deferredPanelRenderTimer = 0;
        if (!deferredPanelRender || panelHasTextSelection()) return;
        deferredPanelRender = false;
        renderPanel();
      }, 80);
    }

    function renderActiveTurn() {
      renderTaskList();
      renderSelectedHeader();
      renderForEvent();
    }

    return Object.freeze({
      flushDeferredPanelRender,
      normalizeSelectedTask,
      renderActiveTurn,
      renderAll,
      renderEmptyWorkspace,
      renderForEvent,
      renderSelectionPanel,
      renderSelectionShell,
      renderTaskWorkspace
    });
  }

  window.AHARenderOrchestrator = Object.freeze({ createRenderOrchestrator });
})();
