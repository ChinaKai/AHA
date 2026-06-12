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
    const documentRef = deps.documentRef || windowRef.document || (typeof document !== "undefined" ? document : null);
    const escapeHtml = deps.escapeHtml || (value => String(value ?? ""));
    const realtimeDebug = deps.realtimeDebug || (() => {});
    let createInFlight = false;
    let activeTaskMemoId = "";
    let taskMemoOptions = [];
    let taskMemoPickerOpen = false;
    let taskMemoPickerSearch = "";
    let taskMemoPickerFilter = "active_unlinked";
    const memoStatuses = ["todo", "doing", "done", "closed"];
    const memoStatusAliases = Object.freeze({
      open: "todo",
      incomplete: "todo",
      pending: "todo",
      paused: "todo",
      running: "doing",
      blocked: "todo",
      suspended: "todo",
      complete: "done",
      completed: "done",
      archived: "closed"
    });

    function t(key, fallback = "") {
      return window.AHAI18n?.t?.(key, fallback) || fallback;
    }

    function currentRunId() {
      return String(deps.currentRunId?.() || "").trim();
    }

    function setDraftStatus(message = "") {
      if (elements.taskDraftStateEl) elements.taskDraftStateEl.textContent = message;
    }

    function setText(element, message = "") {
      if (element) element.textContent = message;
    }

    function normalizeMemoStatus(status) {
      const key = String(status || "todo").trim().toLowerCase().replace(/-/g, "_");
      const normalized = memoStatusAliases[key] || key;
      return memoStatuses.includes(normalized) ? normalized : "todo";
    }

    function memoStatusLabel(status) {
      const normalized = normalizeMemoStatus(status);
      return t(`memo.status_${normalized}`, normalized);
    }

    function readAskUserGates() {
      return deps.readAskUserGateControls?.(elements.taskSupervisionAskUserGatesEl) || {};
    }

    function syncCreateTaskContextFields() {
      const enabled = elements.taskContextAutoCompactEnabledEl?.checked !== false;
      if (elements.taskContextThresholdFieldEl) {
        elements.taskContextThresholdFieldEl.hidden = !enabled;
        elements.taskContextThresholdFieldEl.classList.toggle("hidden", !enabled);
      }
      if (elements.taskContextThresholdEl) {
        elements.taskContextThresholdEl.disabled = !enabled;
      }
    }

    function readFormDraft() {
      return {
        values: {
          title: elements.newTaskTitleEl?.value || "",
          description: elements.newTaskDescriptionEl?.value || "",
          backend: elements.taskBackendEl?.value || "",
          model: elements.taskModelEl?.value || "",
          sandbox: elements.taskSandboxEl?.value || "",
          approval: elements.taskApprovalEl?.value || "",
          proxy_enabled: Boolean(elements.taskProxyEnabledEl?.checked),
          workspace_value: elements.workspaceSelectEl?.value || "",
          workspace_path: selectedWorkspacePath(),
          workspace_custom: elements.workspaceCustomEl?.value || "",
          collaboration_mode: elements.collaborationModeEl?.value || "auto",
          workflow_template: elements.workflowTemplateEl?.value || "auto",
          max_sub_agents: elements.maxSubAgentsEl?.value || "",
          supervision_mode: elements.taskSupervisionModeEl?.value || "manual",
          supervision_host_model: elements.taskSupervisionHostModelEl?.value || "",
          supervision_host_proxy_enabled: Boolean(elements.taskSupervisionHostProxyEnabledEl?.checked),
          supervision_max_rounds: elements.taskSupervisionMaxRoundsEl?.value || "",
          supervision_ask_user_gates: readAskUserGates(),
          context_auto_compact_enabled: elements.taskContextAutoCompactEnabledEl?.checked !== false,
          context_threshold_percent: elements.taskContextThresholdEl?.value || ""
        }
      };
    }

    function memoLinkLabel(memo = {}) {
      const title = String(memo.title || "").trim();
      const description = String(memo.description || "").trim().replace(/\s+/g, " ");
      const base = title || (description ? description.slice(0, 48) : t("task.untitled_draft", "Untitled draft"));
      const date = taskMemoDateRangeLabel(memo);
      return date ? `${base} · ${date}` : base;
    }

    function taskMemoDateRangeLabel(memo = {}) {
      const start = String(memo.scheduled_date || "").trim();
      const end = String(memo.end_date || "").trim();
      return start && end && end > start ? `${start} ~ ${end}` : start;
    }

    function memoLinkMeta(memo = {}) {
      const parts = [];
      const date = taskMemoDateRangeLabel(memo);
      if (date) parts.push(date);
      parts.push(memo.created_task_id ? t("memo.linked_task", "Linked") : t("memo.unlinked_task", "No task"));
      parts.push(memoStatusLabel(memo.status));
      return parts.filter(Boolean).join(" · ");
    }

    function upsertTaskMemoOption(memo = {}) {
      const memoId = String(memo.id || "").trim();
      if (!memoId) return;
      taskMemoOptions = [memo, ...taskMemoOptions.filter(item => item.id !== memoId)];
    }

    function selectedTaskMemo() {
      return taskMemoOptions.find(memo => memo.id === activeTaskMemoId) || null;
    }

    function memoPickerParams() {
      const params = { limit: 50 };
      if (activeTaskMemoId) params.include_id = activeTaskMemoId;
      if (taskMemoPickerSearch) params.q = taskMemoPickerSearch;
      if (taskMemoPickerFilter === "active_unlinked" || taskMemoPickerFilter === "open_unlinked") {
        params.status = "active";
        params.linked = "unlinked";
      } else if (taskMemoPickerFilter === "active" || taskMemoPickerFilter === "open") {
        params.status = "active";
      } else if (taskMemoPickerFilter === "linked") {
        params.linked = "linked";
      }
      return params;
    }

    function renderDraftBox() {
      const selectedMemo = selectedTaskMemo();
      setText(elements.taskMemoLinkSummaryEl, selectedMemo ? memoLinkLabel(selectedMemo) : t("task.memo_link_empty", "No memo selected"));
      if (elements.taskMemoLinkSummaryEl) {
        elements.taskMemoLinkSummaryEl.title = selectedMemo ? memoLinkMeta(selectedMemo) : "";
      }
      setText(elements.taskMemoPickerToggleEl, selectedMemo ? t("task.memo_change", "Change memo") : t("task.memo_choose", "Choose memo"));
      if (elements.taskMemoLinkClearEl) elements.taskMemoLinkClearEl.hidden = !selectedMemo;
      if (elements.taskMemoPickerEl) elements.taskMemoPickerEl.hidden = !taskMemoPickerOpen;
      if (elements.taskMemoPickerSearchEl && elements.taskMemoPickerSearchEl.value !== taskMemoPickerSearch) {
        elements.taskMemoPickerSearchEl.value = taskMemoPickerSearch;
      }
      if (elements.taskMemoPickerFilterEl && elements.taskMemoPickerFilterEl.value !== taskMemoPickerFilter) {
        elements.taskMemoPickerFilterEl.value = taskMemoPickerFilter;
      }
      renderMemoPickerList();
      return taskMemoOptions;
    }

    function renderMemoPickerList() {
      const listEl = elements.taskMemoPickerListEl;
      if (!listEl || !documentRef) return;
      listEl.innerHTML = "";
      if (!taskMemoPickerOpen) return;
      if (!taskMemoOptions.length) {
        listEl.innerHTML = `<div class="empty compact">${escapeHtml(t("memo.empty", "No memos."))}</div>`;
        return;
      }
      for (const memo of taskMemoOptions) {
        const button = documentRef.createElement("button");
        button.type = "button";
        button.className = `entity-picker-item${memo.id === activeTaskMemoId ? " active" : ""}`;
        button.dataset.taskMemoOption = memo.id;
        const title = documentRef.createElement("span");
        title.className = "entity-picker-title";
        title.textContent = memoLinkLabel(memo);
        const meta = documentRef.createElement("span");
        meta.className = "entity-picker-meta";
        meta.textContent = memoLinkMeta(memo);
        button.appendChild(title);
        button.appendChild(meta);
        listEl.appendChild(button);
      }
    }

    async function loadTaskMemoOptions() {
      if (!currentRunId()) {
        taskMemoOptions = [];
        renderDraftBox();
        return [];
      }
      const payload = await deps.fetchJson(deps.apiUrl("/api/task-memos", memoPickerParams()), {}, "Failed to load task memos");
      taskMemoOptions = Array.isArray(payload?.memos) ? payload.memos : [];
      if (activeTaskMemoId && !taskMemoOptions.some(memo => memo.id === activeTaskMemoId)) activeTaskMemoId = "";
      renderDraftBox();
      return taskMemoOptions;
    }

    function deleteDraft(_draftId = "", options = {}) {
      activeTaskMemoId = "";
      taskMemoPickerOpen = false;
      renderDraftBox();
      if (options.showStatus) setDraftStatus(t("task.memo_link_cleared", "Memo link cleared."));
      return true;
    }

    function clearDraft() {
      activeTaskMemoId = "";
      taskMemoPickerOpen = false;
      renderDraftBox();
      setDraftStatus("");
    }

    function selectHasValue(selectEl, value) {
      return Boolean(selectEl && [...(selectEl.options || [])].some(option => option.value === value));
    }

    function setSelectValue(selectEl, value) {
      const next = String(value || "");
      if (!selectEl || !next || !selectHasValue(selectEl, next)) return false;
      selectEl.value = next;
      return true;
    }

    function setInputValue(inputEl, value) {
      if (inputEl && value != null) inputEl.value = String(value);
    }

    function setCheckboxValue(inputEl, value) {
      if (inputEl && typeof value === "boolean") inputEl.checked = value;
    }

    function restoreWorkspaceDraft(values = {}) {
      if (!elements.workspaceSelectEl) return;
      const workspaceValue = String(values.workspace_value || "");
      const workspacePath = String(values.workspace_path || values.workspace_custom || "");
      const restoredSelect = workspaceValue && workspaceValue !== "__custom__" && setSelectValue(elements.workspaceSelectEl, workspaceValue);
      if (!restoredSelect && workspacePath && selectHasValue(elements.workspaceSelectEl, "__custom__")) {
        elements.workspaceSelectEl.value = "__custom__";
        setInputValue(elements.workspaceCustomEl, workspacePath);
      } else if (values.workspace_custom) {
        setInputValue(elements.workspaceCustomEl, values.workspace_custom);
      }
      elements.workspaceCustomEl?.classList.toggle("hidden", elements.workspaceSelectEl.value !== "__custom__");
    }

    function restoreAskUserGates(values = {}) {
      const gates = values.supervision_ask_user_gates || {};
      if (!gates || typeof gates !== "object") return;
      elements.taskSupervisionAskUserGatesEl?.querySelectorAll("[data-supervision-ask-user-gate]").forEach(input => {
        const key = input.dataset.supervisionAskUserGate;
        if (Object.prototype.hasOwnProperty.call(gates, key)) input.checked = Boolean(gates[key]);
      });
    }

    function applyTaskFormValues(values = {}) {
      setInputValue(elements.newTaskTitleEl, values.title);
      setInputValue(elements.newTaskDescriptionEl, values.description);
      const restoredBackend = setSelectValue(elements.taskBackendEl, values.backend);
      if (restoredBackend) {
        deps.fillModelSelect?.(elements.taskModelEl, elements.taskBackendEl.value, values.model || "");
      } else {
        setSelectValue(elements.taskModelEl, values.model);
      }
      setSelectValue(elements.taskSandboxEl, values.sandbox);
      setSelectValue(elements.taskApprovalEl, values.approval);
      restoreWorkspaceDraft(values);
      setSelectValue(elements.collaborationModeEl, values.collaboration_mode);
      setSelectValue(elements.workflowTemplateEl, values.workflow_template);
      setInputValue(elements.maxSubAgentsEl, values.max_sub_agents);
      deps.taskOptionsController?.syncCollaborationFields?.();
      deps.taskOptionsController?.syncWorkflowTemplateHelp?.();
      setSelectValue(elements.taskSupervisionModeEl, values.supervision_mode);
      deps.syncCreateTaskSupervisionModeFields?.();
      setSelectValue(elements.taskSupervisionHostModelEl, values.supervision_host_model);
      setCheckboxValue(elements.taskSupervisionHostProxyEnabledEl, values.supervision_host_proxy_enabled);
      setInputValue(elements.taskSupervisionMaxRoundsEl, values.supervision_max_rounds);
      restoreAskUserGates(values);
      setCheckboxValue(elements.taskContextAutoCompactEnabledEl, values.context_auto_compact_enabled);
      setInputValue(elements.taskContextThresholdEl, values.context_threshold_percent);
      syncCreateTaskContextFields();
      setCheckboxValue(elements.taskProxyEnabledEl, values.proxy_enabled);
    }

    function memoTaskValues(memo = {}) {
      return {
        title: memo.title || "",
        description: memo.description || "",
        backend: memo.backend || "",
        model: memo.model || "",
        sandbox: memo.sandbox || "",
        approval: memo.approval || "",
        workspace_path: memo.workspace_path || "",
        workspace_custom: memo.workspace_path || "",
        collaboration_mode: memo.collaboration_mode || "auto",
        workflow_template: memo.workflow_template || "auto",
        max_sub_agents: memo.max_sub_agents ?? "",
        supervision_mode: "manual",
        context_auto_compact_enabled: true,
        context_threshold_percent: "75",
        proxy_enabled: typeof memo.proxy_enabled === "boolean" ? memo.proxy_enabled : undefined
      };
    }

    function restoreDraft(draftId = activeTaskMemoId, options = {}) {
      const selectedId = String(draftId || "").trim();
      const memo = taskMemoOptions.find(item => item.id === selectedId) || null;
      if (!memo) {
        if (options.showStatus) setDraftStatus("");
        return false;
      }
      applyTaskFormValues(memoTaskValues(memo));
      activeTaskMemoId = memo.id;
      taskMemoPickerOpen = false;
      renderDraftBox();
      if (options.showStatus) setDraftStatus(t("task.memo_linked", "Memo linked."));
      return true;
    }

    function applyTaskMemoToForm(memo = {}) {
      const memoId = String(memo.id || "").trim();
      if (!memoId) return false;
      applyTaskFormValues(memoTaskValues(memo));
      upsertTaskMemoOption(memo);
      activeTaskMemoId = memoId;
      taskMemoPickerOpen = false;
      renderDraftBox();
      setDraftStatus(t("task.memo_linked", "Memo linked."));
      return true;
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
      const supervision = payload.supervision || {};
      return {
        runId: currentRunId() || "-",
        workspaceLabel: selectedWorkspaceLabel() || payload.workspace_path || payload.workspace_id || "-",
        backendLabel: taskBackendConfirmLabel(payload),
        hostModelLabel: supervision.host_backend
          ? deps.modelLabelForBackend?.(supervision.host_backend, supervision.host_model)
          : "",
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
        alertUser(t("task.create_run_first", "Create a run before adding a task."));
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
        deps.readAskUserGateControls?.(elements.taskSupervisionAskUserGatesEl),
        {
          hostModel: elements.taskSupervisionHostModelEl?.value || null,
          hostProxyEnabled: Boolean(elements.taskSupervisionHostProxyEnabledEl?.checked)
        }
      );
      deps.setCreateProxyDefaultsFromInputs?.();
      const createProxyEnabled = Boolean(elements.taskProxyEnabledEl?.checked);
      const contextThreshold = deps.normalizeTaskContextThreshold?.(elements.taskContextThresholdEl?.value || 75) || 75;
      if (elements.taskContextThresholdEl) elements.taskContextThresholdEl.value = String(contextThreshold);
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
        contextAutoCompactEnabled: elements.taskContextAutoCompactEnabledEl?.checked !== false,
        contextThreshold,
        dispatch: true
      });
      if (payload && activeTaskMemoId) payload.source_memo_id = activeTaskMemoId;
      const createdFromMemo = Boolean(activeTaskMemoId);
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
          workflow_template: payload?.workflow_template || "",
          context_auto_compact_enabled: payload?.context_management?.auto_compact_enabled
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
        clearDraft();
        if (elements.newTaskTitleEl) elements.newTaskTitleEl.value = "";
        if (elements.newTaskDescriptionEl) elements.newTaskDescriptionEl.value = "";
        if (createdTaskId && createdFromMemo && deps.refreshRunScopedView) {
          await deps.refreshRunScopedView();
        } else {
          await deps.loadStatus?.({ forceAgents: Boolean(createdTaskId) });
        }
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
      elements.taskMemoPickerToggleEl?.addEventListener("click", () => {
        taskMemoPickerOpen = !taskMemoPickerOpen;
        if (taskMemoPickerOpen) {
          void loadTaskMemoOptions().catch(err => {
            const message = err?.message || String(err);
            setDraftStatus(message);
          });
        } else {
          renderDraftBox();
        }
      });
      elements.taskMemoLinkClearEl?.addEventListener("click", () => {
        deleteDraft("", { showStatus: true });
      });
      elements.taskMemoPickerSearchEl?.addEventListener("input", () => {
        taskMemoPickerSearch = String(elements.taskMemoPickerSearchEl?.value || "").trim();
        void loadTaskMemoOptions().catch(err => {
          const message = err?.message || String(err);
          setDraftStatus(message);
        });
      });
      elements.taskMemoPickerFilterEl?.addEventListener("change", () => {
        taskMemoPickerFilter = String(elements.taskMemoPickerFilterEl?.value || "active_unlinked");
        void loadTaskMemoOptions().catch(err => {
          const message = err?.message || String(err);
          setDraftStatus(message);
        });
      });
      elements.taskMemoPickerListEl?.addEventListener("click", event => {
        const option = event.target instanceof Element ? event.target.closest("[data-task-memo-option]") : null;
        if (!option) return;
        restoreDraft(option.dataset.taskMemoOption || "", { showStatus: true });
      });
      elements.taskContextAutoCompactEnabledEl?.addEventListener("change", syncCreateTaskContextFields);
      if (elements.taskCreateDialogEl && windowRef.MutationObserver) {
        const observer = new windowRef.MutationObserver(() => {
          if (elements.taskCreateDialogEl.open) {
            void loadTaskMemoOptions().catch(err => {
              const message = err?.message || String(err);
              setDraftStatus(message);
            });
          }
        });
        observer.observe(elements.taskCreateDialogEl, { attributes: true, attributeFilter: ["open"] });
      }
      renderDraftBox();
      syncCreateTaskContextFields();
      void loadTaskMemoOptions().catch(err => {
        const message = err?.message || String(err);
        setDraftStatus(message);
      });
    }

    return Object.freeze({
      bind,
      applyTaskMemoToForm,
      clearDraft,
      confirmAddTask,
      deleteDraft,
      loadTaskMemoOptions,
      renderDraftBox,
      restoreDraft,
      selectedWorkspaceId,
      selectedWorkspaceLabel,
      selectedWorkspacePath,
      taskBackendConfirmLabel
    });
  }

  window.AHATaskCreateController = Object.freeze({ createTaskCreateController, createTaskOptionsController });
})();
