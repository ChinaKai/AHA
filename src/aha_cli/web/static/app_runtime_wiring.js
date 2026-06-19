const domRefs = window.AHAControllerRegistry.collectDomRefs(document);
const {
  agentTargetEl, agentsEl, appVersionEl, authLogoutEl, conversationFiltersEl, headerRunConsoleEl, headerRunTitleEl, headerWorkspaceDirEl,
  mobileTaskStatusEl, mobileTaskSummaryEl, mobileTaskTitleEl, newRunGoalEl, openRunCreateEl, panelEl, pendingMessagesEl,
  playConsoleEl, playConsolePopoverEl, renameRunNameEl, runArchiveStateEl, runCreateFormEl, runExportEl,
  runCreateDialogEl, closeRunCreateEl, cancelRunCreateEl, runExportLogsEl, runIdEl, runImportEl, runImportFileEl, runLifecycleActionsEl, runLifecycleEl,
  runLifecycleFilterEl, runLifecycleStateEl, runMaintenanceCloseEl, runManagerEl,
  runMaintenanceDetailEl, runMaintenancePopoverEl, runMaintenanceRefreshEl, runMaintenanceSummaryEl,
  runSettingsActionsEl, runSettingsCloseEl, runSettingsPanelEl, runSettingsProtectionEl, runSettingsSubtitleEl,
  runRenameFormEl, runSelectEl, runStateEl, selectedAgentInfoEl, selectedIdEl, selectedStatusEl,
  selectedTaskMetaEl, selectedTitleEl, sessionControlEl, sessionDetailTextEl, sessionMenuEl, sessionRefreshEl,
  sessionTitleEl, sessionToggleEl, summaryEl, taskRunContextEl, taskSettingsActionsEl, taskSettingsCloseEl,
  taskSettingsPanelEl, taskSettingsSubtitleEl, tasksEl, taskVisibilityFilterEl,
  webRestartEl, webRestartStateEl, webUpgradeEl, weixinConsoleEl, weixinConsolePopoverEl
} = domRefs;
const initialControllers = window.AHAAppControllerFactory.createInitialControllers(domRefs, {
  accessControlData: () => accessControlData,
  accessControlError: () => accessControlError,
  activeTab: () => activeTab,
  activateTab: tab => activateTab(tab),
  afterLogin: async () => {
    await loadBootstrap();
    void loadAccessControlStatus();
    if (currentRunId) {
      await renderScheduler.tick();
      if (initialTaskMemoHomeActive) {
        taskMemoController.openDialog?.();
      } else {
        taskMemoController.closeDialog?.();
      }
    } else {
      renderFirstRunState(true);
    }
  },
  afterLogout: () => {
    accessControlData = null;
    accessControlError = "";
    setSessionMenu(false);
  },
  agentBackendProcessStatus,
  agentLifecycleStatus,
  agentTargetValue: () => agentTargetEl.value || "main",
  agentWaitingReason,
  alertError: message => alert(message),
  apiUrl,
  backendStatusData: () => backendStatusData,
  bootstrapCodexEnvGroups,
  bootstrapConfigData,
  bootstrapData: () => bootstrapData,
  bootstrapEnvGroupName,
  bootstrapEnvGroups,
  captureContextScrollState,
  claudeEnvModelPrefix,
  closeEventWebSocket,
  collaborationModeDescription,
  collaborationModeOptions,
  configString,
  contextDetail: taskId => conversationController.contextDetail(taskId),
  conversationAutoFollow: () => conversationAutoFollow,
  conversationSourceEvents,
  copyTextByKey,
  createAccessControlController,
  currentRunId: () => currentRunId,
  defaultAskUserGates,
  defaultHttpProxy,
  defaultHttpsProxy,
  defaultNoProxy,
  defaultTaskContextThresholdPercent,
  defaultTaskSupervisionMaxRounds,
  documentRef: document,
  ensureActiveTabData,
  escapeHtml,
  eventMatchesSelectedAgent,
  fetchJson,
  finalDetail: taskId => conversationController.finalDetail(taskId),
  formatDuration,
  formatLocalTimestamp,
  isAuthRequiredError,
  isSupervisionAgent,
  latestTurnTimingForContext,
  loadStatus,
  logState,
  hardwareIoState,
  messageComposer: () => messageComposer,
  navigatorRef: navigator,
  normalizeAskUserGates,
  normalizeTaskContextThreshold,
  panelEl,
  parseTimestamp,
  promptMetricsState,
  renderContextPanelHtml,
  renderConversation,
  renderConversationFilters,
  renderFinalPanelHtml,
  renderFirstRunState,
  renderHardwareIoPanelHtml,
  renderLogsPanelHtml,
  renderPromptMetricsPanel,
  renderRawPromptSection,
  restoreContextScrollState,
  runHasNoTasks,
  runScopedPayload,
  selectOptions,
  selectedAgent,
  selectedAgentFromTask,
  selectedTask,
  selectedTaskFromStatus,
  selectedTaskId: () => selectedTaskId,
  selectedTaskRealtimeActiveFromState,
  setAccessControlData: value => { accessControlData = value; },
  setAccessControlError: value => { accessControlError = String(value || ""); },
  setActiveTab: value => { activeTab = value || "conversation"; },
  setBootstrapError: value => { bootstrapError = value || ""; },
  setConversationAutoFollow: value => { conversationAutoFollow = Boolean(value); },
  setDefaultWorkspacePath: value => {
    bootstrapData = { ...(bootstrapData || {}), default_workspace_path: value };
  },
  statusData: () => statusData,
  staticWorkflowTemplateDescription,
  staticWorkflowTemplateOptions,
  supervisionAskUserGateDefs,
  syncExpandedMessageKeysFromDom,
  taskActivityStatus,
  taskAgentCount,
  taskContextManagementPolicy,
  taskContextSummary,
  taskSkillsPolicy,
  taskSkillsSummary,
  hardwareDebugPermissionKeys,
  normalizeHardwareDebugPermissions,
  taskHardwareDebugPolicy,
  taskHardwareDebugSummary,
  taskEvents,
  taskMetadata,
  taskMetadataCollaborationModeMaxSubAgents,
  taskMetaTimingForContext,
  taskProxySummary,
  taskSupervisionModeValue,
  taskSupervisionPayloadFromMode,
  taskSupervisionPolicy,
  taskSupervisionSummary,
  taskTimingLabelForContext,
  terminalAgentStatuses,
  windowRef: window
});
taskOptionsController = initialControllers.taskOptionsController;
promptMetricsPopover = initialControllers.promptMetricsPopover;
accessControlController = initialControllers.accessControlController;
appSelectors = initialControllers.appSelectors;
runtimeOptions = initialControllers.runtimeOptions;
const {
  activateTab,
  agentInputWaitBlocked,
  authController,
  clearLoginState,
  copyTimelineMessage,
  eventBindings,
  isPanelNearBottom,
  isTaskContextEditing,
  isTaskHardwareEditing,
  isTaskProxyEditing,
  isTaskSupervisionEditing,
  logoutAuthSession,
  panelController,
  panelHasTextSelection,
  readAskUserGateControls,
  renderAskUserGateControls,
  renderBackendStatus,
  renderLoginState,
  renderPanel,
  renderRunProxyEditor,
  renderTaskContextEditor,
  renderTaskHardwareEditor,
  renderTaskProxyEditor,
  renderTaskSupervisionEditor,
  resetEditing: resetTaskConfigEditing,
  scrubAuthTokenFromUrl,
  selectedAgentInputBlocked,
  selectedBackendActive,
  setCreateProxyDefaultsFromInputs,
  submitLoginForm,
  syncCreateProxyDefaultForBackend,
  syncCreateTaskSupervisionModeFields,
  syncTaskContextFields,
  taskConfigController,
  taskHostInputBlocked,
  uiShell
} = initialControllers;
conversationController = window.AHAConversationController.createConversationController({
  allEvents,
  compactResetStates,
  contextDetails,
  conversationFilters,
  conversationFiltersEl,
  conversationPageLimit,
  conversationSessionRefreshAt,
  conversationSessionRefreshes,
  conversationStates,
  copyTextByKey,
  currentRunId: () => currentRunId,
  documentRef: document,
  finalDetails,
  activeTab: () => activeTab,
  backendTarget,
  eventTailInitialized: () => eventTailInitialized,
  hardwareIoStates,
  lastEventId: () => lastEventId,
  logPageLimit,
  logStates,
  offset: () => offset,
  openPromptMetricsKey: promptMetricsPopover.openKey,
  panelEl,
  promptArtifactCache,
  realtimeEventCacheLimit,
  realtimeSeenEventCacheLimit,
  seenRealtimeEvents,
  selectedTaskId: () => selectedTaskId,
  sessionRefreshFallbackMs: CONVERSATION_SESSION_REFRESH_FALLBACK_MS,
  setEventTailInitialized: value => { eventTailInitialized = Boolean(value); },
  setLastEventId: value => { lastEventId = String(value || ""); },
  setOffset: value => { offset = Number(value); }
}, {
  activeConversationCategoryKey,
  agentStatusSession,
  apiUrl,
  applyConversationPagePayload,
  backendSessionRefreshEventTypes,
  backendSessionCompactBytes: BACKEND_SESSION_COMPACT_BYTES,
  backendSessionWatchBytes: BACKEND_SESSION_WATCH_BYTES,
  componentMetricRows,
  contextPressurePercent,
  contextPressureStatus,
  contextPressureSummary,
  conversationEventCategory,
  conversationEventOrder,
  conversationFilterCounts,
  conversationFilterOptions,
  conversationKey,
  conversationSourceEvents,
  conversationState,
  clearRuntimeCacheForEvents: events => statusStore.clearRuntimeCacheForEvents(events),
  clearStoredLastEventId: eventCursorStore.clearStoredLastEventId,
  eventData,
  eventIdentity,
  eventMatchesAgent,
  eventTaskId,
  escapeHtml,
  fetchJson,
  fetchWithTimeout,
  initialTaskMemoHomeActive,
  finalDetailInvalidatingEvents,
  formatMetricBytes,
  formatMetricCompact,
  formatMetricCountChars,
  formatMetricNumber,
  isTaskEvent,
  isTimelineEvent,
  mergeConversationEvents,
  parseConversationKey,
  prepareConversationStateForLoad,
  promptArtifactMeta,
  promptRefPath,
  readJsonResponse,
  refreshTaskMemosIfOpen: () => taskMemoController.refreshIfOpen?.(),
  realtimeDebug,
  rememberEventCursor: eventCursorStore.remember,
  rememberEventCursorFromEvent: eventCursorStore.rememberFromEvent,
  removeOptimisticEventsMatchedBy,
  renderAhaInputBreakdown,
  renderConversationFiltersHtml,
  renderConversationPanelHtml,
  renderPanel,
  renderPanelForRealtime,
  renderSessionBreakdown,
  renderTimelineEvent,
  renderTurnTimer,
  renderUsageBreakdown,
  selectedTask,
  shouldSkipConversationLoad,
  taskConversationEvents,
  taskEvents,
  turnEventTypes,
  usageCacheCreationTokens,
  usageCacheReadTokens
});
compactResetController = window.AHACompactReset.createCompactResetController({
  compactResetStates
}, {
  alert,
  agentStatusSession: (taskId, agentId) => compactResetController.agentStatusSession(taskId, agentId),
  apiUrl,
  backendTarget,
  catchUpRealtimeEvents,
  compactResetLooksComplete: (taskId, agentId, previousSessionId, afterOrder) => (
    conversationController.compactResetLooksComplete(taskId, agentId, previousSessionId, afterOrder)
  ),
  confirmDialogAction,
  conversationBackendSession,
  fetchWithTimeout,
  isRequestTimeoutError,
  latestKnownEventOrder,
  loadConversationPage,
  loadStatus,
  promptMetricsKey,
  readJsonResponse,
  renderPanel,
  requestTimeoutMs: () => requestTimeoutMs,
  selectedTask,
  tasks: () => statusData?.tasks || [],
  verifyTimeoutMs: () => 30000,
  windowRef: window
});
const messageFlow = window.AHAMessageFlow.createMessageFlow({
  pendingMessages,
  interruptedContexts,
  terminalTaskStatuses
}, {
  currentRunId: () => currentRunId,
  selectedTaskId: () => selectedTaskId,
  selectedTask,
  selectedAgent,
  backendTarget,
  selectedAgentInputBlocked,
  selectedBackendActive,
  taskCurrentStatus,
  isAhaCommand,
  isInterruptCommand,
  pendingMessagesEl: () => pendingMessagesEl,
  escapeHtml,
  formatLocalTimestamp,
  realtimeDebug,
  addOptimisticSendFeedback,
  clearOptimisticEventsForContext,
  renderPanelForRealtime,
  prepareRealtimeCatchupBaseline,
  fetchJson,
  apiUrl,
  runScopedPayload,
  catchUpRealtimeEvents,
  loadStatus,
  renderPanel,
  setConversationAutoFollow: value => { conversationAutoFollow = Boolean(value); },
  agentTarget: () => agentTargetEl.value || "main",
  activeTab: () => activeTab
});
const {
  renderPendingMessages,
  removePendingMessage,
  clearPendingState,
  maybeAutoFlushPending,
  handleComposerSubmit,
  interruptBackend
} = messageFlow;
optimisticEvents = window.AHAOptimisticEvents.createOptimisticEvents({
  allEvents,
  conversationStates
}, {
  appendRealtimeConversationEvents,
  backendStatusData: () => backendStatusData,
  backendTarget,
  eventData,
  eventTaskId,
  isAhaCommand,
  messageDisplaySender,
  messageDisplayTarget,
  nextOptimisticEventSeq: () => ++optimisticEventSeq,
  renderPanelForRealtime,
  renderPendingMessages,
  renderSelectedAgentInfo,
  renderSelectedHeader,
  renderTaskList,
  selectedAgent,
  selectedTaskId: () => selectedTaskId,
  setBackendStatusData: value => { backendStatusData = value; },
  setConversationAutoFollow: value => { conversationAutoFollow = Boolean(value); }
});
const renderOrchestrator = window.AHARenderOrchestrator.createRenderOrchestrator({
  renderSessionSummary,
  renderTaskList,
  renderSelectedHeader,
  renderRunProxyEditor,
  renderTaskProxyEditor,
  renderTaskSupervisionEditor,
  renderTaskContextEditor,
  renderTaskHardwareEditor,
  renderAgents,
  renderSelectedAgentInfo,
  renderPendingMessages,
  renderBackendStatus,
  renderSessionMenu,
  renderPanel,
  renderPanelForRealtime: renderPanel,
  panelHasTextSelection,
  windowRef: window,
  visibleTasks,
  selectedTaskId: () => selectedTaskId,
  setSelectedTaskId: value => { selectedTaskId = value || null; },
  readStoredSelectedTaskId,
  defaultTaskId,
  writeStoredSelectedTaskId,
  isTaskProxyEditing,
  isTaskSupervisionEditing,
  isTaskContextEditing,
  isTaskHardwareEditing,
  isAgentsPanelEditing,
  statusGoal: () => statusData?.goal || "",
  setSummaryText: value => { if (summaryEl) summaryEl.textContent = value || ""; },
  setSelectedTitle: value => { if (selectedTitleEl) selectedTitleEl.textContent = value || ""; },
  hideConversationFilters: () => conversationFiltersEl?.classList.add("hidden")
});
window.addEventListener("aha:languagechange", () => {
  taskOptionsController.syncCollaborationFields();
  renderSessionMenu();
  renderOrchestrator.renderAll({
    forceTaskProxy: true,
    forceTaskSupervision: true,
    forceTaskContext: true,
    forceAgents: true
  });
  renderPanel();
  renderBackendStatus();
});
const statusStore = window.AHAStatusStore.createStatusStore({
  allEvents,
  agentsRuntimeCache,
  contextDetails,
  conversationStates,
  expandedMessageKeys,
  finalDetails,
  getCurrentRunId: () => currentRunId,
  setCurrentRunId: value => { currentRunId = value || ""; },
  getDefaultRunId: () => defaultRunId,
  setDefaultRunId: value => { defaultRunId = value || ""; },
  getRunsData: () => runsData,
  setRunsData: value => { runsData = Array.isArray(value) ? value : []; },
  getSelectedTaskId: () => selectedTaskId,
  setSelectedTaskId: value => { selectedTaskId = value || null; },
  getStatusData: () => statusData,
  setBackendStatusData: value => { backendStatusData = value; },
  setConversationAutoFollow: value => { conversationAutoFollow = Boolean(value); },
  hardwareIoStates,
  logStates,
  seenRealtimeEvents
}, {
  agentBackendProcessStatus,
  agentLifecycleStatus,
  backendTarget,
  clearPendingState,
  closeEventWebSocket,
  documentBody: document.body,
  initialRunId,
  initialSelectedTaskId,
  readStoredSelectedTaskId,
  renderAll: renderOrchestrator.renderAll,
  resetEventWebSocketReconnectState,
  resetRunMaintenanceState,
  restoreEventCursorFromStorage: eventCursorStore.restoreFromStorage,
  runIdOf,
  syncRunUrl,
  taskCurrentStatus,
  writeSelectedRunId: writePersistedSelectedRunId
});
const statusController = window.AHAStatusController.createStatusController({
  bootstrapData: () => bootstrapData,
  currentRunId: () => currentRunId,
  defaultRunId: () => defaultRunId,
  runsData: () => runsData,
  runsLoaded: () => runsLoaded,
  selectedTaskId: () => selectedTaskId,
  setBootstrapData: value => { bootstrapData = value; },
  setBootstrapError: value => { bootstrapError = String(value || ""); },
  setRunsData: value => { runsData = Array.isArray(value) ? value : []; },
  setRunsError: value => { runsError = String(value || ""); },
  setRunsLoaded: value => { runsLoaded = Boolean(value); },
  setSelectedTaskId: value => { selectedTaskId = value || null; },
  setStatusData: value => { statusData = value; },
  statusData: () => statusData
}, {
  apiUrl,
  applyBackendData: backends => {
    runtimeOptions.applyBackendData(backends);
    syncCreateTaskSupervisionModeFields();
    if (!isTaskSupervisionEditing()) renderTaskSupervisionEditor();
  },
  applyWorkflowTemplateData: taskOptionsController.applyWorkflowTemplateData,
  clearLoginState,
  ensureRemoteSelectedTaskId,
  escapeHtml,
  fetchJson,
  initialSelectedTaskId,
  isAuthRequiredError,
  isAgentsPanelEditing,
  panelEl,
  renderAgents,
  renderBackendStatus,
  renderFirstRunState,
  renderLoginState,
  renderPanelForRealtime,
  renderSelectedAgentInfo,
  renderSessionMenu,
  runIdOf,
  runtimeOptions,
  selectedTaskNeedsAgentDetails,
  statusStore,
  windowRef: window
});
const runActions = window.AHARunActions.createRunActions({
  documentRef: document,
  newRunGoalEl,
  runExportLogsEl
}, {
  alert,
  apiUrl,
  applyBootstrapPayload,
  applyRunListData,
  catchUpRealtimeEvents,
  closeRunCreateDialog: () => runController.closeRunCreateDialog(),
  confirmDialogAction,
  currentAppVersion,
  currentRunId: () => currentRunId,
  currentRunSummary,
  defaultRunId: () => defaultRunId,
  fetchJson,
  fetchWithTimeout,
  formatMetricBytes,
  loadRunMaintenance,
  loadRuns,
  loadStatus,
  readJsonResponse,
  refreshRunScopedView,
  renderPanelForRealtime,
  renderRunMaintenance,
  renderSchedulerResetFailures: () => renderScheduler?.resetFailures(),
  renderSessionMenu,
  requestTimeoutMs,
  resetEventWebSocketReconnectState,
  resetRunScopedState,
  runActionInFlight: () => runActionInFlight,
  runIdOf,
  runLifecycleLabel,
  runLifecycleProtectionReason,
  runDeleteProtectionReason,
  runLifecycleReasonText,
  runMaintenanceActionConfirm,
  runMaintenanceActionInFlight: () => runController.runMaintenanceActionInFlight(),
  runMaintenanceData: () => runController.runMaintenanceData(),
  runMaintenanceRunId: () => runController.runMaintenanceRunId(),
  runMaintenancePayload,
  runTitleOf,
  runsData: () => runsData,
  setRunActionInFlight: value => { runActionInFlight = Boolean(value); },
  setRunArchiveState,
  setRunLifecycleState,
  setRunMaintenanceActionInFlight: value => runController.setRunMaintenanceActionInFlight(value),
  setRunMaintenanceData: value => runController.setRunMaintenanceData(value),
  setRunMaintenanceMessage: value => runController.setRunMaintenanceMessage(value),
  setRunMaintenanceRunId: value => runController.setRunMaintenanceRunId(value),
  setRunsError: value => { runsError = String(value || ""); },
  setRunsLoaded: value => { runsLoaded = Boolean(value); },
  setWebRestartInFlight: value => { webRestartInFlight = Boolean(value); },
  setWebRestartState,
  switchRun,
  syncCurrentRunDisplay,
  syncRealtimeEvents,
  webRestartInFlight: () => webRestartInFlight,
  windowRef: window
});
bootstrapController = window.AHABootstrapController.createBootstrapController({
  panelEl
}, {
  alertError: message => alert(message),
  applyBootstrapPayload,
  backendModels: () => runtimeOptions.backendModels(),
  bootstrapConfigHelpers,
  bootstrapData: () => bootstrapData,
  bootstrapError: () => bootstrapError,
  closeEventWebSocket,
  confirmDialogAction,
  createRun: runActions.createRun,
  currentRunId: () => currentRunId,
  escapeHtml,
  fetchJson,
  loadStatus,
  locationOrigin: () => location.origin,
  modelOptionsForBackend: backend => runtimeOptions.modelOptionsForBackend(backend),
  openTaskMemoHome: () => taskMemoController.openDialog?.(),
  renderEmptyWorkspace: renderOrchestrator.renderEmptyWorkspace,
  resetEmptyRunState: () => {
    document.body.classList.add("empty-run");
    currentRunId = "";
    statusData = null;
    selectedTaskId = null;
    backendStatusData = null;
  }
});
const appActions = window.AHAAppActions.createAppActions({
  activeTab: () => activeTab,
  allTasks: () => statusData?.tasks || [],
  setActiveTab: value => { activeTab = value || "conversation"; },
  apiUrl,
  closeMobileActionPanel: () => uiShell.closeMobileActionPanel(),
  closeMobileSheets: () => uiShell.closeMobileSheets(),
  confirmDialogAction,
  ensureActiveTabData,
  fetchWithTimeout,
  hardwareIoState,
  loadAgentsRuntime,
  loadRunMaintenance,
  loadStatus,
  logState,
  renderOrchestrator,
  resetEventWebSocketReconnectState,
  resetTaskConfigEditing,
  restartWebService: runActions.restartWebService,
  upgradeWebService: runActions.upgradeWebService,
  runMaintenanceAction: runActions.runMaintenanceAction,
  saveBootstrapConfigForm,
  selectedTaskId: () => selectedTaskId,
  setConversationAutoFollow: value => { conversationAutoFollow = Boolean(value); },
  setSelectedTaskId: value => { selectedTaskId = value || null; },
  setTaskActionInFlight: value => { taskActionInFlight = Boolean(value); },
  writeStoredSelectedTaskId
});
const featureControllers = window.AHAAppControllerFactory.createFeatureControllers(domRefs, {
  addBootstrapConfigRow,
  agentBackendProcessStatus,
  agentBackendModelChanged,
  agentConfigLabel,
  agentConfigValue,
  agentRuntimeConfigChanged,
  allTasks: () => statusData?.tasks || [],
  alert,
  apiUrl,
  approvalOptions,
  bootstrapConfigFormHtml,
  bootstrapData: () => bootstrapData,
  closeMobileActionPanel: () => uiShell.closeMobileActionPanel(),
  closeMobileSheets: () => uiShell.closeMobileSheets(),
  closeTaskCreateDialog: () => uiShell.closeTaskCreateDialog(),
  collaborationModeDelegationPolicy,
  consoleRef: console,
  confirmDialogAction,
  contextDetails,
  createTaskConfirmRows,
  createTaskFallbackConfirmText,
  createTaskPayload,
  currentRunId: () => currentRunId,
  defaultTaskSupervisionMaxRounds,
  dispatchAction: appActions.dispatch,
  documentRef: document,
  ensureActiveTabData,
  escapeHtml,
  fetchJson,
  fetchWithTimeout,
  fillBootstrapProxyDefaultFor: bootstrapConfigHelpers.fillBootstrapProxyDefaultFor,
  formatDuration,
  formatLocalTimestamp,
  handleComposerSubmit,
  handleHardwareRawKey,
  onComposerInput: () => {
    // Keep the terminal's live "pending line" preview in sync with the input box as the
    // user types (line mode only; raw mode sends each key live instead).
    if (activeTab !== "hardware" || !selectedTaskId) return;
    if (hardwareIoState(selectedTaskId).rawMode) return;
    renderPanel();
  },
  loadBootstrap,
  loadStatus,
  normalizeAgentConfig,
  openTaskCreateDialog: () => uiShell.openTaskCreateDialog(),
  proxySelectOptions,
  readAgentConfigEditor,
  readAskUserGateControls,
  realtimeDebug,
  removeBootstrapConfigRow,
  renderAgents,
  renderPanel,
  resetEventWebSocketReconnectState,
  runScopedPayload,
  runtimeOptions,
  sandboxOptions,
  selectOptions,
  selectTask,
  selectedAgent,
  selectedTask,
  selectedTaskId: () => selectedTaskId,
  setCreateProxyDefaultsFromInputs,
  setPlayConsoleOpen,
  setRunMaintenanceConsoleOpen,
  setSelectedTaskId: value => { selectedTaskId = value || null; },
  setSessionMenu,
  setWeixinConsoleOpen,
  syncCreateTaskSupervisionModeFields,
  syncBootstrapModelOptions,
  syncBootstrapProxyDefaultsForInput: bootstrapConfigHelpers.syncBootstrapProxyDefaultsForInput,
  syncMobileComposerToggle: hasMessage => uiShell.syncMobileComposerToggle(hasMessage),
  taskOptionsController,
  taskMetadata,
  taskDisplayStatus,
  taskSupervisionPayloadFromMode,
  taskSupervisionSummary,
  windowRef: window,
  writeStoredSelectedTaskId
});
const {
  agentConfigController,
  messageComposer,
  settingsController,
  taskCreateController,
  taskMemoController
} = featureControllers;
playConsoleController = featureControllers.playConsoleController;
weixinConsoleController = featureControllers.weixinConsoleController;
const taskController = window.AHATaskController.createTaskController({
  headerWorkspaceDirEl,
  mobileTaskStatusEl,
  mobileTaskSummaryEl,
  mobileTaskTitleEl,
  selectedIdEl,
  selectedStatusEl,
  selectedTaskMetaEl,
  selectedTitleEl,
  taskSettingsActionsEl,
  taskSettingsCloseEl,
  taskSettingsPanelEl,
  taskSettingsSubtitleEl,
  tasksEl,
  taskVisibilityFilterEl
}, {
  activeTab: () => activeTab,
  allTasks: () => statusData?.tasks || [],
  defaultTaskId,
  documentRef: document,
  dispatchAction: appActions.dispatch,
  closeTaskMemoPage: () => taskMemoController.closeDialog?.(),
  normalizeTaskVisibilityFilter,
  renderAgents,
  renderConversationFilters,
  renderPanel,
  renderTaskContextEditor,
  renderTaskHardwareEditor,
  renderTaskProxyEditor,
  renderTaskSupervisionEditor,
  isTaskContextEditing,
  isTaskHardwareEditing,
  isTaskProxyEditing,
  isTaskSupervisionEditing,
  selectedTaskId: () => selectedTaskId,
  selectedTask,
  setTaskSettingsEditorTaskId: taskConfigController.setTaskSettingsEditorTaskId,
  setSelectedTaskId: value => { selectedTaskId = value || null; },
  setTaskVisibilityFilter: value => { taskVisibilityFilter = value; },
  escapeHtml,
  taskActivityStatus,
  taskCollaborationSummary,
  taskContextManagementPolicy,
  taskContextSummary,
  taskHardwareDebugPolicy,
  taskHardwareDebugSummary,
  taskDisplayStatus,
  taskProxyConfigured,
  taskListItemClass,
  taskListItemHtml,
  taskListTitle,
  taskProxySummary,
  taskSupervisionPolicy,
  taskSupervisionSummary,
  taskTimingLabel,
  taskVisibilityFilter: () => taskVisibilityFilter,
  taskVisibilityFilterHtml,
  taskWorkflowSummary,
  pathName,
  visibleTasksForFilter: taskListVisibleTasks,
  windowRef: window,
  writeStoredSelectedTaskId
});
const agentController = window.AHAAgentController.createAgentController({
  agentTargetEl,
  agentsEl,
  selectedAgentInfoEl
}, {
  agentBackendProcessLabel,
  agentBackendProcessStatus,
  agentConfigController,
  agentContextPressureSummary,
  agentDisplayModel,
  agentLifecycleDisplay,
  agentLifecycleStatus,
  agentModelValue,
  agentOptionGroups,
  agentOptionLabel,
  agentRuntimeDefaults,
  agentStatusTiming,
  agentStatusTimingText,
  closeMobileSheets: () => uiShell.closeMobileSheets(),
  documentRef: document,
  ensureConversationLoaded,
  escapeHtml,
  formatClock,
  formatLocalTimestamp,
  loadAgentsRuntime,
  modelLabelForBackend: (backend, value) => runtimeOptions.modelLabelForBackend(backend, value),
  normalizeAgentConfig,
  renderConversationFilters,
  renderPanel,
  renderPendingMessages,
  selectedTask,
  setConversationAutoFollow: value => { conversationAutoFollow = Boolean(value); },
  taskProxySummary,
  windowRef: window
});
const runController = window.AHARunController.createRunController({
  appVersionEl,
  authLogoutEl,
  documentRef: document,
  openRunCreateEl,
  runCreateDialogEl,
  headerRunConsoleEl,
  headerRunTitleEl,
  closeRunCreateEl,
  cancelRunCreateEl,
  newRunGoalEl,
  playConsoleEl,
  playConsolePopoverEl,
  renameRunNameEl,
  runCreateFormEl,
  runExportEl,
  runExportLogsEl,
  runImportEl,
  runImportFileEl,
  runArchiveStateEl,
  runIdEl,
  runMaintenanceCloseEl,
  runMaintenanceDetailEl,
  runMaintenancePopoverEl,
  runMaintenanceRefreshEl,
  runMaintenanceSummaryEl,
  runLifecycleActionsEl,
  runLifecycleEl,
  runLifecycleFilterEl,
  runLifecycleStateEl,
  runManagerEl,
  runSettingsActionsEl,
  runSettingsCloseEl,
  runSettingsPanelEl,
  runSettingsProtectionEl,
  runSettingsSubtitleEl,
  runRenameFormEl,
  runSelectEl,
  runStateEl,
  headerWorkspaceDirEl,
  sessionDetailTextEl,
  sessionControlEl,
  sessionMenuEl,
  sessionRefreshEl,
  sessionTitleEl,
  sessionToggleEl,
  summaryEl,
  taskRunContextEl,
  webRestartEl,
  webRestartStateEl,
  webUpgradeEl,
  weixinConsoleEl,
  weixinConsolePopoverEl
}, {
  createRun: runActions.createRun,
  currentRunId: () => currentRunId,
  currentRunSummary,
  currentAppVersion,
  dispatchAction: appActions.dispatch,
  deleteRunFromMenu: runActions.deleteRunFromMenu,
  exportCurrentRun: runActions.exportCurrentRun,
  fallbackCurrentRun,
  importRunArchive: runActions.importRunArchive,
  loadAccessControlStatus,
  loadRuns,
  logoutAuthSession,
  playConsoleOpen: () => playConsoleController.isOpen(),
  renameCurrentRun: runActions.renameCurrentRun,
  apiUrl,
  bootstrapData: () => bootstrapData,
  escapeHtml,
  fetchJson,
  formatMetricBytes,
  renderAccessControlStatus,
  renderPlayConsolePopover,
  runMaintenanceView,
  renderWeixinConsolePopover,
  realtimeTransportText,
  runActionInFlight: () => runActionInFlight,
  runIdOf,
  runLifecycleActionsView,
  runLifecycleClass,
  runLifecycleFiltersHtml: runLifecycleView.filtersHtml,
  runLifecycleLabel,
  runLifecycleRowsHtml: runLifecycleView.rowsHtml,
  runLifecycleTitle,
  runsData: () => runsData,
  runsError: () => runsError,
  runTitleOf,
  sessionOptionLabel,
  setPlayConsoleOpen,
  setWeixinConsoleOpen,
  statusData: () => statusData,
  switchRun,
  updateStatusRunTitle: (runName, updatedAt) => {
    if (statusData) {
      statusData.goal = runName || statusData.goal;
      statusData.updated_at = updatedAt || statusData.updated_at;
    }
  },
  updateRunLifecycleFromMenu: runActions.updateRunLifecycleFromMenu,
  webRestartInFlight: () => webRestartInFlight,
  weixinConsoleOpen: () => weixinConsoleController.isOpen()
});
// Translate a keydown into the bytes a serial terminal would send. Returns null for keys
// that should be ignored (modifiers alone, F-keys, unknown Ctrl combos).
function hardwareRawKeyToBytes(event) {
  const key = event.key;
  if (event.ctrlKey) {
    if (key && key.length === 1) {
      const lower = key.toLowerCase();
      if (lower >= "a" && lower <= "z") return String.fromCharCode(lower.charCodeAt(0) - 96);
      const ctrlPunct = { "@": "\u0000", "[": "\u001b", "\\": "\u001c", "]": "\u001d", "^": "\u001e", "_": "\u001f", " ": "\u0000" };
      if (key in ctrlPunct) return ctrlPunct[key];
    }
    return null;
  }
  switch (key) {
    case "Enter": return "\r";
    case "Backspace": return "\u007f";
    case "Tab": return "\t";
    case "Escape": return "\u001b";
    case "ArrowUp": return "\u001b[A";
    case "ArrowDown": return "\u001b[B";
    case "ArrowRight": return "\u001b[C";
    case "ArrowLeft": return "\u001b[D";
    case "Home": return "\u001b[H";
    case "End": return "\u001b[F";
    case "Delete": return "\u001b[3~";
    case "PageUp": return "\u001b[5~";
    case "PageDown": return "\u001b[6~";
    default:
      if (key && key.length === 1) return key; // a printable character
      return null;
  }
}

