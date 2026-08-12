(() => {
  const SEARCH_QUERY_STORAGE_KEY = "aha.globalSearchQuery.v1";

  function createGlobalSearchController(elements = {}, deps = {}) {
    const windowRef = deps.windowRef || window;
    const documentRef = deps.documentRef || windowRef.document;
    const escapeHtml = deps.escapeHtml || (value => String(value ?? ""));
    let requestSequence = 0;
    let searchTimer = 0;
    let taskScope = null;
    let scopePreviousType = "all";

    function t(key, fallback = "") {
      return windowRef.AHAI18n?.t?.(key, fallback) || fallback;
    }

    function isOpen() {
      return Boolean(elements.dialogEl?.open);
    }

    function readStoredQuery() {
      try {
        return String(windowRef.localStorage?.getItem(SEARCH_QUERY_STORAGE_KEY) || "");
      } catch (_err) {
        return "";
      }
    }

    function writeStoredQuery(query) {
      try {
        const value = String(query || "");
        if (value) windowRef.localStorage?.setItem(SEARCH_QUERY_STORAGE_KEY, value);
        else windowRef.localStorage?.removeItem(SEARCH_QUERY_STORAGE_KEY);
      } catch (_err) {
        // localStorage can be unavailable in restricted browser modes.
      }
    }

    function setState(message = "", isError = false) {
      if (!elements.stateEl) return;
      elements.stateEl.textContent = message;
      elements.stateEl.classList.toggle("error", Boolean(isError));
    }

    function resultDescription(item = {}) {
      const description = String(item.description || "").replace(/\s+/g, " ").trim();
      return description.length > 180 ? `${description.slice(0, 177)}...` : description;
    }

    function resultTypeLabel(type) {
      if (type === "memo") return t("global_search.memo", "Memo");
      if (type === "kb") return t("global_search.knowledge", "Knowledge");
      return t("global_search.task", "Task");
    }

    function scopeHeader() {
      if (!taskScope) return "";
      return `
        <div class="global-search-scope">
          <button type="button" data-global-search-back>${escapeHtml(t("global_search.back", "Back"))}</button>
          <span>${escapeHtml(t("global_search.chat_scope", "Chats in {task}").replace("{task}", taskScope.title || taskScope.taskId))}</span>
        </div>`;
    }

    function resultCard(item = {}) {
      const type = ["memo", "kb"].includes(item.type) ? item.type : "task";
      const isChat = type === "task" && item.result_kind === "chat";
      const chatCount = Number(item.chat_match_count || 0);
      const typeLabel = isChat ? t("global_search.chat", "Chat") : resultTypeLabel(type);
      const displayTitle = isChat
        ? [item.message_sender, item.message_target].filter(Boolean).join(" → ")
          || t("global_search.chat_message", "Chat message")
        : item.title || item.id;
      const status = String(item.status || "");
      const hidden = item.hidden ? ` · ${t("global_search.hidden", "hidden")}` : "";
      const date = String(item.updated_at || item.created_at || item.scheduled_date || "");
      const context = type === "kb"
        ? [
            item.kb_kind === "note"
              ? t("global_search.knowledge_note", "Capture")
              : t("global_search.knowledge_entry", "Entry"),
            item.project_key || item.scope || "",
            item.entry_type || status,
          ].filter(Boolean).join(" · ")
        : isChat
          ? [item.message_sender, date].filter(Boolean).join(" · ")
          : [item.run_goal || item.run_id, status ? `${status}${hidden}` : ""].filter(Boolean).join(" · ");
      const description = chatCount ? (item.chat_preview || item.description) : item.description;
      const countBadge = chatCount
        ? `<span class="global-search-result-count">${escapeHtml(
            t("global_search.chat_matches", "{count} chat matches").replace("{count}", String(chatCount))
          )}</span>`
        : '<span class="global-search-result-arrow" aria-hidden="true">›</span>';
      return `
        <button class="global-search-result" type="button"
          data-global-search-type="${escapeHtml(type)}"
          data-global-search-result-kind="${escapeHtml(item.result_kind)}"
          data-global-search-run-id="${escapeHtml(item.run_id)}"
          data-global-search-kb-kind="${escapeHtml(item.kb_kind)}"
          data-global-search-message-cursor="${escapeHtml(item.message_cursor)}"
          data-global-search-chat-count="${escapeHtml(chatCount)}"
          data-global-search-match-query="${escapeHtml(item.match_query)}"
          data-global-search-match-field="${escapeHtml(item.match_field)}"
          data-global-search-item-title="${escapeHtml(item.title || item.id)}"
          data-global-search-item-id="${escapeHtml(item.id)}">
          <span class="global-search-result-type ${escapeHtml(type)}">${escapeHtml(typeLabel)}</span>
          <span class="global-search-result-content">
            <strong>${escapeHtml(displayTitle)}</strong>
            <span class="global-search-result-description">${escapeHtml(resultDescription({ description }))}</span>
            <span class="global-search-result-meta">${escapeHtml(context)}${date && !isChat ? ` · ${escapeHtml(date)}` : ""}</span>
          </span>
          ${countBadge}
        </button>`;
    }

    function renderResults(payload = {}) {
      const results = Array.isArray(payload.results) ? payload.results : [];
      if (!elements.resultsEl) return;
      if (!results.length) {
        elements.resultsEl.innerHTML = `${scopeHeader()}<div class="global-search-empty">${escapeHtml(t("global_search.no_results", "No matching tasks, memos, or knowledge."))}</div>`;
        setState("");
        return;
      }
      if (taskScope) {
        elements.resultsEl.innerHTML = `${scopeHeader()}<div class="global-search-group-results">${results.map(resultCard).join("")}</div>`;
      } else {
        const groups = ["task", "memo", "kb"]
          .map(type => ({ type, items: results.filter(item => item.type === type) }))
          .filter(group => group.items.length);
        elements.resultsEl.innerHTML = groups.map(group => `
          <section class="global-search-group">
            <h3>${escapeHtml(resultTypeLabel(group.type))}<span>${group.items.length}</span></h3>
            <div class="global-search-group-results">${group.items.map(resultCard).join("")}</div>
          </section>
        `).join("");
      }
      const total = Number(payload.total || results.length);
      setState(
        total > results.length
          ? t("global_search.result_count_limited", "Showing {shown} of {count} results")
              .replace("{shown}", String(results.length))
              .replace("{count}", String(total))
          : t("global_search.result_count", "{count} results").replace("{count}", String(total))
      );
    }

    async function search() {
      const query = String(elements.inputEl?.value || "").trim();
      const resultType = String(elements.typeEl?.value || "all").trim();
      const sequence = ++requestSequence;
      writeStoredQuery(query);
      if (!query) {
        if (elements.resultsEl) {
          elements.resultsEl.innerHTML = `<div class="global-search-empty">${escapeHtml(t("global_search.hint", "Search tasks, memos, and knowledge."))}</div>`;
        }
        setState("");
        return;
      }
      setState(t("global_search.loading", "Searching..."));
      try {
        const url = deps.apiUrl(
          "/api/global-search",
          {
            q: query,
            type: taskScope ? "task" : resultType,
            limit: taskScope ? "100" : "80",
            ...(taskScope ? {
              chat_only: "1",
              scope_run_id: taskScope.runId,
              scope_task_id: taskScope.taskId,
            } : {})
          },
          { runScoped: false }
        );
        const payload = await deps.fetchJson(url, {}, "Global search failed");
        if (sequence !== requestSequence) return;
        renderResults(payload);
      } catch (err) {
        if (sequence !== requestSequence) return;
        setState(err?.message || t("global_search.failed", "Search failed."), true);
      }
    }

    function queueSearch() {
      if (searchTimer) windowRef.clearTimeout(searchTimer);
      searchTimer = windowRef.setTimeout(() => {
        searchTimer = 0;
        void search();
      }, 180);
    }

    function open() {
      try {
        if (typeof elements.dialogEl?.showModal === "function") {
          if (!elements.dialogEl.open) elements.dialogEl.showModal();
        } else {
          elements.dialogEl?.setAttribute("open", "");
        }
      } catch (_err) {
        elements.dialogEl?.setAttribute("open", "");
      }
      elements.openEl?.setAttribute("aria-expanded", "true");
      if (String(elements.inputEl?.value || "").trim()) void search();
      windowRef.setTimeout(() => {
        elements.inputEl?.focus();
        elements.inputEl?.select();
      }, 0);
    }

    function openHelp() {
      try {
        if (typeof elements.helpDialogEl?.showModal === "function") {
          if (!elements.helpDialogEl.open) elements.helpDialogEl.showModal();
        } else {
          elements.helpDialogEl?.setAttribute("open", "");
        }
      } catch (_err) {
        elements.helpDialogEl?.setAttribute("open", "");
      }
      elements.helpOpenEl?.setAttribute("aria-expanded", "true");
      windowRef.setTimeout(() => elements.helpCloseEl?.focus(), 0);
    }

    function closeHelp() {
      if (typeof elements.helpDialogEl?.close === "function" && elements.helpDialogEl.open) {
        elements.helpDialogEl.close();
      } else {
        elements.helpDialogEl?.removeAttribute("open");
      }
      elements.helpOpenEl?.setAttribute("aria-expanded", "false");
    }

    function resetTaskScope() {
      const wasScoped = Boolean(taskScope);
      taskScope = null;
      if (elements.typeEl) {
        elements.typeEl.disabled = false;
        if (wasScoped) elements.typeEl.value = scopePreviousType;
      }
      return wasScoped;
    }

    function close() {
      closeHelp();
      if (typeof elements.dialogEl?.close === "function" && elements.dialogEl.open) {
        elements.dialogEl.close();
      } else {
        elements.dialogEl?.removeAttribute("open");
      }
      elements.openEl?.setAttribute("aria-expanded", "false");
      if (resetTaskScope()) void search();
    }

    function resultUrl(type, runId, itemId, options = {}) {
      const url = new URL(windowRef.location.href);
      if (runId) url.searchParams.set("run_id", runId);
      url.searchParams.delete("run");
      if (type === "kb") {
        url.searchParams.set("view", "kb");
        if (options.kbKind === "note") {
          url.searchParams.set("kb_note_id", itemId);
          url.searchParams.delete("kb_entry_id");
        } else {
          url.searchParams.set("kb_entry_id", itemId);
          url.searchParams.delete("kb_note_id");
        }
        url.searchParams.delete("selected_task_id");
        url.searchParams.delete("task_id");
        url.searchParams.delete("memo_id");
        url.searchParams.delete("chat_event_id");
        if (options.matchQuery && ["title", "description"].includes(options.matchField)) {
          url.searchParams.set("search_query", options.matchQuery);
          url.searchParams.set("search_field", options.matchField);
        } else {
          url.searchParams.delete("search_query");
          url.searchParams.delete("search_field");
        }
      } else if (type === "memo") {
        url.searchParams.set("view", "memo");
        url.searchParams.set("memo_id", itemId);
        url.searchParams.delete("selected_task_id");
        url.searchParams.delete("task_id");
        url.searchParams.delete("kb_entry_id");
        url.searchParams.delete("kb_note_id");
        url.searchParams.delete("chat_event_id");
        if (options.matchQuery && ["title", "description"].includes(options.matchField)) {
          url.searchParams.set("search_query", options.matchQuery);
          url.searchParams.set("search_field", options.matchField);
        } else {
          url.searchParams.delete("search_query");
          url.searchParams.delete("search_field");
        }
      } else {
        url.searchParams.set("view", "task");
        url.searchParams.set("selected_task_id", itemId);
        if (options.messageCursor !== undefined && options.messageCursor !== null && String(options.messageCursor) !== "") {
          url.searchParams.set("chat_event_id", String(options.messageCursor));
        } else {
          url.searchParams.delete("chat_event_id");
        }
        url.searchParams.delete("task_id");
        url.searchParams.delete("memo_id");
        url.searchParams.delete("kb_entry_id");
        url.searchParams.delete("kb_note_id");
        url.searchParams.delete("search_query");
        url.searchParams.delete("search_field");
      }
      return url.toString();
    }

    function enterTaskScope(button) {
      scopePreviousType = String(elements.typeEl?.value || "all");
      taskScope = {
        runId: String(button?.dataset.globalSearchRunId || ""),
        taskId: String(button?.dataset.globalSearchItemId || ""),
        title: String(button?.dataset.globalSearchItemTitle || ""),
      };
      if (elements.typeEl) {
        elements.typeEl.value = "task";
        elements.typeEl.disabled = true;
      }
      void search();
    }

    function leaveTaskScope() {
      taskScope = null;
      if (elements.typeEl) {
        elements.typeEl.disabled = false;
        elements.typeEl.value = scopePreviousType;
      }
      void search();
    }

    function openResult(button) {
      const type = String(button?.dataset.globalSearchType || "");
      const runId = String(button?.dataset.globalSearchRunId || "");
      const itemId = String(button?.dataset.globalSearchItemId || "");
      const kbKind = String(button?.dataset.globalSearchKbKind || "");
      const messageCursor = String(button?.dataset.globalSearchMessageCursor || "");
      const chatCount = Number(button?.dataset.globalSearchChatCount || 0);
      const matchQuery = String(button?.dataset.globalSearchMatchQuery || "");
      const matchField = String(button?.dataset.globalSearchMatchField || "");
      if (!itemId || !["task", "memo", "kb"].includes(type) || (type !== "kb" && !runId)) return;
      if (type === "task" && chatCount > 0 && !taskScope && !messageCursor) {
        enterTaskScope(button);
        return;
      }
      windowRef.location.assign(resultUrl(type, runId, itemId, {
        kbKind,
        messageCursor,
        matchQuery,
        matchField,
      }));
    }

    function bind() {
      elements.openEl?.addEventListener("click", open);
      elements.closeEl?.addEventListener("click", close);
      elements.helpOpenEl?.addEventListener("click", openHelp);
      elements.helpCloseEl?.addEventListener("click", closeHelp);
      elements.formEl?.addEventListener("submit", event => {
        event.preventDefault();
        void search();
      });
      if (elements.inputEl) {
        const storedQuery = readStoredQuery();
        if (!elements.inputEl.value && storedQuery) elements.inputEl.value = storedQuery;
        elements.inputEl.addEventListener("input", () => {
          writeStoredQuery(elements.inputEl.value);
          queueSearch();
        });
      }
      elements.typeEl?.addEventListener("change", () => void search());
      elements.resultsEl?.addEventListener("click", event => {
        const back = event.target instanceof Element ? event.target.closest("[data-global-search-back]") : null;
        if (back) {
          leaveTaskScope();
          return;
        }
        const button = event.target instanceof Element ? event.target.closest("[data-global-search-item-id]") : null;
        if (button) openResult(button);
      });
      elements.dialogEl?.addEventListener("close", () => {
        closeHelp();
        elements.openEl?.setAttribute("aria-expanded", "false");
        if (resetTaskScope()) void search();
      });
      elements.dialogEl?.addEventListener("click", event => {
        if (event.target === elements.dialogEl) close();
      });
      elements.helpDialogEl?.addEventListener("close", () => {
        elements.helpOpenEl?.setAttribute("aria-expanded", "false");
      });
      elements.helpDialogEl?.addEventListener("click", event => {
        if (event.target === elements.helpDialogEl) closeHelp();
      });
      documentRef?.addEventListener("keydown", event => {
        const target = event.target;
        const editing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement || target?.isContentEditable;
        // Cmd/Ctrl+K now belongs to the global command palette; global search keeps "/" and the toolbar button.
        if (event.key === "/" && !editing && !isOpen()) {
          event.preventDefault();
          open();
        }
      });
    }

    return Object.freeze({
      bind,
      close,
      closeHelp,
      isOpen,
      open,
      openHelp,
      resultUrl,
      search,
      leaveTaskScope,
    });
  }

  window.AHAGlobalSearchController = Object.freeze({ createGlobalSearchController });
})();
