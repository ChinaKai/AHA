const queryParams = new URLSearchParams(location.search);
const rawPollInterval = Number(queryParams.get("poll") || "1000");
const rawRequestTimeoutMs = Number(queryParams.get("timeout") || "12000");
const rawWsStaleMs = Number(queryParams.get("ws_stale_ms") || queryParams.get("ws_watchdog_ms") || "15000");
const pollInterval = Number.isFinite(rawPollInterval) ? Math.max(250, rawPollInterval) : 1000;
const requestTimeoutMs = Number.isFinite(rawRequestTimeoutMs) ? Math.max(1000, rawRequestTimeoutMs) : 12000;
const COMPACT_RESET_TIMEOUT_MS = Math.max(requestTimeoutMs, 60000);
const COMPACT_RESET_VERIFY_TIMEOUT_MS = 30000;
const eventSocketStaleMs = Number.isFinite(rawWsStaleMs) ? Math.max(5000, rawWsStaleMs) : 15000;
const eventTransport = String(queryParams.get("transport") || queryParams.get("events") || "").toLowerCase();
const wsConfig = String(queryParams.get("ws") || "").trim();
const wsDisabled = eventTransport === "poll" || eventTransport === "polling" || ["0", "false", "off"].includes(wsConfig.toLowerCase());
const realtimeDebugParam = String(queryParams.get("realtime_debug") || queryParams.get("debug") || "").toLowerCase();
const realtimeDebugEnabled = !["0", "false", "off", "no", "none"].includes(realtimeDebugParam);
let currentRunId = String(queryParams.get("run_id") || queryParams.get("run") || "").trim();
let bootstrapData = null;
let bootstrapError = "";
let defaultRunId = "";
let runsData = [];
let runsLoaded = false;
let runsError = "";
let workspaceData = [];
let sessionMenuOpen = false;
let runActionInFlight = false;
let webRestartInFlight = false;
let runArchiveMessage = "";
let runArchiveError = false;
let offset = -1;
let lastEventId = "";
let statusData = null;
const initialSelectedTaskId = String(queryParams.get("selected_task_id") || queryParams.get("task_id") || "").trim();
let selectedTaskId = initialSelectedTaskId || null;
let activeTab = "conversation";
let backendModels = new Map();
let backendCommands = new Map();
let taskActionInFlight = false;
let tickInFlight = false;
let tickFailureCount = 0;
let tickBackoffUntil = 0;
let backendStatusData = null;
let conversationAutoFollow = true;
let agentsPanelEditingUntil = 0;
let taskProxyEditingUntil = 0;
let taskSupervisionEditingUntil = 0;
let eventTailInitialized = false;
let pendingMessageId = 0;
let pendingSendInFlight = false;
let eventSocket = null;
let eventSocketState = "idle";
let eventSocketFailureCount = 0;
let eventSocketReconnectAt = 0;
let realtimeCatchupPromise = null;
let realtimeCatchupRequested = false;
let lastRealtimeMessageAt = 0;
let lastRealtimeFallbackPollAt = 0;
let realtimeDebugSeq = 0;
let deferredPanelRender = false;
let deferredPanelRenderTimer = 0;
let openPromptMetricsKey = "";
let mobileViewportRaf = 0;
const allEvents = [];
const seenRealtimeEvents = new Set();
const pendingMessages = [];
const interruptedContexts = new Set();
const conversationPageLimit = 30;
const logPageLimit = 200;
const conversationStates = new Map();
const expandedMessageKeys = new Set();
const copyTextByKey = new Map();
const finalDetails = new Map();
const contextDetails = new Map();
const logStates = new Map();
const compactResetStates = new Map();
const terminalTaskStatuses = new Set(["completed", "failed", "blocked"]);
const terminalAgentStatuses = new Set(["completed", "failed", "blocked", "interrupted"]);
const sandboxOptions = ["workspace-write", "read-only", "danger-full-access"];
const approvalOptions = ["never", "on-failure", "on-request", "untrusted"];
const defaultHttpProxy = "http://127.0.0.1:7890";
const defaultHttpsProxy = defaultHttpProxy;
const defaultNoProxy = "localhost,127.0.0.1,::1";
const collapsedMessageCharLimit = 900;
const collapsedMessageLineLimit = 2;
const conversationFilters = {
  chat: true,
  runtime: false,
  commands: false,
  usage: false
};
const conversationFilterOptions = [
  { key: "chat", label: "Chat" },
  { key: "runtime", label: "Runtime" },
  { key: "commands", label: "Commands" },
  { key: "usage", label: "Usage" }
];
const supervisionEventTypes = new Set([
  "main_reported_to_host",
  "host_decision",
  "main_applied_decision"
]);
const turnEventTypes = new Set([
  "agent_started",
  "agent_prompt_metrics",
  "agent_usage",
  "agent_context_overflow",
  "agent_thread",
  "agent_finished",
  "agent_status_changed"
]);

const runIdEl = document.getElementById("run-id");
const runStateEl = document.getElementById("run-state");
const sessionControlEl = document.getElementById("session-control");
const sessionToggleEl = document.getElementById("session-toggle");
const sessionTitleEl = document.getElementById("session-title");
const sessionMenuEl = document.getElementById("session-menu");
const sessionRefreshEl = document.getElementById("session-refresh");
const runSelectEl = document.getElementById("run-select");
const runCreateFormEl = document.getElementById("run-create-form");
const newRunGoalEl = document.getElementById("new-run-goal");
const newRunModeEl = document.getElementById("new-run-mode");
const newRunCollaborationEl = document.getElementById("new-run-collaboration");
const newRunCollaborationHelpEl = document.getElementById("new-run-collaboration-help");
const runExportEl = document.getElementById("run-export");
const runImportEl = document.getElementById("run-import");
const runExportLogsEl = document.getElementById("run-export-logs");
const runImportFileEl = document.getElementById("run-import-file");
const runArchiveStateEl = document.getElementById("run-archive-state");
const webRestartEl = document.getElementById("web-restart");
const weixinConsoleEl = document.getElementById("weixin-console");
const weixinConsolePopoverEl = document.getElementById("weixin-console-popover");
const webRestartStateEl = document.getElementById("web-restart-state");
const sessionDetailTextEl = document.getElementById("session-detail-text");
const headerWorkspaceDirEl = document.getElementById("header-workspace-dir");
const mobileTaskSummaryEl = document.getElementById("mobile-task-summary");
const mobileTaskTitleEl = document.getElementById("mobile-task-title");
const mobileTaskStatusEl = document.getElementById("mobile-task-status");
const summaryEl = document.getElementById("summary");
const openTaskCreateEl = document.getElementById("open-task-create");
const taskCreateDialogEl = document.getElementById("task-create-dialog");
const closeTaskCreateEl = document.getElementById("close-task-create");
const cancelTaskCreateEl = document.getElementById("cancel-task-create");
const collapseOverviewEl = document.getElementById("collapse-overview");
const collapseAgentsEl = document.getElementById("collapse-agents");
const overviewRailToggleEl = document.getElementById("overview-rail-toggle");
const agentsRailToggleEl = document.getElementById("agents-rail-toggle");
const mobileSheetBackdropEl = document.getElementById("mobile-sheet-backdrop");
const openTasksSheetEl = document.getElementById("open-tasks-sheet");
const openAgentsSheetEl = document.getElementById("open-agents-sheet");
const closeTasksSheetEl = document.getElementById("close-tasks-sheet");
const closeAgentsSheetEl = document.getElementById("close-agents-sheet");
const mobileActionPanelEl = document.getElementById("mobile-action-panel");
const mobileActionsToggleEl = document.getElementById("mobile-actions-toggle");
const tasksEl = document.getElementById("tasks");
const showHiddenEl = document.getElementById("show-hidden");
const selectedIdEl = document.getElementById("selected-id");
const selectedTitleEl = document.getElementById("selected-title");
const selectedStatusEl = document.getElementById("selected-status");
const selectedTaskMetaEl = document.getElementById("selected-task-meta");
const panelEl = document.getElementById("panel");
const sendFormEl = document.getElementById("send-form");
const messageEl = document.getElementById("message");
const taskFormEl = document.getElementById("task-form");
const newTaskTitleEl = document.getElementById("new-task-title");
const newTaskDescriptionEl = document.getElementById("new-task-description");
const agentTargetEl = document.getElementById("agent-target");
const agentsEl = document.getElementById("agents");
const taskBackendEl = document.getElementById("task-backend");
const taskModelEl = document.getElementById("task-model");
const taskSandboxEl = document.getElementById("task-sandbox");
const taskApprovalEl = document.getElementById("task-approval");
const taskProxyEnabledEl = document.getElementById("task-proxy-enabled");
const taskHttpProxyEl = document.getElementById("task-http-proxy");
const taskHttpsProxyEl = document.getElementById("task-https-proxy");
const taskNoProxyEl = document.getElementById("task-no-proxy");
const taskRunContextEl = document.getElementById("task-run-context");
const collaborationModeEl = document.getElementById("collaboration-mode");
const collaborationModeHelpEl = document.getElementById("collaboration-mode-help");
const maxSubAgentsEl = document.getElementById("max-sub-agents");
const maxSubAgentsFieldEl = document.getElementById("max-sub-agents-field");
const taskSupervisionModeEl = document.getElementById("task-supervision-mode");
const taskSupervisionMaxRoundsFieldEl = document.getElementById("task-supervision-max-rounds-field");
const taskSupervisionMaxRoundsEl = document.getElementById("task-supervision-max-rounds");
const taskSupervisionAskUserFieldEl = document.getElementById("task-supervision-ask-user-field");
const taskSupervisionAskUserGatesEl = document.getElementById("task-supervision-ask-user-gates");
const workspaceSelectEl = document.getElementById("workspace-select");
const workspaceCustomEl = document.getElementById("workspace-custom");
const taskProxyEditorEl = document.getElementById("task-proxy-editor");
const taskProxyFormEl = document.getElementById("task-proxy-form");
const selectedTaskProxyEnabledEl = document.getElementById("selected-task-proxy-enabled");
const selectedTaskHttpProxyEl = document.getElementById("selected-task-http-proxy");
const selectedTaskHttpsProxyEl = document.getElementById("selected-task-https-proxy");
const selectedTaskNoProxyEl = document.getElementById("selected-task-no-proxy");
const taskProxyStateEl = document.getElementById("task-proxy-state");
const taskSupervisionEditorEl = document.getElementById("task-supervision-editor");
const taskSupervisionFormEl = document.getElementById("task-supervision-form");
const selectedTaskSupervisionModeEl = document.getElementById("selected-task-supervision-mode");
const selectedTaskSupervisionMaxRoundsFieldEl = document.getElementById("selected-task-supervision-max-rounds-field");
const selectedTaskSupervisionMaxRoundsEl = document.getElementById("selected-task-supervision-max-rounds");
const selectedTaskSupervisionAskUserFieldEl = document.getElementById("selected-task-supervision-ask-user-field");
const selectedTaskSupervisionAskUserGatesEl = document.getElementById("selected-task-supervision-ask-user-gates");
const taskSupervisionStateEl = document.getElementById("task-supervision-state");
const taskCreateConfirmDialogEl = document.getElementById("task-create-confirm");
const taskCreateConfirmDetailsEl = document.getElementById("task-create-confirm-details");
const selectedAgentInfoEl = document.getElementById("selected-agent-info");
const backendStatusEl = document.getElementById("backend-status");
const pendingMessagesEl = document.getElementById("pending-messages");
const conversationFiltersEl = document.getElementById("conversation-filters");
const commandMenuEl = document.getElementById("command-menu");
let commandSelection = 0;
let weixinConsoleOpen = false;
let weixinPollTimer = null;
const weixinState = {
  loaded: false,
  loading: false,
  sending: false,
  togglingNotifications: false,
  error: "",
  notice: "",
  status: null,
  testMessage: "AHA 微信通知测试"
};
const ahaSlashCommands = [
  { scope: "aha", name: "/aha help", insert: "/aha help", desc: "Show AHA commands. Handled locally." },
  { scope: "aha", name: "/aha status", insert: "/aha status", desc: "Show selected task status. Handled locally." },
  { scope: "aha", name: "/aha agents", insert: "/aha agents", desc: "List selected task agents. Handled locally." },
  { scope: "aha", name: "/aha checkpoint", insert: "/aha checkpoint ", desc: "Record a task journal checkpoint. Handled locally." },
  { scope: "aha", name: "/aha final", insert: "/aha final", desc: "Ask task-main to generate the Final and complete the task." },
  { scope: "aha", name: "/aha finalize", insert: "/aha finalize", desc: "Alias for /aha final." },
  { scope: "aha", name: "/aha complete", insert: "/aha complete", desc: "Alias for /aha final." },
  { scope: "aha", name: "/aha reopen", insert: "/aha reopen", desc: "Reopen a completed task for follow-up." },
  { scope: "aha", name: "/aha interrupt", insert: "/aha interrupt", desc: "Interrupt the selected agent's current turn." },
  { scope: "aha", name: "/aha session compact-reset", insert: "/aha session compact-reset", desc: "Compact and reset the selected backend session." }
];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function nodeInsidePanel(node) {
  if (!node) return false;
  const element = node instanceof Element ? node : node.parentElement;
  return Boolean(element && panelEl.contains(element));
}

function panelHasTextSelection() {
  const selection = window.getSelection?.();
  if (!selection || selection.isCollapsed || !selection.toString().trim()) return false;
  return nodeInsidePanel(selection.anchorNode) || nodeInsidePanel(selection.focusNode);
}

function renderPanelForRealtime(options = {}) {
  if (panelHasTextSelection()) {
    deferredPanelRender = true;
    return false;
  }
  if (deferredPanelRenderTimer) {
    window.clearTimeout(deferredPanelRenderTimer);
    deferredPanelRenderTimer = 0;
  }
  deferredPanelRender = false;
  renderPanel(options);
  return true;
}

function activePromptMetricsPopover() {
  return panelEl.querySelector(".turn-metrics[open] .turn-metrics-popover");
}

function activePromptMetricsTrigger() {
  return panelEl.querySelector(".turn-metrics[open] .turn-metrics-trigger");
}

function closePromptMetricsBreakdowns(root = panelEl) {
  root.querySelectorAll("[data-metrics-breakdown][open]").forEach(details => {
    if (details instanceof HTMLDetailsElement) details.open = false;
  });
}

function closePromptMetricsPopover() {
  openPromptMetricsKey = "";
  closePromptMetricsBreakdowns();
  panelEl.querySelectorAll(".turn-metrics[open]").forEach(details => {
    if (details instanceof HTMLDetailsElement) details.open = false;
  });
}

function targetInsidePromptMetrics(target) {
  const element = target instanceof Element ? target : null;
  return Boolean(element?.closest?.(".turn-metrics"));
}

function closePromptMetricsPopoverForOutsideEvent(event) {
  if (!openPromptMetricsKey) return;
  if (targetInsidePromptMetrics(event.target)) return;
  closePromptMetricsPopover();
}

function capturePromptMetricsPopoverState() {
  const popover = activePromptMetricsPopover();
  if (!popover) return null;
  const trigger = activePromptMetricsTrigger();
  const breakdownOpen = {};
  popover.querySelectorAll("[data-metrics-breakdown]").forEach(details => {
    if (details instanceof HTMLDetailsElement) breakdownOpen[details.dataset.metricsBreakdown || ""] = details.open;
  });
  return {
    breakdownOpen,
    popoverScrollTop: popover.scrollTop,
    triggerTop: trigger?.getBoundingClientRect?.().top ?? null
  };
}

function positionPromptMetricsPopover() {
  const popover = activePromptMetricsPopover();
  const trigger = activePromptMetricsTrigger();
  if (!popover || !trigger) return;
  const margin = 16;
  const gap = 8;
  const composerTop = sendFormEl?.getBoundingClientRect?.().top ?? window.innerHeight;
  const lowerBoundary = Math.max(margin + 120, Math.min(window.innerHeight - margin, composerTop - gap));
  const maxHeight = Math.max(120, Math.min(window.innerHeight * 0.62, 520, lowerBoundary - margin));
  popover.style.maxHeight = `${maxHeight}px`;
  popover.style.left = "";
  popover.style.top = "";
  const triggerRect = trigger.getBoundingClientRect();
  const popoverRect = popover.getBoundingClientRect();
  const width = popoverRect.width || Math.min(480, window.innerWidth - margin * 2);
  const height = popover.offsetHeight || popoverRect.height || Math.min(window.innerHeight * 0.62, 520);
  const maxLeft = Math.max(margin, window.innerWidth - width - margin);
  const left = Math.min(Math.max(margin, triggerRect.right - width), maxLeft);
  let top = triggerRect.top - height - gap;
  if (top < margin) top = triggerRect.bottom + gap;
  if (top + height > lowerBoundary) top = Math.max(margin, lowerBoundary - height);
  popover.style.left = `${left}px`;
  popover.style.top = `${top}px`;
}

function restorePromptMetricsPopoverState(state) {
  if (!state) return;
  const restore = () => {
    const popover = activePromptMetricsPopover();
    const trigger = activePromptMetricsTrigger();
    if (trigger && state.triggerTop != null) {
      panelEl.scrollTop += trigger.getBoundingClientRect().top - state.triggerTop;
    }
    Object.entries(state.breakdownOpen || {}).forEach(([key, open]) => {
      const breakdown = Array.from(popover?.querySelectorAll("[data-metrics-breakdown]") || [])
        .find(item => item instanceof HTMLDetailsElement && item.dataset.metricsBreakdown === key);
      if (breakdown instanceof HTMLDetailsElement) breakdown.open = Boolean(open);
    });
    const sessionBreakdown = popover?.querySelector("[data-metrics-breakdown=\"session\"]");
    if (sessionBreakdown instanceof HTMLDetailsElement && state.sessionBreakdownOpen != null) {
      sessionBreakdown.open = Boolean(state.sessionBreakdownOpen);
    }
    if (popover && state.popoverScrollTop != null) popover.scrollTop = state.popoverScrollTop;
    positionPromptMetricsPopover();
  };
  restore();
  window.requestAnimationFrame(restore);
}

function flushDeferredPanelRender() {
  if (!deferredPanelRender || panelHasTextSelection()) return;
  if (deferredPanelRenderTimer) return;
  deferredPanelRenderTimer = window.setTimeout(() => {
    deferredPanelRenderTimer = 0;
    if (!deferredPanelRender || panelHasTextSelection()) return;
    deferredPanelRender = false;
    renderPanel();
  }, 80);
}

function fallbackCopyText(text) {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-9999px";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  try {
    return document.execCommand("copy");
  } finally {
    textarea.remove();
  }
}

async function copyTimelineMessage(button) {
  const key = button.dataset.copyMessageKey || "";
  const text = copyTextByKey.get(key) || "";
  if (!text) return;
  const originalLabel = button.getAttribute("aria-label") || "Copy message";
  const label = button.querySelector(".message-copy-label");
  const setState = (state, textLabel) => {
    button.dataset.copyState = state;
    button.setAttribute("aria-label", textLabel);
    button.title = textLabel;
    if (label) label.textContent = textLabel;
  };
  button.disabled = true;
  setState("copying", "Copying");
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else if (!fallbackCopyText(text)) {
      throw new Error("copy failed");
    }
    setState("copied", "Copied");
  } catch (_err) {
    const copied = fallbackCopyText(text);
    setState(copied ? "copied" : "failed", copied ? "Copied" : "Copy failed");
  } finally {
    window.setTimeout(() => {
      button.disabled = false;
      setState("idle", originalLabel);
    }, 1200);
  }
}

async function readJsonResponse(res, fallbackMessage = "Request failed") {
  const payload = await res.json().catch(() => null);
  if (!res.ok) {
    const status = [res.status, res.statusText].filter(Boolean).join(" ");
    const detail = payload?.error || status || fallbackMessage;
    throw new Error(`${fallbackMessage}: ${detail}`);
  }
  return payload || {};
}