// Named on-screen keys -> the bytes a serial terminal would send.
function hardwareNamedKeyBytes(name) {
  const map = {
    enter: "\r", tab: "\t", esc: "\u001b", space: " ", backspace: "\u007f",
    up: "\u001b[A", down: "\u001b[B", right: "\u001b[C", left: "\u001b[D",
    home: "\u001b[H", end: "\u001b[F", "page-up": "\u001b[5~", "page-down": "\u001b[6~",
    delete: "\u001b[3~",
    "ctrl-a": "\u0001", "ctrl-c": "\u0003", "ctrl-d": "\u0004", "ctrl-e": "\u0005",
    "ctrl-k": "\u000b", "ctrl-l": "\u000c", "ctrl-r": "\u0012", "ctrl-u": "\u0015",
    "ctrl-w": "\u0017", "ctrl-z": "\u001a"
  };
  return map[name] || "";
}

// Raw-mode keystroke handler bound to the composer textarea. Returns true when it consumed
// the key (so the composer's own keydown logic is skipped). Sending happens live, byte by
// byte, exactly like a serial terminal.
function handleHardwareRawKey(event) {
  if (!selectedTaskId || activeTab !== "hardware") return false;
  const st = hardwareIoState(selectedTaskId);
  if (!st || !st.rawMode || st.readOnly || !st.device) return false;
  if (event.isComposing || event.keyCode === 229) return false; // mid-IME composition
  if (event.metaKey || event.altKey) return false;               // leave OS/browser shortcuts alone
  const bytes = hardwareRawKeyToBytes(event);
  if (bytes === null) return false;
  event.preventDefault();
  void sendHardwareRawBytes(bytes);
  return true;
}

