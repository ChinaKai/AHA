(() => {
  function createRenderScheduler(options = {}) {
    let tickInFlight = false;
    let failureCount = 0;
    let backoffUntil = 0;
    const documentHidden = options.documentHidden || (() => false);

    function activeTurnState() {
      const task = options.selectedTask?.();
      const turn = task ? options.latestTurnTiming?.(task.id) : null;
      return {
        task,
        turn,
        active: Boolean((task && options.taskActivityStatus?.(task) !== "idle") || turn?.running)
      };
    }

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
        await options.syncRealtimeEvents?.({ allowStalePoll: Boolean(options.selectedTaskRealtimeActive?.()) });
        const realtimeConnected = Boolean(options.realtimeConnected?.());
        if (!realtimeConnected) {
          await options.loadStatus?.();
          await options.refreshTaskMemosIfOpen?.();
        }
        options.renderPanelForRealtime?.();
        await options.ensureConversationLoaded?.();
        await options.maybeRefreshConversationBackendSessionFallback?.();
        const autoFlushResponse = await options.maybeAutoFlushPending?.();
        if (autoFlushResponse) {
          await options.loadStatus?.({ forceAgents: true });
          await options.refreshTaskMemosIfOpen?.();
        }
        if (!realtimeConnected) await options.refreshTaskMemosIfOpen?.();
        resetFailures();
        options.renderPendingMessages?.();
        options.renderPanelForRealtime?.();
      } catch (err) {
        if (options.isAuthRequiredError?.(err)) {
          options.renderLoginState?.(window.AHAI18n?.t?.("auth.session_expired", "Login expired. Enter the token again."), true);
          return;
        }
        recordFailure();
        options.renderError?.(err);
      } finally {
        tickInFlight = false;
      }
    }

    function renderActiveTurn() {
      const state = activeTurnState();
      if (state.active) {
        options.renderTaskList?.();
        options.renderSelectedHeader?.();
        options.renderPanelForRealtime?.();
      }
    }

    function tickIntervalMs() {
      const baseInterval = Math.max(250, Number(options.pollInterval || 1000));
      const idleInterval = Math.max(baseInterval, Number(options.idlePollInterval || baseInterval * 4));
      const hiddenInterval = Math.max(idleInterval, Number(options.hiddenPollInterval || idleInterval * 3));
      if (documentHidden()) return hiddenInterval;
      if (activeTurnState().active || options.selectedTaskRealtimeActive?.()) return baseInterval;
      if (options.realtimeConnected?.()) return idleInterval;
      return baseInterval;
    }

    return Object.freeze({
      renderActiveTurn,
      resetFailures,
      tick,
      tickIntervalMs
    });
  }

  window.AHARenderScheduler = Object.freeze({ createRenderScheduler });
})();