async function fetchWithTimeout(url, options = {}, timeoutMs = requestTimeoutMs) {
  const controller = new AbortController();
  const init = { ...options, signal: options.signal || controller.signal };
  const timer = options.signal ? null : setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, init);
  } catch (err) {
    if (err?.name === "AbortError") {
      throw new Error(`Request timed out after ${timeoutMs}ms: ${url}`);
    }
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function isRequestTimeoutError(err) {
  return String(err?.message || err || "").includes("Request timed out after");
}

async function fetchJson(url, options = {}, fallbackMessage = "Request failed") {
  const res = await fetchWithTimeout(url, options);
  return readJsonResponse(res, fallbackMessage);
}

function apiUrl(path, params = {}, options = {}) {
  const query = new URLSearchParams();
  const source = params instanceof URLSearchParams ? params : new URLSearchParams(params);
  for (const [key, value] of source.entries()) {
    if (value !== null && value !== undefined && value !== "") query.set(key, value);
  }
  if (options.runScoped !== false && currentRunId) query.set("run_id", currentRunId);
  const suffix = query.toString();
  return suffix ? `${path}?${suffix}` : path;
}

function eventCursorStorageKey() {
  return currentRunId ? `aha:last-event-id:${currentRunId}` : "";
}

function readStoredLastEventId() {
  const key = eventCursorStorageKey();
  if (!key) return "";
  try {
    return String(window.localStorage?.getItem(key) || "").trim();
  } catch (_err) {
    return "";
  }
}

function writeStoredLastEventId(value) {
  const key = eventCursorStorageKey();
  if (!key || !value) return;
  try {
    window.localStorage?.setItem(key, String(value));
  } catch (_err) {
    // localStorage can be disabled; realtime still works for this page session.
  }
}

function clearStoredLastEventId() {
  const key = eventCursorStorageKey();
  if (!key) return;
  try {
    window.localStorage?.removeItem(key);
  } catch (_err) {
    // Ignore storage errors and fall back to tail initialization.
  }
}

function selectedTaskStorageKey(runId = currentRunId) {
  return runId ? `aha:selected-task-id:${runId}` : "";
}

function readStoredSelectedTaskId(runId = currentRunId) {
  const key = selectedTaskStorageKey(runId);
  if (!key) return "";
  try {
    return String(window.localStorage?.getItem(key) || "").trim();
  } catch (_err) {
    return "";
  }
}

function writeStoredSelectedTaskId(taskId, runId = currentRunId) {
  const key = selectedTaskStorageKey(runId);
  if (!key) return;
  try {
    const value = String(taskId || "").trim();
    if (value) {
      window.localStorage?.setItem(key, value);
    } else {
      window.localStorage?.removeItem(key);
    }
  } catch (_err) {
    // localStorage can be disabled; task selection still works for this page session.
  }
}

function restoreEventCursorFromStorage() {
  lastEventId = readStoredLastEventId();
  const numericOffset = Number(lastEventId);
  offset = lastEventId && Number.isFinite(numericOffset) ? numericOffset : -1;
  eventTailInitialized = Boolean(lastEventId);
}

function rememberEventCursor(payload) {
  if (Number.isFinite(payload.offset)) offset = payload.offset;
  const nextEventId = String(payload.last_event_id || payload.offset || "").trim();
  if (nextEventId) {
    lastEventId = nextEventId;
    writeStoredLastEventId(nextEventId);
  }
}

function rememberEventCursorFromEvent(event) {
  const nextEventId = String(event?.event_id || "").trim();
  if (!nextEventId) return;
  lastEventId = nextEventId;
  const numericOffset = Number(nextEventId);
  if (Number.isFinite(numericOffset)) offset = numericOffset;
  eventTailInitialized = true;
  writeStoredLastEventId(nextEventId);
}

function runScopedPayload(payload = {}) {
  return currentRunId ? { ...payload, run_id: currentRunId } : payload;
}

function eventSocketReadyStateName() {
  if (!eventSocket || typeof WebSocket === "undefined") return "none";
  if (eventSocket.readyState === WebSocket.CONNECTING) return "connecting";
  if (eventSocket.readyState === WebSocket.OPEN) return "open";
  if (eventSocket.readyState === WebSocket.CLOSING) return "closing";
  if (eventSocket.readyState === WebSocket.CLOSED) return "closed";
  return String(eventSocket.readyState);
}

function realtimeDebug(stage, detail = {}) {
  const payload = {
    seq: ++realtimeDebugSeq,
    stage,
    run_id: currentRunId,
    selected_task_id: selectedTaskId,
    target: agentTargetEl?.value || "",
    active_tab: activeTab,
    visibility: document.visibilityState,
    online: navigator.onLine,
    ws_state: eventSocketState,
    ws_ready_state: eventSocketReadyStateName(),
    last_event_id: lastEventId,
    offset,
    tail_initialized: eventTailInitialized,
    last_ws_message_age_ms: lastRealtimeMessageAt ? Date.now() - lastRealtimeMessageAt : null,
    ...detail
  };
  if (!realtimeDebugEnabled) return;
  console.info("[AHA realtime]", payload);
  fetch(apiUrl("/api/debug/realtime"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(runScopedPayload(payload)),
    keepalive: true
  }).catch(() => {});
}

function runIdOf(run) {
  return String(run?.id || run?.run_id || "").trim();
}

function applyRunListData(payload = {}) {
  defaultRunId = String(payload.default_run_id || defaultRunId || "").trim();
  runsData = Array.isArray(payload.runs) ? payload.runs : [];
  const knownRunIds = new Set(runsData.map(runIdOf).filter(Boolean));
  if (currentRunId && !knownRunIds.has(currentRunId)) {
    currentRunId = "";
    syncRunUrl();
  }
  const preferred = currentRunId || defaultRunId || runIdOf(runsData[0]) || String(statusData?.run_id || "").trim();
  if (preferred && preferred !== currentRunId) {
    currentRunId = preferred;
    syncRunUrl();
  }
  if (!preferred && currentRunId) {
    currentRunId = "";
    syncRunUrl();
  }
  if (currentRunId) {
    if (!initialSelectedTaskId) selectedTaskId = readStoredSelectedTaskId() || selectedTaskId;
    restoreEventCursorFromStorage();
  }
}

function applyWorkspaceData(workspaces = []) {
  workspaceData = Array.isArray(workspaces) ? workspaces : [];
  renderWorkspaceSelect();
}

async function loadBootstrap() {
  const payload = await fetchJson("/api/bootstrap", {}, "Failed to bootstrap AHA");
  bootstrapError = "";
  bootstrapData = payload;
  applyRunListData(payload);
  applyWorkspaceData(payload.workspaces || workspaceData);
  applyBackendData(payload.backends || []);
  runsError = "";
  runsLoaded = true;
  renderSessionMenu();
  return payload;
}

function runTitleOf(run) {
  const goal = String(run?.goal || "").trim();
  return goal || runIdOf(run) || "未命名 Run";
}

function runUpdatedAtOf(run) {
  return run?.updated_at || run?.created_at || "";
}

function currentRunSummary() {
  return runsData.find(run => runIdOf(run) === currentRunId) || null;
}

function fallbackCurrentRun() {
  const id = currentRunId || statusData?.run_id || defaultRunId;
  if (!id) return null;
  return {
    id,
    goal: statusData?.goal || "当前 Run",
    mode: statusData?.mode || "",
    status: statusData?.status || "",
    updated_at: statusData?.updated_at || ""
  };
}

function sessionOptionLabel(run) {
  const title = runTitleOf(run);
  const status = run?.status ? ` · ${run.status}` : "";
  const taskCount = Number.isFinite(run?.task_count) ? ` · ${run.task_count} 个任务` : "";
  return `${title}${status}${taskCount}`;
}

function syncRunUrl() {
  if (!window.history?.replaceState) return;
  const url = new URL(window.location.href);
  if (currentRunId) {
    url.searchParams.set("run_id", currentRunId);
  } else {
    url.searchParams.delete("run_id");
  }
  url.searchParams.delete("run");
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
}

function resetRunScopedState() {
  closeEventWebSocket();
  eventSocketState = "idle";
  eventSocketFailureCount = 0;
  eventSocketReconnectAt = 0;
  lastRealtimeMessageAt = 0;
  lastRealtimeFallbackPollAt = 0;
  selectedTaskId = readStoredSelectedTaskId() || null;
  backendStatusData = null;
  restoreEventCursorFromStorage();
  conversationAutoFollow = true;
  allEvents.length = 0;
  seenRealtimeEvents.clear();
  conversationStates.clear();
  expandedMessageKeys.clear();
  finalDetails.clear();
  contextDetails.clear();
  logStates.clear();
  pendingMessages.length = 0;
  interruptedContexts.clear();
  panelEl.innerHTML = '<div class="empty">正在切换 Run...</div>';
}

function realtimeTransportText() {
  if (!currentRunId) return "realtime Disconnected";
  if (wsDisabled || typeof WebSocket === "undefined") return "realtime Polling";
  if (eventSocketState === "open") return "realtime WebSocket";
  if (eventSocketState === "connecting") return "realtime Connecting";
  if (eventSocketState === "stale") return "realtime Reconnecting (polling)";
  if (Date.now() < eventSocketReconnectAt) return "realtime Reconnecting (polling)";
  if (eventSocketState === "polling") return "realtime Polling fallback";
  if (eventSocketState === "error") return "realtime Polling fallback";
  if (eventSocketState === "closed") return "realtime Disconnected";
  return "realtime WebSocket pending";
}

function refreshRealtimeIndicator() {
  if (runStateEl && (statusData || currentRunId)) renderSessionSummary();
}

function renderSessionSummary() {
  const run = currentRunSummary() || fallbackCurrentRun();
  const runId = currentRunId || runIdOf(run);
  if (!run && !statusData) {
    if (sessionTitleEl) sessionTitleEl.textContent = currentRunId || "未选择 Run";
    if (runIdEl) runIdEl.textContent = currentRunId || "-";
    if (runStateEl) runStateEl.textContent = bootstrapData?.aha_home ? `AHA_HOME ${bootstrapData.aha_home}` : "";
    if (sessionDetailTextEl) sessionDetailTextEl.textContent = "创建 Run 后开始";
    if (taskRunContextEl) taskRunContextEl.textContent = "当前没有 Run";
    return;
  }
  const title = statusData?.goal || runTitleOf(run);
  if (sessionTitleEl) {
    sessionTitleEl.textContent = title || "未选择 Run";
    sessionTitleEl.title = title || "";
  }
  if (sessionToggleEl) {
    const label = runId ? `Run 操作台: ${title || runId}` : "Run 操作台";
    sessionToggleEl.title = label;
    sessionToggleEl.setAttribute("aria-label", label);
  }
  if (runIdEl) {
    runIdEl.textContent = runId || "-";
    runIdEl.title = runId || "";
  }
  const updatedAt = statusData?.updated_at || runUpdatedAtOf(run);
  const runStateText = `updated ${formatLocalTimestamp(updatedAt, updatedAt || "-")}`;
  if (runStateEl) {
    runStateEl.textContent = runStateText;
    runStateEl.title = runStateText;
  }
  if (sessionDetailTextEl) {
    const taskCount = Number.isFinite(run?.task_count) ? `${run.completed_count || 0}/${run.task_count} tasks` : "";
    sessionDetailTextEl.textContent = [
      run?.mode ? `mode ${run.mode}` : statusData?.mode ? `mode ${statusData.mode}` : "",
      run?.status ? `状态 ${run.status}` : "",
      taskCount,
      updatedAt ? `更新 ${formatLocalTimestamp(updatedAt, updatedAt)}` : "",
      realtimeTransportText(),
      runsError ? `提示 ${runsError}` : ""
    ].filter(Boolean).join(" · ") || "Run 详情";
  }
  if (taskRunContextEl) {
    const mode = statusData?.mode || run?.mode || "-";
    const runLabel = title || runId || "-";
    taskRunContextEl.textContent = `当前 Run: ${runLabel} / ${mode}`;
    taskRunContextEl.title = runId || "";
  }
}

function renderSessionMenu() {
  if (!runSelectEl) return;
  const fallback = fallbackCurrentRun();
  const runs = runsData.length ? runsData : (fallback ? [fallback] : []);
  runSelectEl.innerHTML = "";
  for (const run of runs) {
    const id = runIdOf(run);
    if (!id) continue;
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = sessionOptionLabel(run);
    opt.selected = id === currentRunId;
    runSelectEl.appendChild(opt);
  }
  runSelectEl.disabled = runActionInFlight || runSelectEl.options.length === 0;
  if (sessionRefreshEl) sessionRefreshEl.disabled = runActionInFlight;
  if (newRunGoalEl) newRunGoalEl.disabled = runActionInFlight;
  if (newRunModeEl) newRunModeEl.disabled = runActionInFlight;
  const hasRun = Boolean(currentRunId);
  if (runExportEl) runExportEl.disabled = runActionInFlight || !hasRun;
  if (runImportEl) runImportEl.disabled = runActionInFlight;
  if (runExportLogsEl) runExportLogsEl.disabled = runActionInFlight || !hasRun;
  if (runImportFileEl) runImportFileEl.disabled = runActionInFlight;
  if (webRestartEl) webRestartEl.disabled = webRestartInFlight || !hasRun;
  if (weixinConsoleEl) weixinConsoleEl.disabled = runActionInFlight || !hasRun;
  if (!hasRun || runActionInFlight) {
    setWeixinConsoleOpen(false);
  } else if (weixinConsoleOpen && weixinConsolePopoverEl) {
    renderWeixinConsolePopover();
  }
  renderRunArchiveState();
  renderSessionSummary();
}

async function loadRuns(force = false) {
  if (runsLoaded && !force) {
    renderSessionMenu();
    return;
  }
  try {
    const payload = await fetchJson("/api/runs", {}, "Failed to load runs");
    applyRunListData(payload);
    runsError = "";
  } catch (err) {
    runsError = err?.message || String(err || "Run 列表不可用");
    runsData = fallbackCurrentRun() ? [fallbackCurrentRun()] : [];
  } finally {
    runsLoaded = true;
    renderSessionMenu();
  }
}

function setSessionMenu(open) {
  sessionMenuOpen = Boolean(open);
  sessionMenuEl?.classList.toggle("hidden", !sessionMenuOpen);
  sessionToggleEl?.setAttribute("aria-expanded", String(sessionMenuOpen));
  if (!sessionMenuOpen) setWeixinConsoleOpen(false);
}

function setWeixinConsoleOpen(open) {
  weixinConsoleOpen = Boolean(open && currentRunId && weixinConsolePopoverEl);
  if (!weixinConsolePopoverEl) return;
  if (weixinConsoleOpen) {
    renderWeixinConsolePopover();
    weixinConsolePopoverEl.hidden = false;
    void loadWeixinStatus({ silent: weixinState.loaded });
  } else {
    clearWeixinPoll();
    weixinConsolePopoverEl.hidden = true;
    weixinConsolePopoverEl.innerHTML = "";
  }
  weixinConsoleEl?.setAttribute("aria-expanded", String(weixinConsoleOpen));
}

function closeWeixinConsoleForOutsideEvent(event) {
  if (!weixinConsoleOpen) return;
  const target = event.target instanceof Element ? event.target : null;
  if (weixinConsoleEl?.contains(target) || weixinConsolePopoverEl?.contains(target)) return;
  setWeixinConsoleOpen(false);
}

function clearWeixinPoll() {
  if (weixinPollTimer) {
    clearTimeout(weixinPollTimer);
    weixinPollTimer = null;
  }
}

function weixinPairingStatus() {
  const status = weixinState.status || {};
  return status.pairing?.status || (status.paired ? "paired" : "idle");
}

function scheduleWeixinPoll() {
  clearWeixinPoll();
  if (!weixinConsoleOpen) return;
  if (weixinState.loading || weixinState.sending) return;
  if (!["waiting", "scanned"].includes(weixinPairingStatus())) return;
  weixinPollTimer = setTimeout(() => {
    void loadWeixinStatus({ silent: true });
  }, 2000);
}

function renderWeixinConsolePopover() {
  if (!weixinConsolePopoverEl) return;
  weixinConsolePopoverEl.innerHTML = renderWeixinConsole();
  scheduleWeixinPoll();
}

async function loadWeixinStatus(options = {}) {
  if (!currentRunId) return;
  const silent = Boolean(options.silent);
  if (!silent) {
    weixinState.loading = true;
    renderWeixinConsolePopover();
  }
  try {
    const payload = await fetchJson(apiUrl("/api/weixin"), {}, "加载微信状态失败");
    weixinState.status = payload;
    weixinState.loaded = true;
    weixinState.error = payload.error || "";
  } catch (err) {
    weixinState.error = err?.message || String(err || "加载微信状态失败");
  } finally {
    weixinState.loading = false;
    renderWeixinConsolePopover();
  }
}

async function startWeixinPairing() {
  if (!currentRunId || weixinState.loading) return;
  weixinState.loading = true;
  weixinState.error = "";
  weixinState.notice = "";
  renderWeixinConsolePopover();
  try {
    const payload = await fetchJson(apiUrl("/api/weixin/pair"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    }, "生成微信配对二维码失败");
    weixinState.status = payload;
    weixinState.loaded = true;
  } catch (err) {
    weixinState.error = err?.message || String(err || "生成微信配对二维码失败");
  } finally {
    weixinState.loading = false;
    renderWeixinConsolePopover();
  }
}

async function sendWeixinTestNotification() {
  if (!currentRunId || weixinState.sending) return;
  weixinState.sending = true;
  weixinState.error = "";
  weixinState.notice = "";
  renderWeixinConsolePopover();
  try {
    await fetchJson(apiUrl("/api/weixin/test"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: weixinState.testMessage })
    }, "发送微信测试通知失败");
    weixinState.notice = "测试通知已发送";
    await loadWeixinStatus({ silent: true });
  } catch (err) {
    weixinState.error = err?.message || String(err || "发送微信测试通知失败");
  } finally {
    weixinState.sending = false;
    renderWeixinConsolePopover();
  }
}

async function setWeixinNotificationsEnabled(enabled) {
  if (!currentRunId || weixinState.togglingNotifications) return;
  weixinState.togglingNotifications = true;
  weixinState.error = "";
  weixinState.notice = "";
  renderWeixinConsolePopover();
  try {
    const payload = await fetchJson(apiUrl("/api/weixin/notifications"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: Boolean(enabled) })
    }, "更新微信通知开关失败");
    weixinState.status = { ...(weixinState.status || {}), notifications: payload.notifications || {} };
    weixinState.notice = payload.notifications?.enabled ? "微信通知已开启" : "微信通知已关闭";
  } catch (err) {
    weixinState.error = err?.message || String(err || "更新微信通知开关失败");
    await loadWeixinStatus({ silent: true });
  } finally {
    weixinState.togglingNotifications = false;
    renderWeixinConsolePopover();
  }
}

async function refreshRunScopedView() {
  if (!currentRunId) {
    renderFirstRunState();
    return;
  }
  await loadStatus({ forceAgents: true });
  await Promise.all([ensureConversationLoaded(), loadBackendStatus()]);
  await syncRealtimeEvents();
  renderPanel();
}

async function switchRun(runId) {
  const nextRunId = String(runId || "").trim();
  if (!nextRunId || nextRunId === currentRunId) {
    renderSessionMenu();
    setSessionMenu(false);
    return;
  }
  runActionInFlight = true;
  try {
    currentRunId = nextRunId;
    syncRunUrl();
    resetRunScopedState();
    renderSessionMenu();
    setSessionMenu(false);
    await refreshRunScopedView();
  } catch (err) {
    panelEl.innerHTML = `<pre>${escapeHtml(String(err))}</pre>`;
  } finally {
    runActionInFlight = false;
    renderSessionMenu();
  }
}

async function createRun(goal, mode, options = {}) {
  const trimmedGoal = String(goal || "").trim();
  if (!trimmedGoal) return;
  const selectedMode = String(mode || "research").trim() || "research";
  const collaborationMode = String(options.collaborationMode || "auto").trim() || "auto";
  const body = {
    goal: trimmedGoal,
    mode: selectedMode,
    backend: options.backend || "codex",
    collaboration_mode: collaborationMode,
    dispatch: options.dispatch !== false,
    task_titles: options.taskTitles || [trimmedGoal]
  };
  if (options.workspaceId) body.workspace_id = options.workspaceId;
  if (options.workspacePath) body.workspace_path = options.workspacePath;
  if (options.proxyEnabled !== undefined) body.proxy_enabled = Boolean(options.proxyEnabled);
  if (options.httpProxy !== undefined) body.http_proxy = options.httpProxy;
  if (options.httpsProxy !== undefined) body.https_proxy = options.httpsProxy;
  if (options.noProxy !== undefined) body.no_proxy = options.noProxy;
  runActionInFlight = true;
  try {
    const payload = await fetchJson("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }, "Failed to create run");
    const run = payload.run || payload;
    const nextRunId = runIdOf(run);
    if (!nextRunId) throw new Error("New run did not include an id");
    if (newRunGoalEl) newRunGoalEl.value = "";
    const previousRunId = currentRunId;
    runsLoaded = false;
    await loadRuns(true);
    if (currentRunId === nextRunId) {
      if (previousRunId !== nextRunId) resetRunScopedState();
      await refreshRunScopedView();
    } else {
      await switchRun(nextRunId);
    }
  } catch (err) {
    alert(err?.message || String(err));
  } finally {
    runActionInFlight = false;
    renderSessionMenu();
  }
}

function setRunArchiveState(message, isError = false) {
  runArchiveMessage = String(message || "");
  runArchiveError = Boolean(isError);
  renderRunArchiveState();
}

function renderRunArchiveState() {
  if (!runArchiveStateEl) return;
  runArchiveStateEl.textContent = runArchiveMessage;
  runArchiveStateEl.title = runArchiveMessage;
  runArchiveStateEl.classList.toggle("error", runArchiveError);
}

function exportCurrentRun() {
  if (!currentRunId) {
    alert("请先选择 Run");
    return;
  }
  const includeLogs = Boolean(runExportLogsEl?.checked);
  const link = document.createElement("a");
  link.href = apiUrl("/api/run/export", { no_logs: includeLogs ? "0" : "1" });
  link.download = `aha-run-${currentRunId}.tar.gz`;
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setRunArchiveState(includeLogs ? "导出已开始，包含日志" : "导出已开始，未包含原始日志");
}

async function importRunArchive(file) {
  if (!file) return;
  const form = new FormData();
  form.append("archive", file);
  runActionInFlight = true;
  setRunArchiveState("正在导入...");
  renderSessionMenu();
  try {
    const response = await fetchWithTimeout(
      apiUrl("/api/run/import"),
      { method: "POST", body: form },
      Math.max(requestTimeoutMs, 60000)
    );
    const payload = await readJsonResponse(response, "导入失败");
    const nextRunId = String(payload.imported_run_id || runIdOf(payload.run) || "").trim();
    if (Array.isArray(payload.runs)) {
      applyRunListData({ default_run_id: defaultRunId, runs: payload.runs });
      runsLoaded = true;
      renderSessionMenu();
    } else {
      runsLoaded = false;
      await loadRuns(true);
    }
    if (nextRunId) await switchRun(nextRunId);
    setRunArchiveState(nextRunId ? `已导入 ${nextRunId}` : "导入完成");
  } catch (err) {
    const message = err?.message || String(err || "导入失败");
    setRunArchiveState(message, true);
    alert(message);
  } finally {
    runActionInFlight = false;
    renderSessionMenu();
  }
}

function setWebRestartState(message, isError = false) {
  if (!webRestartStateEl) return;
  const text = String(message || "");
  webRestartStateEl.textContent = text;
  webRestartStateEl.title = text;
  webRestartStateEl.classList.toggle("error", Boolean(isError));
}

async function restartWebService() {
  if (webRestartInFlight) return;
  if (!currentRunId) {
    alert("请先选择 Run");
    return;
  }
  webRestartInFlight = true;
  setWebRestartState("正在安排重启...");
  renderSessionMenu();
  try {
    const payload = await fetchJson(apiUrl("/api/web/restart"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host: "0.0.0.0", port: 8766 })
    }, "重启 Web 失败");
    setWebRestartState(`已安排 ${payload.host || "0.0.0.0"}:${payload.port || 8766}，等待恢复...`);
    waitForWebRestartAndReload();
  } catch (err) {
    webRestartInFlight = false;
    setWebRestartState(err?.message || String(err || "重启 Web 失败"), true);
    renderSessionMenu();
  }
}

async function waitForWebRestartAndReload() {
  await sleep(2500);
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    try {
      const response = await fetchWithTimeout(apiUrl("/api/status"), { cache: "no-store" }, 2000);
      if (response.ok) {
        window.location.reload();
        return;
      }
    } catch (_err) {
      // The socket is expected to drop while the web service restarts.
    }
    await sleep(1000);
  }
  webRestartInFlight = false;
  setWebRestartState("重启已安排；如果页面未更新，请手动刷新。");
  renderSessionMenu();
}

function closeTaskCreateDialog() {
  if (!taskCreateDialogEl) return;
  if (typeof taskCreateDialogEl.close === "function" && taskCreateDialogEl.open) {
    taskCreateDialogEl.close();
  } else {
    taskCreateDialogEl.removeAttribute("open");
  }
}

function openTaskCreateDialog() {
  if (!currentRunId) {
    alert("请先创建 Run，再添加任务。");
    return;
  }
  if (!taskCreateDialogEl) return;
  closeMobileSheets();
  closeMobileActionPanel();
  try {
    if (typeof taskCreateDialogEl.showModal === "function") {
      if (!taskCreateDialogEl.open) taskCreateDialogEl.showModal();
    } else {
      taskCreateDialogEl.setAttribute("open", "");
    }
  } catch (_err) {
    taskCreateDialogEl.setAttribute("open", "");
  }
  setTimeout(() => newTaskTitleEl?.focus(), 0);
}

function initTaskCreateDialog() {
  openTaskCreateEl?.addEventListener("click", openTaskCreateDialog);
  closeTaskCreateEl?.addEventListener("click", closeTaskCreateDialog);
  cancelTaskCreateEl?.addEventListener("click", closeTaskCreateDialog);
  taskCreateDialogEl?.addEventListener("click", event => {
    if (event.target === taskCreateDialogEl) closeTaskCreateDialog();
  });
}

const collaborationModeOptions = ["auto", "solo", "pair", "team"];
const collaborationModeDescriptions = {
  auto: "AHA 会在能提速时自动组队，否则保持 solo。",
  solo: "单人模式：main 自己完成全部工作，适合小任务和快速修改。",
  pair: "双人模式：最多 1 个 sub-agent 并行处理实现、调研或 review。",
  team: "团队模式：最多 2 个 sub-agent 处理可拆分责任区，main 负责协调和合并。"
};

function renderCollaborationModeOptions(selected = "auto") {
  return collaborationModeOptions.map(mode => (
    `<option value="${escapeHtml(mode)}" ${mode === selected ? "selected" : ""}>${escapeHtml(mode)}</option>`
  )).join("");
}

function collaborationModeDescription(mode) {
  return collaborationModeDescriptions[mode] || collaborationModeDescriptions.auto;
}

function syncCollaborationHelp(selectEl, helpEl) {
  if (!helpEl) return;
  const mode = selectEl?.value || "auto";
  helpEl.textContent = collaborationModeDescription(mode);
}

function collaborationModeMaxSubAgents(mode) {
  if (mode === "solo") return 0;
  if (mode === "pair") return 1;
  if (mode === "team") return 2;
  return Number(maxSubAgentsEl?.value || "3");
}

function collaborationModeDelegationPolicy(mode) {
  return mode === "solo" ? "disabled" : "auto";
}

function syncCollaborationFields() {
  const isAuto = collaborationModeEl?.value === "auto";
  syncCollaborationHelp(collaborationModeEl, collaborationModeHelpEl);
  if (maxSubAgentsFieldEl) {
    maxSubAgentsFieldEl.hidden = !isAuto;
    maxSubAgentsFieldEl.classList.toggle("hidden", !isAuto);
  }
  if (maxSubAgentsEl) maxSubAgentsEl.disabled = !isAuto;
}

function syncNewRunCollaborationHelp() {
  syncCollaborationHelp(newRunCollaborationEl, newRunCollaborationHelpEl);
}

function syncBootstrapCollaborationHelp(form) {
  if (!form) return;
  syncCollaborationHelp(
    form.querySelector("[data-bootstrap-collaboration-mode]"),
    form.querySelector("[data-bootstrap-collaboration-help]")
  );
}

function sidebarStorageKey(side) {
  return `aha.${side}.sidebarCollapsed`;
}

function readSidebarCollapsed(side) {
  try {
    return localStorage.getItem(sidebarStorageKey(side)) === "true";
  } catch {
    return false;
  }
}

function writeSidebarCollapsed(side, collapsed) {
  try {
    localStorage.setItem(sidebarStorageKey(side), collapsed ? "true" : "false");
  } catch {
    // localStorage can be unavailable in restricted browser modes.
  }
}

function setSidebarCollapsed(side, collapsed) {
  const className = `${side}-collapsed`;
  document.body.classList.toggle(className, collapsed);
  const expanded = String(!collapsed);
  const controls = side === "overview"
    ? [collapseOverviewEl, overviewRailToggleEl]
    : [collapseAgentsEl, agentsRailToggleEl];
  for (const control of controls) {
    if (control) control.setAttribute("aria-expanded", expanded);
  }
  writeSidebarCollapsed(side, collapsed);
}

function initDesktopSidebars() {
  setSidebarCollapsed("overview", readSidebarCollapsed("overview"));
  setSidebarCollapsed("agents", readSidebarCollapsed("agents"));
  collapseOverviewEl?.addEventListener("click", () => setSidebarCollapsed("overview", true));
  overviewRailToggleEl?.addEventListener("click", () => setSidebarCollapsed("overview", false));
  collapseAgentsEl?.addEventListener("click", () => setSidebarCollapsed("agents", true));
  agentsRailToggleEl?.addEventListener("click", () => setSidebarCollapsed("agents", false));
}

function setMobileSheet(sheet) {
  const taskOpen = sheet === "tasks";
  const agentsOpen = sheet === "agents";
  if (taskOpen || agentsOpen) closeMobileActionPanel();
  document.body.classList.toggle("mobile-tasks-open", taskOpen);
  document.body.classList.toggle("mobile-agents-open", agentsOpen);
  if (mobileSheetBackdropEl) mobileSheetBackdropEl.hidden = !taskOpen && !agentsOpen;
  openTasksSheetEl?.setAttribute("aria-expanded", String(taskOpen));
  openAgentsSheetEl?.setAttribute("aria-expanded", String(agentsOpen));
  mobileTaskSummaryEl?.setAttribute("aria-expanded", String(taskOpen));
}

function closeMobileSheets() {
  setMobileSheet(null);
}

function setMobileActionPanel(open) {
  if (!mobileActionPanelEl) return;
  if (open && messageEl.value.trim()) {
    open = false;
  }
  mobileActionPanelEl.hidden = !open;
  document.body.classList.toggle("mobile-actions-open", open);
  mobileActionsToggleEl?.setAttribute("aria-expanded", String(open));
  if (open) commandMenuEl.classList.add("hidden");
}

function closeMobileActionPanel() {
  setMobileActionPanel(false);
}

function targetInsideMobileActionPanel(target) {
  const element = target instanceof Element ? target : null;
  return Boolean(element && (mobileActionPanelEl?.contains(element) || mobileActionsToggleEl?.contains(element)));
}

function closeMobileActionPanelForOutsideEvent(event) {
  if (!mobileActionPanelEl || mobileActionPanelEl.hidden) return;
  if (targetInsideMobileActionPanel(event.target)) return;
  closeMobileActionPanel();
}

async function handleMobileAction(action) {
  closeMobileActionPanel();
  if (action === "tasks") {
    setMobileSheet("tasks");
    return;
  }
  if (action === "agents") {
    setMobileSheet("agents");
    return;
  }
  if (action === "add-task") {
    openTaskCreateDialog();
    return;
  }
  if (["conversation", "final", "logs", "context"].includes(action)) {
    await activateTab(action);
  }
}

function syncMobileActionPanel() {
  mobileActionPanelEl?.querySelectorAll("[data-mobile-action]").forEach(button => {
    const action = button.dataset.mobileAction || "";
    button.classList.toggle("active", action === activeTab);
  });
}

function syncMobileComposerAction() {
  if (!mobileActionsToggleEl) return;
  const hasMessage = Boolean(messageEl.value.trim());
  mobileActionsToggleEl.classList.toggle("sending", hasMessage);
  mobileActionsToggleEl.textContent = hasMessage ? "发送" : "+";
  mobileActionsToggleEl.setAttribute("aria-label", hasMessage ? "发送消息" : "打开工具面板");
  mobileActionsToggleEl.title = hasMessage ? "发送消息" : "打开工具面板";
  if (hasMessage) closeMobileActionPanel();
}

function mobileViewportMatches() {
  return window.matchMedia("(max-width: 640px)").matches;
}

function isKeyboardTextControl(element) {
  if (!(element instanceof HTMLElement)) return false;
  if (element.isContentEditable) return true;
  if (element instanceof HTMLTextAreaElement) return true;
  if (!(element instanceof HTMLInputElement)) return false;
  const nonTextTypes = new Set([
    "button",
    "checkbox",
    "color",
    "file",
    "hidden",
    "image",
    "radio",
    "range",
    "reset",
    "submit"
  ]);
  return !nonTextTypes.has(String(element.type || "text").toLowerCase());
}

function activeKeyboardTextControl() {
  return isKeyboardTextControl(document.activeElement) ? document.activeElement : null;
}

function mobileKeyboardInset() {
  const virtualKeyboardHeight = Number(navigator.virtualKeyboard?.boundingRect?.height || 0);
  if (virtualKeyboardHeight > 0) return virtualKeyboardHeight;
  const viewport = window.visualViewport;
  if (!viewport) return 0;
  return Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop);
}

function mobileDialogScrollerFor(element) {
  if (!taskCreateDialogEl?.open || !element || !taskCreateDialogEl.contains(element)) return null;
  return taskCreateDialogEl.querySelector(".task-dialog-panel");
}

function keepMobileControlVisible(control, keyboardInset) {
  if (!control || !mobileViewportMatches()) return;
  const scroller = mobileDialogScrollerFor(control);
  if (!scroller) return;
  const rect = control.getBoundingClientRect();
  const topLimit = 16;
  const bottomLimit = window.innerHeight - keyboardInset - 86;
  if (rect.bottom > bottomLimit) {
    scroller.scrollTop += rect.bottom - bottomLimit;
  } else if (rect.top < topLimit) {
    scroller.scrollTop -= topLimit - rect.top;
  }
}

function applyMobileViewport() {
  mobileViewportRaf = 0;
  if (!mobileViewportMatches()) {
    document.documentElement.style.setProperty("--mobile-keyboard-inset", "0px");
    document.body.classList.remove("mobile-keyboard-active");
    return;
  }
  const keyboardActive = Boolean(activeKeyboardTextControl());
  const keyboardInset = keyboardActive ? mobileKeyboardInset() : 0;
  document.body.classList.toggle("mobile-keyboard-active", keyboardActive);
  document.documentElement.style.setProperty("--mobile-keyboard-inset", `${Math.round(keyboardInset)}px`);
  if (keyboardActive) keepMobileControlVisible(activeKeyboardTextControl(), keyboardInset);
}

