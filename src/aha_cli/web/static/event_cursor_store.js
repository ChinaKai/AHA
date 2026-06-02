(() => {
  function createEventCursorStore(state = {}, deps = {}) {
    const storage = deps.storage || window.localStorage;
    const eventIdOf = deps.eventIdOf || (event => String(event?.event_id || "").trim());
    const getCurrentRunId = state.getCurrentRunId || (() => "");
    const getLastEventId = state.getLastEventId || (() => "");
    const setLastEventId = state.setLastEventId || (() => {});
    const setOffset = state.setOffset || (() => {});
    const setEventTailInitialized = state.setEventTailInitialized || (() => {});

    function storageKey() {
      const runId = String(getCurrentRunId() || "").trim();
      return runId ? `aha:last-event-id:${runId}` : "";
    }

    function readStoredLastEventId() {
      const key = storageKey();
      if (!key) return "";
      try {
        return String(storage?.getItem(key) || "").trim();
      } catch (_err) {
        return "";
      }
    }

    function writeStoredLastEventId(value) {
      const key = storageKey();
      if (!key) return;
      try {
        const clean = String(value || "").trim();
        if (clean) {
          storage?.setItem(key, clean);
        } else {
          storage?.removeItem(key);
        }
      } catch (_err) {
        // Browser storage may be disabled; realtime still works for this page session.
      }
    }

    function clearStoredLastEventId() {
      writeStoredLastEventId("");
    }

    function restoreFromStorage() {
      const stored = readStoredLastEventId();
      setLastEventId(stored);
      setOffset(stored ? Number(stored) || -1 : -1);
      setEventTailInitialized(Boolean(stored));
    }

    function remember(payload) {
      if (Number.isFinite(payload?.offset)) setOffset(payload.offset);
      const eventId = String(payload?.last_event_id || payload?.offset || "").trim();
      if (!eventId) return;
      setLastEventId(eventId);
      writeStoredLastEventId(eventId);
    }

    function rememberFromEvent(event) {
      const eventId = eventIdOf(event);
      if (!eventId) return;
      setLastEventId(eventId);
      const numericOffset = Number(eventId);
      if (Number.isFinite(numericOffset)) setOffset(numericOffset);
      setEventTailInitialized(true);
      writeStoredLastEventId(eventId);
    }

    return Object.freeze({
      clearStoredLastEventId,
      readStoredLastEventId,
      remember,
      rememberFromEvent,
      restoreFromStorage,
      storageKey,
      writeStoredLastEventId,
      lastEventId: getLastEventId
    });
  }

  window.AHAEventCursorStore = Object.freeze({ createEventCursorStore });
})();
