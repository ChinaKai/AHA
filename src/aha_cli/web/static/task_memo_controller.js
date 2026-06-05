(() => {
  function createTaskMemoController(elements = {}, deps = {}) {
    const windowRef = deps.windowRef || window;
    const documentRef = deps.documentRef || windowRef.document || (typeof document !== "undefined" ? document : null);
    const escapeHtml = deps.escapeHtml || (value => String(value ?? ""));
    const taskDisplayStatus = deps.taskDisplayStatus || (task => String(task?.display_status || task?.outcome_status || task?.current_status || task?.status || "pending").toLowerCase());
    let memos = [];
    let selectedDate = isoDate(new Date());
    let selectedMemoId = "";
    let selectedMemoRunId = "";
    let editorMode = "empty";
    let draftTaskLinkMemoId = "";
    let draftTaskLinkId = "";
    let memoFilter = "day";
    let remoteSelectedMemoRunId = "";
    let remoteSelectedMemoLoaded = false;
    let remoteSelectedMemoId = "";
    let taskPickerOpen = false;
    let taskPickerSearch = "";
    let taskPickerFilter = "active";
    let pageModeSyncTimer = 0;
    let lunarFormatter = null;
    let lunarFormatterReady = false;
    const terminalTaskStatuses = new Set(["completed", "failed", "blocked"]);
    const memoStatuses = ["todo", "doing", "paused", "done", "closed"];
    const memoFilters = ["day", ...memoStatuses, "all"];
    const terminalMemoStatuses = new Set(["done", "closed"]);
    const memoStatusAliases = Object.freeze({
      open: "todo",
      incomplete: "todo",
      pending: "todo",
      running: "doing",
      blocked: "paused",
      suspended: "paused",
      complete: "done",
      completed: "done",
      archived: "closed"
    });
    const lunarDayNames = [
      "",
      "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
      "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
      "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十"
    ];

    function t(key, fallback = "") {
      return windowRef.AHAI18n?.t?.(key, fallback) || fallback;
    }

    function isoDate(date) {
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      return `${year}-${month}-${day}`;
    }

    function monthValue(dateText = selectedDate) {
      return String(dateText || isoDate(new Date())).slice(0, 7);
    }

    function memoDateValue(value = selectedDate) {
      const text = String(value || "").trim();
      return /^\d{4}-\d{2}-\d{2}$/.test(text) ? text : isoDate(new Date());
    }

    function selectedMemoStorageKey() {
      const runId = currentRunId();
      return runId ? `aha:selectedTaskMemo:${runId}` : "";
    }

    function currentRunId() {
      return String(deps.currentRunId?.() || "").trim();
    }

    function readStoredSelectedMemoId() {
      const key = selectedMemoStorageKey();
      if (!key) return "";
      try {
        return String(windowRef.localStorage?.getItem(key) || "").trim();
      } catch (_err) {
        return "";
      }
    }

    function writeStoredSelectedMemoId(memoId = "") {
      const key = selectedMemoStorageKey();
      if (!key) return;
      try {
        const value = String(memoId || "").trim();
        if (value) {
          windowRef.localStorage?.setItem(key, value);
        } else {
          windowRef.localStorage?.removeItem(key);
        }
      } catch (_err) {
        // localStorage can be unavailable in restricted browser modes.
      }
    }

    async function readPersistedSelectedMemoId() {
      const runId = currentRunId();
      if (remoteSelectedMemoRunId !== runId) {
        remoteSelectedMemoRunId = runId;
        remoteSelectedMemoLoaded = false;
        remoteSelectedMemoId = "";
      }
      if (remoteSelectedMemoLoaded) return remoteSelectedMemoId || readStoredSelectedMemoId();
      remoteSelectedMemoLoaded = true;
      try {
        const payload = await deps.fetchJson(deps.apiUrl("/api/ui-state"), {}, "Failed to load UI state");
        remoteSelectedMemoId = String(payload?.last_selected_memo_id || "").trim();
        return remoteSelectedMemoId || readStoredSelectedMemoId();
      } catch (err) {
        deps.consoleRef?.warn?.("Failed to load memo UI state", err);
        remoteSelectedMemoId = "";
        return readStoredSelectedMemoId();
      }
    }

    function writePersistedSelectedMemoId(memoId = "") {
      const value = String(memoId || "").trim();
      remoteSelectedMemoRunId = currentRunId();
      remoteSelectedMemoLoaded = true;
      if (remoteSelectedMemoId === value) {
        writeStoredSelectedMemoId(value);
        return;
      }
      remoteSelectedMemoId = value;
      writeStoredSelectedMemoId(value);
      const write = deps.fetchWithTimeout || ((url, options) => deps.fetchJson?.(url, options, "Failed to save UI state"));
      write(deps.apiUrl("/api/ui-state"), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ last_selected_memo_id: value })
      }).catch(err => deps.consoleRef?.warn?.("Failed to save selected memo", err));
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

    function memoFilterLabel(filter) {
      const normalized = String(filter || "day");
      if (normalized === "day") return t("memo.filter_day", "Day");
      if (normalized === "all") return t("memo.filter_all", "All");
      return memoStatusLabel(normalized);
    }

    function isTerminalMemoStatus(status) {
      return terminalMemoStatuses.has(normalizeMemoStatus(status));
    }

    function taskStatusLabel(status) {
      const key = String(status || "").trim();
      return key ? t(`memo.task_status_${key}`, key) : "";
    }

    function getLunarFormatter() {
      if (lunarFormatterReady) return lunarFormatter;
      lunarFormatterReady = true;
      try {
        lunarFormatter = new Intl.DateTimeFormat("zh-CN-u-ca-chinese", { month: "long", day: "numeric" });
      } catch (_err) {
        lunarFormatter = null;
      }
      return lunarFormatter;
    }

    function lunarDateLabel(date) {
      const formatter = getLunarFormatter();
      if (!formatter || typeof formatter.formatToParts !== "function") return "";
      const parts = formatter.formatToParts(date);
      const month = parts.find(part => part.type === "month")?.value || "";
      const day = Number(parts.find(part => part.type === "day")?.value || 0);
      if (!month || !day) return "";
      return day === 1 ? month : (lunarDayNames[day] || "");
    }

    function allTasks() {
      const tasks = deps.allTasks?.();
      return Array.isArray(tasks) ? tasks : [];
    }

    function taskId(task) {
      return String(task?.id || "").trim();
    }

    function taskTitle(task) {
      return String(task?.title || "").trim();
    }

    function taskOptionLabel(task) {
      const id = taskId(task);
      const title = taskTitle(task);
      const status = taskStatusLabel(taskDisplayStatus(task));
      return [id, title && title !== id ? title : "", status].filter(Boolean).join(" · ");
    }

    function taskById(value) {
      const id = String(value || "").trim();
      return id ? allTasks().find(task => taskId(task) === id) || null : null;
    }

    function ensureDraftTaskLink(memo) {
      const memoId = String(memo?.id || "").trim();
      if (!memoId) {
        draftTaskLinkMemoId = "";
        draftTaskLinkId = "";
        return;
      }
      if (draftTaskLinkMemoId !== memoId) {
        draftTaskLinkMemoId = memoId;
        draftTaskLinkId = String(memo?.created_task_id || "").trim();
      }
    }

    function currentDraftTaskLinkId(memo) {
      const memoId = String(memo?.id || "").trim();
      if (!memoId) return "";
      return draftTaskLinkMemoId === memoId ? draftTaskLinkId : String(memo?.created_task_id || "").trim();
    }

    function memoWithDraftTaskLink(memo) {
      if (!memo) return null;
      const taskLinkId = currentDraftTaskLinkId(memo);
      if (!taskLinkId) {
        return { ...memo, created_task_id: "", created_task_title: "", created_task_status: "" };
      }
      const task = taskById(taskLinkId);
      const originalTaskId = String(memo.created_task_id || "").trim();
      return {
        ...memo,
        created_task_id: taskLinkId,
        created_task_title: task ? taskTitle(task) : (taskLinkId === originalTaskId ? memo.created_task_title || "" : ""),
        created_task_status: task ? taskDisplayStatus(task) : (taskLinkId === originalTaskId ? memo.created_task_status || "" : "")
      };
    }

    function linkedTaskSummary(memo) {
      if (!memo?.created_task_id) return t("memo.unlinked_task", "No task");
      const taskId = String(memo.created_task_id || "").trim();
      const title = String(memo.created_task_title || "").trim();
      const status = taskStatusLabel(memo.created_task_status);
      return [taskId, title && title !== taskId ? title : "", status].filter(Boolean).join(" · ");
    }

    function taskMatchesSearch(task, queryText) {
      if (!queryText) return true;
      const query = queryText.toLowerCase();
      return [taskId(task), taskTitle(task), taskDisplayStatus(task)]
        .some(value => String(value || "").toLowerCase().includes(query));
    }

    function taskMatchesFilter(task, filter) {
      const status = taskDisplayStatus(task);
      if (filter === "all") return true;
      if (filter === "running") return status === "running";
      if (filter === "completed") return status === "completed";
      return !task.hidden && !terminalTaskStatuses.has(status);
    }

    function taskPickerOptions(memo) {
      const linkedTaskId = String(memo?.created_task_id || "").trim();
      const query = taskPickerSearch.toLowerCase();
      const tasks = allTasks()
        .filter(task => taskId(task))
        .filter(task => taskMatchesFilter(task, taskPickerFilter))
        .filter(task => taskMatchesSearch(task, query))
        .slice(0, 50);
      if (linkedTaskId && !tasks.some(task => taskId(task) === linkedTaskId)) {
        const linkedTask = allTasks().find(task => taskId(task) === linkedTaskId) || {
          id: linkedTaskId,
          title: memo?.created_task_title || "",
          status: memo?.created_task_status || "missing"
        };
        return [linkedTask, ...tasks];
      }
      return tasks;
    }

    function linkedTaskLabel(memo) {
      if (!memo?.created_task_id) return t("memo.unlinked_task", "No task");
      const status = taskStatusLabel(memo.created_task_status);
      return status ? `${t("memo.linked_task", "Linked")} · ${status}` : t("memo.linked_task", "Linked");
    }

    function selectedMemo() {
      return memos.find(memo => memo.id === selectedMemoId) || null;
    }

    function setText(element, message = "") {
      if (element) element.textContent = message;
    }

    function setHidden(element, hidden) {
      if (element) element.hidden = Boolean(hidden);
    }

    function setDisabled(element, disabled) {
      if (element) element.disabled = Boolean(disabled);
    }

    function setState(message = "") {
      setText(elements.taskMemoStateEl, message);
    }

    function isPageMode() {
      return Boolean(elements.taskMemoDialogEl?.classList?.contains("task-memo-page"));
    }

    function writeStoredPageMode(active) {
      try {
        windowRef.localStorage?.setItem("aha.taskMemoView", active ? "memo" : "task");
      } catch (_err) {
        // localStorage can be unavailable in restricted browser modes.
      }
    }

    function syncPageModeUrl(active) {
      if (!isPageMode()) return;
      writeStoredPageMode(active);
      if (!windowRef.history?.replaceState) return;
      const url = new URL(windowRef.location.href);
      url.searchParams.set("view", active ? "memo" : "task");
      if (active) {
        url.searchParams.delete("selected_task_id");
        url.searchParams.delete("task_id");
      }
      windowRef.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    }

    function schedulePageModeUrlSync(active) {
      syncPageModeUrl(active);
      if (!isPageMode() || !windowRef.setTimeout) return;
      if (pageModeSyncTimer) windowRef.clearTimeout?.(pageModeSyncTimer);
      pageModeSyncTimer = windowRef.setTimeout(() => {
        pageModeSyncTimer = 0;
        syncPageModeUrl(active);
      }, 0);
    }

    function setPageMode(active) {
      if (!isPageMode()) return;
      documentRef?.body?.classList?.toggle("task-memo-home", Boolean(active));
      schedulePageModeUrlSync(Boolean(active));
    }

    function updateViewToggle() {
      if (!elements.openTaskMemosEl || !isPageMode()) return;
      elements.openTaskMemosEl.setAttribute("aria-pressed", String(isOpen()));
    }

    function reportError(err) {
      const message = err?.message || String(err);
      setState(message);
      deps.alert?.(message);
    }

    function openDialog() {
      if (!deps.currentRunId?.()) {
        deps.alert?.(t("task.create_run_first", "Create a run before adding a task."));
        return;
      }
      if (isPageMode()) {
        elements.taskMemoDialogEl?.setAttribute("open", "");
        setPageMode(true);
        updateViewToggle();
        deps.closeMobileSheets?.();
        void loadMemos().catch(reportError);
        return;
      }
      try {
        if (typeof elements.taskMemoDialogEl?.showModal === "function") {
          if (!elements.taskMemoDialogEl.open) elements.taskMemoDialogEl.showModal();
        } else {
          elements.taskMemoDialogEl?.setAttribute("open", "");
        }
      } catch (_err) {
        elements.taskMemoDialogEl?.setAttribute("open", "");
      }
      void loadMemos().catch(reportError);
    }

    function closeDialog() {
      if (isPageMode()) {
        elements.taskMemoDialogEl?.removeAttribute("open");
        setPageMode(false);
        updateViewToggle();
        return;
      }
      if (typeof elements.taskMemoDialogEl?.close === "function" && elements.taskMemoDialogEl.open) {
        elements.taskMemoDialogEl.close();
      } else {
        elements.taskMemoDialogEl?.removeAttribute("open");
      }
    }

    function isOpen() {
      return Boolean(elements.taskMemoDialogEl?.open || elements.taskMemoDialogEl?.hasAttribute?.("open"));
    }

    async function loadMemos() {
      const payload = await deps.fetchJson(deps.apiUrl("/api/task-memos"), {}, "Failed to load task memos");
      const runId = currentRunId();
      if (selectedMemoRunId !== runId) {
        selectedMemoRunId = runId;
        selectedMemoId = "";
        editorMode = "empty";
        draftTaskLinkMemoId = "";
        draftTaskLinkId = "";
      }
      memos = Array.isArray(payload?.memos) ? payload.memos : [];
      if (selectedMemoId && !memos.some(memo => memo.id === selectedMemoId)) {
        selectedMemoId = "";
        writePersistedSelectedMemoId("");
        if (editorMode === "edit") editorMode = "empty";
      }
      render();
      return memos;
    }

    async function refreshIfOpen() {
      if (!isOpen()) return memos;
      return await loadMemos();
    }

    function renderCalendar() {
      if (!elements.taskMemoCalendarEl || !documentRef) return;
      const currentMonth = monthValue();
      setText(elements.taskMemoCurrentMonthEl, currentMonth);
      const [year, month] = currentMonth.split("-").map(Number);
      const first = new Date(year, month - 1, 1);
      const daysInMonth = new Date(year, month, 0).getDate();
      const weekCount = Math.max(5, Math.ceil((first.getDay() + daysInMonth) / 7));
      const start = new Date(first);
      start.setDate(first.getDate() - first.getDay());
      const counts = new Map();
      for (const memo of memos) {
        const date = memo.scheduled_date || "";
        if (!date) continue;
        const count = counts.get(date) || { total: 0, open: 0 };
        count.total += 1;
        if (!isTerminalMemoStatus(memo.status)) count.open += 1;
        counts.set(date, count);
      }
      elements.taskMemoCalendarEl.innerHTML = "";
      elements.taskMemoCalendarEl.classList.toggle("weeks-5", weekCount === 5);
      elements.taskMemoCalendarEl.classList.toggle("weeks-6", weekCount === 6);
      for (let index = 0; index < weekCount * 7; index += 1) {
        const date = new Date(start);
        date.setDate(start.getDate() + index);
        const value = isoDate(date);
        const button = documentRef.createElement("button");
        button.type = "button";
        const count = counts.get(value) || { total: 0, open: 0 };
        button.className = [
          "task-memo-day",
          value === selectedDate ? "active" : "",
          count.total && count.open ? "has-open" : "",
          count.total && !count.open ? "all-done" : "",
          date.getMonth() === month - 1 ? "" : "outside"
        ].filter(Boolean).join(" ");
        button.dataset.memoDate = value;
        const dayNumber = documentRef.createElement("span");
        dayNumber.className = "task-memo-day-number";
        dayNumber.textContent = String(date.getDate());
        const lunar = documentRef.createElement("span");
        lunar.className = "task-memo-day-lunar";
        lunar.textContent = lunarDateLabel(date);
        button.appendChild(dayNumber);
        button.appendChild(lunar);
        if (count.total) {
          const badge = documentRef.createElement("span");
          badge.className = "task-memo-day-count";
          badge.textContent = String(count.total);
          button.appendChild(badge);
        }
        elements.taskMemoCalendarEl.appendChild(button);
      }
    }

    function memoButton(memo, options = {}) {
      const item = documentRef.createElement("div");
      const status = normalizeMemoStatus(memo.status);
      item.className = `task-memo-item task-memo-status-${status}${memo.id === selectedMemoId ? " active" : ""}${isTerminalMemoStatus(status) ? " done" : ""}`;
      const button = documentRef.createElement("button");
      button.type = "button";
      button.className = "task-memo-item-main";
      button.dataset.memoId = memo.id;
      const title = documentRef.createElement("span");
      title.className = "task-memo-item-title";
      title.textContent = memo.title || t("task.untitled_draft", "Untitled draft");
      const badges = documentRef.createElement("span");
      badges.className = "task-memo-item-badges";
      if (options.showDate && memo.scheduled_date) {
        const date = documentRef.createElement("span");
        date.className = "task-memo-item-date";
        date.textContent = memo.scheduled_date;
        badges.appendChild(date);
      }
      const statusBadge = documentRef.createElement("span");
      statusBadge.className = `task-memo-item-status task-memo-status-${status}`;
      statusBadge.textContent = memoStatusLabel(status);
      badges.appendChild(statusBadge);
      const linked = documentRef.createElement("span");
      const taskStatusClass = String(memo.created_task_status || "none").replace(/[^a-z0-9_-]/gi, "-").toLowerCase();
      linked.className = `task-memo-item-link task-memo-task-status-${taskStatusClass}${memo.created_task_id ? " linked" : ""}`;
      linked.textContent = linkedTaskLabel(memo);
      button.appendChild(title);
      badges.appendChild(linked);
      button.appendChild(badges);
      item.appendChild(button);
      return item;
    }

    function sortMemoList(items, showDate) {
      if (!showDate) return items;
      return [...items].sort((left, right) => {
        const dateOrder = String(right.scheduled_date || "").localeCompare(String(left.scheduled_date || ""));
        if (dateOrder) return dateOrder;
        return String(right.updated_at || "").localeCompare(String(left.updated_at || ""));
      });
    }

    function selectedDayMemos() {
      return memos.filter(memo => (memo.scheduled_date || "") === selectedDate);
    }

    function historicalOpenMemos() {
      return sortMemoList(
        memos.filter(memo => {
          const date = String(memo.scheduled_date || "");
          return date && date < selectedDate && !isTerminalMemoStatus(normalizeMemoStatus(memo.status));
        }),
        true
      );
    }

    function memoFilterCount(filter) {
      if (filter === "day") return selectedDayMemos().length + historicalOpenMemos().length;
      if (filter === "all") return memos.length;
      return memos.filter(memo => normalizeMemoStatus(memo.status) === filter).length;
    }

    function renderMemoFilters() {
      if (!elements.taskMemoFilterEl || !documentRef) return;
      elements.taskMemoFilterEl.innerHTML = "";
      for (const filter of memoFilters) {
        const button = documentRef.createElement("button");
        button.type = "button";
        button.className = `task-list-filter${memoFilter === filter ? " active" : ""}`;
        button.dataset.memoFilter = filter;
        button.setAttribute("aria-pressed", String(memoFilter === filter));
        const label = documentRef.createElement("span");
        label.textContent = memoFilterLabel(filter);
        const separator = documentRef.createElement("span");
        separator.className = "task-memo-filter-separator";
        separator.textContent = "·";
        const count = documentRef.createElement("code");
        count.textContent = String(memoFilterCount(filter));
        button.appendChild(label);
        button.appendChild(separator);
        button.appendChild(count);
        elements.taskMemoFilterEl.appendChild(button);
      }
    }

    function filteredMemos() {
      if (memoFilter === "day") {
        return {
          sections: [
            { title: t("memo.section_selected_day", "Selected day"), items: selectedDayMemos(), showDate: false },
            { title: t("memo.section_history_open", "History open"), items: historicalOpenMemos(), showDate: true }
          ]
        };
      }
      if (memoFilter === "all") {
        return { items: sortMemoList(memos, true), showDate: true };
      }
      return {
        items: sortMemoList(memos.filter(memo => normalizeMemoStatus(memo.status) === memoFilter), true),
        showDate: true
      };
    }

    function renderMemoSection(section) {
      if (!section?.items?.length || !documentRef) return null;
      const wrapper = documentRef.createElement("section");
      wrapper.className = "task-memo-list-section";
      const title = documentRef.createElement("div");
      title.className = "task-memo-list-section-title";
      const label = documentRef.createElement("span");
      label.textContent = section.title;
      const count = documentRef.createElement("code");
      count.textContent = String(section.items.length);
      title.appendChild(label);
      title.appendChild(count);
      wrapper.appendChild(title);
      section.items.forEach(memo => wrapper.appendChild(memoButton(memo, { showDate: section.showDate })));
      return wrapper;
    }

    function renderList() {
      if (!elements.taskMemoListEl || !documentRef) return;
      elements.taskMemoListEl.innerHTML = "";
      const { items, showDate, sections } = filteredMemos();
      if (sections) {
        const renderedSections = sections.map(renderMemoSection).filter(Boolean);
        if (!renderedSections.length) {
          elements.taskMemoListEl.innerHTML = `<div class="empty compact">${escapeHtml(t("memo.empty", "No memos."))}</div>`;
          return;
        }
        renderedSections.forEach(section => elements.taskMemoListEl.appendChild(section));
        return;
      }
      if (!items.length) {
        elements.taskMemoListEl.innerHTML = `<div class="empty compact">${escapeHtml(t("memo.empty", "No memos."))}</div>`;
        return;
      }
      items.forEach(memo => elements.taskMemoListEl.appendChild(memoButton(memo, { showDate })));
    }

    function fillEditor(values = {}) {
      if (elements.taskMemoEditTitleEl) elements.taskMemoEditTitleEl.value = values.title || "";
      if (elements.taskMemoEditDescriptionEl) elements.taskMemoEditDescriptionEl.value = values.description || "";
      if (elements.taskMemoEditStatusEl) elements.taskMemoEditStatusEl.value = normalizeMemoStatus(values.status);
      if (elements.taskMemoEditDateEl) elements.taskMemoEditDateEl.value = memoDateValue(values.scheduled_date || selectedDate);
    }

    function renderStatusOptions(status, disabled) {
      const optionsEl = elements.taskMemoStatusOptionsEl;
      if (!optionsEl || !documentRef) return;
      const activeStatus = normalizeMemoStatus(status);
      optionsEl.innerHTML = "";
      for (const option of memoStatuses) {
        const button = documentRef.createElement("button");
        button.type = "button";
        button.className = `task-memo-status-option task-memo-status-${option}${activeStatus === option ? " active" : ""}`;
        button.dataset.memoStatusOption = option;
        button.disabled = Boolean(disabled);
        button.setAttribute("aria-pressed", String(activeStatus === option));
        button.textContent = memoStatusLabel(option);
        optionsEl.appendChild(button);
      }
      if (elements.taskMemoEditStatusEl) {
        elements.taskMemoEditStatusEl.value = activeStatus;
        elements.taskMemoEditStatusEl.disabled = Boolean(disabled);
      }
    }

    function editorFieldsFromMemo(memo = {}) {
      return {
        title: String(memo.title || ""),
        description: String(memo.description || ""),
        status: normalizeMemoStatus(memo.status),
        scheduled_date: memoDateValue(memo.scheduled_date || selectedDate),
        created_task_id: String(memo.created_task_id || "").trim()
      };
    }

    function readCurrentEditorFields() {
      const memo = editorMode === "edit" ? selectedMemo() : null;
      return {
        title: String(elements.taskMemoEditTitleEl?.value || ""),
        description: String(elements.taskMemoEditDescriptionEl?.value || ""),
        status: normalizeMemoStatus(elements.taskMemoEditStatusEl?.value || "todo"),
        scheduled_date: memoDateValue(elements.taskMemoEditDateEl?.value || selectedDate),
        created_task_id: editorMode === "edit" ? currentDraftTaskLinkId(memo) : ""
      };
    }

    function editorFieldsHaveContent(fields) {
      return Boolean(String(fields?.title || "").trim() || String(fields?.description || "").trim());
    }

    function editorCanSave() {
      if (editorMode === "empty") return false;
      const current = readCurrentEditorFields();
      if (editorMode === "create") return editorFieldsHaveContent(current);
      const memo = selectedMemo();
      if (!memo) return false;
      const baseline = editorFieldsFromMemo(memo);
      return current.title !== baseline.title
        || current.description !== baseline.description
        || current.status !== baseline.status
        || current.scheduled_date !== baseline.scheduled_date
        || current.created_task_id !== baseline.created_task_id;
    }

    function updateSaveState() {
      const isEmpty = editorMode === "empty";
      const canSave = editorCanSave();
      setHidden(elements.taskMemoSaveEl, isEmpty);
      setDisabled(elements.taskMemoSaveEl, !canSave);
      elements.taskMemoSaveEl?.classList?.toggle("task-memo-save-dirty", canSave);
    }

    function renderTaskLinkPicker(memo, isEdit) {
      if (!isEdit) {
        taskPickerOpen = false;
        setHidden(elements.taskMemoTaskLinkFieldEl, true);
        setHidden(elements.taskMemoTaskPickerToggleEl, true);
        setHidden(elements.taskMemoTaskLinkClearEl, true);
        return;
      }
      const draftMemo = memoWithDraftTaskLink(memo);
      const linked = Boolean(draftMemo?.created_task_id);
      setHidden(elements.taskMemoTaskLinkFieldEl, false);
      setHidden(elements.taskMemoTaskPickerToggleEl, false);
      setText(elements.taskMemoTaskLinkSummaryEl, linkedTaskSummary(draftMemo));
      if (elements.taskMemoTaskLinkSummaryEl) elements.taskMemoTaskLinkSummaryEl.title = linkedTaskSummary(draftMemo);
      setText(elements.taskMemoTaskPickerToggleEl, linked ? t("memo.task_change", "Change task") : t("memo.task_choose", "Choose task"));
      if (elements.taskMemoTaskLinkClearEl) elements.taskMemoTaskLinkClearEl.hidden = !linked;
      if (elements.taskMemoTaskPickerEl) elements.taskMemoTaskPickerEl.hidden = !taskPickerOpen;
      if (elements.taskMemoTaskPickerSearchEl && elements.taskMemoTaskPickerSearchEl.value !== taskPickerSearch) {
        elements.taskMemoTaskPickerSearchEl.value = taskPickerSearch;
      }
      if (elements.taskMemoTaskPickerFilterEl && elements.taskMemoTaskPickerFilterEl.value !== taskPickerFilter) {
        elements.taskMemoTaskPickerFilterEl.value = taskPickerFilter;
      }
      renderTaskPickerList(draftMemo);
    }

    function renderTaskPickerList(memo) {
      const listEl = elements.taskMemoTaskPickerListEl;
      if (!listEl || !documentRef) return;
      listEl.innerHTML = "";
      if (!taskPickerOpen) return;
      const tasks = taskPickerOptions(memo);
      if (!tasks.length) {
        listEl.innerHTML = `<div class="empty compact">${escapeHtml(t("memo.no_tasks", "No tasks."))}</div>`;
        return;
      }
      const linkedTaskId = String(memo?.created_task_id || "").trim();
      for (const task of tasks) {
        const button = documentRef.createElement("button");
        button.type = "button";
        button.className = `entity-picker-item${taskId(task) === linkedTaskId ? " active" : ""}`;
        button.dataset.taskLinkOption = taskId(task);
        const title = documentRef.createElement("span");
        title.className = "entity-picker-title";
        title.textContent = taskOptionLabel(task);
        button.appendChild(title);
        listEl.appendChild(button);
      }
    }

    function clearEditorForCreate() {
      fillEditor({ title: "", description: "", scheduled_date: selectedDate });
    }

    function renderEditor() {
      let memo = editorMode === "edit" ? selectedMemo() : null;
      if (editorMode === "edit" && !memo) {
        selectedMemoId = "";
        editorMode = "empty";
        memo = null;
      }
      const isEdit = Boolean(memo);
      const isCreate = editorMode === "create";
      const isEmpty = !isCreate && !isEdit;
      if (isEdit) {
        ensureDraftTaskLink(memo);
        fillEditor(memo);
      }
      if (isEmpty) {
        draftTaskLinkMemoId = "";
        draftTaskLinkId = "";
        fillEditor({});
      }
      if (isCreate) fillEditor({
        title: elements.taskMemoEditTitleEl?.value || "",
        description: elements.taskMemoEditDescriptionEl?.value || "",
        status: elements.taskMemoEditStatusEl?.value || "todo",
        scheduled_date: elements.taskMemoEditDateEl?.value || selectedDate
      });
      const editorStatus = isEdit ? memo?.status : (isCreate ? elements.taskMemoEditStatusEl?.value : "todo");
      renderStatusOptions(editorStatus, isEmpty);
      renderTaskLinkPicker(memo, isEdit);
      setText(elements.taskMemoEditorTitleEl, isEdit
        ? t("memo.editor_edit", "Edit memo")
        : isCreate
          ? t("memo.editor_create", "New memo")
          : t("memo.editor_empty", "Select or create a memo"));
      setText(elements.taskMemoEditorHintEl, isEdit
        ? t("memo.editor_edit_hint", "Update this memo or turn it into a task.")
        : isCreate
          ? t("memo.editor_create_hint", "Fill in a future task idea, then save it as a memo.")
          : t("memo.editor_empty_hint", "Choose a memo from the list, or create a new one from the left column."));
      [elements.taskMemoEditTitleEl, elements.taskMemoEditDateEl, elements.taskMemoEditDescriptionEl]
        .forEach(element => setDisabled(element, isEmpty));
      setHidden(elements.taskMemoCancelEl, !isCreate);
      setHidden(elements.taskMemoDeleteEl, !isEdit);
      setHidden(elements.taskMemoConvertEl, !isEdit);
      setDisabled(elements.taskMemoDeleteEl, !isEdit);
      setDisabled(elements.taskMemoConvertEl, !isEdit);
      setText(elements.taskMemoSaveEl, isCreate
        ? t("memo.save_new", "Save New Memo")
        : t("memo.save_changes", "Save Changes"));
      setText(elements.taskMemoConvertEl, memoWithDraftTaskLink(memo)?.created_task_id
        ? t("memo.jump_task", "Jump to Task")
        : t("memo.convert", "Create Task"));
      updateSaveState();
    }

    function render() {
      renderCalendar();
      renderMemoFilters();
      renderList();
      renderEditor();
    }

    function readEditorPayload() {
      const payload = {
        title: elements.taskMemoEditTitleEl?.value || "",
        description: elements.taskMemoEditDescriptionEl?.value || "",
        status: normalizeMemoStatus(elements.taskMemoEditStatusEl?.value || "todo"),
        scheduled_date: memoDateValue(elements.taskMemoEditDateEl?.value || selectedDate)
      };
      if (editorMode === "edit") {
        payload.created_task_id = currentDraftTaskLinkId(selectedMemo());
      }
      return payload;
    }

    function enterCreateMode() {
      selectedMemoId = "";
      editorMode = "create";
      draftTaskLinkMemoId = "";
      draftTaskLinkId = "";
      clearEditorForCreate();
      render();
      setState("");
    }

    function enterEmptyMode() {
      selectedMemoId = "";
      editorMode = "empty";
      draftTaskLinkMemoId = "";
      draftTaskLinkId = "";
      render();
      setState("");
    }

    async function saveEditor() {
      const memo = editorMode === "edit" ? selectedMemo() : null;
      const creating = editorMode === "create";
      if (!creating && !memo) return;
      if (!editorCanSave()) {
        updateSaveState();
        return;
      }
      const payload = readEditorPayload();
      const url = creating ? deps.apiUrl("/api/task-memos") : deps.apiUrl(`/api/task-memos/${encodeURIComponent(memo.id)}`);
      const method = creating ? "POST" : "PATCH";
      const response = await deps.fetchJson(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      }, "Failed to save memo");
      selectedDate = response?.memo?.scheduled_date || selectedDate;
      if (creating) {
        selectedMemoId = "";
        editorMode = "create";
        clearEditorForCreate();
        setState(t("memo.created", "Memo saved. Ready for the next one."));
      } else {
        selectedMemoId = response?.memo?.id || selectedMemoId;
        editorMode = "edit";
        draftTaskLinkMemoId = response?.memo?.id || "";
        draftTaskLinkId = String(response?.memo?.created_task_id || "").trim();
        writePersistedSelectedMemoId(selectedMemoId);
        setState(t("memo.saved", "Memo saved."));
      }
      await loadMemos();
    }

    function linkSelectedTask(taskId) {
      const memo = editorMode === "edit" ? selectedMemo() : null;
      if (!memo) return;
      const nextTaskId = String(taskId || "").trim();
      ensureDraftTaskLink(memo);
      const currentTaskId = currentDraftTaskLinkId(memo);
      if (nextTaskId === currentTaskId) return;
      draftTaskLinkMemoId = String(memo.id || "").trim();
      draftTaskLinkId = nextTaskId;
      taskPickerOpen = false;
      renderTaskLinkPicker(memo, true);
      updateSaveState();
      setState(nextTaskId ? t("memo.link_pending", "Task link pending save.") : t("memo.link_clear_pending", "Task link removal pending save."));
    }

    async function deleteSelected() {
      const memo = editorMode === "edit" ? selectedMemo() : null;
      if (!memo) return;
      const confirmed = await (deps.confirmDialogAction
        ? deps.confirmDialogAction({
            title: t("memo.delete_confirm_title", "Delete memo?"),
            message: t("memo.delete_confirm_message", "Delete this memo. This cannot be undone."),
            details: [[t("task.title", "Title"), memo.title || t("task.untitled_draft", "Untitled draft")]],
            confirmLabel: t("memo.delete", "Delete"),
            danger: true
          })
        : Promise.resolve(windowRef.confirm(t("memo.delete_confirm_message", "Delete this memo. This cannot be undone."))));
      if (!confirmed) return;
      await deps.fetchJson(deps.apiUrl(`/api/task-memos/${encodeURIComponent(memo.id)}`), { method: "DELETE" }, "Failed to delete memo");
      selectedMemoId = "";
      writePersistedSelectedMemoId("");
      editorMode = "empty";
      setState("");
      await loadMemos();
    }

    async function convertSelected() {
      const memo = editorMode === "edit" ? selectedMemo() : null;
      if (!memo) return;
      const draftMemo = memoWithDraftTaskLink(memo);
      if (draftMemo?.created_task_id) {
        const taskId = String(draftMemo.created_task_id || "").trim();
        if (!taskId) return;
        deps.setSelectedTaskId?.(taskId);
        deps.writeStoredSelectedTaskId?.(taskId);
        await deps.selectTask?.(taskId);
        deps.closeMobileSheets?.();
        closeDialog();
        return;
      }
      deps.applyTaskMemoToForm?.(memo);
      deps.openTaskCreateDialog?.();
    }

    function shiftMonth(delta) {
      const [year, month] = monthValue().split("-").map(Number);
      const next = new Date(year, month - 1 + delta, 1);
      selectedDate = isoDate(next);
      render();
    }

    function goToday() {
      selectedDate = isoDate(new Date());
      render();
    }

    function bind() {
      elements.openTaskMemosEl?.addEventListener("click", () => {
        if (isPageMode() && isOpen()) {
          closeDialog();
          return;
        }
        openDialog();
      });
      elements.closeTaskMemosEl?.addEventListener("click", closeDialog);
      elements.taskMemoDialogEl?.addEventListener("click", event => {
        if (isPageMode()) return;
        if (event.target === elements.taskMemoDialogEl) closeDialog();
      });
      elements.taskMemoPrevYearEl?.addEventListener("click", () => shiftMonth(-12));
      elements.taskMemoPrevMonthEl?.addEventListener("click", () => shiftMonth(-1));
      elements.taskMemoNextMonthEl?.addEventListener("click", () => shiftMonth(1));
      elements.taskMemoNextYearEl?.addEventListener("click", () => shiftMonth(12));
      elements.taskMemoCurrentMonthEl?.addEventListener("click", goToday);
      elements.taskMemoCalendarEl?.addEventListener("click", event => {
        const button = event.target instanceof Element ? event.target.closest("[data-memo-date]") : null;
        if (!button) return;
        selectedDate = button.dataset.memoDate || selectedDate;
        memoFilter = "day";
        render();
      });
      const selectFromClick = event => {
        const button = event.target instanceof Element ? event.target.closest("[data-memo-id]") : null;
        if (!button) return;
        selectedMemoId = button.dataset.memoId || "";
        writePersistedSelectedMemoId(selectedMemoId);
        editorMode = selectedMemoId ? "edit" : "empty";
        render();
        setState("");
      };
      elements.taskMemoFilterEl?.addEventListener("click", event => {
        const button = event.target instanceof Element ? event.target.closest("[data-memo-filter]") : null;
        if (!button) return;
        memoFilter = memoFilters.includes(button.dataset.memoFilter || "") ? button.dataset.memoFilter : "day";
        render();
      });
      elements.taskMemoListEl?.addEventListener("click", selectFromClick);
      elements.taskMemoFormEl?.addEventListener("submit", event => {
        event.preventDefault();
        void saveEditor().catch(reportError);
      });
      elements.taskMemoStatusOptionsEl?.addEventListener("click", event => {
        const button = event.target instanceof Element ? event.target.closest("[data-memo-status-option]") : null;
        if (!button || button.disabled) return;
        const status = normalizeMemoStatus(button.dataset.memoStatusOption || "todo");
        if (elements.taskMemoEditStatusEl) elements.taskMemoEditStatusEl.value = status;
        renderStatusOptions(status, false);
        updateSaveState();
      });
      [elements.taskMemoEditTitleEl, elements.taskMemoEditDateEl, elements.taskMemoEditDescriptionEl]
        .forEach(element => element?.addEventListener("input", updateSaveState));
      elements.taskMemoNewEl?.addEventListener("click", enterCreateMode);
      elements.taskMemoCancelEl?.addEventListener("click", enterEmptyMode);
      elements.taskMemoDeleteEl?.addEventListener("click", () => void deleteSelected().catch(reportError));
      elements.taskMemoConvertEl?.addEventListener("click", () => void convertSelected().catch(reportError));
      elements.taskMemoTaskPickerToggleEl?.addEventListener("click", () => {
        taskPickerOpen = !taskPickerOpen;
        renderTaskLinkPicker(selectedMemo(), editorMode === "edit");
      });
      elements.taskMemoTaskLinkClearEl?.addEventListener("click", () => linkSelectedTask(""));
      elements.taskMemoTaskPickerSearchEl?.addEventListener("input", () => {
        taskPickerSearch = String(elements.taskMemoTaskPickerSearchEl?.value || "").trim();
        renderTaskLinkPicker(selectedMemo(), editorMode === "edit");
      });
      elements.taskMemoTaskPickerFilterEl?.addEventListener("change", () => {
        taskPickerFilter = String(elements.taskMemoTaskPickerFilterEl?.value || "active");
        renderTaskLinkPicker(selectedMemo(), editorMode === "edit");
      });
      elements.taskMemoTaskPickerListEl?.addEventListener("click", event => {
        const option = event.target instanceof Element ? event.target.closest("[data-task-link-option]") : null;
        if (!option) return;
        linkSelectedTask(option.dataset.taskLinkOption || "");
      });
      if (isPageMode() && isOpen()) {
        if (deps.initialHomeActive === false) {
          closeDialog();
          updateViewToggle();
          return;
        }
        setPageMode(true);
        updateViewToggle();
        if (deps.currentRunId?.()) void loadMemos().catch(reportError);
      }
      updateViewToggle();
    }

    return Object.freeze({ bind, loadMemos, openDialog, closeDialog, refreshIfOpen });
  }

  window.AHATaskMemoController = Object.freeze({ createTaskMemoController });
})();