function scheduleMobileViewportSync() {
  if (!mobileViewportRaf) {
    mobileViewportRaf = window.requestAnimationFrame(applyMobileViewport);
  }
}

function clearMobileViewportSync() {
  if (mobileViewportRaf) window.cancelAnimationFrame(mobileViewportRaf);
  mobileViewportRaf = 0;
}

function initMobileViewport() {
  const mobileQuery = window.matchMedia("(max-width: 640px)");
  if (mobileQuery.addEventListener) {
    mobileQuery.addEventListener("change", scheduleMobileViewportSync);
  } else {
    mobileQuery.addListener(scheduleMobileViewportSync);
  }
  window.addEventListener("resize", scheduleMobileViewportSync, { passive: true });
  window.addEventListener("orientationchange", scheduleMobileViewportSync);
  window.visualViewport?.addEventListener("resize", scheduleMobileViewportSync, { passive: true });
  window.visualViewport?.addEventListener("scroll", scheduleMobileViewportSync, { passive: true });
  if (navigator.virtualKeyboard) {
    try {
      navigator.virtualKeyboard.overlaysContent = true;
    } catch (_err) {
      // Some browsers expose the API as read-only.
    }
    navigator.virtualKeyboard.addEventListener?.("geometrychange", scheduleMobileViewportSync);
  }
  document.addEventListener("focusin", event => {
    if (isKeyboardTextControl(event.target)) scheduleMobileViewportSync();
  }, true);
  document.addEventListener("focusout", event => {
    if (isKeyboardTextControl(event.target)) scheduleMobileViewportSync();
  }, true);
  window.addEventListener("pagehide", clearMobileViewportSync);
  scheduleMobileViewportSync();
}

function initMobileSheets() {
  const mobileQuery = window.matchMedia("(max-width: 640px)");
  mobileTaskSummaryEl?.addEventListener("click", () => setMobileSheet("tasks"));
  openTasksSheetEl?.addEventListener("click", () => setMobileSheet("tasks"));
  openAgentsSheetEl?.addEventListener("click", () => setMobileSheet("agents"));
  closeTasksSheetEl?.addEventListener("click", closeMobileSheets);
  closeAgentsSheetEl?.addEventListener("click", closeMobileSheets);
  mobileSheetBackdropEl?.addEventListener("click", closeMobileSheets);
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      closeMobileSheets();
      closeMobileActionPanel();
    }
  });
  const closeWhenLeavingMobile = () => {
    if (!mobileQuery.matches) {
      closeMobileSheets();
      closeMobileActionPanel();
    }
  };
  if (mobileQuery.addEventListener) {
    mobileQuery.addEventListener("change", closeWhenLeavingMobile);
  } else {
    mobileQuery.addListener(closeWhenLeavingMobile);
  }
}

function initMobileActionPanel() {
  mobileActionsToggleEl?.addEventListener("click", event => {
    if (messageEl.value.trim()) {
      event.preventDefault();
      if (sendFormEl.requestSubmit) {
        sendFormEl.requestSubmit();
      } else {
        sendFormEl.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
      }
      return;
    }
    setMobileActionPanel(Boolean(mobileActionPanelEl?.hidden));
  });
  mobileActionPanelEl?.addEventListener("click", event => {
    const button = event.target instanceof Element ? event.target.closest("[data-mobile-action]") : null;
    if (!button) return;
    handleMobileAction(button.dataset.mobileAction || "");
  });
  document.addEventListener("pointerdown", closeMobileActionPanelForOutsideEvent, true);
  document.addEventListener("focusin", closeMobileActionPanelForOutsideEvent, true);
  syncMobileActionPanel();
  syncMobileComposerAction();
}

function initSessionControl() {
  sessionToggleEl?.addEventListener("click", async event => {
    event.stopPropagation();
    setSessionMenu(!sessionMenuOpen);
    if (sessionMenuOpen) await loadRuns();
  });
  sessionMenuEl?.addEventListener("click", event => event.stopPropagation());
  sessionRefreshEl?.addEventListener("click", async () => loadRuns(true));
  runSelectEl?.addEventListener("change", async () => switchRun(runSelectEl.value));
  runExportEl?.addEventListener("click", exportCurrentRun);
  webRestartEl?.addEventListener("click", restartWebService);
  weixinConsoleEl?.addEventListener("click", event => {
    event.stopPropagation();
    if (runActionInFlight || !currentRunId) return;
    setWeixinConsoleOpen(!weixinConsoleOpen);
  });
  weixinConsolePopoverEl?.addEventListener("click", event => event.stopPropagation());
  weixinConsolePopoverEl?.addEventListener("click", event => {
    const target = event.target instanceof Element ? event.target : null;
    const actionEl = target?.closest("[data-weixin-action]");
    const action = actionEl?.getAttribute("data-weixin-action") || "";
    if (action === "pair") void startWeixinPairing();
    if (action === "refresh") void loadWeixinStatus();
    if (action === "test") void sendWeixinTestNotification();
  });
  weixinConsolePopoverEl?.addEventListener("input", event => {
    const target = event.target instanceof HTMLTextAreaElement ? event.target : null;
    if (target?.matches("[data-weixin-test-message]")) weixinState.testMessage = target.value;
  });
  weixinConsolePopoverEl?.addEventListener("change", event => {
    const target = event.target instanceof HTMLInputElement ? event.target : null;
    if (target?.matches("[data-weixin-notifications-toggle]")) void setWeixinNotificationsEnabled(target.checked);
  });
  runImportEl?.addEventListener("click", () => {
    if (runActionInFlight) return;
    runImportFileEl?.click();
  });
  runImportFileEl?.addEventListener("change", async () => {
    const file = runImportFileEl.files?.[0] || null;
    runImportFileEl.value = "";
    await importRunArchive(file);
  });
  runCreateFormEl?.addEventListener("submit", async event => {
    event.preventDefault();
    await createRun(newRunGoalEl?.value || "", newRunModeEl?.value || "research", {
      collaborationMode: newRunCollaborationEl?.value || "auto",
      workspaceId: selectedWorkspaceId(),
      workspacePath: selectedWorkspacePath()
    });
  });
  document.addEventListener("click", event => {
    const target = event.target instanceof Element ? event.target : null;
    if (sessionMenuOpen && !sessionControlEl?.contains(target)) setSessionMenu(false);
    if (weixinConsoleOpen && !weixinConsoleEl?.contains(target) && !weixinConsolePopoverEl?.contains(target)) {
      setWeixinConsoleOpen(false);
    }
  });
  document.addEventListener("pointerdown", closeWeixinConsoleForOutsideEvent, true);
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      setSessionMenu(false);
      setWeixinConsoleOpen(false);
    }
  });
  renderSessionMenu();
}

function selectedTask() {
  return (statusData?.tasks || []).find(task => task.id === selectedTaskId) || null;
}

function selectedTaskNeedsAgentDetails(task = selectedTask()) {
  return Boolean(task && taskAgentCount(task) > (task.agents || []).length);
}

function selectedAgent() {
  const task = selectedTask();
  return (task?.agents || []).find(item => item.id === agentTargetEl.value) || null;
}

function backendTarget() {
  return agentTargetEl.value || "main";
}

function isSupervisionAgent(agent) {
  const role = agent?.role || "";
  return role === "host" || role === "supervision-host";
}

function messageContextKey(taskId = selectedTaskId, target = backendTarget()) {
  return `${currentRunId || ""}::${taskId || ""}::${target || "main"}`;
}

function isAhaCommand(message) {
  return /^\/aha(?:\s|$)/i.test(String(message || "").trim());
}

function isInterruptCommand(message) {
  return /^\/aha\s+(interrupt|stop)(?:\s|$)/i.test(String(message || "").trim());
}

function selectedBackendActive() {
  const status = String(backendStatusData?.status || agentBackendProcessStatus(selectedAgent()) || "stopped").toLowerCase();
  return status === "busy";
}

function selectedTaskRealtimeActive() {
  const task = selectedTask();
  if (!task) return false;
  const turn = latestTurnTiming(task.id);
  return selectedBackendActive() || taskActivityStatus(task) !== "idle" || Boolean(turn?.running);
}

function pendingForContext(taskId = selectedTaskId, target = backendTarget()) {
  const key = messageContextKey(taskId, target);
  return pendingMessages.filter(item => item.contextKey === key);
}

function renderPendingMessages() {
  if (!pendingMessagesEl) return;
  const task = selectedTask();
  const target = backendTarget();
  const key = messageContextKey(task?.id, target);
  const items = task ? pendingForContext(task.id, target) : [];
  const interrupted = interruptedContexts.has(key);
  pendingMessagesEl.classList.toggle("hidden", !items.length && !interrupted);
  if (!items.length && !interrupted) {
    pendingMessagesEl.innerHTML = "";
    return;
  }
  const note = interrupted
    ? '<div class="pending-note">上一轮已中断。确认 pending 后点 Send，会合并发送下一轮。</div>'
    : '<div class="pending-note">Agent working 中的消息会先暂存，当前轮结束后自动合并发送。</div>';
  const list = items.map((item, index) => `
    <div class="pending-message" data-pending-id="${escapeHtml(item.id)}">
      <div>
        <strong>#${index + 1}</strong>
        <span>${escapeHtml(item.message)}</span>
      </div>
      <button type="button" class="pending-remove" data-remove-pending="${escapeHtml(item.id)}" title="删除 pending 消息">Delete</button>
    </div>
  `).join("");
  pendingMessagesEl.innerHTML = `${note}${list}`;
}

function addPendingMessage(message, task, agentId) {
  const target = agentId || "main";
  pendingMessageId += 1;
  pendingMessages.push({
    id: String(pendingMessageId),
    contextKey: messageContextKey(task.id, target),
    runId: currentRunId,
    taskId: task.id,
    agentId: target,
    role: target === "main" ? "main" : "sub",
    message,
    createdAt: new Date().toISOString()
  });
  renderPendingMessages();
}

function removePendingMessage(id) {
  const index = pendingMessages.findIndex(item => item.id === String(id));
  if (index >= 0) pendingMessages.splice(index, 1);
  renderPendingMessages();
}

function clearPendingForContext(taskId, target) {
  const key = messageContextKey(taskId, target);
  for (let index = pendingMessages.length - 1; index >= 0; index -= 1) {
    if (pendingMessages[index].contextKey === key) pendingMessages.splice(index, 1);
  }
}

function mergedPendingPrompt(items, currentMessage, interrupted) {
  const lines = [];
  if (interrupted) {
    lines.push(
      "上一轮 agent 工作被用户中断。",
      "继续前请注意：当前工作区或命令可能已有部分副作用，请基于当前实际状态判断后继续。",
      ""
    );
  }
  if (items.length) {
    lines.push("用户在你工作期间补充了以下消息，请按时间顺序合并理解并继续处理：");
    items.forEach((item, index) => {
      lines.push(`${index + 1}. [${formatLocalTimestamp(item.createdAt, item.createdAt)}] ${item.message}`);
    });
  }
  if (currentMessage) {
    if (items.length) lines.push("", "用户当前发送的新消息：");
    lines.push(currentMessage);
  }
  if (!items.length && !currentMessage && interrupted) {
    lines.push("用户中断了上一轮，但没有补充新消息。");
  }
  return lines.join("\n").trim();
}

async function sendBackendMessage(task, agentId, message) {
  const target = agentId === "main" ? "main" : agentId;
  realtimeDebug("send.request", { task_id: task.id, target, message_len: message.length });
  await prepareRealtimeCatchupBaseline();
  try {
    const response = await fetchJson(apiUrl("/api/send"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(runScopedPayload({
        target,
        role: agentId === "main" ? "main" : "sub",
        task_id: task.id,
        from_agent: "browser",
        to_agent: agentId,
        message,
        sender: "browser"
      }))
    }, "Failed to send message");
    realtimeDebug("send.response", {
      task_id: task.id,
      target,
      ok: Boolean(response?.ok),
      handled_by: response?.handled_by || "",
      backend_started: Boolean(response?.backend),
      interrupted: Boolean(response?.interrupt?.interrupted)
    });
    return response;
  } catch (err) {
    realtimeDebug("send.error", { task_id: task.id, target, error: err?.message || String(err) });
    throw err;
  }
}

async function flushPendingMessages(task, agentId, currentMessage = "", options = {}) {
  const target = agentId || "main";
  const key = messageContextKey(task.id, target);
  if (pendingSendInFlight) return null;
  const items = pendingForContext(task.id, target);
  const interrupted = interruptedContexts.has(key);
  if (!items.length && !currentMessage && !interrupted) return null;
  if (options.auto && interrupted) return null;
  if (options.auto && terminalTaskStatuses.has(taskCurrentStatus(task))) return null;
  pendingSendInFlight = true;
  try {
    const message = items.length || interrupted ? mergedPendingPrompt(items, currentMessage, interrupted) : currentMessage;
    const response = await sendBackendMessage(task, target, message);
    clearPendingForContext(task.id, target);
    interruptedContexts.delete(key);
    renderPendingMessages();
    return response;
  } finally {
    pendingSendInFlight = false;
  }
}

async function maybeAutoFlushPending() {
  const task = selectedTask();
  if (!task || selectedBackendActive()) return null;
  const agentId = backendTarget();
  if (!pendingForContext(task.id, agentId).length) return null;
  const response = await flushPendingMessages(task, agentId, "", { auto: true });
  if (!response) return null;
  await catchUpRealtimeEvents();
  conversationAutoFollow = true;
  return response;
}

function agentBackendProcessStatus(agent) {
  const raw = String(agent?.backend_process_status || "stopped").toLowerCase();
  if (raw === "running" || raw === "busy") return raw;
  return "stopped";
}

function agentBackendProcessLabel(agent) {
  return agentBackendProcessStatus(agent).toUpperCase();
}

function agentLifecycleStatus(agent) {
  return String(agent?.status || "pending").toLowerCase();
}

function agentLifecycleLabel(agent) {
  return agentLifecycleStatus(agent).toUpperCase();
}

function agentStatusTiming(agent) {
  const status = agentLifecycleStatus(agent);
  const startedAt =
    parseTimestamp(agent?.status_started_at) ||
    (status === "running" ? parseTimestamp(agent?.started_at) : null) ||
    parseTimestamp(agent?.last_active_at);
  if (!startedAt) return null;
  const terminal = terminalAgentStatuses.has(status);
  const finishedAt = terminal ? parseTimestamp(agent?.finished_at) || parseTimestamp(agent?.last_active_at) : null;
  const endAt = terminal ? (finishedAt || startedAt) : Date.now();
  return {
    status,
    startedAt,
    finishedAt,
    elapsedMs: endAt - startedAt,
    running: !terminal
  };
}

function agentStatusTimingText(agent) {
  const timing = agentStatusTiming(agent);
  if (!timing) return "";
  return `${timing.status} · ${formatDuration(timing.elapsedMs)}`;
}

function taskCurrentStatus(task) {
  return String(task?.current_status || task?.status || "pending").toLowerCase();
}

function taskOutcomeStatus(task) {
  const raw = task?.outcome_status || (terminalTaskStatuses.has(taskCurrentStatus(task)) ? taskCurrentStatus(task) : "");
  return raw ? String(raw).toLowerCase() : "";
}

function taskActivityStatus(task) {
  return String(task?.activity_status || (taskCurrentStatus(task) === "running" ? "running" : "idle")).toLowerCase();
}

function taskDisplayStatus(task) {
  return String(task?.display_status || taskOutcomeStatus(task) || taskCurrentStatus(task)).toLowerCase();
}

function taskStatusBadges(task) {
  if (task?.hidden) return '<span class="status hidden">hidden</span>';
  const primary = taskDisplayStatus(task);
  const activity = taskActivityStatus(task);
  const badges = [`<span class="status ${escapeHtml(primary)}">${escapeHtml(primary)}</span>`];
  if (activity !== "idle" && activity !== primary) {
    badges.push(`<span class="status activity ${escapeHtml(activity)}">${escapeHtml(activity)}</span>`);
  }
  return badges.join("");
}

function taskProxyConfigured(task) {
  return Boolean(task?.preferred_http_proxy || task?.preferred_https_proxy);
}

function taskProxyBadge(task) {
  if (!taskProxyConfigured(task)) return '<span class="status proxy-unset">proxy unset</span>';
  if (task?.preferred_proxy_enabled) return '<span class="status proxy-on">proxy default on</span>';
  return '<span class="status proxy-off">proxy default off</span>';
}

function taskProxySummary(task) {
  if (!task) return "";
  const parts = [];
  if (task.preferred_http_proxy) parts.push("HTTP");
  if (task.preferred_https_proxy) parts.push("HTTPS");
  if (task.preferred_no_proxy) parts.push("NO_PROXY");
  return parts.length ? `${task.preferred_proxy_enabled ? "default on" : "default off"} · ${parts.join(" · ")}` : "not configured";
}

function inferredTaskCollaborationMode(task) {
  const mode = String(task?.collaboration_mode || "").toLowerCase();
  if (collaborationModeOptions.includes(mode)) return mode;
  if (task?.delegation_policy === "disabled") return "solo";
  const rawLimit = task?.max_sub_agents;
  const limit = rawLimit === undefined || rawLimit === null ? 3 : Number(rawLimit || 0);
  if (limit === 0) return "solo";
  if (limit === 1) return "pair";
  if (limit === 2) return "team";
  return "auto";
}

function taskCollaborationSummary(task) {
  const mode = inferredTaskCollaborationMode(task);
  const rawLimit = task?.max_sub_agents;
  const limit = rawLimit === undefined || rawLimit === null ? 3 : Number(rawLimit || 0);
  if (mode === "auto") return `auto (${limit})`;
  if (mode === "solo") return "solo";
  if (mode === "pair") return "pair (1)";
  if (mode === "team") return "team (2)";
  return mode;
}

const supervisionAskUserGateDefs = [
  ["real_ui_validation", "Real UI/device"],
  ["scope_change", "Scope change"],
  ["commit_merge_delete", "Commit/merge/delete"],
  ["destructive_or_high_risk", "High risk"],
  ["permissions_or_external", "Permissions/external"],
  ["product_preference", "Product preference"]
];

function defaultAskUserGates() {
  return Object.fromEntries(supervisionAskUserGateDefs.map(([key]) => [key, true]));
}

function normalizeAskUserGates(value) {
  const gates = defaultAskUserGates();
  if (value && typeof value === "object") {
    supervisionAskUserGateDefs.forEach(([key]) => {
      if (Object.prototype.hasOwnProperty.call(value, key)) gates[key] = Boolean(value[key]);
    });
  }
  return gates;
}

function renderAskUserGateControls(containerEl, gates) {
  if (!containerEl) return;
  const normalized = normalizeAskUserGates(gates);
  containerEl.innerHTML = supervisionAskUserGateDefs.map(([key, label]) => `
    <label>
      <input type="checkbox" data-supervision-ask-user-gate="${escapeHtml(key)}" ${normalized[key] ? "checked" : ""}>
      <span>${escapeHtml(label)}</span>
    </label>
  `).join("");
}

function readAskUserGateControls(containerEl) {
  const gates = defaultAskUserGates();
  if (!containerEl) return gates;
  containerEl.querySelectorAll("[data-supervision-ask-user-gate]").forEach(input => {
    gates[input.dataset.supervisionAskUserGate] = Boolean(input.checked);
  });
  return gates;
}

function taskSupervisionPolicy(task) {
  const policy = task?.supervision && typeof task.supervision === "object" ? task.supervision : {};
  return {
    mode: policy.mode === "assisted" ? "assisted" : "manual",
    host_backend: policy.host_backend || "stub",
    real_agent_enabled: Boolean(policy.real_agent_enabled),
    max_rounds: Number(policy.max_rounds || 5),
    ask_user_gates: normalizeAskUserGates(policy.ask_user_gates)
  };
}

function taskSupervisionModeValue(policy) {
  if (policy.mode !== "assisted") return "manual";
  if (policy.host_backend === "codex" && policy.real_agent_enabled) return "assisted_codex";
  if (policy.host_backend === "claude" && policy.real_agent_enabled) return "assisted_claude";
  return "assisted_stub";
}

function taskSupervisionBadge(task) {
  const policy = taskSupervisionPolicy(task);
  const label = policy.mode === "assisted" && policy.real_agent_enabled ? `${policy.host_backend} host` : (policy.mode === "assisted" ? "assisted stub" : "manual");
  return `<span class="status supervision-${escapeHtml(policy.mode)}">${escapeHtml(label)}</span>`;
}

function taskSupervisionSummary(task) {
  const policy = taskSupervisionPolicy(task);
  if (policy.mode === "manual") return "manual";
  const askCount = Object.values(policy.ask_user_gates).filter(Boolean).length;
  return `${policy.mode}${policy.mode === "assisted" ? ` via ${policy.host_backend}` : ""} | max rounds ${policy.max_rounds} | ask user ${askCount}/${supervisionAskUserGateDefs.length}`;
}

function taskAgentCount(task) {
  const value = Number(task?.agent_count);
  if (Number.isFinite(value)) return value;
  return (task?.agents || []).length;
}

function taskSupervisionPayloadFromMode(selectedMode, maxRoundsValue, askUserGates) {
  const assisted = selectedMode !== "manual";
  const codexHost = selectedMode === "assisted_codex";
  const claudeHost = selectedMode === "assisted_claude";
  return {
    mode: assisted ? "assisted" : "manual",
    host_backend: codexHost ? "codex" : (claudeHost ? "claude" : "stub"),
    real_agent_enabled: codexHost || claudeHost,
    max_rounds: Number(maxRoundsValue || "5"),
    ask_user_gates: normalizeAskUserGates(askUserGates)
  };
}

function applyProxyDefaultValues(httpEl, httpsEl, noProxyEl, enabledEl) {
  let changed = false;
  if (httpEl && !httpEl.value.trim()) {
    httpEl.value = defaultHttpProxy;
    changed = true;
  }
  if (httpsEl && !httpsEl.value.trim()) {
    httpsEl.value = defaultHttpsProxy;
    changed = true;
  }
  if (noProxyEl && !noProxyEl.value.trim()) {
    noProxyEl.value = defaultNoProxy;
    changed = true;
  }
  if ((httpEl?.value.trim() || httpsEl?.value.trim()) && enabledEl && !enabledEl.checked) {
    enabledEl.checked = true;
    changed = true;
  }
  return changed;
}

function fillTaskCreateProxyDefaults() {
  return applyProxyDefaultValues(taskHttpProxyEl, taskHttpsProxyEl, taskNoProxyEl, taskProxyEnabledEl);
}

function fillSelectedTaskProxyDefaults() {
  return applyProxyDefaultValues(selectedTaskHttpProxyEl, selectedTaskHttpsProxyEl, selectedTaskNoProxyEl, selectedTaskProxyEnabledEl);
}

function fillBootstrapProxyDefaults(input) {
  const form = input?.closest?.("[data-bootstrap-run-form]");
  if (!form) return false;
  return applyProxyDefaultValues(
    form.querySelector("[data-bootstrap-http-proxy]"),
    form.querySelector("[data-bootstrap-https-proxy]"),
    form.querySelector("[data-bootstrap-no-proxy]"),
    form.querySelector("[data-bootstrap-proxy-enabled]")
  );
}

function setCreateProxyDefaultsFromInputs() {
  const configured = Boolean(taskHttpProxyEl?.value.trim() || taskHttpsProxyEl?.value.trim());
  if (configured && taskProxyEnabledEl && !taskProxyEnabledEl.checked) taskProxyEnabledEl.checked = true;
  if (configured && taskNoProxyEl && !taskNoProxyEl.value.trim()) taskNoProxyEl.value = defaultNoProxy;
}

function renderTaskProxyEditor() {
  if (!taskProxyEditorEl || !taskProxyFormEl) return;
  const task = selectedTask();
  const disabled = !task;
  taskProxyFormEl.querySelectorAll("input, button").forEach(item => {
    item.disabled = disabled;
  });
  if (!task) {
    if (taskProxyStateEl) taskProxyStateEl.textContent = "Select a task to edit proxy.";
    if (selectedTaskProxyEnabledEl) selectedTaskProxyEnabledEl.checked = false;
    if (selectedTaskHttpProxyEl) selectedTaskHttpProxyEl.value = "";
    if (selectedTaskHttpsProxyEl) selectedTaskHttpsProxyEl.value = "";
    if (selectedTaskNoProxyEl) selectedTaskNoProxyEl.value = defaultNoProxy;
    return;
  }
  if (selectedTaskProxyEnabledEl) selectedTaskProxyEnabledEl.checked = Boolean(task.preferred_proxy_enabled);
  if (selectedTaskHttpProxyEl) selectedTaskHttpProxyEl.value = task.preferred_http_proxy || "";
  if (selectedTaskHttpsProxyEl) selectedTaskHttpsProxyEl.value = task.preferred_https_proxy || "";
  if (selectedTaskNoProxyEl) selectedTaskNoProxyEl.value = task.preferred_no_proxy || defaultNoProxy;
  if (taskProxyStateEl) taskProxyStateEl.textContent = taskProxySummary(task);
}

function renderTaskSupervisionEditor() {
  if (!taskSupervisionEditorEl || !taskSupervisionFormEl) return;
  const task = selectedTask();
  const disabled = !task;
  const applyDisabledState = () => taskSupervisionFormEl.querySelectorAll("input, select, button").forEach(item => {
    item.disabled = disabled;
  });
  if (!task) {
    if (selectedTaskSupervisionModeEl) selectedTaskSupervisionModeEl.value = "manual";
    if (selectedTaskSupervisionMaxRoundsEl) selectedTaskSupervisionMaxRoundsEl.value = "5";
    renderAskUserGateControls(selectedTaskSupervisionAskUserGatesEl, defaultAskUserGates());
    if (taskSupervisionStateEl) taskSupervisionStateEl.textContent = "Select a task to edit supervision.";
    syncTaskSupervisionModeFields();
    applyDisabledState();
    return;
  }
  const policy = taskSupervisionPolicy(task);
  if (selectedTaskSupervisionModeEl) selectedTaskSupervisionModeEl.value = taskSupervisionModeValue(policy);
  if (selectedTaskSupervisionMaxRoundsEl) selectedTaskSupervisionMaxRoundsEl.value = String(policy.max_rounds || 5);
  renderAskUserGateControls(selectedTaskSupervisionAskUserGatesEl, policy.ask_user_gates);
  syncTaskSupervisionModeFields();
  if (taskSupervisionStateEl) taskSupervisionStateEl.textContent = taskSupervisionSummary(task);
  applyDisabledState();
}

function syncTaskSupervisionModeFields() {
  syncSupervisionModeFields(selectedTaskSupervisionModeEl, selectedTaskSupervisionMaxRoundsFieldEl, selectedTaskSupervisionAskUserFieldEl);
}

function syncCreateTaskSupervisionModeFields() {
  syncSupervisionModeFields(taskSupervisionModeEl, taskSupervisionMaxRoundsFieldEl, taskSupervisionAskUserFieldEl);
}

function syncSupervisionModeFields(modeEl, maxRoundsFieldEl, askUserFieldEl) {
  const manual = modeEl?.value === "manual";
  maxRoundsFieldEl?.classList.toggle("hidden", manual);
  if (maxRoundsFieldEl) maxRoundsFieldEl.hidden = manual;
  askUserFieldEl?.classList.toggle("hidden", manual);
  if (askUserFieldEl) askUserFieldEl.hidden = manual;
}

function visibleTasks() {
  const tasks = statusData?.tasks || [];
  return showHiddenEl.checked ? tasks : tasks.filter(task => !task.hidden);
}

