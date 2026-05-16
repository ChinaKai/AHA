const queryParams = new URLSearchParams(location.search);
const rawPollInterval = Number(queryParams.get("poll") || "1000");
const rawRequestTimeoutMs = Number(queryParams.get("timeout") || "12000");
const pollInterval = Number.isFinite(rawPollInterval) ? Math.max(250, rawPollInterval) : 1000;
const requestTimeoutMs = Number.isFinite(rawRequestTimeoutMs) ? Math.max(1000, rawRequestTimeoutMs) : 12000;
let currentRunId = String(queryParams.get("run_id") || queryParams.get("run") || "").trim();
let defaultRunId = "";
let runsData = [];
let runsLoaded = false;
let runsError = "";
let sessionMenuOpen = false;
let runActionInFlight = false;
let offset = -1;
let statusData = null;
let selectedTaskId = null;
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
let eventTailInitialized = false;
const allEvents = [];
const conversationPageLimit = 50;
const logPageLimit = 200;
const conversationStates = new Map();
const expandedMessageKeys = new Set();
const finalDetails = new Map();
const contextDetails = new Map();
const logStates = new Map();
const terminalTaskStatuses = new Set(["completed", "failed", "blocked"]);
const terminalAgentStatuses = new Set(["completed", "failed", "blocked"]);
const sandboxOptions = ["workspace-write", "read-only", "danger-full-access"];
const approvalOptions = ["never", "on-failure", "on-request", "untrusted"];
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
const sessionDetailTextEl = document.getElementById("session-detail-text");
const headerWorkspaceDirEl = document.getElementById("header-workspace-dir");
const mobileTaskSummaryEl = document.getElementById("mobile-task-summary");
const mobileTaskTitleEl = document.getElementById("mobile-task-title");
const mobileTaskStatusEl = document.getElementById("mobile-task-status");
const summaryEl = document.getElementById("summary");
const taskCreateEl = document.getElementById("task-create");
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
const agentTargetEl = document.getElementById("agent-target");
const agentsEl = document.getElementById("agents");
const taskBackendEl = document.getElementById("task-backend");
const taskModelEl = document.getElementById("task-model");
const taskSandboxEl = document.getElementById("task-sandbox");
const taskApprovalEl = document.getElementById("task-approval");
const workspaceSelectEl = document.getElementById("workspace-select");
const workspaceCustomEl = document.getElementById("workspace-custom");
const selectedAgentInfoEl = document.getElementById("selected-agent-info");
const backendStatusEl = document.getElementById("backend-status");
const conversationFiltersEl = document.getElementById("conversation-filters");
const commandMenuEl = document.getElementById("command-menu");
let commandSelection = 0;
const ahaSlashCommands = [
  { scope: "aha", name: "/aha help", insert: "/aha help", desc: "Show AHA commands. Handled locally." },
  { scope: "aha", name: "/aha status", insert: "/aha status", desc: "Show selected task status. Handled locally." },
  { scope: "aha", name: "/aha agents", insert: "/aha agents", desc: "List selected task agents. Handled locally." },
  { scope: "aha", name: "/aha final", insert: "/aha final", desc: "Ask task-main to generate or update the Final." },
  { scope: "aha", name: "/aha finalize", insert: "/aha finalize", desc: "Alias for /aha final." }
];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
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

function runScopedPayload(payload = {}) {
  return currentRunId ? { ...payload, run_id: currentRunId } : payload;
}

function runIdOf(run) {
  return String(run?.id || run?.run_id || "").trim();
}

function runTitleOf(run) {
  const goal = String(run?.goal || "").trim();
  return goal || runIdOf(run) || "未命名会话";
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
    goal: statusData?.goal || "当前会话",
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
  selectedTaskId = null;
  backendStatusData = null;
  offset = -1;
  eventTailInitialized = false;
  conversationAutoFollow = true;
  allEvents.length = 0;
  conversationStates.clear();
  expandedMessageKeys.clear();
  finalDetails.clear();
  contextDetails.clear();
  logStates.clear();
  panelEl.innerHTML = '<div class="empty">正在切换会话...</div>';
}

