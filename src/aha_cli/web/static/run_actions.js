(() => {
  function sleep(ms) {
    return new Promise(resolve => window.setTimeout(resolve, ms));
  }

  function createRunActions(elements = {}, deps = {}) {
    const alertUser = deps.alert || (message => window.alert(message));
    const windowRef = deps.windowRef || window;
    const documentRef = elements.documentRef || document;
    const requestTimeoutMs = Number(deps.requestTimeoutMs || 30000);

    function t(key, fallback = "") {
      return window.AHAI18n?.t?.(key, fallback) || fallback;
    }

    function format(key, values = {}, fallback = "") {
      return window.AHAI18n?.format?.(key, values, fallback) || fallback;
    }

    function currentRunId() {
      return String(deps.currentRunId?.() || "").trim();
    }

    function setRunActionInFlight(value) {
      deps.setRunActionInFlight?.(Boolean(value));
    }

    function setWebRestartInFlight(value) {
      deps.setWebRestartInFlight?.(Boolean(value));
    }

    async function createRun(goal, mode, options = {}) {
      const trimmedGoal = String(goal || "").trim();
      if (!trimmedGoal) return;
      const selectedMode = String(mode || "research").trim() || "research";
      const collaborationMode = String(options.collaborationMode || "auto").trim() || "auto";
      const workflowTemplate = String(options.workflowTemplate || "auto").trim() || "auto";
      const createInitialTask = options.createInitialTask !== false;
      const body = {
        goal: trimmedGoal,
        mode: selectedMode,
        collaboration_mode: collaborationMode,
        workflow_template: workflowTemplate,
        dispatch: createInitialTask && options.dispatch !== false,
        create_initial_task: createInitialTask,
        task_titles: createInitialTask ? (options.taskTitles || [trimmedGoal]) : []
      };
      if (options.backend) body.backend = options.backend;
      if (options.workspaceId) body.workspace_id = options.workspaceId;
      if (options.workspacePath) body.workspace_path = options.workspacePath;
      if (options.proxyEnabled !== undefined) body.proxy_enabled = Boolean(options.proxyEnabled);
      if (options.httpProxy !== undefined) body.http_proxy = options.httpProxy;
      if (options.httpsProxy !== undefined) body.https_proxy = options.httpsProxy;
      if (options.noProxy !== undefined) body.no_proxy = options.noProxy;
      setRunActionInFlight(true);
      try {
        const payload = await deps.fetchJson("/api/runs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        }, "Failed to create run");
        const run = payload.run || payload;
        const nextRunId = deps.runIdOf?.(run);
        if (!nextRunId) throw new Error("New run did not include an id");
        if (elements.newRunGoalEl) elements.newRunGoalEl.value = "";
        deps.closeRunCreateDialog?.();
        const previousRunId = currentRunId();
        deps.setRunsLoaded?.(false);
        await deps.loadRuns?.(true);
        if (currentRunId() === nextRunId) {
          if (previousRunId !== nextRunId) deps.resetRunScopedState?.();
          await deps.refreshRunScopedView?.();
        } else {
          await deps.switchRun?.(nextRunId);
        }
        return nextRunId;
      } catch (err) {
        alertUser(err?.message || String(err));
        return "";
      } finally {
        setRunActionInFlight(false);
        deps.renderSessionMenu?.();
      }
    }

    async function renameCurrentRun(name) {
      const runId = currentRunId();
      const trimmedName = String(name || "").trim();
      if (!runId || !trimmedName) return;
      setRunActionInFlight(true);
      try {
        const payload = await deps.fetchJson(`/api/runs/${encodeURIComponent(runId)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: trimmedName })
        }, "Failed to rename run");
        deps.applyRunListData?.(payload);
        const run = payload.run || deps.currentRunSummary?.();
        deps.syncCurrentRunDisplay?.(run, trimmedName);
        deps.setRunsError?.("");
      } catch (err) {
        alertUser(err?.message || String(err));
      } finally {
        setRunActionInFlight(false);
        deps.renderSessionMenu?.();
      }
    }

    async function updateRunLifecycleFromMenu(runId, status) {
      const targetRunId = String(runId || "").trim();
      const nextStatus = String(status || "").trim();
      if (!targetRunId || !nextStatus || deps.runActionInFlight?.()) return;
      const run = (deps.runsData?.() || []).find(item => deps.runIdOf?.(item) === targetRunId) || null;
      const protectedReason = deps.runLifecycleProtectionReason?.(run, currentRunId());
      if (protectedReason) {
        deps.setRunLifecycleState?.(deps.runLifecycleReasonText?.(protectedReason), true);
        return;
      }
      setRunActionInFlight(true);
      deps.renderSessionMenu?.();
      try {
        const payload = await deps.fetchJson(deps.apiUrl(`/api/runs/${encodeURIComponent(targetRunId)}/lifecycle`), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: nextStatus, current_run_id: currentRunId() })
        }, "Failed to update run lifecycle");
        deps.applyRunListData?.(payload);
        deps.setRunsLoaded?.(true);
        const updatedRun = payload.run || (deps.runsData?.() || []).find(item => deps.runIdOf?.(item) === targetRunId) || null;
        deps.setRunLifecycleState?.(format("run.lifecycle_result", {
          title: deps.runTitleOf?.(updatedRun || { id: targetRunId }),
          lifecycle: deps.runLifecycleLabel?.(updatedRun)
        }, `${deps.runTitleOf?.(updatedRun || { id: targetRunId })} lifecycle=${deps.runLifecycleLabel?.(updatedRun)}`));
      } catch (err) {
        deps.setRunLifecycleState?.(err?.message || String(err), true);
      } finally {
        setRunActionInFlight(false);
        deps.renderSessionMenu?.();
      }
    }

    async function deleteRunFromMenu(runId) {
      const targetRunId = String(runId || "").trim();
      if (!targetRunId || deps.runActionInFlight?.()) return;
      const run = (deps.runsData?.() || []).find(item => deps.runIdOf?.(item) === targetRunId) || null;
      const protectedReason = deps.runDeleteProtectionReason?.(run, currentRunId());
      if (protectedReason) {
        deps.setRunLifecycleState?.(deps.runLifecycleReasonText?.(protectedReason), true);
        return;
      }
      const title = deps.runTitleOf?.(run || { id: targetRunId }) || targetRunId;
      const confirmed = await deps.confirmDialogAction?.({
        title: format("run.delete_confirm_title", { run: title }, `Delete ${title}?`),
        message: format(
          "run.delete_confirm_message",
          { run: title },
          "Delete this run and its local AHA data. This cannot be undone."
        ),
        details: [
          [t("run.current", "Run"), title],
          ["ID", targetRunId]
        ],
        confirmLabel: t("run.delete", "Delete"),
        danger: true
      });
      if (!confirmed) return;
      setRunActionInFlight(true);
      deps.renderSessionMenu?.();
      try {
        const payload = await deps.fetchJson(
          deps.apiUrl(`/api/runs/${encodeURIComponent(targetRunId)}`, {
            current_run_id: currentRunId(),
            force: "1"
          }, { runScoped: false }),
          { method: "DELETE" },
          t("run.delete_failed", "Failed to delete run")
        );
        deps.applyRunListData?.(payload);
        deps.setRunsLoaded?.(true);
        deps.setRunLifecycleState?.(format("run.delete_result", { run: title }, `Deleted ${title}`));
      } catch (err) {
        deps.setRunLifecycleState?.(err?.message || String(err), true);
      } finally {
        setRunActionInFlight(false);
        deps.renderSessionMenu?.();
      }
    }

    function exportCurrentRun() {
      const runId = currentRunId();
      if (!runId) {
        alertUser(t("run.none", "No run selected"));
        return;
      }
      const includeLogs = Boolean(elements.runExportLogsEl?.checked);
      const link = documentRef.createElement("a");
      link.href = deps.apiUrl("/api/run/export", { no_logs: includeLogs ? "0" : "1" });
      link.download = `aha-run-${runId}.tar.gz`;
      link.style.display = "none";
      documentRef.body.appendChild(link);
      link.click();
      link.remove();
      deps.setRunArchiveState?.(includeLogs ? t("run.archive_export_started_logs", "Export started with logs") : t("run.archive_export_started_no_logs", "Export started without raw logs"));
    }

    async function importRunArchive(file) {
      if (!file) return;
      const form = new FormData();
      form.append("archive", file);
      setRunActionInFlight(true);
      deps.setRunArchiveState?.(t("run.importing", "Importing..."));
      deps.renderSessionMenu?.();
      try {
        const response = await deps.fetchWithTimeout(
          deps.apiUrl("/api/run/import"),
          { method: "POST", body: form },
          Math.max(requestTimeoutMs, 60000)
        );
        const payload = await deps.readJsonResponse(response, t("run.import_failed", "Import failed"));
        const nextRunId = String(payload.imported_run_id || deps.runIdOf?.(payload.run) || "").trim();
        if (Array.isArray(payload.runs)) {
          deps.applyRunListData?.({ default_run_id: deps.defaultRunId?.(), runs: payload.runs });
          deps.setRunsLoaded?.(true);
          deps.renderSessionMenu?.();
        } else {
          deps.setRunsLoaded?.(false);
          await deps.loadRuns?.(true);
        }
        if (nextRunId) await deps.switchRun?.(nextRunId);
        deps.setRunArchiveState?.(nextRunId ? format("run.imported", { runId: nextRunId }, `Imported ${nextRunId}`) : t("run.import_complete", "Import complete"));
      } catch (err) {
        const message = err?.message || String(err || t("run.import_failed", "Import failed"));
        deps.setRunArchiveState?.(message, true);
        alertUser(message);
      } finally {
        setRunActionInFlight(false);
        deps.renderSessionMenu?.();
      }
    }

    async function postRunMaintenanceAction(path, body, fallbackMessage, targetRunId) {
      deps.setRunMaintenanceActionInFlight?.(true);
      deps.setRunMaintenanceMessage?.(t("run.maintenance_action_running", "Running..."));
      deps.renderRunMaintenance?.();
      try {
        const payload = await deps.fetchJson(deps.apiUrl(path, {}, { runScoped: false }), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...body, current_run_id: currentRunId() })
        }, fallbackMessage);
        if (payload.retention || payload.recovery || payload.retention_archives) {
          deps.setRunMaintenanceData?.({ ...(deps.runMaintenanceData?.() || {}), ...payload });
          deps.setRunMaintenanceRunId?.(payload.run_id || targetRunId);
        }
        deps.setRunMaintenanceMessage?.(t("run.maintenance_action_done", "Done"));
        await deps.loadRunMaintenance?.(true);
      } catch (err) {
        deps.setRunMaintenanceMessage?.(err?.message || String(err || fallbackMessage || t("run.maintenance_action_failed", "Action failed")));
      } finally {
        deps.setRunMaintenanceActionInFlight?.(false);
        deps.renderRunMaintenance?.();
      }
    }

    async function runMaintenanceAction(action, detail = {}) {
      const runId = deps.runMaintenanceRunId?.() || currentRunId();
      if (!runId || deps.runMaintenanceActionInFlight?.()) return;
      const encodedRunId = encodeURIComponent(runId);
      const confirmPayload = deps.runMaintenanceActionConfirm?.(action, detail, deps.runMaintenancePayload?.() || {}, {
        runId,
        formatBytes: deps.formatMetricBytes
      });
      if (!confirmPayload || !await deps.confirmDialogAction?.(confirmPayload)) return;
      if (action === "archive" || action === "compact") {
        const force = action === "compact";
        await postRunMaintenanceAction(
          `/api/runs/${encodedRunId}/retention`,
          {
            action,
            force,
            confirm: force ? "delete archived originals" : "archive"
          },
          "Retention action failed",
          runId
        );
      } else if (action === "recover") {
        const taskId = String(detail.taskId || "").trim();
        const agentId = String(detail.agentId || "").trim();
        if (!taskId || !agentId) return;
        await postRunMaintenanceAction(
          `/api/runs/${encodedRunId}/recovery`,
          {
            task_id: taskId,
            agent_id: agentId,
            confirm: "recover stale agent"
          },
          "Stale recovery failed",
          runId
        );
      } else if (action === "restore-archive") {
        const archive = String(detail.archive || "").trim();
        if (!archive) return;
        await postRunMaintenanceAction(
          `/api/runs/${encodedRunId}/retention-archive/restore`,
          {
            archive,
            confirm: "restore archive"
          },
          "Archive restore failed",
          runId
        );
      }
    }

    async function restartWebService() {
      if (deps.webRestartInFlight?.()) return;
      if (!currentRunId()) {
        alertUser(t("run.none", "No run selected"));
        return;
      }
      const restartVersion = deps.currentAppVersion?.();
      setWebRestartInFlight(true);
      deps.setWebRestartState?.(t("run.restart_scheduling", "Scheduling restart..."));
      deps.renderSessionMenu?.();
      try {
        await deps.fetchJson(deps.apiUrl("/api/web/restart"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({})
        }, t("run.restart_failed", "Failed to restart Web"));
        deps.setWebRestartState?.(t("run.restart_waiting", "Restart requested. Waiting for recovery..."));
        waitForWebRestartAndReload(restartVersion);
      } catch (err) {
        setWebRestartInFlight(false);
        deps.setWebRestartState?.(err?.message || String(err || t("run.restart_failed", "Failed to restart Web")), true);
        deps.renderSessionMenu?.();
      }
    }

    async function upgradeWebService() {
      if (deps.webRestartInFlight?.()) return;
      if (!currentRunId()) {
        alertUser(t("run.none", "No run selected"));
        return;
      }
      const restartVersion = deps.currentAppVersion?.();
      setWebRestartInFlight(true);
      deps.setWebRestartState?.(t("run.upgrade_scheduling", "Starting upgrade..."));
      deps.renderSessionMenu?.();
      try {
        await deps.fetchJson(deps.apiUrl("/api/web/upgrade"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({})
        }, t("run.upgrade_failed", "Failed to upgrade Web"));
        deps.setWebRestartState?.(t("run.upgrade_waiting", "Upgrade started. Waiting for recovery..."));
        waitForWebRestartAndReload(restartVersion, {
          completeMessage: t("run.upgrade_complete", "Upgrade complete."),
          manualMessage: t("run.upgrade_manual", "Upgrade started. Check the upgrade log or start the service manually if the page does not recover.")
        });
      } catch (err) {
        setWebRestartInFlight(false);
        deps.setWebRestartState?.(err?.message || String(err || t("run.upgrade_failed", "Failed to upgrade Web")), true);
        deps.renderSessionMenu?.();
      }
    }

    async function waitForWebRestartAndReload(restartVersion = "", options = {}) {
      const completeMessage = options.completeMessage || t("run.restart_complete", "Restart complete.");
      const manualMessage = options.manualMessage || t("run.restart_manual", "Restart requested. Start the service manually if the page does not recover.");
      await sleep(500);
      const deadline = Date.now() + 15000;
      while (Date.now() < deadline) {
        try {
          const response = await deps.fetchWithTimeout(deps.apiUrl("/api/bootstrap"), { cache: "no-store" }, 2000);
          if (response.ok) {
            const payload = await deps.readJsonResponse(response, "Failed to bootstrap AHA");
            const nextVersion = String(payload?.aha_version || "").trim();
            if (nextVersion && nextVersion !== restartVersion) {
              windowRef.location.reload();
              return;
            }
            deps.applyBootstrapPayload?.(payload);
            deps.renderSchedulerResetFailures?.();
            deps.resetEventWebSocketReconnectState?.("web_restart_recovered");
            try {
              await deps.syncRealtimeEvents?.();
            } catch (err) {
              console.warn("Failed to reopen websocket after web restart", err);
            }
            void refreshAfterWebRestart();
            setWebRestartInFlight(false);
            deps.setWebRestartState?.(completeMessage);
            deps.renderSessionMenu?.();
            deps.renderPanelForRealtime?.();
            return;
          }
        } catch (_err) {
          // The socket is expected to drop while the web service restarts.
        }
        await sleep(1000);
      }
      setWebRestartInFlight(false);
      deps.setWebRestartState?.(manualMessage);
      deps.renderSessionMenu?.();
    }

    async function refreshAfterWebRestart() {
      try {
        const accepted = await deps.catchUpRealtimeEvents?.();
        if (accepted?.length) deps.renderPanelForRealtime?.();
      } catch (err) {
        console.warn("Failed to catch up realtime after web restart", err);
      }
      try {
        await deps.loadStatus?.({ forceAgents: true });
      } catch (err) {
        console.warn("Failed to refresh status after web restart", err);
      }
      deps.renderPanelForRealtime?.();
    }

    return Object.freeze({
      createRun,
      exportCurrentRun,
      importRunArchive,
      renameCurrentRun,
      deleteRunFromMenu,
      restartWebService,
      upgradeWebService,
      runMaintenanceAction,
      updateRunLifecycleFromMenu,
      waitForWebRestartAndReload,
      refreshAfterWebRestart
    });
  }

  window.AHARunActions = Object.freeze({ createRunActions });
})();
