(() => {
  const ahaSlashCommands = Object.freeze([
    { scope: "aha", name: "/aha kb", insert: "/aha kb ", desc: "Ask the current agent to generate knowledge-base candidates." },
    { scope: "aha", name: "/aha nav", insert: "/aha nav ", desc: "Ask the current agent to generate project navigation candidates." },
    { scope: "aha", name: "/aha complete", insert: "/aha complete", desc: "Mark the task completed." },
    { scope: "aha", name: "/aha reopen", insert: "/aha reopen", desc: "Reopen a completed task for follow-up." },
    { scope: "aha", name: "/aha interrupt", insert: "/aha interrupt", desc: "Interrupt the selected agent's current turn." }
  ]);

  function createInitialControllers(elements = {}, deps = {}) {
    const {
      accessControlStatusEl, agentRuntimeConfirmDialogEl, agentRuntimeConfirmMessageEl, agentTargetEl,
      agentsEl, agentsRailToggleEl, cancelTaskCreateEl, closeAgentsSheetEl, closeTaskCreateEl,
      closeTasksSheetEl, collapseAgentsEl, collapseOverviewEl, collaborationModeEl,
      collaborationModeHelpEl, commandMenuEl, conversationFiltersEl, backendStatusEl,
      headerWorkspaceDirEl, loginFormEl, loginStateEl, loginTokenEl, loginViewEl,
      maxSubAgentsEl, maxSubAgentsFieldEl, messageEl, mobileActionPanelEl,
      mobileActionsToggleEl, mobileSheetBackdropEl, mobileTaskStatusEl, mobileTaskSummaryEl,
      mobileTaskTitleEl, newTaskTitleEl, openAgentsSheetEl, openTaskCreateEl, openTasksSheetEl,
      overviewRailToggleEl, panelEl, pendingMessagesEl, selectedAgentInfoEl, selectedIdEl,
      runCreateDialogEl, runHttpProxyEl, runHttpsProxyEl, runNoProxyEl, runProxyEditorEl, runProxyEnabledEl,
      runManagerEl, runManagerToggleEl, runProxyFormEl, runProxyStateEl,
      selectedStatusEl, selectedTaskContextAutoCompactEnabledEl, selectedTaskContextThresholdEl,
      selectedTaskContextThresholdFieldEl, selectedTaskMetaEl, selectedTaskProxyEnabledEl,
      taskSkillsEditorEl, taskSkillsFormEl, selectedTaskSkillSelectEl, taskSkillsStateEl,
      selectedTaskSupervisionAskUserFieldEl, selectedTaskSupervisionAskUserGatesEl,
      selectedTaskSupervisionMaxRoundsEl, selectedTaskSupervisionMaxRoundsFieldEl,
      selectedTaskSupervisionHostModelEl, selectedTaskSupervisionHostModelFieldEl,
      selectedTaskSupervisionHostProxyEnabledEl, selectedTaskSupervisionHostProxyFieldEl,
      selectedTaskSupervisionModeEl, selectedTitleEl, sendFormEl, taskBackendEl,
      taskContextEditorEl, taskContextFormEl, taskContextStateEl, taskModelEl,
      taskContextAutoCompactEnabledEl, taskContextThresholdEl, taskContextThresholdFieldEl,
      taskSkillSelectEl,
      taskHardwareEditorEl, taskHardwareFormEl, taskHardwareStateEl,
      taskProxyEditorEl, taskProxyEnabledEl,
      taskProxyFormEl, taskProxyStateEl, taskSupervisionAskUserFieldEl,
      taskSupervisionAskUserGatesEl, taskSupervisionEditorEl, taskSupervisionFormEl,
      taskSupervisionHostModelEl, taskSupervisionHostModelFieldEl,
      taskSupervisionHostProxyEnabledEl, taskSupervisionHostProxyFieldEl,
      taskSupervisionMaxRoundsFieldEl, taskSupervisionModeEl, taskSupervisionStateEl,
      workflowTemplateEl, workflowTemplateHelpEl, workspaceCustomEl, workspaceSelectEl,
      webServiceAddressEl, authLogoutEl
    } = elements;

    const panelUiHelpers = window.AHAAppHelpers.createPanelUiHelpers({
      panelEl
    }, {
      copyTextByKey: deps.copyTextByKey,
      documentRef: deps.documentRef,
      navigatorRef: deps.navigatorRef,
      windowRef: deps.windowRef
    });
    const { copyTimelineMessage, panelHasTextSelection } = panelUiHelpers;

    const taskOptionsController = window.AHATaskCreateController.createTaskOptionsController({
      collaborationModeEl,
      collaborationModeHelpEl,
      maxSubAgentsEl,
      maxSubAgentsFieldEl,
      workflowTemplateEl,
      workflowTemplateHelpEl
    }, {
      collaborationModeDescription: deps.collaborationModeDescription,
      collaborationModeMaxSubAgents: deps.taskMetadataCollaborationModeMaxSubAgents,
      collaborationModeOptions: deps.collaborationModeOptions,
      escapeHtml: deps.escapeHtml,
      workflowTemplateDescription: deps.staticWorkflowTemplateDescription,
      workflowTemplateOptions: deps.staticWorkflowTemplateOptions
    });

    const promptMetricsPopover = window.AHAPromptMetricsPopover.createPromptMetricsPopoverController({
      panelEl,
      sendFormEl
    }, {
      windowRef: deps.windowRef
    });

    const runtimeOptions = window.AHARuntimeConfig.createRuntimeOptionsController({
      taskBackendEl,
      taskModelEl,
      workspaceCustomEl,
      workspaceSelectEl
    }, {
      bootstrapCodexEnvGroups: deps.bootstrapCodexEnvGroups,
      bootstrapConfigData: deps.bootstrapConfigData,
      bootstrapData: deps.bootstrapData,
      bootstrapEnvGroupName: deps.bootstrapEnvGroupName,
      bootstrapEnvGroups: deps.bootstrapEnvGroups,
      claudeEnvModelPrefix: deps.claudeEnvModelPrefix,
      configString: deps.configString,
      escapeHtml: deps.escapeHtml,
      fetchJson: deps.fetchJson,
      selectOptions: deps.selectOptions,
      setDefaultWorkspacePath: deps.setDefaultWorkspacePath
    });

    const taskConfigController = window.AHATaskConfigController.createTaskConfigController({
      escapeHtml: deps.escapeHtml,
      selectedTask: deps.selectedTask,
      els: {
        runProxyEditorEl,
        runProxyFormEl,
        runProxyEnabledEl,
        runHttpProxyEl,
        runHttpsProxyEl,
        runNoProxyEl,
        runProxyStateEl,
        taskBackendEl,
        taskProxyEnabledEl,
        taskProxyEditorEl,
        taskProxyFormEl,
        selectedTaskProxyEnabledEl,
        taskProxyStateEl,
        taskSupervisionEditorEl,
        taskSupervisionFormEl,
        selectedTaskSupervisionModeEl,
        selectedTaskSupervisionHostModelFieldEl,
        selectedTaskSupervisionHostModelEl,
        selectedTaskSupervisionHostProxyFieldEl,
        selectedTaskSupervisionHostProxyEnabledEl,
        selectedTaskSupervisionMaxRoundsFieldEl,
        selectedTaskSupervisionMaxRoundsEl,
        selectedTaskSupervisionAskUserFieldEl,
        selectedTaskSupervisionAskUserGatesEl,
        taskSupervisionStateEl,
        taskContextEditorEl,
        taskContextFormEl,
        selectedTaskContextAutoCompactEnabledEl,
        selectedTaskContextThresholdFieldEl,
        selectedTaskContextThresholdEl,
        taskContextStateEl,
        taskSkillsEditorEl,
        taskSkillsFormEl,
        selectedTaskSkillSelectEl,
        taskSkillsStateEl,
        taskHardwareEditorEl,
        taskHardwareFormEl,
        taskHardwareStateEl,
        taskSupervisionModeEl,
        taskSupervisionHostModelFieldEl,
        taskSupervisionHostModelEl,
        taskSupervisionHostProxyFieldEl,
        taskSupervisionHostProxyEnabledEl,
        taskSupervisionMaxRoundsFieldEl,
        taskSupervisionAskUserFieldEl
      },
      currentRunId: deps.currentRunId,
      statusData: deps.statusData,
      configData: deps.bootstrapConfigData,
      skillOptions: () => deps.bootstrapData?.()?.skill_options || [],
      defaults: {
        defaultHttpProxy: deps.defaultHttpProxy,
        defaultHttpsProxy: deps.defaultHttpsProxy,
        defaultNoProxy: deps.defaultNoProxy,
        defaultTaskSupervisionMaxRounds: deps.defaultTaskSupervisionMaxRounds,
        defaultTaskContextThresholdPercent: deps.defaultTaskContextThresholdPercent
      },
      helpers: {
        defaultAskUserGates: deps.defaultAskUserGates,
        normalizeAskUserGates: deps.normalizeAskUserGates,
        normalizeTaskContextThreshold: deps.normalizeTaskContextThreshold,
        supervisionAskUserGateDefs: deps.supervisionAskUserGateDefs,
        taskContextManagementPolicy: deps.taskContextManagementPolicy,
        taskContextSummary: deps.taskContextSummary,
        taskSkillsPolicy: deps.taskSkillsPolicy,
        taskSkillsSummary: deps.taskSkillsSummary,
        defaultHardwareDebugPermissions: deps.taskMetadata?.defaultHardwareDebugPermissions,
        normalizeHardwareDebugPermissions: deps.normalizeHardwareDebugPermissions,
        taskHardwareDebugPolicy: deps.taskHardwareDebugPolicy,
        taskHardwareDebugSummary: deps.taskHardwareDebugSummary,
        taskProxySummary: deps.taskProxySummary,
        defaultModelForBackend: backend => runtimeOptions.defaultModelForBackend(backend),
        fillModelSelect: (select, backend, selected = "") => runtimeOptions.fillModelSelect(select, backend, selected),
        taskSupervisionModeValue: deps.taskSupervisionModeValue,
        taskSupervisionPayloadFromMode: deps.taskSupervisionPayloadFromMode,
        taskSupervisionPolicy: deps.taskSupervisionPolicy,
        taskSupervisionSummary: deps.taskSupervisionSummary
      },
      api: {
        apiUrl: deps.apiUrl,
        fetchJson: deps.fetchJson,
        loadStatus: deps.loadStatus,
        runScopedPayload: deps.runScopedPayload
      }
    });

    const uiShell = window.AHAUiShell.createUiShell({
      body: deps.documentRef?.body,
      agentsRailToggleEl,
      cancelTaskCreateEl,
      closeTaskCreateEl,
      collapseAgentsEl,
      collapseOverviewEl,
      mobileSheetBackdropEl,
      openTasksSheetEl,
      openAgentsSheetEl,
      closeTasksSheetEl,
      closeAgentsSheetEl,
      newTaskTitleEl,
      openTaskCreateEl,
      runManagerEl,
      runManagerToggleEl,
      overviewRailToggleEl,
      mobileTaskSummaryEl,
      mobileActionPanelEl,
      mobileActionsToggleEl,
      commandMenuEl,
      runCreateDialogEl,
      taskCreateDialogEl: elements.taskCreateDialogEl
    }, {
      windowRef: deps.windowRef,
      documentRef: deps.documentRef,
      navigatorRef: deps.navigatorRef,
      activeTab: deps.activeTab,
      activateTab: deps.activateTab,
      alertError: deps.alertError,
      currentRunId: deps.currentRunId,
      selectedTask: deps.selectedTask,
      hasMessage: () => deps.messageComposer?.()?.hasMessage(),
      pointerSubmitActive: () => deps.messageComposer?.()?.pointerSubmitActive(),
      requestComposerSubmit: () => deps.messageComposer?.()?.requestSubmit(),
      requestComposerSubmitFromPointer: event => deps.messageComposer?.()?.requestSubmitFromPointer(event),
      syncCreateProxyDefaultForBackend: options => taskConfigController.syncCreateProxyDefaultForBackend(options),
      syncMobileComposerAction: () => deps.messageComposer?.()?.syncMobileAction()
    });

    const authController = window.AHAAuthController.createAuthController({
      body: deps.documentRef?.body,
      loginViewEl,
      loginFormEl,
      loginTokenEl,
      loginStateEl
    }, {
      fetchJson: deps.fetchJson,
      isAuthRequiredError: deps.isAuthRequiredError,
      setBootstrapError: deps.setBootstrapError,
      closeRealtime: deps.closeEventWebSocket,
      afterLogin: deps.afterLogin,
      afterLogout: deps.afterLogout
    });

    const accessControlController = deps.createAccessControlController?.({
      accessControlStatusEl,
      authLogoutEl,
      webServiceAddressEl
    }, {
      accessControlData: deps.accessControlData,
      accessControlError: deps.accessControlError,
      fetchJson: deps.fetchJson,
      isAuthRequiredError: deps.isAuthRequiredError,
      loginInFlight: () => authController.loginInFlight(),
      renderLoginState: authController.renderLoginState,
      setAccessControlData: deps.setAccessControlData,
      setAccessControlError: deps.setAccessControlError
    });

    const backendStatusController = window.AHABackendStatus.createBackendStatusController({
      backendStatusEl
    }, {
      agentBackendProcessStatus: deps.agentBackendProcessStatus,
      agentLifecycleStatus: deps.agentLifecycleStatus,
      agentWaitingReason: deps.agentWaitingReason,
      backendStatusData: deps.backendStatusData,
      currentRunId: deps.currentRunId,
      escapeHtml: deps.escapeHtml,
      formatLocalTimestamp: deps.formatLocalTimestamp,
      isSupervisionAgent: deps.isSupervisionAgent,
      selectedAgent: deps.selectedAgent,
      selectedTask: deps.selectedTask
    });

    const appSelectors = window.AHAAppSelectors.createAppSelectors({
      agentTargetValue: () => agentTargetEl.value || "main",
      selectedTaskId: deps.selectedTaskId,
      statusData: deps.statusData
    }, {
      agentLifecycleStatus: deps.agentLifecycleStatus,
      agentWaitingReason: deps.agentWaitingReason,
      conversationSourceEvents: deps.conversationSourceEvents,
      eventMatchesSelectedAgent: deps.eventMatchesSelectedAgent,
      formatDuration: deps.formatDuration,
      latestTurnTimingForContext: deps.latestTurnTimingForContext,
      parseTimestamp: deps.parseTimestamp,
      selectedAgentFromTask: deps.selectedAgentFromTask,
      selectedAgentInputBlocked: backendStatusController.selectedAgentInputBlocked,
      selectedTaskFromStatus: deps.selectedTaskFromStatus,
      selectedTaskRealtimeActiveFromState: deps.selectedTaskRealtimeActiveFromState,
      taskActivityStatus: deps.taskActivityStatus,
      taskAgentCount: deps.taskAgentCount,
      taskEvents: deps.taskEvents,
      taskMetaTimingForContext: deps.taskMetaTimingForContext,
      taskTimingLabelForContext: deps.taskTimingLabelForContext,
      terminalAgentStatuses: deps.terminalAgentStatuses
    });

    const eventBindings = window.AHAEventBindings;
    const panelController = window.AHAPanelController.createPanelController({
      panelEl,
      documentRef: deps.documentRef
    }, {
      activeTab: deps.activeTab,
      setActiveTab: deps.setActiveTab,
      currentRunId: deps.currentRunId,
      selectedTask: deps.selectedTask,
      selectedTaskId: deps.selectedTaskId,
      runHasNoTasks: deps.runHasNoTasks,
      renderFirstRunState: deps.renderFirstRunState,
      renderConversationFilters: deps.renderConversationFilters,
      renderConversation: deps.renderConversation,
      renderFinalPanelHtml: deps.renderFinalPanelHtml,
      renderLogsPanelHtml: deps.renderLogsPanelHtml,
      renderHardwareIoPanelHtml: deps.renderHardwareIoPanelHtml,
      renderContextPanelHtml: deps.renderContextPanelHtml,
      logState: deps.logState,
      hardwareIoState: deps.hardwareIoState,
      finalDetail: deps.finalDetail,
      contextDetail: deps.contextDetail,
      promptMetricsState: deps.promptMetricsState,
      renderRawPromptSection: deps.renderRawPromptSection,
      renderPromptMetricsPanel: deps.renderPromptMetricsPanel,
      capturePromptMetricsPopoverState: promptMetricsPopover.captureState,
      restorePromptMetricsPopoverState: promptMetricsPopover.restoreState,
      positionPromptMetricsPopover: promptMetricsPopover.position,
      captureContextScrollState: deps.captureContextScrollState,
      restoreContextScrollState: deps.restoreContextScrollState,
      syncExpandedMessageKeysFromDom: deps.syncExpandedMessageKeysFromDom,
      syncMobileActionPanel: () => uiShell.syncMobileActionPanel(deps.activeTab?.()),
      ensureActiveTabData: deps.ensureActiveTabData,
      conversationAutoFollow: deps.conversationAutoFollow,
      setConversationAutoFollow: deps.setConversationAutoFollow
    });

    return Object.freeze({
      accessControlController,
      activateTab: panelController.activateTab,
      agentInputWaitBlocked: backendStatusController.agentInputWaitBlocked,
      appSelectors,
      authController,
      backendStatusController,
      clearLoginState: authController.clearLoginState,
      copyTimelineMessage,
      eventBindings,
      isPanelNearBottom: panelController.isPanelNearBottom,
      panelController,
      panelHasTextSelection,
      promptMetricsPopover,
      renderBackendStatus: backendStatusController.renderBackendStatus,
      renderLoginState: authController.renderLoginState,
      renderPanel: panelController.renderPanel,
      runtimeOptions,
      scrubAuthTokenFromUrl: authController.scrubAuthTokenFromUrl,
      selectedAgentInputBlocked: backendStatusController.selectedAgentInputBlocked,
      selectedBackendActive: backendStatusController.selectedBackendActive,
      submitLoginForm: authController.submitLoginForm,
      taskConfigController,
      taskHostInputBlocked: backendStatusController.taskHostInputBlocked,
      taskOptionsController,
      uiShell,
      logoutAuthSession: authController.logoutAuthSession,
      ...taskConfigController
    });
  }

  window.AHAAppControllerFactory = Object.freeze({
    createInitialControllers,
    createFeatureControllers
  });

  function createFeatureControllers(elements = {}, deps = {}) {
    const {
      agentRuntimeConfirmDialogEl, agentRuntimeConfirmMessageEl, agentTargetEl, agentsEl,
      ahaSettingsEl, closeSettingsEl, collaborationModeEl, commandMenuEl, messageEl,
      messageImageFileEl, messageImageUploadEl,
      closeTaskMemosEl, mobileActionsToggleEl, newTaskDescriptionEl, newTaskTitleEl, openKnowledgeBaseEl, openTaskMemosEl, openTaskViewEl, playConsoleEl,
      playConsolePopoverEl, selectedAgentInfoEl, sendFormEl, sessionMenuEl, settingsContentEl,
      skillsConsoleEl, skillsConsolePopoverEl, tokenUsageEl, tokenUsagePopoverEl,
      settingsDialogEl, taskApprovalEl, taskBackendEl, taskCreateConfirmDetailsEl,
      taskCreateConfirmDialogEl, taskCreateDialogEl, taskDraftStateEl, taskFormEl, taskMemoLinkClearEl,
      taskMemoLinkSummaryEl, taskMemoPickerEl, taskMemoPickerFilterEl, taskMemoPickerListEl,
      taskMemoPickerSearchEl, taskMemoPickerToggleEl, taskModelEl,
      taskContextAutoCompactEnabledEl, taskContextThresholdEl, taskContextThresholdFieldEl,
      taskSkillSelectEl,
      taskMemoCalendarCollapseEl, taskMemoCalendarEl, taskMemoCancelEl, taskMemoConvertEl, taskMemoDeleteEl, taskMemoDialogEl,
      taskMemoCurrentMonthEl,
      taskMemoCompletedDateFieldEl, taskMemoClosedDateFieldEl,
      taskMemoAttachmentListEl, taskMemoDescriptionEditorEl, taskMemoEditCompletedDateEl, taskMemoEditClosedDateEl, taskMemoEditDateEl, taskMemoEditDescriptionEl, taskMemoEditEndDateEl, taskMemoEditModeEl, taskMemoEditStatusEl, taskMemoEditorColumnEl, taskMemoEditorHintEl, taskMemoEditorJumpEl, taskMemoEditorTitleEl,
      taskMemoEditTitleEl, taskMemoFilterEl, taskMemoFormEl, taskMemoImageFileEl, taskMemoImageUploadEl, taskMemoListEl, taskMemoMarkdownEditorEl,
      taskMemoNewEl, taskMemoNextMonthEl, taskMemoNextYearEl, taskMemoPrevMonthEl, taskMemoPrevYearEl, taskMemoSaveEl, taskMemoStateEl,
      taskMemoPreviewModeEl, taskMemoStatusOptionsEl,
      taskMemoTaskLinkClearEl, taskMemoTaskLinkFieldEl,
      taskMemoTaskPickerEl, taskMemoTaskPickerFilterEl, taskMemoTaskPickerListEl,
      taskMemoTaskPickerSearchEl, taskMemoTaskPickerToggleEl,
      taskProxyEnabledEl, taskSandboxEl,
      taskSupervisionAskUserGatesEl, taskSupervisionHostModelEl, taskSupervisionHostProxyEnabledEl,
      taskSupervisionMaxRoundsEl, taskSupervisionModeEl,
      weixinConsoleEl, weixinConsolePopoverEl, workflowTemplateEl, workspaceCustomEl,
      workspaceSelectEl
    } = elements;

    const agentConfigController = window.AHAAgentConfigController.createAgentConfigController({
      escapeHtml: deps.escapeHtml,
      selectOptions: deps.selectOptions,
      backendModelSelectOptions: (backend, current) => deps.runtimeOptions?.backendModelSelectOptions(backend, current),
      agentBackendOptions: () => deps.runtimeOptions?.agentBackendOptions() || "",
      sandboxOptions: deps.sandboxOptions,
      approvalOptions: deps.approvalOptions,
      proxySelectOptions: deps.proxySelectOptions,
      readAgentConfigEditor: deps.readAgentConfigEditor,
      normalizeAgentConfig: deps.normalizeAgentConfig,
      agentConfigValue: deps.agentConfigValue,
      agentConfigLabel: deps.agentConfigLabel,
      agentBackendModelChanged: deps.agentBackendModelChanged,
      agentRuntimeConfigChanged: deps.agentRuntimeConfigChanged,
      fillModelSelect: (select, backend, selected = "") => deps.runtimeOptions?.fillModelSelect(select, backend, selected),
      confirmDialogAction: deps.confirmDialogAction,
      agentBackendProcessStatus: deps.agentBackendProcessStatus,
      agentRuntimeConfirmDialogEl,
      agentRuntimeConfirmMessageEl,
      windowRef: deps.windowRef,
      selectedTask: deps.selectedTask,
      loadStatus: deps.loadStatus,
      ensureActiveTabData: deps.ensureActiveTabData,
      renderPanel: deps.renderPanel,
      renderAgents: deps.renderAgents,
      contextDetails: deps.contextDetails,
      fetchWithTimeout: deps.fetchWithTimeout,
      apiUrl: deps.apiUrl,
      runScopedPayload: deps.runScopedPayload
    });

    const settingsController = window.AHASettingsController.createSettingsController({
      ahaSettingsEl,
      closeSettingsEl,
      settingsContentEl,
      settingsDialogEl,
      sessionMenuEl
    }, {
      addBootstrapConfigRow: deps.addBootstrapConfigRow,
      bootstrapConfigFormHtml: deps.bootstrapConfigFormHtml,
      bootstrapData: deps.bootstrapData,
      closeMobileActionPanel: deps.closeMobileActionPanel,
      closeMobileSheets: deps.closeMobileSheets,
      dispatchAction: deps.dispatchAction,
      fillBootstrapProxyDefaultFor: deps.fillBootstrapProxyDefaultFor,
      loadBootstrap: deps.loadBootstrap,
      removeBootstrapConfigRow: deps.removeBootstrapConfigRow,
      setSessionMenu: deps.setSessionMenu,
      syncBootstrapProxyDefaultsForInput: deps.syncBootstrapProxyDefaultsForInput,
      syncBootstrapModelOptions: deps.syncBootstrapModelOptions
    });

    const playConsoleController = window.AHAPlayConsole.createPlayConsoleController({
      playConsoleEl,
      playConsolePopoverEl,
      sessionMenuEl
    }, {
      apiUrl: deps.apiUrl,
      currentRunId: deps.currentRunId,
      escapeHtml: deps.escapeHtml,
      fetchJson: deps.fetchJson,
      setRunMaintenanceConsoleOpen: deps.setRunMaintenanceConsoleOpen,
      setWeixinConsoleOpen: deps.setWeixinConsoleOpen
    });

    const tokenUsageController = window.AHATokenUsage.createTokenUsageController({
      runIdEl: elements.runIdEl,
      sessionMenuEl,
      tokenUsageEl,
      tokenUsagePopoverEl
    }, {
      apiUrl: deps.apiUrl,
      currentRunId: deps.currentRunId,
      fetchJson: deps.fetchJson,
      fetchWithTimeout: deps.fetchWithTimeout,
      readJsonResponse: deps.readJsonResponse,
      windowRef: deps.windowRef
    });

    const skillsConsoleController = window.AHASkillsConsole.createSkillsConsoleController({
      sessionMenuEl,
      skillsConsoleEl,
      skillsConsolePopoverEl
    }, {
      apiUrl: deps.apiUrl,
      confirmDialogAction: deps.confirmDialogAction,
      escapeHtml: deps.escapeHtml,
      fetchJson: deps.fetchJson,
      onSkillsChanged: deps.onSkillsChanged,
      setPlayConsoleOpen: deps.setPlayConsoleOpen,
      setRunMaintenanceConsoleOpen: deps.setRunMaintenanceConsoleOpen,
      setWeixinConsoleOpen: deps.setWeixinConsoleOpen
    });

    const weixinConsoleController = window.AHAWeixinConsole.createWeixinConsoleController({
      documentRef: deps.documentRef,
      sessionMenuEl,
      weixinConsoleEl,
      weixinConsolePopoverEl
    }, {
      apiUrl: deps.apiUrl,
      confirmDialogAction: deps.confirmDialogAction,
      currentRunId: deps.currentRunId,
      escapeHtml: deps.escapeHtml,
      fetchJson: deps.fetchJson,
      formatDuration: deps.formatDuration,
      formatLocalTimestamp: deps.formatLocalTimestamp,
      setPlayConsoleOpen: deps.setPlayConsoleOpen,
      setRunMaintenanceConsoleOpen: deps.setRunMaintenanceConsoleOpen
    });

    const taskCreateController = window.AHATaskCreateController.createTaskCreateController({
      collaborationModeEl,
      newTaskDescriptionEl,
      newTaskTitleEl,
      taskApprovalEl,
      taskBackendEl,
      taskCreateConfirmDetailsEl,
      taskCreateConfirmDialogEl,
      taskCreateDialogEl,
      taskContextAutoCompactEnabledEl,
      taskContextThresholdFieldEl,
      taskContextThresholdEl,
      taskSkillSelectEl,
      taskDraftStateEl,
      taskFormEl,
      taskMemoLinkClearEl,
      taskMemoLinkSummaryEl,
      taskMemoPickerEl,
      taskMemoPickerFilterEl,
      taskMemoPickerListEl,
      taskMemoPickerSearchEl,
      taskMemoPickerToggleEl,
      taskModelEl,
      taskProxyEnabledEl,
      taskSandboxEl,
      taskSupervisionAskUserGatesEl,
      taskSupervisionHostModelEl,
      taskSupervisionHostProxyEnabledEl,
      taskSupervisionMaxRoundsEl,
      taskSupervisionModeEl,
      workflowTemplateEl,
      workspaceCustomEl,
      workspaceSelectEl
    }, {
      alert: deps.alert,
      apiUrl: deps.apiUrl,
      closeMobileSheets: deps.closeMobileSheets,
      closeTaskCreateDialog: deps.closeTaskCreateDialog,
      collaborationModeDelegationPolicy: deps.collaborationModeDelegationPolicy,
      collaborationModeMaxSubAgents: deps.taskOptionsController?.collaborationModeMaxSubAgents,
      createTaskConfirmRows: deps.createTaskConfirmRows,
      createTaskFallbackConfirmText: deps.createTaskFallbackConfirmText,
      createTaskPayload: deps.createTaskPayload,
      currentRunId: deps.currentRunId,
      defaultTaskSupervisionMaxRounds: deps.defaultTaskSupervisionMaxRounds,
      defaultHardwareDebugPermissions: deps.taskMetadata?.defaultHardwareDebugPermissions,
      documentRef: deps.documentRef,
      escapeHtml: deps.escapeHtml,
      fetchJson: deps.fetchJson,
      fillModelSelect: (select, backend, selected = "") => deps.runtimeOptions?.fillModelSelect(select, backend, selected),
      loadStatus: deps.loadStatus,
      modelLabelForBackend: (backend, value) => deps.runtimeOptions?.modelLabelForBackend(backend, value),
      normalizeTaskContextThreshold: deps.normalizeTaskContextThreshold,
      openTaskCreateDialog: deps.openTaskCreateDialog,
      readAskUserGateControls: deps.readAskUserGateControls,
      realtimeDebug: deps.realtimeDebug,
      refreshRunScopedView: deps.refreshRunScopedView,
      resetEventWebSocketReconnectState: deps.resetEventWebSocketReconnectState,
      runScopedPayload: deps.runScopedPayload,
      selectTask: deps.selectTask,
      selectedTaskId: deps.selectedTaskId,
      setCreateProxyDefaultsFromInputs: deps.setCreateProxyDefaultsFromInputs,
      setSelectedTaskId: deps.setSelectedTaskId,
      taskSkillOptions: () => deps.bootstrapData?.()?.skill_options || [],
      syncCreateTaskSupervisionModeFields: deps.syncCreateTaskSupervisionModeFields,
      taskOptionsController: deps.taskOptionsController,
      taskSupervisionPayloadFromMode: deps.taskSupervisionPayloadFromMode,
      taskSupervisionSummary: deps.taskSupervisionSummary,
      windowRef: deps.windowRef,
      writeStoredSelectedTaskId: deps.writeStoredSelectedTaskId
    });

    const taskMemoController = window.AHATaskMemoController.createTaskMemoController({
      closeTaskMemosEl,
      openKnowledgeBaseEl,
      openTaskViewEl,
      openTaskMemosEl,
      taskMemoCalendarCollapseEl,
      taskMemoCalendarEl,
      taskMemoCancelEl,
      taskMemoConvertEl,
      taskMemoCurrentMonthEl,
      taskMemoDeleteEl,
      taskMemoDescriptionEditorEl,
      taskMemoDialogEl,
      taskMemoCompletedDateFieldEl,
      taskMemoClosedDateFieldEl,
      taskMemoAttachmentListEl,
      taskMemoEditCompletedDateEl,
      taskMemoEditClosedDateEl,
      taskMemoEditDateEl,
      taskMemoEditDescriptionEl,
      taskMemoEditEndDateEl,
      taskMemoEditModeEl,
      taskMemoEditStatusEl,
      taskMemoEditorColumnEl,
      taskMemoEditorHintEl,
      taskMemoEditorJumpEl,
      taskMemoEditorTitleEl,
      taskMemoEditTitleEl,
      taskMemoFilterEl,
      taskMemoFormEl,
      taskMemoImageFileEl,
      taskMemoImageUploadEl,
      taskMemoListEl,
      taskMemoMarkdownEditorEl,
      taskMemoNewEl,
      taskMemoNextMonthEl,
      taskMemoNextYearEl,
      taskMemoPrevMonthEl,
      taskMemoPrevYearEl,
      taskMemoPreviewModeEl,
      taskMemoSaveEl,
      taskMemoStateEl,
      taskMemoStatusOptionsEl,
      taskMemoTaskLinkClearEl,
      taskMemoTaskLinkFieldEl,
      taskMemoTaskPickerEl,
      taskMemoTaskPickerFilterEl,
      taskMemoTaskPickerListEl,
      taskMemoTaskPickerSearchEl,
      taskMemoTaskPickerToggleEl
    }, {
      alert: deps.alert,
      allTasks: deps.allTasks,
      apiUrl: deps.apiUrl,
      applyTaskMemoToForm: memo => taskCreateController.applyTaskMemoToForm?.(memo),
      bootstrapData: deps.bootstrapData,
      confirmDialogAction: deps.confirmDialogAction,
      currentRunId: deps.currentRunId,
      documentRef: deps.documentRef,
      escapeHtml: deps.escapeHtml,
      fetchJson: deps.fetchJson,
      fetchWithTimeout: deps.fetchWithTimeout,
      initialHomeActive: deps.initialTaskMemoHomeActive,
      isAuthRequiredError: deps.isAuthRequiredError,
      closeMobileSheets: deps.closeMobileSheets,
      consoleRef: deps.consoleRef,
      openTaskCreateDialog: deps.openTaskCreateDialog,
      renderLoginState: authController.renderLoginState,
      selectTask: deps.selectTask,
      setSelectedTaskId: deps.setSelectedTaskId,
      taskDisplayStatus: deps.taskDisplayStatus,
      windowRef: deps.windowRef,
      writeStoredSelectedTaskId: deps.writeStoredSelectedTaskId
    });

    const messageComposer = window.AHAMessageComposer.createMessageComposer({
      sendFormEl,
      messageEl,
      messageImageFileEl,
      messageImageUploadEl,
      mobileActionsToggleEl,
      commandMenuEl
    }, {
      apiUrl: deps.apiUrl,
      escapeHtml: deps.escapeHtml,
      fetchJson: deps.fetchJson,
      imageUploadsEnabled: () => deps.activeTab?.() !== "hardware",
      markdownForImage: async ({ dataUrl, file, index }) => {
        if (window.AHATaskMemoMarkdown?.uploadMemoImageMarkdown) {
          return await window.AHATaskMemoMarkdown.uploadMemoImageMarkdown({ dataUrl, file, index }, {
            apiUrl: deps.apiUrl,
            fetchJson: deps.fetchJson,
            imagePaste: window.AHATextareaImagePaste,
            windowRef: deps.windowRef
          });
        }
        return window.AHATextareaImagePaste?.imageMarkdown?.(dataUrl, file) || "";
      },
      selectedTask: deps.selectedTask,
      closeMobileActionPanel: deps.closeMobileActionPanel,
      syncMobileComposerToggle: deps.syncMobileComposerToggle,
      matchingCommands: value => {
        const text = String(value || "").trimStart();
        if (!text.startsWith("/")) return [];
        const query = text.toLowerCase();
        const agent = deps.selectedAgent?.();
        const agentCommands = deps.runtimeOptions?.backendCommandsFor(agent?.backend || "codex") || [];
        const slashCommands = [...ahaSlashCommands, ...agentCommands];
        return slashCommands.filter(item => item.name.toLowerCase().startsWith(query) || item.insert.toLowerCase().startsWith(query));
      },
      onSubmit: deps.handleComposerSubmit,
      handleRawKey: deps.handleHardwareRawKey,
      onInput: deps.onComposerInput,
      textareaImagePaste: window.AHATextareaImagePaste,
      windowRef: deps.windowRef,
      onError: err => {
        deps.realtimeDebug?.("composer.error", { error: err?.message || String(err) });
        deps.alert?.(err.message || String(err));
      }
    });

    return Object.freeze({
      agentConfigController,
      messageComposer,
      playConsoleController,
      settingsController,
      skillsConsoleController,
      taskCreateController,
      taskMemoController,
      tokenUsageController,
      weixinConsoleController
    });
  }
})();
