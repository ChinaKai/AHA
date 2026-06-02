(() => {
  function createConversationStateHelpers(options = {}) {
    const allEvents = options.allEvents || [];
    const conversationStates = options.conversationStates || new Map();
    const conversationFilters = options.conversationFilters || {};
    const conversationFilterOptions = options.conversationFilterOptions || [];
    const selectedTaskId = options.selectedTaskId || (() => "");
    const backendTarget = options.backendTarget || (() => "main");
    const isTaskEvent = options.isTaskEvent || (() => false);
    const isTimelineEvent = options.isTimelineEvent || (() => false);
    const eventMatchesAgent = options.eventMatchesAgent || (() => false);
    const conversationEventCategory = options.conversationEventCategory || (() => "runtime");
    const conversationMetadataFilterCounts = options.conversationMetadataFilterCounts || (() => ({}));
    const dedupeConversationEvents = options.dedupeConversationEvents || (events => events || []);
    const mergeConversationEvents = options.mergeConversationEvents || ((current, incoming, prepend = false) => (
      prepend ? [...(incoming || []), ...(current || [])] : [...(current || []), ...(incoming || [])]
    ));
    const backendSessionWithPreviousContextPressure = options.backendSessionWithPreviousContextPressure || ((next, previous) => next || previous || null);

    function conversationKey(taskId = selectedTaskId(), target = backendTarget()) {
      return `${taskId || ""}::${target || "main"}`;
    }

    function activeConversationCategoryList() {
      return conversationFilterOptions
        .map(item => item.key)
        .filter(key => conversationFilters[key]);
    }

    function activeConversationCategoryKey() {
      const categories = activeConversationCategoryList();
      return categories.length ? categories.join(",") : "none";
    }

    function conversationState(taskId = selectedTaskId(), target = backendTarget()) {
      const key = conversationKey(taskId, target);
      if (!conversationStates.has(key)) {
        conversationStates.set(key, { events: [], beforeOffset: null, hasMore: true, initialized: false, loading: false, error: "", backendSession: null, categoryKey: "" });
      }
      return conversationStates.get(key);
    }

    function taskEvents(taskId) {
      return allEvents.filter(event => isTaskEvent(event, taskId));
    }

    function taskTimelineEvents(taskId) {
      return taskEvents(taskId).filter(isTimelineEvent);
    }

    function eventMatchesSelectedAgent(event) {
      return eventMatchesAgent(event, backendTarget());
    }

    function agentTimelineEvents(taskId, target = backendTarget()) {
      return taskTimelineEvents(taskId).filter(event => eventMatchesAgent(event, target));
    }

    function conversationSourceEvents(taskId, target = backendTarget()) {
      const state = conversationStates.get(conversationKey(taskId, target));
      return state?.initialized ? state.events : agentTimelineEvents(taskId, target);
    }

    function dedupedConversationEvents(taskId, target = backendTarget()) {
      return dedupeConversationEvents(conversationSourceEvents(taskId, target), target);
    }

    function taskConversationEvents(taskId) {
      return dedupedConversationEvents(taskId).filter(event => conversationFilters[conversationEventCategory(event)]);
    }

    function conversationFilterCounts(taskId) {
      return conversationMetadataFilterCounts(dedupedConversationEvents(taskId), conversationFilterOptions);
    }

    function prepareConversationStateForLoad(state, categoryKey, older = false) {
      if (!older && state.categoryKey !== categoryKey) {
        state.events = [];
        state.beforeOffset = null;
        state.hasMore = true;
        state.initialized = false;
        state.error = "";
        state.categoryKey = categoryKey;
      }
      return state;
    }

    function shouldSkipConversationLoad(state, older = false, force = false) {
      return Boolean(state.loading || (!force && !older && state.initialized) || (older && !state.hasMore));
    }

    function assignConversationKeys(events, start = 0) {
      events.forEach((event, index) => {
        const cursor = event._cursor ?? event.cursor ?? start + index;
        if (!event._uiKey) event._uiKey = `conversation-${cursor}-${event.type || "event"}`;
      });
      return events;
    }

    function applyConversationPagePayload(state, payload = {}, options = {}) {
      const older = Boolean(options.older);
      const events = assignConversationKeys([...(payload.events || []), ...(payload.turn_events || [])], payload.before_offset || 0);
      state.events = older ? mergeConversationEvents(state.events, events, true) : mergeConversationEvents(events, state.events, false);
      if (payload.backend_session) {
        state.backendSession = backendSessionWithPreviousContextPressure(payload.backend_session, state.backendSession);
      }
      state.beforeOffset = payload.next_before_offset ?? payload.before ?? null;
      state.hasMore = Boolean(payload.has_more);
      state.initialized = true;
      state.error = "";
      return { events, afterOffset: payload.after_offset };
    }

    function selectedTaskRealtimeActive(task, latestTurnTiming, selectedAgentInputBlocked, taskActivityStatus) {
      if (!task) return false;
      const turn = latestTurnTiming(task.id);
      return selectedAgentInputBlocked() || taskActivityStatus(task) !== "idle" || Boolean(turn?.running);
    }

    return Object.freeze({
      conversationKey,
      activeConversationCategoryList,
      activeConversationCategoryKey,
      conversationState,
      taskEvents,
      taskTimelineEvents,
      eventMatchesSelectedAgent,
      agentTimelineEvents,
      conversationSourceEvents,
      dedupedConversationEvents,
      taskConversationEvents,
      conversationFilterCounts,
      prepareConversationStateForLoad,
      shouldSkipConversationLoad,
      assignConversationKeys,
      applyConversationPagePayload,
      selectedTaskRealtimeActive
    });
  }

  window.AHAConversationState = Object.freeze({ createConversationStateHelpers });
})();