async function sendHardwareRawBytes(bytes) {
  const taskId = selectedTaskId;
  if (!taskId || !bytes) return;
  try {
    await fetchJson(apiUrl(`/api/task/${encodeURIComponent(taskId)}/hardware-send`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(runScopedPayload({ data: bytes }))
    }, "Failed to send key");
  } catch (err) {
    return;
  }
  try {
    await conversationController.loadHardwareIoPage(taskId, true);
  } catch (err) {
    return;
  }
  if (activeTab === "hardware") renderPanel();
}

window.AHAControllerRegistry.bindTopLevelEvents(domRefs, {
  activeTab: () => activeTab,
  activateTab,
  addBootstrapConfigRow,
  agentController,
  alertError: message => alert(message),
  backendTarget,
  closePromptMetricsBreakdowns: promptMetricsPopover.closeBreakdowns,
  closePromptMetricsPopover: promptMetricsPopover.close,
  closePromptMetricsPopoverForOutsideEvent: promptMetricsPopover.closeForOutsideEvent,
  compactResetSelectedSession,
  copyTimelineMessage,
  createRunFromBootstrapForm,
  defaultAskUserGates,
  defaultNoProxy,
  dispatchAction: appActions.dispatch,
  documentRef: document,
  eventBindings,
  flushDeferredPanelRender,
  hasConversationFilter: key => key in conversationFilters,
  hardwareIoState,
  hardwareBridgeControl: async action => {
    // Pause releases the physical port (so an operator can use minicom); resume
    // re-acquires it. Both target the machine-level bridge for this task's device.
    if (!selectedTaskId) return;
    const endpoint = action === "pause" ? "hardware-pause" : "hardware-resume";
    try {
      await fetchJson(apiUrl(`/api/task/${encodeURIComponent(selectedTaskId)}/${endpoint}`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(runScopedPayload({}))
      }, "Failed to control hardware bridge");
    } catch (err) {
      return;
    }
    try {
      await conversationController.loadHardwareIoPage(selectedTaskId, true);
    } catch (err) {
      return;
    }
    if (activeTab === "hardware") renderPanel();
  },
  hardwareSendKey: key => {
    // On-screen keys (line-mode quick keys + the raw-mode accessory bar) map a name to the
    // bytes a terminal would send. The raw bar covers what mobile soft keyboards lack (Esc,
    // Tab, arrows, Ctrl-combos) so vi / tab-completion work on phones too.
    if (!selectedTaskId) return;
    const bytes = hardwareNamedKeyBytes(key);
    if (!bytes) return;
    void sendHardwareRawBytes(bytes);
    // Keep the composer focused so a mobile soft keyboard does not collapse between taps.
    const input = document.getElementById("message");
    if (input && typeof input.focus === "function") input.focus({ preventScroll: true });
  },
  hardwareToggleRawMode: () => {
    // Raw mode sends every keystroke live (real minicom-style input): Tab completion, arrows,
    // Ctrl-combos and vi all work. Capture happens on the persistent composer textarea (NOT
    // the terminal), so a flood of serial output never steals input focus.
    if (!selectedTaskId) return;
    const st = hardwareIoState(selectedTaskId);
    st.rawMode = !st.rawMode;
    if (activeTab !== "hardware") return;
    renderPanel();
    if (st.rawMode) {
      const input = document.getElementById("message");
      if (input && typeof input.focus === "function") input.focus({ preventScroll: true });
    }
  },
  initDesktopSidebars: () => uiShell.initDesktopSidebars(),
  initMobileActionPanel: () => uiShell.initMobileActionPanel(),
  initMobileSheets: () => uiShell.initMobileSheets(),
  initMobileViewport: () => uiShell.initMobileViewport(),
  initSessionControl,
  initSettingsDialog,
  initTaskCreateDialog: () => uiShell.initTaskCreateDialog(),
  interruptBackend,
  isPanelNearBottom,
  loadConversationPage,
  loadOlderConversation,
  loadOlderLogs,
  logState,
  messageComposer,
  openPromptMetricsKey: promptMetricsPopover.openKey,
  openTaskCreateDialog: () => uiShell.openTaskCreateDialog(),
  positionPromptMetricsPopover: promptMetricsPopover.position,
  realtimeDebug,
  removeBootstrapConfigRow,
  removePendingMessage,
  renderAskUserGateControls,
  renderModelOptions: () => runtimeOptions.renderModelOptions(),
  renderPanel,
  requestRealtimeCatchup,
  scrubAuthTokenFromUrl,
  selectedTask,
  selectedTaskId: () => selectedTaskId,
  setConversationAutoFollow: value => { conversationAutoFollow = Boolean(value); },
  setConversationFilter: (key, value) => { conversationFilters[key] = Boolean(value); },
  setExpandedMessageKey,
  setOpenPromptMetricsKey: promptMetricsPopover.setOpenKey,
  submitLoginForm,
  syncBootstrapModelOptions,
  fillBootstrapProxyDefaultFor: bootstrapConfigHelpers.fillBootstrapProxyDefaultFor,
  syncBootstrapProxyDefaultsForInput: bootstrapConfigHelpers.syncBootstrapProxyDefaultsForInput,
  syncCollaborationFields: taskOptionsController.syncCollaborationFields,
  syncCreateProxyDefaultForBackend,
  syncCreateTaskSupervisionModeFields,
  syncTaskContextFields,
  syncWorkflowTemplateHelp: taskOptionsController.syncWorkflowTemplateHelp,
  taskConfigController,
  taskCreateController,
  taskMemoController,
  taskController,
  windowRef: window
});

