(() => {
  function createAppBridge(state = {}, controllers = {}, deps = {}) {
    const storage = deps.localStorage || window.localStorage;

    function currentRunId() {
      return String(state.currentRunId?.() || "").trim();
    }

    function setCurrentRunId(value) {
      state.setCurrentRunId?.(String(value || ""));
    }

    function selectedTaskId() {
      return state.selectedTaskId?.() || null;
    }

    function setSelectedTaskId(value) {
      state.setSelectedTaskId?.(value || null);
    }

    function statusData() {
      return state.statusData?.() || null;
    }

    function setStatusData(value) {
      state.setStatusData?.(value || null);
    }

    function backendTargetValue() {
      return deps.agentTargetEl?.()?.value || "main";
    }

    function renderPanelForRealtime(options = {}) {
      return controllers.renderOrchestrator?.()?.renderForEvent(options);
    }

    function flushDeferredPanelRender() {
      controllers.renderOrchestrator?.()?.flushDeferredPanelRender();
    }

    function readStoredSelectedTaskId(runId = currentRunId()) {
      return deps.readStoredSelectedTaskIdForRun?.(runId, storage);
    }

    function writeStoredSelectedTaskId(taskId, runId = currentRunId()) {
      deps.writeStoredSelectedTaskIdForRun?.(runId, taskId, storage);
    }

    function runScopedPayload(payload = {}) {
      return controllers.statusController?.()?.runScopedPayload(payload);
    }

    function realtimeDebug(stage, detail = {}) {
      return controllers.realtimeClient?.()?.debug(stage, detail);
    }

    function applyRunListData(payload = {}) {
      controllers.statusController?.()?.applyRunListData(payload);
    }

    function applyWorkspaceData(workspaces = []) {
      controllers.statusController?.()?.applyWorkspaceData(workspaces);
    }

    function applyBootstrapPayload(payload = {}) {
      controllers.statusController?.()?.applyBootstrapPayload(payload);
    }

    async function loadBootstrap() {
      return await controllers.statusController?.()?.loadBootstrap();
    }

    function currentRunSummary() {
      return controllers.statusController?.()?.currentRunSummary();
    }

    function fallbackCurrentRun() {
      return controllers.statusController?.()?.fallbackCurrentRun();
    }

    function syncRunUrl() {
      controllers.statusController?.()?.syncRunUrl();
    }

    function resetRunScopedState() {
      controllers.statusController?.()?.resetRunScopedState();
    }

    function realtimeTransportText() {
      return controllers.realtimeClient?.()?.transportText();
    }

    function refreshRealtimeIndicator() {
      if (deps.runStateEl?.() && (statusData() || currentRunId())) renderSessionSummary();
    }

    function currentAppVersion() {
      return controllers.statusController?.()?.currentAppVersion();
    }

    function renderSessionSummary() {
      controllers.runController?.()?.renderSessionSummary();
    }

    function syncCurrentRunDisplay(run, fallbackName = "") {
      controllers.runController?.()?.syncCurrentRunDisplay(run, fallbackName);
    }

    function setRunLifecycleState(message, isError = false) {
      controllers.runController?.()?.setRunLifecycleState(message, isError);
    }

    function resetRunMaintenanceState() {
      controllers.runController?.()?.resetRunMaintenanceState();
    }

    function runMaintenancePayload() {
      return controllers.runController?.()?.runMaintenancePayload();
    }

    function renderRunMaintenance() {
      controllers.runController?.()?.renderRunMaintenance();
    }

    async function loadRunMaintenance(force = false) {
      await controllers.runController?.()?.loadRunMaintenance(force);
    }

    function renderAccessControlStatus() {
      controllers.accessControlController?.()?.renderAccessControlStatus();
    }

    async function loadAccessControlStatus() {
      await controllers.accessControlController?.()?.loadAccessControlStatus();
    }

    function renderSessionMenu() {
      controllers.runController?.()?.renderSessionMenu();
    }

    async function loadRuns(force = false) {
      await controllers.statusController?.()?.loadRuns(force);
    }

    function setSessionMenu(open) {
      controllers.runController?.()?.setSessionMenu(open);
    }

    function setRunMaintenanceConsoleOpen(open) {
      controllers.runController?.()?.setRunMaintenanceConsoleOpen(open);
    }

    function setWeixinConsoleOpen(open) {
      controllers.weixinConsoleController?.()?.setWeixinConsoleOpen(open);
    }

    function renderPlayConsolePopover() {
      controllers.playConsoleController?.()?.renderPlayConsolePopover();
    }

    function setPlayConsoleOpen(open) {
      controllers.playConsoleController?.()?.setPlayConsoleOpen(open);
    }

    function renderSkillsConsolePopover() {
      controllers.skillsConsoleController?.()?.renderSkillsConsolePopover();
    }

    function setSkillsConsoleOpen(open) {
      controllers.skillsConsoleController?.()?.setSkillsConsoleOpen(open);
    }

    function renderWeixinConsolePopover() {
      controllers.weixinConsoleController?.()?.renderWeixinConsolePopover();
    }

    async function refreshRunScopedView() {
      if (!currentRunId()) {
        renderFirstRunState();
        return;
      }
      await loadStatus({ forceAgents: true });
      await ensureConversationLoaded();
      await syncRealtimeEvents();
      await controllers.taskMemoController?.()?.refreshIfOpen?.();
      controllers.panelController?.()?.renderPanel({ preserveContextScroll: true });
    }

    async function switchRun(runId) {
      const nextRunId = String(runId || "").trim();
      const keepSettingsHome = deps.documentRef?.body?.classList?.contains("settings-home");
      if (!nextRunId || nextRunId === currentRunId()) {
        renderSessionMenu();
        if (keepSettingsHome) setSessionMenu(true);
        else setSessionMenu(false);
        return;
      }
      state.setRunActionInFlight?.(true);
      try {
        setCurrentRunId(nextRunId);
        deps.writeSelectedRunId?.(nextRunId);
        syncRunUrl();
        resetRunScopedState();
        renderSessionMenu();
        if (keepSettingsHome) setSessionMenu(true);
        else setSessionMenu(false);
        await refreshRunScopedView();
      } catch (err) {
        const panelEl = deps.panelEl?.();
        if (panelEl) panelEl.innerHTML = `<pre>${deps.escapeHtml?.(String(err)) || String(err)}</pre>`;
      } finally {
        state.setRunActionInFlight?.(false);
        renderSessionMenu();
      }
    }

    function setRunArchiveState(message, isError = false) {
      controllers.runController?.()?.setRunArchiveState(message, isError);
    }

    function setWebRestartState(message, isError = false) {
      controllers.runController?.()?.setWebRestartState(message, isError);
    }

    function initSettingsDialog() {
      controllers.settingsController?.()?.bind();
    }

    function initSessionControl() {
      controllers.runController?.()?.bind();
      controllers.weixinConsoleController?.()?.bind();
      controllers.skillsConsoleController?.()?.bind();
      controllers.tokenUsageController?.()?.bind();
    }

    function selectedTask() {
      return controllers.appSelectors?.()?.selectedTask();
    }

    function selectedTaskNeedsAgentDetails(task = selectedTask()) {
      return controllers.appSelectors?.()?.selectedTaskNeedsAgentDetails(task);
    }

    function selectedAgent() {
      return controllers.appSelectors?.()?.selectedAgent();
    }

    function backendTarget() {
      return controllers.appSelectors?.()?.backendTarget() || backendTargetValue();
    }

    function isAhaCommand(message) {
      return controllers.appSelectors?.()?.isAhaCommand(message);
    }

    function isInterruptCommand(message) {
      return controllers.appSelectors?.()?.isInterruptCommand(message);
    }

    function selectedTaskRealtimeActive() {
      return controllers.appSelectors?.()?.selectedTaskRealtimeActive();
    }

    function agentStatusTiming(agent) {
      return controllers.appSelectors?.()?.agentStatusTiming(agent);
    }

    function agentStatusTimingText(agent) {
      return controllers.appSelectors?.()?.agentStatusTimingText(agent);
    }

    function visibleTasks() {
      return controllers.taskController?.()?.visibleTasks();
    }

    function renderTaskVisibilityFilter() {
      controllers.taskController?.()?.renderTaskVisibilityFilter();
    }

    function runHasNoTasks() {
      return Boolean(currentRunId()) && (statusData()?.tasks || []).length === 0;
    }

    function isAgentsPanelEditing() {
      const active = deps.documentRef?.activeElement;
      const agentsEl = deps.agentsEl?.();
      const agentTargetEl = deps.agentTargetEl?.();
      return (
        controllers.agentController?.()?.isAgentsPanelEditing() ||
        (active instanceof Element && (agentsEl?.contains(active) || agentTargetEl?.contains(active)))
      );
    }

    function clearOptimisticEventsForContext(taskId, target) {
      return controllers.optimisticEvents?.()?.clearOptimisticEventsForContext(taskId, target);
    }

    function removeOptimisticEventsMatchedBy(events) {
      return controllers.optimisticEvents?.()?.removeOptimisticEventsMatchedBy(events);
    }

    function addOptimisticSendFeedback(task, target, message) {
      return controllers.optimisticEvents?.()?.addOptimisticSendFeedback(task, target, message);
    }

    function conversationBackendSession(taskId, target = backendTarget()) {
      const stateForConversation = deps.conversationStates?.get(deps.conversationKey?.(taskId, target));
      return stateForConversation?.backendSession || null;
    }

    function backendSessionId(session) {
      return String(session?.id || session?.backend_session_id || "").trim();
    }

    function backendSessionIdsDiffer(nextSession, previousSession) {
      const nextId = backendSessionId(nextSession);
      const previousId = backendSessionId(previousSession);
      return Boolean(nextId || previousId) && nextId !== previousId;
    }

    function backendSessionWithPreviousContextPressure(nextSession, previousSession) {
      if (!nextSession) return previousSession || null;
      if (deps.contextPressureHasPercent?.(nextSession.context_pressure)) return nextSession;
      if (backendSessionIdsDiffer(nextSession, previousSession)) return nextSession;
      if (!deps.contextPressureHasPercent?.(previousSession?.context_pressure)) return nextSession;
      return { ...nextSession, context_pressure: previousSession.context_pressure };
    }

    function promptMetricsKey(taskId, target = backendTarget()) {
      return controllers.conversationController?.()?.promptMetricsKey(taskId, target);
    }

    function renderRawPromptSection(data = {}, total = {}) {
      return controllers.conversationController?.()?.renderRawPromptSection(data, total);
    }

    function captureContextScrollState() {
      return controllers.conversationController?.()?.captureContextScrollState();
    }

    function restoreContextScrollState(stateToRestore) {
      controllers.conversationController?.()?.restoreContextScrollState(stateToRestore);
    }

    function promptMetricsState(taskId) {
      return controllers.conversationController?.()?.promptMetricsState(taskId);
    }

    function renderPromptMetricsPanel(taskId) {
      return controllers.conversationController?.()?.renderPromptMetricsPanel(taskId);
    }

    function renderPromptMetricsPopover(taskId) {
      return controllers.conversationController?.()?.renderPromptMetricsPopover(taskId);
    }

    function renderPromptMetricsDock(taskId) {
      return controllers.conversationController?.()?.renderPromptMetricsDock(taskId);
    }

    function latestKnownEventOrder() {
      return controllers.conversationController?.()?.latestKnownEventOrder();
    }

    function agentStatusSession(taskId, agentId) {
      return controllers.compactResetController?.()?.agentStatusSession(taskId, agentId);
    }

    function compactResetLooksComplete(taskId, agentId, previousSessionId, afterOrder) {
      return controllers.compactResetController?.()?.compactResetLooksComplete(taskId, agentId, previousSessionId, afterOrder);
    }

    async function refreshCompactResetStatus(taskId, agentId) {
      await controllers.compactResetController?.()?.refreshCompactResetStatus(taskId, agentId);
    }

    async function verifyCompactResetAfterTimeout(taskId, agentId, previousSessionId, afterOrder) {
      return await controllers.compactResetController?.()?.verifyCompactResetAfterTimeout(taskId, agentId, previousSessionId, afterOrder);
    }

    async function compactResetSelectedSession() {
      await controllers.compactResetController?.()?.compactResetSelectedSession();
    }

    function taskTimingLabel(taskId, task) {
      return controllers.appSelectors?.()?.taskTimingLabel(taskId, task);
    }

    function taskMetaTiming(taskId, task) {
      return controllers.appSelectors?.()?.taskMetaTiming(taskId, task);
    }

    function latestTurnTiming(taskId) {
      return controllers.appSelectors?.()?.latestTurnTiming(taskId);
    }

    function renderModelOptions() {
      controllers.runtimeOptions?.()?.renderModelOptions();
    }

    function applyStatusData(options = {}) {
      controllers.statusController?.()?.applyStatusData(options);
    }

    async function loadStatus(options = {}) {
      return await controllers.statusController?.()?.loadStatus(options);
    }

    async function loadAgentsRuntime(options = {}) {
      return await controllers.statusController?.()?.loadAgentsRuntime(options);
    }

    function logState(taskId) {
      return controllers.conversationController?.()?.logState(taskId);
    }

    function hardwareIoState(taskId) {
      return controllers.conversationController?.()?.hardwareIoState(taskId);
    }

    async function ensureActiveTabData() {
      return await controllers.conversationController?.()?.ensureActiveTabData();
    }

    async function loadOlderLogs() {
      return await controllers.conversationController?.()?.loadOlderLogs();
    }

    async function loadHardwareIoPage(taskId = selectedTaskId(), force = false) {
      return await controllers.conversationController?.()?.loadHardwareIoPage(taskId, force);
    }

    async function loadConversationPage(taskId = selectedTaskId(), target = backendTarget(), older = false, force = false) {
      return await controllers.conversationController?.()?.loadConversationPage(taskId, target, older, force);
    }

    async function initializeEventTailOffset() {
      return await controllers.conversationController?.()?.initializeEventTailOffset();
    }

    async function prepareRealtimeCatchupBaseline() {
      return await controllers.conversationController?.()?.prepareRealtimeCatchupBaseline();
    }

    async function ensureConversationLoaded() {
      return await controllers.conversationController?.()?.ensureConversationLoaded();
    }

    function refreshConversationBackendSession(taskId, target, options = {}) {
      return controllers.conversationController?.()?.refreshConversationBackendSession(taskId, target, options);
    }

    function maybeRefreshConversationBackendSessionFallback() {
      return controllers.conversationController?.()?.maybeRefreshConversationBackendSessionFallback();
    }

    async function loadOlderConversation() {
      return await controllers.conversationController?.()?.loadOlderConversation();
    }

    function appendRealtimeConversationEvents(events) {
      return controllers.conversationController?.()?.appendRealtimeConversationEvents(events);
    }

    function appendRealtimeEvents(events, startOffset = "") {
      return controllers.conversationController?.()?.appendRealtimeEvents(events, startOffset);
    }

    async function pollEvents() {
      return await controllers.conversationController?.()?.pollEvents();
    }

    function closeEventWebSocket() {
      return controllers.realtimeClient?.()?.close();
    }

    function resetEventWebSocketReconnectState(reason = "") {
      return controllers.realtimeClient?.()?.resetReconnect(reason);
    }

    async function syncRealtimeEvents(options = {}) {
      return await controllers.realtimeClient?.()?.syncRealtimeEvents(options);
    }

    async function catchUpRealtimeEvents() {
      return await controllers.realtimeClient?.()?.catchUpRealtimeEvents();
    }

    function requestRealtimeCatchup() {
      controllers.realtimeClient?.()?.requestRealtimeCatchup();
    }

    function renderTaskList() {
      controllers.taskController?.()?.renderTaskList();
    }

    async function selectTask(taskId) {
      return await controllers.appActions?.()?.dispatch("select-task", { taskId });
    }

    async function updateTaskVisibility(taskId, action) {
      return await controllers.appActions?.()?.dispatch("task-visibility", { taskId, action });
    }

    function renderSelectedHeader() {
      controllers.taskController?.()?.renderSelectedHeader();
    }

    function renderAgents() {
      controllers.agentController?.()?.renderAgents();
    }

    function renderSelectedAgentInfo() {
      controllers.agentController?.()?.renderSelectedAgentInfo();
    }

    function setExpandedMessageKey(key, open, contextKey = deps.conversationKey?.()) {
      controllers.timelineView?.()?.setExpandedMessageKey(key, open, contextKey);
    }

    function syncExpandedMessageKeysFromDom(root = deps.panelEl?.()) {
      controllers.timelineView?.()?.syncExpandedMessageKeysFromDom(root);
    }

    function renderMessageBody(body, key = "") {
      return controllers.timelineView?.()?.renderMessageBody(body, key);
    }

    function renderConversationFilters() {
      controllers.conversationController?.()?.renderConversationFilters();
    }

    function renderConversation(taskId) {
      return controllers.conversationController?.()?.renderConversation(taskId);
    }

    function renderTimelineEvent(event) {
      return controllers.timelineView?.()?.renderTimelineEvent(event);
    }

    function renderTurnTimer(taskId) {
      return controllers.timelineView?.()?.renderTurnTimer(taskId);
    }

    function bootstrapConfigData() {
      return controllers.bootstrapController?.()?.configData();
    }

    function bootstrapConfigFormHtml(options = {}) {
      return controllers.bootstrapController?.()?.formHtml(options);
    }

    function renderBootstrapConfigState(force = false) {
      controllers.bootstrapController?.()?.renderBootstrapConfigState(force);
    }

    function renderFirstRunState(force = false) {
      controllers.bootstrapController?.()?.renderFirstRunState(force);
    }

    function renderBootstrapError(error) {
      controllers.bootstrapController?.()?.renderBootstrapError(error);
    }

    function syncBootstrapModelOptions(form) {
      controllers.bootstrapController?.()?.syncModelOptions(form);
    }

    function addBootstrapConfigRow(button) {
      controllers.bootstrapController?.()?.addConfigRow(button);
    }

    function removeBootstrapConfigRow(button) {
      controllers.bootstrapController?.()?.removeConfigRow(button);
    }

    async function saveBootstrapConfigForm(form) {
      await controllers.bootstrapController?.()?.saveConfigForm(form);
    }

    async function createRunFromBootstrapForm(form) {
      await controllers.bootstrapController?.()?.createRunFromForm(form);
    }

    return Object.freeze({
      addBootstrapConfigRow,
      addOptimisticSendFeedback,
      agentStatusSession,
      agentStatusTiming,
      agentStatusTimingText,
      appendRealtimeConversationEvents,
      appendRealtimeEvents,
      applyBootstrapPayload,
      applyRunListData,
      applyStatusData,
      applyWorkspaceData,
      backendSessionId,
      backendSessionIdsDiffer,
      backendSessionWithPreviousContextPressure,
      backendTarget,
      bootstrapConfigData,
      bootstrapConfigFormHtml,
      captureContextScrollState,
      catchUpRealtimeEvents,
      clearOptimisticEventsForContext,
      closeEventWebSocket,
      compactResetLooksComplete,
      compactResetSelectedSession,
      conversationBackendSession,
      createRunFromBootstrapForm,
      currentAppVersion,
      currentRunSummary,
      ensureActiveTabData,
      ensureConversationLoaded,
      fallbackCurrentRun,
      flushDeferredPanelRender,
      initializeEventTailOffset,
      initSessionControl,
      initSettingsDialog,
      isAgentsPanelEditing,
      isAhaCommand,
      isInterruptCommand,
      latestKnownEventOrder,
      latestTurnTiming,
      loadAccessControlStatus,
      loadAgentsRuntime,
      loadBootstrap,
      loadConversationPage,
      loadHardwareIoPage,
      loadOlderConversation,
      loadOlderLogs,
      loadRunMaintenance,
      loadRuns,
      loadStatus,
      logState,
      hardwareIoState,
      maybeRefreshConversationBackendSessionFallback,
      pollEvents,
      prepareRealtimeCatchupBaseline,
      promptMetricsKey,
      promptMetricsState,
      readStoredSelectedTaskId,
      refreshCompactResetStatus,
      refreshConversationBackendSession,
      refreshRealtimeIndicator,
      refreshRunScopedView,
      realtimeDebug,
      realtimeTransportText,
      removeBootstrapConfigRow,
      removeOptimisticEventsMatchedBy,
      renderAccessControlStatus,
      renderAgents,
      renderBootstrapConfigState,
      renderBootstrapError,
      renderConversation,
      renderConversationFilters,
      renderFirstRunState,
      renderMessageBody,
      renderModelOptions,
      renderPanelForRealtime,
      renderPlayConsolePopover,
      renderPromptMetricsDock,
      renderPromptMetricsPanel,
      renderPromptMetricsPopover,
      renderRawPromptSection,
      renderRunMaintenance,
      renderSelectedAgentInfo,
      renderSelectedHeader,
      renderSessionMenu,
      renderSessionSummary,
      renderSkillsConsolePopover,
      renderTaskList,
      renderTaskVisibilityFilter,
      renderTimelineEvent,
      renderTurnTimer,
      renderWeixinConsolePopover,
      requestRealtimeCatchup,
      resetEventWebSocketReconnectState,
      resetRunMaintenanceState,
      resetRunScopedState,
      restoreContextScrollState,
      runHasNoTasks,
      runMaintenancePayload,
      runScopedPayload,
      saveBootstrapConfigForm,
      selectTask,
      selectedAgent,
      selectedTask,
      selectedTaskNeedsAgentDetails,
      selectedTaskRealtimeActive,
      setExpandedMessageKey,
      setPlayConsoleOpen,
      setRunArchiveState,
      setRunLifecycleState,
      setRunMaintenanceConsoleOpen,
      setSessionMenu,
      setSkillsConsoleOpen,
      setWebRestartState,
      setWeixinConsoleOpen,
      switchRun,
      syncBootstrapModelOptions,
      syncCurrentRunDisplay,
      syncExpandedMessageKeysFromDom,
      syncRealtimeEvents,
      syncRunUrl,
      taskMetaTiming,
      taskTimingLabel,
      updateTaskVisibility,
      verifyCompactResetAfterTimeout,
      visibleTasks,
      writeStoredSelectedTaskId
    });
  }

  window.AHAAppBridge = Object.freeze({
    createAppBridge
  });
})();
