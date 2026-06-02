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
      renderAppVersion();
      const run = deps.currentRunSummary?.() || deps.fallbackCurrentRun?.();
      const statusData = deps.statusData?.();
      const bootstrapData = deps.bootstrapData?.();
      const runId = currentRunId() || deps.runIdOf?.(run) || "";
      if (!run && !statusData) {
        if (elements.sessionTitleEl) elements.sessionTitleEl.textContent = currentRunId() || "未选择 Run";
        if (elements.runIdEl) elements.runIdEl.textContent = currentRunId() || "-";
        if (elements.runStateEl) elements.runStateEl.textContent = bootstrapData?.aha_home ? `AHA_HOME ${bootstrapData.aha_home}` : "";
        if (elements.sessionDetailTextEl) elements.sessionDetailTextEl.textContent = "创建 Run 后开始";
        renderRunLifecycleBadge(null);
        if (elements.taskRunContextEl) elements.taskRunContextEl.textContent = "当前没有 Run";
        return;
      }
      const title = statusData?.goal || deps.runTitleOf?.(run) || "";
      if (elements.sessionTitleEl) {
        elements.sessionTitleEl.textContent = title || "未选择 Run";
        elements.sessionTitleEl.title = title || "";
      }
      if (elements.sessionToggleEl) {
        const label = runId ? `Run 操作台: ${title || runId}` : "Run 操作台";
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
          run?.status ? `状态 ${run.status}` : "",
          taskCount,
          deps.realtimeTransportText?.(),
          deps.runsError?.() ? `提示 ${deps.runsError?.()}` : ""
        ].filter(Boolean).join(" · ") || "Run 详情";
      }
      if (elements.taskRunContextEl) {
        const mode = statusData?.mode || run?.mode || "-";
        const runLabel = title || runId || "-";
        elements.taskRunContextEl.textContent = `当前 Run: ${runLabel} / ${mode}`;
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
        renderRunLifecycleState();
        return;
      }
      elements.runLifecycleActionsEl.innerHTML = deps.runLifecycleRowsHtml?.(view.rows || []) || "";
      renderRunLifecycleState();
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
      const runId = currentRunId();
      if (!runMaintenanceData || runMaintenanceRunId !== runId) return null;
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
      if (!elements.runMaintenanceSummaryEl || !elements.runMaintenanceDetailEl) return;
      if (!currentRunId()) {
        elements.runMaintenanceSummaryEl.textContent = "未选择 Run";
        elements.runMaintenanceSummaryEl.classList.remove("error");
        elements.runMaintenanceDetailEl.innerHTML = "";
        return;
      }
      if (runMaintenanceLoading) {
        elements.runMaintenanceSummaryEl.textContent = "诊断中...";
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
        elements.runMaintenanceSummaryEl.textContent = "未诊断";
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
      const runId = currentRunId();
      if (!runId) {
        resetRunMaintenanceState();
        renderRunMaintenance();
        return;
      }
      if (!force && runMaintenancePayload()) {
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
        if (runId !== currentRunId()) return;
        runMaintenanceData = payload;
      } catch (err) {
        if (runId !== currentRunId()) return;
        runMaintenanceData = null;
        runMaintenanceError = err?.message || String(err || "诊断不可用");
      } finally {
        if (runId === currentRunId()) {
          runMaintenanceLoading = false;
          renderRunMaintenance();
        }
      }
    }

    function renderSessionMenu() {
      if (!elements.runSelectEl) return;
      const fallback = deps.fallbackCurrentRun?.();
      const rawRuns = deps.runsData?.();
      const loadedRuns = Array.isArray(rawRuns) ? rawRuns : [];
      const runs = loadedRuns.length ? loadedRuns : (fallback ? [fallback] : []);
      const currentRun = deps.currentRunSummary?.() || fallback;
      const runId = currentRunId();
      const actionInFlight = runActionInFlight();
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
      const hasRun = Boolean(runId);
      if (elements.runExportEl) elements.runExportEl.disabled = actionInFlight || !hasRun;
      if (elements.runImportEl) elements.runImportEl.disabled = actionInFlight;
      if (elements.runExportLogsEl) elements.runExportLogsEl.disabled = actionInFlight || !hasRun;
      if (elements.runImportFileEl) elements.runImportFileEl.disabled = actionInFlight;
      if (elements.webRestartEl) elements.webRestartEl.disabled = Boolean(deps.webRestartInFlight?.()) || !hasRun;
      if (elements.runMaintenanceConsoleEl) elements.runMaintenanceConsoleEl.disabled = actionInFlight || !hasRun;
      if (elements.weixinConsoleEl) elements.weixinConsoleEl.disabled = actionInFlight || !hasRun;
      if (elements.playConsoleEl) elements.playConsoleEl.disabled = actionInFlight || !hasRun;
      if (elements.runMaintenanceRefreshEl) {
        elements.runMaintenanceRefreshEl.disabled = runMaintenanceLoading || runMaintenanceActionInFlight || actionInFlight || !hasRun;
      }
      if (!hasRun || actionInFlight) {
        setRunMaintenanceConsoleOpen(false);
        deps.setWeixinConsoleOpen?.(false);
        deps.setPlayConsoleOpen?.(false);
      } else if (runMaintenanceConsoleOpen && elements.runMaintenancePopoverEl) {
        renderRunMaintenance();
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

    function setSessionMenu(open) {
      sessionMenuOpen = Boolean(open);
      elements.sessionMenuEl?.classList.toggle("hidden", !sessionMenuOpen);
      elements.sessionToggleEl?.setAttribute("aria-expanded", String(sessionMenuOpen));
      if (!sessionMenuOpen) {
        setRunMaintenanceConsoleOpen(false);
        deps.setWeixinConsoleOpen?.(false);
        deps.setPlayConsoleOpen?.(false);
      }
    }

    function setRunMaintenanceConsoleOpen(open) {
      runMaintenanceConsoleOpen = Boolean(open && currentRunId() && elements.runMaintenancePopoverEl);
      if (!elements.runMaintenancePopoverEl) return;
      if (runMaintenanceConsoleOpen) {
        deps.setWeixinConsoleOpen?.(false);
        deps.setPlayConsoleOpen?.(false);
      }
      elements.sessionMenuEl?.classList.toggle("maintenance-open", runMaintenanceConsoleOpen);
      elements.runMaintenancePopoverEl.hidden = !runMaintenanceConsoleOpen;
      if (runMaintenanceConsoleOpen) {
        renderRunMaintenance();
        void loadRunMaintenance(false);
      }
      elements.runMaintenanceConsoleEl?.setAttribute("aria-expanded", String(runMaintenanceConsoleOpen));
    }

    function bind() {
      elements.sessionToggleEl?.addEventListener("click", async event => {
        event.stopPropagation();
        setSessionMenu(!sessionMenuOpen);
        if (sessionMenuOpen) {
          await deps.loadRuns?.();
          void deps.loadAccessControlStatus?.();
        }
      });
      elements.sessionMenuEl?.addEventListener("click", event => event.stopPropagation());
      elements.sessionRefreshEl?.addEventListener("click", async () => {
        await deps.loadRuns?.(true);
        if (runMaintenanceConsoleOpen) await loadRunMaintenance(true);
        await deps.loadAccessControlStatus?.();
      });
      elements.runMaintenanceConsoleEl?.addEventListener("click", event => {
        event.stopPropagation();
        if (runActionInFlight() || !currentRunId()) return;
        setRunMaintenanceConsoleOpen(!runMaintenanceConsoleOpen);
      });
      elements.runMaintenanceRefreshEl?.addEventListener("click", () => {
        void deps.dispatchAction?.("run-maintenance-refresh");
      });
      elements.runMaintenanceCloseEl?.addEventListener("click", () => setRunMaintenanceConsoleOpen(false));
      elements.runSelectEl?.addEventListener("change", async () => deps.switchRun?.(elements.runSelectEl.value));
      elements.sessionMenuEl?.addEventListener("click", event => {
        const target = event.target instanceof Element ? event.target : null;
        const filterEl = target?.closest("[data-run-lifecycle-filter]");
        if (filterEl) {
          runLifecycleFilter = filterEl.getAttribute("data-run-lifecycle-filter") || "active";
          renderRunLifecycleActions();
          return;
        }
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
          return;
        }
        const actionEl = target?.closest("[data-run-lifecycle-status]");
        if (!actionEl) return;
        const runId = actionEl.getAttribute("data-run-lifecycle-run") || "";
        const status = actionEl.getAttribute("data-run-lifecycle-status") || "";
        void deps.updateRunLifecycleFromMenu?.(runId, status);
      });
      elements.runExportEl?.addEventListener("click", deps.exportCurrentRun);
      elements.webRestartEl?.addEventListener("click", () => {
        void deps.dispatchAction?.("web-restart");
      });
      elements.authLogoutEl?.addEventListener("click", deps.logoutAuthSession);
      elements.weixinConsoleEl?.addEventListener("click", event => {
        event.stopPropagation();
        if (runActionInFlight() || !currentRunId()) return;
        deps.setWeixinConsoleOpen?.(!deps.weixinConsoleOpen?.());
      });
      elements.playConsoleEl?.addEventListener("click", event => {
        event.stopPropagation();
        if (runActionInFlight() || !currentRunId()) return;
        deps.setPlayConsoleOpen?.(!deps.playConsoleOpen?.());
      });
      elements.runMaintenancePopoverEl?.addEventListener("click", event => event.stopPropagation());
      elements.weixinConsolePopoverEl?.addEventListener("click", event => event.stopPropagation());
      elements.playConsolePopoverEl?.addEventListener("click", event => event.stopPropagation());
      elements.runImportEl?.addEventListener("click", () => {
        if (runActionInFlight()) return;
        elements.runImportFileEl?.click();
      });
      elements.runImportFileEl?.addEventListener("change", async () => {
        const file = elements.runImportFileEl.files?.[0] || null;
        elements.runImportFileEl.value = "";
        await deps.importRunArchive?.(file);
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
        if (sessionMenuOpen && !elements.sessionControlEl?.contains(target)) setSessionMenu(false);
        if (runMaintenanceConsoleOpen && !elements.runMaintenanceConsoleEl?.contains(target) && !elements.runMaintenancePopoverEl?.contains(target)) {
          setRunMaintenanceConsoleOpen(false);
        }
        if (deps.weixinConsoleOpen?.() && !elements.weixinConsoleEl?.contains(target) && !elements.weixinConsolePopoverEl?.contains(target)) {
          deps.setWeixinConsoleOpen?.(false);
        }
        if (deps.playConsoleOpen?.() && !elements.playConsoleEl?.contains(target) && !elements.playConsolePopoverEl?.contains(target)) {
          deps.setPlayConsoleOpen?.(false);
        }
      });
      elements.documentRef?.addEventListener("keydown", event => {
        if (event.key === "Escape") {
          setSessionMenu(false);
          setRunMaintenanceConsoleOpen(false);
          deps.setWeixinConsoleOpen?.(false);
          deps.setPlayConsoleOpen?.(false);
        }
      });
      renderSessionMenu();
    }

    return Object.freeze({
      bind,
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
