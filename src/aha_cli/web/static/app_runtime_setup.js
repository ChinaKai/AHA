const queryParams = new URLSearchParams(location.search);
const rawPollInterval = Number(queryParams.get("poll") || "1000");
const rawIdlePollInterval = Number(queryParams.get("idle_poll") || queryParams.get("poll_idle") || "4000");
const rawHiddenPollInterval = Number(queryParams.get("hidden_poll") || queryParams.get("poll_hidden") || "15000");
const rawRequestTimeoutMs = Number(queryParams.get("timeout") || "12000");
const rawWsStaleMs = Number(queryParams.get("ws_stale_ms") || queryParams.get("ws_watchdog_ms") || "15000");
const pollInterval = Number.isFinite(rawPollInterval) ? Math.max(250, rawPollInterval) : 1000;
const idlePollInterval = Number.isFinite(rawIdlePollInterval) ? Math.max(pollInterval, rawIdlePollInterval) : 4000;
const hiddenPollInterval = Number.isFinite(rawHiddenPollInterval) ? Math.max(idlePollInterval, rawHiddenPollInterval) : 15000;
const requestTimeoutMs = Number.isFinite(rawRequestTimeoutMs) ? Math.max(1000, rawRequestTimeoutMs) : 12000;
const BACKEND_SESSION_WATCH_BYTES = 5 * 1024 * 1024;
const BACKEND_SESSION_COMPACT_BYTES = 8 * 1024 * 1024;
const eventSocketStaleMs = Number.isFinite(rawWsStaleMs) ? Math.max(5000, rawWsStaleMs) : 15000;
const eventTransport = String(queryParams.get("transport") || queryParams.get("events") || "").toLowerCase();
const wsConfig = String(queryParams.get("ws") || "").trim();
const wsDisabled = eventTransport === "poll" || eventTransport === "polling" || ["0", "false", "off"].includes(wsConfig.toLowerCase());
const realtimeDebugParam = String(queryParams.get("realtime_debug") || queryParams.get("debug") || "").toLowerCase();
const realtimeDebugEnabled = ["1", "true", "on", "yes"].includes(realtimeDebugParam);
const {
  confirmDialogAction,
  escapeHtml,
  selectOptions
} = window.AHAAppHelpers;
let currentRunId = String(queryParams.get("run_id") || queryParams.get("run") || "").trim();
let bootstrapData = null;
let bootstrapError = "";
let defaultRunId = "";
let runsData = [];
let runsLoaded = false;
let runsError = "";
let runActionInFlight = false;
let webRestartInFlight = false;
let accessControlData = null;
let accessControlError = "";
let offset = -1;
let lastEventId = "";
let statusData = null;
const initialRunId = currentRunId;
const initialSelectedTaskId = String(queryParams.get("selected_task_id") || queryParams.get("task_id") || "").trim();
const initialTaskMemoQueryView = String(queryParams.get("view") || "").trim().toLowerCase();
function readStoredTaskMemoView() {
  try {
    return String(window.localStorage?.getItem("aha.taskMemoViewExplicit") || "").trim().toLowerCase();
  } catch (_err) {
    return "";
  }
}
const initialTaskMemoStoredView = readStoredTaskMemoView();
const initialTaskMemoHomeActive = initialTaskMemoQueryView === "memo" || (!initialSelectedTaskId && !initialTaskMemoQueryView && (initialTaskMemoStoredView === "memo" || initialTaskMemoStoredView === ""));
const initialKnowledgeHomeActive = initialTaskMemoQueryView === "kb" || (!initialSelectedTaskId && !initialTaskMemoQueryView && initialTaskMemoStoredView === "kb");
const initialSettingsHomeActive = initialTaskMemoQueryView === "settings" || (!initialSelectedTaskId && !initialTaskMemoQueryView && initialTaskMemoStoredView === "settings");
let selectedTaskId = (initialTaskMemoHomeActive || initialKnowledgeHomeActive || initialSettingsHomeActive) ? null : (initialSelectedTaskId || null);

