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

    function allTasks() {
      return Array.isArray(deps.allTasks?.()) ? deps.allTasks() : [];
    }

    function filter() {
      return normalizeTaskVisibilityFilter(deps.taskVisibilityFilter?.());
    }

    function visibleTasks() {
      return visibleTasksForFilter(allTasks(), filter());
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
        item.className = taskListItemClass(task, deps.selectedTaskId?.());
        item.dataset.taskId = task.id;
        item.innerHTML = taskListItemHtml(task, {
          summaries,
          statusBadgesHtml: taskStatusBadges(task),
          proxyBadgeHtml: taskProxyBadge(task),
          supervisionBadgeHtml: taskSupervisionBadge(task),
          contextBadgeHtml: taskContextBadge(task)
        });
        item.title = taskListTitle(task, summaries);
        item.addEventListener("click", async event => {
          const target = event.target instanceof Element ? event.target : null;
          if (target?.closest("button")) return;
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
        deps.renderTaskProxyEditor?.();
        deps.renderTaskSupervisionEditor?.();
        deps.renderTaskContextEditor?.();
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
      if (elements.selectedIdEl) elements.selectedIdEl.textContent = task.id;
      if (elements.selectedTitleEl) elements.selectedTitleEl.textContent = task.title;
      if (elements.selectedTaskMetaEl) {
        elements.selectedTaskMetaEl.textContent = `workflow ${deps.taskWorkflowSummary?.(task)} | execution ${deps.taskCollaborationSummary?.(task)} | supervision ${deps.taskSupervisionSummary?.(task)} | context ${deps.taskContextSummary?.(task)}`;
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

    function renderAfterFilterChange() {
      renderTaskList();
      renderSelectedHeader();
      deps.renderTaskProxyEditor?.();
      deps.renderTaskSupervisionEditor?.();
      deps.renderTaskContextEditor?.();
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
