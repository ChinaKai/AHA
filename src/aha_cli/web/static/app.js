const pollInterval = Number(new URLSearchParams(location.search).get("poll") || "1000");
let offset = 0;
let statusData = null;
let selectedTaskId = null;
let activeTab = "conversation";
let backendModels = new Map();
let backendCommands = new Map();
let taskActionInFlight = false;
let conversationAutoFollow = true;
let agentsPanelEditingUntil = 0;
const allEvents = [];
const taskDetails = new Map();
const terminalTaskStatuses = new Set(["completed", "failed", "blocked"]);
const sandboxOptions = ["workspace-write", "read-only", "danger-full-access"];
const approvalOptions = ["never", "on-failure", "on-request", "untrusted"];

const runIdEl = document.getElementById("run-id");
const runStateEl = document.getElementById("run-state");
const summaryEl = document.getElementById("summary");
const tasksEl = document.getElementById("tasks");
const showHiddenEl = document.getElementById("show-hidden");
const selectedIdEl = document.getElementById("selected-id");
const selectedTitleEl = document.getElementById("selected-title");
const selectedStatusEl = document.getElementById("selected-status");
const selectedTaskMetaEl = document.getElementById("selected-task-meta");
const panelEl = document.getElementById("panel");
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
const liveActivityEl = document.getElementById("live-activity");
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

function selectedTask() {
  return (statusData?.tasks || []).find(task => task.id === selectedTaskId) || null;
}

function selectedAgent() {
  const task = selectedTask();
  return (task?.agents || []).find(item => item.id === agentTargetEl.value) || null;
}

