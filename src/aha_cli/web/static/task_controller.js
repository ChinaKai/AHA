(() => {
  function createTaskController(elements = {}, deps = {}) {
    const normalizeTaskVisibilityFilter = deps.normalizeTaskVisibilityFilter || (value => value || "active");
    const taskVisibilityFilterHtml = deps.taskVisibilityFilterHtml || (() => "");
    const taskListItemClass = deps.taskListItemClass || (() => "task");
    const taskListItemHtml = deps.taskListItemHtml || (() => "");
    const taskListTitle = deps.taskListTitle || (() => "");
    const visibleTasksForFilter = deps.visibleTasksForFilter || ((tasks = []) => tasks);
    const dispatchAction = deps.dispatchAction || (() => {});
    const escapeHtml = deps.escapeHtml || (value => String(value ?? ""));
    const documentRef = deps.documentRef || document;
    const windowRef = deps.windowRef || window;
    const terminalTaskStatuses = new Set(["completed", "failed", "blocked"]);
    let taskSettingsOpen = false;
    let taskSettingsTaskId = "";

    function t(key, fallback = "") {
      return window.AHAI18n?.t?.(key, fallback) || fallback;
    }

    function allTasks() {
      return Array.isArray(deps.allTasks?.()) ? deps.allTasks() : [];
    }

    function filter() {
      return normalizeTaskVisibilityFilter(deps.taskVisibilityFilter?.());
    }

    function visibleTasks() {
      return visibleTasksForFilter(allTasks(), filter());
    }

    function taskById(taskId) {
      const id = String(taskId || "");
      if (!id) return null;
      return allTasks().find(task => String(task?.id || "") === id) || null;
    }

    function taskSettingsTask() {
      return taskSettingsTaskId ? taskById(taskSettingsTaskId) : null;
    }

    function renderTaskVisibilityFilter() {
      if (!elements.taskVisibilityFilterEl) return;
      const normalized = filter();
      deps.setTaskVisibilityFilter?.(normalized);
      elements.taskVisibilityFilterEl.innerHTML = taskVisibilityFilterHtml(allTasks(), normalized);
    }

    function taskSummaries(task) {
      return {
        workflow: deps.taskWorkflowSummary?.(task),
        execution: deps.taskCollaborationSummary?.(task),
        defaultBackend: task?.preferred_backend || "-",
        proxy: deps.taskProxySummary?.(task),
        supervision: deps.taskSupervisionSummary?.(task),
        context: deps.taskContextSummary?.(task),
        timing: deps.taskTimingLabel?.(task?.id, task)
      };
    }

    function taskStatusBadges(task) {
      if (task?.hidden) return '<span class="status hidden">hidden</span>';
      const primary = deps.taskDisplayStatus?.(task);
      const activity = deps.taskActivityStatus?.(task);
      const badges = [`<span class="status ${escapeHtml(primary)}">${escapeHtml(primary)}</span>`];
      if (activity !== "idle" && activity !== primary) {
        badges.push(`<span class="status activity ${escapeHtml(activity)}">${escapeHtml(activity)}</span>`);
      }
      return badges.join("");
    }

    function taskProxyBadge(task) {
      if (!deps.taskProxyConfigured?.(task)) return '<span class="status proxy-unset">Core proxy unset</span>';
      if (task?.preferred_proxy_enabled) return '<span class="status proxy-on">proxy switch on</span>';
      return '<span class="status proxy-off">proxy switch off</span>';
    }

    function taskSupervisionBadge(task) {
      const policy = deps.taskSupervisionPolicy?.(task) || {};
      const label = policy.mode === "assisted" && policy.real_agent_enabled
        ? `${policy.host_backend} host`
        : (policy.mode === "assisted" ? "assisted stub" : "manual");
      return `<span class="status supervision-${escapeHtml(policy.mode)}">${escapeHtml(label)}</span>`;
    }

    function taskContextBadge(task) {
      const policy = deps.taskContextManagementPolicy?.(task) || {};
      return policy.auto_compact_enabled
        ? `<span class="status context-auto-on">ctx auto ${escapeHtml(policy.auto_compact_threshold_percent)}%</span>`
        : '<span class="status context-auto-off">ctx auto off</span>';
    }

    function taskCurrentStatus(task) {
      return String(task?.current_status || task?.status || "pending").toLowerCase();
    }

    function taskSettingsButton(role) {
      return elements.taskSettingsActionsEl?.querySelector(`[data-task-settings-role="${role}"]`) || null;
    }

    function setTaskSettingsButton(button, action, label, disabled) {
      if (!button) return;
      button.dataset.taskSettingsAction = action;
      button.textContent = label;
      button.disabled = Boolean(disabled);
      button.setAttribute("aria-disabled", String(Boolean(disabled)));
    }

    function renderTaskSettingsActions(task) {
      const disabled = !task;
      const locked = task ? terminalTaskStatuses.has(taskCurrentStatus(task)) : false;
      const completionAction = locked ? "reopen" : "final";
      const completionLabel = locked ? t("task.reopen", "Reopen") : t("task.final", "Final");
      const visibilityAction = task?.hidden ? "restore" : "hide";
      const visibilityLabel = task?.hidden ? t("task.restore", "Restore") : t("common.hide", "Hide");
      setTaskSettingsButton(taskSettingsButton("completion"), completionAction, completionLabel, disabled);
      setTaskSettingsButton(taskSettingsButton("visibility"), visibilityAction, visibilityLabel, disabled);
      setTaskSettingsButton(taskSettingsButton("delete"), "delete", t("task.delete", "Delete"), disabled);
    }

    function taskSettingsTriggerFor(taskId) {
      const buttons = elements.tasksEl?.querySelectorAll("[data-task-settings-trigger]") || [];
      return Array.from(buttons).find(button => button.getAttribute("data-task-settings-trigger") === taskId) || null;
    }

    function clearTaskSettingsPosition() {
      if (!elements.taskSettingsPanelEl) return;
      elements.taskSettingsPanelEl.style.removeProperty("top");
      elements.taskSettingsPanelEl.style.removeProperty("left");
      elements.taskSettingsPanelEl.style.removeProperty("width");
    }

    function taskSettingsUseSheet() {
      return Boolean(windowRef.matchMedia?.("(max-width: 640px)")?.matches);
    }

    function positionTaskSettingsPanel(task) {
      const panel = elements.taskSettingsPanelEl;
      if (!panel || !task) return;
      clearTaskSettingsPosition();
      const useSheet = taskSettingsUseSheet();
      panel.classList.toggle("task-settings-sheet", useSheet);
      panel.classList.toggle("task-settings-popover", !useSheet);
      if (useSheet) return;
      const trigger = taskSettingsTriggerFor(task.id);
      const rect = trigger?.getBoundingClientRect?.();
      if (!rect) return;
      const margin = 12;
      const gap = 8;
      const width = Math.min(360, Math.max(280, windowRef.innerWidth - margin * 2));
      panel.style.width = `${width}px`;
      const height = Math.min(panel.offsetHeight || 420, windowRef.innerHeight - margin * 2);
      let left = rect.right + gap;
      if (left + width > windowRef.innerWidth - margin) left = rect.left - width - gap;
      left = Math.max(margin, Math.min(left, windowRef.innerWidth - width - margin));
      const maxTop = Math.max(margin, windowRef.innerHeight - height - margin);
      const top = Math.max(margin, Math.min(rect.top, maxTop));
      panel.style.left = `${Math.round(left)}px`;
      panel.style.top = `${Math.round(top)}px`;
    }

    function renderTaskSettingsPanel(task, options = {}) {
      if (!elements.taskSettingsPanelEl) return;
      const open = Boolean(taskSettingsOpen && task && task.id === taskSettingsTaskId);
      const forceEditors = Boolean(options.forceEditors);
      elements.taskSettingsPanelEl.classList.toggle("hidden", !open);
      elements.taskSettingsPanelEl.hidden = !open;
      if (elements.taskSettingsSubtitleEl) {
        elements.taskSettingsSubtitleEl.textContent = task
          ? `${task.id} | ${deps.pathName?.(task.workspace_path) || "-"}`
          : "";
      }
      renderTaskSettingsActions(open ? task : null);
      if (open) {
        if (forceEditors || !deps.isTaskProxyEditing?.()) deps.renderTaskProxyEditor?.(task);
        if (forceEditors || !deps.isTaskSupervisionEditing?.()) deps.renderTaskSupervisionEditor?.(task);
        if (forceEditors || !deps.isTaskContextEditing?.()) deps.renderTaskContextEditor?.(task);
      } else {
        deps.renderTaskProxyEditor?.(null);
        deps.renderTaskSupervisionEditor?.(null);
        deps.renderTaskContextEditor?.(null);
      }
      if (open) positionTaskSettingsPanel(task);
      else clearTaskSettingsPosition();
    }

    function renderTaskList() {
      const tasksEl = elements.tasksEl;
      if (!tasksEl) return;
      renderTaskVisibilityFilter();
      tasksEl.innerHTML = "";
      const tasks = visibleTasks();
      if (!tasks.length) {
        tasksEl.innerHTML = '<div class="empty compact">No visible tasks.</div>';
        return;
      }
      for (const task of tasks) {
        const summaries = taskSummaries(task);
        const item = document.createElement("div");
        const settingsOpen = taskSettingsOpen && task.id === taskSettingsTaskId;
        item.className = `${taskListItemClass(task, deps.selectedTaskId?.())}${settingsOpen ? " settings-open" : ""}`;
        item.dataset.taskId = task.id;
        item.innerHTML = taskListItemHtml(task, {
          summaries,
          statusBadgesHtml: taskStatusBadges(task),
          proxyBadgeHtml: taskProxyBadge(task),
          supervisionBadgeHtml: taskSupervisionBadge(task),
          contextBadgeHtml: taskContextBadge(task),
          selected: task.id === deps.selectedTaskId?.(),
          activeTab: deps.activeTab?.() || "conversation",
          settingsOpen
        });
        item.title = taskListTitle(task, summaries);
        item.addEventListener("click", async event => {
          const target = event.target instanceof Element ? event.target : null;
          if (target?.closest("button, .conversation-filter-popover")) return;
          closeTaskSettings({ renderList: false });
          await selectTask(task.id);
        });
        tasksEl.appendChild(item);
      }
    }

    function renderHeaderWorkspace(task) {
      if (!elements.headerWorkspaceDirEl) return;
      const workspace = task?.workspace_path || "";
      elements.headerWorkspaceDirEl.textContent = workspace ? deps.pathName?.(workspace) : "";
      elements.headerWorkspaceDirEl.title = workspace;
    }

    function renderMobileTaskSummary(task) {
      if (!elements.mobileTaskSummaryEl || !elements.mobileTaskTitleEl || !elements.mobileTaskStatusEl) return;
      if (!task) {
        elements.mobileTaskTitleEl.textContent = "No task";
        elements.mobileTaskStatusEl.textContent = "empty";
        elements.mobileTaskStatusEl.className = "status pending";
        elements.mobileTaskSummaryEl.title = "No task selected";
        return;
      }
      const displayStatus = task.hidden ? "hidden" : deps.taskDisplayStatus?.(task);
      elements.mobileTaskTitleEl.textContent = task.id;
      elements.mobileTaskStatusEl.textContent = displayStatus;
      elements.mobileTaskStatusEl.className = `status ${displayStatus}`;
      elements.mobileTaskSummaryEl.title = `${task.id} / ${task.title}`;
    }

    function renderSelectedHeader() {
      const task = deps.selectedTask?.();
      if (!task) {
        renderHeaderWorkspace(null);
        renderMobileTaskSummary(null);
        taskSettingsOpen = false;
        taskSettingsTaskId = "";
        renderTaskSettingsPanel(null);
        if (elements.selectedIdEl) elements.selectedIdEl.textContent = "";
        if (elements.selectedTitleEl) elements.selectedTitleEl.textContent = "No tasks";
        if (elements.selectedTaskMetaEl) {
          elements.selectedTaskMetaEl.textContent = "";
          elements.selectedTaskMetaEl.hidden = true;
        }
        if (elements.selectedStatusEl) {
          elements.selectedStatusEl.textContent = "empty";
          elements.selectedStatusEl.className = "status pending";
        }
        return;
      }
      renderHeaderWorkspace(task);
      renderMobileTaskSummary(task);
      renderTaskSettingsPanel(taskSettingsTask());
      if (elements.selectedIdEl) elements.selectedIdEl.textContent = task.id;
      if (elements.selectedTitleEl) elements.selectedTitleEl.textContent = task.title;
      if (elements.selectedTaskMetaEl) {
        const agentCount = Number.isFinite(Number(task.agent_count)) ? Number(task.agent_count) : (task.agents || []).length;
        elements.selectedTaskMetaEl.textContent = `${agentCount} ${agentCount === 1 ? "agent" : "agents"} | ${deps.pathName?.(task.workspace_path) || "-"}`;
        elements.selectedTaskMetaEl.hidden = false;
      }
      const displayStatus = task.hidden ? "hidden" : deps.taskDisplayStatus?.(task);
      if (elements.selectedStatusEl) {
        elements.selectedStatusEl.textContent = displayStatus;
        elements.selectedStatusEl.className = `status ${displayStatus}`;
      }
    }

    async function selectTask(taskId) {
      return await dispatchAction("select-task", { taskId });
    }

    async function updateTaskVisibility(taskId, action) {
      return await dispatchAction("task-visibility", { taskId, action });
    }

    async function openTaskSettings(taskId) {
      if (!taskId) return;
      if (taskSettingsOpen && taskSettingsTaskId === taskId) {
        closeTaskSettings();
        return;
      }
      taskSettingsOpen = true;
      taskSettingsTaskId = taskId;
      deps.setTaskSettingsEditorTaskId?.(taskId);
      renderTaskList();
      renderTaskSettingsPanel(taskSettingsTask(), { forceEditors: true });
    }

    function closeTaskSettings(options = {}) {
      taskSettingsOpen = false;
      taskSettingsTaskId = "";
      deps.setTaskSettingsEditorTaskId?.("");
      if (options.renderList !== false) renderTaskList();
      renderTaskSettingsPanel(null, { forceEditors: true });
    }

    function renderAfterFilterChange() {
      renderTaskList();
      renderSelectedHeader();
      if (!deps.isTaskProxyEditing?.()) deps.renderTaskProxyEditor?.();
      if (!deps.isTaskSupervisionEditing?.()) deps.renderTaskSupervisionEditor?.();
      if (!deps.isTaskContextEditing?.()) deps.renderTaskContextEditor?.();
      deps.renderAgents?.();
      deps.renderConversationFilters?.();
      deps.renderPanel?.();
    }

    function bind() {
      elements.tasksEl?.addEventListener("pointerdown", event => {
        const target = event.target instanceof Element ? event.target : null;
        const button = target?.closest("[data-action]");
        if (!button) return;
        const taskEl = button.closest("[data-task-id]");
        if (!taskEl) return;
        event.preventDefault();
        event.stopPropagation();
        void updateTaskVisibility(taskEl.dataset.taskId, button.dataset.action);
      });

      elements.tasksEl?.addEventListener("click", event => {
        const target = event.target instanceof Element ? event.target : null;
        const button = target?.closest("[data-task-settings-trigger]");
        if (!button) return;
        event.preventDefault();
        event.stopPropagation();
        void openTaskSettings(button.getAttribute("data-task-settings-trigger"));
      });

      elements.taskSettingsCloseEl?.addEventListener("pointerdown", event => {
        event.stopPropagation();
      });

      elements.taskSettingsCloseEl?.addEventListener("click", event => {
        event.preventDefault();
        event.stopPropagation();
        closeTaskSettings();
      });

      elements.taskSettingsActionsEl?.addEventListener("click", event => {
        const target = event.target instanceof Element ? event.target : null;
        const button = target?.closest("[data-task-settings-action]");
        const task = taskSettingsTask();
        if (!button || !task || button.disabled) return;
        event.preventDefault();
        event.stopPropagation();
        void updateTaskVisibility(task.id, button.dataset.taskSettingsAction);
      });

      elements.taskVisibilityFilterEl?.addEventListener("click", event => {
        const target = event.target instanceof Element ? event.target : null;
        const button = target?.closest("[data-task-visibility-filter]");
        if (!button) return;
        const nextFilter = normalizeTaskVisibilityFilter(button.getAttribute("data-task-visibility-filter"));
        if (nextFilter === filter()) return;
        deps.setTaskVisibilityFilter?.(nextFilter);
        const tasks = visibleTasks();
        if (!tasks.some(task => task.id === deps.selectedTaskId?.())) {
          deps.setSelectedTaskId?.(deps.defaultTaskId?.(tasks));
        }
        deps.writeStoredSelectedTaskId?.(deps.selectedTaskId?.());
        renderAfterFilterChange();
      });

      documentRef.addEventListener("pointerdown", event => {
        if (!taskSettingsOpen) return;
        const target = event.target instanceof Element ? event.target : null;
        if (!target) return;
        if (elements.taskSettingsPanelEl?.contains(target)) return;
        if (target.closest("[data-task-settings-trigger]")) return;
        closeTaskSettings();
      });

      documentRef.addEventListener("keydown", event => {
        if (event.key !== "Escape" || !taskSettingsOpen) return;
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation?.();
        closeTaskSettings();
      });

      windowRef.addEventListener?.("resize", () => {
        if (!taskSettingsOpen) return;
        renderTaskSettingsPanel(taskSettingsTask());
      });
    }

    return Object.freeze({
      bind,
      renderTaskList,
      renderTaskVisibilityFilter,
      renderSelectedHeader,
      selectTask,
      updateTaskVisibility,
      visibleTasks
    });
  }

  window.AHATaskController = Object.freeze({ createTaskController });
})();
