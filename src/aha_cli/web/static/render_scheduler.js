(() => {
  function createRenderScheduler(options = {}) {
    let tickInFlight = false;
    let failureCount = 0;
    let backoffUntil = 0;

    function resetFailures() {
      failureCount = 0;
      backoffUntil = 0;
    }

    function recordFailure() {
      failureCount += 1;
      const multiplier = 2 ** Math.min(failureCount - 1, 5);
      backoffUntil = Date.now() + Math.min(30000, options.pollInterval * multiplier);
    }

    async function tick() {
      if (tickInFlight || options.actionInFlight?.() || Date.now() < backoffUntil) return;
      if (options.authRequired?.()) return;
      if (options.bootstrapError?.()) return;
      if (!options.currentRunId?.()) {
        options.renderFirstRunState?.();
        return;
      }
      tickInFlight = true;
      try {
        await options.loadStatus?.();
        options.renderPanelForRealtime?.();
        await options.ensureConversationLoaded?.();
        await options.maybeRefreshConversationBackendSessionFallback?.();
        const autoFlushResponse = await options.maybeAutoFlushPending?.();
        if (autoFlushResponse) {
          await options.loadStatus?.({ forceAgents: true });
        }
        await options.syncRealtimeEvents?.({ allowStalePoll: Boolean(options.selectedTaskRealtimeActive?.()) });
        resetFailures();
        options.renderPendingMessages?.();
        options.renderPanelForRealtime?.();
      } catch (err) {
        if (options.isAuthRequiredError?.(err)) {
          options.renderLoginState?.("登录已失效，请重新输入 token。", true);
          return;
        }
        recordFailure();
        options.renderError?.(err);
      } finally {
        tickInFlight = false;
      }
    }

    function renderActiveTurn() {
      const task = options.selectedTask?.();
      const turn = task ? options.latestTurnTiming?.(task.id) : null;
      if ((task && options.taskActivityStatus?.(task) !== "idle") || turn?.running) {
        options.renderTaskList?.();
        options.renderSelectedHeader?.();
        options.renderPanelForRealtime?.();
      }
    }

    return Object.freeze({
      renderActiveTurn,
      resetFailures,
      tick
    });
  }

  window.AHARenderScheduler = Object.freeze({ createRenderScheduler });
})();
