(() => {
  function realtimeReadyStateName(socket, WebSocketImpl = globalThis.WebSocket) {
    if (!socket || typeof WebSocketImpl === "undefined") return "none";
    if (socket.readyState === WebSocketImpl.CONNECTING) return "connecting";
    if (socket.readyState === WebSocketImpl.OPEN) return "open";
    if (socket.readyState === WebSocketImpl.CLOSING) return "closing";
    if (socket.readyState === WebSocketImpl.CLOSED) return "closed";
    return String(socket.readyState);
  }

  function realtimeTransportText(state = {}) {
    if (!state.currentRunId) return "realtime Disconnected";
    if (state.wsDisabled || !state.webSocketAvailable) return "realtime Polling";
    if (state.eventSocketState === "open") return "realtime WebSocket";
    if (state.eventSocketState === "connecting") return "realtime Connecting";
    if (state.eventSocketState === "stale") return "realtime Reconnecting (polling)";
    if (state.reconnectPending) return "realtime Reconnecting (polling)";
    if (state.eventSocketState === "polling") return "realtime Polling fallback";
    if (state.eventSocketState === "error") return "realtime Polling fallback";
    if (state.eventSocketState === "closed") return "realtime Disconnected";
    return "realtime WebSocket pending";
  }

  function realtimeReconnectDelayMs(state = {}) {
    const failureCount = Math.max(1, Number(state.failureCount || 1));
    const pollInterval = Math.max(250, Number(state.pollInterval || 1000));
    const maxDelay = Math.max(pollInterval, Number(state.maxDelay || 30000));
    const multiplier = 2 ** Math.min(failureCount - 1, 5);
    return Math.min(maxDelay, pollInterval * multiplier);
  }

  function realtimeStaleFallbackDue(state = {}) {
    if (state.wsDisabled || !state.webSocketAvailable) return false;
    if (!state.socketOpen) return false;
    if (!state.lastMessageAt) return false;
    const now = Number(state.now || Date.now());
    const staleMs = Math.max(5000, Number(state.staleMs || 15000));
    const pollInterval = Math.max(250, Number(state.pollInterval || 1000));
    const lastMessageAt = Number(state.lastMessageAt || 0);
    const lastFallbackPollAt = Number(state.lastFallbackPollAt || 0);
    const staleAfterMs = Math.max(3000, Math.min(staleMs / 2, pollInterval * 5));
    const minFallbackGapMs = Math.max(1000, pollInterval);
    return now - lastMessageAt >= staleAfterMs && now - lastFallbackPollAt >= minFallbackGapMs;
  }

  window.AHARealtimeState = Object.freeze({
    realtimeReadyStateName,
    realtimeTransportText,
    realtimeReconnectDelayMs,
    realtimeStaleFallbackDue
  });
})();