function applyInitialTaskMemoHomeState() {
  if (!initialTaskMemoHomeActive) return;
  const memoPage = document.getElementById("task-memo-dialog");
  if (!memoPage?.classList?.contains("task-memo-page")) return;
  document.body?.classList?.add("task-memo-home");
  memoPage.setAttribute("open", "");
  document.getElementById("open-task-view")?.setAttribute("aria-pressed", "false");
  document.getElementById("open-task-memos")?.setAttribute("aria-pressed", "true");
}

applyInitialTaskMemoHomeState();

function knowledgeHomeUrl() {
  return currentRunId ? `/static/knowledge.html?run_id=${encodeURIComponent(currentRunId)}` : "/static/knowledge.html";
}

function applyInitialKnowledgeHomeState() {
  if (!initialKnowledgeHomeActive) return;
  const knowledgeHome = document.getElementById("knowledge-home");
  const knowledgeFrame = document.getElementById("knowledge-home-frame");
  if (!knowledgeHome || !knowledgeFrame) return;
  document.body?.classList?.add("knowledge-home");
  knowledgeHome.hidden = false;
  if (!knowledgeFrame.getAttribute("src")) knowledgeFrame.setAttribute("src", knowledgeHomeUrl());
  document.getElementById("open-task-view")?.setAttribute("aria-pressed", "false");
  document.getElementById("open-task-memos")?.setAttribute("aria-pressed", "false");
  document.getElementById("open-knowledge-base")?.setAttribute("aria-pressed", "true");
}

applyInitialKnowledgeHomeState();

function applyInitialSettingsHomeState() {
  if (!initialSettingsHomeActive) return;
  const sessionMenu = document.getElementById("session-menu");
  if (!sessionMenu) return;
  document.body?.classList?.add("settings-home");
  sessionMenu.classList.remove("hidden");
  document.getElementById("open-task-view")?.setAttribute("aria-pressed", "false");
  document.getElementById("open-task-memos")?.setAttribute("aria-pressed", "false");
  document.getElementById("open-knowledge-base")?.setAttribute("aria-pressed", "false");
  document.getElementById("session-toggle")?.setAttribute("aria-expanded", "true");
  document.getElementById("session-toggle")?.setAttribute("aria-pressed", "true");
}

applyInitialSettingsHomeState();

function applyInitialTaskHomeState() {
  if (initialTaskMemoHomeActive || initialKnowledgeHomeActive || initialSettingsHomeActive) return;
  document.getElementById("open-task-view")?.setAttribute("aria-pressed", "true");
  document.getElementById("open-task-memos")?.setAttribute("aria-pressed", "false");
  document.getElementById("open-knowledge-base")?.setAttribute("aria-pressed", "false");
}

applyInitialTaskHomeState();

