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
    const realtimeDebug = deps.realtimeDebug || (() => {});
    const draftStoragePrefix = "aha.taskCreateDrafts";
    const legacyDraftStoragePrefix = "aha.taskCreateDraft";
    let createInFlight = false;
    let activeDraftId = "";
    let activeDraftSource = "";

    function t(key, fallback = "") {
      return window.AHAI18n?.t?.(key, fallback) || fallback;
    }

    function currentRunId() {
      return String(deps.currentRunId?.() || "").trim();
    }

    function draftStorage() {
      try {
        return deps.localStorage || windowRef.localStorage || null;
      } catch (_err) {
        return null;
      }
    }

    function draftStorageKey(runId = currentRunId()) {
      return `${draftStoragePrefix}.${String(runId || "global").trim() || "global"}`;
    }

    function legacyDraftStorageKey(runId = currentRunId()) {
      return `${legacyDraftStoragePrefix}.${String(runId || "global").trim() || "global"}`;
    }

    function setDraftStatus(message = "") {
      if (elements.taskDraftStateEl) elements.taskDraftStateEl.textContent = message;
    }

    function readAskUserGates() {
      return deps.readAskUserGateControls?.(elements.taskSupervisionAskUserGatesEl) || {};
    }

    function newDraftId() {
      return `draft-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    }

    function readFormDraft(draftId = activeDraftId) {
      return {
        id: String(draftId || newDraftId()),
        version: 1,
        run_id: currentRunId(),
        saved_at: new Date().toISOString(),
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
          supervision_ask_user_gates: readAskUserGates()
        }
      };
    }

    function normalizeDraft(draft) {
      if (!draft || typeof draft !== "object") return null;
      const values = draft.values && typeof draft.values === "object" ? draft.values : null;
      if (!values) return null;
      return {
        id: String(draft.id || newDraftId()),
        version: 1,
        run_id: String(draft.run_id || currentRunId()),
        saved_at: String(draft.saved_at || new Date().toISOString()),
        values: { ...values }
      };
    }

    function sortDrafts(drafts = []) {
      return drafts.slice().sort((left, right) => String(right.saved_at || "").localeCompare(String(left.saved_at || "")));
    }

    function readDrafts() {
      const storage = draftStorage();
      if (!storage) return [];
      const drafts = [];
      try {
        const raw = storage.getItem(draftStorageKey());
        const parsed = raw ? JSON.parse(raw) : [];
        const items = Array.isArray(parsed) ? parsed : [parsed];
        for (const item of items) {
          const draft = normalizeDraft(item);
          if (draft) drafts.push(draft);
        }
      } catch (_err) {
        try {
          storage.removeItem(draftStorageKey());
        } catch (_removeErr) {
          // Ignore storage cleanup failures.
        }
      }
      try {
        const legacyRaw = storage.getItem(legacyDraftStorageKey());
        const legacyDraft = legacyRaw ? normalizeDraft({ ...JSON.parse(legacyRaw), id: "legacy" }) : null;
        if (legacyDraft && !drafts.some(draft => draft.id === legacyDraft.id)) drafts.push(legacyDraft);
      } catch (_err) {
        // Ignore malformed legacy drafts.
      }
      return sortDrafts(drafts);
    }

    function writeDrafts(drafts = []) {
      const storage = draftStorage();
      if (!storage) return false;
      try {
        storage.setItem(draftStorageKey(), JSON.stringify(sortDrafts(drafts).map(draft => normalizeDraft(draft)).filter(Boolean)));
        storage.removeItem(legacyDraftStorageKey());
        return true;
      } catch (_err) {
        return false;
      }
    }

    function draftLabel(draft) {
      const values = draft?.values || {};
      const title = String(values.title || "").trim();
      const description = String(values.description || "").trim().replace(/\s+/g, " ");
      const base = title || (description ? description.slice(0, 48) : t("task.untitled_draft", "Untitled draft"));
      let saved = String(draft?.saved_at || "");
      try {
        saved = new Date(saved).toLocaleString();
      } catch (_err) {
        // Keep the stored timestamp if date formatting is unavailable.
      }
      return saved ? `${base} · ${saved}` : base;
    }

    function renderDraftBox(selectedId = activeDraftId) {
      const drafts = readDrafts();
      const selectEl = elements.taskDraftSelectEl;
      if (selectEl && documentRef) {
        selectEl.innerHTML = "";
        if (!drafts.length) {
          const option = documentRef.createElement("option");
          option.value = "";
          option.textContent = t("task.draft_empty", "No drafts");
          selectEl.appendChild(option);
        } else {
          for (const draft of drafts) {
            const option = documentRef.createElement("option");
            option.value = draft.id;
            option.textContent = draftLabel(draft);
            selectEl.appendChild(option);
          }
          if (drafts.some(draft => draft.id === selectedId)) selectEl.value = selectedId;
        }
        selectEl.disabled = !drafts.length;
      }
      const disabled = !drafts.length;
      if (elements.restoreTaskDraftEl) elements.restoreTaskDraftEl.disabled = disabled;
      if (elements.deleteTaskDraftEl) elements.deleteTaskDraftEl.disabled = disabled;
      return drafts;
    }

    function saveDraft(options = {}) {
      const drafts = readDrafts();
      const updateExisting = activeDraftSource === "restored" && activeDraftId && drafts.some(item => item.id === activeDraftId);
      const draft = readFormDraft(updateExisting ? activeDraftId : "");
      const nextDrafts = updateExisting
        ? drafts.map(item => (item.id === draft.id ? draft : item))
        : [draft, ...drafts];
      const saved = writeDrafts(nextDrafts);
      if (saved) {
        activeDraftId = draft.id;
        activeDraftSource = updateExisting ? "restored" : "saved";
        renderDraftBox(activeDraftId);
        if (options.showStatus) setDraftStatus(t("task.draft_saved", "Draft saved."));
      } else if (options.showStatus) {
        setDraftStatus(t("task.draft_unavailable", "Draft storage unavailable."));
      }
      return saved;
    }

    function deleteDraft(draftId = elements.taskDraftSelectEl?.value, options = {}) {
      const id = String(draftId || "").trim();
      if (!id) return false;
      const drafts = readDrafts();
      const nextDrafts = drafts.filter(draft => draft.id !== id);
      if (nextDrafts.length === drafts.length) return false;
      const saved = writeDrafts(nextDrafts);
      if (saved) {
        if (activeDraftId === id) {
          activeDraftId = "";
          activeDraftSource = "";
        }
        renderDraftBox();
        if (options.showStatus) setDraftStatus(t("task.draft_deleted", "Draft deleted."));
      } else if (options.showStatus) {
        setDraftStatus(t("task.draft_unavailable", "Draft storage unavailable."));
      }
      return saved;
    }

    function clearDraft() {
      if (activeDraftId) {
        deleteDraft(activeDraftId);
      } else {
        renderDraftBox();
      }
      activeDraftId = "";
      activeDraftSource = "";
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

    function restoreDraft(draftId = elements.taskDraftSelectEl?.value, options = {}) {
      const selectedId = String(draftId || "").trim();
      const draft = readDrafts().find(item => item.id === selectedId) || null;
      const values = draft?.values || null;
      if (!values) {
        if (options.showStatus) setDraftStatus("");
        return false;
      }
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
      setCheckboxValue(elements.taskProxyEnabledEl, values.proxy_enabled);
      activeDraftId = draft.id;
      activeDraftSource = "restored";
      renderDraftBox(activeDraftId);
      if (options.showStatus) setDraftStatus(t("task.draft_restored", "Draft restored."));
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
        clearDraft();
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
      elements.saveTaskDraftEl?.addEventListener("click", () => {
        saveDraft({ showStatus: true });
      });
      elements.restoreTaskDraftEl?.addEventListener("click", () => {
        restoreDraft(elements.taskDraftSelectEl?.value, { showStatus: true });
      });
      elements.deleteTaskDraftEl?.addEventListener("click", () => {
        deleteDraft(elements.taskDraftSelectEl?.value, { showStatus: true });
      });
      if (elements.taskCreateDialogEl && windowRef.MutationObserver) {
        const observer = new windowRef.MutationObserver(() => {
          if (elements.taskCreateDialogEl.open) renderDraftBox();
        });
        observer.observe(elements.taskCreateDialogEl, { attributes: true, attributeFilter: ["open"] });
      }
      renderDraftBox();
    }

    return Object.freeze({
      bind,
      clearDraft,
      confirmAddTask,
      deleteDraft,
      draftStorageKey,
      renderDraftBox,
      restoreDraft,
      saveDraft,
      selectedWorkspaceId,
      selectedWorkspaceLabel,
      selectedWorkspacePath,
      taskBackendConfirmLabel
    });
  }

  window.AHATaskCreateController = Object.freeze({ createTaskCreateController, createTaskOptionsController });
})();
