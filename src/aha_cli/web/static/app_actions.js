(() => {
  function createAppActions(deps = {}) {
    const confirmDialogAction = deps.confirmDialogAction || (() => Promise.resolve(true));
    const alertUser = deps.alert || (message => window.alert(message));
    const renderOrchestrator = deps.renderOrchestrator || {};
    const selectedTaskId = deps.selectedTaskId || (() => null);
    const setSelectedTaskId = deps.setSelectedTaskId || (() => {});
    const writeStoredSelectedTaskId = deps.writeStoredSelectedTaskId || (() => {});
    const activeTab = deps.activeTab || (() => "conversation");
    const logState = deps.logState || (() => ({}));
    const setConversationAutoFollow = deps.setConversationAutoFollow || (() => {});
    const setTaskActionInFlight = deps.setTaskActionInFlight || (() => {});
    const fetchWithTimeout = deps.fetchWithTimeout || window.fetch.bind(window);
    const apiUrl = deps.apiUrl || (path => path);
    const loadStatus = deps.loadStatus || (() => Promise.resolve());
    const loadAgentsRuntime = deps.loadAgentsRuntime || (() => Promise.resolve());
    const ensureActiveTabData = deps.ensureActiveTabData || (() => Promise.resolve());
    const hardwareIoState = deps.hardwareIoState || (() => ({}));

    async function selectTask(taskId) {
      const changedTask = selectedTaskId() !== taskId;
      setSelectedTaskId(taskId);
      writeStoredSelectedTaskId(taskId);
      if (changedTask) deps.resetEventWebSocketReconnectState?.("task_selected");
      deps.resetTaskConfigEditing?.();
      setConversationAutoFollow(true);
      if (activeTab() === "logs") logState(taskId).autoFollow = true;
      if (activeTab() === "hardware") hardwareIoState(taskId).autoFollow = true;
      deps.closeMobileSheets?.();
      deps.closeMobileActionPanel?.();
      renderOrchestrator.renderSelectionShell?.();
      await Promise.all([loadAgentsRuntime(), ensureActiveTabData()]);
      renderOrchestrator.renderSelectionPanel?.();
    }

    async function updateTaskVisibility(taskId, action) {
      const confirmPayloads = {
        delete: {
          title: "Delete task?",
          message: "This removes the task from the task list.",
          confirmLabel: "Delete",
          danger: true,
          details: [["Task", taskId]]
        },
        hide: {
          title: "Hide task?",
          message: "You can restore hidden tasks later.",
          confirmLabel: "Hide",
          details: [["Task", taskId]]
        },
        final: {
          title: "Generate Final?",
          message: "Ask task-main to produce the final answer for this task.",
          confirmLabel: "Ask Final",
          details: [["Task", taskId]]
        },
        complete: {
          title: "Complete task?",
          message: "Ask task-main to produce the final answer for this task.",
          confirmLabel: "Complete",
          details: [["Task", taskId]]
        }
      };
      if (confirmPayloads[action] && !await confirmDialogAction(confirmPayloads[action])) return;
      setTaskActionInFlight(true);
      try {
        const res = await fetchWithTimeout(apiUrl(`/api/task/${encodeURIComponent(taskId)}/${action}`), { method: "POST" });
        if (!res.ok) {
          const payload = await res.json().catch(() => ({}));
          alertUser(payload.error || `Task action failed: ${action}`);
          return;
        }
        if (action === "restore" || action === "final" || action === "complete" || action === "reopen") setSelectedTaskId(taskId);
        if (action === "hide" || action === "delete") setSelectedTaskId(null);
        writeStoredSelectedTaskId(selectedTaskId());
        await loadStatus();
        renderOrchestrator.renderSelectionPanel?.();
      } finally {
        setTaskActionInFlight(false);
      }
    }

    function dispatch(action, payload = {}) {
      if (action === "select-task") return selectTask(payload.taskId || payload);
      if (action === "task-visibility") return updateTaskVisibility(payload.taskId, payload.action);
      if (action === "settings-save") return deps.saveBootstrapConfigForm?.(payload.form || payload);
      if (action === "web-restart") return deps.restartWebService?.();
      if (action === "run-maintenance-refresh") return deps.loadRunMaintenance?.(true);
      if (action === "run-maintenance-action") return deps.runMaintenanceAction?.(payload.action, payload.detail || {});
      return undefined;
    }

    return Object.freeze({
      dispatch,
      selectTask,
      updateTaskVisibility
    });
  }

  window.AHAAppActions = Object.freeze({ createAppActions });
})();