function taskActivityMillis(task) {
  const candidates = [
    task?.started_at,
    task?.finished_at,
    task?.hidden_at,
    ...(task?.agents || []).flatMap(agent => [
      agent.last_active_at,
      agent.started_at,
      agent.finished_at,
      agent.session_updated_at
    ])
  ];
  return Math.max(0, ...candidates.map(parseTimestamp).filter(value => value !== null));
}

function defaultTaskId(tasks) {
  if (!tasks.length) return null;
  return tasks.reduce((latest, task) => (taskActivityMillis(task) >= taskActivityMillis(latest) ? task : latest), tasks[0]).id;
}

function pathName(path) {
  if (!path) return "-";
  const trimmed = String(path).replace(/\/+$/, "");
  return trimmed.split("/").filter(Boolean).pop() || trimmed || "-";
}

function selectOptions(options, current) {
  return options.map(option => `<option value="${escapeHtml(option)}" ${option === current ? "selected" : ""}>${escapeHtml(option)}</option>`).join("");
}

function matchingSlashCommands() {
  const value = messageEl.value.trimStart();
  if (!value.startsWith("/")) return [];
  const query = value.toLowerCase();
  const agent = selectedAgent();
  const agentCommands = backendCommands.get(agent?.backend || "codex") || [];
  const slashCommands = [...ahaSlashCommands, ...agentCommands];
  return slashCommands.filter(item => item.name.toLowerCase().startsWith(query) || item.insert.toLowerCase().startsWith(query));
}

function renderCommandMenu() {
  const commands = matchingSlashCommands();
  if (!commands.length) {
    commandMenuEl.classList.add("hidden");
    commandMenuEl.innerHTML = "";
    return;
  }
  commandSelection = Math.min(commandSelection, commands.length - 1);
  commandMenuEl.classList.remove("hidden");
  commandMenuEl.innerHTML = commands.map((item, index) => `
    <button class="command-item ${index === commandSelection ? "active" : ""}" type="button" data-command-index="${index}">
      <span class="command-scope">${escapeHtml(item.scope)}</span>
      <span class="command-name">${escapeHtml(item.name)}</span>
      <span class="command-desc">${escapeHtml(item.desc)}</span>
    </button>
  `).join("");
}

function applySlashCommand(index) {
  const command = matchingSlashCommands()[index];
  if (!command) return;
  messageEl.value = command.insert;
  messageEl.focus();
  commandMenuEl.classList.add("hidden");
  syncMobileComposerAction();
}

function markAgentsPanelEditing(durationMs = 10000) {
  agentsPanelEditingUntil = Date.now() + durationMs;
}

function isAgentsPanelEditing() {
  const active = document.activeElement;
  return (
    Date.now() < agentsPanelEditingUntil ||
    (active instanceof Element && (agentsEl.contains(active) || agentTargetEl.contains(active)))
  );
}

function markTaskProxyEditing(durationMs = 10000) {
  taskProxyEditingUntil = Date.now() + durationMs;
}

function markTaskSupervisionEditing(durationMs = 10000) {
  taskSupervisionEditingUntil = Date.now() + durationMs;
}

function isTaskProxyEditing() {
  const active = document.activeElement;
  return (
    Date.now() < taskProxyEditingUntil ||
    (active instanceof Element && Boolean(taskProxyFormEl?.contains(active)))
  );
}

function isTaskSupervisionEditing() {
  const active = document.activeElement;
  return (
    Date.now() < taskSupervisionEditingUntil ||
    (active instanceof Element && Boolean(taskSupervisionFormEl?.contains(active)))
  );
}

function eventData(event) {
  return event.data || {};
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
  if (event.type === "message" && /^task-\d+$/.test(data.target || "")) return data.target;
  return null;
}

function isTaskEvent(event, taskId) {
  return eventTaskId(event) === taskId;
}

function taskEvents(taskId) {
  return allEvents.filter(event => isTaskEvent(event, taskId));
}

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
  "agent_finished",
  "agent_interrupted",
  "workspace_missing"
]);

function isTimelineEvent(event) {
  return timelineEventTypes.has(event.type);
}

function taskTimelineEvents(taskId) {
  return taskEvents(taskId).filter(isTimelineEvent);
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
  if (event.type === "message") {
    addAgentRef(refs, data.sender);
    if (["role", "from_agent", "to_agent", "sender", "target"].some(key => String(data[key] || "").toLowerCase() === "aha")) refs.add("main");
  }
  if (!refs.size && (
    event.type.startsWith("agent_") ||
    event.type.startsWith("task_") ||
    supervisionEventTypes.has(event.type) ||
    event.type === "workspace_missing"
  )) {
    refs.add("main");
  }
  return refs;
}

function eventMatchesSelectedAgent(event) {
  return eventMatchesAgent(event, backendTarget());
}

function eventMatchesAgent(event, target) {
  return eventAgentRefs(event).has(target || "main");
}

function agentTimelineEvents(taskId, target = backendTarget()) {
  return taskTimelineEvents(taskId).filter(event => eventMatchesAgent(event, target));
}

function conversationKey(taskId = selectedTaskId, target = backendTarget()) {
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

function parseConversationKey(key) {
  const index = key.indexOf("::");
  return index < 0 ? { taskId: key, target: "main" } : { taskId: key.slice(0, index), target: key.slice(index + 2) || "main" };
}

function eventIdentity(event) {
  return `${event.ts || ""}|${event.type || ""}|${JSON.stringify(eventData(event))}`;
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

function conversationState(taskId = selectedTaskId, target = backendTarget()) {
  const key = conversationKey(taskId, target);
  if (!conversationStates.has(key)) {
    conversationStates.set(key, { events: [], beforeOffset: null, hasMore: true, initialized: false, loading: false, error: "", backendSession: null, categoryKey: "" });
  }
  return conversationStates.get(key);
}

function conversationSourceEvents(taskId, target = backendTarget()) {
  const state = conversationStates.get(conversationKey(taskId, target));
  return state?.initialized ? state.events : agentTimelineEvents(taskId, target);
}

function conversationBackendSession(taskId, target = backendTarget()) {
  const state = conversationStates.get(conversationKey(taskId, target));
  return state?.backendSession || null;
}

function normalizedMessageEndpoint(value) {
  return String(value || "").trim().toLowerCase();
}

function messageDisplaySender(data) {
  return normalizedMessageEndpoint(data.display_sender || data.sender || data.from_agent);
}

function messageDisplayTarget(data) {
  return normalizedMessageEndpoint(data.display_target || data.to_agent || data.target);
}

function isMainBrowserMessage(event) {
  if (event.type !== "message") return false;
  const data = eventData(event);
  return messageDisplaySender(data) === "main" && messageDisplayTarget(data) === "browser";
}

function isMainHostSupervisionMirror(event, text) {
  if (event.type !== "message") return false;
  const data = eventData(event);
  const target = messageDisplayTarget(data);
  return (
    String(data.message || "").trim() === text &&
    messageDisplaySender(data) === "main" &&
    target &&
    !["browser", "system", "aha", "main"].includes(target) &&
    Boolean(data.display_target || data.agent_id)
  );
}

function dedupedConversationEvents(taskId, target = backendTarget()) {
  const events = conversationSourceEvents(taskId, target);
  const consumedAgentMessages = new Set();
  const mirroredMainBrowserMessages = new Set();
  events.forEach((event, index) => {
    const data = eventData(event);
    if (event.type === "agent_message") {
      const text = String(data.text || "").trim();
      const agent = String(data.target || "main").trim();
      if (!text || isAhaActionEnvelopeText(text)) return;
      const consumed = events.slice(index + 1).some(candidate => {
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
      const mirroredToHost = events.some((candidate, candidateIndex) => (
        candidateIndex !== index && isMainHostSupervisionMirror(candidate, text)
      ));
      if (mirroredToHost) mirroredMainBrowserMessages.add(index);
    }
  });
  return events.filter((event, index) => {
    if (event.type === "agent_message") {
      const text = String(eventData(event).text || "").trim();
      if (target === "main" && isAhaActionEnvelopeText(text)) return false;
      if (consumedAgentMessages.has(index)) return false;
    }
    if (event.type === "message" && mirroredMainBrowserMessages.has(index)) return false;
    return true;
  });
}

function conversationEventCategory(event) {
  if (event.type === "agent_message") return "chat";
  if (event.type === "agent_usage" || event.type === "agent_prompt_metrics") return "usage";
  if (event.type === "agent_command_started" || event.type === "agent_command_finished") return "commands";
  if (event.type === "message") return "chat";
  return "runtime";
}

function taskConversationEvents(taskId) {
  return dedupedConversationEvents(taskId).filter(event => conversationFilters[conversationEventCategory(event)]);
}

function conversationFilterCounts(taskId) {
  const counts = Object.fromEntries(conversationFilterOptions.map(item => [item.key, 0]));
  for (const event of dedupedConversationEvents(taskId)) {
    counts[conversationEventCategory(event)] += 1;
  }
  return counts;
}

function promptMetricCandidateEvents(taskId, target = backendTarget()) {
  const candidates = [...conversationSourceEvents(taskId, target), ...taskEvents(taskId)];
  const seen = new Set();
  return candidates
    .filter(event => isTaskEvent(event, taskId) && eventMatchesAgent(event, target))
    .filter(event => {
      const id = eventIdentity(event);
      if (seen.has(id)) return false;
      seen.add(id);
      return true;
    })
    .sort((left, right) => conversationEventOrder(left) - conversationEventOrder(right));
}

function latestTurnStartOrder(taskId, target = backendTarget()) {
  const events = conversationSourceEvents(taskId, target).filter(event => eventMatchesAgent(event, target));
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index].type === "agent_started") return conversationEventOrder(events[index]);
  }
  return null;
}

function latestTurnEvent(taskId, type, target = backendTarget()) {
  const startOrder = latestTurnStartOrder(taskId, target);
  const events = promptMetricCandidateEvents(taskId, target).filter(event => (
    event.type === type &&
    (startOrder == null || conversationEventOrder(event) >= startOrder)
  ));
  return events.length ? events[events.length - 1] : null;
}

function latestPromptMetricsEvent(taskId, target = backendTarget()) {
  const events = promptMetricCandidateEvents(taskId, target).filter(event => event.type === "agent_prompt_metrics");
  return latestTurnEvent(taskId, "agent_prompt_metrics", target) || (events.length ? events[events.length - 1] : null);
}

function hasPromptMetricsForLatestTurn(taskId, target = backendTarget()) {
  const startOrder = latestTurnStartOrder(taskId, target);
  if (startOrder != null) return Boolean(latestTurnEvent(taskId, "agent_prompt_metrics", target));
  return Boolean(latestPromptMetricsEvent(taskId, target));
}

function latestAgentUsageEvent(taskId, target = backendTarget()) {
  const events = promptMetricCandidateEvents(taskId, target).filter(event => event.type === "agent_usage");
  return latestTurnEvent(taskId, "agent_usage", target) || (events.length ? events[events.length - 1] : null);
}

function usageMetricsStatus(taskId, usageEvent, target = backendTarget()) {
  const startOrder = latestTurnStartOrder(taskId, target);
  if (!usageEvent) {
    return { label: startOrder == null ? "no usage" : "pending", className: "usage-pending" };
  }
  if (startOrder != null && conversationEventOrder(usageEvent) >= startOrder) {
    return { label: "current turn", className: "usage-current" };
  }
  if (startOrder != null) {
    return { label: "previous turn", className: "usage-previous" };
  }
  return { label: "latest", className: "usage-current" };
}

function ahaInputMetricsStatus(taskId, metricsEvent, target = backendTarget()) {
  const startOrder = latestTurnStartOrder(taskId, target);
  if (!metricsEvent) return { label: "none", className: "prompt-none" };
  if (startOrder != null && conversationEventOrder(metricsEvent) >= startOrder) {
    return { label: "current", className: "prompt-current" };
  }
  return { label: "latest", className: "prompt-latest" };
}

function latestContextOverflowEvent(taskId, target = backendTarget()) {
  const events = promptMetricCandidateEvents(taskId, target).filter(event => event.type === "agent_context_overflow");
  const latestTurnOverflow = latestTurnEvent(taskId, "agent_context_overflow", target);
  if (latestTurnOverflow) return latestTurnOverflow;
  return events.length ? events[events.length - 1] : null;
}

function promptMetricsKey(taskId, target = backendTarget()) {
  return `${taskId || ""}:${target || ""}`;
}

function compactResetState(taskId, target = backendTarget()) {
  return compactResetStates.get(promptMetricsKey(taskId, target)) || null;
}

function formatMetricNumber(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return "0";
  return new Intl.NumberFormat("en-US").format(number);
}

function formatMetricCompact(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return "0";
  if (number < 1000) return String(Math.round(number));
  if (number < 1000000) {
    const valueInK = number / 1000;
    return `${valueInK < 10 ? valueInK.toFixed(1) : Math.round(valueInK)}k`;
  }
  return `${(number / 1000000).toFixed(1)}m`;
}

function formatMetricBytes(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return "0 B";
  if (number < 1024) return `${formatMetricNumber(number)} B`;
  if (number < 1024 * 1024) return `${(number / 1024).toFixed(1)} KB`;
  return `${(number / 1024 / 1024).toFixed(2)} MB`;
}

function formatMetricCountChars(count, chars, noun) {
  const safeCount = Number(count || 0);
  const safeChars = Number(chars || 0);
  return `${formatMetricNumber(safeCount)} ${noun} · ${formatMetricNumber(safeChars)} chars`;
}

const BACKEND_SESSION_LARGE_BYTES = 5 * 1024 * 1024;
const BACKEND_SESSION_LARGE_CHARS = 1000 * 1000;

function backendSessionStatus(backendSession, overflow = false) {
  const analysis = backendSession?.analysis || {};
  const hasSessionId = Boolean(backendSession?.id);
  if (!backendSession?.exists) {
    return hasSessionId
      ? { label: "missing", className: "session-missing" }
      : { label: "none", className: "session-none" };
  }
  if (analysis.error) return { label: "error", className: "session-error" };
  if (overflow) return { label: "overflow", className: "session-overflow" };
  const sessionSize = Number(backendSession.size_bytes || 0);
  const payloadChars = Number(analysis.total_payload_text_chars || 0);
  const promptChars = Number(analysis.aha_prompt_total_chars || 0);
  const toolChars = Number(analysis.tool_output_chars || 0);
  const assistantChars = Number(analysis.assistant_message_chars || 0);
  const isLarge = (
    sessionSize >= BACKEND_SESSION_LARGE_BYTES ||
    payloadChars >= BACKEND_SESSION_LARGE_CHARS ||
    promptChars >= BACKEND_SESSION_LARGE_CHARS ||
    toolChars + assistantChars >= BACKEND_SESSION_LARGE_CHARS
  );
  return isLarge
    ? { label: "large", className: "session-large" }
    : { label: "active", className: "session-active" };
}

function metricMapRows(counts, chars = null) {
  const keys = new Set([...Object.keys(counts || {}), ...Object.keys(chars || {})]);
  return Array.from(keys).map(name => ({
    name,
    count: Number((counts || {})[name] || 0),
    chars: chars ? Number(chars[name] || 0) : null
  })).sort((left, right) => {
    const leftValue = left.chars == null ? left.count : left.chars;
    const rightValue = right.chars == null ? right.count : right.chars;
    return rightValue - leftValue || left.name.localeCompare(right.name);
  });
}

function renderSessionMapRows(rows, emptyLabel = "none") {
  if (!rows.length) {
    return `<div class="session-breakdown-empty">${escapeHtml(emptyLabel)}</div>`;
  }
  return rows.map(row => `
    <div class="session-breakdown-row">
      <span>${escapeHtml(row.name)}</span>
      <code>${escapeHtml(formatMetricNumber(row.count))}</code>
      ${row.chars == null ? "" : `<code>${escapeHtml(formatMetricNumber(row.chars))}</code>`}
    </div>
  `).join("");
}

function renderSessionBreakdown(analysis) {
  if (!analysis || !Object.keys(analysis).length) return "";
  if (analysis.error) {
    return `<div class="session-breakdown-error">${escapeHtml(analysis.error)}</div>`;
  }
  const totals = [
    `payload ${formatMetricNumber(analysis.total_payload_text_chars || 0)} chars`,
    `assistant ${formatMetricNumber(analysis.assistant_message_chars || 0)} chars`,
    `mirrors ${formatMetricNumber(analysis.event_msg_prompt_mirror_total_chars || 0)} chars`,
    `parse errors ${formatMetricNumber(analysis.parse_errors || 0)}`
  ];
  const latest = Array.isArray(analysis.latest_aha_prompts) ? analysis.latest_aha_prompts : [];
  return `
    <details class="metrics-breakdown session-breakdown" data-metrics-breakdown="session">
      <summary>Session breakdown</summary>
      <div class="session-breakdown-kpis">
        ${totals.map(item => `<code>${escapeHtml(item)}</code>`).join("")}
      </div>
      <div class="session-breakdown-group">
        <strong>AHA prompts</strong>
        ${renderSessionMapRows(metricMapRows(analysis.aha_prompt_counts, analysis.aha_prompt_chars))}
      </div>
      <div class="session-breakdown-group">
        <strong>Event mirrors</strong>
        ${renderSessionMapRows(metricMapRows(analysis.event_msg_prompt_mirror_counts, analysis.event_msg_prompt_mirror_chars))}
      </div>
      <div class="session-breakdown-group">
        <strong>Response items</strong>
        ${renderSessionMapRows(metricMapRows(analysis.response_item_counts, analysis.response_item_chars))}
      </div>
      <div class="session-breakdown-group">
        <strong>Record types</strong>
        ${renderSessionMapRows(metricMapRows(analysis.type_counts))}
      </div>
      <div class="session-breakdown-group">
        <strong>Latest prompts</strong>
        ${latest.length ? latest.map(item => `
          <div class="session-breakdown-row">
            <span>${escapeHtml(`${item.mode || "unknown"} @ line ${item.line || "-"}`)}</span>
            <code>${escapeHtml(formatMetricNumber(item.chars || 0))}</code>
          </div>
        `).join("") : `<div class="session-breakdown-empty">none</div>`}
      </div>
    </details>
  `;
}

function renderAhaInputBreakdown(data, rows) {
  const flags = [
    data.prompt_mode ? `mode ${data.prompt_mode}` : "",
    data.source ? `source ${data.source}` : "",
    data.target ? `target ${data.target}` : "",
    data.sender ? `sender ${data.sender}` : "",
    data.task_id ? `task ${data.task_id}` : "",
    data.is_finalization ? "finalization" : "",
    data.is_agent_command ? "agent command" : ""
  ].filter(Boolean);
  return `
    <details class="metrics-breakdown aha-breakdown" data-metrics-breakdown="aha">
      <summary>AHA input breakdown</summary>
      <div class="session-breakdown-kpis">
        ${(flags.length ? flags : ["no prompt metadata"]).map(item => `<code>${escapeHtml(item)}</code>`).join("")}
      </div>
      <div class="session-breakdown-group">
        <strong>Components</strong>
        ${rows.length ? rows.map(row => `
          <div class="session-breakdown-row metrics-breakdown-row-wide">
            <span>${escapeHtml(row.name)}</span>
            <code>${escapeHtml(formatMetricNumber(row.chars))}</code>
            <code>${escapeHtml(formatMetricBytes(row.bytes))}</code>
            <code>${escapeHtml(formatMetricNumber(row.lines))} lines</code>
          </div>
        `).join("") : `<div class="session-breakdown-empty">none</div>`}
      </div>
    </details>
  `;
}

function usageCacheReadTokens(usage) {
  return Number(usage.cached_input_tokens ?? usage.cache_read_input_tokens ?? 0);
}

function usageCacheCreationTokens(usage) {
  return Number(usage.cache_creation_input_tokens ?? 0);
}

function renderUsageBreakdown(usage, usageStatus, source) {
  const isClaude = String(source || "").includes("claude");
  const inputTokens = Number(usage.input_tokens || 0);
  const cachedTokens = usageCacheReadTokens(usage);
  const cacheCreationTokens = usageCacheCreationTokens(usage);
  const outputTokens = Number(usage.output_tokens || 0);
  const reasoningTokens = Number(usage.reasoning_output_tokens || 0);
  const effectiveInputTokens = isClaude ? inputTokens + cachedTokens + cacheCreationTokens : inputTokens;
  const uncachedInputTokens = usage.cached_input_tokens != null ? Math.max(0, inputTokens - cachedTokens) : inputTokens;
  const cacheRatio = effectiveInputTokens > 0 ? `${(cachedTokens / effectiveInputTokens * 100).toFixed(1)}% cached` : "";
  const rows = [
    ["input_tokens", inputTokens],
    [usage.cached_input_tokens != null ? "cached_input_tokens" : "cache_read_input_tokens", cachedTokens],
    ["cache_creation_input_tokens", cacheCreationTokens],
    ["uncached_input_tokens", uncachedInputTokens],
    ["effective_input_tokens", effectiveInputTokens],
    ["output_tokens", outputTokens],
    ["reasoning_output_tokens", reasoningTokens],
    ["visible_output_tokens", Math.max(0, outputTokens - reasoningTokens)],
    ["total_reported_tokens", effectiveInputTokens + outputTokens]
  ];
  const flags = [
    `status ${usageStatus.label}`,
    source ? `source ${source}` : "",
    cacheRatio,
    usage.total_cost_usd != null ? `cost $${Number(usage.total_cost_usd || 0).toFixed(6)}` : "",
    usage.duration_ms != null ? `duration ${formatMetricNumber(usage.duration_ms)}ms` : "",
    usage.num_turns != null ? `turns ${formatMetricNumber(usage.num_turns)}` : "",
    usage.subtype ? `subtype ${usage.subtype}` : ""
  ].filter(Boolean);
  return `
    <details class="metrics-breakdown usage-breakdown" data-metrics-breakdown="usage">
      <summary>Backend usage breakdown</summary>
      <div class="session-breakdown-kpis">
        ${flags.map(item => `<code>${escapeHtml(item)}</code>`).join("")}
      </div>
      <div class="session-breakdown-group">
        <strong>Tokens</strong>
        ${rows.map(([name, value]) => `
          <div class="session-breakdown-row">
            <span>${escapeHtml(name)}</span>
            <code>${escapeHtml(formatMetricNumber(value))}</code>
          </div>
        `).join("")}
      </div>
    </details>
  `;
}

function componentMetricRows(components, totalChars) {
  return Object.entries(components || {})
    .map(([name, metric]) => ({
      name,
      chars: Number(metric?.chars || 0),
      bytes: Number(metric?.bytes || 0),
      lines: Number(metric?.lines || 0)
    }))
    .sort((left, right) => right.chars - left.chars)
    .map(item => {
      const percent = totalChars > 0 ? Math.min(100, Math.max(0, item.chars / totalChars * 100)) : 0;
      return { ...item, percent };
    });
}

function promptMetricsState(taskId) {
  const target = backendTarget();
  const metricsEvent = latestPromptMetricsEvent(taskId, target);
  const usageEvent = latestAgentUsageEvent(taskId, target);
  const usageStatus = usageMetricsStatus(taskId, usageEvent, target);
  const ahaInputStatus = ahaInputMetricsStatus(taskId, metricsEvent, target);
  const overflowEvent = latestContextOverflowEvent(taskId, target);
  const data = eventData(metricsEvent || {});
  const total = data.total || {};
  const totalChars = Number(total.chars || 0);
  const rows = componentMetricRows(data.components || {}, totalChars);
  const largest = rows[0];
  const overflow = Boolean(overflowEvent && (!metricsEvent || conversationEventOrder(overflowEvent) >= conversationEventOrder(metricsEvent)));
  const backendSession = conversationBackendSession(taskId);
  const sessionStatus = backendSessionStatus(backendSession, overflow);
  return { ahaInputStatus, backendSession, data, largest, metricsEvent, overflow, overflowEvent, rows, sessionStatus, total, totalChars, usageEvent, usageStatus };
}

function renderPromptMetricsPanel(taskId) {
  const metrics = promptMetricsState(taskId);
  const { ahaInputStatus, backendSession, data, largest, metricsEvent, overflow, overflowEvent, rows, sessionStatus, total, totalChars, usageEvent, usageStatus } = metrics;
  const compactState = compactResetState(taskId);
  const displayedSessionStatus = compactState
    ? { label: compactState.label, className: compactState.className }
    : sessionStatus;
  const hasSessionHistory = Array.isArray(backendSession?.history) && backendSession.history.length > 0;
  const hasSessionInfo = Boolean(backendSession?.id || backendSession?.exists || hasSessionHistory || backendSession?.compact_summary);
  if (!metricsEvent && !overflowEvent && !hasSessionInfo) {
    return `
      <section class="prompt-metrics empty-metrics">
        <div>
          <span>Prompt Input</span>
          <strong>No metrics yet</strong>
        </div>
        <code>send a message after the metrics build is running</code>
      </section>
    `;
  }

  const source = data.source || eventData(overflowEvent || {}).source || "backend";
  const usage = eventData(usageEvent || {}).usage || {};
  const sessionSize = Number(backendSession?.size_bytes);
  const sessionLabel = backendSession?.exists && Number.isFinite(sessionSize)
    ? `${backendSession.backend || "backend"} jsonl · ${formatMetricBytes(sessionSize)}`
    : backendSession?.id
      ? `${backendSession.backend || "backend"} jsonl · missing`
      : "";
  const sessionAnalysis = backendSession?.analysis || {};
  const sessionAhaCounts = sessionAnalysis.aha_prompt_counts || {};
  const sessionAhaChars = sessionAnalysis.aha_prompt_chars || {};
  const sessionFullCount = Number(sessionAhaCounts.full || 0);
  const sessionFullChars = Number(sessionAhaChars.full || 0);
  const sessionDeltaCount = Number(sessionAhaCounts.sticky_delta || 0);
  const sessionDeltaChars = Number(sessionAhaChars.sticky_delta || 0);
  const sessionPromptCount = Number(sessionAnalysis.aha_prompt_total_count || 0);
  const sessionPromptChars = Number(sessionAnalysis.aha_prompt_total_chars || 0);
  const sessionMirrorChars = Number(sessionAnalysis.event_msg_prompt_mirror_total_chars || 0);
  const sessionToolChars = Number(sessionAnalysis.tool_output_chars || 0);
  const sessionLineCount = Number(sessionAnalysis.line_count || 0);
  const sessionPromptMode = sessionAnalysis.latest_prompt_mode || data.prompt_mode || "";
  const sessionHistory = Array.isArray(backendSession?.history) ? backendSession.history : [];
  const compactSummary = backendSession?.compact_summary || null;
  const sessionSummary = backendSession?.exists
    ? sessionPromptCount
      ? `${formatMetricCompact(sessionPromptChars)} AHA prompt`
      : "no AHA prompt"
    : backendSession?.id
      ? "session missing"
      : sessionHistory.length
        ? `${formatMetricNumber(sessionHistory.length)} archived`
        : "no session";
  const sessionParts = backendSession?.exists
    ? [
        Number.isFinite(sessionSize) ? `file ${formatMetricBytes(sessionSize)}` : "",
        sessionLineCount ? `${formatMetricNumber(sessionLineCount)} lines` : "",
        formatMetricCountChars(sessionFullCount, sessionFullChars, "full"),
        formatMetricCountChars(sessionDeltaCount, sessionDeltaChars, "delta"),
        `mirrors ${formatMetricNumber(sessionMirrorChars)} chars`,
        `tools ${formatMetricNumber(sessionToolChars)} chars`,
        sessionPromptMode ? `latest ${sessionPromptMode}` : "",
        sessionAnalysis.parse_errors ? `${formatMetricNumber(sessionAnalysis.parse_errors)} parse errors` : ""
      ].filter(Boolean)
    : [
        backendSession?.id ? "jsonl not found" : "no current session",
        sessionHistory.length ? `${formatMetricNumber(sessionHistory.length)} archived sessions` : "",
        compactSummary?.id ? `summary ${compactSummary.id}` : ""
      ].filter(Boolean);
  const sessionActionButton = backendSession?.id
    ? `<button type="button" data-session-action="compact-reset"${compactState ? " disabled" : ""}>${escapeHtml(compactState?.buttonLabel || "Compact")}</button>`
    : "";
  const ahaParts = [
    `${formatMetricNumber(totalChars)} chars`,
    `${formatMetricBytes(total.bytes)} bytes`,
    `${formatMetricNumber(total.lines)} lines`,
    data.event_limit ? `${formatMetricNumber(data.event_limit)} events` : ""
  ].filter(Boolean);
  const usageParts = [
    usage.input_tokens != null ? `input ${formatMetricNumber(usage.input_tokens)}` : "",
    (usage.cached_input_tokens != null || usage.cache_read_input_tokens != null) ? `cached ${formatMetricNumber(usageCacheReadTokens(usage))}` : "",
    usage.cache_creation_input_tokens != null ? `created ${formatMetricNumber(usageCacheCreationTokens(usage))}` : "",
    usage.output_tokens != null ? `output ${formatMetricNumber(usage.output_tokens)}` : "",
    usage.reasoning_output_tokens != null ? `reasoning ${formatMetricNumber(usage.reasoning_output_tokens)}` : "",
    usage.total_cost_usd != null ? `$${Number(usage.total_cost_usd || 0).toFixed(4)}` : "",
    usage.num_turns != null ? `${formatMetricNumber(usage.num_turns)} turns` : ""
  ].filter(Boolean);
  const backendParts = [
    ...usageParts,
  ].filter(Boolean);
  const backendSummary = usage.input_tokens != null ? `${formatMetricNumber(usage.input_tokens)} input` : usageStatus.label;
  const topLabel = largest ? `${largest.name} · ${formatMetricNumber(largest.chars)} chars` : "no components";
  return `
    <section class="prompt-metrics ${overflow ? "has-overflow" : ""}">
      <div class="prompt-metrics-section aha-metrics-section">
        <div class="prompt-metrics-head">
          <div>
            <span>AHA Input</span>
            <strong>${escapeHtml(ahaParts[0] || "0 chars")}</strong>
            <code>${escapeHtml(topLabel)}</code>
          </div>
          <div class="prompt-metrics-head-actions">
            <span class="status ${ahaInputStatus.className}">${escapeHtml(ahaInputStatus.label)}</span>
          </div>
        </div>
        <div class="prompt-metric-kpis">
          ${ahaParts.map(part => `<code>${escapeHtml(part)}</code>`).join("")}
        </div>
        <div class="prompt-component-bars">
          ${rows.map(row => `
            <div class="prompt-component-row">
              <span>${escapeHtml(row.name)}</span>
              <div class="prompt-component-track" aria-hidden="true">
                <i style="width: ${row.percent.toFixed(2)}%"></i>
              </div>
              <code>${escapeHtml(formatMetricNumber(row.chars))}</code>
            </div>
          `).join("")}
        </div>
        ${renderAhaInputBreakdown(data, rows)}
      </div>
      <div class="prompt-metrics-section backend-metrics-section">
        <div class="prompt-metrics-head">
          <div>
            <span>Backend Usage</span>
            <strong>${escapeHtml(backendSummary)}</strong>
            <code>${escapeHtml(source || "waiting for backend usage")}</code>
          </div>
          <div class="prompt-metrics-head-actions">
            <span class="status ${usageStatus.className}">${escapeHtml(usageStatus.label)}</span>
          </div>
        </div>
        <div class="prompt-metric-kpis">
          ${(backendParts.length ? backendParts : [`usage ${usageStatus.label}`]).map(part => `<code>${escapeHtml(part)}</code>`).join("")}
        </div>
        ${renderUsageBreakdown(usage, usageStatus, source)}
      </div>
      <div class="prompt-metrics-section session-metrics-section">
        <div class="prompt-metrics-head">
          <div>
            <span>Backend Session</span>
            <strong>${escapeHtml(sessionSummary)}</strong>
            <code>${escapeHtml(sessionLabel || "waiting for backend session")}</code>
          </div>
          <div class="prompt-metrics-head-actions">
            <span class="status ${displayedSessionStatus.className}">${escapeHtml(displayedSessionStatus.label)}</span>
            ${sessionActionButton}
          </div>
        </div>
        <div class="prompt-metric-kpis">
          ${sessionParts.map(part => `<code>${escapeHtml(part)}</code>`).join("")}
        </div>
        ${backendSession?.exists ? renderSessionBreakdown(sessionAnalysis) : ""}
      </div>
    </section>
  `;
}

function renderPromptMetricsPopover(taskId) {
  const metrics = promptMetricsState(taskId);
  const hasHistory = Array.isArray(metrics.backendSession?.history) && metrics.backendSession.history.length > 0;
  const hasMetrics = Boolean(metrics.metricsEvent || metrics.overflowEvent || metrics.backendSession?.id || metrics.backendSession?.exists || hasHistory || metrics.backendSession?.compact_summary);
  const summary = metrics.metricsEvent ? formatMetricCompact(metrics.totalChars) : "--";
  const top = metrics.largest?.name || "no components";
  const key = promptMetricsKey(taskId);
  const open = openPromptMetricsKey === key ? " open" : "";
  const classes = ["turn-metrics", metrics.overflow ? "has-overflow" : "", metrics.sessionStatus?.className || "", hasMetrics ? "" : "is-empty"].filter(Boolean).join(" ");
  const sessionLabel = metrics.sessionStatus?.label || "none";
  const label = hasMetrics
    ? `AHA input: ${formatMetricNumber(metrics.totalChars)} chars, top ${top}; session ${sessionLabel}`
    : "Prompt metrics unavailable";
  return `
    <details class="${classes}" data-turn-metrics-key="${escapeHtml(key)}"${open}>
      <summary class="turn-metrics-trigger" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}">
        <span class="turn-metrics-dot" aria-hidden="true"></span>
        <code>${escapeHtml(summary)}</code>
      </summary>
      <div class="turn-metrics-popover">
        ${renderPromptMetricsPanel(taskId)}
      </div>
    </details>
  `;
}

function renderPromptMetricsDock(taskId) {
  const metrics = promptMetricsState(taskId);
  const hasHistory = Array.isArray(metrics.backendSession?.history) && metrics.backendSession.history.length > 0;
  if (!metrics.metricsEvent && !metrics.overflowEvent && !metrics.backendSession?.id && !metrics.backendSession?.exists && !hasHistory && !metrics.backendSession?.compact_summary) return "";
  return `<div class="conversation-metrics-dock">${renderPromptMetricsPopover(taskId)}</div>`;
}

function latestKnownEventOrder() {
  const orders = allEvents.map(event => conversationEventOrder(event)).filter(Number.isFinite);
  return orders.length ? Math.max(...orders) : -1;
}

function latestScopedTaskEvent(taskId, target, type, afterOrder = null) {
  const events = taskEvents(taskId)
    .filter(event => event.type === type && eventMatchesAgent(event, target))
    .filter(event => afterOrder == null || conversationEventOrder(event) > afterOrder)
    .sort((left, right) => conversationEventOrder(left) - conversationEventOrder(right));
  return events.length ? events[events.length - 1] : null;
}

function agentStatusSession(taskId, agentId) {
  const task = (statusData?.tasks || []).find(item => item.id === taskId);
  return (task?.agents || []).find(item => item.id === agentId) || null;
}

function compactResetLooksComplete(taskId, agentId, previousSessionId, afterOrder) {
  const compactEvent = latestScopedTaskEvent(taskId, agentId, "backend_session_compact_reset", afterOrder);
  const startedEvent = latestScopedTaskEvent(taskId, agentId, "backend_started", afterOrder);
  const backendSession = conversationBackendSession(taskId, agentId);
  const history = Array.isArray(backendSession?.history) ? backendSession.history : [];
  const archived = Boolean(previousSessionId && history.some(item => item.backend_session_id === previousSessionId));
  const currentSessionId = String(backendSession?.id || "");
  const hasNewConversationSession = Boolean(currentSessionId && currentSessionId !== previousSessionId);
  const agent = agentStatusSession(taskId, agentId);
  const statusSessionId = String(agent?.backend_session_id || "");
  const hasNewStatusSession = Boolean(statusSessionId && statusSessionId !== previousSessionId && String(agent?.session_status || "").toLowerCase() === "active");
  return Boolean(compactEvent && (startedEvent || archived || hasNewConversationSession || hasNewStatusSession || backendSession?.compact_summary));
}

function sleep(ms) {
  return new Promise(resolve => window.setTimeout(resolve, ms));
}

async function refreshCompactResetStatus(taskId, agentId) {
  await catchUpRealtimeEvents();
  await loadStatus({ forceAgents: true });
  await loadBackendStatus();
  await loadConversationPage(taskId, agentId, false, true);
}

async function verifyCompactResetAfterTimeout(taskId, agentId, previousSessionId, afterOrder) {
  const deadline = Date.now() + COMPACT_RESET_VERIFY_TIMEOUT_MS;
  while (Date.now() <= deadline) {
    await refreshCompactResetStatus(taskId, agentId);
    if (compactResetLooksComplete(taskId, agentId, previousSessionId, afterOrder)) return true;
    await sleep(1000);
  }
  return false;
}

async function compactResetSelectedSession(button) {
  const task = selectedTask();
  if (!task) return;
  const agentId = backendTarget();
  const stateKey = promptMetricsKey(task.id, agentId);
  const previousSessionId = String(conversationBackendSession(task.id, agentId)?.id || agentStatusSession(task.id, agentId)?.backend_session_id || "");
  const actionStartOrder = latestKnownEventOrder();
  const confirmed = window.confirm(
    `Compact and reset backend session for ${task.id}/${agentId}?\n\n` +
    "AHA will archive the current backend session, write a compact summary, and restart a fresh backend session."
  );
  if (!confirmed) return;
  compactResetStates.set(stateKey, { label: "compacting", className: "session-pending", buttonLabel: "Compacting" });
  renderPanel();
  try {
    const res = await fetchWithTimeout(apiUrl(`/api/task/${encodeURIComponent(task.id)}/session/compact-reset`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: agentId, reason: "manual", restart: true })
    }, COMPACT_RESET_TIMEOUT_MS);
    await readJsonResponse(res, "Failed to compact-reset backend session");
    compactResetStates.set(stateKey, { label: "restarting", className: "session-pending", buttonLabel: "Restarting" });
    renderPanel();
    await refreshCompactResetStatus(task.id, agentId);
    compactResetStates.set(stateKey, { label: "done", className: "session-done", buttonLabel: "Done" });
    renderPanel();
    window.setTimeout(() => {
      const state = compactResetStates.get(stateKey);
      if (state?.label === "done") {
        compactResetStates.delete(stateKey);
        renderPanel();
      }
    }, 2200);
  } catch (err) {
    if (isRequestTimeoutError(err)) {
      compactResetStates.set(stateKey, { label: "checking", className: "session-pending", buttonLabel: "Checking" });
      renderPanel();
      let completed = false;
      try {
        completed = await verifyCompactResetAfterTimeout(task.id, agentId, previousSessionId, actionStartOrder);
      } catch (verifyErr) {
        console.warn("Compact-reset verification failed", verifyErr);
      }
      if (completed) {
        compactResetStates.set(stateKey, { label: "done", className: "session-done", buttonLabel: "Done" });
        renderPanel();
        window.setTimeout(() => {
          const state = compactResetStates.get(stateKey);
          if (state?.label === "done") {
            compactResetStates.delete(stateKey);
            renderPanel();
          }
        }, 2200);
        return;
      }
    }
    compactResetStates.set(stateKey, { label: "failed", className: "session-error", buttonLabel: "Retry" });
    renderPanel();
    alert(err.message || String(err));
    window.setTimeout(() => {
      const state = compactResetStates.get(stateKey);
      if (state?.label === "failed") {
        compactResetStates.delete(stateKey);
        renderPanel();
      }
    }, 4000);
  }
}

