(() => {
  function createMessageFlow(state = {}, deps = {}) {
    const pendingMessages = state.pendingMessages || [];
    const interruptedContexts = state.interruptedContexts || new Set();
    const terminalTaskStatuses = state.terminalTaskStatuses || new Set();
    let pendingMessageId = 0;
    let pendingSendInFlight = false;

    const currentRunId = deps.currentRunId || (() => "");
    const selectedTaskId = deps.selectedTaskId || (() => "");
    const selectedTask = deps.selectedTask || (() => null);
    const selectedAgent = deps.selectedAgent || (() => null);
    const backendTarget = deps.backendTarget || (() => "main");
    const selectedAgentInputBlocked = deps.selectedAgentInputBlocked || (() => false);
    const selectedBackendActive = deps.selectedBackendActive || (() => false);
    const taskCurrentStatus = deps.taskCurrentStatus || (() => "");
    const isAhaCommand = deps.isAhaCommand || (() => false);
    const isInterruptCommand = deps.isInterruptCommand || (() => false);
    const pendingMessagesEl = deps.pendingMessagesEl || (() => null);
    const escapeHtml = deps.escapeHtml || (value => String(value ?? ""));
    const formatLocalTimestamp = deps.formatLocalTimestamp || ((value, fallback) => value || fallback || "");
    const realtimeDebug = deps.realtimeDebug || (() => {});
    const addOptimisticSendFeedback = deps.addOptimisticSendFeedback || (() => false);
    const clearOptimisticEventsForContext = deps.clearOptimisticEventsForContext || (() => false);
    const renderPanelForRealtime = deps.renderPanelForRealtime || (() => {});
    const prepareRealtimeCatchupBaseline = deps.prepareRealtimeCatchupBaseline || (async () => {});
    const fetchJson = deps.fetchJson || (async () => ({}));
    const apiUrl = deps.apiUrl || (path => path);
    const runScopedPayload = deps.runScopedPayload || (payload => payload);
    const catchUpRealtimeEvents = deps.catchUpRealtimeEvents || (async () => []);
    const loadStatus = deps.loadStatus || (async () => {});
    const renderPanel = deps.renderPanel || (() => {});
    const setConversationAutoFollow = deps.setConversationAutoFollow || (() => {});
    const agentTarget = deps.agentTarget || (() => "main");

    function messageContextKey(taskId = selectedTaskId(), target = backendTarget()) {
      return `${currentRunId() || ""}::${taskId || ""}::${target || "main"}`;
    }

    function pendingForContext(taskId = selectedTaskId(), target = backendTarget()) {
      const key = messageContextKey(taskId, target);
      return pendingMessages.filter(item => item.contextKey === key);
    }

    function renderPendingMessages() {
      const container = pendingMessagesEl();
      if (!container) return;
      const task = selectedTask();
      const target = backendTarget();
      const key = messageContextKey(task?.id, target);
      const items = task ? pendingForContext(task.id, target) : [];
      const interrupted = interruptedContexts.has(key);
      container.classList.toggle("hidden", !items.length && !interrupted);
      if (!items.length && !interrupted) {
        container.innerHTML = "";
        return;
      }
      const note = interrupted
        ? '<div class="pending-note">上一轮已中断。确认 pending 后点 Send，会合并发送下一轮。</div>'
        : '<div class="pending-note">Agent 忙碌或等待中收到的消息会先暂存，当前轮可继续后自动合并发送。</div>';
      const list = items.map((item, index) => `
        <div class="pending-message" data-pending-id="${escapeHtml(item.id)}">
          <div>
            <strong>#${index + 1}</strong>
            <span>${escapeHtml(item.message)}</span>
          </div>
          <button type="button" class="pending-remove" data-remove-pending="${escapeHtml(item.id)}" title="删除 pending 消息">Delete</button>
        </div>
      `).join("");
      container.innerHTML = `${note}${list}`;
    }

    function addPendingMessage(message, task, agentId) {
      const target = agentId || "main";
      pendingMessageId += 1;
      pendingMessages.push({
        id: String(pendingMessageId),
        contextKey: messageContextKey(task.id, target),
        runId: currentRunId(),
        taskId: task.id,
        agentId: target,
        role: target === "main" ? "main" : "sub",
        message,
        createdAt: new Date().toISOString()
      });
      renderPendingMessages();
    }

    function removePendingMessage(id) {
      const index = pendingMessages.findIndex(item => item.id === String(id));
      if (index >= 0) pendingMessages.splice(index, 1);
      renderPendingMessages();
    }

    function clearPendingForContext(taskId, target) {
      const key = messageContextKey(taskId, target);
      for (let index = pendingMessages.length - 1; index >= 0; index -= 1) {
        if (pendingMessages[index].contextKey === key) pendingMessages.splice(index, 1);
      }
    }

    function clearPendingState() {
      pendingMessages.length = 0;
      interruptedContexts.clear();
      pendingSendInFlight = false;
    }

    function markInterruptedContext(taskId, target) {
      interruptedContexts.add(messageContextKey(taskId, target));
      renderPendingMessages();
    }

    function mergedPendingPrompt(items, currentMessage, interrupted) {
      const lines = [];
      if (interrupted) {
        lines.push(
          "上一轮 agent 工作被用户中断。",
          "继续前请注意：当前工作区或命令可能已有部分副作用，请基于当前实际状态判断后继续。",
          ""
        );
      }
      if (items.length) {
        lines.push("用户在你工作期间补充了以下消息，请按时间顺序合并理解并继续处理：");
        items.forEach((item, index) => {
          lines.push(`${index + 1}. [${formatLocalTimestamp(item.createdAt, item.createdAt)}] ${item.message}`);
        });
      }
      if (currentMessage) {
        if (items.length) lines.push("", "用户当前发送的新消息：");
        lines.push(currentMessage);
      }
      if (!items.length && !currentMessage && interrupted) {
        lines.push("用户中断了上一轮，但没有补充新消息。");
      }
      return lines.join("\n").trim();
    }

    async function sendBackendMessage(task, agentId, message) {
      const target = agentId === "main" ? "main" : agentId;
      const optimistic = addOptimisticSendFeedback(task, target, message);
      realtimeDebug("send.request", { task_id: task.id, target, message_len: message.length });
      await prepareRealtimeCatchupBaseline();
      try {
        const response = await fetchJson(apiUrl("/api/send"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(runScopedPayload({
            target,
            role: agentId === "main" ? "main" : "sub",
            task_id: task.id,
            from_agent: "browser",
            to_agent: agentId,
            message,
            sender: "browser"
          }))
        }, "Failed to send message");
        realtimeDebug("send.response", {
          task_id: task.id,
          target,
          ok: Boolean(response?.ok),
          handled_by: response?.handled_by || "",
          backend_started: Boolean(response?.backend),
          interrupted: Boolean(response?.interrupt?.interrupted),
          deferred: Boolean(response?.deferred),
          reason: response?.reason || ""
        });
        if (response?.deferred && optimistic) {
          clearOptimisticEventsForContext(task.id, target);
          renderPanelForRealtime();
        }
        return response;
      } catch (err) {
        if (optimistic) {
          clearOptimisticEventsForContext(task.id, target);
          renderPanelForRealtime();
        }
        realtimeDebug("send.error", { task_id: task.id, target, error: err?.message || String(err) });
        throw err;
      }
    }

    async function flushPendingMessages(task, agentId, currentMessage = "", options = {}) {
      const target = agentId || "main";
      const key = messageContextKey(task.id, target);
      const items = pendingForContext(task.id, target);
      const interrupted = interruptedContexts.has(key);
      if (pendingSendInFlight) {
        realtimeDebug("flush.deferred", {
          task_id: task.id,
          target,
          reason: "send_in_flight",
          message_len: currentMessage.length,
          pending_count: items.length,
          interrupted
        });
        if (currentMessage) addPendingMessage(currentMessage, task, target);
        renderPendingMessages();
        return { ok: true, deferred: true, reason: "send_in_flight" };
      }
      if (!items.length && !currentMessage && !interrupted) return null;
      if (options.auto && interrupted) {
        realtimeDebug("flush.skip", { task_id: task.id, target, reason: "interrupted_auto" });
        return null;
      }
      if (options.auto && terminalTaskStatuses.has(taskCurrentStatus(task))) {
        realtimeDebug("flush.skip", { task_id: task.id, target, reason: "terminal_auto", status: taskCurrentStatus(task) });
        return null;
      }
      pendingSendInFlight = true;
      try {
        const message = items.length || interrupted ? mergedPendingPrompt(items, currentMessage, interrupted) : currentMessage;
        const response = await sendBackendMessage(task, target, message);
        if (response?.deferred) {
          if (currentMessage) addPendingMessage(currentMessage, task, target);
          renderPendingMessages();
          return response;
        }
        clearPendingForContext(task.id, target);
        interruptedContexts.delete(key);
        renderPendingMessages();
        return response;
      } finally {
        pendingSendInFlight = false;
      }
    }

    async function maybeAutoFlushPending() {
      const task = selectedTask();
      if (!task || selectedAgentInputBlocked()) return null;
      const agentId = backendTarget();
      if (!pendingForContext(task.id, agentId).length) return null;
      const response = await flushPendingMessages(task, agentId, "", { auto: true });
      if (!response) return null;
      await catchUpRealtimeEvents();
      setConversationAutoFollow(true);
      return response;
    }

    async function handleComposerSubmit({ task, message }) {
      const agentId = agentTarget() || "main";
      const isAha = isAhaCommand(message);
      realtimeDebug("composer.submit", {
        task_id: task.id,
        target: agentId,
        is_aha: isAha,
        backend_active: selectedBackendActive(),
        input_blocked: Boolean(selectedAgentInputBlocked()),
        message_len: message.length
      });
      let response = null;
      if (selectedAgentInputBlocked() && !isAha) {
        realtimeDebug("composer.pending", { task_id: task.id, target: agentId, reason: "input_blocked", message_len: message.length });
        addPendingMessage(message, task, agentId);
      } else if (isAha) {
        response = await sendBackendMessage(task, agentId, message);
        if (isInterruptCommand(message) && response?.interrupt?.interrupted) {
          interruptedContexts.add(messageContextKey(task.id, agentId));
        }
      } else {
        response = await flushPendingMessages(task, agentId, message);
        if (!response && message) {
          realtimeDebug("composer.send_fallback", { task_id: task.id, target: agentId, reason: "flush_returned_null", message_len: message.length });
          response = await sendBackendMessage(task, agentId, message);
        }
      }
      const accepted = await catchUpRealtimeEvents();
      realtimeDebug("composer.catchup_complete", { accepted_count: accepted.length });
      await loadStatus({ forceAgents: Boolean(response?.interrupt) });
      setConversationAutoFollow(true);
      renderPendingMessages();
      renderPanel();
    }

    async function interruptBackend(task, agentId) {
      const target = agentId || "main";
      const response = await sendBackendMessage(task, target, "/aha interrupt");
      if (response?.interrupt?.interrupted) markInterruptedContext(task.id, target);
      const accepted = await catchUpRealtimeEvents();
      realtimeDebug("interrupt.catchup_complete", { accepted_count: accepted.length });
      await loadStatus({ forceAgents: true });
      renderPendingMessages();
      renderPanel();
      return response;
    }

    return Object.freeze({
      messageContextKey,
      pendingForContext,
      renderPendingMessages,
      addPendingMessage,
      removePendingMessage,
      clearPendingForContext,
      clearPendingState,
      markInterruptedContext,
      mergedPendingPrompt,
      sendBackendMessage,
      flushPendingMessages,
      maybeAutoFlushPending,
      handleComposerSubmit,
      interruptBackend
    });
  }

  window.AHAMessageFlow = Object.freeze({ createMessageFlow });
})();
