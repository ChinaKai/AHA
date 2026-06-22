// The header view switcher opens the knowledge console as an in-page view.
(() => {
  const VIEW_STORAGE_KEY = "aha.taskMemoViewExplicit";
  const KNOWLEDGE_URL = "/static/knowledge.html";

  function knowledgeUrl() {
    const params = new URLSearchParams(window.location.search || "");
    const runId = String(params.get("run_id") || params.get("run") || "").trim();
    return runId ? `${KNOWLEDGE_URL}?run_id=${encodeURIComponent(runId)}` : KNOWLEDGE_URL;
  }

  function setStoredView(view) {
    try {
      window.localStorage?.setItem(VIEW_STORAGE_KEY, view);
    } catch (_err) {
      // Ignore private-mode/storage failures; click handling still works.
    }
  }

  function syncViewUrl(view) {
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("view", view);
      if (view === "kb") {
        url.searchParams.delete("selected_task_id");
        url.searchParams.delete("task_id");
      }
      window.history?.replaceState?.(window.history.state, "", url);
    } catch (_err) {
      // URL sync is best effort.
    }
  }

  function setViewButtons(view) {
    document.getElementById("open-task-view")?.setAttribute("aria-pressed", String(view === "task"));
    document.getElementById("open-task-memos")?.setAttribute("aria-pressed", String(view === "memo"));
    document.getElementById("open-knowledge-base")?.setAttribute("aria-pressed", String(view === "kb"));
    document.getElementById("session-toggle")?.setAttribute("aria-pressed", "false");
  }

  function showKnowledgeView() {
    const home = document.getElementById("knowledge-home");
    const frame = document.getElementById("knowledge-home-frame");
    if (!home || !frame) return;
    document.body?.classList?.remove("task-memo-home");
    document.body?.classList?.add("knowledge-home");
    document.getElementById("task-memo-dialog")?.removeAttribute("open");
    home.hidden = false;
    if (!frame.getAttribute("src")) frame.setAttribute("src", knowledgeUrl());
    setStoredView("kb");
    syncViewUrl("kb");
    setViewButtons("kb");
  }

  function hideKnowledgeView(nextView = "task", sync = true) {
    const home = document.getElementById("knowledge-home");
    document.body?.classList?.remove("knowledge-home");
    if (home) home.hidden = true;
    if (sync) {
      setStoredView(nextView);
      syncViewUrl(nextView);
    }
    setViewButtons(nextView);
  }

  function initialView() {
    const params = new URLSearchParams(window.location.search || "");
    const queryView = String(params.get("view") || "").trim().toLowerCase();
    if (queryView) return queryView;
    if (params.get("selected_task_id") || params.get("task_id")) return "task";
    try {
      return String(window.localStorage?.getItem(VIEW_STORAGE_KEY) || "").trim().toLowerCase();
    } catch (_err) {
      return "";
    }
  }

  function init() {
    document.getElementById("open-task-view")?.addEventListener("click", () => hideKnowledgeView("task"));
    document.getElementById("open-task-memos")?.addEventListener("click", () => hideKnowledgeView("memo", false));
    const headerBtn = document.getElementById("open-knowledge-base");
    headerBtn?.addEventListener("click", (ev) => {
      ev.preventDefault();
      showKnowledgeView();
    });
    if (initialView() === "kb") showKnowledgeView();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