function parseTimestamp(value) {
  if (!value) return null;
  const millis = Date.parse(value);
  return Number.isNaN(millis) ? null : millis;
}

function formatLocalTimestamp(value, fallback = "-") {
  const millis = parseTimestamp(value);
  if (millis === null) return fallback;
  return new Date(millis).toLocaleString("zh-CN", { hour12: false });
}

function eventTimeLabel(event) {
  const value = event?.ts || eventData(event).ts;
  return formatLocalTimestamp(value, value || "");
}

function localizeTimestampFields(value, key = "") {
  if (Array.isArray(value)) return value.map(item => localizeTimestampFields(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([itemKey, itemValue]) => [itemKey, localizeTimestampFields(itemValue, itemKey)]));
  }
  if ((key === "ts" || key.endsWith("_at")) && typeof value === "string") {
    return formatLocalTimestamp(value, value);
  }
  return value;
}

function localizeTimestampText(value) {
  return String(value ?? "").replace(
    /\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b/g,
    match => formatLocalTimestamp(match, match)
  );
}

function formatDuration(millis) {
  const totalSeconds = Math.max(0, Math.floor((millis || 0) / 1000));
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  const minutes = String(Math.floor(totalSeconds / 60) % 60).padStart(2, "0");
  const hours = Math.floor(totalSeconds / 3600);
  return hours > 0 ? `${hours}:${minutes}:${seconds}` : `${minutes}:${seconds}`;
}

function formatClock(millis) {
  if (!millis) return "-";
  return new Date(millis).toLocaleTimeString("zh-CN", { hour12: false });
}

function eventTimestamp(event) {
  return parseTimestamp(event.ts || eventData(event).ts);
}

function taskTiming(taskId, task) {
  const events = taskEvents(taskId);
  const firstMatchingTime = predicate => {
    for (const event of events) {
      if (predicate(event)) {
        const millis = eventTimestamp(event);
        if (millis) return millis;
      }
    }
    return null;
  };
  const lastMatchingTime = predicate => {
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index];
      if (predicate(event)) {
        const millis = eventTimestamp(event);
        if (millis) return millis;
      }
    }
    return null;
  };
  const startedAt = parseTimestamp(task?.started_at) || firstMatchingTime(event => {
    const data = eventData(event);
    const hasStartedStatus = task?.status && task.status !== "pending";
    return (
      event.type === "task_started" ||
      event.type === "agent_started" ||
      (event.type === "task_dispatched" && hasStartedStatus) ||
      (event.type === "task_status_changed" && data.status === "running")
    );
  });
  const terminalStatus = terminalTaskStatuses.has(taskDisplayStatus(task));
  const finishedAt = parseTimestamp(task?.finished_at) || lastMatchingTime(event => {
    const data = eventData(event);
    return (
      event.type === "task_finished" ||
      event.type === "agent_finished" ||
      (event.type === "task_status_changed" && terminalTaskStatuses.has(data.status || ""))
    );
  });
  if (!startedAt) return null;
  const running = taskCurrentStatus(task) === "running" && taskActivityStatus(task) !== "idle";
  const endAt = running ? Date.now() : finishedAt || (terminalStatus ? lastMatchingTime(() => true) : null);
  if (!endAt) return { startedAt, finishedAt: null, elapsedMs: Date.now() - startedAt, running };
  return { startedAt, finishedAt: running ? null : endAt, elapsedMs: endAt - startedAt, running };
}

function subAgents(task) {
  return (task?.agents || []).filter(agent => agent.role === "sub");
}

function pendingSubAgents(task) {
  return subAgents(task).filter(agent => !terminalAgentStatuses.has(agent.status || ""));
}

function waitingSubagentTiming(task) {
  const agents = subAgents(task);
  if (!agents.length) return null;
  const coordination = task?.coordination || {};
  const startedAt = parseTimestamp(coordination.followup_started_at);
  if (!startedAt) return null;
  const pending = pendingSubAgents(task);
  const finalRequestedAt = parseTimestamp(coordination.final_summary_requested_at);
  const finalCompletedAt = parseTimestamp(coordination.final_summary_completed_at);
  const running = taskCurrentStatus(task) === "running" && pending.length > 0 && !finalRequestedAt;
  const endAt = running ? Date.now() : finalRequestedAt || finalCompletedAt || Date.now();
  return {
    startedAt,
    finishedAt: running ? null : endAt,
    elapsedMs: endAt - startedAt,
    running,
    pending,
    completed: agents.filter(agent => terminalAgentStatuses.has(agent.status || ""))
  };
}

function taskTimingLabel(taskId, task) {
  const timing = taskTiming(taskId, task);
  if (!timing) return "";
  return `${timing.running ? "elapsed" : "duration"} ${formatDuration(timing.elapsedMs)}`;
}

function taskMetaTiming(taskId, task) {
  const parts = [];
  const taskLabel = taskTimingLabel(taskId, task);
  if (taskLabel) parts.push(`task ${taskLabel}`);
  const waiting = waitingSubagentTiming(task);
  if (waiting) parts.push(`waiting subagents ${formatDuration(waiting.elapsedMs)} (${waiting.completed.length}/${subAgents(task).length})`);
  return parts.join(" | ");
}

function latestTurnTiming(taskId) {
  const events = conversationSourceEvents(taskId).filter(eventMatchesSelectedAgent);
  let startIndex = -1;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index].type === "agent_started") {
      startIndex = index;
      break;
    }
  }
  if (startIndex < 0) return null;
  const startedEvent = events[startIndex];
  const startedAt = eventTimestamp(startedEvent);
  if (!startedAt) return null;
  const task = (statusData?.tasks || []).find(item => item.id === taskId);
  const target = backendTarget();
  const agent = (task?.agents || []).find(item => item.id === target);
  const coordination = task?.coordination || {};
  const followupStartedAt = parseTimestamp(coordination.followup_started_at);
  const finalCompletedAt = parseTimestamp(coordination.final_summary_completed_at);
  const followupCoversTurn =
    target === "main" &&
    followupStartedAt &&
    ((!finalCompletedAt && followupStartedAt >= startedAt) || (finalCompletedAt && finalCompletedAt >= startedAt));
  if (followupCoversTurn) {
    const waiting = waitingSubagentTiming(task);
    const agentStatus = agentLifecycleStatus(agent);
    const followupStillActive =
      taskCurrentStatus(task) === "running" ||
      waiting?.running ||
      agentStatus === "waiting";
    if (!finalCompletedAt && !followupStillActive) {
      return null;
    }
    let logicalStartedEvent = startedEvent;
    for (let index = startIndex; index >= 0; index -= 1) {
      if (events[index].type !== "agent_started") continue;
      const candidateStartedAt = eventTimestamp(events[index]);
      if (candidateStartedAt && candidateStartedAt <= followupStartedAt) {
        logicalStartedEvent = events[index];
        break;
      }
    }
    const logicalStartedAt = eventTimestamp(logicalStartedEvent) || followupStartedAt;
    const status = finalCompletedAt
      ? "completed"
      : waiting?.running || agentStatus === "waiting"
        ? "waiting"
        : agentStatus || "running";
    const endAt = finalCompletedAt || Date.now();
    return {
      startedAt: logicalStartedAt,
      finishedAt: finalCompletedAt || null,
      elapsedMs: endAt - logicalStartedAt,
      running: !finalCompletedAt,
      status,
      target,
      sender: eventData(logicalStartedEvent).sender || "-"
    };
  }
  let latestStatusEvent = null;
  let terminalStatusEvent = null;
  let agentFinishedEvent = null;
  for (let index = startIndex + 1; index < events.length; index += 1) {
    const data = eventData(events[index]);
    if (events[index].type === "agent_status_changed") {
      latestStatusEvent = events[index];
      if (terminalAgentStatuses.has(data.status || "")) terminalStatusEvent = events[index];
    } else if (events[index].type === "agent_finished") {
      agentFinishedEvent = events[index];
    }
  }
  const latestStatus = eventData(latestStatusEvent || {}).status || agentLifecycleStatus(agent);
  let finishedAt = terminalStatusEvent ? eventTimestamp(terminalStatusEvent) : null;
  if (!finishedAt && !["running", "waiting"].includes(latestStatus) && agentFinishedEvent) {
    finishedAt = eventTimestamp(agentFinishedEvent);
  }
  if (!finishedAt && terminalAgentStatuses.has(agent?.status || "")) {
    finishedAt = parseTimestamp(agent.finished_at) || parseTimestamp(agent.last_active_at) || parseTimestamp(task?.finished_at) || startedAt;
  }
  const running = !finishedAt || latestStatus === "waiting";
  const endAt = running ? Date.now() : finishedAt;
  const finishedData = eventData(terminalStatusEvent || agentFinishedEvent || {});
  const exitCode = finishedData.exit_code;
  const status = running ? latestStatus || "running" : finishedData.status || agent?.status || (exitCode === 0 ? "completed" : "failed");
  return {
    startedAt,
    finishedAt: running ? null : finishedAt,
    elapsedMs: endAt - startedAt,
    running,
    status,
    target: eventData(startedEvent).target || "main",
    sender: eventData(startedEvent).sender || "-"
  };
}

function applyBackendData(backends = []) {
  backendModels = new Map();
  backendCommands = new Map();
  taskBackendEl.innerHTML = "";
  for (const backend of backends) {
    backendModels.set(backend.name, backend.models || [{ name: "", label: "default" }]);
    backendCommands.set(backend.name, backend.commands || []);
    const opt = document.createElement("option");
    opt.value = backend.name;
    opt.textContent = backend.name;
    taskBackendEl.appendChild(opt);
  }
  if ([...taskBackendEl.options].some(item => item.value === "codex")) taskBackendEl.value = "codex";
  renderModelOptions();
}

async function loadBackends() {
  const payload = await fetchJson("/api/backends", {}, "Failed to load backends");
  applyBackendData(payload.backends || []);
  return payload;
}

function renderModelOptions() {
  const previous = taskModelEl.value;
  const models = backendModels.get(taskBackendEl.value) || [{ name: "", label: "default" }];
  taskModelEl.innerHTML = "";
  for (const model of models) {
    const opt = document.createElement("option");
    opt.value = model.name;
    opt.textContent = model.label || model.name || "default";
    taskModelEl.appendChild(opt);
  }
  if ([...taskModelEl.options].some(item => item.value === previous)) taskModelEl.value = previous;
}

async function loadWorkspaces() {
  const payload = await fetchJson("/api/workspaces", {}, "Failed to load workspaces");
  if (payload.default_workspace_path) {
    bootstrapData = { ...(bootstrapData || {}), default_workspace_path: payload.default_workspace_path };
  }
  applyWorkspaceData(payload.workspaces || []);
}

function renderWorkspaceSelect() {
  if (!workspaceSelectEl || !workspaceCustomEl) return;
  const previous = workspaceSelectEl.value;
  workspaceSelectEl.innerHTML = "";
  for (const workspace of workspaceData) {
    const opt = document.createElement("option");
    opt.value = workspace.path;
    opt.dataset.workspaceId = workspace.id || "";
    opt.textContent = workspace.label || workspace.name;
    workspaceSelectEl.appendChild(opt);
  }
  const custom = document.createElement("option");
  custom.value = "__custom__";
  custom.textContent = "Custom path...";
  workspaceSelectEl.appendChild(custom);

  const preferred =
    workspaceData.find(item => item.path === previous) ||
    workspaceData.find(item => item.name === "fw_omni_builder") ||
    workspaceData[0];
  if (preferred) {
    workspaceSelectEl.value = preferred.path;
  } else {
    workspaceSelectEl.value = "__custom__";
    if (!workspaceCustomEl.value && bootstrapData?.default_workspace_path) {
      workspaceCustomEl.value = bootstrapData.default_workspace_path;
    }
  }
  workspaceCustomEl.classList.toggle("hidden", workspaceSelectEl.value !== "__custom__");
}

function applyStatusData(options = {}) {
  document.body.classList.remove("empty-run");
  if (statusData.run_id && statusData.run_id !== currentRunId) {
    closeEventWebSocket();
    currentRunId = String(statusData.run_id);
    syncRunUrl();
    restoreEventCursorFromStorage();
  }
  renderSessionSummary();
  summaryEl.textContent = statusData.goal;
  const tasks = visibleTasks();
  if (!selectedTaskId) selectedTaskId = readStoredSelectedTaskId() || null;
  if (!selectedTaskId || !tasks.some(task => task.id === selectedTaskId)) selectedTaskId = defaultTaskId(tasks);
  writeStoredSelectedTaskId(selectedTaskId);
  renderTaskList();
  renderSelectedHeader();
  if (options.forceTaskProxy || !isTaskProxyEditing()) {
    renderTaskProxyEditor();
  }
  if (options.forceTaskSupervision || !isTaskSupervisionEditing()) {
    renderTaskSupervisionEditor();
  }
  if (options.forceAgents || !isAgentsPanelEditing()) {
    renderAgents();
  } else {
    renderSelectedAgentInfo();
  }
  renderPendingMessages();
}

async function loadStatus(options = {}) {
  if (!currentRunId) {
    statusData = null;
    renderFirstRunState();
    return null;
  }
  const params = { lite: "1" };
  const requestedSelectedTaskId = selectedTaskId;
  if (selectedTaskId) params.selected_task_id = selectedTaskId;
  statusData = await fetchJson(apiUrl("/api/status", params), {}, "Failed to load status");
  applyStatusData(options);
  if (options.ensureSelectedAgents !== false && !requestedSelectedTaskId && selectedTaskNeedsAgentDetails()) {
    loadStatus({ ...options, forceAgents: true, ensureSelectedAgents: false })
      .then(() => loadBackendStatus())
      .then(() => renderPanelForRealtime())
      .catch(err => console.warn("Failed to load selected task agent details", err));
  }
  return statusData;
}

async function loadBackendStatus() {
  if (!currentRunId) {
    backendStatusData = null;
    renderBackendStatus();
    return null;
  }
  const params = new URLSearchParams({ target: backendTarget() });
  if (selectedTaskId) params.set("task_id", selectedTaskId);
  backendStatusData = await fetchJson(apiUrl("/api/backend", params), {}, "Failed to load backend status");
  renderBackendStatus();
  return backendStatusData;
}

async function loadFinalDetail(taskId, force = false) {
  if (!taskId) return null;
  if (!force && finalDetails.has(taskId)) return finalDetails.get(taskId);
  const detail = await fetchJson(apiUrl(`/api/task/${encodeURIComponent(taskId)}/final`), {}, "Failed to load final");
  finalDetails.set(taskId, detail);
  return detail;
}

async function loadContextDetail(taskId, force = false) {
  if (!taskId) return null;
  if (!force && contextDetails.has(taskId)) return contextDetails.get(taskId);
  const detail = await fetchJson(apiUrl(`/api/task/${encodeURIComponent(taskId)}/context`), {}, "Failed to load context");
  contextDetails.set(taskId, detail);
  return detail;
}

function logState(taskId) {
  if (!logStates.has(taskId)) {
    logStates.set(taskId, { text: "", beforeOffset: null, hasMore: true, initialized: false, loading: false, source: "auto", autoFollow: true });
  }
  return logStates.get(taskId);
}

async function loadLogPage(taskId, older = false, force = false) {
  if (!taskId) return null;
  const state = logState(taskId);
  if (state.loading || (!force && !older && state.initialized) || (older && !state.hasMore)) return state;
  state.loading = true;
  try {
    const params = new URLSearchParams({ limit: String(logPageLimit) });
    if (older && state.source) params.set("source", state.source);
    if (older && state.beforeOffset !== null && state.beforeOffset !== undefined) params.set("before_offset", String(state.beforeOffset));
    const payload = await fetchJson(apiUrl(`/api/task/${encodeURIComponent(taskId)}/logs`, params), {}, "Failed to load logs");
    const text = payload.text || "";
    state.text = older ? [text, state.text].filter(Boolean).join("\n") : text;
    state.beforeOffset = payload.next_before_offset ?? payload.before ?? null;
    state.hasMore = Boolean(payload.has_more);
    state.source = payload.source || state.source || "auto";
    state.initialized = true;
    return state;
  } finally {
    state.loading = false;
  }
}

