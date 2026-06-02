(() => {
  function selectedTaskStorageKey(runId) {
    return runId ? `aha:selected-task-id:${runId}` : "";
  }

  function readStoredSelectedTaskId(runId, storage = window.localStorage) {
    const key = selectedTaskStorageKey(runId);
    if (!key) return "";
    try {
      return String(storage?.getItem(key) || "").trim();
    } catch (_err) {
      return "";
    }
  }

  function writeStoredSelectedTaskId(runId, taskId, storage = window.localStorage) {
    const key = selectedTaskStorageKey(runId);
    if (!key) return;
    try {
      const value = String(taskId || "").trim();
      if (value) {
        storage?.setItem(key, value);
      } else {
        storage?.removeItem(key);
      }
    } catch (_err) {
      // localStorage can be disabled; task selection still works for this page session.
    }
  }

  function selectedTask(statusData, selectedTaskId) {
    return (statusData?.tasks || []).find(task => task.id === selectedTaskId) || null;
  }

  function selectedAgent(task, agentId) {
    return (task?.agents || []).find(item => item.id === agentId) || null;
  }

  function selectedTaskRealtimeActive(task, latestTurnTiming, selectedAgentInputBlocked, taskActivityStatus) {
    const turn = task ? latestTurnTiming(task.id) : null;
    return Boolean(
      task &&
      (taskActivityStatus(task) !== "idle" || turn?.running || selectedAgentInputBlocked())
    );
  }

  window.AHASelectionState = Object.freeze({
    readStoredSelectedTaskId,
    selectedAgent,
    selectedTask,
    selectedTaskRealtimeActive,
    selectedTaskStorageKey,
    writeStoredSelectedTaskId
  });
})();