let activeTab = "conversation";
let taskActionInFlight = false;
let backendStatusData = null;
const agentsRuntimeCache = new Map();
let conversationAutoFollow = true;
let eventTailInitialized = false;
let optimisticEventSeq = 0;
let renderScheduler = null;
let conversationController = null;
let compactResetController = null;
let bootstrapController = null;
let timelineView = null;
let optimisticEvents = null;
let accessControlController = null;
let playConsoleController = null;
let skillsConsoleController = null;
let tokenUsageController = null;
let weixinConsoleController = null;
let runtimeOptions = null;
let promptMetricsPopover = null;
let eventCursorStore = null;
let taskOptionsController = null;
let appSelectors = null;
let appBridge = null;
const allEvents = [];
const seenRealtimeEvents = new Set();
const realtimeEventCacheLimit = 2000;
const realtimeSeenEventCacheLimit = 4000;
const pendingMessages = [];
const interruptedContexts = new Set();
const conversationPageLimit = 30;
const CONVERSATION_SESSION_REFRESH_FALLBACK_MS = Math.max(5000, pollInterval * 5);
const logPageLimit = 200;
const conversationStates = new Map();
const conversationSessionRefreshes = new Map();
const conversationSessionRefreshAt = new Map();
const expandedMessageKeys = new Set();
const copyTextByKey = new Map();
const promptArtifactCache = new Map();
const finalDetails = new Map();
const contextDetails = new Map();
const logStates = new Map();
const hardwareIoStates = new Map();
const compactResetStates = new Map();
const terminalTaskStatuses = new Set(["completed", "failed", "blocked"]);
const terminalAgentStatuses = new Set(["completed", "failed", "blocked", "interrupted"]);
const sandboxOptions = ["workspace-write", "read-only", "danger-full-access"];
const approvalOptions = ["never", "on-failure", "on-request", "untrusted"];
const apiClient = window.AHAApiClient.createApiClient({
  requestTimeoutMs,
  currentRunId: () => currentRunId
});
const {
  readJsonResponse,
  fetchWithTimeout,
  fetchJson,
  isRequestTimeoutError,
  isAuthRequiredError,
  apiUrl
} = apiClient;
const taskMetadata = window.AHATaskMetadata;
const {
  collaborationModeOptions,
  workflowTemplateOptions: staticWorkflowTemplateOptions,
  supervisionAskUserGateDefs,
  defaultTaskSupervisionMaxRounds,
  defaultTaskContextThresholdPercent,
  collaborationModeDescription,
  workflowTemplateDescription: staticWorkflowTemplateDescription,
  collaborationModeMaxSubAgents: taskMetadataCollaborationModeMaxSubAgents,
  collaborationModeDelegationPolicy,
  inferredTaskCollaborationMode,
  taskCollaborationSummary,
  taskWorkflowSummary,
  defaultAskUserGates,
  normalizeAskUserGates,
  taskSupervisionPolicy,
  taskSupervisionModeValue,
  taskSupervisionSummary,
  normalizeTaskContextThreshold,
  taskContextManagementPolicy,
  taskContextSummary,
  taskSkillsPolicy,
  taskSkillsSummary,
  hardwareDebugPermissionKeys,
  taskHardwareDebugPolicy,
  taskHardwareDebugSummary,
  normalizeHardwareDebugPermissions,
  taskSupervisionPayloadFromMode
} = taskMetadata;
const bootstrapConfigHelpers = window.AHABootstrapConfig;
const {
  claudeEnvModelPrefix,
  configString,
  bootstrapBackendOptions,
  bootstrapEnvGroups,
  bootstrapCodexEnvGroups,
  bootstrapEnvGroupName
} = bootstrapConfigHelpers;
const taskFormHelpers = window.AHATaskForm;
const {
  createTaskPayload,
  createTaskConfirmRows,
  createTaskFallbackConfirmText
} = taskFormHelpers;
const runMetadata = window.AHARunMetadata;
const {
  runIdOf,
  runTitleOf,
  runUpdatedAtOf,
  runLifecycleLabel,
  runLifecycleClass,
  runLifecycleTitle,
  sessionOptionLabel,
  runLifecycleProtectionReason,
  runDeleteProtectionReason,
  runLifecycleReasonText,
  runLifecycleActionsView
} = runMetadata;
const runLifecycleView = window.AHARunLifecycleView.createRunLifecycleView({ escapeHtml });
const runMaintenanceHelpers = window.AHARunMaintenance;
const {
  runMaintenanceView,
  runMaintenanceActionConfirm
} = runMaintenanceHelpers;
const runtimeConfigHelpers = window.AHARuntimeConfig.createRuntimeConfigHelpers({
  configString,
  defaultModelForBackend: backend => runtimeOptions.defaultModelForBackend(backend),
  modelLabelForBackend: (backend, value) => runtimeOptions.modelLabelForBackend(backend, value)
});
const {
  agentModelValue,
  normalizeAgentConfig,
  agentConfigValue,
  agentConfigLabel,
  agentBackendModelChanged,
  agentRuntimeConfigChanged,
  proxySelectOptions,
  readAgentConfigEditor
} = runtimeConfigHelpers;
const agentMetadata = window.AHAAgentMetadata;
const {
  agentBackendProcessStatus,
  agentBackendProcessLabel,
  agentLifecycleStatus,
  agentWaitingReason,
  agentLifecycleDisplay,
  agentLifecycleLabel,
  isSupervisionAgent,
  agentOptionGroups,
  agentOptionLabel,
  agentRuntimeDefaults,
  agentDisplayModel
} = agentMetadata;
const taskListHelpers = window.AHATaskList;
const {
  taskCurrentStatus,
  taskOutcomeStatus,
  taskActivityStatus,
  taskDisplayStatus,
  taskProxyConfigured,
  taskProxySummary,
  taskAgentCount,
  normalizeTaskVisibilityFilter,
  visibleTasks: taskListVisibleTasks,
  taskVisibilityFilterHtml,
  taskActivityMillis,
  defaultTaskId,
  pathName,
  taskListTitle,
  taskListItemClass,
  taskListItemHtml
} = taskListHelpers;
const timeFormat = window.AHATimeFormat;
const {
  parseTimestamp,
  formatLocalTimestamp,
  localizeTimestampFields,
  localizeTimestampText,
  formatDuration,
  formatClock
} = timeFormat;
const promptMetrics = window.AHAPromptMetrics;
const {
  contextPressureHasPercent,
  formatMetricNumber,
  formatMetricCompact,
  formatMetricBytes,
  contextPressureStatus,
  contextPressurePercent,
  contextPressureSummary,
  agentContextPressureSummary,
  formatMetricCountChars,
  metricMapRows,
  usageCacheReadTokens,
  usageCacheCreationTokens,
  tokenLedgerFromMetrics,
  tokenLedgerVerdict,
  componentMetricRows,
  promptRefPath,
  promptArtifactMeta
} = promptMetrics;
const promptMetricsView = window.AHAPromptMetricsView.createPromptMetricsView({
  escapeHtml,
  formatMetricNumber,
  formatMetricBytes,
  usageCacheReadTokens,
  usageCacheCreationTokens,
  contextPressurePercent,
  metricMapRows
});
const {
  renderAhaInputBreakdown,
  renderSessionBreakdown,
  renderUsageBreakdown
} = promptMetricsView;
const accessControlHelpers = window.AHAAccessControl;
const {
  createAccessControlController
} = accessControlHelpers;
const conversationMetadata = window.AHAConversationMetadata;
const {
  conversationFilterOptions,
  eventData,
  ahaActionEnvelopePayload,
  isAhaActionEnvelopeText,
  eventTaskId,
  isTaskEvent,
  isTimelineEvent,
  eventAgentRefs,
  eventMatchesAgent,
  messageDisplaySender,
  messageDisplayTarget,
  messageTimelineDisplay,
  dedupeConversationEvents,
  eventIdentity,
  conversationEventOrder,
  mergeConversationEvents,
  parseConversationKey,
  conversationEventCategory,
  conversationFilterCounts: conversationMetadataFilterCounts,
  agentUpdateTitle,
  agentUpdateBody
} = conversationMetadata;
eventCursorStore = window.AHAEventCursorStore.createEventCursorStore({
  getCurrentRunId: () => currentRunId,
  getLastEventId: () => lastEventId,
  setLastEventId: value => { lastEventId = String(value || ""); },
  setOffset: value => { offset = Number(value); },
  setEventTailInitialized: value => { eventTailInitialized = Boolean(value); }
}, {
  storage: window.localStorage
});
const conversationPanelHelpers = window.AHAConversationPanel.createConversationPanelHelpers({
  escapeHtml,
  localizeTimestampText
});
const {
  renderConversationFiltersHtml,
  renderConversationPanelHtml,
  renderFinalPanelHtml,
  renderHardwareIoPanelHtml,
  renderLogsPanelHtml,
  renderContextPanelHtml
} = conversationPanelHelpers;
const taskTimingHelpers = window.AHATaskTiming;
const {
  taskTimingLabel: taskTimingLabelForContext,
  taskMetaTiming: taskMetaTimingForContext,
  latestTurnTiming: latestTurnTimingForContext
} = taskTimingHelpers;
const selectionState = window.AHASelectionState;
const {
  createRemoteSelectedTaskState,
  readStoredSelectedTaskId: readStoredSelectedTaskIdForRun,
  selectedAgent: selectedAgentFromTask,
  selectedTask: selectedTaskFromStatus,
  selectedTaskRealtimeActive: selectedTaskRealtimeActiveFromState,
  writeStoredSelectedTaskId: writeStoredSelectedTaskIdForRun
} = selectionState;
const remoteSelectedTaskState = createRemoteSelectedTaskState({
  apiUrl,
  currentRunId: () => currentRunId,
  fetchJson,
  fetchWithTimeout
});
function readPersistedSelectedTaskIdForRun(runId, storage = window.localStorage) {
  const remoteValue = String(currentRunId === runId ? remoteSelectedTaskState.cachedValue() || "" : "").trim();
  return remoteValue || readStoredSelectedTaskIdForRun(runId, storage);
}
function writePersistedSelectedTaskIdForRun(runId, taskId, storage = window.localStorage) {
  writeStoredSelectedTaskIdForRun(runId, taskId, storage);
  if (currentRunId === runId) remoteSelectedTaskState.writeSelectedTaskId(taskId);
}
async function ensureRemoteSelectedTaskId() {
  return await remoteSelectedTaskState.readSelectedTaskId();
}
let persistedSelectedRunId = "";
function writePersistedSelectedRunId(runId) {
  const value = String(runId || "").trim();
  if (!value || persistedSelectedRunId === value) return;
  persistedSelectedRunId = value;
  fetchWithTimeout(apiUrl("/api/ui-state", {}, { runScoped: false }), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ last_selected_run_id: value })
  }).catch(err => console.warn("Failed to save selected run", err));
}
const realtimeState = window.AHARealtimeState;
const {
  realtimeReadyStateName,
  realtimeTransportText: realtimeTransportLabel,
  realtimeReconnectDelayMs,
  realtimeStaleFallbackDue: realtimeStaleFallbackDueForState
} = realtimeState;
appBridge = window.AHAAppBridge.createAppBridge({
  activeTab: () => activeTab,
  currentRunId: () => currentRunId,
  selectedTaskId: () => selectedTaskId,
  setCurrentRunId: value => { currentRunId = value || ""; },
  setRunActionInFlight: value => { runActionInFlight = Boolean(value); },
  setSelectedTaskId: value => { selectedTaskId = value || null; },
  setStatusData: value => { statusData = value; },
  statusData: () => statusData
}, {
  accessControlController: () => accessControlController,
  agentController: () => agentController,
  appActions: () => appActions,
  appSelectors: () => appSelectors,
  bootstrapController: () => bootstrapController,
  compactResetController: () => compactResetController,
  conversationController: () => conversationController,
  optimisticEvents: () => optimisticEvents,
  panelController: () => panelController,
  playConsoleController: () => playConsoleController,
  realtimeClient: () => realtimeClient,
  renderOrchestrator: () => renderOrchestrator,
  runController: () => runController,
  runtimeOptions: () => runtimeOptions,
  settingsController: () => settingsController,
  skillsConsoleController: () => skillsConsoleController,
  tokenUsageController: () => tokenUsageController,
  statusController: () => statusController,
  taskMemoController: () => taskMemoController,
  taskController: () => taskController,
  timelineView: () => timelineView,
  weixinConsoleController: () => weixinConsoleController
}, {
  agentTargetEl: () => agentTargetEl,
  agentsEl: () => agentsEl,
  contextPressureHasPercent,
  conversationKey: (...args) => conversationKey(...args),
  conversationStates,
  documentRef: document,
  escapeHtml,
  localStorage: window.localStorage,
  panelEl: () => panelEl,
  readStoredSelectedTaskIdForRun: readPersistedSelectedTaskIdForRun,
  runStateEl: () => runStateEl,
  writeSelectedRunId: writePersistedSelectedRunId,
  writeStoredSelectedTaskIdForRun: writePersistedSelectedTaskIdForRun
});
const {
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
  backendSessionWithPreviousContextPressure,
  backendTarget,
  bootstrapConfigData,
  bootstrapConfigFormHtml,
  captureContextScrollState,
  catchUpRealtimeEvents,
  clearOptimisticEventsForContext,
  closeEventWebSocket,
  compactResetSelectedSession,
  conversationBackendSession,
  createRunFromBootstrapForm,
  currentAppVersion,
  currentRunSummary,
  ensureActiveTabData,
  ensureConversationLoaded,
  fallbackCurrentRun,
  flushDeferredPanelRender,
  hardwareIoState,
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
  maybeRefreshConversationBackendSessionFallback,
  pollEvents,
  prepareRealtimeCatchupBaseline,
  promptMetricsKey,
  promptMetricsState,
  readStoredSelectedTaskId,
  refreshConversationBackendSession,
  refreshRealtimeIndicator,
  refreshRunScopedView,
  realtimeDebug,
  realtimeTransportText,
  removeBootstrapConfigRow,
  removeOptimisticEventsMatchedBy,
  renderAccessControlStatus,
  renderAgents,
  renderBootstrapError,
  renderConversation,
  renderConversationFilters,
  renderFirstRunState,
  renderModelOptions,
  renderPanelForRealtime,
  renderPlayConsolePopover,
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
} = appBridge;
const realtimeStatusRefreshEventTypes = new Set([
  "agent_backend_switched",
  "agent_config_updated",
  "agent_created",
  "agent_finished",
  "agent_started",
  "agent_status_changed",
  "backend_start_failed",
  "backend_start_queued",
  "backend_session_compact_reset",
  "backend_session_reset",
  "backend_started",
  "backend_stopped",
  "task_completed",
  "task_context_management_config_updated",
  "task_created",
  "task_reopened",
  "task_status_changed",
  "task_supervision_config_updated",
  "task_waiting_for_subagents"
]);
let realtimeStatusRefreshTimer = null;
let realtimeStatusRefreshInFlight = null;
function acceptedEventsNeedStatusRefresh(events = []) {
  return events.some(event => realtimeStatusRefreshEventTypes.has(String(event?.type || "")));
}
function scheduleRealtimeStatusRefresh(events = []) {
  if (!acceptedEventsNeedStatusRefresh(events)) return false;
  if (realtimeStatusRefreshTimer) clearTimeout(realtimeStatusRefreshTimer);
  const eventTypes = events.map(event => String(event?.type || "")).filter(Boolean).slice(0, 8);
  realtimeStatusRefreshTimer = setTimeout(() => {
    realtimeStatusRefreshTimer = null;
    realtimeDebug("status.event_refresh", { event_types: eventTypes });
    const refresh = Promise.resolve(loadStatus())
      .then(() => renderPanelForRealtime())
      .catch(err => {
        console.warn("Realtime status refresh failed", err);
        realtimeDebug("status.event_refresh_error", { error: err?.message || String(err) });
      });
    realtimeStatusRefreshInFlight = refresh;
    refresh.finally(() => {
      if (realtimeStatusRefreshInFlight === refresh) realtimeStatusRefreshInFlight = null;
    });
  }, Math.min(250, pollInterval));
  return true;
}
const realtimeClient = window.AHARealtimeClient.createRealtimeClient({
  queryParams,
  pollInterval,
  staleMs: eventSocketStaleMs,
  wsConfig,
  wsDisabled,
  debugEnabled: realtimeDebugEnabled,
  realtimeReadyStateName,
  realtimeTransportLabel,
  realtimeReconnectDelayMs,
  realtimeStaleFallbackDue: realtimeStaleFallbackDueForState,
  currentRunId: () => currentRunId,
  selectedTaskId: () => selectedTaskId,
  lastEventId: () => lastEventId,
  eventTailInitialized: () => eventTailInitialized,
  debugContext: () => ({
    target: agentTargetEl?.value || "",
    active_tab: activeTab,
    visibility: document.visibilityState,
    online: navigator.onLine,
    offset,
    tail_initialized: eventTailInitialized
  }),
  sendDebugPayload: payload => {
    fetch(apiUrl("/api/debug/realtime"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(runScopedPayload(payload)),
      keepalive: true
    }).catch(() => {});
  },
  refreshRealtimeIndicator,
  initializeEventTailOffset,
  pollEvents,
  onStatus: payload => {
    statusData = payload || {};
    applyStatusData();
    renderPanelForRealtime();
  },
  onHeartbeat: () => refreshRealtimeIndicator(),
  onEvent: event => appendRealtimeEvents([event]),
  onAcceptedEvents: accepted => {
    scheduleRealtimeStatusRefresh(accepted);
    if (accepted.length) renderPanelForRealtime();
  }
});
const defaultHttpProxy = "http://127.0.0.1:7890";
const defaultHttpsProxy = defaultHttpProxy;
const defaultNoProxy = "localhost,127.0.0.1,::1";
const collapsedMessageCharLimit = 900;
const collapsedMessageLineLimit = 2;
const conversationFilters = {
  chat: true,
  runtime: false,
  commands: false,
  usage: false
};
let taskVisibilityFilter = "active";
const turnEventTypes = new Set([
  "agent_started",
  "agent_error",
  "agent_prompt_metrics",
  "agent_usage",
  "agent_context_overflow",
  "agent_thread",
  "agent_finished",
  "agent_status_changed",
  "backend_stopped"
]);
const backendSessionRefreshEventTypes = new Set([
  "agent_started",
  "agent_prompt_metrics",
  "agent_usage",
  "agent_context_overflow",
  "backend_started",
  "agent_backend_switched",
  "backend_session_reset",
  "backend_session_compact_reset"
]);
const finalDetailInvalidatingEvents = new Set([
  "task_result_written",
  "task_journal_rendered",
  "task_round_recorded",
  "task_reopened",
  "task_status_changed"
]);
const conversationStateHelpers = window.AHAConversationState.createConversationStateHelpers({
  allEvents,
  conversationStates,
  conversationFilters,
  conversationFilterOptions,
  selectedTaskId: () => selectedTaskId,
  backendTarget,
  isTaskEvent,
  isTimelineEvent,
  eventMatchesAgent,
  conversationEventCategory,
  conversationMetadataFilterCounts,
  dedupeConversationEvents,
  mergeConversationEvents,
  backendSessionWithPreviousContextPressure
});
const {
  conversationKey,
  activeConversationCategoryKey,
  conversationState,
  taskEvents,
  eventMatchesSelectedAgent,
  agentTimelineEvents,
  conversationSourceEvents,
  taskConversationEvents,
  conversationFilterCounts,
  prepareConversationStateForLoad,
  shouldSkipConversationLoad,
  applyConversationPagePayload
} = conversationStateHelpers;
timelineView = window.AHATimelineView.createTimelineView({
  collapsedMessageCharLimit,
  collapsedMessageLineLimit,
  copyTextByKey,
  expandedMessageKeys
}, {
  agentLifecycleDisplay,
  agentUpdateBody,
  agentUpdateTitle,
  backendTarget,
  componentMetricRows,
  conversationKey,
  defaultTaskContextThresholdPercent: () => defaultTaskContextThresholdPercent,
  apiUrl,
  documentRef: document,
  escapeHtml,
  eventData,
  formatClock,
  formatDuration,
  formatLocalTimestamp,
  latestTurnTiming,
  messageTimelineDisplay,
  renderMarkdownHtml: window.AHATaskMemoMarkdown?.renderMarkdownHtml,
  renderPromptMetricsPopover,
  t: window.AHAI18n?.t,
  tasks: () => statusData?.tasks || []
});
