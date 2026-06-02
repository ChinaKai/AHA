(() => {
  function createTaskOptionsController(elements = {}, deps = {}) {
    const escapeHtml = deps.escapeHtml || (value => String(value ?? ""));
    const collaborationModeOptions = deps.collaborationModeOptions || ["auto"];
    const collaborationModeDescription = deps.collaborationModeDescription || (mode => String(mode || ""));
    const workflowTemplateDescriptionFallback = deps.workflowTemplateDescription || (template => String(template || ""));
    const collaborationModeMaxSubAgentsFor = deps.collaborationModeMaxSubAgents || (() => 3);
    let workflowTemplateOptions = deps.workflowTemplateOptions || ["auto"];
    let workflowTemplateLabels = new Map(workflowTemplateOptions.map(template => [template, template]));
    let workflowTemplateDescriptionsById = new Map(Object.entries(deps.workflowTemplateDescriptions || {}));

    function workflowTemplateDescription(template) {
      const key = String(template || "auto");
      return workflowTemplateDescriptionsById.get(key) || workflowTemplateDescriptionFallback(key);
    }

    function renderWorkflowTemplateOptions(selected = "auto") {
      if (!elements.workflowTemplateEl) return;
      const selectedTemplate = workflowTemplateOptions.includes(selected) ? selected : "auto";
      elements.workflowTemplateEl.innerHTML = workflowTemplateOptions.map(template => {
        const label = workflowTemplateLabels.get(template) || template;
        return `<option value="${escapeHtml(template)}" ${template === selectedTemplate ? "selected" : ""}>${escapeHtml(label)}</option>`;
      }).join("");
    }

    function applyWorkflowTemplateData(templates) {
      if (!Array.isArray(templates) || !templates.length) return;
      const normalized = templates
        .map(item => ({
          id: String(item?.id || "").trim(),
          label: String(item?.label || item?.id || "").trim(),
          description: String(item?.description || item?.guidance || "").trim(),
          order: Number(item?.order ?? 0)
        }))
        .filter(item => item.id);
      if (!normalized.length) return;
      normalized.sort((left, right) => left.order - right.order || left.id.localeCompare(right.id));
      workflowTemplateOptions = normalized.map(item => item.id);
      workflowTemplateLabels = new Map(normalized.map(item => [item.id, item.label || item.id]));
      workflowTemplateDescriptionsById = new Map(normalized.map(item => [item.id, item.description || item.label || item.id]));
      renderWorkflowTemplateOptions(elements.workflowTemplateEl?.value || "auto");
      syncWorkflowTemplateHelp();
    }

    function renderCollaborationModeOptions(selected = "auto") {
      return collaborationModeOptions.map(mode => (
        `<option value="${escapeHtml(mode)}" ${mode === selected ? "selected" : ""}>${escapeHtml(mode)}</option>`
      )).join("");
    }

    function syncCollaborationHelp(selectEl = elements.collaborationModeEl, helpEl = elements.collaborationModeHelpEl) {
      if (!helpEl) return;
      const mode = selectEl?.value || "auto";
      helpEl.textContent = collaborationModeDescription(mode);
    }

    function syncWorkflowTemplateHelp() {
      if (!elements.workflowTemplateHelpEl) return;
      elements.workflowTemplateHelpEl.textContent = workflowTemplateDescription(elements.workflowTemplateEl?.value || "auto");
    }

    function collaborationModeMaxSubAgents(mode) {
      return collaborationModeMaxSubAgentsFor(mode, Number(elements.maxSubAgentsEl?.value || "3"));
    }

    function syncCollaborationFields() {
      syncCollaborationHelp();
      syncWorkflowTemplateHelp();
      if (elements.maxSubAgentsFieldEl) {
        elements.maxSubAgentsFieldEl.hidden = false;
        elements.maxSubAgentsFieldEl.classList.toggle("hidden", false);
      }
      if (elements.maxSubAgentsEl) elements.maxSubAgentsEl.disabled = false;
    }

    return Object.freeze({
      applyWorkflowTemplateData,
      collaborationModeMaxSubAgents,
      renderCollaborationModeOptions,
      renderWorkflowTemplateOptions,
      syncCollaborationFields,
      syncCollaborationHelp,
      syncWorkflowTemplateHelp,
      workflowTemplateDescription
    });
  }

  function createTaskCreateController(elements = {}, deps = {}) {
    const alertUser = deps.alert || (message => window.alert(message));
    const windowRef = deps.windowRef || window;
    const realtimeDebug = deps.realtimeDebug || (() => {});
    let createInFlight = false;

    function currentRunId() {
      return String(deps.currentRunId?.() || "").trim();
    }

    function selectedWorkspacePath() {
      if (!elements.workspaceSelectEl) return "";
      return elements.workspaceSelectEl.value === "__custom__" ? (elements.workspaceCustomEl?.value || "").trim() : elements.workspaceSelectEl.value;
    }

    function selectedWorkspaceId() {
      if (!elements.workspaceSelectEl || elements.workspaceSelectEl.value === "__custom__") return "";
      const option = elements.workspaceSelectEl.options[elements.workspaceSelectEl.selectedIndex];
      return option?.dataset.workspaceId || "";
    }

    function selectedWorkspaceLabel() {
      if (!elements.workspaceSelectEl) return "";
      if (elements.workspaceSelectEl.value === "__custom__") return selectedWorkspacePath();
      const option = elements.workspaceSelectEl.options[elements.workspaceSelectEl.selectedIndex];
      return option?.textContent || selectedWorkspacePath();
    }

    function taskBackendConfirmLabel(payload) {
      return `${payload.backend || "default"} / ${deps.modelLabelForBackend?.(payload.backend, payload.model)}`;
    }

    function createTaskConfirmContext(payload) {
      return {
        runId: currentRunId() || "-",
        workspaceLabel: selectedWorkspaceLabel() || payload.workspace_path || payload.workspace_id || "-",
        backendLabel: taskBackendConfirmLabel(payload),
        supervisionSummary: deps.taskSupervisionSummary?.({ supervision: payload.supervision || {} })
      };
    }

    function confirmAddTask(payload) {
      const confirmContext = createTaskConfirmContext(payload);
      const fallbackText = deps.createTaskFallbackConfirmText?.(payload, confirmContext) || "Create task?";
      if (!elements.taskCreateConfirmDialogEl || typeof elements.taskCreateConfirmDialogEl.showModal !== "function") {
        return Promise.resolve(windowRef.confirm(fallbackText));
      }
      if (elements.taskCreateConfirmDetailsEl) {
        elements.taskCreateConfirmDetailsEl.innerHTML = (deps.createTaskConfirmRows?.(payload, confirmContext) || []).map(([label, value]) => `
          <div>
            <dt>${deps.escapeHtml?.(label)}</dt>
            <dd>${deps.escapeHtml?.(value || "-")}</dd>
          </div>
        `).join("") || "";
      }
      if (elements.taskCreateConfirmDialogEl.open) elements.taskCreateConfirmDialogEl.close("cancel");
      return new Promise(resolve => {
        const onClose = () => resolve(elements.taskCreateConfirmDialogEl.returnValue === "confirm");
        elements.taskCreateConfirmDialogEl.returnValue = "cancel";
        elements.taskCreateConfirmDialogEl.addEventListener("close", onClose, { once: true });
        try {
          elements.taskCreateConfirmDialogEl.showModal();
        } catch (_err) {
          elements.taskCreateConfirmDialogEl.removeEventListener("close", onClose);
          resolve(windowRef.confirm(fallbackText));
        }
      });
    }

    async function handleSubmit(event) {
      event.preventDefault();
      if (createInFlight) {
        realtimeDebug("task_create.skip", { reason: "in_flight" });
        return;
      }
      if (!currentRunId()) {
        realtimeDebug("task_create.skip", { reason: "missing_run" });
        alertUser("请先创建 Run，再添加任务。");
        return;
      }
      const title = elements.newTaskTitleEl?.value.trim() || "";
      if (!title) {
        realtimeDebug("task_create.skip", { reason: "missing_title" });
        return;
      }
      createInFlight = true;
      const description = elements.newTaskDescriptionEl?.value.trim() || "";
      const collaborationMode = elements.collaborationModeEl?.value || "auto";
      const workflowTemplate = elements.workflowTemplateEl?.value || "auto";
      const maxSubAgents = deps.collaborationModeMaxSubAgents?.(collaborationMode);
      const supervision = deps.taskSupervisionPayloadFromMode?.(
        elements.taskSupervisionModeEl?.value || "manual",
        elements.taskSupervisionMaxRoundsEl?.value || deps.defaultTaskSupervisionMaxRounds,
        deps.readAskUserGateControls?.(elements.taskSupervisionAskUserGatesEl)
      );
      deps.setCreateProxyDefaultsFromInputs?.();
      const createProxyEnabled = Boolean(elements.taskProxyEnabledEl?.checked);
      const payload = deps.createTaskPayload?.({
        title,
        description,
        backend: elements.taskBackendEl.value,
        model: elements.taskModelEl.value || null,
        sandbox: elements.taskSandboxEl.value,
        approval: elements.taskApprovalEl.value,
        proxyEnabled: createProxyEnabled,
        workspaceId: selectedWorkspaceId(),
        workspacePath: selectedWorkspacePath(),
        collaborationMode,
        workflowTemplate,
        delegationPolicy: deps.collaborationModeDelegationPolicy?.(collaborationMode),
        maxSubAgents,
        preferredSubBackend: elements.taskBackendEl.value,
        supervision,
        dispatch: true
      });
      const reopenCreateDialog = Boolean(elements.taskCreateDialogEl?.open);
      if (reopenCreateDialog) deps.closeTaskCreateDialog?.();
      try {
        realtimeDebug("task_create.confirm", {
          run_id: currentRunId(),
          title_len: title.length,
          description_len: description.length,
          backend: payload?.backend || "",
          workspace_id: payload?.workspace_id || "",
          workspace_path: payload?.workspace_path || "",
          collaboration_mode: payload?.collaboration_mode || "",
          workflow_template: payload?.workflow_template || ""
        });
        const confirmed = await confirmAddTask(payload);
        realtimeDebug("task_create.confirm_result", { confirmed: Boolean(confirmed) });
        if (!confirmed) {
          if (reopenCreateDialog) deps.openTaskCreateDialog?.();
          return;
        }
        realtimeDebug("task_create.request", { run_id: currentRunId(), backend: payload?.backend || "", dispatch: Boolean(payload?.dispatch) });
        const response = await deps.fetchJson(deps.apiUrl("/api/tasks"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(deps.runScopedPayload?.(payload))
        }, "Failed to create task");
        const createdTaskId = String(response?.task?.id || "").trim();
        realtimeDebug("task_create.response", { ok: Boolean(response?.ok), task_id: createdTaskId });
        if (createdTaskId) {
          const previousTaskId = deps.selectedTaskId?.();
          deps.setSelectedTaskId?.(createdTaskId);
          deps.writeStoredSelectedTaskId?.(createdTaskId);
          if (previousTaskId !== createdTaskId) {
            deps.resetEventWebSocketReconnectState?.("task_created");
          }
        }
        if (elements.newTaskTitleEl) elements.newTaskTitleEl.value = "";
        if (elements.newTaskDescriptionEl) elements.newTaskDescriptionEl.value = "";
        await deps.loadStatus?.({ forceAgents: Boolean(createdTaskId) });
        if (createdTaskId) await deps.selectTask?.(createdTaskId);
        deps.closeMobileSheets?.();
        deps.closeTaskCreateDialog?.();
      } catch (err) {
        realtimeDebug("task_create.error", { error: err?.message || String(err) });
        if (reopenCreateDialog) deps.openTaskCreateDialog?.();
        alertUser(err.message || String(err));
      } finally {
        createInFlight = false;
      }
    }

    function bind() {
      elements.taskFormEl?.addEventListener("submit", event => {
        void handleSubmit(event);
      });
    }

    return Object.freeze({
      bind,
      confirmAddTask,
      selectedWorkspaceId,
      selectedWorkspaceLabel,
      selectedWorkspacePath,
      taskBackendConfirmLabel
    });
  }

  window.AHATaskCreateController = Object.freeze({ createTaskCreateController, createTaskOptionsController });
})();