async function ensureActiveTabData() {
  if (!selectedTaskId) return;
  if (activeTab === "conversation") {
    await ensureConversationLoaded();
  } else if (activeTab === "logs") {
    await loadLogPage(selectedTaskId);
  } else if (activeTab === "final") {
    await loadFinalDetail(selectedTaskId, true);
  } else if (activeTab === "context") {
    await loadContextDetail(selectedTaskId);
  }
}

async function loadOlderLogs() {
  if (activeTab !== "logs" || !selectedTaskId) return;
  const state = logState(selectedTaskId);
  if (!state.initialized || !state.hasMore || state.loading) return;
  const previousHeight = panelEl.scrollHeight;
  const previousTop = panelEl.scrollTop;
  await loadLogPage(selectedTaskId, true);
  renderPanel({ preserveScroll: true, previousHeight, previousTop });
}

function assignConversationKeys(events, start = 0) {
  events.forEach((event, index) => {
    const cursor = event._cursor ?? event.cursor ?? start + index;
    if (!event._uiKey) event._uiKey = `conversation-${cursor}-${event.type || "event"}`;
  });
  return events;
}

async function loadConversationPage(taskId = selectedTaskId, target = backendTarget(), older = false, force = false) {
  if (!taskId) return null;
  const state = conversationState(taskId, target);
  const categoryKey = activeConversationCategoryKey();
  if (!older && state.categoryKey !== categoryKey) {
    state.events = [];
    state.beforeOffset = null;
    state.hasMore = true;
    state.initialized = false;
    state.error = "";
    state.categoryKey = categoryKey;
  }
  if (state.loading || (!force && !older && state.initialized) || (older && !state.hasMore)) return state;
  state.loading = true;
  try {
    const params = new URLSearchParams({
      task_id: taskId,
      target,
      limit: String(conversationPageLimit),
      categories: categoryKey
    });
    if (older && state.beforeOffset !== null && state.beforeOffset !== undefined) params.set("before_offset", String(state.beforeOffset));
    let res;
    try {
      res = await fetchWithTimeout(apiUrl("/api/conversation-events", params));
    } catch (err) {
      await markConversationUnavailable(state, err);
      return state;
    }
    if (!res.ok) {
      const error = await responseError(res, "Failed to load conversation");
      await markConversationUnavailable(state, error);
      return state;
    }
    const payload = await readJsonResponse(res, "Failed to load conversation");
    const events = assignConversationKeys([...(payload.events || []), ...(payload.turn_events || [])], payload.before_offset || 0);
    state.events = older ? mergeConversationEvents(state.events, events, true) : mergeConversationEvents(events, state.events, false);
    if (payload.backend_session) state.backendSession = payload.backend_session;
    state.beforeOffset = payload.next_before_offset ?? payload.before ?? null;
    state.hasMore = Boolean(payload.has_more);
    state.initialized = true;
    state.error = "";
    if (!older && offset < 0 && Number.isFinite(payload.after_offset)) offset = payload.after_offset;
    return state;
  } finally {
    state.loading = false;
  }
}

async function responseError(res, fallbackMessage = "Request failed") {
  try {
    await readJsonResponse(res, fallbackMessage);
  } catch (err) {
    return err;
  }
  return new Error(fallbackMessage);
}

async function initializeEventTailOffset() {
  if (eventTailInitialized) return;
  realtimeDebug("events.tail.request");
  const payload = await fetchJson(apiUrl("/api/events", { offset: "-1" }), {}, "Failed to initialize event stream");
  rememberEventCursor(payload);
  eventTailInitialized = true;
  realtimeDebug("events.tail.response", {
    last_event_id: payload.last_event_id || "",
    offset: payload.offset,
    snapshot_event_id: payload.snapshot_event_id || payload.snapshot_offset || "",
    event_count: (payload.events || []).length
  });
}

async function prepareRealtimeCatchupBaseline() {
  if (lastEventId || eventTailInitialized) return;
  try {
    await initializeEventTailOffset();
  } catch (err) {
    realtimeDebug("events.tail.error", { error: err?.message || String(err) });
    // Sending should still proceed; the post-send catch-up remains best effort.
  }
}

async function markConversationUnavailable(state, err) {
  state.events = [];
  state.beforeOffset = null;
  state.hasMore = false;
  state.initialized = true;
  state.error = err?.message || String(err || "Conversation unavailable");
  try {
    await initializeEventTailOffset();
  } catch (tailErr) {
    state.error = `${state.error}; ${tailErr?.message || tailErr}`;
  }
}

async function ensureConversationLoaded() {
  if (activeTab !== "conversation" || !selectedTaskId) return;
  await loadConversationPage(selectedTaskId, backendTarget(), false);
}

async function loadOlderConversation() {
  if (activeTab !== "conversation" || !selectedTaskId) return;
  const state = conversationState(selectedTaskId, backendTarget());
  if (!state.initialized || !state.hasMore || state.loading) return;
  const previousHeight = panelEl.scrollHeight;
  const previousTop = panelEl.scrollTop;
  await loadConversationPage(selectedTaskId, backendTarget(), true);
  renderPanel({ preserveScroll: true, previousHeight, previousTop });
}

function appendRealtimeConversationEvents(events) {
  if (!events.length) return;
  for (const [key, state] of conversationStates.entries()) {
    if (!state.initialized) continue;
    const { taskId, target } = parseConversationKey(key);
    const matching = events.filter(event => (
      isTaskEvent(event, taskId) &&
      isTimelineEvent(event) &&
      eventMatchesAgent(event, target) &&
      (conversationFilters[conversationEventCategory(event)] || turnEventTypes.has(event.type))
    ));
    if (matching.length) state.events = mergeConversationEvents(state.events, matching, false);
  }
}

const finalDetailInvalidatingEvents = new Set([
  "task_result_written",
  "task_journal_rendered",
  "task_round_recorded",
  "task_reopened",
  "task_status_changed"
]);

function invalidateRealtimeTaskDetails(events) {
  const finalTaskIds = new Set();
  events.forEach(event => {
    if (!finalDetailInvalidatingEvents.has(event.type)) return;
    const taskId = eventTaskId(event);
    if (taskId) finalTaskIds.add(taskId);
  });
  finalTaskIds.forEach(taskId => finalDetails.delete(taskId));
  if (activeTab === "final" && selectedTaskId && finalTaskIds.has(selectedTaskId)) {
    loadFinalDetail(selectedTaskId, true)
      .then(() => renderPanel())
      .catch(err => {
        panelEl.innerHTML = `<pre>${escapeHtml(String(err))}</pre>`;
      });
  }
}

function realtimeEventCursor(event, index = 0, startOffset = "") {
  return String(event?.event_id || event?._cursor || (startOffset !== "" ? `${startOffset}-${index}` : eventIdentity(event))).trim();
}

function appendRealtimeEvents(events, startOffset = "") {
  const accepted = [];
  events.forEach((event, index) => {
    const cursor = realtimeEventCursor(event, index, startOffset);
    const dedupeKey = event?.event_id ? `event_id:${event.event_id}` : `event:${eventIdentity(event)}`;
    if (seenRealtimeEvents.has(dedupeKey)) return;
    seenRealtimeEvents.add(dedupeKey);
    if (!event._uiKey) event._uiKey = `event-${cursor || index}-${event.type || "event"}`;
    rememberEventCursorFromEvent(event);
    accepted.push(event);
  });
  if (!accepted.length) return accepted;
  allEvents.push(...accepted);
  appendRealtimeConversationEvents(accepted);
  invalidateRealtimeTaskDetails(accepted);
  realtimeDebug("events.accepted", {
    count: accepted.length,
    start_offset: startOffset,
    last_event_id: lastEventId,
    types: accepted.slice(0, 8).map(event => event.type || "")
  });
  return accepted;
}

async function pollEvents() {
  let res;
  const params = lastEventId ? { last_event_id: lastEventId } : { offset: String(offset) };
  realtimeDebug("poll.request", { params });
  try {
    res = await fetchWithTimeout(apiUrl("/api/events", params));
  } catch (err) {
    realtimeDebug("poll.fetch_error", { params, error: err?.message || String(err) });
    if (!lastEventId && offset < 0) {
      await initializeEventTailOffset();
      return [];
    }
    throw err;
  }
  if (!res.ok) {
    realtimeDebug("poll.http_error", { params, status: res.status, status_text: res.statusText });
    if (lastEventId) {
      lastEventId = "";
      offset = -1;
      eventTailInitialized = false;
      clearStoredLastEventId();
      await initializeEventTailOffset();
      return [];
    }
    if (offset < 0) await initializeEventTailOffset();
    return [];
  }
  const payload = await readJsonResponse(res, "Failed to poll events");
  const startOffset = offset;
  rememberEventCursor(payload);
  const accepted = appendRealtimeEvents(payload.events || [], startOffset);
  realtimeDebug("poll.response", {
    params,
    event_count: (payload.events || []).length,
    accepted_count: accepted.length,
    response_last_event_id: payload.last_event_id || "",
    response_offset: payload.offset,
    snapshot_event_id: payload.snapshot_event_id || payload.snapshot_offset || "",
    has_more: Boolean(payload.has_more)
  });
  return accepted;
}

function webSocketSupported() {
  return !wsDisabled && currentRunId && typeof WebSocket !== "undefined";
}

function closeEventWebSocket() {
  const socket = eventSocket;
  eventSocket = null;
  eventSocketState = "closed";
  realtimeDebug("ws.close_local");
  if (socket && socket.readyState !== WebSocket.CLOSED) {
    socket.onclose = null;
    socket.close();
  }
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
  if (currentRunId) url.searchParams.set("run_id", currentRunId);
  if (lastEventId) url.searchParams.set("last_event_id", lastEventId);
  url.searchParams.set("lite", "1");
  if (selectedTaskId) url.searchParams.set("selected_task_id", selectedTaskId);
  return url.toString();
}

function scheduleEventWebSocketReconnect() {
  eventSocketFailureCount += 1;
  const multiplier = 2 ** Math.min(eventSocketFailureCount - 1, 5);
  eventSocketReconnectAt = Date.now() + Math.min(30000, pollInterval * multiplier);
  realtimeDebug("ws.reconnect_scheduled", {
    failure_count: eventSocketFailureCount,
    delay_ms: Math.max(0, eventSocketReconnectAt - Date.now())
  });
}

function realtimeStaleFallbackDue() {
  if (wsDisabled || typeof WebSocket === "undefined") return false;
  if (!eventSocket || eventSocket.readyState !== WebSocket.OPEN) return false;
  if (!lastRealtimeMessageAt) return false;
  const now = Date.now();
  const staleAfterMs = Math.max(3000, Math.min(eventSocketStaleMs / 2, pollInterval * 5));
  const minFallbackGapMs = Math.max(1000, pollInterval);
  return now - lastRealtimeMessageAt >= staleAfterMs && now - lastRealtimeFallbackPollAt >= minFallbackGapMs;
}

function closeStaleEventWebSocket(reason = "stale") {
  if (!webSocketSupported() || !eventSocket || typeof WebSocket === "undefined") return false;
  if (eventSocket.readyState !== WebSocket.OPEN || !lastRealtimeMessageAt) return false;
  const ageMs = Date.now() - lastRealtimeMessageAt;
  if (ageMs < eventSocketStaleMs) return false;
  const socket = eventSocket;
  eventSocket = null;
  eventSocketState = "stale";
  eventSocketReconnectAt = 0;
  lastRealtimeFallbackPollAt = 0;
  realtimeDebug("ws.stale_close", { reason, age_ms: ageMs, stale_after_ms: eventSocketStaleMs });
  socket.onclose = null;
  try {
    socket.close(4000, "stale");
  } catch (err) {
    realtimeDebug("ws.stale_close_error", { reason, error: err?.message || String(err) });
  }
  refreshRealtimeIndicator();
  return true;
}

function handleEventWebSocketMessage(message) {
  lastRealtimeMessageAt = Date.now();
  let payload;
  try {
    payload = JSON.parse(message.data);
  } catch (_err) {
    realtimeDebug("ws.message.invalid_json", { raw_len: String(message.data || "").length });
    return;
  }
  realtimeDebug("ws.message", {
    type: payload.type || "",
    event_type: payload.data?.type || "",
    event_id: payload.data?.event_id || ""
  });
  if (payload.type === "status") {
    statusData = payload.data || {};
    applyStatusData();
    renderPanelForRealtime();
    return;
  }
  if (payload.type === "heartbeat") {
    refreshRealtimeIndicator();
    return;
  }
  if (payload.type === "event" && payload.data) {
    const accepted = appendRealtimeEvents([payload.data]);
    if (accepted.length) renderPanelForRealtime();
  }
}

function openEventWebSocket() {
  if (!webSocketSupported()) return false;
  if (eventSocket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(eventSocket.readyState)) return true;
  try {
    const url = eventWebSocketUrl();
    realtimeDebug("ws.open_request", { url });
    const socket = new WebSocket(url);
    eventSocket = socket;
    eventSocketState = "connecting";
    refreshRealtimeIndicator();
    socket.onopen = () => {
      if (eventSocket !== socket) return;
      eventSocketState = "open";
      eventSocketFailureCount = 0;
      eventSocketReconnectAt = 0;
      lastRealtimeMessageAt = Date.now();
      lastRealtimeFallbackPollAt = 0;
      realtimeDebug("ws.open");
      refreshRealtimeIndicator();
    };
    socket.onmessage = message => {
      if (eventSocket === socket) handleEventWebSocketMessage(message);
    };
    socket.onerror = () => {
      if (eventSocket === socket) {
        eventSocketState = "error";
        realtimeDebug("ws.error");
        refreshRealtimeIndicator();
        requestRealtimeCatchup();
      }
    };
    socket.onclose = event => {
      if (eventSocket !== socket) return;
      eventSocket = null;
      eventSocketState = "closed";
      realtimeDebug("ws.close", { code: event.code, reason: event.reason || "", was_clean: event.wasClean });
      scheduleEventWebSocketReconnect();
      refreshRealtimeIndicator();
      requestRealtimeCatchup();
    };
    return true;
  } catch (err) {
    eventSocketState = "error";
    realtimeDebug("ws.open_error", { error: err?.message || String(err) });
    scheduleEventWebSocketReconnect();
    refreshRealtimeIndicator();
    return false;
  }
}

async function ensureEventWebSocket() {
  if (!webSocketSupported()) return false;
  if (closeStaleEventWebSocket("ensure")) return false;
  if (eventSocket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(eventSocket.readyState)) return true;
  if (Date.now() < eventSocketReconnectAt) {
    realtimeDebug("ws.reconnect_wait", { remaining_ms: eventSocketReconnectAt - Date.now() });
    return false;
  }
  if (!lastEventId && !eventTailInitialized) {
    try {
      await initializeEventTailOffset();
    } catch (err) {
      realtimeDebug("ws.tail_before_open_error", { error: err?.message || String(err) });
      scheduleEventWebSocketReconnect();
      return false;
    }
  }
  return openEventWebSocket();
}

async function syncRealtimeEvents(options = {}) {
  const staleSocketClosed = closeStaleEventWebSocket("sync");
  const forcePoll = Boolean(options.forcePoll || staleSocketClosed);
  const staleFallback = !forcePoll && options.allowStalePoll && realtimeStaleFallbackDue();
  if (!forcePoll && !staleFallback && await ensureEventWebSocket()) {
    realtimeDebug("sync.skip_poll_ws_active", { force_poll: false, allow_stale_poll: Boolean(options.allowStalePoll) });
    return [];
  }
  realtimeDebug("sync.poll", {
    force_poll: forcePoll,
    allow_stale_poll: Boolean(options.allowStalePoll),
    stale_fallback: Boolean(staleFallback),
    stale_socket_closed: Boolean(staleSocketClosed)
  });
  const accepted = await pollEvents();
  if (forcePoll || staleFallback) lastRealtimeFallbackPollAt = Date.now();
  if (!wsDisabled && typeof WebSocket !== "undefined" && eventSocketState === "idle") eventSocketState = "polling";
  if (staleSocketClosed) {
    try {
      await ensureEventWebSocket();
    } catch (err) {
      realtimeDebug("ws.reopen_after_stale_error", { error: err?.message || String(err) });
    }
  }
  refreshRealtimeIndicator();
  return accepted;
}

async function catchUpRealtimeEvents() {
  realtimeCatchupRequested = true;
  realtimeDebug("catchup.request");
  if (!realtimeCatchupPromise) {
    realtimeCatchupPromise = (async () => {
      const accepted = [];
      try {
        while (realtimeCatchupRequested) {
          realtimeCatchupRequested = false;
          accepted.push(...await syncRealtimeEvents({ forcePoll: true }));
          realtimeDebug("catchup.batch", { accepted_count: accepted.length });
        }
      } catch (err) {
        console.warn("Realtime catch-up failed", err);
        realtimeDebug("catchup.error", { error: err?.message || String(err) });
      }
      return accepted;
    })().finally(() => {
      realtimeDebug("catchup.done");
      realtimeCatchupPromise = null;
    });
  }
  return realtimeCatchupPromise;
}

function requestRealtimeCatchup() {
  if (!currentRunId) return;
  realtimeDebug("catchup.schedule");
  catchUpRealtimeEvents().then(accepted => {
    if (accepted.length) renderPanelForRealtime();
  });
}

function renderTaskList() {
  tasksEl.innerHTML = "";
  const tasks = visibleTasks();
  if (!tasks.length) {
    tasksEl.innerHTML = '<div class="empty compact">No visible tasks.</div>';
    return;
  }
  for (const task of tasks) {
    const locked = terminalTaskStatuses.has(taskCurrentStatus(task));
    const completionAction = locked ? "reopen" : "final";
    const completionLabel = locked ? "Reopen" : "Final";
    const item = document.createElement("div");
    item.className = `task ${task.id === selectedTaskId ? "active" : ""} ${task.hidden ? "hidden-task" : ""}`;
    item.dataset.taskId = task.id;
    item.innerHTML = `
      <div class="task-row">
        <strong>${escapeHtml(task.id)}</strong>
        <span class="task-statuses">${taskStatusBadges(task)}${taskProxyBadge(task)}${taskSupervisionBadge(task)}</span>
      </div>
      <div class="task-title">${escapeHtml(task.title)}</div>
      <div class="meta truncate">${escapeHtml(taskAgentCount(task))} agent(s) | collaboration ${escapeHtml(taskCollaborationSummary(task))} | default ${escapeHtml(task.preferred_backend || "-")} | proxy ${escapeHtml(taskProxySummary(task))} | supervision ${escapeHtml(taskSupervisionSummary(task))} | ${escapeHtml(pathName(task.workspace_path))}${taskTimingLabel(task.id, task) ? ` | ${escapeHtml(taskTimingLabel(task.id, task))}` : ""}</div>
      <div class="task-actions">
        <button class="task-action" type="button" data-action="${completionAction}">${completionLabel}</button>
        <button class="task-action" type="button" data-action="${task.hidden ? "restore" : "hide"}">${task.hidden ? "Restore" : "Hide"}</button>
        <button class="task-action danger" type="button" data-action="delete">Delete</button>
      </div>
    `;
    item.title = `${task.title}${task.description ? `\n\n${task.description}` : ""}\ncollaboration=${taskCollaborationSummary(task)}\ndefault backend=${task.preferred_backend || "-"}\nproxy=${taskProxySummary(task)}\nsupervision=${taskSupervisionSummary(task)}\nworkspace=${task.workspace_path || "-"}`;
    item.addEventListener("click", async event => {
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest("button")) return;
      await selectTask(task.id);
    });
    tasksEl.appendChild(item);
  }
}

async function selectTask(taskId) {
  const changedTask = selectedTaskId !== taskId;
  selectedTaskId = taskId;
  writeStoredSelectedTaskId(taskId);
  if (changedTask) {
    eventSocketReconnectAt = 0;
    closeEventWebSocket();
  }
  taskProxyEditingUntil = 0;
  taskSupervisionEditingUntil = 0;
  conversationAutoFollow = true;
  if (activeTab === "logs") logState(taskId).autoFollow = true;
  closeMobileSheets();
  closeMobileActionPanel();
  renderTaskList();
  renderSelectedHeader();
  renderTaskProxyEditor();
  renderTaskSupervisionEditor();
  renderAgents();
  await Promise.all([loadBackendStatus(), ensureActiveTabData()]);
  renderPendingMessages();
  renderPanel();
}

async function updateTaskVisibility(taskId, action) {
  if (action === "delete" && !confirm(`Delete ${taskId} from the task list?`)) return;
  if ((action === "final" || action === "complete") && !confirm(`Ask task-main to generate the Final for ${taskId}?`)) return;
  taskActionInFlight = true;
  try {
    const res = await fetchWithTimeout(apiUrl(`/api/task/${encodeURIComponent(taskId)}/${action}`), { method: "POST" });
    if (!res.ok) {
      const payload = await res.json().catch(() => ({}));
      alert(payload.error || `Task action failed: ${action}`);
      return;
    }
    if (action === "restore" || action === "final" || action === "complete" || action === "reopen") selectedTaskId = taskId;
    if (action === "hide" || action === "delete") selectedTaskId = null;
    writeStoredSelectedTaskId(selectedTaskId);
    await loadStatus();
    renderPanel();
  } finally {
    taskActionInFlight = false;
  }
}

async function saveTaskProxyConfig() {
  const task = selectedTask();
  if (!task) return;
  const httpProxy = selectedTaskHttpProxyEl?.value.trim() || "";
  const httpsProxy = selectedTaskHttpsProxyEl?.value.trim() || "";
  let noProxy = selectedTaskNoProxyEl?.value.trim() || "";
  if ((httpProxy || httpsProxy) && !noProxy) {
    noProxy = defaultNoProxy;
    if (selectedTaskNoProxyEl) selectedTaskNoProxyEl.value = noProxy;
  }
  const proxyEnabled = Boolean(selectedTaskProxyEnabledEl?.checked);
  await fetchJson(apiUrl(`/api/task/${encodeURIComponent(task.id)}/proxy`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(runScopedPayload({
      proxy_enabled: proxyEnabled,
      http_proxy: httpProxy,
      https_proxy: httpsProxy,
      no_proxy: noProxy
    }))
  }, "Failed to update task proxy");
  taskProxyEditingUntil = 0;
  await loadStatus({ forceAgents: true, forceTaskProxy: true });
}

async function saveTaskSupervisionConfig() {
  const task = selectedTask();
  if (!task) return;
  const selectedMode = selectedTaskSupervisionModeEl?.value || "manual";
  const supervision = taskSupervisionPayloadFromMode(
    selectedMode,
    selectedTaskSupervisionMaxRoundsEl?.value || "5",
    readAskUserGateControls(selectedTaskSupervisionAskUserGatesEl)
  );
  await fetchJson(apiUrl(`/api/task/${encodeURIComponent(task.id)}/supervision`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(runScopedPayload(supervision))
  }, "Failed to update task supervision");
  taskSupervisionEditingUntil = 0;
  await loadStatus({ forceTaskSupervision: true });
}

function renderSelectedHeader() {
  const task = selectedTask();
  if (!task) {
    renderHeaderWorkspace(null);
    renderMobileTaskSummary(null);
    renderTaskProxyEditor();
    selectedIdEl.textContent = "";
    selectedTitleEl.textContent = "No tasks";
    selectedTaskMetaEl.textContent = "";
    selectedTaskMetaEl.hidden = true;
    selectedStatusEl.textContent = "empty";
    selectedStatusEl.className = "status pending";
    return;
  }
  renderHeaderWorkspace(task);
  renderMobileTaskSummary(task);
  selectedIdEl.textContent = task.id;
  selectedTitleEl.textContent = task.title;
  selectedTaskMetaEl.textContent = `collaboration ${taskCollaborationSummary(task)} | supervision ${taskSupervisionSummary(task)}`;
  selectedTaskMetaEl.hidden = false;
  const displayStatus = task.hidden ? "hidden" : taskDisplayStatus(task);
  selectedStatusEl.textContent = displayStatus;
  selectedStatusEl.className = `status ${displayStatus}`;
}

function renderHeaderWorkspace(task) {
  if (!headerWorkspaceDirEl) return;
  const workspace = task?.workspace_path || "";
  headerWorkspaceDirEl.textContent = workspace ? pathName(workspace) : "";
  headerWorkspaceDirEl.title = workspace;
}

function renderMobileTaskSummary(task) {
  if (!mobileTaskSummaryEl || !mobileTaskTitleEl || !mobileTaskStatusEl) return;
  if (!task) {
    mobileTaskTitleEl.textContent = "No task";
    mobileTaskStatusEl.textContent = "empty";
    mobileTaskStatusEl.className = "status pending";
    mobileTaskSummaryEl.title = "No task selected";
    return;
  }
  const displayStatus = task.hidden ? "hidden" : taskDisplayStatus(task);
  mobileTaskTitleEl.textContent = task.id;
  mobileTaskStatusEl.textContent = displayStatus;
  mobileTaskStatusEl.className = `status ${displayStatus}`;
  mobileTaskSummaryEl.title = `${task.id} / ${task.title}`;
}

