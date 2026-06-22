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
    let draftTaskLinkDirty = false;
    let memoFilter = "day";
    let memoCalendarCollapsed = true;
    let remoteSelectedMemoRunId = "";
    let remoteSelectedMemoLoaded = false;
    let remoteSelectedMemoId = "";
    let taskPickerOpen = false;
    let taskPickerSearch = "";
    let taskPickerFilter = "active";
    let taskPickerTasks = [];
    let taskPickerRunId = "";
    let taskPickerLoaded = false;
    let taskPickerLoading = false;
    let taskPickerError = "";
    let taskPickerRequestPromise = null;
    let taskPickerRequestSeq = 0;
    let pageModeSyncTimer = 0;
    let lunarFormatter = null;
    let lunarFormatterReady = false;
    const terminalTaskStatuses = new Set(["completed", "failed", "blocked"]);
    const memoStatuses = ["todo", "doing", "done", "closed"];
    const memoFilters = ["day", ...memoStatuses, "all"];
    const terminalMemoStatuses = new Set(["done", "closed"]);
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
    const lunarDayNames = [
      "",
      "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
      "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
      "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十"
    ];

    function t(key, fallback = "") {
      return windowRef.AHAI18n?.t?.(key, fallback) || fallback;
    }

    function promptForAuth(err) {
      if (!deps.isAuthRequiredError?.(err)) return false;
      deps.renderLoginState?.(t("auth.session_expired", "Login expired. Enter the token again."), true);
      return true;
    }

    const memoMarkdownTools = (deps.taskMemoMarkdown || windowRef.AHATaskMemoMarkdown)?.createTaskMemoMarkdownTools?.({
      windowRef,
      documentRef,
      elements,
      apiUrl: deps.apiUrl,
      fetchJson: deps.fetchJson,
      textareaImagePaste: deps.textareaImagePaste || windowRef.AHATextareaImagePaste,
      consoleRef: deps.consoleRef,
      t,
      setState,
      reportError,
      updateSaveState
    }) || null;

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

    function memoOptionalDateValue(value = "") {
      const text = String(value || "").trim();
      return /^\d{4}-\d{2}-\d{2}$/.test(text) ? text : "";
    }

    function dateFromIsoDate(value) {
      const text = memoOptionalDateValue(value);
      if (!text) return null;
      const [year, month, day] = text.split("-").map(Number);
      return new Date(year, month - 1, day);
    }

    function nextIsoDate(value) {
      const date = dateFromIsoDate(value);
      if (!date) return "";
      date.setDate(date.getDate() + 1);
      return isoDate(date);
    }

    function memoEndDateValue(value = "", scheduledDate = selectedDate) {
      const start = memoOptionalDateValue(scheduledDate);
      const end = memoOptionalDateValue(value);
      return start && end && end >= start ? end : "";
    }

    function memoTerminalDateValue(value = "", scheduledDate = selectedDate) {
      const start = memoOptionalDateValue(scheduledDate);
      const terminalDate = memoOptionalDateValue(value);
      if (!terminalDate) return "";
      return start && terminalDate < start ? "" : terminalDate;
    }

    function memoDefaultTerminalDate(scheduledDate = selectedDate) {
      const start = memoOptionalDateValue(scheduledDate);
      const today = isoDate(new Date());
      return start && today < start ? start : today;
    }

    function memoTimestampDate(value = "") {
      const match = String(value || "").trim().match(/^(\d{4}-\d{2}-\d{2})/);
      return match ? memoOptionalDateValue(match[1]) : "";
    }

    function memoRangeEndDate(memo = {}) {
      const start = memoOptionalDateValue(memo.scheduled_date);
      return memoEndDateValue(memo.end_date, start) || start;
    }

    function memoDateRangeLabel(memo = {}) {
      const start = memoOptionalDateValue(memo.scheduled_date);
      if (!start) return "";
      const end = memoEndDateValue(memo.end_date, start);
      return end ? `${start} ~ ${end}` : start;
    }

    function memoDisplaysOnDate(memo = {}, dateText = selectedDate) {
      return Boolean(memoCalendarEntryForDate(memo, dateText));
    }

    function memoCalendarEntryForDate(memo = {}, dateText = selectedDate) {
      const target = memoOptionalDateValue(dateText);
      if (!target) return null;
      return memoCalendarEntries(memo).find(info => info.date === target) || null;
    }

    function memoTerminalInfo(memo = {}) {
      const status = normalizeMemoStatus(memo.status);
      const end = memoRangeEndDate(memo);
      if (status === "done") {
        const completedDate = memoTimestampDate(memo.completed_at) || memoTimestampDate(memo.updated_at) || end;
        if (completedDate && end && completedDate > end) return { date: end, overdue: true };
        return { date: completedDate, overdue: false };
      }
      if (status === "closed") {
        const closedDate = memoTimestampDate(memo.closed_at) || memoTimestampDate(memo.updated_at) || end;
        return {
          date: closedDate && end && closedDate > end ? end : closedDate,
          overdue: false,
        };
      }
      return { date: "", overdue: false };
    }

    function memoTerminalDate(memo = {}) {
      return memoTerminalInfo(memo).date;
    }

    function memoTerminalInputDate(memo = {}, status = normalizeMemoStatus(memo.status)) {
      const start = memoOptionalDateValue(memo.scheduled_date);
      if (status === "done") {
        const completedDate = memoTimestampDate(memo.completed_at) || (memo.id ? memoTimestampDate(memo.updated_at) || memoRangeEndDate(memo) : "");
        return memoTerminalDateValue(completedDate, start);
      }
      if (status === "closed") {
        const closedDate = memoTimestampDate(memo.closed_at) || (memo.id ? memoTimestampDate(memo.updated_at) || memoRangeEndDate(memo) : "");
        return memoTerminalDateValue(closedDate, start);
      }
      return "";
    }

    function memoCalendarInfo(memo = {}, today = isoDate(new Date())) {
      const start = memoOptionalDateValue(memo.scheduled_date);
      if (!start) return { date: "", overdue: false };
      if (isTerminalMemoStatus(normalizeMemoStatus(memo.status))) {
        return memoTerminalInfo(memo);
      }
      const end = memoRangeEndDate(memo);
      if (today < start) return { date: start, overdue: false };
      if (today <= end) return { date: today, overdue: false };
      return { date: end, overdue: true };
    }

    function memoCalendarEntries(memo = {}, today = isoDate(new Date())) {
      const start = memoOptionalDateValue(memo.scheduled_date);
      if (!start) return [];
      const end = memoRangeEndDate(memo);
      const status = normalizeMemoStatus(memo.status);
      const terminal = isTerminalMemoStatus(status) ? memoTerminalInfo(memo) : null;
      const lastDate = terminal ? terminal.date : (today < start ? start : (today <= end ? today : end));
      const entries = [];
      for (let date = start; date && date <= lastDate && date <= end; date = nextIsoDate(date)) {
        const completed = Boolean(terminal && date === terminal.date);
        entries.push({
          date,
          completed,
          pending: Boolean(!terminal && date === lastDate),
          overdue: Boolean(terminal?.overdue && date === end) || (!terminal && today > end && date === end),
        });
      }
      if (!entries.length && terminal?.date) {
        entries.push({ date: terminal.date, completed: true, overdue: terminal.overdue });
      }
      return entries;
    }

    function memoCalendarDate(memo = {}, today = isoDate(new Date())) {
      return memoCalendarInfo(memo, today).date;
    }

    function selectedMemoStorageKey() {
      const runId = currentRunId();
      return runId ? `aha:selectedTaskMemo:${runId}` : "";
    }

    function currentRunId() {
      return String(deps.currentRunId?.() || "").trim();
    }

    function bootstrapMemoSummary() {
      const summary = deps.bootstrapData?.()?.memo_summary || null;
      if (!summary || typeof summary !== "object") return null;
      const runId = currentRunId();
      if (!runId || String(summary.run_id || "").trim() !== runId) return null;
      return summary;
    }

    function bootstrapSelectedMemoId() {
      return String(bootstrapMemoSummary()?.last_selected_memo_id || "").trim();
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
        if (promptForAuth(err)) return readStoredSelectedMemoId();
        deps.consoleRef?.warn?.("Failed to load memo UI state", err);
        remoteSelectedMemoId = "";
        return readStoredSelectedMemoId();
      }
    }

    async function preferredSelectedMemoId() {
      return bootstrapSelectedMemoId() || await readPersistedSelectedMemoId();
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

    function memoReportStatus(memo = {}) {
      const status = String(memo.report_status || "none").trim().toLowerCase().replace(/-/g, "_");
      return ["none", "generating", "ready", "failed"].includes(status) ? status : "none";
    }

    function isMemoReportGenerating(memo = {}) {
      return memoReportStatus(memo) === "generating";
    }

    function memoReportPayload(memo = {}) {
      return {
        status: memoReportStatus(memo),
        report: String(memo.completion_report || ""),
        error: String(memo.report_error || ""),
      };
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

    function taskPickerSourceTasks() {
      if (taskPickerRunId === currentRunId() && (taskPickerLoaded || taskPickerLoading || taskPickerError)) return taskPickerTasks;
      return allTasks();
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
      if (!id) return null;
      const cachedTask = taskPickerSourceTasks().find(task => taskId(task) === id);
      if (cachedTask) return cachedTask;
      if (taskPickerRunId === currentRunId() && (taskPickerLoaded || taskPickerLoading || taskPickerError)) return null;
      return allTasks().find(task => taskId(task) === id) || null;
    }

    function ensureDraftTaskLink(memo) {
      const memoId = String(memo?.id || "").trim();
      if (!memoId) {
        draftTaskLinkMemoId = "";
        draftTaskLinkId = "";
        draftTaskLinkDirty = false;
        return;
      }
      const memoTaskId = String(memo?.created_task_id || "").trim();
      if (draftTaskLinkMemoId !== memoId) {
        draftTaskLinkMemoId = memoId;
        draftTaskLinkId = memoTaskId;
        draftTaskLinkDirty = false;
        return;
      }
      if (!draftTaskLinkDirty && draftTaskLinkId !== memoTaskId) {
        draftTaskLinkId = memoTaskId;
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
      const tasks = taskPickerSourceTasks()
        .filter(task => taskId(task))
        .filter(task => taskMatchesFilter(task, taskPickerFilter))
        .filter(task => taskMatchesSearch(task, query))
        .slice(0, 50);
      if (linkedTaskId && !tasks.some(task => taskId(task) === linkedTaskId)) {
        const linkedTask = taskPickerSourceTasks().find(task => taskId(task) === linkedTaskId)
          || allTasks().find(task => taskId(task) === linkedTaskId)
          || {
          id: linkedTaskId,
          title: memo?.created_task_title || "",
          status: memo?.created_task_status || "missing"
        };
        return [linkedTask, ...tasks];
      }
      return tasks;
    }

    function taskPickerIncludeId(memo) {
      return String(currentDraftTaskLinkId(memo) || memo?.created_task_id || "").trim();
    }

    function prepareTaskPickerLoading() {
      const runId = currentRunId();
      if (!runId) return;
      if (taskPickerRunId !== runId) {
        taskPickerRunId = runId;
        taskPickerLoaded = false;
        taskPickerTasks = [];
      }
      if (!taskPickerLoaded) {
        taskPickerLoading = true;
        taskPickerError = "";
      }
    }

    async function loadTaskPickerOptions(memo, options = {}) {
      const runId = currentRunId();
      if (!runId) return [];
      if (!options.force && taskPickerLoaded && taskPickerRunId === runId) return taskPickerTasks;
      if (!options.force && taskPickerLoading && taskPickerRequestPromise) return taskPickerRequestPromise;
      const requestSeq = ++taskPickerRequestSeq;
      taskPickerRunId = runId;
      taskPickerLoading = true;
      taskPickerError = "";
      renderTaskPickerList(memoWithDraftTaskLink(memo) || memo);
      taskPickerRequestPromise = (async () => {
        const params = { filter: "all", limit: "500" };
        const includeId = taskPickerIncludeId(memo);
        if (includeId) params.include_id = includeId;
        const payload = await deps.fetchJson(deps.apiUrl("/api/task-options", params), {}, "Failed to load task options");
        if (requestSeq !== taskPickerRequestSeq) return taskPickerTasks;
        taskPickerTasks = Array.isArray(payload?.tasks) ? payload.tasks : [];
        taskPickerLoaded = true;
        return taskPickerTasks;
      })();
      try {
        return await taskPickerRequestPromise;
      } catch (err) {
        if (promptForAuth(err)) return taskPickerTasks;
        if (requestSeq === taskPickerRequestSeq) {
          taskPickerError = err?.message || String(err);
          deps.consoleRef?.warn?.("Failed to load memo task options", err);
        }
        return taskPickerTasks;
      } finally {
        if (requestSeq === taskPickerRequestSeq) {
          taskPickerLoading = false;
          taskPickerRequestPromise = null;
          renderTaskPickerList(memoWithDraftTaskLink(selectedMemo()) || selectedMemo());
        }
      }
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
      if (!element) return;
      const isDisabled = Boolean(disabled);
      if ("disabled" in element) element.disabled = isDisabled;
      element.classList?.toggle("is-disabled", isDisabled);
      element.setAttribute?.("aria-disabled", String(isDisabled));
    }

    function setState(message = "") {
      setText(elements.taskMemoStateEl, message);
    }

    function isPageMode() {
      return Boolean(elements.taskMemoDialogEl?.classList?.contains("task-memo-page"));
    }

    function writeStoredPageMode(active) {
      try {
        windowRef.localStorage?.setItem("aha.taskMemoViewExplicit", active ? "memo" : "task");
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

    function isKnowledgeMode() {
      return Boolean(documentRef?.body?.classList?.contains("knowledge-home"));
    }

    function isSettingsMode() {
      return Boolean(documentRef?.body?.classList?.contains("settings-home"));
    }

    function updateViewToggle() {
      if (!isPageMode()) return;
      const memoOpen = isOpen();
      const knowledgeOpen = isKnowledgeMode();
      const settingsOpen = isSettingsMode();
      elements.openTaskViewEl?.setAttribute("aria-pressed", String(!memoOpen && !knowledgeOpen && !settingsOpen));
      elements.openTaskMemosEl?.setAttribute("aria-pressed", String(memoOpen));
      elements.openKnowledgeBaseEl?.setAttribute("aria-pressed", String(knowledgeOpen));
      documentRef?.getElementById?.("session-toggle")?.setAttribute("aria-pressed", String(settingsOpen));
    }

    function closeKnowledgeView() {
      documentRef?.body?.classList?.remove("knowledge-home");
      const knowledgeHome = documentRef?.getElementById?.("knowledge-home");
      if (knowledgeHome) knowledgeHome.hidden = true;
    }

    function reportError(err) {
      if (promptForAuth(err)) return;
      const message = err?.message || String(err);
      setState(message);
      deps.alert?.(message);
    }

    function resetMemoRunState() {
      memos = [];
      selectedMemoRunId = "";
      selectedMemoId = "";
      editorMode = "empty";
      draftTaskLinkMemoId = "";
      draftTaskLinkId = "";
      draftTaskLinkDirty = false;
      taskPickerRunId = "";
      taskPickerLoaded = false;
      taskPickerTasks = [];
      taskPickerRequestPromise = null;
    }

    function renderMemoHomeWithoutRun() {
      resetMemoRunState();
      render();
      setState(t("memo.create_run_first", "Create a run to start using memos."));
    }

    function openDialog() {
      if (!deps.currentRunId?.()) {
        if (isPageMode()) {
          closeKnowledgeView();
          elements.taskMemoDialogEl?.setAttribute("open", "");
          setPageMode(true);
          updateViewToggle();
          deps.closeMobileSheets?.();
          renderMemoHomeWithoutRun();
          return;
        }
        deps.alert?.(t("memo.create_run_first", "Create a run to start using memos."));
        return;
      }
      if (isPageMode()) {
        closeKnowledgeView();
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
        if (isKnowledgeMode() || isSettingsMode()) {
          updateViewToggle();
          return;
        }
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
      if (!currentRunId()) {
        renderMemoHomeWithoutRun();
        return [];
      }
      const payload = await deps.fetchJson(deps.apiUrl("/api/task-memos"), {}, "Failed to load task memos");
      const runId = currentRunId();
      if (selectedMemoRunId !== runId) {
        selectedMemoRunId = runId;
        selectedMemoId = "";
        editorMode = "empty";
        draftTaskLinkMemoId = "";
        draftTaskLinkId = "";
        draftTaskLinkDirty = false;
        taskPickerRunId = "";
        taskPickerLoaded = false;
        taskPickerTasks = [];
        taskPickerRequestPromise = null;
      }
      memos = Array.isArray(payload?.memos) ? payload.memos : [];
      if (!selectedMemoId && editorMode === "empty") {
        const persistedMemoId = await preferredSelectedMemoId();
        if (persistedMemoId && memos.some(memo => memo.id === persistedMemoId)) {
          selectedMemoId = persistedMemoId;
          editorMode = "edit";
        }
      }
      if (selectedMemoId && !memos.some(memo => memo.id === selectedMemoId)) {
        selectedMemoId = "";
        writePersistedSelectedMemoId("");
        if (editorMode === "edit") editorMode = "empty";
        memoMarkdownTools?.setMode?.("preview", { focus: false });
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
      const selected = dateFromIsoDate(selectedDate) || new Date();
      const first = new Date(year, month - 1, 1);
      const daysInMonth = new Date(year, month, 0).getDate();
      const fullWeekCount = Math.max(5, Math.ceil((first.getDay() + daysInMonth) / 7));
      const weekCount = memoCalendarCollapsed ? 1 : fullWeekCount;
      const start = memoCalendarCollapsed ? new Date(selected) : new Date(first);
      start.setDate(start.getDate() - start.getDay());
      const counts = new Map();
      for (const memo of memos) {
        for (const info of memoCalendarEntries(memo)) {
          const date = info.date;
          if (!date) continue;
          const count = counts.get(date) || { total: 0, completed: 0, overdue: 0, openOverdue: 0 };
          count.total += 1;
          if (info.completed) count.completed += 1;
          if (info.overdue) {
            count.overdue += 1;
            if (!info.completed) count.openOverdue += 1;
          }
          counts.set(date, count);
        }
      }
      elements.taskMemoCalendarEl.innerHTML = "";
      elements.taskMemoCalendarEl.classList.toggle("collapsed", memoCalendarCollapsed);
      elements.taskMemoCalendarEl.classList.toggle("weeks-5", weekCount === 5);
      elements.taskMemoCalendarEl.classList.toggle("weeks-6", weekCount === 6);
      const collapseLabel = memoCalendarCollapsed
        ? t("memo.calendar_expand", "Expand calendar")
        : t("memo.calendar_collapse", "Collapse calendar");
      setText(elements.taskMemoCalendarCollapseEl, memoCalendarCollapsed ? "▾" : "▴");
      if (elements.taskMemoCalendarCollapseEl) {
        elements.taskMemoCalendarCollapseEl.title = collapseLabel;
        elements.taskMemoCalendarCollapseEl.setAttribute("aria-label", collapseLabel);
        elements.taskMemoCalendarCollapseEl.setAttribute("aria-expanded", String(!memoCalendarCollapsed));
      }
      for (let index = 0; index < weekCount * 7; index += 1) {
        const date = new Date(start);
        date.setDate(start.getDate() + index);
        const value = isoDate(date);
        const button = documentRef.createElement("button");
        button.type = "button";
        const count = counts.get(value) || { total: 0, completed: 0, overdue: 0, openOverdue: 0 };
        const progressState = count.openOverdue ? "overdue" : (count.overdue ? "late" : "on-track");
        const riskLabel = count.openOverdue
          ? t("memo.calendar_overdue_open", "overdue pending")
          : count.overdue
            ? t("memo.calendar_late_done", "late done")
            : "";
        const dayLabel = count.total
          ? `${value} ${t("memo.calendar_progress", "memo progress")} ${count.completed}/${count.total}${riskLabel ? ` · ${riskLabel}` : ""}`
          : value;
        button.className = [
          "task-memo-day",
          value === selectedDate ? "active" : "",
          count.total ? `progress-${progressState}` : "",
          date.getMonth() === month - 1 ? "" : "outside"
        ].filter(Boolean).join(" ");
        button.dataset.memoDate = value;
        button.title = dayLabel;
        button.setAttribute("aria-label", dayLabel);
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
          badge.className = [
            "task-memo-day-count",
            progressState
          ].filter(Boolean).join(" ");
          badge.textContent = `${count.completed}/${count.total}`;
          badge.title = dayLabel;
          badge.setAttribute("aria-label", dayLabel);
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
      const text = documentRef.createElement("span");
      text.className = "task-memo-item-text";
      const title = documentRef.createElement("span");
      title.className = "task-memo-item-title";
      title.textContent = memo.title || t("task.untitled_draft", "Untitled draft");
      text.appendChild(title);
      const badges = documentRef.createElement("span");
      badges.className = "task-memo-item-badges";
      const rangeLabel = memoDateRangeLabel(memo);
      if (options.showDate && rangeLabel) {
        const date = documentRef.createElement("span");
        date.className = "task-memo-item-date";
        date.textContent = rangeLabel;
        text.appendChild(date);
      }
      const statusBadge = documentRef.createElement("span");
      statusBadge.className = `task-memo-item-status task-memo-status-${status}`;
      statusBadge.textContent = memoStatusLabel(status);
      badges.appendChild(statusBadge);
      const linked = documentRef.createElement("span");
      const taskStatusClass = String(memo.created_task_status || "none").replace(/[^a-z0-9_-]/gi, "-").toLowerCase();
      linked.className = `task-memo-item-link task-memo-task-status-${taskStatusClass}${memo.created_task_id ? " linked" : ""}`;
      linked.textContent = linkedTaskLabel(memo);
      button.appendChild(text);
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
        const endOrder = String(memoRangeEndDate(right) || "").localeCompare(String(memoRangeEndDate(left) || ""));
        if (endOrder) return endOrder;
        return String(right.updated_at || "").localeCompare(String(left.updated_at || ""));
      });
    }

    function selectedDayMemos() {
      return memos.filter(memo => memoDisplaysOnDate(memo, selectedDate));
    }

    function selectedDayMemoSections() {
      const onTimeDone = [];
      const lateDone = [];
      const pending = [];
      const unfinishedRecord = [];
      for (const memo of memos) {
        const info = memoCalendarEntryForDate(memo, selectedDate);
        if (!info) continue;
        if (!info.completed) {
          if (info.pending) {
            pending.push(memo);
          } else {
            unfinishedRecord.push(memo);
          }
        } else if (info.overdue) {
          lateDone.push(memo);
        } else {
          onTimeDone.push(memo);
        }
      }
      return [
        { title: t("memo.section_done_on_time", "Done on time"), items: sortMemoList(onTimeDone, true), showDate: true },
        { title: t("memo.section_done_late", "Done late"), items: sortMemoList(lateDone, true), showDate: true },
        { title: t("memo.section_pending", "Pending"), items: sortMemoList(pending, true), showDate: true },
        { title: t("memo.section_unfinished_record", "Unfinished record"), items: sortMemoList(unfinishedRecord, true), showDate: true }
      ];
    }

    function memoFilterCount(filter) {
      if (filter === "day") return selectedDayMemos().length;
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
          sections: selectedDayMemoSections()
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
      if (!currentRunId()) {
        elements.taskMemoListEl.innerHTML = `<div class="empty compact">${escapeHtml(t("memo.create_run_first", "Create a run to start using memos."))}</div>`;
        return;
      }
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
      const status = normalizeMemoStatus(values.status);
      if (elements.taskMemoEditStatusEl) elements.taskMemoEditStatusEl.value = status;
      const scheduledDate = memoDateValue(values.scheduled_date || selectedDate);
      if (elements.taskMemoEditDateEl) elements.taskMemoEditDateEl.value = scheduledDate;
      if (elements.taskMemoEditEndDateEl) elements.taskMemoEditEndDateEl.value = memoEndDateValue(values.end_date, scheduledDate);
      if (elements.taskMemoEditCompletedDateEl) elements.taskMemoEditCompletedDateEl.value = memoTerminalInputDate(values, "done");
      if (elements.taskMemoEditClosedDateEl) elements.taskMemoEditClosedDateEl.value = memoTerminalInputDate(values, "closed");
      syncEndDateInputBounds();
      syncTerminalDateFields();
    }

    function syncDateInputEmptyState(input) {
      input?.classList?.toggle("task-memo-date-empty", !input.value);
    }

    function syncDateInputEmptyStates() {
      [
        elements.taskMemoEditDateEl,
        elements.taskMemoEditEndDateEl,
        elements.taskMemoEditCompletedDateEl,
        elements.taskMemoEditClosedDateEl
      ].forEach(syncDateInputEmptyState);
    }

    function syncEndDateInputBounds() {
      const start = memoDateValue(elements.taskMemoEditDateEl?.value || selectedDate);
      [
        elements.taskMemoEditEndDateEl,
        elements.taskMemoEditCompletedDateEl,
        elements.taskMemoEditClosedDateEl
      ].forEach(input => {
        if (!input) return;
        if (start) {
          input.min = start;
        } else {
          input.removeAttribute?.("min");
        }
        if (input.value && start && input.value < start) {
          input.value = "";
        }
      });
    }

    function syncTerminalDateFields(options = {}) {
      const status = normalizeMemoStatus(elements.taskMemoEditStatusEl?.value || "todo");
      const disabled = Boolean(options.disabled);
      const defaultDate = Boolean(options.defaultDate);
      const defaultTerminalDate = memoDefaultTerminalDate(elements.taskMemoEditDateEl?.value || selectedDate);
      const showCompleted = status === "done";
      const showClosed = status === "closed";
      setHidden(elements.taskMemoCompletedDateFieldEl, !showCompleted);
      setHidden(elements.taskMemoClosedDateFieldEl, !showClosed);
      setDisabled(elements.taskMemoEditCompletedDateEl, disabled || !showCompleted);
      setDisabled(elements.taskMemoEditClosedDateEl, disabled || !showClosed);
      if (showCompleted && defaultDate && elements.taskMemoEditCompletedDateEl && !elements.taskMemoEditCompletedDateEl.value) {
        elements.taskMemoEditCompletedDateEl.value = defaultTerminalDate;
      }
      if (showClosed && defaultDate && elements.taskMemoEditClosedDateEl && !elements.taskMemoEditClosedDateEl.value) {
        elements.taskMemoEditClosedDateEl.value = defaultTerminalDate;
      }
      if (!showCompleted && elements.taskMemoEditCompletedDateEl) elements.taskMemoEditCompletedDateEl.value = "";
      if (!showClosed && elements.taskMemoEditClosedDateEl) elements.taskMemoEditClosedDateEl.value = "";
      syncDateInputEmptyStates();
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
      const status = normalizeMemoStatus(memo.status);
      return {
        title: String(memo.title || ""),
        description: String(memo.description || ""),
        status,
        scheduled_date: memoDateValue(memo.scheduled_date || selectedDate),
        end_date: memoEndDateValue(memo.end_date, memo.scheduled_date || selectedDate),
        completed_at: status === "done" ? memoTerminalInputDate(memo, "done") : "",
        closed_at: status === "closed" ? memoTerminalInputDate(memo, "closed") : "",
        created_task_id: String(memo.created_task_id || "").trim()
      };
    }

    function readCurrentEditorFields() {
      const memo = editorMode === "edit" ? selectedMemo() : null;
      const status = normalizeMemoStatus(elements.taskMemoEditStatusEl?.value || "todo");
      return {
        title: String(elements.taskMemoEditTitleEl?.value || ""),
        description: String(elements.taskMemoEditDescriptionEl?.value || ""),
        status,
        scheduled_date: memoDateValue(elements.taskMemoEditDateEl?.value || selectedDate),
        end_date: memoEndDateValue(elements.taskMemoEditEndDateEl?.value, elements.taskMemoEditDateEl?.value || selectedDate),
        completed_at: status === "done" ? memoTerminalDateValue(elements.taskMemoEditCompletedDateEl?.value, elements.taskMemoEditDateEl?.value || selectedDate) : "",
        closed_at: status === "closed" ? memoTerminalDateValue(elements.taskMemoEditClosedDateEl?.value, elements.taskMemoEditDateEl?.value || selectedDate) : "",
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
      if (isMemoReportGenerating(memo)) return false;
      const baseline = editorFieldsFromMemo(memo);
      return current.title !== baseline.title
        || current.description !== baseline.description
        || current.status !== baseline.status
        || current.scheduled_date !== baseline.scheduled_date
        || current.end_date !== baseline.end_date
        || current.completed_at !== baseline.completed_at
        || current.closed_at !== baseline.closed_at
        || current.created_task_id !== baseline.created_task_id;
    }

    function updateSaveState() {
      const isEmpty = editorMode === "empty";
      const reportGenerating = editorMode === "edit" && isMemoReportGenerating(selectedMemo());
      const canSave = editorCanSave();
      const showCancel = editorMode === "create" || (editorMode === "edit" && canSave);
      const blockTaskAction = editorMode === "edit" && (canSave || reportGenerating);
      setHidden(elements.taskMemoCancelEl, !showCancel);
      setHidden(elements.taskMemoSaveEl, isEmpty);
      setDisabled(elements.taskMemoSaveEl, !canSave || reportGenerating);
      setDisabled(elements.taskMemoConvertEl, editorMode !== "edit" || reportGenerating);
      if (elements.taskMemoConvertEl) {
        elements.taskMemoConvertEl.classList.toggle("task-memo-action-blocked", blockTaskAction);
        elements.taskMemoConvertEl.setAttribute("aria-disabled", String(editorMode !== "edit" || blockTaskAction));
        if (blockTaskAction) {
          elements.taskMemoConvertEl.title = t("memo.task_action_save_first", "Save memo changes before using task actions.");
        } else {
          elements.taskMemoConvertEl.removeAttribute?.("title");
        }
      }
      elements.taskMemoSaveEl?.classList?.toggle("task-memo-save-dirty", canSave);
    }

    async function showTaskActionSaveFirstDialog() {
      const message = t("memo.task_action_save_first", "Save memo changes before using task actions.");
      setState(message);
      if (deps.confirmDialogAction) {
        await deps.confirmDialogAction({
          title: t("memo.task_action_save_first_title", "Unsaved memo changes"),
          message,
          actions: [{
            value: "confirm",
            label: t("common.confirm", "Confirm"),
            primary: true
          }]
        });
      } else {
        deps.alert?.(message);
      }
    }

    async function confirmCompletionReportGeneration(memo = {}) {
      const taskId = String(memo.created_task_id || "").trim();
      if (!taskId) return false;
      if (deps.confirmDialogAction) {
        return await deps.confirmDialogAction({
          title: t("memo.report_generate_title", "Generate completion report?"),
          message: t("memo.report_generate_message", "This memo is linked to a task. Generate a completion report with the task main agent in the background?"),
          details: [[t("task.id", "Task"), taskId]],
          actions: [
            {
              value: "confirm",
              label: t("memo.report_confirm", "Generate report"),
              primary: true
            },
            {
              value: "cancel",
              label: t("common.cancel", "Cancel")
            }
          ]
        });
      }
      return Promise.resolve(windowRef.confirm(t("memo.report_generate_message", "This memo is linked to a task. Generate a completion report with the task main agent in the background?")));
    }

    async function requestCompletionReport(memo = {}) {
      const memoId = String(memo.id || "").trim();
      if (!memoId) return null;
      const response = await deps.fetchJson(deps.apiUrl(`/api/task-memos/${encodeURIComponent(memoId)}/completion-report`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      }, "Failed to generate memo completion report");
      return response?.memo || null;
    }

    function isCompactMemoViewport() {
      return Boolean(windowRef.matchMedia?.("(max-width: 640px)")?.matches);
    }

    function focusEditorOnCompactViewport() {
      if (!isCompactMemoViewport()) return;
      elements.taskMemoEditorColumnEl?.scrollIntoView?.({ block: "start", behavior: "smooth" });
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
      const reportGenerating = isMemoReportGenerating(memo);
      setHidden(elements.taskMemoTaskLinkFieldEl, false);
      setHidden(elements.taskMemoTaskPickerToggleEl, false);
      setText(elements.taskMemoTaskPickerToggleEl, linked ? t("memo.task_change", "Change task") : t("memo.task_choose", "Choose task"));
      if (elements.taskMemoTaskLinkClearEl) elements.taskMemoTaskLinkClearEl.hidden = !linked;
      setDisabled(elements.taskMemoTaskPickerToggleEl, reportGenerating);
      setDisabled(elements.taskMemoTaskLinkClearEl, reportGenerating || !linked);
      if (reportGenerating) taskPickerOpen = false;
      if (elements.taskMemoTaskPickerEl) elements.taskMemoTaskPickerEl.hidden = !taskPickerOpen;
      if (elements.taskMemoTaskPickerSearchEl && elements.taskMemoTaskPickerSearchEl.value !== taskPickerSearch) {
        elements.taskMemoTaskPickerSearchEl.value = taskPickerSearch;
      }
      if (elements.taskMemoTaskPickerFilterEl && elements.taskMemoTaskPickerFilterEl.value !== taskPickerFilter) {
        elements.taskMemoTaskPickerFilterEl.value = taskPickerFilter;
      }
      renderTaskPickerList(draftMemo);
    }

    function closeTaskPicker() {
      if (!taskPickerOpen) return;
      taskPickerOpen = false;
      renderTaskLinkPicker(selectedMemo(), editorMode === "edit");
    }

    function taskPickerOwnsTarget(target) {
      if (!(target instanceof Element)) return false;
      if (elements.taskMemoTaskPickerEl?.contains(target)) return true;
      if (elements.taskMemoTaskPickerToggleEl?.contains(target)) return true;
      return false;
    }

    function renderTaskPickerList(memo) {
      const listEl = elements.taskMemoTaskPickerListEl;
      if (!listEl || !documentRef) return;
      listEl.innerHTML = "";
      if (!taskPickerOpen) return;
      if (taskPickerLoading && !taskPickerSourceTasks().length) {
        listEl.innerHTML = `<div class="empty compact">${escapeHtml(t("memo.task_loading", "Loading tasks..."))}</div>`;
        return;
      }
      if (taskPickerError && !taskPickerSourceTasks().length) {
        listEl.innerHTML = `<div class="empty compact">${escapeHtml(t("memo.task_load_failed", "Failed to load tasks."))}</div>`;
        return;
      }
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
      const hasRun = Boolean(currentRunId());
      let memo = editorMode === "edit" ? selectedMemo() : null;
      if (editorMode === "edit" && !memo) {
        selectedMemoId = "";
        editorMode = "empty";
        memo = null;
      }
      const isEdit = Boolean(memo);
      const isCreate = editorMode === "create";
      const isEmpty = !isCreate && !isEdit;
      const reportGenerating = isEdit && isMemoReportGenerating(memo);
      if (isEdit) {
        ensureDraftTaskLink(memo);
        fillEditor(memo);
      }
      if (isEmpty) {
        draftTaskLinkMemoId = "";
        draftTaskLinkId = "";
        draftTaskLinkDirty = false;
        fillEditor({});
      }
      if (isCreate) fillEditor({
        title: elements.taskMemoEditTitleEl?.value || "",
        description: elements.taskMemoEditDescriptionEl?.value || "",
        status: elements.taskMemoEditStatusEl?.value || "todo",
        scheduled_date: elements.taskMemoEditDateEl?.value || selectedDate,
        end_date: elements.taskMemoEditEndDateEl?.value || ""
      });
      memoMarkdownTools?.setReport?.(isEdit ? memoReportPayload(memo) : {});
      if (reportGenerating) memoMarkdownTools?.setMode?.("report", { focus: false });
      const editorStatus = isEdit ? memo?.status : (isCreate ? elements.taskMemoEditStatusEl?.value : "todo");
      renderStatusOptions(editorStatus, isEmpty || reportGenerating);
      syncTerminalDateFields({ disabled: isEmpty || reportGenerating, defaultDate: isCreate });
      renderTaskLinkPicker(memo, isEdit);
      setText(elements.taskMemoEditorTitleEl, isEdit
        ? t("memo.editor_edit", "Edit memo")
        : isCreate
          ? t("memo.editor_create", "New memo")
          : hasRun
            ? t("memo.editor_empty", "Select or create a memo")
            : t("memo.workspace_needed", "Create a run"));
      setText(elements.taskMemoEditorHintEl, isEdit
        ? (reportGenerating
          ? t("memo.report_generating", "Completion report is generating. Memo is read-only.")
          : t("memo.editor_edit_hint", "Update this memo or turn it into a task."))
        : isCreate
          ? t("memo.editor_create_hint", "Fill in a future task idea, then save it as a memo.")
          : hasRun
            ? t("memo.editor_empty_hint", "Choose a memo from the list, or click New Memo to create one.")
            : t("memo.create_run_first", "Create a run to start using memos."));
      [
        elements.taskMemoEditTitleEl,
        elements.taskMemoEditDateEl,
        elements.taskMemoEditEndDateEl,
        elements.taskMemoEditCompletedDateEl,
        elements.taskMemoEditClosedDateEl,
        elements.taskMemoEditDescriptionEl
      ]
        .forEach(element => setDisabled(element, isEmpty || reportGenerating));
      syncTerminalDateFields({ disabled: isEmpty || reportGenerating, defaultDate: isCreate });
      if (elements.taskMemoDescriptionEditorEl) {
        elements.taskMemoDescriptionEditorEl.setAttribute("aria-disabled", String(isEmpty || reportGenerating));
      }
      memoMarkdownTools?.setDisabled?.(isEmpty || reportGenerating);
      setHidden(elements.taskMemoImageUploadEl, isEmpty);
      setDisabled(elements.taskMemoImageUploadEl, isEmpty || reportGenerating);
      setHidden(elements.taskMemoImageFileEl, isEmpty);
      setDisabled(elements.taskMemoImageFileEl, isEmpty || reportGenerating);
      setHidden(elements.taskMemoCancelEl, !isCreate);
      setHidden(elements.taskMemoConvertEl, !isEdit);
      setHidden(elements.taskMemoDeleteEl, !isEdit);
      setDisabled(elements.taskMemoNewEl, !hasRun);
      setDisabled(elements.taskMemoDeleteEl, !isEdit || reportGenerating);
      setDisabled(elements.taskMemoConvertEl, !isEdit || reportGenerating);
      setText(elements.taskMemoSaveEl, t("memo.save", "Save"));
      setText(elements.taskMemoConvertEl, memoWithDraftTaskLink(memo)?.created_task_id
        ? t("memo.jump_task", "Jump to Task")
        : t("memo.convert", "Create Task"));
      memoMarkdownTools?.renderDescriptionEditor?.();
      updateSaveState();
    }

    function render() {
      renderCalendar();
      renderMemoFilters();
      renderList();
      renderEditor();
    }

    function readEditorPayload() {
      const status = normalizeMemoStatus(elements.taskMemoEditStatusEl?.value || "todo");
      const payload = {
        title: elements.taskMemoEditTitleEl?.value || "",
        description: elements.taskMemoEditDescriptionEl?.value || "",
        status,
        scheduled_date: memoDateValue(elements.taskMemoEditDateEl?.value || selectedDate),
        end_date: memoEndDateValue(elements.taskMemoEditEndDateEl?.value, elements.taskMemoEditDateEl?.value || selectedDate),
        completed_at: status === "done" ? memoTerminalDateValue(elements.taskMemoEditCompletedDateEl?.value, elements.taskMemoEditDateEl?.value || selectedDate) : "",
        closed_at: status === "closed" ? memoTerminalDateValue(elements.taskMemoEditClosedDateEl?.value, elements.taskMemoEditDateEl?.value || selectedDate) : ""
      };
      if (editorMode === "edit") {
        payload.created_task_id = currentDraftTaskLinkId(selectedMemo());
      }
      return payload;
    }

    function enterCreateMode() {
      if (!currentRunId()) {
        setState(t("memo.create_run_first", "Create a run to start using memos."));
        return;
      }
      selectedMemoId = "";
      editorMode = "create";
      draftTaskLinkMemoId = "";
      draftTaskLinkId = "";
      draftTaskLinkDirty = false;
      clearEditorForCreate();
      render();
      memoMarkdownTools?.setMode?.("edit");
      setState("");
    }

    function enterEmptyMode() {
      selectedMemoId = "";
      editorMode = "empty";
      draftTaskLinkMemoId = "";
      draftTaskLinkId = "";
      draftTaskLinkDirty = false;
      render();
      memoMarkdownTools?.setMode?.("preview", { focus: false });
      setState("");
    }

    function cancelEditor() {
      if (editorMode === "create") {
        enterEmptyMode();
        return;
      }
      if (editorMode !== "edit" || !selectedMemo()) return;
      draftTaskLinkMemoId = "";
      draftTaskLinkId = "";
      draftTaskLinkDirty = false;
      taskPickerOpen = false;
      taskPickerError = "";
      renderEditor();
      memoMarkdownTools?.setMode?.("preview", { focus: false });
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
      const baseline = creating ? null : editorFieldsFromMemo(memo);
      const payload = readEditorPayload();
      const shouldOfferReport = Boolean(
        !creating
        && payload.status === "done"
        && baseline?.status !== "done"
        && String(payload.created_task_id || "").trim()
      );
      const url = creating ? deps.apiUrl("/api/task-memos") : deps.apiUrl(`/api/task-memos/${encodeURIComponent(memo.id)}`);
      const method = creating ? "POST" : "PATCH";
      const response = await deps.fetchJson(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      }, "Failed to save memo");
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
        draftTaskLinkDirty = false;
        writePersistedSelectedMemoId(selectedMemoId);
        setState(t("memo.saved", "Memo saved."));
      }
      if (!creating && shouldOfferReport) {
        const savedMemo = response?.memo || null;
        const confirmed = await confirmCompletionReportGeneration(savedMemo);
        if (confirmed) {
          const reportMemo = await requestCompletionReport(savedMemo);
          if (reportMemo?.id) {
            selectedMemoId = reportMemo.id;
            draftTaskLinkMemoId = reportMemo.id;
            draftTaskLinkId = String(reportMemo.created_task_id || "").trim();
            draftTaskLinkDirty = false;
          }
          setState(t("memo.report_queued", "Completion report generation started."));
        }
      }
      await loadMemos();
    }

    function linkSelectedTask(taskId) {
      const memo = editorMode === "edit" ? selectedMemo() : null;
      if (!memo) return;
      if (isMemoReportGenerating(memo)) return;
      const nextTaskId = String(taskId || "").trim();
      ensureDraftTaskLink(memo);
      const currentTaskId = currentDraftTaskLinkId(memo);
      if (nextTaskId === currentTaskId) return;
      draftTaskLinkMemoId = String(memo.id || "").trim();
      draftTaskLinkId = nextTaskId;
      draftTaskLinkDirty = true;
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
      if (editorCanSave()) {
        updateSaveState();
        await showTaskActionSaveFirstDialog();
        return;
      }
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
      memoMarkdownTools?.bind?.();
      elements.openTaskViewEl?.addEventListener("click", () => {
        if (isPageMode()) closeDialog();
      });
      elements.openTaskMemosEl?.addEventListener("click", () => {
        if (isPageMode()) {
          if (!isOpen()) openDialog();
          else updateViewToggle();
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
      elements.taskMemoCalendarCollapseEl?.addEventListener("click", () => {
        memoCalendarCollapsed = !memoCalendarCollapsed;
        renderCalendar();
      });
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
        memoMarkdownTools?.setMode?.("preview", { focus: false });
        focusEditorOnCompactViewport();
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
        syncTerminalDateFields({ defaultDate: true });
        updateSaveState();
      });
      const handleFieldInput = () => {
        updateSaveState();
      };
      elements.taskMemoEditTitleEl?.addEventListener("input", handleFieldInput);
      elements.taskMemoEditDateEl?.addEventListener("input", () => {
        syncEndDateInputBounds();
        syncDateInputEmptyStates();
        updateSaveState();
      });
      const handleDateFieldInput = () => {
        syncDateInputEmptyStates();
        updateSaveState();
      };
      elements.taskMemoEditEndDateEl?.addEventListener("input", handleDateFieldInput);
      elements.taskMemoEditCompletedDateEl?.addEventListener("input", handleDateFieldInput);
      elements.taskMemoEditClosedDateEl?.addEventListener("input", handleDateFieldInput);
      elements.taskMemoEditDescriptionEl?.addEventListener("input", () => {
        memoMarkdownTools?.renderDescriptionEditor?.();
        updateSaveState();
      });
      elements.taskMemoDescriptionEditorEl?.addEventListener("click", event => {
        if (!memoMarkdownTools?.openClickedImage?.(event.target)) return;
        event.preventDefault();
      });
      elements.taskMemoDescriptionEditorEl?.addEventListener("keydown", event => {
        if (!["Enter", " "].includes(event.key)) return;
        if (!memoMarkdownTools?.openClickedImage?.(event.target)) return;
        event.preventDefault();
      });
      elements.taskMemoNewEl?.addEventListener("click", enterCreateMode);
      elements.taskMemoEditorJumpEl?.addEventListener("click", focusEditorOnCompactViewport);
      elements.taskMemoCancelEl?.addEventListener("click", cancelEditor);
      elements.taskMemoDeleteEl?.addEventListener("click", () => void deleteSelected().catch(reportError));
      elements.taskMemoConvertEl?.addEventListener("click", () => void convertSelected().catch(reportError));
      elements.taskMemoTaskPickerToggleEl?.addEventListener("click", () => {
        const opening = !taskPickerOpen;
        taskPickerOpen = opening;
        if (opening) prepareTaskPickerLoading();
        renderTaskLinkPicker(selectedMemo(), editorMode === "edit");
        if (opening) {
          void loadTaskPickerOptions(selectedMemo()).catch(reportError);
        }
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
      documentRef?.addEventListener("pointerdown", event => {
        if (!taskPickerOpen || taskPickerOwnsTarget(event.target)) return;
        closeTaskPicker();
      });
      documentRef?.addEventListener("keydown", event => {
        if (event.key !== "Escape" || !taskPickerOpen) return;
        event.preventDefault();
        event.stopPropagation();
        closeTaskPicker();
      });
      elements.taskMemoImageFileEl?.addEventListener("change", () => {
        const files = Array.from(elements.taskMemoImageFileEl?.files || []);
        if (elements.taskMemoImageFileEl) elements.taskMemoImageFileEl.value = "";
        void memoMarkdownTools?.insertMemoImageFiles?.(files).catch(reportError);
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
        if (deps.currentRunId?.()) {
          void loadMemos().catch(reportError);
        } else {
          renderMemoHomeWithoutRun();
        }
      }
      updateViewToggle();
    }

    return Object.freeze({ bind, loadMemos, openDialog, closeDialog, refreshIfOpen });
  }

  window.AHATaskMemoController = Object.freeze({ createTaskMemoController });
})();
