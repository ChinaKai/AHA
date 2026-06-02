(() => {
  const conversationFilterOptions = Object.freeze([
    { key: "chat", label: "Chat" },
    { key: "runtime", label: "Runtime" },
    { key: "commands", label: "Commands" },
    { key: "usage", label: "Usage" }
  ]);

  const supervisionEventTypes = new Set([
    "main_reported_to_host",
    "host_decision",
    "main_applied_decision"
  ]);

  const timelineEventTypes = new Set([
    "message",
    "task_dispatched",
    "task_started",
    "task_finished",
    "task_result_written",
    "task_final_requested",
    "task_round_summary_requested",
    "task_proxy_config_updated",
    "task_supervision_config_updated",
    "task_context_management_config_updated",
    "main_reported_to_host",
    "host_decision",
    "main_applied_decision",
    "task_reopened",
    "task_completed",
    "task_waiting_for_subagents",
    "task_status_changed",
    "agent_started",
    "agent_status_changed",
    "agent_thread",
    "agent_command_started",
    "agent_command_finished",
    "agent_message",
    "agent_prompt_metrics",
    "agent_usage",
    "agent_error",
    "agent_context_overflow",
    "agent_delegated",
    "agent_message_routed",
    "sub_agent_reported",
    "sub_agent_report_ignored",
    "sub_agent_backend_recovered",
    "sub_agent_backend_failed",
    "agent_created",
    "agent_config_updated",
    "agent_backend_switched",
    "agent_backend_restarted",
    "agent_finished",
    "agent_interrupted",
    "workspace_missing"
  ]);

  function eventData(event) {
    return event?.data || {};
  }

  function ahaActionEnvelopePayload(text) {
    const raw = String(text || "").trim();
    if (!raw.startsWith("{") || !raw.endsWith("}")) return null;
    try {
      const payload = JSON.parse(raw);
      return payload && Array.isArray(payload.actions) && typeof payload.response === "string" ? payload : null;
    } catch (_err) {
      return null;
    }
  }

  function isAhaActionEnvelopeText(text) {
    return Boolean(ahaActionEnvelopePayload(text));
  }

  function eventTaskId(event) {
    const data = eventData(event);
    if (data.task_id) return data.task_id;
    if (event?.type === "message" && /^task-\d+$/.test(data.target || "")) return data.target;
    return null;
  }

  function isTaskEvent(event, taskId) {
    return eventTaskId(event) === taskId;
  }

  function isTimelineEvent(event) {
    return timelineEventTypes.has(event?.type);
  }

  function addAgentRef(refs, value) {
    const text = String(value || "").trim();
    const lower = text.toLowerCase();
    if (!text || lower === "browser" || lower === "system" || lower === "aha") return;
    refs.add(text);
  }

  function eventAgentRefs(event) {
    const data = eventData(event);
    const refs = new Set();
    addAgentRef(refs, data.target);
    addAgentRef(refs, data.to_agent);
    addAgentRef(refs, data.from_agent);
    addAgentRef(refs, data.agent_id);
    if (event?.type === "message") {
      addAgentRef(refs, data.sender);
      if (["role", "from_agent", "to_agent", "sender", "target"].some(key => String(data[key] || "").toLowerCase() === "aha")) refs.add("main");
    }
    if (hostBrowserMessageVisibleToMain(event)) refs.add("main");
    if (!refs.size && (
      String(event?.type || "").startsWith("agent_") ||
      String(event?.type || "").startsWith("task_") ||
      supervisionEventTypes.has(event?.type) ||
      event?.type === "workspace_missing"
    )) {
      refs.add("main");
    }
    return refs;
  }

  function eventMatchesAgent(event, target) {
    return eventAgentRefs(event).has(target || "main");
  }

  function normalizedMessageEndpoint(value) {
    return String(value || "").trim().toLowerCase();
  }

  function messageDisplaySender(data) {
    return normalizedMessageEndpoint(data?.display_sender || data?.sender || data?.from_agent);
  }

  function messageDisplayTarget(data) {
    return normalizedMessageEndpoint(data?.display_target || data?.to_agent || data?.target);
  }

  function hostBrowserMessageVisibleToMain(event) {
    if (event?.type !== "message") return false;
    const data = eventData(event);
    const sender = messageDisplaySender(data);
    const target = messageDisplayTarget(data);
    return (
      String(data?.role || "").trim().toLowerCase() === "host" &&
      sender &&
      sender !== "main" &&
      target === "browser"
    );
  }

  function messageTimelineDisplay(data) {
    const displaySender = data?.display_sender || data?.sender || "-";
    const displayTarget = data?.display_target || data?.to_agent || data?.role || data?.target || "-";
    const className = data?.display_sender
      ? "from-supervision"
      : data?.sender === "browser"
        ? "from-browser"
        : data?.sender === "main"
          ? "from-main"
          : data?.sender === "system"
            ? "from-system"
            : "";
    return { displaySender, displayTarget, className };
  }

  function isMainBrowserMessage(event) {
    if (event?.type !== "message") return false;
    const data = eventData(event);
    return messageDisplaySender(data) === "main" && messageDisplayTarget(data) === "browser";
  }

  function supervisionMainLatestReply(text) {
    const raw = String(text || "");
    const marker = "\n- main_latest_reply:\n";
    const start = raw.indexOf(marker);
    if (start < 0) return "";
    return raw.slice(start + marker.length).trim();
  }

  function isMainHostSupervisionMirror(event, text) {
    if (event?.type !== "message") return false;
    const data = eventData(event);
    const target = messageDisplayTarget(data);
    const message = String(data.message || "").trim();
    const mirrorText = supervisionMainLatestReply(data.message) || message;
    return (
      mirrorText === text &&
      messageDisplaySender(data) === "main" &&
      target &&
      !["browser", "system", "aha", "main"].includes(target) &&
      Boolean(data.display_target || data.agent_id)
    );
  }

  function dedupeConversationEvents(events = [], target = "main") {
    const items = Array.isArray(events) ? events : [];
    const consumedAgentMessages = new Set();
    const mirroredMainBrowserMessages = new Set();
    items.forEach((event, index) => {
      const data = eventData(event);
      if (event?.type === "agent_message") {
        const text = String(data.text || "").trim();
        const agent = String(data.target || "main").trim();
        if (!text || isAhaActionEnvelopeText(text)) return;
        const consumed = items.slice(index + 1).some(candidate => {
          if (candidate.type !== "message") return false;
          const candidateData = eventData(candidate);
          const message = String(candidateData.message || "").trim();
          const sender = String(candidateData.display_sender || candidateData.sender || candidateData.from_agent || "").trim();
          return message === text && sender === agent;
        });
        if (consumed) consumedAgentMessages.add(index);
      }
      if (target === "main" && isMainBrowserMessage(event)) {
        const text = String(data.message || "").trim();
        if (!text) return;
        const mirroredToHost = items.some((candidate, candidateIndex) => (
          candidateIndex !== index && isMainHostSupervisionMirror(candidate, text)
        ));
        if (mirroredToHost) mirroredMainBrowserMessages.add(index);
      }
    });
    return items.filter((event, index) => {
      if (event?.type === "agent_message") {
        const text = String(eventData(event).text || "").trim();
        if (target === "main" && isAhaActionEnvelopeText(text)) return false;
        if (consumedAgentMessages.has(index)) return false;
      }
      if (event?.type === "message" && mirroredMainBrowserMessages.has(index)) return false;
      return true;
    });
  }

  function parseTimestamp(value) {
    if (!value) return null;
    const millis = Date.parse(value);
    return Number.isNaN(millis) ? null : millis;
  }

  function eventTimestamp(event) {
    return parseTimestamp(event?.ts || eventData(event).ts);
  }

  function eventIdentity(event) {
    return `${event?.ts || ""}|${event?.type || ""}|${JSON.stringify(eventData(event))}`;
  }

  function conversationEventOrder(event) {
    const cursor = event?._cursor ?? event?.event_id ?? event?.cursor;
    const numeric = Number(cursor);
    if (Number.isFinite(numeric)) return numeric;
    return eventTimestamp(event) ?? Number.MAX_SAFE_INTEGER;
  }

  function mergeConversationEvents(current, incoming, prepend = false) {
    const merged = prepend ? [...incoming, ...current] : [...current, ...incoming];
    const seen = new Set();
    return merged.filter(event => {
      const id = eventIdentity(event);
      if (seen.has(id)) return false;
      seen.add(id);
      return true;
    }).sort((left, right) => {
      const order = conversationEventOrder(left) - conversationEventOrder(right);
      return order === 0 ? 0 : order;
    });
  }

  function parseConversationKey(key) {
    const index = String(key || "").indexOf("::");
    return index < 0 ? { taskId: key, target: "main" } : { taskId: key.slice(0, index), target: key.slice(index + 2) || "main" };
  }

  function conversationEventCategory(event) {
    if (event?.type === "agent_message") return "chat";
    if (event?.type === "agent_usage" || event?.type === "agent_prompt_metrics") return "usage";
    if (event?.type === "agent_command_started" || event?.type === "agent_command_finished") return "commands";
    if (event?.type === "message") return "chat";
    return "runtime";
  }

  function conversationFilterCounts(events = [], options = conversationFilterOptions) {
    const counts = Object.fromEntries(options.map(item => [item.key, 0]));
    for (const event of Array.isArray(events) ? events : []) {
      const category = conversationEventCategory(event);
      counts[category] = (counts[category] || 0) + 1;
    }
    return counts;
  }

  function agentUpdateTitle(data) {
    const target = data?.target || "main";
    return target === "host" ? "host update" : `agent update (${target})`;
  }

  function agentUpdateBody(data) {
    const text = String(data?.text || "");
    const payload = ahaActionEnvelopePayload(text);
    if (!payload) return text;
    const actions = Array.isArray(payload.actions)
      ? payload.actions.map(action => action?.type || "action").filter(Boolean).join(", ")
      : "";
    return [
      payload.decision ? `decision: ${payload.decision}` : "",
      payload.reason ? `reason: ${payload.reason}` : "",
      payload.response ? `response: ${payload.response}` : "",
      actions ? `actions: ${actions}` : "actions: none"
    ].filter(Boolean).join("\n");
  }

  window.AHAConversationMetadata = Object.freeze({
    conversationFilterOptions,
    eventData,
    ahaActionEnvelopePayload,
    isAhaActionEnvelopeText,
    eventTaskId,
    isTaskEvent,
    isTimelineEvent,
    eventAgentRefs,
    eventMatchesAgent,
    normalizedMessageEndpoint,
    messageDisplaySender,
    messageDisplayTarget,
    messageTimelineDisplay,
    isMainBrowserMessage,
    supervisionMainLatestReply,
    isMainHostSupervisionMirror,
    dedupeConversationEvents,
    eventTimestamp,
    eventIdentity,
    conversationEventOrder,
    mergeConversationEvents,
    parseConversationKey,
    conversationEventCategory,
    conversationFilterCounts,
    agentUpdateTitle,
    agentUpdateBody
  });
})();
