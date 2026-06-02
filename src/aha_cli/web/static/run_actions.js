(() => {
  function sleep(ms) {
    return new Promise(resolve => window.setTimeout(resolve, ms));
  }

  function createRunActions(elements = {}, deps = {}) {
    const alertUser = deps.alert || (message => window.alert(message));
    const windowRef = deps.windowRef || window;
    const documentRef = elements.documentRef || document;
    const requestTimeoutMs = Number(deps.requestTimeoutMs || 12000);

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
        const previousRunId = currentRunId();
        deps.setRunsLoaded?.(false);
        await deps.loadRuns?.(true);
        if (currentRunId() === nextRunId) {
          if (previousRunId !== nextRunId) deps.resetRunScopedState?.();
          await deps.refreshRunScopedView?.();
        } else {
          await deps.switchRun?.(nextRunId);
        }
      } catch (err) {
        alertUser(err?.message || String(err));
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
          body: JSON.stringify({ status: nextStatus })
        }, "Failed to update run lifecycle");
        deps.applyRunListData?.(payload);
        deps.setRunsLoaded?.(true);
        const updatedRun = payload.run || (deps.runsData?.() || []).find(item => deps.runIdOf?.(item) === targetRunId) || null;
        deps.setRunLifecycleState?.(`${deps.runTitleOf?.(updatedRun || { id: targetRunId })} lifecycle=${deps.runLifecycleLabel?.(updatedRun)}`);
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
        alertUser("请先选择 Run");
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
      deps.setRunArchiveState?.(includeLogs ? "导出已开始，包含日志" : "导出已开始，未包含原始日志");
    }

    async function importRunArchive(file) {
      if (!file) return;
      const form = new FormData();
      form.append("archive", file);
      setRunActionInFlight(true);
      deps.setRunArchiveState?.("正在导入...");
      deps.renderSessionMenu?.();
      try {
        const response = await deps.fetchWithTimeout(
          deps.apiUrl("/api/run/import"),
          { method: "POST", body: form },
          Math.max(requestTimeoutMs, 60000)
        );
        const payload = await deps.readJsonResponse(response, "导入失败");
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
        deps.setRunArchiveState?.(nextRunId ? `已导入 ${nextRunId}` : "导入完成");
      } catch (err) {
        const message = err?.message || String(err || "导入失败");
        deps.setRunArchiveState?.(message, true);
        alertUser(message);
      } finally {
        setRunActionInFlight(false);
        deps.renderSessionMenu?.();
      }
    }

    async function postRunMaintenanceAction(path, body, fallbackMessage) {
      deps.setRunMaintenanceActionInFlight?.(true);
      deps.setRunMaintenanceMessage?.("执行中...");
      deps.renderRunMaintenance?.();
      try {
        const payload = await deps.fetchJson(deps.apiUrl(path, {}, { runScoped: false }), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        }, fallbackMessage);
        if (payload.retention || payload.recovery || payload.retention_archives) {
          deps.setRunMaintenanceData?.({ ...(deps.runMaintenanceData?.() || {}), ...payload });
          deps.setRunMaintenanceRunId?.(payload.run_id || currentRunId());
        }
        deps.setRunMaintenanceMessage?.("完成");
        await deps.loadRunMaintenance?.(true);
      } catch (err) {
        deps.setRunMaintenanceMessage?.(err?.message || String(err || "执行失败"));
      } finally {
        deps.setRunMaintenanceActionInFlight?.(false);
        deps.renderRunMaintenance?.();
      }
    }

    async function runMaintenanceAction(action, detail = {}) {
      const runId = currentRunId();
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
          "Retention action failed"
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
          "Stale recovery failed"
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
          "Archive restore failed"
        );
      }
    }

    async function restartWebService() {
      if (deps.webRestartInFlight?.()) return;
      if (!currentRunId()) {
        alertUser("请先选择 Run");
        return;
      }
      const restartVersion = deps.currentAppVersion?.();
      setWebRestartInFlight(true);
      deps.setWebRestartState?.("正在安排重启...");
      deps.renderSessionMenu?.();
      try {
        await deps.fetchJson(deps.apiUrl("/api/web/restart"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({})
        }, "重启 Web 失败");
        deps.setWebRestartState?.("已请求重启，等待恢复...");
        waitForWebRestartAndReload(restartVersion);
      } catch (err) {
        setWebRestartInFlight(false);
        deps.setWebRestartState?.(err?.message || String(err || "重启 Web 失败"), true);
        deps.renderSessionMenu?.();
      }
    }

    async function waitForWebRestartAndReload(restartVersion = "") {
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
            deps.setWebRestartState?.("重启完成。");
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
      deps.setWebRestartState?.("已请求重启，若页面未恢复请手动启动。");
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
      restartWebService,
      runMaintenanceAction,
      updateRunLifecycleFromMenu,
      waitForWebRestartAndReload,
      refreshAfterWebRestart
    });
  }

  window.AHARunActions = Object.freeze({ createRunActions });
})();
