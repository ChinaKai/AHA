(() => {
  function createCompactResetController(state = {}, deps = {}) {
    const compactResetStates = state.compactResetStates || new Map();
    const resetTimeoutMs = Math.max(Number(deps.requestTimeoutMs?.() || 0), 60000);
    const verifyTimeoutMs = Math.max(Number(deps.verifyTimeoutMs?.() || 0), 30000);
    const windowRef = deps.windowRef || window;
    const alertUser = deps.alert || (message => windowRef.alert(message));

    function agentStatusSession(taskId, agentId) {
      const task = (deps.tasks?.() || []).find(item => item.id === taskId);
      return (task?.agents || []).find(item => item.id === agentId) || null;
    }

    function compactResetLooksComplete(taskId, agentId, previousSessionId, afterOrder) {
      return deps.compactResetLooksComplete?.(taskId, agentId, previousSessionId, afterOrder);
    }

    function sleep(ms) {
      return new Promise(resolve => windowRef.setTimeout(resolve, ms));
    }

    function setResetState(stateKey, value) {
      compactResetStates.set(stateKey, value);
      deps.renderPanel?.();
    }

    function clearDoneStateLater(stateKey) {
      windowRef.setTimeout(() => {
        const state = compactResetStates.get(stateKey);
        if (state?.label === "done") {
          compactResetStates.delete(stateKey);
          deps.renderPanel?.();
        }
      }, 2200);
    }

    async function refreshCompactResetStatus(taskId, agentId) {
      await deps.catchUpRealtimeEvents?.();
      await deps.loadStatus?.({ forceAgents: true });
      await deps.loadConversationPage?.(taskId, agentId, false, true);
    }

    async function verifyCompactResetAfterTimeout(taskId, agentId, previousSessionId, afterOrder) {
      const deadline = Date.now() + verifyTimeoutMs;
      while (Date.now() <= deadline) {
        await refreshCompactResetStatus(taskId, agentId);
        if (compactResetLooksComplete(taskId, agentId, previousSessionId, afterOrder)) return true;
        await sleep(1000);
      }
      return false;
    }

    async function compactResetSelectedSession() {
      const task = deps.selectedTask?.();
      if (!task) return;
      const agentId = deps.backendTarget?.();
      const stateKey = deps.promptMetricsKey?.(task.id, agentId);
      const previousSessionId = String(
        deps.conversationBackendSession?.(task.id, agentId)?.id ||
        agentStatusSession(task.id, agentId)?.backend_session_id ||
        ""
      );
      const actionStartOrder = deps.latestKnownEventOrder?.();
      const confirmed = await deps.confirmDialogAction?.({
        title: "Compact and reset session?",
        message: "AHA will archive the current backend session, write a compact summary, and restart a fresh backend session.",
        confirmLabel: "Compact reset",
        details: [
          ["Task", task.id],
          ["Agent", agentId],
          ["Previous session", previousSessionId || "-"]
        ]
      });
      if (!confirmed) return;
      setResetState(stateKey, { label: "compacting", className: "session-pending", buttonLabel: "Compacting" });
      try {
        const res = await deps.fetchWithTimeout?.(deps.apiUrl?.(`/api/task/${encodeURIComponent(task.id)}/session/compact-reset`), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target: agentId, reason: "manual", restart: true })
        }, resetTimeoutMs);
        await deps.readJsonResponse?.(res, "Failed to compact-reset backend session");
        setResetState(stateKey, { label: "restarting", className: "session-pending", buttonLabel: "Restarting" });
        await refreshCompactResetStatus(task.id, agentId);
        setResetState(stateKey, { label: "done", className: "session-done", buttonLabel: "Done" });
        clearDoneStateLater(stateKey);
      } catch (err) {
        if (deps.isRequestTimeoutError?.(err)) {
          setResetState(stateKey, { label: "checking", className: "session-pending", buttonLabel: "Checking" });
          let completed = false;
          try {
            completed = await verifyCompactResetAfterTimeout(task.id, agentId, previousSessionId, actionStartOrder);
          } catch (verifyErr) {
            console.warn("Compact-reset verification failed", verifyErr);
          }
          if (completed) {
            setResetState(stateKey, { label: "done", className: "session-done", buttonLabel: "Done" });
            clearDoneStateLater(stateKey);
            return;
          }
        }
        setResetState(stateKey, { label: "failed", className: "session-error", buttonLabel: "Retry" });
        alertUser(err.message || String(err));
        windowRef.setTimeout(() => {
          const state = compactResetStates.get(stateKey);
          if (state?.label === "failed") {
            compactResetStates.delete(stateKey);
            deps.renderPanel?.();
          }
        }, 4000);
      }
    }

    return Object.freeze({
      agentStatusSession,
      compactResetLooksComplete,
      compactResetSelectedSession,
      refreshCompactResetStatus,
      verifyCompactResetAfterTimeout
    });
  }

  window.AHACompactReset = Object.freeze({ createCompactResetController });
})();
