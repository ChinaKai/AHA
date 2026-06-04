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

  function createRemoteSelectedTaskState(deps = {}) {
    const apiUrl = deps.apiUrl || (path => path);
    const fetchJson = deps.fetchJson || (() => Promise.resolve({}));
    const fetchWithTimeout = deps.fetchWithTimeout || window.fetch.bind(window);
    const currentRunId = deps.currentRunId || (() => "");
    const consoleRef = deps.consoleRef || window.console;
    const cache = new Map();

    function runId() {
      return String(currentRunId() || "").trim();
    }

    function cacheKey() {
      return runId();
    }

    function cachedValue() {
      const key = cacheKey();
      return key && cache.has(key) ? cache.get(key) : null;
    }

    async function readSelectedTaskId(options = {}) {
      const key = cacheKey();
      if (!key) return "";
      if (!options.force && cache.has(key)) return cache.get(key);
      try {
        const payload = await fetchJson(apiUrl("/api/ui-state"), {}, "Failed to load UI state");
        const value = String(payload?.last_selected_task_id || "").trim();
        cache.set(key, value);
        return value;
      } catch (err) {
        consoleRef?.warn?.("Failed to load UI state", err);
        cache.set(key, "");
        return "";
      }
    }

    function writeSelectedTaskId(taskId) {
      const key = cacheKey();
      if (!key) return;
      const value = String(taskId || "").trim();
      if (cache.has(key) && cache.get(key) === value) return;
      cache.set(key, value);
      fetchWithTimeout(apiUrl("/api/ui-state"), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ last_selected_task_id: value })
      }).catch(err => consoleRef?.warn?.("Failed to save UI state", err));
    }

    function reset(runId = "") {
      const key = String(runId || "").trim();
      if (key) cache.delete(key);
      else cache.clear();
    }

    return Object.freeze({
      cachedValue,
      readSelectedTaskId,
      reset,
      writeSelectedTaskId
    });
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
    createRemoteSelectedTaskState,
    readStoredSelectedTaskId,
    selectedAgent,
    selectedTask,
    selectedTaskRealtimeActive,
    selectedTaskStorageKey,
    writeStoredSelectedTaskId
  });
})();