window.AHAAppRuntime = Object.freeze({
  start() {
    renderScheduler = window.AHAControllerRegistry.startApp({
      actionInFlight: () => taskActionInFlight || runActionInFlight,
      pollHardwareStream: async () => {
        // The device bridge writes a machine-level stream (not the run event bus),
        // so the live Hardware console refreshes by polling while it is open.
        if (activeTab !== "hardware" || !selectedTaskId) return;
        const st = hardwareIoState(selectedTaskId);
        const before = st.afterOffset;
        try {
          await conversationController.loadHardwareIoPage(selectedTaskId, true);
        } catch (err) {
          return;
        }
        if (activeTab !== "hardware") return;
        // Only re-render when the stream actually advanced. An idle board (no new output)
        // must not rebuild the panel — otherwise it would reset the key bar's scroll and
        // interrupt an in-progress tap/drag every second.
        if (hardwareIoState(selectedTaskId).afterOffset === before) return;
        renderPanel();
      },
      authRequired: () => authController.isRequired(),
      bootstrapError: () => bootstrapError,
      currentRunId: () => currentRunId,
      ensureConversationLoaded,
      isAuthRequiredError,
      latestTurnTiming,
      loadAccessControlStatus,
      loadBootstrap,
      loadStatus,
      maybeAutoFlushPending,
      maybeRefreshConversationBackendSessionFallback,
      openInitialTaskMemoHome: () => {
        if (initialTaskMemoHomeActive) {
          taskMemoController.openDialog?.();
        } else {
          taskMemoController.closeDialog?.();
        }
      },
      pollInterval,
      refreshTaskMemosIfOpen: () => taskMemoController.refreshIfOpen?.(),
      realtimeConnected: () => realtimeClient.readyStateName() === "open",
      renderActiveTurn: () => renderOrchestrator.renderActiveTurn(),
      renderBootstrapError,
      renderError: err => { panelEl.innerHTML = `<pre>${escapeHtml(String(err))}</pre>`; },
      renderFirstRunState,
      renderLoginState,
      renderPanelForRealtime: renderOrchestrator.renderForEvent,
      renderPendingMessages,
      renderSelectedHeader,
      renderTaskList,
      selectedTask,
      selectedTaskRealtimeActive,
      setBootstrapError: value => { bootstrapError = value || ""; },
      syncRealtimeEvents,
      taskActivityStatus
    });
    return renderScheduler;
  }
});