function visibleTasks() {
  const tasks = statusData?.tasks || [];
  return showHiddenEl.checked ? tasks : tasks.filter(task => !task.hidden);
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

function taskConversation(taskId) {
  return allEvents.filter(event => event.type === "message" && isTaskEvent(event, taskId));
}

function taskEvents(taskId) {
  return allEvents.filter(event => isTaskEvent(event, taskId));
}

function taskTimelineEvents(taskId) {
  const visibleTypes = new Set([
    "message",
    "task_dispatched",
    "task_started",
    "task_finished",
    "task_result_written",
    "task_final_requested",
    "task_status_changed",
    "agent_started",
    "agent_thread",
    "agent_command_started",
    "agent_command_finished",
    "agent_message",
    "agent_usage",
    "agent_error",
    "agent_delegated",
    "agent_created",
    "agent_config_updated",
    "agent_finished",
    "workspace_missing"
  ]);
  return taskEvents(taskId).filter(event => visibleTypes.has(event.type));
}

function taskConversationEvents(taskId) {
  const hiddenTypes = new Set(["agent_message", "agent_usage", "agent_thread"]);
  return taskTimelineEvents(taskId).filter(event => !hiddenTypes.has(event.type));
}

function parseTimestamp(value) {
  if (!value) return null;
  const millis = Date.parse(value);
  return Number.isNaN(millis) ? null : millis;
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
  const terminalStatus = terminalTaskStatuses.has(task?.status || "");
  const finishedAt = parseTimestamp(task?.finished_at) || lastMatchingTime(event => {
    const data = eventData(event);
    return (
      event.type === "task_finished" ||
      event.type === "agent_finished" ||
      (event.type === "task_status_changed" && terminalTaskStatuses.has(data.status || ""))
    );
  });
  if (!startedAt) return null;
  const running = task?.status === "running";
  const endAt = running ? Date.now() : finishedAt || (terminalStatus ? lastMatchingTime(() => true) : null);
  if (!endAt) return { startedAt, finishedAt: null, elapsedMs: Date.now() - startedAt, running };
  return { startedAt, finishedAt: running ? null : endAt, elapsedMs: endAt - startedAt, running };
}

function latestTurnTiming(taskId) {
  const events = taskEvents(taskId);
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
  let finishedEvent = null;
  for (let index = startIndex + 1; index < events.length; index += 1) {
    if (events[index].type === "agent_finished") {
      finishedEvent = events[index];
      break;
    }
  }
  const finishedAt = finishedEvent ? eventTimestamp(finishedEvent) : null;
  const running = !finishedAt;
  const endAt = running ? Date.now() : finishedAt;
  const exitCode = eventData(finishedEvent || {}).exit_code;
  return {
    startedAt,
    finishedAt,
    elapsedMs: endAt - startedAt,
    running,
    status: running ? "running" : exitCode === 0 ? "completed" : "failed",
    target: eventData(startedEvent).target || "main",
    sender: eventData(startedEvent).sender || "-"
  };
}

function formatEvent(event) {
  const data = eventData(event);
  if (event.type === "log") return `[${event.ts}] ${data.task_id || "-"}: ${data.line || ""}`;
  if (event.type === "message") {
    const task = data.task_id ? ` task=${data.task_id}` : "";
    return `[${event.ts}] message${task} ${data.sender || "main"} -> ${data.target || "-"}: ${data.message || ""}`;
  }
  return `[${event.ts}] ${event.type}: ${JSON.stringify(data)}`;
}

async function loadBackends() {
  const res = await fetch("/api/backends");
  const payload = await res.json();
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
  const res = await fetch("/api/workspaces");
  const payload = await res.json();
  workspaceSelectEl.innerHTML = "";
  for (const workspace of payload.workspaces || []) {
    const opt = document.createElement("option");
    opt.value = workspace.path;
    opt.textContent = workspace.name;
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
  const res = await fetch("/api/status");
  statusData = await res.json();
  runIdEl.textContent = statusData.run_id;
  runStateEl.textContent = `${statusData.mode} | updated ${statusData.updated_at}`;
  summaryEl.textContent = statusData.goal;
  const tasks = visibleTasks();
  if (!selectedTaskId || !tasks.some(task => task.id === selectedTaskId)) selectedTaskId = tasks[0]?.id || null;
  renderTaskList();
  renderSelectedHeader();
  if (options.forceAgents || !isAgentsPanelEditing()) {
    renderAgents();
  } else {
    renderSelectedAgentInfo();
  }
}

async function refreshTaskDetail(taskId) {
  if (!taskId) return null;
  const res = await fetch(`/api/task/${encodeURIComponent(taskId)}`);
  const detail = await res.json();
  taskDetails.set(taskId, detail);
  return detail;
}

async function pollEvents() {
  const res = await fetch(`/api/events?offset=${offset}`);
  const payload = await res.json();
  offset = payload.offset;
  allEvents.push(...payload.events);
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
        <span class="status ${escapeHtml(task.hidden ? "hidden" : task.status)}">${escapeHtml(task.hidden ? "hidden" : task.status)}</span>
      </div>
      <div class="task-title">${escapeHtml(task.title)}</div>
      <div class="meta truncate">${escapeHtml((task.agents || []).length)} agent(s) | ${escapeHtml(task.preferred_backend || "-")} | ${escapeHtml(pathName(task.workspace_path))}</div>
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
  renderTaskList();
  renderSelectedHeader();
  renderAgents();
  await refreshTaskDetail(selectedTaskId);
  renderPanel();
}

async function updateTaskVisibility(taskId, action) {
  if (action === "delete" && !confirm(`Delete ${taskId} from the task list?`)) return;
  taskActionInFlight = true;
  try {
    const res = await fetch(`/api/task/${encodeURIComponent(taskId)}/${action}`, { method: "POST" });
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
    selectedIdEl.textContent = "";
    selectedTitleEl.textContent = "No tasks";
    selectedTaskMetaEl.textContent = "";
    selectedStatusEl.textContent = "empty";
    selectedStatusEl.className = "status pending";
    return;
  }
  selectedIdEl.textContent = task.id;
  selectedTitleEl.textContent = task.title;
  selectedTaskMetaEl.textContent =
    `${task.preferred_backend || "backend?"} | ${task.preferred_model || "default"} | sandbox=${task.preferred_sandbox || "process default"} | approval=${task.preferred_approval || "process default"} | ${task.workspace_path || "workspace not set"}`;
  selectedStatusEl.textContent = task.status;
  selectedStatusEl.className = `status ${task.status}`;
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
      `session=${agent.backend_session_id || "-"}`,
      `workspace=${agent.workspace_path || task.workspace_path || "-"}`
    ].join("\n");
    card.innerHTML = `
      <strong>${escapeHtml(agent.id)}</strong>
      <div class="meta truncate">${escapeHtml(agent.role)} | ${escapeHtml(agent.backend)} | ${escapeHtml(agent.model || "default")}</div>
      <div class="meta truncate">sandbox=${escapeHtml(sandbox)} | approval=${escapeHtml(approval)}</div>
      <div class="meta truncate">session=${escapeHtml(agent.backend_session_id || "-")}</div>
      <div class="agent-permissions">
        <select data-agent-field="sandbox" data-agent-id="${escapeHtml(agent.id)}">${selectOptions(sandboxOptions, sandbox)}</select>
        <select data-agent-field="approval" data-agent-id="${escapeHtml(agent.id)}">${selectOptions(approvalOptions, approval)}</select>
      </div>
    `;
    card.addEventListener("click", event => {
      const clicked = event.target instanceof Element ? event.target : null;
      if (clicked?.closest("select")) return;
      agentTargetEl.value = agent.id;
      syncAgentCards();
      renderSelectedAgentInfo();
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
  selectedAgentInfoEl.textContent =
    `To ${agent.id} | role=${agent.role} | backend=${agent.backend} | model=${agent.model || "default"} | sandbox=${agent.sandbox || task.preferred_sandbox || "process default"} | approval=${agent.approval || task.preferred_approval || "process default"} | session=${agent.backend_session_id || "-"} | scope=${agent.session_scope || "-"} | workspace=${agent.workspace_path || task.workspace_path || "-"}`;
}

async function updateAgentConfig(agentId, field, value) {
  const task = selectedTask();
  if (!task || !agentId || !field) return;
  const payload = { task_id: task.id, agent_id: agentId };
  payload[field] = value;
  const res = await fetch("/api/agent-config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.error || "Failed to update agent permission");
    return;
  }
  await loadStatus({ forceAgents: true });
  await refreshTaskDetail(task.id);
  renderPanel();
}

function compactText(value, limit = 180) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}...` : text;
}

function activitySummary(event, task) {
  const data = eventData(event);
  if (event.type === "agent_command_started") {
    return { title: "running command", body: compactText(data.command), state: "running" };
  }
  if (event.type === "agent_command_finished") {
    const state = data.exit_code === 0 ? "completed" : "failed";
    return { title: `command finished exit=${data.exit_code ?? "-"}`, body: compactText(data.command), state };
  }
  if (event.type === "agent_message") {
    return { title: "agent replied", body: compactText(data.text), state: task?.status || "session" };
  }
  if (event.type === "agent_started") {
    return { title: "agent started", body: `${data.target || "main"} sandbox=${data.sandbox || "-"} approval=${data.approval || "-"}`, state: "running" };
  }
  if (event.type === "agent_thread") {
    return { title: "codex session", body: data.thread_id || "-", state: "session" };
  }
  if (event.type === "task_status_changed") {
    return { title: `task ${data.status}`, body: `exit=${data.exit_code ?? "-"}`, state: data.status || "session" };
  }
  if (event.type === "agent_error") {
    return { title: "agent error", body: compactText(data.message || JSON.stringify(data)), state: "failed" };
  }
  if (event.type === "workspace_missing") {
    return { title: "workspace missing", body: data.workspace_path || "-", state: "blocked" };
  }
  if (event.type === "task_dispatched") {
    return { title: "task dispatched", body: `target=${data.target || "-"}`, state: "session" };
  }
  if (event.type === "task_final_requested") {
    return { title: "final requested", body: `target=${data.target || "main"}`, state: "running" };
  }
  if (event.type === "task_result_written") {
    return { title: "final written", body: `${data.chars || 0} chars`, state: "completed" };
  }
  if (event.type === "agent_config_updated") {
    return { title: "agent permission updated", body: `${data.agent_id || "-"} sandbox=${data.sandbox || "-"} approval=${data.approval || "-"}`, state: "session" };
  }
  if (event.type === "agent_usage") {
    const usage = data.usage || {};
    return { title: "usage", body: `input=${usage.input_tokens ?? "-"} output=${usage.output_tokens ?? "-"}`, state: "usage" };
  }
  if (event.type === "message") {
    return { title: `${data.sender || "-"} message`, body: compactText(data.message), state: data.sender === "browser" ? "session" : task?.status || "session" };
  }
  return { title: event.type, body: compactText(JSON.stringify(data)), state: "session" };
}

function latestActivityEvent(taskId) {
  const events = taskTimelineEvents(taskId).filter(event => {
    const data = eventData(event);
    return !(event.type === "message" && data.sender === "browser");
  });
  return events.at(-1) || null;
}

function renderLiveActivity() {
  const task = selectedTask();
  if (!task) {
    liveActivityEl.className = "live-activity empty-activity";
    liveActivityEl.innerHTML = '<span>Select a task to see live backend activity.</span>';
    return;
  }
  const event = latestActivityEvent(task.id);
  if (!event) {
    liveActivityEl.className = "live-activity pending";
    liveActivityEl.innerHTML = `
      <span class="activity-dot"></span>
      <strong>No backend activity yet</strong>
      <code>${escapeHtml(task.id)}</code>
      <time></time>
    `;
    return;
  }
  const summary = activitySummary(event, task);
  const eventCount = taskTimelineEvents(task.id).length;
  const timing = latestTurnTiming(task.id) || taskTiming(task.id, task);
  const duration = timing ? `${timing.running ? "elapsed" : "duration"} ${formatDuration(timing.elapsedMs)}` : "";
  liveActivityEl.className = `live-activity ${escapeHtml(summary.state || "session")}`;
  liveActivityEl.innerHTML = `
    <span class="activity-dot"></span>
    <strong>${escapeHtml(summary.title)}</strong>
    <code title="${escapeHtml(summary.body)}">${escapeHtml(summary.body || "-")}</code>
    <span class="activity-count">${escapeHtml([duration, `${eventCount} events`].filter(Boolean).join(" | "))}</span>
    <time>${escapeHtml(event.ts || "")}</time>
  `;
}

function selectedWorkspacePath() {
  return workspaceSelectEl.value === "__custom__" ? workspaceCustomEl.value.trim() : workspaceSelectEl.value;
}

function renderConversation(taskId) {
  const events = taskConversationEvents(taskId);
  if (!events.length) return '<div class="empty">No task conversation yet.</div>';
  const timer = renderTurnTimer(taskId);
  return `<div class="conversation timeline">${events.map(renderTimelineEvent).join("")}${timer}</div>`;
}

function renderTimelineEvent(event) {
  const data = eventData(event);
  if (event.type === "message") {
    const cls = data.sender === "browser" ? "from-browser" : data.sender === "main" ? "from-main" : data.sender === "system" ? "from-system" : "";
    return renderTimelineCard(
      `${data.sender || "-"} -> ${data.to_agent || data.role || data.target || "-"}`,
      data.message || "",
      event.ts || data.ts || "",
      cls
    );
  }
  if (event.type === "agent_message") return renderTimelineCard("agent update", data.text || "", event.ts, "agent-update");
  if (event.type === "agent_command_started") return renderTimelineCard("running command", data.command || "", event.ts, "agent-command");
  if (event.type === "agent_command_finished") {
    const output = data.output_tail ? `\n\nOutput tail:\n${data.output_tail}` : "";
    return renderTimelineCard(`command finished exit=${data.exit_code ?? "-"}`, `${data.command || ""}${output}`, event.ts, data.exit_code === 0 ? "agent-command" : "event-error");
  }
  if (event.type === "agent_error") return renderTimelineCard("agent error", data.message || JSON.stringify(data), event.ts, "event-error");
  if (event.type === "agent_usage") {
    const usage = data.usage || {};
    return renderTimelineStatus(
      "usage",
      `input=${usage.input_tokens ?? "-"} cached=${usage.cached_input_tokens ?? "-"} output=${usage.output_tokens ?? "-"} reasoning=${usage.reasoning_output_tokens ?? "-"}`,
      "usage",
      event.ts
    );
  }
  if (event.type === "task_status_changed") return renderTimelineStatus(`task ${data.status}`, `exit=${data.exit_code ?? "-"}`, data.status, event.ts);
  if (event.type === "task_started") return renderTimelineStatus("task started", data.title || "", "running", event.ts);
  if (event.type === "task_finished") return renderTimelineStatus(`task ${data.status || "finished"}`, `exit=${data.exit_code ?? "-"}`, data.status || "completed", event.ts);
  if (event.type === "task_result_written") return renderTimelineStatus("final written", `${data.chars || 0} chars`, "completed", event.ts);
  if (event.type === "task_final_requested") return renderTimelineStatus("final requested", `target=${data.target || "main"}`, "running", event.ts);
  if (event.type === "agent_started") return renderTimelineStatus("agent started", `${data.target || "main"} from ${data.sender || "-"} sandbox=${data.sandbox || "-"} approval=${data.approval || "-"}`, "running", event.ts);
  if (event.type === "agent_config_updated") return renderTimelineStatus("agent permission updated", `${data.agent_id || "-"} sandbox=${data.sandbox || "-"} approval=${data.approval || "-"}`, "session", event.ts);
  if (event.type === "agent_thread") return renderTimelineStatus("codex session", data.thread_id || "-", "session", event.ts);
  if (event.type === "agent_finished") return renderTimelineStatus("agent finished", `exit=${data.exit_code ?? "-"}`, data.exit_code === 0 ? "completed" : "failed", event.ts);
  if (event.type === "task_dispatched") return renderTimelineStatus("task dispatched", `target=${data.target || "-"}`, "session", event.ts);
  if (event.type === "agent_created") return renderTimelineStatus("sub-agent created", `${data.agent_id || "-"} backend=${data.backend || "-"}`, "session", event.ts);
  if (event.type === "agent_delegated") return renderTimelineStatus("delegated", `${data.count || 0} action(s)`, "session", event.ts);
  if (event.type === "workspace_missing") return renderTimelineStatus("workspace missing", data.workspace_path || "-", "blocked", event.ts);
  return renderTimelineStatus(event.type, JSON.stringify(data), "session", event.ts);
}

function renderTurnTimer(taskId) {
  const timing = latestTurnTiming(taskId);
  if (!timing) return "";
  const title = timing.running ? "Agent is working" : `Agent turn ${timing.status}`;
  const label = timing.running ? "elapsed" : "duration";
  const details = [
    `${label} ${formatDuration(timing.elapsedMs)}`,
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

function renderTimelineCard(title, body, ts, cls) {
  return `
    <div class="message ${cls}">
      <div class="message-head">
        <span>${escapeHtml(title)}</span>
        <span>${escapeHtml(ts || "")}</span>
      </div>
      <div class="message-body">${escapeHtml(body || "")}</div>
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

function renderPanel() {
  renderLiveActivity();
  const task = selectedTask();
  if (!task) {
    panelEl.innerHTML = '<div class="empty">No task selected.</div>';
    return;
  }
  const detail = taskDetails.get(task.id);
  if (activeTab === "conversation") {
    const previousTop = panelEl.scrollTop;
    const shouldFollow = conversationAutoFollow || isPanelNearBottom();
    panelEl.innerHTML = renderConversation(task.id);
    panelEl.scrollTop = shouldFollow ? panelEl.scrollHeight : previousTop;
    return;
  }
  if (!detail) {
    panelEl.innerHTML = '<div class="empty">Loading...</div>';
    return;
  }
  if (activeTab === "final") {
    panelEl.innerHTML = `<pre>${escapeHtml(detail.result || "No Final yet. Use /aha final to generate it.")}</pre>`;
  } else if (activeTab === "logs") {
    const logs = detail.log || taskEvents(task.id).map(formatEvent).join("\n") || "No logs yet.";
    panelEl.innerHTML = `<pre>${escapeHtml(logs)}</pre>`;
  } else {
    const context = [
      "Task:",
      JSON.stringify(detail.task, null, 2),
      "",
      "Sessions:",
      JSON.stringify(detail.sessions || [], null, 2),
      "",
      "Prompt:",
      detail.prompt || "No prompt file."
    ].join("\n");
    panelEl.innerHTML = `<pre>${escapeHtml(context)}</pre>`;
  }
}

function isPanelNearBottom() {
  return panelEl.scrollHeight - panelEl.scrollTop - panelEl.clientHeight < 80;
}

document.querySelectorAll(".tab").forEach(button => {
  button.addEventListener("click", async () => {
    activeTab = button.dataset.tab;
    if (activeTab === "conversation") conversationAutoFollow = true;
    document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item === button));
    if (activeTab !== "conversation") await refreshTaskDetail(selectedTaskId);
    renderPanel();
  });
});

