(() => {
  const elementIds = Object.freeze({
    runIdEl: "run-id",
    runStateEl: "run-state",
    sessionControlEl: "session-control",
    sessionToggleEl: "session-toggle",
    sessionTitleEl: "session-title",
    sessionMenuEl: "session-menu",
    sessionRefreshEl: "session-refresh",
    headerRunConsoleEl: "header-run-console",
    headerRunTitleEl: "header-run-title",
    runSelectEl: "run-select",
    runRenameFormEl: "run-rename-form",
    renameRunNameEl: "rename-run-name",
    openRunCreateEl: "open-run-create",
    runCreateDialogEl: "run-create-dialog",
    closeRunCreateEl: "close-run-create",
    cancelRunCreateEl: "cancel-run-create",
    runCreateFormEl: "run-create-form",
    newRunGoalEl: "new-run-goal",
    runExportEl: "run-export",
    runImportEl: "run-import",
    runExportLogsEl: "run-export-logs",
    runImportFileEl: "run-import-file",
    runArchiveStateEl: "run-archive-state",
    runManagerEl: "run-manager",
    runManagerToggleEl: "run-manager-toggle",
    runLifecycleFilterEl: "run-lifecycle-filter",
    runLifecycleActionsEl: "run-lifecycle-actions",
    runLifecycleStateEl: "run-lifecycle-state",
    runSettingsActionsEl: "run-settings-actions",
    runSettingsCloseEl: "run-settings-close",
    runSettingsPanelEl: "run-settings-panel",
    runSettingsProtectionEl: "run-settings-protection",
    runSettingsSubtitleEl: "run-settings-subtitle",
    runMaintenanceRefreshEl: "run-maintenance-refresh",
    runMaintenanceCloseEl: "run-maintenance-close",
    runMaintenanceSummaryEl: "run-maintenance-summary",
    runMaintenanceDetailEl: "run-maintenance-detail",
    ahaSettingsEl: "aha-settings",
    settingsDialogEl: "settings-dialog",
    closeSettingsEl: "close-settings",
    settingsContentEl: "settings-content",
    webRestartEl: "web-restart",
    webPublishConsoleEl: "web-publish-console",
    webUpgradeEl: "web-upgrade",
    authLogoutEl: "auth-logout",
    accessControlStatusEl: "access-control-status",
    runMaintenancePopoverEl: "run-maintenance-popover",
    observeProxyEl: "observe-proxy",
    observeProxyPopoverEl: "observe-proxy-popover",
    weixinConsoleEl: "weixin-console",
    weixinConsolePopoverEl: "weixin-console-popover",
    skillsConsoleEl: "skills-console",
    skillsConsolePopoverEl: "skills-console-popover",
    tokenUsageEl: "token-usage",
    tokenUsagePopoverEl: "token-usage-popover",
    playConsoleEl: "play-console",
    playConsolePopoverEl: "play-console-popover",
    webRestartStateEl: "web-restart-state",
    sessionDetailTextEl: "session-detail-text",
    runLifecycleEl: "run-lifecycle",
    webServiceAddressEl: "web-service-address",
    appVersionEl: "app-version",
    headerWorkspaceDirEl: "header-workspace-dir",
    mobileTaskSummaryEl: "mobile-task-summary",
    mobileTaskTitleEl: "mobile-task-title",
    mobileTaskStatusEl: "mobile-task-status",
    summaryEl: "summary",
    openTaskCreateEl: "open-task-create",
    openTaskViewEl: "open-task-view",
    openTaskMemosEl: "open-task-memos",
    openKnowledgeBaseEl: "open-knowledge-base",
    knowledgeHomeEl: "knowledge-home",
    knowledgeHomeFrameEl: "knowledge-home-frame",
    taskCreateDialogEl: "task-create-dialog",
    closeTaskCreateEl: "close-task-create",
    closeTaskMemosEl: "close-task-memos",
    taskMemoDialogEl: "task-memo-dialog",
    taskMemoPrevYearEl: "task-memo-prev-year",
    taskMemoPrevMonthEl: "task-memo-prev-month",
    taskMemoNextMonthEl: "task-memo-next-month",
    taskMemoNextYearEl: "task-memo-next-year",
    taskMemoCurrentMonthEl: "task-memo-current-month",
    taskMemoCalendarEl: "task-memo-calendar",
    taskMemoCalendarCollapseEl: "task-memo-calendar-collapse",
    taskMemoFilterEl: "task-memo-filter",
    taskMemoEditorJumpEl: "task-memo-editor-jump",
    taskMemoListEl: "task-memo-list",
    taskMemoFormEl: "task-memo-form",
    taskMemoEditorColumnEl: "task-memo-editor-column",
    taskMemoEditorTitleEl: "task-memo-editor-title",
    taskMemoEditorHintEl: "task-memo-editor-hint",
    taskMemoEditDateEl: "task-memo-edit-date",
    taskMemoEditEndDateEl: "task-memo-edit-end-date",
    taskMemoCompletedDateFieldEl: "task-memo-completed-date-field",
    taskMemoClosedDateFieldEl: "task-memo-closed-date-field",
    taskMemoEditCompletedDateEl: "task-memo-edit-completed-date",
    taskMemoEditClosedDateEl: "task-memo-edit-closed-date",
    taskMemoEditTitleEl: "task-memo-edit-title",
    taskMemoMarkdownEditorEl: "task-memo-markdown-editor",
    taskMemoAttachmentListEl: "task-memo-attachments",
    taskMemoDescriptionEditorEl: "task-memo-description-editor",
    taskMemoEditDescriptionEl: "task-memo-edit-description",
    taskMemoPreviewModeEl: "task-memo-preview-mode",
    taskMemoEditModeEl: "task-memo-edit-mode",
    taskMemoImageFileEl: "task-memo-image-file",
    taskMemoImageUploadEl: "task-memo-image-upload",
    taskMemoEditStatusEl: "task-memo-edit-status",
    taskMemoStatusOptionsEl: "task-memo-status-options",
    taskMemoTaskLinkClearEl: "task-memo-task-link-clear",
    taskMemoTaskLinkFieldEl: "task-memo-task-link-field",
    taskMemoTaskPickerEl: "task-memo-task-picker",
    taskMemoTaskPickerFilterEl: "task-memo-task-picker-filter",
    taskMemoTaskPickerListEl: "task-memo-task-picker-list",
    taskMemoTaskPickerSearchEl: "task-memo-task-picker-search",
    taskMemoTaskPickerToggleEl: "task-memo-task-picker-toggle",
    taskMemoNewEl: "task-memo-new",
    taskMemoCancelEl: "task-memo-cancel",
    taskMemoDeleteEl: "task-memo-delete",
    taskMemoConvertEl: "task-memo-convert",
    taskMemoSaveEl: "task-memo-save",
    taskMemoStateEl: "task-memo-state",
    cancelTaskCreateEl: "cancel-task-create",
    collapseOverviewEl: "collapse-overview",
    collapseAgentsEl: "collapse-agents",
    overviewRailToggleEl: "overview-rail-toggle",
    agentsRailToggleEl: "agents-rail-toggle",
    mobileSheetBackdropEl: "mobile-sheet-backdrop",
    openTasksSheetEl: "open-tasks-sheet",
    openAgentsSheetEl: "open-agents-sheet",
    closeTasksSheetEl: "close-tasks-sheet",
    closeAgentsSheetEl: "close-agents-sheet",
    mobileActionPanelEl: "mobile-action-panel",
    mobileActionsToggleEl: "mobile-actions-toggle",
    tasksEl: "tasks",
    taskSettingsActionsEl: "task-settings-actions",
    taskSettingsCloseEl: "task-settings-close",
    taskSettingsPanelEl: "task-settings-panel",
    taskSettingsSubtitleEl: "task-settings-subtitle",
    taskVisibilityFilterEl: "task-visibility-filter",
    runProxyEditorEl: "run-proxy-editor",
    runProxyFormEl: "run-proxy-form",
    runProxyEnabledEl: "run-proxy-enabled",
    runHttpProxyEl: "run-http-proxy",
    runHttpsProxyEl: "run-https-proxy",
    runNoProxyEl: "run-no-proxy",
    runProxyStateEl: "run-proxy-state",
    selectedIdEl: "selected-id",
    selectedTitleEl: "selected-title",
    selectedStatusEl: "selected-status",
    selectedTaskMetaEl: "selected-task-meta",
    panelEl: "panel",
    sendFormEl: "send-form",
    messageEl: "message",
    messageImageFileEl: "message-image-file",
    messageImageUploadEl: "message-image-upload",
    taskFormEl: "task-form",
    newTaskTitleEl: "new-task-title",
    newTaskDescriptionEl: "new-task-description",
    agentTargetEl: "agent-target",
    agentsEl: "agents",
    taskBackendEl: "task-backend",
    taskModelEl: "task-model",
    taskReasoningEffortEl: "task-reasoning-effort",
    taskSandboxEl: "task-sandbox",
    taskApprovalEl: "task-approval",
    taskProxyEnabledEl: "task-proxy-enabled",
    taskProxyDefaultsPreviewEl: "task-proxy-defaults-preview",
    taskContextAutoCompactEnabledEl: "task-token-saving-enabled",
    taskContextThresholdFieldEl: "task-token-saving-threshold-field",
    taskContextThresholdEl: "task-token-saving-threshold",
    taskObserveProxyEnabledEl: "task-observe-proxy-enabled",
    taskSkillSelectEl: "task-skill-select",
    taskRunContextEl: "task-run-context",
    collaborationModeEl: "collaboration-mode",
    collaborationModeHelpEl: "collaboration-mode-help",
    workflowTemplateEl: "workflow-template",
    workflowTemplateHelpEl: "workflow-template-help",
    maxSubAgentsEl: "max-sub-agents",
    maxSubAgentsFieldEl: "max-sub-agents-field",
    taskSupervisionModeEl: "task-supervision-mode",
    taskSupervisionHostModelFieldEl: "task-supervision-host-model-field",
    taskSupervisionHostModelEl: "task-supervision-host-model",
    taskSupervisionHostProxyFieldEl: "task-supervision-host-proxy-field",
    taskSupervisionHostProxyEnabledEl: "task-supervision-host-proxy-enabled",
    taskSupervisionMaxRoundsFieldEl: "task-supervision-max-rounds-field",
    taskSupervisionMaxRoundsEl: "task-supervision-max-rounds",
    taskSupervisionAskUserFieldEl: "task-supervision-ask-user-field",
    taskSupervisionAskUserGatesEl: "task-supervision-ask-user-gates",
    workspaceSelectEl: "workspace-select",
    workspaceCustomEl: "workspace-custom",
    taskProxyEditorEl: "task-proxy-editor",
    taskProxyFormEl: "task-proxy-form",
    selectedTaskProxyEnabledEl: "selected-task-proxy-enabled",
    taskProxyStateEl: "task-proxy-state",
    taskSupervisionEditorEl: "task-supervision-editor",
    taskSupervisionFormEl: "task-supervision-form",
    selectedTaskSupervisionModeEl: "selected-task-supervision-mode",
    selectedTaskSupervisionHostModelFieldEl: "selected-task-supervision-host-model-field",
    selectedTaskSupervisionHostModelEl: "selected-task-supervision-host-model",
    selectedTaskSupervisionHostProxyFieldEl: "selected-task-supervision-host-proxy-field",
    selectedTaskSupervisionHostProxyEnabledEl: "selected-task-supervision-host-proxy-enabled",
    selectedTaskSupervisionMaxRoundsFieldEl: "selected-task-supervision-max-rounds-field",
    selectedTaskSupervisionMaxRoundsEl: "selected-task-supervision-max-rounds",
    selectedTaskSupervisionAskUserFieldEl: "selected-task-supervision-ask-user-field",
    selectedTaskSupervisionAskUserGatesEl: "selected-task-supervision-ask-user-gates",
    taskSupervisionStateEl: "task-supervision-state",
    taskContextEditorEl: "task-token-saving-editor",
    taskContextFormEl: "task-token-saving-form",
    selectedTaskContextAutoCompactEnabledEl: "selected-task-token-saving-enabled",
    selectedTaskContextThresholdFieldEl: "selected-task-token-saving-threshold-field",
    selectedTaskContextThresholdEl: "selected-task-token-saving-threshold",
    taskContextStateEl: "task-token-saving-state",
    taskObserveProxyEditorEl: "task-observe-proxy-editor",
    taskObserveProxyFormEl: "task-observe-proxy-form",
    selectedTaskObserveProxyEnabledEl: "selected-task-observe-proxy-enabled",
    taskObserveProxyStateEl: "task-observe-proxy-state",
    taskSkillsEditorEl: "task-skills-editor",
    taskSkillsFormEl: "task-skills-form",
    selectedTaskSkillSelectEl: "selected-task-skill-select",
    taskSkillsStateEl: "task-skills-state",
    taskHardwareEditorEl: "task-hardware-editor",
    taskHardwareFormEl: "task-hardware-form",
    taskHardwareStateEl: "task-hardware-state",
    taskCreateConfirmDialogEl: "task-create-confirm",
    taskCreateConfirmDetailsEl: "task-create-confirm-details",
    taskDraftStateEl: "task-draft-state",
    taskMemoLinkClearEl: "task-memo-link-clear",
    taskMemoLinkSummaryEl: "task-memo-link-summary",
    taskMemoPickerEl: "task-memo-picker",
    taskMemoPickerFilterEl: "task-memo-picker-filter",
    taskMemoPickerListEl: "task-memo-picker-list",
    taskMemoPickerSearchEl: "task-memo-picker-search",
    taskMemoPickerToggleEl: "task-memo-picker-toggle",
    agentRuntimeConfirmDialogEl: "agent-runtime-confirm",
    agentRuntimeConfirmMessageEl: "agent-runtime-confirm-message",
    selectedAgentInfoEl: "selected-agent-info",
    backendStatusEl: "backend-status",
    pendingMessagesEl: "pending-messages",
    conversationFiltersEl: "conversation-filters",
    commandMenuEl: "command-menu",
    loginViewEl: "login-view",
    loginFormEl: "login-form",
    loginTokenEl: "login-token",
    loginStateEl: "login-state"
  });

  function collectDomRefs(documentRef = document) {
    return Object.fromEntries(
      Object.entries(elementIds).map(([name, id]) => [name, documentRef.getElementById(id)])
    );
  }

  function bindTopLevelEvents(elements = {}, deps = {}) {
    deps.eventBindings?.bindTabButtons?.({ documentRef: deps.documentRef }, { activateTab: deps.activateTab });

    elements.loginFormEl?.addEventListener("submit", event => {
      event.preventDefault();
      void deps.submitLoginForm?.();
    });

    deps.taskCreateController?.bind?.();
    deps.taskMemoController?.bind?.();
    deps.messageComposer?.bind?.();

    elements.pendingMessagesEl?.addEventListener("click", event => {
      const button = event.target instanceof Element ? event.target.closest("[data-remove-pending]") : null;
      if (!button) return;
      deps.removePendingMessage?.(button.dataset.removePending || "");
    });

    elements.backendStatusEl?.addEventListener("click", async event => {
      const button = event.target instanceof Element ? event.target.closest("[data-backend-action='interrupt']") : null;
      if (!button) return;
      const task = deps.selectedTask?.();
      if (!task) return;
      button.disabled = true;
      try {
        await deps.interruptBackend?.(task, deps.backendTarget?.());
      } catch (err) {
        deps.alertError?.(err?.message || String(err));
      } finally {
        button.disabled = false;
      }
    });

    elements.conversationFiltersEl?.addEventListener("change", async event => {
      const input = event.target instanceof HTMLInputElement ? event.target : null;
      const key = input?.dataset.conversationFilter;
      if (!key || !deps.hasConversationFilter?.(key)) return;
      deps.setConversationFilter?.(key, input.checked);
      deps.setConversationAutoFollow?.(true);
      await deps.loadConversationPage?.(deps.selectedTaskId?.(), deps.backendTarget?.(), false, true);
      deps.renderPanel?.();
    });

    deps.eventBindings?.bindPanelEvents?.({
      panelEl: elements.panelEl,
      documentRef: deps.documentRef,
      windowRef: deps.windowRef
    }, {
      activeTab: deps.activeTab,
      setConversationAutoFollow: deps.setConversationAutoFollow,
      selectedTaskId: deps.selectedTaskId,
      logState: deps.logState,
      isPanelNearBottom: deps.isPanelNearBottom,
      positionPromptMetricsPopover: deps.positionPromptMetricsPopover,
      loadOlderLogs: deps.loadOlderLogs,
      saveBootstrapConfigForm: form => deps.dispatchAction?.("settings-save", { form }),
      createRunFromBootstrapForm: deps.createRunFromBootstrapForm,
      fillBootstrapProxyDefaultFor: deps.fillBootstrapProxyDefaultFor,
      syncBootstrapModelOptions: deps.syncBootstrapModelOptions,
      syncBootstrapProxyDefaultsForInput: deps.syncBootstrapProxyDefaultsForInput,
      openTaskCreateDialog: deps.openTaskCreateDialog,
      hardwareBridgeControl: deps.hardwareBridgeControl,
      hardwareSendKey: deps.hardwareSendKey,
      hardwareToggleRawMode: deps.hardwareToggleRawMode,
      addBootstrapConfigRow: deps.addBootstrapConfigRow,
      removeBootstrapConfigRow: deps.removeBootstrapConfigRow,
      copyTimelineMessage: deps.copyTimelineMessage,
      compactResetSelectedSession: deps.compactResetSelectedSession,
      loadOlderConversation: deps.loadOlderConversation,
      setExpandedMessageKey: deps.setExpandedMessageKey,
      openPromptMetricsKey: deps.openPromptMetricsKey,
      setOpenPromptMetricsKey: deps.setOpenPromptMetricsKey,
      closePromptMetricsBreakdowns: deps.closePromptMetricsBreakdowns,
      closePromptMetricsPopoverForOutsideEvent: deps.closePromptMetricsPopoverForOutsideEvent,
      closePromptMetricsPopover: deps.closePromptMetricsPopover
    });

    deps.taskController?.bind?.();
    deps.agentController?.bind?.();
    deps.taskConfigController?.bind?.();
    elements.taskBackendEl?.addEventListener("change", () => {
      deps.renderModelOptions?.();
      deps.syncCreateProxyDefaultForBackend?.();
    });
    elements.taskModelEl?.addEventListener("change", () => {
      deps.fillReasoningEffortSelect?.(elements.taskReasoningEffortEl, elements.taskBackendEl?.value, elements.taskModelEl?.value, elements.taskReasoningEffortEl?.value || "");
    });
    elements.collaborationModeEl?.addEventListener("change", deps.syncCollaborationFields);
    elements.workflowTemplateEl?.addEventListener("change", deps.syncWorkflowTemplateHelp);
    elements.workspaceSelectEl?.addEventListener("change", () => {
      const isCustom = elements.workspaceSelectEl.value === "__custom__";
      elements.workspaceCustomEl?.classList.toggle("hidden", !isCustom);
      if (isCustom) elements.workspaceCustomEl?.focus();
    });

    deps.eventBindings?.bindRealtimeDocumentEvents?.({
      documentRef: deps.documentRef,
      windowRef: deps.windowRef
    }, {
      realtimeDebug: deps.realtimeDebug,
      requestRealtimeCatchup: deps.requestRealtimeCatchup,
      flushDeferredPanelRender: deps.flushDeferredPanelRender
    });

    deps.initTaskCreateDialog?.();
    deps.initSettingsDialog?.();
    deps.renderAskUserGateControls?.(elements.taskSupervisionAskUserGatesEl, deps.defaultAskUserGates?.());
    deps.syncCollaborationFields?.();
    deps.syncCreateTaskSupervisionModeFields?.();
    deps.syncTaskContextFields?.();
    deps.initDesktopSidebars?.();
    deps.initMobileViewport?.();
    deps.initMobileSheets?.();
    deps.initMobileActionPanel?.();
    deps.initSessionControl?.();
    deps.scrubAuthTokenFromUrl?.();
  }

  function startApp(deps = {}) {
    const renderScheduler = window.AHARenderScheduler.createRenderScheduler({
      actionInFlight: deps.actionInFlight,
      authRequired: deps.authRequired,
      bootstrapError: deps.bootstrapError,
      currentRunId: deps.currentRunId,
      ensureConversationLoaded: deps.ensureConversationLoaded,
      isAuthRequiredError: deps.isAuthRequiredError,
      latestTurnTiming: deps.latestTurnTiming,
      loadStatus: deps.loadStatus,
      maybeAutoFlushPending: deps.maybeAutoFlushPending,
      maybeRefreshConversationBackendSessionFallback: deps.maybeRefreshConversationBackendSessionFallback,
      documentHidden: deps.documentHidden,
      hiddenPollInterval: deps.hiddenPollInterval,
      idlePollInterval: deps.idlePollInterval,
      pollInterval: deps.pollInterval,
      renderError: deps.renderError,
      renderFirstRunState: deps.renderFirstRunState,
      renderLoginState: deps.renderLoginState,
      renderPanelForRealtime: deps.renderPanelForRealtime,
      renderPendingMessages: deps.renderPendingMessages,
      renderSelectedHeader: deps.renderSelectedHeader,
      renderTaskList: deps.renderTaskList,
      realtimeConnected: deps.realtimeConnected,
      selectedTask: deps.selectedTask,
      selectedTaskRealtimeActive: deps.selectedTaskRealtimeActive,
      syncRealtimeEvents: deps.syncRealtimeEvents,
      taskActivityStatus: deps.taskActivityStatus
    });
    deps.setRenderScheduler?.(renderScheduler);
    let pollTimer = 0;
    let pollLoopStarted = false;

    function queueNextTick(delayMs) {
      if (pollTimer) clearTimeout(pollTimer);
      const fallback = renderScheduler.tickIntervalMs?.() || deps.pollInterval;
      const waitMs = Math.max(250, Number(delayMs ?? fallback) || deps.pollInterval);
      pollTimer = setTimeout(scheduleNextTick, waitMs);
    }

    async function scheduleNextTick() {
      pollTimer = 0;
      await renderScheduler.tick();
      queueNextTick(renderScheduler.tickIntervalMs?.() || deps.pollInterval);
    }

    function startPollLoop() {
      if (pollLoopStarted) return;
      pollLoopStarted = true;
      queueNextTick(renderScheduler.tickIntervalMs?.() || deps.pollInterval);
    }

    deps.loadBootstrap?.().then(async () => {
      void deps.loadAccessControlStatus?.();
      if (deps.currentRunId?.()) {
        await renderScheduler.tick();
        deps.openInitialTaskMemoHome?.();
      } else {
        deps.renderFirstRunState?.(true);
      }
    }).catch(err => {
      if (deps.isAuthRequiredError?.(err)) {
        deps.renderLoginState?.(window.AHAI18n?.t?.("auth.token_invalid", "Token is incorrect. Try again."), true);
        return;
      }
      deps.setBootstrapError?.(err?.message || String(err));
      deps.renderBootstrapError?.(deps.bootstrapError?.());
    }).finally(() => {
      startPollLoop();
    });

    document.addEventListener("visibilitychange", () => {
      if (!deps.documentHidden?.()) queueNextTick(250);
    });
    setInterval(() => {
      if (deps.documentHidden?.()) return;
      const task = deps.selectedTask?.();
      const turn = task ? deps.latestTurnTiming?.(task.id) : null;
      if ((task && deps.taskActivityStatus?.(task) !== "idle") || turn?.running) {
        deps.renderActiveTurn?.();
      }
    }, 1000);
    setInterval(() => { deps.pollHardwareStream?.(); }, 1000);

    return renderScheduler;
  }

  window.AHAControllerRegistry = Object.freeze({
    bindTopLevelEvents,
    collectDomRefs,
    startApp
  });
})();