function renderSessionSummary() {
  const run = currentRunSummary() || fallbackCurrentRun();
  const runId = currentRunId || runIdOf(run);
  if (!run && !statusData) {
    if (sessionTitleEl) sessionTitleEl.textContent = currentRunId || "加载中...";
    if (runIdEl) runIdEl.textContent = currentRunId || "-";
    if (runStateEl) runStateEl.textContent = "";
    if (sessionDetailTextEl) sessionDetailTextEl.textContent = "会话详情";
    return;
  }
  const title = statusData?.goal || runTitleOf(run);
  if (sessionTitleEl) {
    sessionTitleEl.textContent = title || "未选择会话";
    sessionTitleEl.title = title || "";
  }
  if (sessionToggleEl) {
    const label = runId ? `会话历史: ${title || runId}` : "会话历史";
    sessionToggleEl.title = label;
    sessionToggleEl.setAttribute("aria-label", label);
  }
  if (runIdEl) {
    runIdEl.textContent = runId || "-";
    runIdEl.title = runId || "";
  }
  const mode = statusData?.mode || run?.mode || "-";
  const updatedAt = statusData?.updated_at || runUpdatedAtOf(run);
  const runStateText = `${mode} | updated ${formatLocalTimestamp(updatedAt, updatedAt || "-")}`;
  if (runStateEl) {
    runStateEl.textContent = runStateText;
    runStateEl.dataset.mobileLabel = mode || "run";
    runStateEl.title = runStateText;
  }
  if (sessionDetailTextEl) {
    const taskCount = Number.isFinite(run?.task_count) ? `${run.completed_count || 0}/${run.task_count} 个任务` : "";
    sessionDetailTextEl.textContent = [
      run?.status ? `状态 ${run.status}` : "",
      taskCount,
      updatedAt ? `更新 ${formatLocalTimestamp(updatedAt, updatedAt)}` : "",
      runsError ? `提示 ${runsError}` : ""
    ].filter(Boolean).join(" · ") || "会话详情";
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
  renderSessionSummary();
}

async function loadRuns(force = false) {
  if (runsLoaded && !force) {
    renderSessionMenu();
    return;
  }
  try {
    const payload = await fetchJson("/api/runs", {}, "Failed to load sessions");
    defaultRunId = String(payload.default_run_id || defaultRunId || "").trim();
    runsData = Array.isArray(payload.runs) ? payload.runs : [];
    runsError = "";
    const preferred = currentRunId || defaultRunId || runIdOf(runsData[0]) || String(statusData?.run_id || "").trim();
    if (preferred && preferred !== currentRunId) {
      currentRunId = preferred;
      syncRunUrl();
    }
  } catch (err) {
    runsError = err?.message || String(err || "会话列表不可用");
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
}

async function refreshRunScopedView() {
  await loadStatus({ forceAgents: true });
  await ensureConversationLoaded();
  await loadBackendStatus();
  await pollEvents();
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

async function createRun(goal) {
  const trimmedGoal = String(goal || "").trim();
  if (!trimmedGoal) return;
  runActionInFlight = true;
  try {
    const payload = await fetchJson("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal: trimmedGoal })
    }, "Failed to create session");
    const run = payload.run || payload;
    const nextRunId = runIdOf(run);
    if (!nextRunId) throw new Error("New session did not include an id");
    if (newRunGoalEl) newRunGoalEl.value = "";
    runsLoaded = false;
    await loadRuns(true);
    await switchRun(nextRunId);
  } catch (err) {
    alert(err?.message || String(err));
  } finally {
    runActionInFlight = false;
    renderSessionMenu();
  }
}

function initTaskCreateDisclosure() {
  if (!taskCreateEl) return;
  const mobileQuery = window.matchMedia("(max-width: 640px)");
  let applyingDefault = false;
  const applyDefault = () => {
    if (taskCreateEl.dataset.userToggled === "true") return;
    applyingDefault = true;
    taskCreateEl.open = !mobileQuery.matches;
    setTimeout(() => {
      applyingDefault = false;
    }, 50);
  };
  taskCreateEl.addEventListener("toggle", () => {
    if (!applyingDefault) taskCreateEl.dataset.userToggled = "true";
  });
  if (mobileQuery.addEventListener) {
    mobileQuery.addEventListener("change", applyDefault);
  } else {
    mobileQuery.addListener(applyDefault);
  }
  applyDefault();
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
    taskCreateEl.open = true;
    setMobileSheet("tasks");
    setTimeout(() => document.getElementById("new-task-title")?.focus(), 0);
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
  runCreateFormEl?.addEventListener("submit", async event => {
    event.preventDefault();
    await createRun(newRunGoalEl?.value || "");
  });
  document.addEventListener("click", event => {
    const target = event.target instanceof Element ? event.target : null;
    if (sessionMenuOpen && !sessionControlEl?.contains(target)) setSessionMenu(false);
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") setSessionMenu(false);
  });
  renderSessionMenu();
}

function selectedTask() {
  return (statusData?.tasks || []).find(task => task.id === selectedTaskId) || null;
}

function selectedAgent() {
  const task = selectedTask();
  return (task?.agents || []).find(item => item.id === agentTargetEl.value) || null;
}

function backendTarget() {
  return agentTargetEl.value || "main";
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

function eventData(event) {
  return event.data || {};
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
  "task_waiting_for_subagents",
  "task_status_changed",
  "agent_started",
  "agent_status_changed",
  "agent_thread",
  "agent_command_started",
  "agent_command_finished",
  "agent_message",
  "agent_usage",
  "agent_error",
  "agent_delegated",
  "agent_message_routed",
  "sub_agent_reported",
  "sub_agent_report_ignored",
  "sub_agent_backend_recovered",
  "sub_agent_backend_failed",
  "agent_created",
  "agent_config_updated",
  "agent_finished",
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
  if (!refs.size && (event.type.startsWith("agent_") || event.type.startsWith("task_") || event.type === "workspace_missing")) {
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

function agentTimelineEvents(taskId) {
  return taskTimelineEvents(taskId).filter(eventMatchesSelectedAgent);
}

function conversationKey(taskId = selectedTaskId, target = backendTarget()) {
  return `${taskId || ""}::${target || "main"}`;
}

function parseConversationKey(key) {
  const index = key.indexOf("::");
  return index < 0 ? { taskId: key, target: "main" } : { taskId: key.slice(0, index), target: key.slice(index + 2) || "main" };
}

function eventIdentity(event) {
  return `${event.ts || ""}|${event.type || ""}|${JSON.stringify(eventData(event))}`;
}

function mergeConversationEvents(current, incoming, prepend = false) {
  const merged = prepend ? [...incoming, ...current] : [...current, ...incoming];
  const seen = new Set();
  return merged.filter(event => {
    const id = eventIdentity(event);
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

function conversationState(taskId = selectedTaskId, target = backendTarget()) {
  const key = conversationKey(taskId, target);
  if (!conversationStates.has(key)) {
    conversationStates.set(key, { events: [], beforeOffset: null, hasMore: true, initialized: false, loading: false, error: "" });
  }
  return conversationStates.get(key);
}

function conversationSourceEvents(taskId) {
  const state = conversationStates.get(conversationKey(taskId));
  return state?.initialized ? state.events : agentTimelineEvents(taskId);
}

function dedupedConversationEvents(taskId) {
  let latestAgentMessage = "";
  return conversationSourceEvents(taskId).filter(event => {
    const data = eventData(event);
    if (event.type === "agent_message") {
      latestAgentMessage = String(data.text || "").trim();
      return true;
    }
    if (
      event.type === "message" &&
      data.sender === "main" &&
      (data.to_agent || data.target) === "browser" &&
      latestAgentMessage &&
      String(data.message || "").trim() === latestAgentMessage
    ) {
      return false;
    }
    return true;
  });
}

function conversationEventCategory(event) {
  const data = eventData(event);
  if (event.type === "agent_message") return "chat";
  if (event.type === "agent_usage") return "usage";
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
    const waiting = waitingSubagentTiming(task);
    const status = finalCompletedAt
      ? "completed"
      : waiting?.running || agentLifecycleStatus(agent) === "waiting"
        ? "waiting"
        : agentLifecycleStatus(agent) || "running";
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

async function loadBackends() {
  const payload = await fetchJson("/api/backends", {}, "Failed to load backends");
  backendModels = new Map();
  backendCommands = new Map();
  taskBackendEl.innerHTML = "";
  for (const backend of payload.backends) {
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
  workspaceSelectEl.innerHTML = "";
  for (const workspace of payload.workspaces || []) {
    const opt = document.createElement("option");
    opt.value = workspace.path;
    opt.textContent = workspace.label || workspace.name;
    workspaceSelectEl.appendChild(opt);
  }
  const custom = document.createElement("option");
  custom.value = "__custom__";
  custom.textContent = "Custom path...";
  workspaceSelectEl.appendChild(custom);

  const preferred = (payload.workspaces || []).find(item => item.name === "fw_omni_builder") || (payload.workspaces || [])[0];
  if (preferred) workspaceSelectEl.value = preferred.path;
  workspaceCustomEl.classList.toggle("hidden", workspaceSelectEl.value !== "__custom__");
}

async function loadStatus(options = {}) {
  statusData = await fetchJson(apiUrl("/api/status"), {}, "Failed to load status");
  if (statusData.run_id && statusData.run_id !== currentRunId) {
    currentRunId = String(statusData.run_id);
    syncRunUrl();
  }
  renderSessionSummary();
  summaryEl.textContent = statusData.goal;
  const tasks = visibleTasks();
  if (!selectedTaskId || !tasks.some(task => task.id === selectedTaskId)) selectedTaskId = defaultTaskId(tasks);
  renderTaskList();
  renderSelectedHeader();
  if (options.forceAgents || !isAgentsPanelEditing()) {
    renderAgents();
  } else {
    renderSelectedAgentInfo();
  }
}

async function loadBackendStatus() {
  const params = new URLSearchParams({ target: backendTarget() });
  if (selectedTaskId) params.set("task_id", selectedTaskId);
  backendStatusData = await fetchJson(apiUrl("/api/backend", params), {}, "Failed to load backend status");
  renderBackendStatus();
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
    await loadFinalDetail(selectedTaskId);
  } else {
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

async function loadConversationPage(taskId = selectedTaskId, target = backendTarget(), older = false) {
  if (!taskId) return null;
  const state = conversationState(taskId, target);
  if (state.loading || (!older && state.initialized) || (older && !state.hasMore)) return state;
  state.loading = true;
  try {
    const params = new URLSearchParams({
      task_id: taskId,
      target,
      limit: String(conversationPageLimit)
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
    const events = assignConversationKeys(payload.events || [], payload.before_offset || 0);
    state.events = older ? mergeConversationEvents(state.events, events, true) : mergeConversationEvents(events, state.events, false);
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
  const payload = await fetchJson(apiUrl("/api/events", { offset: "-1" }), {}, "Failed to initialize event stream");
  if (Number.isFinite(payload.offset)) offset = payload.offset;
  eventTailInitialized = true;
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
    const matching = events.filter(event => isTaskEvent(event, taskId) && isTimelineEvent(event) && eventMatchesAgent(event, target));
    if (matching.length) state.events = mergeConversationEvents(state.events, matching, false);
  }
}

async function pollEvents() {
  let res;
  try {
    res = await fetchWithTimeout(apiUrl("/api/events", { offset: String(offset) }));
  } catch (err) {
    if (offset < 0) {
      await initializeEventTailOffset();
      return;
    }
    throw err;
  }
  if (!res.ok) {
    if (offset < 0) await initializeEventTailOffset();
    return;
  }
  const payload = await readJsonResponse(res, "Failed to poll events");
  const startOffset = offset;
  offset = payload.offset;
  const events = payload.events || [];
  events.forEach((event, index) => {
    if (!event._uiKey) event._uiKey = `event-${startOffset}-${index}`;
  });
  allEvents.push(...events);
  appendRealtimeConversationEvents(events);
}

function renderTaskList() {
  tasksEl.innerHTML = "";
  const tasks = visibleTasks();
  if (!tasks.length) {
    tasksEl.innerHTML = '<div class="empty compact">No visible tasks.</div>';
    return;
  }
  for (const task of tasks) {
    const item = document.createElement("div");
    item.className = `task ${task.id === selectedTaskId ? "active" : ""} ${task.hidden ? "hidden-task" : ""}`;
    item.dataset.taskId = task.id;
    item.innerHTML = `
      <div class="task-row">
        <strong>${escapeHtml(task.id)}</strong>
        <span class="task-statuses">${taskStatusBadges(task)}</span>
      </div>
      <div class="task-title">${escapeHtml(task.title)}</div>
      <div class="meta truncate">${escapeHtml((task.agents || []).length)} agent(s) | ${escapeHtml(task.preferred_backend || "-")} | ${escapeHtml(pathName(task.workspace_path))}${taskTimingLabel(task.id, task) ? ` | ${escapeHtml(taskTimingLabel(task.id, task))}` : ""}</div>
      <div class="task-actions">
        <button class="task-action" type="button" data-action="${task.hidden ? "restore" : "hide"}">${task.hidden ? "Restore" : "Hide"}</button>
        <button class="task-action danger" type="button" data-action="delete">Delete</button>
      </div>
    `;
    item.title = `${task.title}\nbackend=${task.preferred_backend || "-"}\nworkspace=${task.workspace_path || "-"}`;
    item.addEventListener("click", async event => {
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest("button")) return;
      await selectTask(task.id);
    });
    tasksEl.appendChild(item);
  }
}

async function selectTask(taskId) {
  selectedTaskId = taskId;
  conversationAutoFollow = true;
  if (activeTab === "logs") logState(taskId).autoFollow = true;
  closeMobileSheets();
  closeMobileActionPanel();
  renderTaskList();
  renderSelectedHeader();
  renderAgents();
  await loadBackendStatus();
  await ensureActiveTabData();
  renderPanel();
}

async function updateTaskVisibility(taskId, action) {
  if (action === "delete" && !confirm(`Delete ${taskId} from the task list?`)) return;
  taskActionInFlight = true;
  try {
    const res = await fetchWithTimeout(apiUrl(`/api/task/${encodeURIComponent(taskId)}/${action}`), { method: "POST" });
    if (!res.ok) {
      const payload = await res.json().catch(() => ({}));
      alert(payload.error || `Task action failed: ${action}`);
      return;
    }
    if (action === "restore") selectedTaskId = taskId;
    if (action === "hide" || action === "delete") selectedTaskId = null;
    await loadStatus();
    renderPanel();
  } finally {
    taskActionInFlight = false;
  }
}

function renderSelectedHeader() {
  const task = selectedTask();
  if (!task) {
    renderHeaderWorkspace(null);
    renderMobileTaskSummary(null);
    selectedIdEl.textContent = "";
    selectedTitleEl.textContent = "No tasks";
    selectedTaskMetaEl.textContent = "";
    selectedStatusEl.textContent = "empty";
    selectedStatusEl.className = "status pending";
    return;
  }
  renderHeaderWorkspace(task);
  renderMobileTaskSummary(task);
  selectedIdEl.textContent = task.id;
  selectedTitleEl.textContent = task.title;
  const timing = taskMetaTiming(task.id, task);
  const outcome = taskOutcomeStatus(task) || "-";
  const activity = taskActivityStatus(task);
  selectedTaskMetaEl.textContent =
    `outcome=${outcome} | activity=${activity} | ${task.preferred_backend || "backend?"} | ${task.preferred_model || "default"} | sandbox=${task.preferred_sandbox || "process default"} | approval=${task.preferred_approval || "process default"}${timing ? ` | ${timing}` : ""} | ${task.workspace_path || "workspace not set"}`;
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
    return;
  }
  for (const agent of task.agents || []) {
    const sandbox = agent.sandbox || task.preferred_sandbox || "workspace-write";
    const approval = agent.approval || task.preferred_approval || "never";
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
    const opt = document.createElement("option");
    opt.value = agent.id;
    opt.textContent = `${agent.id} (${agent.backend})`;
    agentTargetEl.appendChild(opt);
    const card = document.createElement("div");
    card.className = `agent-card ${agent.id === previous ? "active" : ""}`;
    card.title = [
      `${agent.id} ${agent.role}`,
      `backend=${agent.backend}`,
      `model=${agent.model || "default"}`,
      `sandbox=${sandbox}`,
      `approval=${approval}`,
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
      <div class="meta truncate">status=${escapeHtml(lifecycleTimingText || lifecycleStatus)} | ${escapeHtml(agent.role)} | ${escapeHtml(agent.backend)} | ${escapeHtml(agent.model || "default")}</div>
      <div class="meta truncate">sandbox=${escapeHtml(sandbox)} | approval=${escapeHtml(approval)}</div>
      <div class="meta truncate">process=${escapeHtml(rawProcessStatus)} | session=${escapeHtml(agent.backend_session_id || "-")}</div>
      <div class="agent-permissions">
        <select data-agent-field="sandbox" data-agent-id="${escapeHtml(agent.id)}">${selectOptions(sandboxOptions, sandbox)}</select>
        <select data-agent-field="approval" data-agent-id="${escapeHtml(agent.id)}">${selectOptions(approvalOptions, approval)}</select>
      </div>
    `;
    card.addEventListener("click", event => {
      const clicked = event.target instanceof Element ? event.target : null;
      if (clicked?.closest("select")) return;
      agentTargetEl.value = agent.id;
      agentTargetEl.dispatchEvent(new Event("change"));
      closeMobileSheets();
    });
    card.addEventListener("change", event => {
      const target = event.target instanceof HTMLSelectElement ? event.target : null;
      if (!target?.dataset.agentField) return;
      updateAgentConfig(agent.id, target.dataset.agentField, target.value);
    });
    agentsEl.appendChild(card);
  }
  if ([...agentTargetEl.options].some(item => item.value === previous)) agentTargetEl.value = previous;
  [...agentsEl.querySelectorAll(".agent-card")].forEach((card, index) => {
    const agent = (task.agents || [])[index];
    card.classList.toggle("active", agent?.id === agentTargetEl.value);
  });
  renderSelectedAgentInfo();
}

function syncAgentCards() {
  const task = selectedTask();
  [...agentsEl.querySelectorAll(".agent-card")].forEach((card, index) => {
    const agent = (task?.agents || [])[index];
    card.classList.toggle("active", agent?.id === agentTargetEl.value);
  });
}

function renderSelectedAgentInfo() {
  const task = selectedTask();
  const agent = (task?.agents || []).find(item => item.id === agentTargetEl.value);
  if (!task || !agent) {
    selectedAgentInfoEl.textContent = "";
    return;
  }
  const lifecycleTiming = agentStatusTimingText(agent) || agentLifecycleStatus(agent);
  selectedAgentInfoEl.textContent =
    `To ${agent.id} | status=${lifecycleTiming} | process=${agent.backend_process_status || "stopped"} | pid=${agent.backend_process_pid || "-"} | role=${agent.role} | backend=${agent.backend} | model=${agent.model || "default"} | sandbox=${agent.sandbox || task.preferred_sandbox || "process default"} | approval=${agent.approval || task.preferred_approval || "process default"} | session=${agent.backend_session_id || "-"} | scope=${agent.session_scope || "-"} | workspace=${agent.workspace_path || task.workspace_path || "-"}`;
}

function renderBackendStatus() {
  if (!backendStatusEl) return;
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
    state.backend || "codex-chat",
    state.pid ? `pid=${state.pid}` : "pid=-",
    state.last_reply_at ? `last reply ${formatLocalTimestamp(state.last_reply_at, state.last_reply_at)}` : ""
  ].filter(Boolean).join(" | ");
  backendStatusEl.className = `backend-status ${escapeHtml(status)}`;
  backendStatusEl.innerHTML = `
    <span class="activity-dot"></span>
    <strong>${escapeHtml(status)}</strong>
    <code title="${escapeHtml(detail)}">${escapeHtml(detail)}</code>
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
    alert(body.error || "Failed to update agent permission");
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
  return workspaceSelectEl.value === "__custom__" ? workspaceCustomEl.value.trim() : workspaceSelectEl.value;
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
  const state = conversationState(taskId);
  if (!state.initialized || state.loading && !state.events.length) {
    return `<div class="empty">Loading conversation...</div>`;
  }
  if (state.error && !state.events.length) {
    return `<div class="empty">Conversation unavailable. Realtime updates will start from the latest event offset.<br><code>${escapeHtml(state.error)}</code></div>`;
  }
  const events = taskConversationEvents(taskId);
  if (!events.length && !state.hasMore) return `<div class="empty">No conversation for ${escapeHtml(backendTarget())} yet.</div>`;
  const older = state.hasMore ? `<button class="load-older" type="button" data-load-older="true">${state.loading ? "Loading..." : "Load older"}</button>` : "";
  const taskTimer = renderTaskTimer(taskId);
  const timer = renderTurnTimer(taskId);
  return `<div class="conversation timeline">${older}${taskTimer}${events.map(renderTimelineEvent).join("")}${timer}</div>`;
}

function renderTimelineEvent(event) {
  const data = eventData(event);
  if (event.type === "message") {
    const cls = data.sender === "browser" ? "from-browser" : data.sender === "main" ? "from-main" : data.sender === "system" ? "from-system" : "";
    return renderTimelineCard(
      `${data.sender || "-"} -> ${data.to_agent || data.role || data.target || "-"}`,
      data.message || "",
      eventTimeLabel(event),
      cls,
      event._uiKey
    );
  }
  if (event.type === "agent_message") return renderTimelineCard(`agent update (${data.target || "main"})`, data.text || "", eventTimeLabel(event), "agent-update", event._uiKey);
  if (event.type === "agent_command_started") return renderTimelineCard(`running command (${data.target || "main"})`, data.command || "", eventTimeLabel(event), "agent-command", event._uiKey);
  if (event.type === "agent_command_finished") {
    const output = data.output_tail ? `\n\nOutput tail:\n${data.output_tail}` : "";
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
  const ts = eventTimeLabel(event);
  if (event.type === "task_status_changed") return renderTimelineStatus(`task ${data.status}`, `exit=${data.exit_code ?? "-"}`, data.status, ts);
  if (event.type === "task_started") return renderTimelineStatus("task started", data.title || "", "running", ts);
  if (event.type === "task_finished") return renderTimelineStatus(`task ${data.status || "finished"}`, `exit=${data.exit_code ?? "-"}`, data.status || "completed", ts);
  if (event.type === "task_result_written") return renderTimelineStatus("final written", `${data.chars || 0} chars`, "completed", ts);
  if (event.type === "task_final_requested") return renderTimelineStatus("final requested", `target=${data.target || "main"}`, "running", ts);
  if (event.type === "task_waiting_for_subagents") return renderTimelineStatus("waiting for sub-agents", `pending=${(data.pending || []).join(", ") || "-"}`, "running", ts);
  if (event.type === "agent_started") return renderTimelineStatus("agent started", `${data.target || "main"} from ${data.sender || "-"} sandbox=${data.sandbox || "-"} approval=${data.approval || "-"}`, "running", ts);
  if (event.type === "agent_status_changed") return renderTimelineStatus("agent status", `${data.agent_id || "-"} ${data.status || "-"}`, data.status || "session", ts);
  if (event.type === "agent_config_updated") return renderTimelineStatus("agent permission updated", `${data.agent_id || "-"} sandbox=${data.sandbox || "-"} approval=${data.approval || "-"}`, "session", ts);
  if (event.type === "agent_thread") return renderTimelineStatus("codex session", data.thread_id || "-", "session", ts);
  if (event.type === "agent_finished") return renderTimelineStatus("agent finished", `exit=${data.exit_code ?? "-"}`, data.exit_code === 0 ? "completed" : "failed", ts);
  if (event.type === "task_dispatched") return renderTimelineStatus("task dispatched", `target=${data.target || "-"}`, "session", ts);
  if (event.type === "agent_created") return renderTimelineStatus("sub-agent created", `${data.agent_id || "-"} backend=${data.backend || "-"}`, "session", ts);
  if (event.type === "agent_delegated") return renderTimelineStatus("delegated", `${data.count || 0} action(s)`, "session", ts);
  if (event.type === "agent_message_routed") return renderTimelineStatus("routed to agent", `${data.target || "-"} ${data.reason || ""}`, "running", ts);
  if (event.type === "sub_agent_reported") return renderTimelineStatus("sub-agent reported", `${data.agent_id || "-"} ${data.status || "-"}`, data.status || "session", ts);
  if (event.type === "sub_agent_report_ignored") return renderTimelineStatus("sub-agent report ignored", `${data.agent_id || "-"} ${data.reason || ""}`, "session", ts);
  if (event.type === "sub_agent_backend_recovered") return renderTimelineStatus("sub-agent backend recovered", `${data.agent_id || "-"} attempt=${data.attempt || "-"}`, "running", ts);
  if (event.type === "sub_agent_backend_failed") return renderTimelineStatus("sub-agent backend failed", `${data.agent_id || "-"} attempts=${data.attempts || "-"}`, "failed", ts);
  if (event.type === "workspace_missing") return renderTimelineStatus("workspace missing", data.workspace_path || "-", "blocked", ts);
  return renderTimelineStatus(event.type, JSON.stringify(data), "session", ts);
}

function renderTurnTimer(taskId) {
  const timing = latestTurnTiming(taskId);
  if (!timing) return "";
  const title = timing.running
    ? (timing.status === "waiting" ? "Agent is waiting" : "Agent is working")
    : `Agent turn ${timing.status}`;
  const label = timing.running ? "elapsed" : "duration";
  const details = [
    `${label} ${formatDuration(timing.elapsedMs)}`,
    `status ${timing.status}`,
    `target ${timing.target}`,
    `started ${formatClock(timing.startedAt)}`,
    timing.finishedAt ? `finished ${formatClock(timing.finishedAt)}` : ""
  ].filter(Boolean).join(" | ");
  return `
    <div class="turn-timer ${escapeHtml(timing.status)}">
      <span class="activity-dot"></span>
      <strong>${escapeHtml(title)}</strong>
      <code>${escapeHtml(details)}</code>
    </div>
  `;
}

function renderTaskTimer(taskId) {
  const task = (statusData?.tasks || []).find(item => item.id === taskId);
  const timing = taskTiming(taskId, task);
  if (!timing) return "";
  const waiting = waitingSubagentTiming(task);
  const displayStatus = taskDisplayStatus(task);
  const outcome = taskOutcomeStatus(task);
  const activity = taskActivityStatus(task);
  const title = timing.running && outcome ? `Task activity ${activity}` : (timing.running ? "Task is running" : `Task ${displayStatus || "finished"}`);
  const details = [
    `wall ${formatDuration(timing.elapsedMs)}`,
    outcome ? `outcome ${outcome}` : "",
    `activity ${activity}`,
    `started ${formatClock(timing.startedAt)}`,
    timing.finishedAt ? `finished ${formatClock(timing.finishedAt)}` : "",
    waiting ? `waiting subagents ${formatDuration(waiting.elapsedMs)} (${waiting.completed.length}/${subAgents(task).length})` : ""
  ].filter(Boolean).join(" | ");
  return `
    <div class="turn-timer task-timer ${escapeHtml(timing.running ? activity : displayStatus || "completed")}">
      <span class="activity-dot"></span>
      <strong>${escapeHtml(title)}</strong>
      <code>${escapeHtml(details)}</code>
    </div>
  `;
}

function renderTimelineCard(title, body, ts, cls, key = "") {
  return `
    <div class="message ${cls}">
      <div class="message-head">
        <span>${escapeHtml(title)}</span>
        <span>${escapeHtml(ts || "")}</span>
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

function renderPanel(options = {}) {
  renderConversationFilters();
  const task = selectedTask();
  if (!task) {
    panelEl.innerHTML = '<div class="empty">No task selected.</div>';
    return;
  }
  if (activeTab === "conversation") {
    const previousTop = options.previousTop ?? panelEl.scrollTop;
    const previousHeight = options.previousHeight ?? panelEl.scrollHeight;
    const shouldFollow = conversationAutoFollow || isPanelNearBottom();
    panelEl.innerHTML = renderConversation(task.id);
    if (options.preserveScroll) {
      panelEl.scrollTop = panelEl.scrollHeight - previousHeight + previousTop;
    } else {
      panelEl.scrollTop = shouldFollow ? panelEl.scrollHeight : previousTop;
    }
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

document.getElementById("task-form").addEventListener("submit", async event => {
  event.preventDefault();
  const title = document.getElementById("new-task-title").value.trim();
  if (!title) return;
  try {
    await fetchJson(apiUrl("/api/tasks"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(runScopedPayload({
        title,
        backend: taskBackendEl.value,
        model: taskModelEl.value || null,
        sandbox: taskSandboxEl.value,
        approval: taskApprovalEl.value,
        workspace_path: selectedWorkspacePath(),
        delegation_policy: document.getElementById("delegation-policy").value,
        max_sub_agents: Number(document.getElementById("max-sub-agents").value || "0"),
        preferred_sub_backend: taskBackendEl.value,
        dispatch: true
      }))
    }, "Failed to create task");
    document.getElementById("new-task-title").value = "";
    await loadStatus();
    closeMobileSheets();
  } catch (err) {
    alert(err.message || String(err));
  }
});

sendFormEl.addEventListener("submit", async event => {
  event.preventDefault();
  const task = selectedTask();
  const message = messageEl.value.trim();
  if (!task || !message) return;
  const agentId = agentTargetEl.value || "main";
  const target = agentId === "main" ? "main" : agentId;
  try {
    await fetchJson(apiUrl("/api/send"), {
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
    messageEl.value = "";
    syncMobileComposerAction();
    commandMenuEl.classList.add("hidden");
    closeMobileActionPanel();
    await pollEvents();
    conversationAutoFollow = true;
    renderPanel();
  } catch (err) {
    alert(err.message || String(err));
  }
});

messageEl.addEventListener("input", () => {
  commandSelection = 0;
  syncMobileComposerAction();
  renderCommandMenu();
});
messageEl.addEventListener("focus", renderCommandMenu);
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

conversationFiltersEl.addEventListener("change", event => {
  const input = event.target instanceof HTMLInputElement ? event.target : null;
  const key = input?.dataset.conversationFilter;
  if (!key || !(key in conversationFilters)) return;
  conversationFilters[key] = input.checked;
  conversationAutoFollow = true;
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
  if (activeTab === "conversation") {
    conversationAutoFollow = isPanelNearBottom();
    if (panelEl.scrollTop < 48) loadOlderConversation();
  } else if (activeTab === "logs") {
    if (selectedTaskId) logState(selectedTaskId).autoFollow = isPanelNearBottom();
    if (panelEl.scrollTop < 48) loadOlderLogs();
  }
});
panelEl.addEventListener("click", event => {
  const button = event.target instanceof Element ? event.target.closest("[data-load-older]") : null;
  if (button) loadOlderConversation();
  const logButton = event.target instanceof Element ? event.target.closest("[data-load-older-log]") : null;
  if (logButton) loadOlderLogs();
});
panelEl.addEventListener("toggle", event => {
  const details = event.target instanceof HTMLDetailsElement ? event.target : null;
  const key = details?.dataset.messageKey;
  if (!key) return;
  if (details.open) {
    expandedMessageKeys.add(key);
  } else {
    expandedMessageKeys.delete(key);
  }
}, true);

agentsEl.addEventListener("pointerdown", () => markAgentsPanelEditing());
agentsEl.addEventListener("focusin", () => markAgentsPanelEditing());
agentsEl.addEventListener("change", () => markAgentsPanelEditing(1500));
agentTargetEl.addEventListener("change", async () => {
  syncAgentCards();
  renderSelectedAgentInfo();
  await loadBackendStatus();
  conversationAutoFollow = true;
  renderConversationFilters();
  await ensureConversationLoaded();
  renderPanel();
});
taskBackendEl.addEventListener("change", renderModelOptions);
showHiddenEl.addEventListener("change", () => {
  const tasks = visibleTasks();
  if (!tasks.some(task => task.id === selectedTaskId)) selectedTaskId = defaultTaskId(tasks);
  renderTaskList();
  renderSelectedHeader();
  renderAgents();
  renderConversationFilters();
  renderPanel();
});
workspaceSelectEl.addEventListener("change", () => {
  const isCustom = workspaceSelectEl.value === "__custom__";
  workspaceCustomEl.classList.toggle("hidden", !isCustom);
  if (isCustom) workspaceCustomEl.focus();
});

initTaskCreateDisclosure();
initDesktopSidebars();
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
  tickInFlight = true;
  try {
    await loadStatus();
    await ensureConversationLoaded();
    await loadBackendStatus();
    await pollEvents();
    tickFailureCount = 0;
    tickBackoffUntil = 0;
    renderPanel();
  } catch (err) {
    recordTickFailure();
    panelEl.innerHTML = `<pre>${escapeHtml(String(err))}</pre>`;
  } finally {
    tickInFlight = false;
  }
}

Promise.all([loadBackends(), loadWorkspaces()]).then(async () => {
  await loadRuns().catch(() => {});
  await tick();
}).catch(err => {
  panelEl.innerHTML = `<pre>${escapeHtml(String(err))}</pre>`;
});
setInterval(tick, pollInterval);
setInterval(() => {
  const task = selectedTask();
  const turn = task ? latestTurnTiming(task.id) : null;
  if ((task && taskActivityStatus(task) !== "idle") || turn?.running) {
    renderTaskList();
    renderSelectedHeader();
    renderPanel();
  }
}, 1000);
