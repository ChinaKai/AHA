(() => {
  function createRealtimeClient(options = {}) {
    const queryParams = options.queryParams || new URLSearchParams();
    const pollInterval = Number(options.pollInterval) || 1000;
    const staleMs = Number(options.staleMs) || 15000;
    const wsConfig = String(options.wsConfig || "").trim();
    const wsDisabled = Boolean(options.wsDisabled);
    const debugEnabled = Boolean(options.debugEnabled);
    const realtimeReadyStateName = options.realtimeReadyStateName || (() => "unavailable");
    const realtimeTransportLabel = options.realtimeTransportLabel || (() => "");
    const realtimeReconnectDelayMs = options.realtimeReconnectDelayMs || (() => pollInterval);
    const realtimeStaleFallbackDue = options.realtimeStaleFallbackDue || (() => false);
    let eventSocket = null;
    let eventSocketState = "idle";
    let eventSocketFailureCount = 0;
    let eventSocketReconnectAt = 0;
    let realtimeCatchupPromise = null;
    let realtimeCatchupRequested = false;
    let lastRealtimeMessageAt = 0;
    let lastRealtimeFallbackPollAt = 0;
    let realtimeDebugSeq = 0;

    function currentRunId() {
      return String(options.currentRunId?.() || "").trim();
    }

    function selectedTaskId() {
      return String(options.selectedTaskId?.() || "").trim();
    }

    function lastEventId() {
      return String(options.lastEventId?.() || "").trim();
    }

    function eventTailInitialized() {
      return Boolean(options.eventTailInitialized?.());
    }

    function webSocketAvailable() {
      return typeof WebSocket !== "undefined";
    }

    function readyStateName() {
      return realtimeReadyStateName(eventSocket, webSocketAvailable() ? WebSocket : undefined);
    }

    function debug(stage, detail = {}) {
      const context = typeof options.debugContext === "function" ? options.debugContext() : {};
      const payload = {
        seq: ++realtimeDebugSeq,
        stage,
        run_id: currentRunId(),
        selected_task_id: selectedTaskId(),
        last_event_id: lastEventId(),
        ws_state: eventSocketState,
        ws_ready_state: readyStateName(),
        ...context,
        ...detail
      };
      if (!debugEnabled) return payload;
      console.info("[AHA realtime]", payload);
      options.sendDebugPayload?.(payload);
      return payload;
    }

    function transportText() {
      return realtimeTransportLabel({
        wsDisabled,
        webSocketAvailable: webSocketAvailable(),
        eventSocketState,
        reconnectPending: Date.now() < eventSocketReconnectAt
      });
    }

    function webSocketSupported() {
      return !wsDisabled && currentRunId() && webSocketAvailable();
    }

    function close() {
      const socket = eventSocket;
      eventSocket = null;
      eventSocketState = "closed";
      if (!socket) return false;
      debug("ws.close_local");
      if (socket.readyState !== WebSocket.CLOSED) {
        socket.onclose = null;
        socket.close();
      }
      return true;
    }

    function hasSocket() {
      return Boolean(eventSocket);
    }

    function resetReconnect(reason = "") {
      if (eventSocket) close();
      eventSocketFailureCount = 0;
      eventSocketReconnectAt = 0;
      lastRealtimeMessageAt = 0;
      lastRealtimeFallbackPollAt = 0;
      eventSocketState = "idle";
      debug("ws.reconnect_reset", { reason });
      options.refreshRealtimeIndicator?.();
    }

    function eventWebSocketBaseUrl() {
      const explicit = String(queryParams.get("ws_url") || wsConfig || "").trim();
      let explicitAbsolute = false;
      let url;
      if (explicit && !["1", "true", "on"].includes(explicit.toLowerCase())) {
        if (/^\d+$/.test(explicit)) {
          url = new URL("/ws", window.location.href);
          url.port = explicit;
        } else {
          explicitAbsolute = /^[a-z][a-z0-9+.-]*:\/\//i.test(explicit);
          url = new URL(explicit, window.location.href);
        }
      } else {
        url = new URL("/ws", window.location.href);
      }
      if (!explicitAbsolute || url.protocol === "http:" || url.protocol === "https:") {
        url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      }
      const wsPort = String(queryParams.get("ws_port") || "").trim();
      if (wsPort) url.port = wsPort;
      return url;
    }

    function eventWebSocketUrl() {
      const url = eventWebSocketBaseUrl();
      const runId = currentRunId();
      const cursor = lastEventId();
      if (runId) url.searchParams.set("run_id", runId);
      if (cursor) url.searchParams.set("last_event_id", cursor);
      url.searchParams.set("lite", "1");
      const taskLimit = Number(options.taskPageLimit || 0);
      if (taskLimit > 0) url.searchParams.set("task_limit", String(taskLimit));
      const taskFilter = options.taskVisibilityFilter?.();
      if (taskFilter) url.searchParams.set("task_filter", taskFilter);
      const taskId = selectedTaskId();
      if (taskId) url.searchParams.set("selected_task_id", taskId);
      return url.toString();
    }

    function scheduleWebSocketReconnect() {
      eventSocketFailureCount += 1;
      const delayMs = realtimeReconnectDelayMs({ failureCount: eventSocketFailureCount, pollInterval });
      eventSocketReconnectAt = Date.now() + delayMs;
      debug("ws.reconnect_scheduled", {
        failure_count: eventSocketFailureCount,
        delay_ms: delayMs
      });
    }

    function staleFallbackDue() {
      return realtimeStaleFallbackDue({
        wsDisabled,
        webSocketAvailable: webSocketAvailable(),
        socketOpen: Boolean(webSocketAvailable() && eventSocket && eventSocket.readyState === WebSocket.OPEN),
        lastMessageAt: lastRealtimeMessageAt,
        lastFallbackPollAt: lastRealtimeFallbackPollAt,
        staleMs,
        pollInterval,
        now: Date.now()
      });
    }

    function closeStaleWebSocket(reason = "stale") {
      if (!webSocketSupported() || !eventSocket || !webSocketAvailable()) return false;
      if (eventSocket.readyState !== WebSocket.OPEN || !lastRealtimeMessageAt) return false;
      const ageMs = Date.now() - lastRealtimeMessageAt;
      if (ageMs < staleMs) return false;
      const socket = eventSocket;
      eventSocket = null;
      eventSocketState = "stale";
      eventSocketReconnectAt = 0;
      lastRealtimeFallbackPollAt = 0;
      debug("ws.stale_close", { reason, age_ms: ageMs, stale_after_ms: staleMs });
      socket.onclose = null;
      try {
        socket.close(4000, "stale");
      } catch (err) {
        debug("ws.stale_close_error", { reason, error: err?.message || String(err) });
      }
      options.refreshRealtimeIndicator?.();
      return true;
    }

    function handleWebSocketMessage(message) {
      lastRealtimeMessageAt = Date.now();
      let payload;
      try {
        payload = JSON.parse(message.data);
      } catch (_err) {
        debug("ws.message.invalid_json", { raw_len: String(message.data || "").length });
        return;
      }
      debug("ws.message", {
        type: payload.type || "",
        event_type: payload.data?.type || "",
        event_id: payload.data?.event_id || ""
      });
      if (payload.type === "status") {
        options.onStatus?.(payload.data || {});
        return;
      }
      if (payload.type === "heartbeat") {
        options.onHeartbeat?.();
        options.refreshRealtimeIndicator?.();
        return;
      }
      if (payload.type === "event" && payload.data) {
        const accepted = options.onEvent?.(payload.data) || [];
        if (accepted.length) options.onAcceptedEvents?.(accepted);
      }
    }

    function openWebSocket() {
      if (!webSocketSupported()) return false;
      if (eventSocket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(eventSocket.readyState)) return true;
      try {
        const url = eventWebSocketUrl();
        debug("ws.open_request", { url });
        const socket = new WebSocket(url);
        eventSocket = socket;
        eventSocketState = "connecting";
        options.refreshRealtimeIndicator?.();
        socket.onopen = () => {
          if (eventSocket !== socket) return;
          eventSocketState = "open";
          eventSocketFailureCount = 0;
          eventSocketReconnectAt = 0;
          lastRealtimeMessageAt = Date.now();
          lastRealtimeFallbackPollAt = 0;
          debug("ws.open");
          options.refreshRealtimeIndicator?.();
        };
        socket.onmessage = message => {
          if (eventSocket === socket) handleWebSocketMessage(message);
        };
        socket.onerror = () => {
          if (eventSocket === socket) {
            eventSocketState = "error";
            debug("ws.error");
            options.refreshRealtimeIndicator?.();
            requestRealtimeCatchup();
          }
        };
        socket.onclose = event => {
          if (eventSocket !== socket) return;
          eventSocket = null;
          eventSocketState = "closed";
          debug("ws.close", { code: event.code, reason: event.reason || "", was_clean: event.wasClean });
          scheduleWebSocketReconnect();
          options.refreshRealtimeIndicator?.();
          requestRealtimeCatchup();
        };
        return true;
      } catch (err) {
        eventSocketState = "error";
        debug("ws.open_error", { error: err?.message || String(err) });
        scheduleWebSocketReconnect();
        options.refreshRealtimeIndicator?.();
        return false;
      }
    }

    async function ensureWebSocket() {
      if (!webSocketSupported()) return false;
      if (closeStaleWebSocket("ensure")) return false;
      if (eventSocket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(eventSocket.readyState)) return true;
      if (Date.now() < eventSocketReconnectAt) {
        debug("ws.reconnect_wait", { remaining_ms: eventSocketReconnectAt - Date.now() });
        return false;
      }
      if (!lastEventId() && !eventTailInitialized()) {
        try {
          await options.initializeEventTailOffset?.();
        } catch (err) {
          debug("ws.tail_before_open_error", { error: err?.message || String(err) });
          scheduleWebSocketReconnect();
          return false;
        }
      }
      return openWebSocket();
    }

    async function syncRealtimeEvents(syncOptions = {}) {
      const staleSocketClosed = closeStaleWebSocket("sync");
      const forcePoll = Boolean(syncOptions.forcePoll || staleSocketClosed);
      const staleFallback = !forcePoll && syncOptions.allowStalePoll && staleFallbackDue();
      if (!forcePoll && !staleFallback && await ensureWebSocket()) {
        debug("sync.skip_poll_ws_active", { force_poll: false, allow_stale_poll: Boolean(syncOptions.allowStalePoll) });
        return [];
      }
      debug("sync.poll", {
        force_poll: forcePoll,
        allow_stale_poll: Boolean(syncOptions.allowStalePoll),
        stale_fallback: Boolean(staleFallback),
        stale_socket_closed: Boolean(staleSocketClosed)
      });
      const accepted = await options.pollEvents?.() || [];
      if (forcePoll || staleFallback) lastRealtimeFallbackPollAt = Date.now();
      if (!wsDisabled && webSocketAvailable() && eventSocketState === "idle") eventSocketState = "polling";
      if (staleSocketClosed) {
        try {
          await ensureWebSocket();
        } catch (err) {
          debug("ws.reopen_after_stale_error", { error: err?.message || String(err) });
        }
      }
      options.refreshRealtimeIndicator?.();
      return accepted;
    }

    async function catchUpRealtimeEvents() {
      realtimeCatchupRequested = true;
      debug("catchup.request");
      if (!realtimeCatchupPromise) {
        realtimeCatchupPromise = (async () => {
          const accepted = [];
          try {
            while (realtimeCatchupRequested) {
              realtimeCatchupRequested = false;
              accepted.push(...await syncRealtimeEvents({ forcePoll: true }));
              debug("catchup.batch", { accepted_count: accepted.length });
            }
          } catch (err) {
            console.warn("Realtime catch-up failed", err);
            debug("catchup.error", { error: err?.message || String(err) });
          }
          return accepted;
        })().finally(() => {
          debug("catchup.done");
          realtimeCatchupPromise = null;
        });
      }
      return realtimeCatchupPromise;
    }

    function requestRealtimeCatchup() {
      if (!currentRunId()) return;
      debug("catchup.schedule");
      catchUpRealtimeEvents().then(accepted => {
        if (accepted.length) options.onAcceptedEvents?.(accepted);
      });
    }

    return Object.freeze({
      readyStateName,
      debug,
      transportText,
      close,
      hasSocket,
      resetReconnect,
      syncRealtimeEvents,
      catchUpRealtimeEvents,
      requestRealtimeCatchup
    });
  }

  window.AHARealtimeClient = Object.freeze({ createRealtimeClient });
})();