function renderAgents() {
  const task = selectedTask();
  agentsEl.innerHTML = "";
  const previous = agentTargetEl.value;
  agentTargetEl.innerHTML = "";
  if (!task) {
    selectedAgentInfoEl.textContent = "";
    selectedAgentInfoEl.hidden = true;
    return;
  }

  const allAgents = task.agents || [];
  const executionAgents = allAgents.filter(agent => !isSupervisionAgent(agent));
  const supervisionAgents = allAgents.filter(agent => isSupervisionAgent(agent));
  const optionGroups = [
    ["Main & sub agents", executionAgents],
    ["Supervision", supervisionAgents]
  ];
  for (const [label, agents] of optionGroups) {
    if (!agents.length) continue;
    const optGroup = document.createElement("optgroup");
    optGroup.label = label;
    for (const agent of agents) {
      const opt = document.createElement("option");
      opt.value = agent.id;
      opt.textContent = isSupervisionAgent(agent) ? `${agent.id} (host/${agent.backend})` : `${agent.id} (${agent.backend})`;
      optGroup.appendChild(opt);
    }
    agentTargetEl.appendChild(optGroup);
  }

  const renderAgentCard = agent => {
    const isHostAgent = isSupervisionAgent(agent);
    const roleLabel = isHostAgent ? "host / user proxy" : agent.role;
    const sandbox = agent.sandbox || task.preferred_sandbox || "workspace-write";
    const approval = agent.approval || task.preferred_approval || "never";
    const proxyEnabled = Boolean(agent.proxy_enabled);
    const processStatus = agentBackendProcessStatus(agent);
    const rawProcessStatus = agent.backend_process_status || processStatus;
    const lifecycleStatus = agentLifecycleStatus(agent);
    const lifecycleTiming = agentStatusTiming(agent);
    const lifecycleTimingText = agentStatusTimingText(agent);
    const lastReply = formatLocalTimestamp(agent.backend_process_last_reply_at, agent.backend_process_last_reply_at || "");
    const processDetail = [
      `process=${rawProcessStatus}`,
      agent.backend_process_pid ? `pid=${agent.backend_process_pid}` : "pid=-",
      agent.backend_process_last_reply_at ? `last_reply=${lastReply}` : ""
    ].filter(Boolean).join(" | ");
    const card = document.createElement("div");
    card.className = `agent-card ${isHostAgent ? "host-agent" : ""} ${agent.id === previous ? "active" : ""}`;
    card.dataset.agentId = agent.id;
    card.title = [
      `${agent.id} ${roleLabel}`,
      `backend=${agent.backend}`,
      `model=${agent.model || "default"}`,
      `sandbox=${sandbox}`,
      `approval=${approval}`,
      `proxy=${proxyEnabled ? "on" : "off"} (${taskProxySummary(task)})`,
      lifecycleTimingText ? `status=${lifecycleTimingText}` : `status=${lifecycleStatus}`,
      lifecycleTiming?.startedAt ? `status_started=${formatClock(lifecycleTiming.startedAt)}` : "",
      lifecycleTiming?.finishedAt ? `status_finished=${formatClock(lifecycleTiming.finishedAt)}` : "",
      processDetail,
      `session=${agent.backend_session_id || "-"}`,
      `workspace=${agent.workspace_path || task.workspace_path || "-"}`
    ].join("\n");
    card.innerHTML = `
      <div class="agent-card-head">
        <strong>${escapeHtml(agent.id)}</strong>
        <span class="agent-process ${escapeHtml(processStatus)}" title="backend process status">${escapeHtml(agentBackendProcessLabel(agent))}</span>
      </div>
      <div class="meta truncate">status=${escapeHtml(lifecycleTimingText || lifecycleStatus)} | ${escapeHtml(roleLabel)} | ${escapeHtml(agent.backend)} | ${escapeHtml(agent.model || "default")}</div>
      <div class="meta truncate">sandbox=${escapeHtml(sandbox)} | approval=${escapeHtml(approval)}</div>
      <div class="meta truncate">proxy=${escapeHtml(proxyEnabled ? "on" : "off")} | task proxy=${escapeHtml(taskProxySummary(task))}</div>
      <div class="meta truncate">process=${escapeHtml(rawProcessStatus)} | session=${escapeHtml(agent.backend_session_id || "-")}</div>
      <div class="agent-permissions">
        <select data-agent-field="sandbox" data-agent-id="${escapeHtml(agent.id)}">${selectOptions(sandboxOptions, sandbox)}</select>
        <select data-agent-field="approval" data-agent-id="${escapeHtml(agent.id)}">${selectOptions(approvalOptions, approval)}</select>
      </div>
      <label class="agent-proxy">
        <input type="checkbox" data-agent-field="proxy_enabled" data-agent-id="${escapeHtml(agent.id)}" ${proxyEnabled ? "checked" : ""}>
        <span>Proxy</span>
      </label>
    `;
    card.addEventListener("click", event => {
      const clicked = event.target instanceof Element ? event.target : null;
      if (clicked?.closest("select") || clicked?.closest("input")) return;
      agentTargetEl.value = agent.id;
      agentTargetEl.dispatchEvent(new Event("change"));
      closeMobileSheets();
    });
    card.addEventListener("change", event => {
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (!target?.dataset.agentField) return;
      const value = target instanceof HTMLInputElement && target.type === "checkbox" ? target.checked : target.value;
      updateAgentConfig(agent.id, target.dataset.agentField, value);
    });
    return card;
  };

  const appendAgentSection = (title, agents, className) => {
    if (!agents.length) return;
    const section = document.createElement("section");
    section.className = `agent-section ${className}`;
    section.innerHTML = `
      <div class="agent-section-head">
        <h3>${escapeHtml(title)}</h3>
        <span>${agents.length}</span>
      </div>
    `;
    for (const agent of agents) section.appendChild(renderAgentCard(agent));
    agentsEl.appendChild(section);
  };

  appendAgentSection("Main & sub agents", executionAgents, "execution-agents");
  appendAgentSection("Supervision", supervisionAgents, "supervision-agents");

  if ([...agentTargetEl.options].some(item => item.value === previous)) agentTargetEl.value = previous;
  [...agentsEl.querySelectorAll(".agent-card")].forEach(card => {
    card.classList.toggle("active", card.dataset.agentId === agentTargetEl.value);
  });
  renderSelectedAgentInfo();
}

function syncAgentCards() {
  [...agentsEl.querySelectorAll(".agent-card")].forEach(card => {
    card.classList.toggle("active", card.dataset.agentId === agentTargetEl.value);
  });
}

function renderSelectedAgentInfo() {
  selectedAgentInfoEl.textContent = "";
  selectedAgentInfoEl.hidden = true;
}

function renderBackendStatus() {
  if (!backendStatusEl) return;
  if (!currentRunId) {
    backendStatusEl.className = "backend-status pending";
    backendStatusEl.innerHTML = `
      <span class="activity-dot"></span>
      <strong>Backend</strong>
      <code>waiting for run</code>
    `;
    return;
  }
  const state = backendStatusData;
  if (!state) {
    backendStatusEl.className = "backend-status pending";
    backendStatusEl.innerHTML = `
      <span class="activity-dot"></span>
      <strong>Backend</strong>
      <code>loading</code>
    `;
    return;
  }
  const status = state.status || "stopped";
  const detail = [
    state.target || "main",
    state.backend || "backend",
    state.pid ? `pid=${state.pid}` : "pid=-",
    state.last_reply_at ? `last reply ${formatLocalTimestamp(state.last_reply_at, state.last_reply_at)}` : ""
  ].filter(Boolean).join(" | ");
  backendStatusEl.className = `backend-status ${escapeHtml(status)}`;
  const canInterrupt = status === "busy";
  backendStatusEl.innerHTML = `
    <span class="activity-dot"></span>
    <strong>${escapeHtml(status)}</strong>
    <code title="${escapeHtml(detail)}">${escapeHtml(detail)}</code>
    ${canInterrupt ? '<button class="interrupt-button" type="button" data-backend-action="interrupt">Interrupt</button>' : ""}
  `;
}

async function updateAgentConfig(agentId, field, value) {
  const task = selectedTask();
  if (!task || !agentId || !field) return;
  const payload = { task_id: task.id, agent_id: agentId };
  payload[field] = value;
  const res = await fetchWithTimeout(apiUrl("/api/agent-config"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(runScopedPayload(payload))
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.error || "Failed to update agent config");
    return;
  }
  await loadStatus({ forceAgents: true });
  contextDetails.delete(task.id);
  await ensureActiveTabData();
  renderPanel();
}

