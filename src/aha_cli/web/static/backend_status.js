(() => {
  function escapeFallback(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function createBackendStatusController(elements = {}, deps = {}) {
    function escapeHtml(value) {
      return (deps.escapeHtml || escapeFallback)(value);
    }

    function selectedBackendActive() {
      const state = deps.backendStatusData?.();
      const status = String(state?.status || deps.agentBackendProcessStatus?.(deps.selectedAgent?.()) || "stopped").toLowerCase();
      return status === "busy";
    }

    function agentInputWaitBlocked(agent) {
      if (deps.agentLifecycleStatus?.(agent) !== "waiting") return false;
      const reason = deps.agentWaitingReason?.(agent);
      return !reason || reason === "subagents" || reason === "host" || reason === "agent_start";
    }

    function supervisionHostReviewActive(host) {
      const status = deps.agentLifecycleStatus?.(host);
      if (status === "running" || status === "waiting") return true;
      if (status !== "pending") return false;
      if (host?.backend_session_id) return true;
      const processStatus = deps.agentBackendProcessStatus?.(host);
      return processStatus === "running" || processStatus === "busy";
    }

    function taskHostInputBlocked(task) {
      const host = (task?.agents || []).find(agent => deps.isSupervisionAgent?.(agent));
      return supervisionHostReviewActive(host);
    }

    function selectedAgentInputBlocked() {
      return selectedBackendActive() || agentInputWaitBlocked(deps.selectedAgent?.()) || taskHostInputBlocked(deps.selectedTask?.());
    }

    function renderBackendStatus() {
      if (!elements.backendStatusEl) return;
      if (!deps.currentRunId?.()) {
        elements.backendStatusEl.className = "backend-status pending";
        elements.backendStatusEl.innerHTML = `
          <span class="activity-dot"></span>
          <strong>Backend</strong>
          <code>waiting for run</code>
        `;
        return;
      }
      const state = deps.backendStatusData?.();
      if (!state) {
        elements.backendStatusEl.className = "backend-status pending";
        elements.backendStatusEl.innerHTML = `
          <span class="activity-dot"></span>
          <strong>Backend</strong>
          <code>loading</code>
        `;
        return;
      }
      const status = state.status || "stopped";
      const formatLocalTimestamp = deps.formatLocalTimestamp || ((value, fallback) => fallback || value || "");
      const detail = [
        state.backend || "backend",
        state.pid ? `pid=${state.pid}` : "",
        state.last_reply_at ? `last reply ${formatLocalTimestamp(state.last_reply_at, state.last_reply_at)}` : ""
      ].filter(Boolean).join(" | ");
      elements.backendStatusEl.className = `backend-status ${escapeHtml(status)}`;
      const canInterrupt = status === "busy";
      elements.backendStatusEl.innerHTML = `
        <span class="activity-dot"></span>
        <strong>${escapeHtml(status)}</strong>
        <code title="${escapeHtml(detail)}">${escapeHtml(detail)}</code>
        ${canInterrupt ? '<button class="interrupt-button" type="button" data-backend-action="interrupt">Interrupt</button>' : ""}
      `;
    }

    return Object.freeze({
      agentInputWaitBlocked,
      renderBackendStatus,
      selectedAgentInputBlocked,
      selectedBackendActive,
      taskHostInputBlocked
    });
  }

  window.AHABackendStatus = Object.freeze({
    createBackendStatusController
  });
})();