document.getElementById("task-form").addEventListener("submit", async event => {
  event.preventDefault();
  const title = document.getElementById("new-task-title").value.trim();
  if (!title) return;
  await fetch("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
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
    })
  });
  document.getElementById("new-task-title").value = "";
  await loadStatus();
});

document.getElementById("send-form").addEventListener("submit", async event => {
  event.preventDefault();
  const task = selectedTask();
  const message = messageEl.value.trim();
  if (!task || !message) return;
  const agentId = agentTargetEl.value || "main";
  const target = agentId === "main" ? "main" : agentId;
  await fetch("/api/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      target,
      role: agentId === "main" ? "main" : "sub",
      task_id: task.id,
      from_agent: "browser",
      to_agent: agentId,
      message,
      sender: "browser"
    })
  });
  messageEl.value = "";
  commandMenuEl.classList.add("hidden");
  await pollEvents();
  conversationAutoFollow = true;
  renderPanel();
});

messageEl.addEventListener("input", () => {
  commandSelection = 0;
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
  if (activeTab === "conversation") conversationAutoFollow = isPanelNearBottom();
});

agentsEl.addEventListener("pointerdown", () => markAgentsPanelEditing());
agentsEl.addEventListener("focusin", () => markAgentsPanelEditing());
agentsEl.addEventListener("change", () => markAgentsPanelEditing(1500));
agentTargetEl.addEventListener("change", () => {
  syncAgentCards();
  renderSelectedAgentInfo();
});
taskBackendEl.addEventListener("change", renderModelOptions);
showHiddenEl.addEventListener("change", () => {
  const tasks = visibleTasks();
  if (!tasks.some(task => task.id === selectedTaskId)) selectedTaskId = tasks[0]?.id || null;
  renderTaskList();
  renderSelectedHeader();
  renderAgents();
  renderLiveActivity();
  renderPanel();
});
workspaceSelectEl.addEventListener("change", () => {
  const isCustom = workspaceSelectEl.value === "__custom__";
  workspaceCustomEl.classList.toggle("hidden", !isCustom);
  if (isCustom) workspaceCustomEl.focus();
});

async function tick() {
  try {
    if (taskActionInFlight) return;
    await loadStatus();
    await pollEvents();
    if (selectedTaskId && activeTab !== "conversation") await refreshTaskDetail(selectedTaskId);
    renderPanel();
  } catch (err) {
    panelEl.innerHTML = `<pre>${escapeHtml(String(err))}</pre>`;
  }
}

Promise.all([loadBackends(), loadWorkspaces()]).then(tick);
setInterval(tick, pollInterval);
setInterval(() => {
  const task = selectedTask();
  const turn = task ? latestTurnTiming(task.id) : null;
  if (task?.status === "running" || turn?.running) renderPanel();
}, 1000);
