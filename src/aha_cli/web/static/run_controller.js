(() => {
  function escapeFallback(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function createRunController(elements = {}, deps = {}) {
    const windowRef = deps.windowRef || window;
    const SETTINGS_TAB_STORAGE_KEY = "aha.settingsTab";
    const settingsTabs = new Set(["run", "system", "integrations"]);
    let sessionMenuOpen = false;
    let runMaintenanceConsoleOpen = false;
    let runMaintenanceData = null;
    let runMaintenanceRunId = "";
    let runMaintenanceLoading = false;
    let runMaintenanceError = "";
    let runMaintenanceActionInFlight = false;
    let runMaintenanceMessage = "";
    let runArchiveMessage = "";
    let runArchiveError = false;
    let runLifecycleMessage = "";
    let runLifecycleError = false;
    let runLifecycleFilter = "active";
    let runSettingsOpenId = "";
    let settingsTab = readStoredSettingsTab();

    function normalizeSettingsTab(value) {
      const tab = String(value || "").trim().toLowerCase();
      return settingsTabs.has(tab) ? tab : "system";
    }

    function readStoredSettingsTab() {
      try {
        return normalizeSettingsTab(windowRef.localStorage?.getItem(SETTINGS_TAB_STORAGE_KEY));
      } catch (_err) {
        return "system";
      }
    }

    function setHomeViewButtons(view) {
      const active = String(view || "task").trim().toLowerCase();
      elements.documentRef?.getElementById?.("open-task-view")?.setAttribute("aria-pressed", String(active === "task"));
      elements.documentRef?.getElementById?.("open-task-memos")?.setAttribute("aria-pressed", String(active === "memo"));
      elements.documentRef?.getElementById?.("open-knowledge-base")?.setAttribute("aria-pressed", String(active === "kb"));
      elements.sessionToggleEl?.setAttribute("aria-pressed", String(active === "settings"));
    }

    function syncHomeViewButtons() {
      const body = elements.documentRef?.body;
      if (body?.classList?.contains("settings-home")) {
        setHomeViewButtons("settings");
      } else if (body?.classList?.contains("knowledge-home")) {
        setHomeViewButtons("kb");
      } else if (body?.classList?.contains("task-memo-home")) {
        setHomeViewButtons("memo");
      } else {
        setHomeViewButtons("task");
      }
    }

    function closeSystemSettingsPanel() {
      const panel = elements.documentRef?.getElementById?.("settings-dialog");
      const trigger = elements.documentRef?.getElementById?.("aha-settings");
      if (!panel) return;
      panel.hidden = true;
      panel.removeAttribute("open");
      trigger?.setAttribute("aria-expanded", "false");
      elements.sessionMenuEl?.classList?.remove("settings-open");
    }

    function setSettingsTab(tab, options = {}) {
      settingsTab = normalizeSettingsTab(tab);
      elements.sessionMenuEl?.setAttribute("data-settings-tab", settingsTab);
      const buttons = elements.sessionMenuEl?.querySelectorAll?.("button.settings-home-tab[data-settings-tab]") || [];
      for (const button of buttons) {
        const active = button.getAttribute("data-settings-tab") === settingsTab;
        button.classList.toggle("active", active);
        button.setAttribute("aria-selected", String(active));
      }
      if (!options.skipStorage) {
        try {
          windowRef.localStorage?.setItem(SETTINGS_TAB_STORAGE_KEY, settingsTab);
        } catch (_err) {
          // Tab persistence is best effort.
        }
      }
      if (settingsTab !== "integrations") {
        deps.setWeixinConsoleOpen?.(false);
        deps.setPlayConsoleOpen?.(false);
        deps.setSkillsConsoleOpen?.(false);
        deps.setTokenUsageOpen?.(false);
      }
      if (settingsTab !== "system") closeSystemSettingsPanel();
    }

    function currentRunId() {
      return String(deps.currentRunId?.() || "").trim();
    }

    function runActionInFlight() {
      return Boolean(deps.runActionInFlight?.());
    }

    function escapeHtml(value) {
      return (deps.escapeHtml || escapeFallback)(value);
    }

    function currentAppVersion() {
      return String(deps.currentAppVersion?.() || "");
    }

    function renderAppVersion() {
      if (!elements.appVersionEl) return;
      const version = currentAppVersion();
      elements.appVersionEl.textContent = version ? `v${version}` : "";
      elements.appVersionEl.title = version ? `AHA version ${version}` : "";
      if (elements.documentRef) elements.documentRef.title = version ? `AHA v${version}` : "AHA Dashboard";
    }

    function closeHeaderRunConsole() {
      if (elements.headerRunConsoleEl && "open" in elements.headerRunConsoleEl) elements.headerRunConsoleEl.open = false;
    }

    function renderHeaderRunTitle(run, title, runId) {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      if (!elements.headerRunTitleEl) return;
      const label = title || deps.runTitleOf?.(run) || runId || t("run.short_label", "Run");
      elements.headerRunTitleEl.textContent = label;
      if (elements.headerRunConsoleEl) {
        const project = String(elements.headerWorkspaceDirEl?.textContent || "").trim();
        elements.headerRunConsoleEl.title = [label, project].filter(Boolean).join(" · ");
      }
    }

    function renderRunLifecycleBadge(run) {
      if (!elements.runLifecycleEl) return;
      elements.runLifecycleEl.hidden = !run;
      if (!run) {
        elements.runLifecycleEl.textContent = "";
        elements.runLifecycleEl.className = "run-lifecycle status run-lifecycle-active";
        elements.runLifecycleEl.title = "";
        return;
      }
      elements.runLifecycleEl.textContent = deps.runLifecycleLabel?.(run) || "";
      elements.runLifecycleEl.className = `run-lifecycle status ${deps.runLifecycleClass?.(run) || ""}`;
      elements.runLifecycleEl.title = deps.runLifecycleTitle?.(run) || "";
    }

    function renderSessionSummary() {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      renderAppVersion();
      const run = deps.currentRunSummary?.() || deps.fallbackCurrentRun?.();
      const statusData = deps.statusData?.();
      const bootstrapData = deps.bootstrapData?.();
      const runId = currentRunId() || deps.runIdOf?.(run) || "";
      if (!run && !statusData) {
        renderHeaderRunTitle(null, "", "");
        if (elements.sessionTitleEl) elements.sessionTitleEl.textContent = currentRunId() || t("run.none", "No run selected");
        if (elements.runIdEl) elements.runIdEl.textContent = currentRunId() || "-";
        if (elements.runStateEl) elements.runStateEl.textContent = bootstrapData?.aha_home ? `AHA_HOME ${bootstrapData.aha_home}` : "";
        if (elements.sessionDetailTextEl) elements.sessionDetailTextEl.textContent = t("run.create_first", "Create a run to start");
        renderRunLifecycleBadge(null);
        if (elements.taskRunContextEl) elements.taskRunContextEl.textContent = t("run.no_current", "No current run");
        return;
      }
      const title = statusData?.goal || deps.runTitleOf?.(run) || "";
      renderHeaderRunTitle(run, title, runId);
      if (elements.sessionTitleEl) {
        elements.sessionTitleEl.textContent = title || t("run.none", "No run selected");
        elements.sessionTitleEl.title = title || "";
      }
      if (elements.sessionToggleEl) {
        const consoleLabel = t("aha.console", "AHA console");
        const label = runId ? `${consoleLabel}: ${title || runId}` : consoleLabel;
        elements.sessionToggleEl.title = label;
        elements.sessionToggleEl.setAttribute("aria-label", label);
      }
      if (elements.runIdEl) {
        elements.runIdEl.textContent = runId || "-";
        elements.runIdEl.title = runId || "";
      }
      if (elements.runStateEl) {
        elements.runStateEl.textContent = "";
        elements.runStateEl.title = "";
      }
      renderRunLifecycleBadge(run);
      if (elements.sessionDetailTextEl) {
        const taskCount = Number.isFinite(run?.task_count) ? `${run.completed_count || 0}/${run.task_count} tasks` : "";
        elements.sessionDetailTextEl.textContent = [
          run?.mode ? `mode ${run.mode}` : statusData?.mode ? `mode ${statusData.mode}` : "",
          run?.status ? `${t("run.status", "status")} ${run.status}` : "",
          taskCount,
          deps.realtimeTransportText?.(),
          deps.runsError?.() ? `${t("run.notice", "notice")} ${deps.runsError?.()}` : ""
        ].filter(Boolean).join(" · ") || t("run.details", "Run details");
      }
      if (elements.taskRunContextEl) {
        const mode = statusData?.mode || run?.mode || "-";
        const runLabel = title || runId || "-";
        elements.taskRunContextEl.textContent = `${t("run.current", "Current run")}: ${runLabel} / ${mode}`;
        elements.taskRunContextEl.title = runId || "";
      }
    }

    function syncCurrentRunDisplay(run, fallbackName = "") {
      if (!run || deps.runIdOf?.(run) !== currentRunId()) return;
      const runName = String(run.goal || fallbackName || "").trim();
      deps.updateStatusRunTitle?.(runName, run.updated_at);
      renderSessionSummary();
      if (elements.summaryEl && runName) elements.summaryEl.textContent = runName;
    }

    function setRunLifecycleState(message, isError = false) {
      runLifecycleMessage = String(message || "");
      runLifecycleError = Boolean(isError);
      renderRunLifecycleState();
    }

    function renderRunLifecycleState() {
      if (!elements.runLifecycleStateEl) return;
      elements.runLifecycleStateEl.textContent = runLifecycleMessage;
      elements.runLifecycleStateEl.title = runLifecycleMessage;
      elements.runLifecycleStateEl.classList.toggle("error", runLifecycleError);
    }

    function renderRunLifecycleFilters(runs) {
      if (!elements.runLifecycleFilterEl) return;
      const view = deps.runLifecycleActionsView?.(runs, runLifecycleFilter, currentRunId(), runActionInFlight()) || { filters: [] };
      elements.runLifecycleFilterEl.innerHTML = deps.runLifecycleFiltersHtml?.(view.filters || []) || "";
    }

    function renderRunLifecycleActions() {
      if (!elements.runLifecycleActionsEl) return;
      const fallback = deps.fallbackCurrentRun?.();
      const runs = (deps.runsData?.() || []).length ? deps.runsData?.() : (fallback ? [fallback] : []);
      const view = deps.runLifecycleActionsView?.(runs, runLifecycleFilter, currentRunId(), runActionInFlight()) || {};
      renderRunLifecycleFilters(runs);
      if (view.emptyMessage) {
        elements.runLifecycleActionsEl.innerHTML = `<div class="meta">${escapeHtml(view.emptyMessage)}</div>`;
        renderRunSettingsPanel(null);
        renderRunLifecycleState();
        return;
      }
      const rows = view.rows || [];
      if (runSettingsOpenId && !rows.some(row => row.id === runSettingsOpenId)) runSettingsOpenId = "";
      elements.runLifecycleActionsEl.innerHTML = deps.runLifecycleRowsHtml?.(rows.map(row => ({
        ...row,
        settingsOpen: row.id === runSettingsOpenId
      }))) || "";
      renderRunSettingsPanel(rows.find(row => row.id === runSettingsOpenId) || null);
      renderRunLifecycleState();
    }

    function runSettingsTriggerFor(runId) {
      const buttons = elements.runLifecycleActionsEl?.querySelectorAll("[data-run-settings-toggle]") || [];
      return Array.from(buttons).find(button => button.getAttribute("data-run-settings-toggle") === runId) || null;
    }

    function clearRunSettingsPosition() {
      if (!elements.runSettingsPanelEl) return;
      elements.runSettingsPanelEl.style.removeProperty("top");
      elements.runSettingsPanelEl.style.removeProperty("left");
      elements.runSettingsPanelEl.style.removeProperty("width");
    }

    function runSettingsUseSheet() {
      return Boolean(windowRef.matchMedia?.("(max-width: 640px)")?.matches);
    }

    function positionRunSettingsPanel(row) {
      const panel = elements.runSettingsPanelEl;
      if (!panel || !row) return;
      clearRunSettingsPosition();
      if (elements.documentRef?.body?.classList?.contains("settings-home")) {
        panel.classList.remove("run-settings-sheet", "run-settings-popover");
        return;
      }
      const useSheet = runSettingsUseSheet();
      panel.classList.toggle("run-settings-sheet", useSheet);
      panel.classList.toggle("run-settings-popover", !useSheet);
      if (useSheet) return;
      const trigger = runSettingsTriggerFor(row.id);
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

    function runSettingsActionButtonHtml(action, runId) {
      return `<button class="run-lifecycle-action" type="button" data-run-lifecycle-run="${escapeHtml(runId)}" data-run-lifecycle-status="${escapeHtml(action.status)}"${action.disabled ? " disabled" : ""} title="${escapeHtml(action.title)}">${escapeHtml(action.label)}</button>`;
    }

    function runSettingsDeleteButtonHtml(action, runId) {
      if (!action) return "";
      return `<button class="run-lifecycle-action danger" type="button" data-run-delete-run="${escapeHtml(runId)}"${action.disabled ? " disabled" : ""} title="${escapeHtml(action.title)}">${escapeHtml(action.label)}</button>`;
    }

    function renderRunSettingsPanel(row) {
      if (!elements.runSettingsPanelEl) return;
      const open = Boolean(runSettingsOpenId && row && row.id === runSettingsOpenId);
      elements.runSettingsPanelEl.classList.toggle("hidden", !open);
      elements.runSettingsPanelEl.hidden = !open;
      if (!open) {
        clearRunSettingsPosition();
        if (runMaintenanceConsoleOpen) setRunMaintenanceConsoleOpen(false);
        return;
      }
      if (elements.runSettingsSubtitleEl) {
        elements.runSettingsSubtitleEl.textContent = `${row.title} | ${row.lifecycle}`;
        elements.runSettingsSubtitleEl.title = row.id;
      }
      if (elements.runRenameFormEl) elements.runRenameFormEl.hidden = !row.current;
      if (elements.renameRunNameEl && row.current && elements.documentRef?.activeElement !== elements.renameRunNameEl) {
        elements.renameRunNameEl.value = row.title || "";
        elements.renameRunNameEl.disabled = Boolean(row.actionInFlight);
      }
      const renameButton = elements.runRenameFormEl?.querySelector("button");
      if (renameButton) renameButton.disabled = Boolean(row.actionInFlight || !row.current);
      if (elements.runSettingsActionsEl) {
        const actionButtons = (row.actions || []).map(action => runSettingsActionButtonHtml(action, row.id)).join("");
        const deleteButton = runSettingsDeleteButtonHtml(row.deleteAction, row.id);
        const maintenanceButton = row.maintenance
          ? `<button class="run-lifecycle-action run-maintenance-trigger" type="button" data-run-maintenance-run="${escapeHtml(row.id)}" title="${escapeHtml(row.maintenance.title)}">${escapeHtml(row.maintenance.label)}</button>`
          : "";
        elements.runSettingsActionsEl.innerHTML = `${actionButtons}${deleteButton}${maintenanceButton}`;
      }
      if (elements.runSettingsProtectionEl) {
        elements.runSettingsProtectionEl.textContent = row.reasonText || "";
        elements.runSettingsProtectionEl.hidden = !row.reasonText;
      }
      positionRunSettingsPanel(row);
    }

    function closeRunSettings() {
      runSettingsOpenId = "";
      renderRunLifecycleActions();
    }

    function setRunArchiveState(message, isError = false) {
      runArchiveMessage = String(message || "");
      runArchiveError = Boolean(isError);
      renderRunArchiveState();
    }

    function renderRunArchiveState() {
      if (!elements.runArchiveStateEl) return;
      elements.runArchiveStateEl.textContent = runArchiveMessage;
      elements.runArchiveStateEl.title = runArchiveMessage;
      elements.runArchiveStateEl.classList.toggle("error", runArchiveError);
    }

    function setWebRestartState(message, isError = false) {
      if (!elements.webRestartStateEl) return;
      const text = String(message || "");
      elements.webRestartStateEl.textContent = text;
      elements.webRestartStateEl.title = text;
      elements.webRestartStateEl.classList.toggle("error", Boolean(isError));
    }

    function resetRunMaintenanceState() {
      runMaintenanceData = null;
      runMaintenanceRunId = "";
      runMaintenanceLoading = false;
      runMaintenanceError = "";
      runMaintenanceActionInFlight = false;
      runMaintenanceMessage = "";
    }

    function runMaintenancePayload() {
      if (!runMaintenanceData || !runMaintenanceRunId) return null;
      return runMaintenanceData;
    }

    function runMaintenanceButtonHtml(button) {
      const classes = `run-maintenance-action${button.danger ? " danger" : ""}`;
      const attrs = [
        `class="${classes}"`,
        `type="button"`,
        `data-run-maintenance-action="${escapeHtml(button.action)}"`
      ];
      if (button.taskId) attrs.push(`data-run-maintenance-task="${escapeHtml(button.taskId)}"`);
      if (button.agentId) attrs.push(`data-run-maintenance-agent="${escapeHtml(button.agentId)}"`);
      if (button.archive) attrs.push(`data-run-maintenance-archive="${escapeHtml(button.archive)}"`);
      if (button.title) attrs.push(`title="${escapeHtml(button.title)}"`);
      if (button.disabled) attrs.push("disabled");
      return `<button ${attrs.join(" ")}>${escapeHtml(button.label)}</button>`;
    }

    function runMaintenanceRowsHtml(rows) {
      return rows.map(row => (
        `<div class="run-maintenance-row"><span>${escapeHtml(row.label)}</span><strong title="${escapeHtml(row.value)}">${escapeHtml(row.value)}</strong></div>`
      )).join("");
    }

    function renderRunMaintenance() {
      const t = window.AHAI18n?.t || ((_, fallback) => fallback);
      if (!elements.runMaintenanceSummaryEl || !elements.runMaintenanceDetailEl) return;
      if (!runMaintenanceRunId && !currentRunId()) {
        elements.runMaintenanceSummaryEl.textContent = t("run.none", "No run selected");
        elements.runMaintenanceSummaryEl.classList.remove("error");
        elements.runMaintenanceDetailEl.innerHTML = "";
        return;
      }
      if (runMaintenanceLoading) {
        elements.runMaintenanceSummaryEl.textContent = t("run.maintenance_loading", "Diagnosing...");
        elements.runMaintenanceSummaryEl.classList.remove("error");
        elements.runMaintenanceDetailEl.innerHTML = "";
        return;
      }
      if (runMaintenanceError) {
        elements.runMaintenanceSummaryEl.textContent = runMaintenanceError;
        elements.runMaintenanceSummaryEl.title = runMaintenanceError;
        elements.runMaintenanceSummaryEl.classList.add("error");
        elements.runMaintenanceDetailEl.innerHTML = "";
        return;
      }
      const payload = runMaintenancePayload();
      if (!payload) {
        elements.runMaintenanceSummaryEl.textContent = t("run.maintenance_empty", "Not diagnosed");
        elements.runMaintenanceSummaryEl.classList.remove("error");
        elements.runMaintenanceDetailEl.innerHTML = "";
        return;
      }
      const view = deps.runMaintenanceView?.(payload, {
        formatBytes: deps.formatMetricBytes,
        actionInFlight: runMaintenanceActionInFlight,
        message: runMaintenanceMessage
      }) || { summary: "", rows: [], buttons: [] };
      elements.runMaintenanceSummaryEl.textContent = view.summary;
      elements.runMaintenanceSummaryEl.title = elements.runMaintenanceSummaryEl.textContent;
      elements.runMaintenanceSummaryEl.classList.remove("error");
      const rows = runMaintenanceRowsHtml(view.rows || []);
      const buttons = (view.buttons || []).map(runMaintenanceButtonHtml).join("");
      elements.runMaintenanceDetailEl.innerHTML = `${rows}<div class="run-maintenance-buttons">${buttons}</div>`;
    }

    async function loadRunMaintenance(force = false) {
      const runId = runMaintenanceRunId || currentRunId();
      if (!runId) {
        resetRunMaintenanceState();
        renderRunMaintenance();
        return;
      }
      if (!force && runMaintenancePayload() && runMaintenanceRunId === runId) {
        renderRunMaintenance();
        return;
      }
      runMaintenanceRunId = runId;
      runMaintenanceLoading = true;
      runMaintenanceError = "";
      renderRunMaintenance();
      try {
        const payload = await deps.fetchJson?.(
          deps.apiUrl?.(`/api/runs/${encodeURIComponent(runId)}/maintenance`, { top: "5" }, { runScoped: false }),
          {},
          "Failed to load run maintenance"
        );
        const currentTargetId = runMaintenanceRunId || currentRunId();
        if (currentTargetId !== runId) return;
        runMaintenanceData = payload;
      } catch (err) {
        const currentTargetId = runMaintenanceRunId || currentRunId();
        if (currentTargetId !== runId) return;
        runMaintenanceData = null;
        runMaintenanceError = err?.message || String(err || (window.AHAI18n?.t?.("run.maintenance_unavailable", "Diagnostics unavailable") || "Diagnostics unavailable"));
      } finally {
        const currentTargetId = runMaintenanceRunId || currentRunId();
        if (currentTargetId === runId) {
          runMaintenanceLoading = false;
          renderRunMaintenance();
        }
      }
    }

    function renderSessionMenu() {
      const fallback = deps.fallbackCurrentRun?.();
      const rawRuns = deps.runsData?.();
      const loadedRuns = Array.isArray(rawRuns) ? rawRuns : [];
      const runs = loadedRuns.length ? loadedRuns : (fallback ? [fallback] : []);
      const currentRun = deps.currentRunSummary?.() || fallback;
      const runId = currentRunId();
      const actionInFlight = runActionInFlight();
      if (elements.runSelectEl) {
        elements.runSelectEl.innerHTML = "";
        for (const run of runs) {
          const id = deps.runIdOf?.(run) || "";
          if (!id) continue;
          const opt = document.createElement("option");
          opt.value = id;
          opt.textContent = deps.sessionOptionLabel?.(run) || id;
          opt.selected = id === runId;
          elements.runSelectEl.appendChild(opt);
        }
        elements.runSelectEl.disabled = actionInFlight || elements.runSelectEl.options.length === 0;
      }
      if (elements.sessionRefreshEl) elements.sessionRefreshEl.disabled = actionInFlight;
      if (elements.renameRunNameEl) {
        if (document.activeElement !== elements.renameRunNameEl) {
          elements.renameRunNameEl.value = currentRun ? deps.runTitleOf?.(currentRun) || "" : "";
        }
        elements.renameRunNameEl.disabled = actionInFlight || !runId;
      }
      if (elements.runRenameFormEl) {
        const button = elements.runRenameFormEl.querySelector("button");
        if (button) button.disabled = actionInFlight || !runId;
      }
      if (elements.newRunGoalEl) elements.newRunGoalEl.disabled = actionInFlight;
      if (elements.openRunCreateEl) elements.openRunCreateEl.disabled = actionInFlight;
      const hasRun = Boolean(runId);
      if (elements.runExportEl) elements.runExportEl.disabled = actionInFlight || !hasRun;
      if (elements.runImportEl) elements.runImportEl.disabled = actionInFlight;
      if (elements.runExportLogsEl) elements.runExportLogsEl.disabled = actionInFlight || !hasRun;
      if (elements.runImportFileEl) elements.runImportFileEl.disabled = actionInFlight;
      if (elements.webRestartEl) elements.webRestartEl.disabled = Boolean(deps.webRestartInFlight?.()) || !hasRun;
      if (elements.webUpgradeEl) elements.webUpgradeEl.disabled = Boolean(deps.webRestartInFlight?.()) || !hasRun;
      if (elements.weixinConsoleEl) elements.weixinConsoleEl.disabled = actionInFlight || !hasRun;
      if (elements.playConsoleEl) elements.playConsoleEl.disabled = actionInFlight || !hasRun;
      if (elements.skillsConsoleEl) elements.skillsConsoleEl.disabled = actionInFlight;
      if (elements.tokenUsageEl) elements.tokenUsageEl.disabled = actionInFlight || !hasRun;
      if (elements.runMaintenanceRefreshEl) {
        elements.runMaintenanceRefreshEl.disabled = runMaintenanceLoading || runMaintenanceActionInFlight || actionInFlight || !hasRun;
      }
      if (!hasRun && runSettingsOpenId) {
        runSettingsOpenId = "";
        renderRunSettingsPanel(null);
      }
      if (!hasRun || actionInFlight) {
        setRunMaintenanceConsoleOpen(false);
        deps.setWeixinConsoleOpen?.(false);
        deps.setPlayConsoleOpen?.(false);
        deps.setTokenUsageOpen?.(false);
        if (actionInFlight) deps.setSkillsConsoleOpen?.(false);
      } else if (runMaintenanceConsoleOpen && elements.runMaintenancePopoverEl) {
        renderRunMaintenance();
      } else if (deps.skillsConsoleOpen?.() && elements.skillsConsolePopoverEl) {
        deps.renderSkillsConsolePopover?.();
      } else if (deps.tokenUsageOpen?.() && elements.tokenUsagePopoverEl) {
        deps.renderTokenUsagePopover?.();
      } else if (deps.weixinConsoleOpen?.() && elements.weixinConsolePopoverEl) {
        deps.renderWeixinConsolePopover?.();
      } else if (deps.playConsoleOpen?.() && elements.playConsolePopoverEl) {
        deps.renderPlayConsolePopover?.();
      }
      renderRunLifecycleActions();
      renderRunMaintenance();
      deps.renderAccessControlStatus?.();
      renderRunArchiveState();
      renderSessionSummary();
    }

    function closeRunCreateDialog() {
      if (!elements.runCreateDialogEl) return;
      if (typeof elements.runCreateDialogEl.close === "function" && elements.runCreateDialogEl.open) {
        elements.runCreateDialogEl.close();
      } else {
        elements.runCreateDialogEl.removeAttribute("open");
      }
    }

    function openRunCreateDialog() {
      if (runActionInFlight() || !elements.runCreateDialogEl) return;
      try {
        if (typeof elements.runCreateDialogEl.showModal === "function") {
          if (!elements.runCreateDialogEl.open) elements.runCreateDialogEl.showModal();
        } else {
          elements.runCreateDialogEl.setAttribute("open", "");
        }
      } catch (_err) {
        elements.runCreateDialogEl.setAttribute("open", "");
      }
      windowRef.setTimeout(() => elements.newRunGoalEl?.focus(), 0);
    }

    function setSessionMenu(open) {
      sessionMenuOpen = Boolean(open);
      elements.sessionMenuEl?.classList.toggle("hidden", !sessionMenuOpen);
      elements.sessionToggleEl?.setAttribute("aria-expanded", String(sessionMenuOpen));
      elements.documentRef?.body?.classList?.toggle("settings-home", sessionMenuOpen);
      if (sessionMenuOpen) {
        setHomeViewButtons("settings");
        setSettingsTab(settingsTab, { skipStorage: true });
        elements.documentRef?.body?.classList?.remove("task-memo-home", "knowledge-home");
        elements.documentRef?.getElementById?.("task-memo-dialog")?.removeAttribute("open");
        const knowledgeHome = elements.documentRef?.getElementById?.("knowledge-home");
        if (knowledgeHome) knowledgeHome.hidden = true;
        try {
          windowRef.localStorage?.setItem("aha.taskMemoViewExplicit", "settings");
          const url = new URL(windowRef.location.href);
          url.searchParams.set("view", "settings");
          url.searchParams.delete("selected_task_id");
          url.searchParams.delete("task_id");
          windowRef.history?.replaceState?.(windowRef.history.state, "", url);
        } catch (_err) {
          // URL/storage sync is best effort.
        }
      }
      if (!sessionMenuOpen) {
        deps.setWeixinConsoleOpen?.(false);
        deps.setPlayConsoleOpen?.(false);
        deps.setSkillsConsoleOpen?.(false);
        deps.setTokenUsageOpen?.(false);
        closeSystemSettingsPanel();
        syncHomeViewButtons();
      }
    }

    function setRunMaintenanceConsoleOpen(open, force = false) {
      runMaintenanceConsoleOpen = Boolean(open && (runMaintenanceRunId || currentRunId()) && elements.runMaintenancePopoverEl);
      if (!elements.runMaintenancePopoverEl) return;
      if (runMaintenanceConsoleOpen) {
        deps.setWeixinConsoleOpen?.(false);
        deps.setPlayConsoleOpen?.(false);
        deps.setSkillsConsoleOpen?.(false);
        deps.setTokenUsageOpen?.(false);
      } else {
        runMaintenanceRunId = "";
      }
      const maintenanceContainerEl = elements.runSettingsPanelEl || elements.runManagerEl || elements.sessionMenuEl;
      maintenanceContainerEl?.classList.toggle("maintenance-open", runMaintenanceConsoleOpen);
      elements.runMaintenancePopoverEl.hidden = !runMaintenanceConsoleOpen;
      if (runMaintenanceConsoleOpen) {
        renderRunMaintenance();
        void loadRunMaintenance(force);
      }
    }

    function bind() {
      if (elements.documentRef?.body?.classList?.contains("settings-home")) setSessionMenu(true);
      elements.sessionToggleEl?.addEventListener("click", async event => {
        event.stopPropagation();
        setSessionMenu(true);
        if (sessionMenuOpen) {
          await deps.loadRuns?.();
          void deps.loadAccessControlStatus?.();
        }
      });
      elements.sessionMenuEl?.addEventListener("click", event => event.stopPropagation());
      for (const id of ["open-task-view", "open-task-memos", "open-knowledge-base"]) {
        elements.documentRef?.getElementById?.(id)?.addEventListener("click", () => setSessionMenu(false));
      }
      const settingsTabButtons = elements.sessionMenuEl?.querySelectorAll?.("button.settings-home-tab[data-settings-tab]") || [];
      for (const button of settingsTabButtons) {
        button.addEventListener("click", event => {
          event.preventDefault();
          event.stopPropagation();
          setSettingsTab(button.getAttribute("data-settings-tab"));
        });
      }
      setSettingsTab(settingsTab, { skipStorage: true });
      elements.sessionRefreshEl?.addEventListener("click", async () => {
        await deps.loadRuns?.(true);
        if (runMaintenanceConsoleOpen) await loadRunMaintenance(true);
        await deps.loadAccessControlStatus?.();
      });
      elements.runMaintenanceRefreshEl?.addEventListener("click", () => {
        void deps.dispatchAction?.("run-maintenance-refresh");
      });
      elements.runMaintenanceCloseEl?.addEventListener("click", () => setRunMaintenanceConsoleOpen(false));
      elements.runSelectEl?.addEventListener("change", async () => deps.switchRun?.(elements.runSelectEl.value));
      const handleRunManagerClick = event => {
        const target = event.target instanceof Element ? event.target : null;
        const filterEl = target?.closest("[data-run-lifecycle-filter]");
        if (filterEl) {
          event.stopPropagation();
          runLifecycleFilter = filterEl.getAttribute("data-run-lifecycle-filter") || "active";
          renderRunLifecycleActions();
          return;
        }
        const settingsToggleEl = target?.closest("[data-run-settings-toggle]");
        if (settingsToggleEl) {
          event.preventDefault();
          event.stopPropagation();
          const runId = settingsToggleEl.getAttribute("data-run-settings-toggle") || "";
          const previousRunId = runSettingsOpenId;
          runSettingsOpenId = runSettingsOpenId === runId ? "" : runId;
          if (!runSettingsOpenId || previousRunId !== runSettingsOpenId) setRunMaintenanceConsoleOpen(false);
          renderRunLifecycleActions();
          return;
        }
        const maintenanceEl = target?.closest("[data-run-maintenance-action]");
        if (maintenanceEl) {
          event.stopPropagation();
          const action = maintenanceEl.getAttribute("data-run-maintenance-action") || "";
          void deps.dispatchAction?.("run-maintenance-action", {
            action,
            detail: {
              taskId: maintenanceEl.getAttribute("data-run-maintenance-task") || "",
              agentId: maintenanceEl.getAttribute("data-run-maintenance-agent") || "",
              archive: maintenanceEl.getAttribute("data-run-maintenance-archive") || ""
            }
          });
          return;
        }
        const maintenanceTriggerEl = target?.closest("[data-run-maintenance-run]");
        if (maintenanceTriggerEl) {
          event.stopPropagation();
          const runId = maintenanceTriggerEl.getAttribute("data-run-maintenance-run") || "";
          if (runId && !runActionInFlight()) {
            runMaintenanceRunId = runId;
            setRunMaintenanceConsoleOpen(true, true);
          }
          return;
        }
        const selectRunEl = target?.closest("[data-run-select-run]");
        if (selectRunEl) {
          event.stopPropagation();
          const runId = selectRunEl.getAttribute("data-run-select-run") || "";
          if (runId && !runActionInFlight()) {
            deps.switchRun?.(runId);
          }
          return;
        }
        const deleteEl = target?.closest("[data-run-delete-run]");
        if (deleteEl) {
          event.stopPropagation();
          const runId = deleteEl.getAttribute("data-run-delete-run") || "";
          if (runId && !runActionInFlight()) void deps.deleteRunFromMenu?.(runId);
          return;
        }
        const actionEl = target?.closest("[data-run-lifecycle-status]");
        if (!actionEl) return;
        event.stopPropagation();
        const runId = actionEl.getAttribute("data-run-lifecycle-run") || "";
        const status = actionEl.getAttribute("data-run-lifecycle-status") || "";
        void deps.updateRunLifecycleFromMenu?.(runId, status);
      };
      elements.runManagerEl?.addEventListener("click", handleRunManagerClick);
      const handleRunSettingsClick = event => {
        event.stopPropagation();
        const target = event.target instanceof Element ? event.target : null;
        const maintenanceTriggerEl = target?.closest("[data-run-maintenance-run]");
        if (maintenanceTriggerEl) {
          const runId = maintenanceTriggerEl.getAttribute("data-run-maintenance-run") || "";
          if (runId && !runActionInFlight()) {
            runMaintenanceRunId = runId;
            setRunMaintenanceConsoleOpen(true, true);
          }
          return;
        }
        const deleteEl = target?.closest("[data-run-delete-run]");
        if (deleteEl) {
          const runId = deleteEl.getAttribute("data-run-delete-run") || "";
          if (runId && !runActionInFlight()) void deps.deleteRunFromMenu?.(runId);
          return;
        }
        const actionEl = target?.closest("[data-run-lifecycle-status]");
        if (!actionEl) return;
        const runId = actionEl.getAttribute("data-run-lifecycle-run") || "";
        const status = actionEl.getAttribute("data-run-lifecycle-status") || "";
        void deps.updateRunLifecycleFromMenu?.(runId, status);
      };
      elements.runSettingsPanelEl?.addEventListener("click", handleRunSettingsClick);
      elements.runSettingsCloseEl?.addEventListener("click", event => {
        event.preventDefault();
        event.stopPropagation();
        closeRunSettings();
      });
      elements.runManagerEl?.addEventListener("submit", async event => {
        const target = event.target instanceof Element ? event.target : null;
        const form = target?.closest("#run-rename-form");
        if (!form) return;
        event.preventDefault();
        const input = form.querySelector("#rename-run-name");
        await deps.renameCurrentRun?.(input?.value || "");
      });
      elements.runRenameFormEl?.addEventListener("submit", async event => {
        event.preventDefault();
        await deps.renameCurrentRun?.(elements.renameRunNameEl?.value || "");
      });
      elements.runExportEl?.addEventListener("click", deps.exportCurrentRun);
      elements.webRestartEl?.addEventListener("click", () => {
        void deps.dispatchAction?.("web-restart");
      });
      elements.webUpgradeEl?.addEventListener("click", () => {
        void deps.dispatchAction?.("web-upgrade");
      });
      elements.authLogoutEl?.addEventListener("click", deps.logoutAuthSession);
      elements.weixinConsoleEl?.addEventListener("click", event => {
        event.stopPropagation();
        if (runActionInFlight() || !currentRunId()) return;
        const nextOpen = !deps.weixinConsoleOpen?.();
        if (nextOpen) {
          deps.setPlayConsoleOpen?.(false);
          deps.setSkillsConsoleOpen?.(false);
          deps.setTokenUsageOpen?.(false);
        }
        deps.setWeixinConsoleOpen?.(nextOpen);
      });
      elements.playConsoleEl?.addEventListener("click", event => {
        event.stopPropagation();
        if (runActionInFlight() || !currentRunId()) return;
        const nextOpen = !deps.playConsoleOpen?.();
        if (nextOpen) {
          deps.setWeixinConsoleOpen?.(false);
          deps.setSkillsConsoleOpen?.(false);
          deps.setTokenUsageOpen?.(false);
        }
        deps.setPlayConsoleOpen?.(nextOpen);
      });
      elements.skillsConsoleEl?.addEventListener("click", event => {
        event.stopPropagation();
        if (runActionInFlight()) return;
        const nextOpen = !deps.skillsConsoleOpen?.();
        if (nextOpen) {
          deps.setWeixinConsoleOpen?.(false);
          deps.setPlayConsoleOpen?.(false);
          deps.setTokenUsageOpen?.(false);
        }
        deps.setSkillsConsoleOpen?.(nextOpen);
      });
      elements.tokenUsageEl?.addEventListener("click", event => {
        event.stopPropagation();
        if (runActionInFlight() || !currentRunId()) return;
        const nextOpen = !deps.tokenUsageOpen?.();
        if (nextOpen) {
          deps.setWeixinConsoleOpen?.(false);
          deps.setPlayConsoleOpen?.(false);
          deps.setSkillsConsoleOpen?.(false);
        }
        deps.setTokenUsageOpen?.(nextOpen);
      });
      elements.runMaintenancePopoverEl?.addEventListener("click", event => {
        event.stopPropagation();
        const target = event.target instanceof Element ? event.target : null;
        const maintenanceEl = target?.closest("[data-run-maintenance-action]");
        if (maintenanceEl) {
          const action = maintenanceEl.getAttribute("data-run-maintenance-action") || "";
          void deps.dispatchAction?.("run-maintenance-action", {
            action,
            detail: {
              taskId: maintenanceEl.getAttribute("data-run-maintenance-task") || "",
              agentId: maintenanceEl.getAttribute("data-run-maintenance-agent") || "",
              archive: maintenanceEl.getAttribute("data-run-maintenance-archive") || ""
            }
          });
        }
      });
      elements.weixinConsolePopoverEl?.addEventListener("click", event => event.stopPropagation());
      elements.playConsolePopoverEl?.addEventListener("click", event => event.stopPropagation());
      elements.skillsConsolePopoverEl?.addEventListener("click", event => event.stopPropagation());
      elements.tokenUsagePopoverEl?.addEventListener("click", event => event.stopPropagation());
      elements.runImportEl?.addEventListener("click", () => {
        if (runActionInFlight()) return;
        elements.runImportFileEl?.click();
      });
      elements.runImportFileEl?.addEventListener("change", async () => {
        const file = elements.runImportFileEl.files?.[0] || null;
        elements.runImportFileEl.value = "";
        await deps.importRunArchive?.(file);
      });
      elements.openRunCreateEl?.addEventListener("click", openRunCreateDialog);
      elements.closeRunCreateEl?.addEventListener("click", closeRunCreateDialog);
      elements.cancelRunCreateEl?.addEventListener("click", closeRunCreateDialog);
      elements.runCreateDialogEl?.addEventListener("click", event => {
        if (event.target === elements.runCreateDialogEl) closeRunCreateDialog();
      });
      elements.runCreateFormEl?.addEventListener("submit", async event => {
        event.preventDefault();
        await deps.createRun?.(elements.newRunGoalEl?.value || "", "research", { createInitialTask: false });
      });
      elements.runRenameFormEl?.addEventListener("submit", async event => {
        event.preventDefault();
        await deps.renameCurrentRun?.(elements.renameRunNameEl?.value || "");
      });
      elements.documentRef?.addEventListener("click", event => {
        const target = event.target instanceof Element ? event.target : null;
        if (sessionMenuOpen && !elements.documentRef?.body?.classList?.contains("settings-home") && !elements.sessionControlEl?.contains(target)) setSessionMenu(false);
        if (elements.headerRunConsoleEl && "open" in elements.headerRunConsoleEl && elements.headerRunConsoleEl.open && !elements.headerRunConsoleEl.contains(target)) closeHeaderRunConsole();
        if (runSettingsOpenId && !elements.runManagerEl?.contains(target) && !elements.runSettingsPanelEl?.contains(target)) {
          closeRunSettings();
        }
        if (runMaintenanceConsoleOpen && !elements.runManagerEl?.contains(target) && !elements.runSettingsPanelEl?.contains(target) && !elements.runMaintenancePopoverEl?.contains(target)) {
          setRunMaintenanceConsoleOpen(false);
        }
        if (deps.weixinConsoleOpen?.() && !elements.weixinConsoleEl?.contains(target) && !elements.weixinConsolePopoverEl?.contains(target)) {
          deps.setWeixinConsoleOpen?.(false);
        }
        if (deps.playConsoleOpen?.() && !elements.playConsoleEl?.contains(target) && !elements.playConsolePopoverEl?.contains(target)) {
          deps.setPlayConsoleOpen?.(false);
        }
        if (deps.skillsConsoleOpen?.() && !elements.skillsConsoleEl?.contains(target) && !elements.skillsConsolePopoverEl?.contains(target)) {
          deps.setSkillsConsoleOpen?.(false);
        }
        if (deps.tokenUsageOpen?.() && !elements.tokenUsageEl?.contains(target) && !elements.tokenUsagePopoverEl?.contains(target)) {
          deps.setTokenUsageOpen?.(false);
        }
      });
      elements.documentRef?.addEventListener("keydown", event => {
        if (event.key === "Escape") {
          setSessionMenu(false);
          closeHeaderRunConsole();
          setRunMaintenanceConsoleOpen(false);
          closeRunCreateDialog();
          deps.setWeixinConsoleOpen?.(false);
          deps.setPlayConsoleOpen?.(false);
          deps.setSkillsConsoleOpen?.(false);
          deps.setTokenUsageOpen?.(false);
        }
      });
      renderSessionMenu();
    }

    return Object.freeze({
      bind,
      closeRunCreateDialog,
      isRunMaintenanceConsoleOpen: () => runMaintenanceConsoleOpen,
      isSessionMenuOpen: () => sessionMenuOpen,
      loadRunMaintenance,
      renderAppVersion,
      renderRunArchiveState,
      renderRunLifecycleActions,
      renderRunLifecycleState,
      renderRunMaintenance,
      renderSessionMenu,
      renderSessionSummary,
      resetRunMaintenanceState,
      runMaintenanceActionInFlight: () => runMaintenanceActionInFlight,
      runMaintenanceData: () => runMaintenanceData,
      runMaintenanceLoading: () => runMaintenanceLoading,
      runMaintenanceRunId: () => runMaintenanceRunId,
      runMaintenancePayload,
      setRunArchiveState,
      setRunMaintenanceConsoleOpen,
      setRunMaintenanceActionInFlight: value => { runMaintenanceActionInFlight = Boolean(value); },
      setRunMaintenanceData: value => { runMaintenanceData = value; },
      setRunMaintenanceMessage: value => { runMaintenanceMessage = String(value || ""); },
      setRunMaintenanceRunId: value => { runMaintenanceRunId = String(value || ""); },
      setRunLifecycleState,
      setSessionMenu,
      setWebRestartState,
      syncCurrentRunDisplay
    });
  }

  window.AHARunController = Object.freeze({ createRunController });
})();