function compactText(value, limit = 180) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}...` : text;
}

function shouldCollapseMessage(value) {
  const text = String(value ?? "");
  return text.length > collapsedMessageCharLimit || text.split("\n").length > collapsedMessageLineLimit;
}

function renderMessageBody(body, key = "") {
  const text = String(body || "");
  if (!shouldCollapseMessage(text)) {
    return `<div class="message-body">${escapeHtml(text)}</div>`;
  }
  const lines = text.split("\n").length;
  const summary = compactText(text, 220);
  const open = key && expandedMessageKeys.has(key) ? " open" : "";
  return `
    <details class="message-body collapsed-message" data-message-key="${escapeHtml(key)}"${open}>
      <summary>
        <span>${escapeHtml(summary || "(empty message)")}</span>
        <em>${escapeHtml(`${text.length} chars | ${lines} lines`)}</em>
      </summary>
      <div class="message-body-full">${escapeHtml(text)}</div>
    </details>
  `;
}

function selectedWorkspacePath() {
  if (!workspaceSelectEl) return "";
  return workspaceSelectEl.value === "__custom__" ? (workspaceCustomEl?.value || "").trim() : workspaceSelectEl.value;
}

function selectedWorkspaceId() {
  if (!workspaceSelectEl || workspaceSelectEl.value === "__custom__") return "";
  const option = workspaceSelectEl.options[workspaceSelectEl.selectedIndex];
  return option?.dataset.workspaceId || "";
}

function selectedWorkspaceLabel() {
  if (!workspaceSelectEl) return "";
  if (workspaceSelectEl.value === "__custom__") return selectedWorkspacePath();
  const option = workspaceSelectEl.options[workspaceSelectEl.selectedIndex];
  return option?.textContent || selectedWorkspacePath();
}

function addTaskConfirmRows(payload) {
  const proxyConfigured = Boolean(payload.http_proxy || payload.https_proxy);
  const proxyLabel = payload.proxy_enabled
    ? `enabled${proxyConfigured ? ", configured" : ""}`
    : proxyConfigured
      ? "configured, disabled by default"
      : "off";
  return [
    ["Run", currentRunId || "-"],
    ["Title", payload.title],
    ["Description", payload.description || "-"],
    ["Workspace", selectedWorkspaceLabel() || payload.workspace_path || payload.workspace_id || "-"],
    ["Backend", `${payload.backend || "default"} / ${payload.model || "default"}`],
    ["Sandbox", payload.sandbox || "-"],
    ["Approval", payload.approval || "-"],
    ["Collaboration", `${payload.collaboration_mode || "auto"} (${payload.max_sub_agents || 0})`],
    ["Supervision", taskSupervisionSummary({ supervision: payload.supervision || {} })],
    ["Proxy", proxyLabel]
  ];
}

function confirmAddTask(payload) {
  const fallbackText = [
    `Create task "${payload.title}"?`,
    payload.description ? `Description: ${payload.description}` : "",
    `Run: ${currentRunId || "-"}`,
    `Workspace: ${selectedWorkspaceLabel() || payload.workspace_path || payload.workspace_id || "-"}`
  ].filter(Boolean).join("\n");
  if (!taskCreateConfirmDialogEl || typeof taskCreateConfirmDialogEl.showModal !== "function") {
    return Promise.resolve(window.confirm(fallbackText));
  }
  if (taskCreateConfirmDetailsEl) {
    taskCreateConfirmDetailsEl.innerHTML = addTaskConfirmRows(payload).map(([label, value]) => `
      <div>
        <dt>${escapeHtml(label)}</dt>
        <dd>${escapeHtml(value || "-")}</dd>
      </div>
    `).join("");
  }
  if (taskCreateConfirmDialogEl.open) taskCreateConfirmDialogEl.close("cancel");
  return new Promise(resolve => {
    const onClose = () => resolve(taskCreateConfirmDialogEl.returnValue === "confirm");
    taskCreateConfirmDialogEl.returnValue = "cancel";
    taskCreateConfirmDialogEl.addEventListener("close", onClose, { once: true });
    try {
      taskCreateConfirmDialogEl.showModal();
    } catch (_err) {
      taskCreateConfirmDialogEl.removeEventListener("close", onClose);
      resolve(window.confirm(fallbackText));
    }
  });
}

function renderConversationFilters() {
  if (!conversationFiltersEl) return;
  conversationFiltersEl.classList.toggle("hidden", activeTab !== "conversation");
  if (activeTab !== "conversation") return;
  const task = selectedTask();
  const counts = task ? conversationFilterCounts(task.id) : {};
  conversationFiltersEl.innerHTML = `
    <span>Show</span>
    ${conversationFilterOptions.map(item => `
      <label class="filter-chip ${conversationFilters[item.key] ? "active" : ""}">
        <input type="checkbox" data-conversation-filter="${escapeHtml(item.key)}" ${conversationFilters[item.key] ? "checked" : ""}>
        <span>${escapeHtml(item.label)}</span>
        <code>${escapeHtml(counts[item.key] ?? 0)}</code>
      </label>
    `).join("")}
  `;
}

function renderConversation(taskId) {
  copyTextByKey.clear();
  const state = conversationState(taskId);
  if (!state.initialized || state.loading && !state.events.length) {
    return `<div class="empty">Loading conversation...</div>`;
  }
  if (state.error && !state.events.length) {
    return `<div class="empty">Conversation unavailable. Realtime updates will start from the latest event offset.<br><code>${escapeHtml(state.error)}</code></div>`;
  }
  const events = taskConversationEvents(taskId);
  if (!events.length && !state.hasMore) {
    const timer = renderTurnTimer(taskId);
    const metricsDock = timer ? "" : renderPromptMetricsDock(taskId);
    const empty = `<div class="empty">No conversation for ${escapeHtml(backendTarget())} yet.</div>`;
    return `<div class="conversation timeline">${empty}${timer}${metricsDock}</div>`;
  }
  const older = state.hasMore ? `<button class="load-older" type="button" data-load-older="true">${state.loading ? "Loading..." : "Load older"}</button>` : "";
  const timer = renderTurnTimer(taskId);
  const metricsDock = timer ? "" : renderPromptMetricsDock(taskId);
  return `<div class="conversation timeline">${older}${events.map(renderTimelineEvent).join("")}${timer}${metricsDock}</div>`;
}

function agentUpdateTitle(data) {
  const target = data.target || "main";
  return target === "host" ? "host update" : `agent update (${target})`;
}

function agentUpdateBody(data) {
  const text = String(data.text || "");
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

function renderTimelineEvent(event) {
  const data = eventData(event);
  if (event.type === "message") {
    const displaySender = data.display_sender || data.sender || "-";
    const displayTarget = data.display_target || data.to_agent || data.role || data.target || "-";
    const cls = data.display_sender ? "from-supervision" : data.sender === "browser" ? "from-browser" : data.sender === "main" ? "from-main" : data.sender === "system" ? "from-system" : "";
    return renderTimelineCard(
      `${displaySender} -> ${displayTarget}`,
      data.message || "",
      eventTimeLabel(event),
      cls,
      event._uiKey
    );
  }
  if (event.type === "agent_message") return renderTimelineCard(agentUpdateTitle(data), agentUpdateBody(data), eventTimeLabel(event), "agent-update", event._uiKey);
  if (event.type === "agent_command_started") return renderTimelineCard(`running command (${data.target || "main"})`, data.command || "", eventTimeLabel(event), "agent-command", event._uiKey);
  if (event.type === "agent_command_finished") {
    const output = data.output_tail
      ? `\n\nOutput tail:\n${data.output_tail}`
      : data.output_tail_omitted
        ? `\n\nOutput tail omitted (${data.output_tail_chars || 0} chars).`
        : "";
    return renderTimelineCard(`command finished (${data.target || "main"}) exit=${data.exit_code ?? "-"}`, `${data.command || ""}${output}`, eventTimeLabel(event), data.exit_code === 0 ? "agent-command" : "event-error", event._uiKey);
  }
  if (event.type === "agent_error") return renderTimelineCard(`agent error (${data.target || "main"})`, data.message || JSON.stringify(data), eventTimeLabel(event), "event-error", event._uiKey);
  if (event.type === "agent_usage") {
    const usage = data.usage || {};
    return renderTimelineStatus(
      "usage",
      `input=${usage.input_tokens ?? "-"} cached=${usage.cached_input_tokens ?? "-"} output=${usage.output_tokens ?? "-"} reasoning=${usage.reasoning_output_tokens ?? "-"}`,
      "usage",
      eventTimeLabel(event)
    );
  }
  if (event.type === "agent_prompt_metrics") {
    const total = data.total || {};
    const rows = componentMetricRows(data.components || {}, Number(total.chars || 0));
    const top = rows[0]?.name ? ` top=${rows[0].name}:${rows[0].chars}` : "";
    return renderTimelineStatus(
      "prompt metrics",
      `chars=${total.chars ?? "-"} bytes=${total.bytes ?? "-"} lines=${total.lines ?? "-"}${top}`,
      "usage",
      eventTimeLabel(event)
    );
  }
  if (event.type === "agent_context_overflow") {
    return renderTimelineStatus("context overflow", data.message || data.reason || "context_window", "failed", eventTimeLabel(event));
  }
  const ts = eventTimeLabel(event);
  if (event.type === "task_status_changed") return renderTimelineStatus(`task ${data.status}`, `exit=${data.exit_code ?? "-"}`, data.status, ts);
  if (event.type === "task_started") return renderTimelineStatus("task started", data.title || "", "running", ts);
  if (event.type === "task_finished") return renderTimelineStatus(`task ${data.status || "finished"}`, `exit=${data.exit_code ?? "-"}`, data.status || "completed", ts);
  if (event.type === "task_result_written") return renderTimelineStatus("final written", `${data.chars || 0} chars`, "completed", ts);
  if (event.type === "task_final_requested") {
    const isRoundSummary = data.policy === "round_summary";
    return renderTimelineStatus(isRoundSummary ? "round summary requested" : "final requested", `target=${data.target || "main"}`, "running", ts);
  }
  if (event.type === "task_round_summary_requested") return renderTimelineStatus("round summary requested", `target=${data.target || "main"}`, "running", ts);
  if (event.type === "task_reopened") return renderTimelineStatus("task reopened", data.task_id || "-", "awaiting_user", ts);
  if (event.type === "task_completed") return renderTimelineStatus("task completed", `exit=${data.exit_code ?? "-"}`, "completed", ts);
  if (event.type === "task_waiting_for_subagents") return renderTimelineStatus("waiting for sub-agents", `pending=${(data.pending || []).join(", ") || "-"}`, "running", ts);
  if (event.type === "agent_started") return renderTimelineStatus("agent started", `${data.target || "main"} from ${data.sender || "-"} sandbox=${data.sandbox || "-"} approval=${data.approval || "-"} proxy=${data.proxy_enabled ? "on" : "off"}`, "running", ts);
  if (event.type === "agent_interrupted") return renderTimelineStatus("agent interrupted", data.agent_id || data.target || "main", "interrupted", ts);
  if (event.type === "agent_status_changed") return renderTimelineStatus("agent status", `${data.agent_id || "-"} ${data.status || "-"}`, data.status || "session", ts);
  if (event.type === "agent_config_updated") return renderTimelineStatus("agent config updated", `${data.agent_id || "-"} sandbox=${data.sandbox || "-"} approval=${data.approval || "-"} proxy=${data.proxy_enabled ? "on" : "off"}`, "session", ts);
  if (event.type === "task_proxy_config_updated") return renderTimelineStatus("task proxy updated", `default=${data.proxy_enabled ? "on" : "off"} http=${data.http_proxy_configured ? "set" : "-"} https=${data.https_proxy_configured ? "set" : "-"} no_proxy=${data.no_proxy_configured ? "set" : "-"}`, "session", ts);
  if (event.type === "task_supervision_config_updated") return renderTimelineStatus("task supervision updated", `${data.mode || "-"} via ${data.host_backend || "stub"} max_rounds=${data.max_rounds || "-"}`, "session", ts);
  if (event.type === "main_reported_to_host") return renderTimelineStatus("main reported to host", `${data.host_backend || "stub"} ${data.channel || "main_only"} reply=${data.reply_chars || 0} chars`, "session", ts);
  if (event.type === "host_decision") return renderTimelineStatus("host decision", data.decision || "-", "session", ts);
  if (event.type === "main_applied_decision") return renderTimelineStatus("main applied host decision", `${data.decision || "-"} effect=${data.effect || "noop"}`, data.applied ? "running" : "session", ts);
  if (event.type === "agent_thread") return renderTimelineStatus(`${data.source || "backend"} session`, data.thread_id || "-", "session", ts);
  if (event.type === "agent_finished") return renderTimelineStatus("agent finished", `exit=${data.exit_code ?? "-"}`, data.exit_code === 0 ? "completed" : "failed", ts);
  if (event.type === "task_dispatched") return renderTimelineStatus("task dispatched", `target=${data.target || "-"}`, "session", ts);
  if (event.type === "agent_created") return renderTimelineStatus("sub-agent created", `${data.agent_id || "-"} backend=${data.backend || "-"}`, "session", ts);
  if (event.type === "agent_delegated") return renderTimelineStatus("delegated", `${data.count || 0} action(s)`, "session", ts);
  if (event.type === "agent_message_routed") return renderTimelineStatus("routed to agent", `${data.target || "-"} ${data.reason || ""}`, "running", ts);
  if (event.type === "claimed_sub_without_aha_agent") return renderTimelineStatus("sub-agent claim mismatch", data.reason || "claimed without AHA spawn_sub", "failed", ts);
  if (event.type === "native_subagent_tool_used") return renderTimelineStatus("native subagent blocked", `${data.tool_name || "-"} ${data.reason || ""}`, "failed", ts);
  if (event.type === "sub_agent_reported") return renderTimelineStatus("sub-agent reported", `${data.agent_id || "-"} ${data.status || "-"}`, data.status || "session", ts);
  if (event.type === "sub_agent_report_ignored") return renderTimelineStatus("sub-agent report ignored", `${data.agent_id || "-"} ${data.reason || ""}`, "session", ts);
  if (event.type === "sub_agent_backend_recovered") return renderTimelineStatus("sub-agent backend recovered", `${data.agent_id || "-"} attempt=${data.attempt || "-"}`, "running", ts);
  if (event.type === "sub_agent_backend_failed") return renderTimelineStatus("sub-agent backend failed", `${data.agent_id || "-"} attempts=${data.attempts || "-"}`, "failed", ts);
  if (event.type === "workspace_missing") return renderTimelineStatus("workspace missing", data.workspace_path || "-", "blocked", ts);
  return renderTimelineStatus(event.type, JSON.stringify(data), "session", ts);
}

function renderTurnTimer(taskId) {
  const task = (statusData?.tasks || []).find(item => item.id === taskId);
  const target = backendTarget();
  const agent = (task?.agents || []).find(item => item.id === target);
  const timing = latestTurnTiming(taskId) || {
    startedAt: null,
    finishedAt: null,
    elapsedMs: 0,
    running: false,
    status: "idle",
    target,
    sender: "-"
  };
  const title = timing.running
    ? (timing.status === "waiting" ? "Agent is waiting" : "Agent is working")
    : timing.status === "idle"
      ? "Agent is idle"
      : `Agent turn ${timing.status}`;
  const label = timing.status === "idle" ? "" : (timing.running ? "elapsed" : "duration");
  const details = [
    label ? `${label} ${formatDuration(timing.elapsedMs)}` : "",
    `status ${timing.status}`,
    `target ${timing.target}`,
    agent ? `agent ${agentLifecycleStatus(agent)}` : "",
    timing.startedAt ? `started ${formatClock(timing.startedAt)}` : "",
    timing.finishedAt ? `finished ${formatClock(timing.finishedAt)}` : ""
  ].filter(Boolean).join(" | ");
  return `
    <div class="turn-timer ${escapeHtml(timing.status)}">
      <span class="activity-dot"></span>
      <strong>${escapeHtml(title)}</strong>
      <code>${escapeHtml(details)}</code>
      ${renderPromptMetricsPopover(taskId)}
    </div>
  `;
}

function renderTimelineCard(title, body, ts, cls, key = "") {
  const copyKey = String(key || "");
  if (copyKey) copyTextByKey.set(copyKey, String(body || ""));
  const copyButton = copyKey
    ? `<button class="message-copy" type="button" data-copy-message-key="${escapeHtml(copyKey)}" data-copy-state="idle" title="Copy message" aria-label="Copy message"><span class="message-copy-icon" aria-hidden="true"></span><span class="message-copy-label sr-only">Copy message</span></button>`
    : "";
  return `
    <div class="message ${cls}">
      <div class="message-head">
        <span class="message-title">${escapeHtml(title)}</span>
        <span class="message-actions">
          <time>${escapeHtml(ts || "")}</time>
          ${copyButton}
        </span>
      </div>
      ${renderMessageBody(body, key)}
    </div>
  `;
}

function renderTimelineStatus(title, body, status, ts = "") {
  return `
    <div class="timeline-status ${escapeHtml(status || "")}">
      <span>${escapeHtml(title)}</span>
      <code>${escapeHtml(body || "")}</code>
      <time>${escapeHtml(ts || "")}</time>
    </div>
  `;
}

function renderBootstrapWorkspaceOptions() {
  const options = workspaceData.map((workspace, index) => `
    <option value="${escapeHtml(workspace.path || "")}" data-workspace-id="${escapeHtml(workspace.id || "")}" ${index === 0 ? "selected" : ""}>
      ${escapeHtml(workspace.label || workspace.name || workspace.path || "Workspace")}
    </option>
  `).join("");
  const selected = workspaceData.length ? "" : " selected";
  return `${options}<option value="__custom__"${selected}>Custom path...</option>`;
}

function renderFirstRunState(force = false) {
  if (bootstrapError) {
    renderBootstrapError(bootstrapError);
    return;
  }
  document.body.classList.add("empty-run");
  currentRunId = "";
  statusData = null;
  selectedTaskId = null;
  backendStatusData = null;
  if (eventSocket) closeEventWebSocket();
  renderSessionMenu();
  if (summaryEl) summaryEl.textContent = "No runs yet";
  renderTaskList();
  renderSelectedHeader();
  if (selectedTitleEl) selectedTitleEl.textContent = "Create a run";
  renderAgents();
  renderBackendStatus();
  renderPendingMessages();
  conversationFiltersEl?.classList.add("hidden");
  if (!force && panelEl.querySelector("[data-bootstrap-run-form]")) return;

  const defaultWorkspacePath = bootstrapData?.default_workspace_path || "";
  const customHidden = workspaceData.length ? " hidden" : "";
  panelEl.innerHTML = `
    <div class="bootstrap-panel">
      <div class="bootstrap-head">
        <h3>First Run</h3>
        <code>${escapeHtml(bootstrapData?.aha_home || "")}</code>
      </div>
      <form class="bootstrap-form" data-bootstrap-run-form>
        <label class="field-label">
          <span>Workspace</span>
          <select data-bootstrap-workspace-select>${renderBootstrapWorkspaceOptions()}</select>
        </label>
        <input data-bootstrap-workspace-custom class="${customHidden}" placeholder="Workspace path" value="${escapeHtml(workspaceData.length ? "" : defaultWorkspacePath)}">
        <label class="field-label">
          <span>Run goal</span>
          <input data-bootstrap-run-goal placeholder="What should AHA work on?" autofocus>
        </label>
        <label class="field-label">
          <span>Mode</span>
          <select data-bootstrap-run-mode>
            <option value="research">research</option>
            <option value="implementation">implementation</option>
          </select>
        </label>
        <label class="field-label">
          <span>Collaboration</span>
          <select data-bootstrap-collaboration-mode>
            ${renderCollaborationModeOptions("auto")}
          </select>
          <div data-bootstrap-collaboration-help class="collaboration-help"></div>
        </label>
        <details class="bootstrap-proxy">
          <summary>Proxy</summary>
          <div class="proxy-form">
            <label class="field-label">
              <span>HTTP proxy</span>
              <input data-bootstrap-http-proxy placeholder="http://127.0.0.1:7890">
            </label>
            <label class="field-label">
              <span>HTTPS proxy</span>
              <input data-bootstrap-https-proxy placeholder="http://127.0.0.1:7890">
            </label>
            <label class="field-label">
              <span>NO_PROXY</span>
              <input data-bootstrap-no-proxy placeholder="localhost,127.0.0.1,::1">
            </label>
            <label class="proxy-toggle">
              <input data-bootstrap-proxy-enabled type="checkbox">
              <span>Enable proxy for initial agents</span>
            </label>
          </div>
        </details>
        <button type="submit">Start Run and Initial Task</button>
      </form>
    </div>
  `;
  syncBootstrapCollaborationHelp(panelEl.querySelector("[data-bootstrap-run-form]"));
}

function renderBootstrapError(error) {
  document.body.classList.add("empty-run");
  currentRunId = "";
  statusData = null;
  selectedTaskId = null;
  backendStatusData = null;
  if (eventSocket) closeEventWebSocket();
  if (summaryEl) summaryEl.textContent = "Bootstrap failed";
  renderSessionMenu();
  renderTaskList();
  renderSelectedHeader();
  if (selectedTitleEl) selectedTitleEl.textContent = "Backend version mismatch";
  renderAgents();
  renderBackendStatus();
  renderPendingMessages();
  conversationFiltersEl?.classList.add("hidden");
  panelEl.innerHTML = `
    <div class="bootstrap-panel">
      <div class="bootstrap-head">
        <h3>Backend Not Ready</h3>
        <code>${escapeHtml(location.origin)}</code>
      </div>
      <p class="meta">前端已经加载，但当前 Web 后端不支持新的 bootstrap API。请重启后端或确认浏览器连接的是同一份 AHA 代码。</p>
      <pre>${escapeHtml(String(error || ""))}</pre>
    </div>
  `;
}

function renderWeixinConsole() {
  const payload = weixinState.status || {};
  const pairing = payload.pairing || {};
  const account = payload.account || {};
  const paired = Boolean(payload.paired);
  const status = weixinPairingStatus();
  const notifications = payload.notifications || {};
  const notificationsEnabled = Boolean(notifications.enabled);
  const statusText = {
    idle: "未配对",
    waiting: "等待扫码",
    scanned: "已扫码，等待确认",
    paired: "已配对",
    expired: "二维码已过期"
  }[status] || status;
  const qrSvg = pairing.qrcode_svg || "";
  const qrSrc = qrSvg ? `data:image/svg+xml;charset=utf-8,${encodeURIComponent(qrSvg)}` : "";
  const pairingActive = ["waiting", "scanned"].includes(status);
  const displayPaired = paired && !pairingActive;
  const canSendTest = paired && !weixinState.sending && !weixinState.loading;
  const notificationToggleDisabled = !paired || weixinState.loading || weixinState.togglingNotifications;
  return `
    <div class="weixin-console">
      <div class="weixin-console-head">
        <div>
          <h3>微信操作台</h3>
          <p>当前 Run: ${escapeHtml(currentRunId || "-")}</p>
        </div>
        <span class="status ${displayPaired ? "completed" : "session"}">${escapeHtml(statusText)}</span>
      </div>
      <div class="weixin-console-actions">
        <button type="button" data-weixin-action="pair" ${weixinState.loading ? "disabled" : ""}>${status === "waiting" || status === "scanned" ? "重新生成二维码" : "配对"}</button>
        <button type="button" data-weixin-action="refresh" ${weixinState.loading ? "disabled" : ""}>刷新状态</button>
      </div>
      ${weixinState.loading ? '<div class="weixin-console-note">正在连接微信服务...</div>' : ""}
      ${weixinState.error ? `<div class="weixin-console-note error">${escapeHtml(weixinState.error)}</div>` : ""}
      ${weixinState.notice ? `<div class="weixin-console-note success">${escapeHtml(weixinState.notice)}</div>` : ""}
      ${qrSrc && status !== "paired" ? `
        <div class="weixin-qr">
          <img src="${escapeHtml(qrSrc)}" alt="微信配对二维码">
          <p>${status === "scanned" ? "已扫码，请在微信里确认授权。" : "用微信扫码并确认授权，页面会自动刷新配对状态。"}</p>
          ${pairing.qrcode_payload ? `<a href="${escapeHtml(pairing.qrcode_payload)}" target="_blank" rel="noreferrer">二维码无法识别时打开链接</a>` : ""}
        </div>
      ` : ""}
      <div class="weixin-console-grid">
        <section>
          <strong>账号</strong>
          <code>${escapeHtml(account.user_id || pairing.user_id || "未配对")}</code>
        </section>
        <section>
          <strong>通道</strong>
          <code>${paired ? "可发送" : "等待配对"}</code>
        </section>
      </div>
      <div class="weixin-notifications">
        <label class="checkbox-line">
          <input type="checkbox" data-weixin-notifications-toggle ${notificationsEnabled ? "checked" : ""} ${notificationToggleDisabled ? "disabled" : ""}>
          <span>微信通知</span>
        </label>
        <small>${paired ? "状态变更、等待用户、完成摘要会推送到当前微信。" : "配对成功后可开启任务通知。"}</small>
      </div>
      <label class="weixin-test">
        <span>测试通知</span>
        <textarea data-weixin-test-message rows="3">${escapeHtml(weixinState.testMessage)}</textarea>
      </label>
      <button type="button" data-weixin-action="test" ${canSendTest ? "" : "disabled"}>${weixinState.sending ? "发送中..." : "发送测试通知"}</button>
    </div>
  `;
}

function bootstrapWorkspaceSelection(form) {
  const select = form.querySelector("[data-bootstrap-workspace-select]");
  const custom = form.querySelector("[data-bootstrap-workspace-custom]");
  const selectedOption = select?.options?.[select.selectedIndex];
  const customSelected = select?.value === "__custom__";
  return {
    workspaceId: customSelected ? "" : (selectedOption?.dataset.workspaceId || ""),
    workspacePath: customSelected ? String(custom?.value || "").trim() : String(select?.value || "").trim(),
    customSelected
  };
}

async function createRunFromBootstrapForm(form) {
  const goalEl = form.querySelector("[data-bootstrap-run-goal]");
  const modeEl = form.querySelector("[data-bootstrap-run-mode]");
  const collaborationEl = form.querySelector("[data-bootstrap-collaboration-mode]");
  const proxyEnabledEl = form.querySelector("[data-bootstrap-proxy-enabled]");
  const httpProxyEl = form.querySelector("[data-bootstrap-http-proxy]");
  const httpsProxyEl = form.querySelector("[data-bootstrap-https-proxy]");
  const noProxyEl = form.querySelector("[data-bootstrap-no-proxy]");
  const submit = form.querySelector('button[type="submit"]');
  const goal = String(goalEl?.value || "").trim();
  if (!goal) {
    goalEl?.focus();
    return;
  }
  let { workspaceId, workspacePath, customSelected } = bootstrapWorkspaceSelection(form);
  if (submit) submit.disabled = true;
  try {
    if (customSelected && workspacePath) {
      const payload = await fetchJson("/api/workspaces", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: workspacePath, name: pathName(workspacePath) })
      }, "Failed to add workspace");
      workspaceId = payload.workspace?.id || "";
      workspacePath = payload.workspace?.path || workspacePath;
      await loadWorkspaces().catch(() => {});
    }
    const httpProxy = String(httpProxyEl?.value || "").trim();
    const httpsProxy = String(httpsProxyEl?.value || "").trim();
    const proxyEnabled = Boolean(proxyEnabledEl?.checked || httpProxy || httpsProxy);
    await createRun(goal, modeEl?.value || "research", {
      collaborationMode: collaborationEl?.value || "auto",
      workspaceId,
      workspacePath,
      proxyEnabled,
      httpProxy,
      httpsProxy,
      noProxy: proxyEnabled ? String(noProxyEl?.value || "").trim() : ""
    });
  } catch (err) {
    alert(err?.message || String(err));
  } finally {
    if (submit) submit.disabled = false;
  }
}

function renderPanel(options = {}) {
  renderConversationFilters();
  if (!currentRunId) {
    renderFirstRunState();
    return;
  }
  const task = selectedTask();
  if (!task) {
    panelEl.innerHTML = '<div class="empty">No task selected.</div>';
    return;
  }
  if (activeTab === "conversation") {
    const previousTop = options.previousTop ?? panelEl.scrollTop;
    const previousHeight = options.previousHeight ?? panelEl.scrollHeight;
    const metricsPopoverState = capturePromptMetricsPopoverState();
    const metricsPopoverOpen = Boolean(metricsPopoverState);
    const shouldFollow = !metricsPopoverOpen && (conversationAutoFollow || isPanelNearBottom());
    panelEl.innerHTML = renderConversation(task.id);
    if (options.preserveScroll) {
      panelEl.scrollTop = panelEl.scrollHeight - previousHeight + previousTop;
    } else if (metricsPopoverOpen) {
      panelEl.scrollTop = previousTop;
    } else {
      panelEl.scrollTop = shouldFollow ? panelEl.scrollHeight : previousTop;
    }
    restorePromptMetricsPopoverState(metricsPopoverState);
    positionPromptMetricsPopover();
    return;
  }
  if (activeTab === "final") {
    const detail = finalDetails.get(task.id);
    panelEl.innerHTML = detail
      ? `<pre>${escapeHtml(detail.result || "No Final yet. Use /aha final to generate it.")}</pre>`
      : '<div class="empty">Loading final...</div>';
  } else if (activeTab === "logs") {
    const state = logState(task.id);
    const previousTop = options.previousTop ?? panelEl.scrollTop;
    const previousHeight = options.previousHeight ?? panelEl.scrollHeight;
    const shouldFollow = state.autoFollow;
    const older = state.hasMore ? `<button class="load-older" type="button" data-load-older-log="true">${state.loading ? "Loading..." : "Load older logs"}</button>` : "";
    const body = state.initialized ? localizeTimestampText(state.text || "No logs yet.") : "Loading logs...";
    panelEl.innerHTML = `<div class="log-view">${older}<pre>${escapeHtml(body)}</pre></div>`;
    if (options.preserveScroll) {
      panelEl.scrollTop = panelEl.scrollHeight - previousHeight + previousTop;
    } else if (state.initialized) {
      panelEl.scrollTop = shouldFollow ? panelEl.scrollHeight : previousTop;
    }
  } else {
    const detail = contextDetails.get(task.id);
    if (!detail) {
      panelEl.innerHTML = '<div class="empty">Loading context...</div>';
      return;
    }
    const context = [
      "Task:",
      JSON.stringify(localizeTimestampFields(detail.task), null, 2),
      "",
      "Sessions:",
      JSON.stringify(localizeTimestampFields(detail.sessions || []), null, 2),
      "",
      "Prompt:",
      detail.prompt ? localizeTimestampText(detail.prompt) : "No prompt file."
    ].join("\n");
    panelEl.innerHTML = `<pre>${escapeHtml(context)}</pre>`;
  }
}

function isPanelNearBottom() {
  return panelEl.scrollHeight - panelEl.scrollTop - panelEl.clientHeight < 80;
}

async function activateTab(tab) {
  activeTab = tab || "conversation";
  if (activeTab === "conversation") conversationAutoFollow = true;
  if (activeTab === "logs" && selectedTaskId) logState(selectedTaskId).autoFollow = true;
  document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item.dataset.tab === activeTab));
  syncMobileActionPanel();
  await ensureActiveTabData();
  renderPanel();
}

document.querySelectorAll(".tab").forEach(button => {
  button.addEventListener("click", () => activateTab(button.dataset.tab));
});

taskFormEl?.addEventListener("submit", async event => {
  event.preventDefault();
  if (!currentRunId) {
    alert("请先创建 Run，再添加任务。");
    return;
  }
  const title = newTaskTitleEl?.value.trim() || "";
  if (!title) return;
  const description = newTaskDescriptionEl?.value.trim() || "";
  const collaborationMode = collaborationModeEl?.value || "auto";
  const maxSubAgents = collaborationModeMaxSubAgents(collaborationMode);
  const supervision = taskSupervisionPayloadFromMode(
    taskSupervisionModeEl?.value || "manual",
    taskSupervisionMaxRoundsEl?.value || "5",
    readAskUserGateControls(taskSupervisionAskUserGatesEl)
  );
  setCreateProxyDefaultsFromInputs();
  const createHttpProxy = taskHttpProxyEl?.value.trim() || "";
  const createHttpsProxy = taskHttpsProxyEl?.value.trim() || "";
  const createProxyEnabled = Boolean(taskProxyEnabledEl?.checked);
  const payload = {
    title,
    description,
    backend: taskBackendEl.value,
    model: taskModelEl.value || null,
    sandbox: taskSandboxEl.value,
    approval: taskApprovalEl.value,
    proxy_enabled: createProxyEnabled,
    http_proxy: createHttpProxy,
    https_proxy: createHttpsProxy,
    no_proxy: (createProxyEnabled || createHttpProxy || createHttpsProxy) ? (taskNoProxyEl?.value.trim() || "") : "",
    workspace_id: selectedWorkspaceId(),
    workspace_path: selectedWorkspacePath(),
    collaboration_mode: collaborationMode,
    delegation_policy: collaborationModeDelegationPolicy(collaborationMode),
    max_sub_agents: maxSubAgents,
    preferred_sub_backend: taskBackendEl.value,
    supervision,
    dispatch: true
  };
  const reopenCreateDialog = Boolean(taskCreateDialogEl?.open);
  if (reopenCreateDialog) closeTaskCreateDialog();
  const confirmed = await confirmAddTask(payload);
  if (!confirmed) {
    if (reopenCreateDialog) openTaskCreateDialog();
    return;
  }
  try {
    const response = await fetchJson(apiUrl("/api/tasks"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(runScopedPayload(payload))
    }, "Failed to create task");
    const createdTaskId = String(response?.task?.id || "").trim();
    if (createdTaskId) {
      const previousTaskId = selectedTaskId;
      selectedTaskId = createdTaskId;
      writeStoredSelectedTaskId(createdTaskId);
      if (previousTaskId !== createdTaskId) {
        eventSocketReconnectAt = 0;
        closeEventWebSocket();
      }
    }
    if (newTaskTitleEl) newTaskTitleEl.value = "";
    if (newTaskDescriptionEl) newTaskDescriptionEl.value = "";
    await loadStatus({ forceAgents: Boolean(createdTaskId) });
    if (createdTaskId) await selectTask(createdTaskId);
    closeMobileSheets();
    closeTaskCreateDialog();
  } catch (err) {
    if (reopenCreateDialog) openTaskCreateDialog();
    alert(err.message || String(err));
  }
});

sendFormEl.addEventListener("submit", async event => {
  event.preventDefault();
  const task = selectedTask();
  const message = messageEl.value.trim();
  if (!task || !message) return;
  const agentId = agentTargetEl.value || "main";
  const isAha = isAhaCommand(message);
  realtimeDebug("composer.submit", { task_id: task.id, target: agentId, is_aha: isAha, backend_active: selectedBackendActive() });
  try {
    let response = null;
    if (selectedBackendActive() && !isAha) {
      addPendingMessage(message, task, agentId);
    } else if (isAha) {
      response = await sendBackendMessage(task, agentId, message);
      if (isInterruptCommand(message) && response?.interrupt?.interrupted) {
        interruptedContexts.add(messageContextKey(task.id, agentId));
      }
    } else {
      response = await flushPendingMessages(task, agentId, message);
    }
    messageEl.value = "";
    syncMobileComposerAction();
    commandMenuEl.classList.add("hidden");
    closeMobileActionPanel();
    const accepted = await catchUpRealtimeEvents();
    realtimeDebug("composer.catchup_complete", { accepted_count: accepted.length });
    await loadStatus({ forceAgents: Boolean(response?.interrupt) });
    await loadBackendStatus();
    conversationAutoFollow = true;
    renderPendingMessages();
    renderPanel();
  } catch (err) {
    realtimeDebug("composer.error", { error: err?.message || String(err) });
    alert(err.message || String(err));
  }
});

messageEl.addEventListener("input", () => {
  commandSelection = 0;
  syncMobileComposerAction();
  renderCommandMenu();
});
messageEl.addEventListener("focus", () => {
  renderCommandMenu();
});
messageEl.addEventListener("keydown", event => {
  const commands = matchingSlashCommands();
  if (!commands.length) return;
  if (event.key === "ArrowDown") {
    event.preventDefault();
    commandSelection = (commandSelection + 1) % commands.length;
    renderCommandMenu();
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    commandSelection = (commandSelection + commands.length - 1) % commands.length;
    renderCommandMenu();
  } else if (event.key === "Tab") {
    event.preventDefault();
    applySlashCommand(commandSelection);
  } else if (event.key === "Enter" && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
    const command = commands[commandSelection];
    if (command && messageEl.value.trim() !== command.insert.trim()) {
      event.preventDefault();
      applySlashCommand(commandSelection);
    }
  } else if (event.key === "Escape") {
    commandMenuEl.classList.add("hidden");
  }
});
commandMenuEl.addEventListener("mousedown", event => {
  const target = event.target instanceof Element ? event.target.closest("[data-command-index]") : null;
  if (!target) return;
  event.preventDefault();
  applySlashCommand(Number(target.dataset.commandIndex || "0"));
});

pendingMessagesEl.addEventListener("click", event => {
  const button = event.target instanceof Element ? event.target.closest("[data-remove-pending]") : null;
  if (!button) return;
  removePendingMessage(button.dataset.removePending || "");
});

backendStatusEl.addEventListener("click", async event => {
  const button = event.target instanceof Element ? event.target.closest("[data-backend-action='interrupt']") : null;
  if (!button) return;
  const task = selectedTask();
  if (!task) return;
  button.disabled = true;
  const agentId = backendTarget();
  try {
    const response = await sendBackendMessage(task, agentId, "/aha interrupt");
    if (response?.interrupt?.interrupted) interruptedContexts.add(messageContextKey(task.id, agentId));
    const accepted = await catchUpRealtimeEvents();
    realtimeDebug("interrupt.catchup_complete", { accepted_count: accepted.length });
    await loadStatus({ forceAgents: true });
    await loadBackendStatus();
    renderPendingMessages();
    renderPanel();
  } catch (err) {
    alert(err.message || String(err));
  } finally {
    button.disabled = false;
  }
});

conversationFiltersEl.addEventListener("change", async event => {
  const input = event.target instanceof HTMLInputElement ? event.target : null;
  const key = input?.dataset.conversationFilter;
  if (!key || !(key in conversationFilters)) return;
  conversationFilters[key] = input.checked;
  conversationAutoFollow = true;
  await loadConversationPage(selectedTaskId, backendTarget(), false, true);
  renderPanel();
});

tasksEl.addEventListener("pointerdown", event => {
  const target = event.target instanceof Element ? event.target : null;
  const button = target?.closest("[data-action]");
  if (!button) return;
  const taskEl = button.closest("[data-task-id]");
  if (!taskEl) return;
  event.preventDefault();
  event.stopPropagation();
  updateTaskVisibility(taskEl.dataset.taskId, button.dataset.action);
});

panelEl.addEventListener("scroll", () => {
  positionPromptMetricsPopover();
  if (activeTab === "conversation") {
    conversationAutoFollow = isPanelNearBottom();
    if (panelEl.scrollTop < 48) loadOlderConversation();
  } else if (activeTab === "logs") {
    if (selectedTaskId) logState(selectedTaskId).autoFollow = isPanelNearBottom();
    if (panelEl.scrollTop < 48) loadOlderLogs();
  }
});
panelEl.addEventListener("submit", event => {
  const form = event.target instanceof Element ? event.target.closest("[data-bootstrap-run-form]") : null;
  if (!form) return;
  event.preventDefault();
  createRunFromBootstrapForm(form);
});
panelEl.addEventListener("change", event => {
  const select = event.target instanceof HTMLSelectElement ? event.target : null;
  if (!select) return;
  const form = select.closest("[data-bootstrap-run-form]");
  if (select?.matches("[data-bootstrap-collaboration-mode]")) {
    syncBootstrapCollaborationHelp(form);
    return;
  }
  if (!select?.matches("[data-bootstrap-workspace-select]")) return;
  const custom = form?.querySelector("[data-bootstrap-workspace-custom]");
  const isCustom = select.value === "__custom__";
  custom?.classList.toggle("hidden", !isCustom);
  if (isCustom) custom?.focus();
});
panelEl.addEventListener("focusin", event => {
  const input = event.target instanceof HTMLInputElement ? event.target : null;
  if (input?.matches("[data-bootstrap-http-proxy], [data-bootstrap-https-proxy], [data-bootstrap-no-proxy]")) {
    fillBootstrapProxyDefaults(input);
  }
});
panelEl.addEventListener("click", event => {
  const proxyInput = event.target instanceof HTMLInputElement ? event.target : null;
  if (proxyInput?.matches("[data-bootstrap-http-proxy], [data-bootstrap-https-proxy], [data-bootstrap-no-proxy]")) {
    fillBootstrapProxyDefaults(proxyInput);
  }
  const copyButton = event.target instanceof Element ? event.target.closest("[data-copy-message-key]") : null;
  if (copyButton) {
    event.preventDefault();
    event.stopPropagation();
    copyTimelineMessage(copyButton);
    return;
  }
  const sessionButton = event.target instanceof Element ? event.target.closest("[data-session-action='compact-reset']") : null;
  if (sessionButton instanceof HTMLButtonElement) {
    event.preventDefault();
    event.stopPropagation();
    compactResetSelectedSession(sessionButton);
    return;
  }
  const button = event.target instanceof Element ? event.target.closest("[data-load-older]") : null;
  if (button) loadOlderConversation();
  const logButton = event.target instanceof Element ? event.target.closest("[data-load-older-log]") : null;
  if (logButton) loadOlderLogs();
});
panelEl.addEventListener("toggle", event => {
  const details = event.target instanceof HTMLDetailsElement ? event.target : null;
  const metricsKey = details?.dataset.turnMetricsKey;
  if (metricsKey) {
    openPromptMetricsKey = details.open ? metricsKey : openPromptMetricsKey === metricsKey ? "" : openPromptMetricsKey;
    if (!details.open) closePromptMetricsBreakdowns(details);
    if (details.open) window.requestAnimationFrame(positionPromptMetricsPopover);
    return;
  }
  const key = details?.dataset.messageKey;
  if (!key) return;
  if (details.open) {
    expandedMessageKeys.add(key);
  } else {
    expandedMessageKeys.delete(key);
  }
}, true);
document.addEventListener("pointerdown", closePromptMetricsPopoverForOutsideEvent, true);
document.addEventListener("focusin", closePromptMetricsPopoverForOutsideEvent, true);
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closePromptMetricsPopover();
});
window.addEventListener("resize", positionPromptMetricsPopover);

agentsEl.addEventListener("pointerdown", () => markAgentsPanelEditing());
agentsEl.addEventListener("focusin", () => markAgentsPanelEditing());
agentsEl.addEventListener("change", () => markAgentsPanelEditing(1500));
taskProxyFormEl?.addEventListener("pointerdown", () => markTaskProxyEditing());
taskProxyFormEl?.addEventListener("focusin", () => markTaskProxyEditing());
taskProxyFormEl?.addEventListener("input", () => markTaskProxyEditing());
taskProxyFormEl?.addEventListener("change", () => markTaskProxyEditing());
taskProxyFormEl?.addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await saveTaskProxyConfig();
  } catch (err) {
    alert(err?.message || String(err));
  }
});
taskSupervisionFormEl?.addEventListener("pointerdown", () => markTaskSupervisionEditing());
taskSupervisionFormEl?.addEventListener("focusin", () => markTaskSupervisionEditing());
taskSupervisionFormEl?.addEventListener("input", () => markTaskSupervisionEditing());
taskSupervisionFormEl?.addEventListener("change", () => {
  markTaskSupervisionEditing();
  syncTaskSupervisionModeFields();
});
taskSupervisionFormEl?.addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await saveTaskSupervisionConfig();
  } catch (err) {
    alert(err?.message || String(err));
  }
});
[selectedTaskHttpProxyEl, selectedTaskHttpsProxyEl].forEach(input => {
  input?.addEventListener("input", () => {
    const configured = Boolean(selectedTaskHttpProxyEl?.value.trim() || selectedTaskHttpsProxyEl?.value.trim());
    if (configured && selectedTaskProxyEnabledEl && !selectedTaskProxyEnabledEl.checked) selectedTaskProxyEnabledEl.checked = true;
    if (configured && selectedTaskNoProxyEl && !selectedTaskNoProxyEl.value.trim()) selectedTaskNoProxyEl.value = defaultNoProxy;
  });
});
[selectedTaskHttpProxyEl, selectedTaskHttpsProxyEl, selectedTaskNoProxyEl].forEach(input => {
  input?.addEventListener("focus", fillSelectedTaskProxyDefaults);
  input?.addEventListener("click", fillSelectedTaskProxyDefaults);
});
agentTargetEl.addEventListener("change", async () => {
  syncAgentCards();
  renderSelectedAgentInfo();
  await loadBackendStatus();
  conversationAutoFollow = true;
  renderConversationFilters();
  await ensureConversationLoaded();
  renderPendingMessages();
  renderPanel();
});
taskBackendEl.addEventListener("change", renderModelOptions);
collaborationModeEl?.addEventListener("change", syncCollaborationFields);
newRunCollaborationEl?.addEventListener("change", syncNewRunCollaborationHelp);
taskSupervisionModeEl?.addEventListener("change", syncCreateTaskSupervisionModeFields);
[taskHttpProxyEl, taskHttpsProxyEl].forEach(input => input?.addEventListener("input", setCreateProxyDefaultsFromInputs));
[taskHttpProxyEl, taskHttpsProxyEl, taskNoProxyEl].forEach(input => {
  input?.addEventListener("focus", fillTaskCreateProxyDefaults);
  input?.addEventListener("click", fillTaskCreateProxyDefaults);
});
showHiddenEl.addEventListener("change", () => {
  const tasks = visibleTasks();
  if (!tasks.some(task => task.id === selectedTaskId)) selectedTaskId = defaultTaskId(tasks);
  writeStoredSelectedTaskId(selectedTaskId);
  renderTaskList();
  renderSelectedHeader();
  renderTaskProxyEditor();
  renderTaskSupervisionEditor();
  renderAgents();
  renderConversationFilters();
  renderPanel();
});
workspaceSelectEl.addEventListener("change", () => {
  const isCustom = workspaceSelectEl.value === "__custom__";
  workspaceCustomEl.classList.toggle("hidden", !isCustom);
  if (isCustom) workspaceCustomEl.focus();
});

document.addEventListener("visibilitychange", () => {
  realtimeDebug("document.visibilitychange", { state: document.visibilityState });
  if (document.visibilityState === "visible") requestRealtimeCatchup();
});
document.addEventListener("selectionchange", flushDeferredPanelRender);
window.addEventListener("online", () => {
  realtimeDebug("window.online");
  requestRealtimeCatchup();
});

initTaskCreateDialog();
if (taskNoProxyEl && !taskNoProxyEl.value) taskNoProxyEl.value = defaultNoProxy;
renderAskUserGateControls(taskSupervisionAskUserGatesEl, defaultAskUserGates());
syncCollaborationFields();
syncNewRunCollaborationHelp();
syncCreateTaskSupervisionModeFields();
initDesktopSidebars();
initMobileViewport();
initMobileSheets();
initMobileActionPanel();
initSessionControl();

function recordTickFailure() {
  tickFailureCount += 1;
  const multiplier = 2 ** Math.min(tickFailureCount - 1, 5);
  tickBackoffUntil = Date.now() + Math.min(30000, pollInterval * multiplier);
}

async function tick() {
  if (tickInFlight || taskActionInFlight || runActionInFlight || Date.now() < tickBackoffUntil) return;
  if (bootstrapError) return;
  if (!currentRunId) {
    renderFirstRunState();
    return;
  }
  tickInFlight = true;
  try {
    await loadStatus();
    renderPanelForRealtime();
    await Promise.all([ensureConversationLoaded(), loadBackendStatus()]);
    const autoFlushResponse = await maybeAutoFlushPending();
    if (autoFlushResponse) {
      await loadStatus({ forceAgents: true });
      await loadBackendStatus();
    }
    await syncRealtimeEvents({ allowStalePoll: selectedTaskRealtimeActive() });
    tickFailureCount = 0;
    tickBackoffUntil = 0;
    renderPendingMessages();
    renderPanelForRealtime();
  } catch (err) {
    recordTickFailure();
    panelEl.innerHTML = `<pre>${escapeHtml(String(err))}</pre>`;
  } finally {
    tickInFlight = false;
  }
}

loadBootstrap().then(async () => {
  if (currentRunId) {
    await tick();
  } else {
    renderFirstRunState(true);
  }
}).catch(err => {
  bootstrapError = err?.message || String(err);
  renderBootstrapError(bootstrapError);
});
setInterval(tick, pollInterval);
setInterval(() => {
  const task = selectedTask();
  const turn = task ? latestTurnTiming(task.id) : null;
  if ((task && taskActivityStatus(task) !== "idle") || turn?.running) {
    renderTaskList();
    renderSelectedHeader();
    renderPanelForRealtime();
  }
}, 1000);
