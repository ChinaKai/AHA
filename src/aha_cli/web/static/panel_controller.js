(() => {
  function createPanelController(elements = {}, deps = {}) {
    const panelEl = elements.panelEl;
    const sendFormEl = elements.sendFormEl;
    const messageEl = elements.messageEl;
    const documentRef = elements.documentRef || document;
    const hardwareTerminalController = deps.hardwareTerminalController || null;
    const browserSessionController = deps.browserSessionController || null;
    const activeTab = deps.activeTab || (() => "conversation");
    const setActiveTab = deps.setActiveTab || (() => {});
    const currentRunId = deps.currentRunId || (() => "");
    const selectedTask = deps.selectedTask || (() => null);
    const selectedTaskId = deps.selectedTaskId || (() => "");
    const runHasNoTasks = deps.runHasNoTasks || (() => false);
    const renderFirstRunState = deps.renderFirstRunState || (() => {});
    const renderConversationFilters = deps.renderConversationFilters || (() => {});
    const renderConversation = deps.renderConversation || (() => "");
    const renderFinalPanelHtml = deps.renderFinalPanelHtml || (() => "");
    const renderLogsPanelHtml = deps.renderLogsPanelHtml || (() => "");
    const renderHardwareIoPanelHtml = deps.renderHardwareIoPanelHtml || (() => "");
    const renderBrowserPanelHtml = deps.renderBrowserPanelHtml || (() => "");
    const renderContextPanelHtml = deps.renderContextPanelHtml || (() => "");
    const renderContextEvidencePanelHtml = deps.renderContextEvidencePanelHtml || (() => "");
    const logState = deps.logState || (() => ({}));
    const hardwareIoState = deps.hardwareIoState || (() => ({}));
    const finalDetail = deps.finalDetail || (() => null);
    const contextDetail = deps.contextDetail || (() => null);
    const contextEvidenceDetail = deps.contextEvidenceDetail || (() => null);
    const promptMetricsState = deps.promptMetricsState || (() => ({}));
    const renderRawPromptSection = deps.renderRawPromptSection || (() => "");
    const renderPromptMetricsPanel = deps.renderPromptMetricsPanel || (() => "");
    const capturePromptMetricsPopoverState = deps.capturePromptMetricsPopoverState || (() => null);
    const restorePromptMetricsPopoverState = deps.restorePromptMetricsPopoverState || (() => {});
    const positionPromptMetricsPopover = deps.positionPromptMetricsPopover || (() => {});
    const captureContextScrollState = deps.captureContextScrollState || (() => null);
    const restoreContextScrollState = deps.restoreContextScrollState || (() => {});
    const syncExpandedMessageKeysFromDom = deps.syncExpandedMessageKeysFromDom || (() => {});
    const syncMobileActionPanel = deps.syncMobileActionPanel || (() => {});
    const ensureActiveTabData = deps.ensureActiveTabData || (async () => {});
    const conversationAutoFollow = deps.conversationAutoFollow || (() => true);
    const setConversationAutoFollow = deps.setConversationAutoFollow || (() => {});
    const initialChatEventId = String(deps.initialChatEventId || "");
    const composerDisabledState = new WeakMap();
    let initialChatFocusPending = Boolean(initialChatEventId);

    function focusInitialChatMessage() {
      if (!initialChatFocusPending || !panelEl) return false;
      const matches = panelEl.querySelectorAll?.("[data-chat-event-cursor]") || [];
      const message = Array.from(matches).find(item => (
        String(item?.dataset?.chatEventCursor || "") === initialChatEventId
      ));
      if (!message) return false;
      const collapsedBody = message.querySelector?.("details.collapsed-message");
      if (collapsedBody) collapsedBody.open = true;
      message.classList?.add("global-search-chat-hit");
      message.setAttribute?.("tabindex", "-1");
      message.scrollIntoView?.({ block: "center", behavior: "auto" });
      message.focus?.({ preventScroll: true });
      initialChatFocusPending = false;
      return true;
    }

    function syncComposerAvailability(tab) {
      const enabled = tab === "conversation";
      if (!sendFormEl) return;
      sendFormEl.hidden = false;
      sendFormEl.dataset.composerEnabled = String(enabled);
      sendFormEl.setAttribute?.("aria-disabled", String(!enabled));
      const controls = sendFormEl.querySelectorAll?.(
        "textarea, select, input, button:not(.mobile-actions-toggle)"
      ) || [];
      for (const control of controls) {
        if (!enabled) {
          if (!composerDisabledState.has(control)) {
            composerDisabledState.set(control, Boolean(control.disabled));
          }
          control.disabled = true;
        } else if (composerDisabledState.has(control)) {
          control.disabled = composerDisabledState.get(control);
          composerDisabledState.delete(control);
        }
      }
      if (!enabled) {
        messageEl?.blur?.();
        documentRef.querySelector?.("#command-menu")?.classList?.add?.("hidden");
      }
    }

    function prepareForTab(tab) {
      syncComposerAvailability(tab);
      if (tab !== "hardware") hardwareTerminalController?.unmount?.();
      if (tab !== "browser") browserSessionController?.unmount?.();
    }

    function isPanelNearBottom() {
      if (!panelEl) return true;
      // The virtualized conversation scrolls inside .vl-host (the panel itself is
      // height-constrained and does not scroll), so check the host when present.
      const host = panelEl.querySelector?.(".vl-host");
      if (host) return host.scrollHeight - host.scrollTop - host.clientHeight < 80;
      return panelEl.scrollHeight - panelEl.scrollTop - panelEl.clientHeight < 80;
    }

    function renderPanel(options = {}) {
      renderConversationFilters();
      if (!currentRunId()) {
        prepareForTab("");
        renderFirstRunState();
        return;
      }
      const task = selectedTask();
      if (!task) {
        prepareForTab("");
        panelEl.innerHTML = runHasNoTasks()
          ? `
            <div class="empty first-task-empty">
              <strong>No tasks yet</strong>
              <span>Create the first task for this run.</span>
              <button type="button" data-open-first-task>Create first task</button>
            </div>
          `
          : '<div class="empty">No task selected.</div>';
        return;
      }
      // Keep the mobile action bar's Hardware entry in sync with the selected task,
      // not just on tab switches (selecting a task must re-evaluate visibility).
      syncMobileActionPanel();
      const tab = activeTab();
      prepareForTab(tab);
      if (tab === "conversation") {
        const previousTop = options.previousTop ?? panelEl.scrollTop;
        const previousHeight = options.previousHeight ?? panelEl.scrollHeight;
        const metricsPopoverState = capturePromptMetricsPopoverState();
        const metricsPopoverOpen = Boolean(metricsPopoverState);
        const shouldFollow = !metricsPopoverOpen && (conversationAutoFollow() || isPanelNearBottom());
        syncExpandedMessageKeysFromDom();
        // When the virtual conversation is already mounted and the new render is also
        // virtual, keep the mounted list alive: re-render the timeline chrome (Load
        // older button, timer) fresh but swap the freshly-built .vl-host for the
        // previously-mounted host element, so the virtual list is NOT torn down and
        // re-created every poll tick (which flickers and loses measured heights and
        // scroll position on large conversations).
        const keptVlHost = panelEl.querySelector(".vl-host");
        const virtualTarget = deps.backendTarget?.() || "main";
        if (keptVlHost && deps.isVirtualConversation?.(task.id, virtualTarget)) {
          const keptScrollTop = keptVlHost.scrollTop;
          panelEl.innerHTML = renderConversation(task.id);
          const freshVlHost = panelEl.querySelector(".vl-host");
          if (freshVlHost && freshVlHost !== keptVlHost) {
            freshVlHost.replaceWith(keptVlHost);
            deps.updateMountedVirtualConversation?.(task.id, virtualTarget, {
              anchorBottom: shouldFollow,
              initialScrollTop: keptScrollTop
            });
          }
          restorePromptMetricsPopoverState(metricsPopoverState);
          positionPromptMetricsPopover();
          return;
        }
        // Capture the virtual host's scroll position before innerHTML replaces it so a
        // re-render (realtime usage update, session refresh) can restore it instead of
        // forcing the user back to the newest message.
        const previousVlHost = panelEl.querySelector(".vl-host");
        const previousVlScrollTop = previousVlHost ? previousVlHost.scrollTop : 0;
        panelEl.innerHTML = renderConversation(task.id);
        const vlHost = panelEl.querySelector(".vl-host");
        if (vlHost) {
          deps.mountVirtualConversation?.(vlHost, vlHost.dataset.vlTask || task.id, vlHost.dataset.vlTarget || "", {
            anchorBottom: shouldFollow,
            initialScrollTop: previousVlScrollTop
          });
          if (shouldFollow) vlHost.scrollTop = vlHost.scrollHeight;
        } else if (options.preserveScroll) {
          panelEl.scrollTop = panelEl.scrollHeight - previousHeight + previousTop;
        } else if (metricsPopoverOpen) {
          panelEl.scrollTop = previousTop;
        } else {
          panelEl.scrollTop = shouldFollow ? panelEl.scrollHeight : previousTop;
        }
        restorePromptMetricsPopoverState(metricsPopoverState);
        positionPromptMetricsPopover();
        focusInitialChatMessage();
        return;
      }
      if (tab === "final") {
        panelEl.innerHTML = renderFinalPanelHtml(finalDetail(task.id));
      } else if (tab === "logs") {
        const state = logState(task.id);
        const previousTop = options.previousTop ?? panelEl.scrollTop;
        const previousHeight = options.previousHeight ?? panelEl.scrollHeight;
        const shouldFollow = state.autoFollow;
        panelEl.innerHTML = renderLogsPanelHtml(state);
        if (options.preserveScroll) {
          panelEl.scrollTop = panelEl.scrollHeight - previousHeight + previousTop;
        } else if (state.initialized) {
          panelEl.scrollTop = shouldFollow ? panelEl.scrollHeight : previousTop;
        }
      } else if (tab === "hardware") {
        const state = hardwareIoState(task.id);
        state.taskId = task.id;
        const expectedKey = `${task.id}:${String(state.transport || "serial")}`;
        const root = panelEl.querySelector("[data-hardware-terminal-root]");
        if (!root || root.dataset.hardwareTerminalKey !== expectedKey) {
          hardwareTerminalController?.unmount?.();
          panelEl.innerHTML = renderHardwareIoPanelHtml(state);
        }
        hardwareTerminalController?.mount?.(task.id, state);
      } else if (tab === "browser") {
        const root = panelEl.querySelector("[data-browser-session-root]");
        if (!root || root.dataset.browserTaskId !== task.id) {
          panelEl.innerHTML = renderBrowserPanelHtml(task);
        }
        browserSessionController?.mount?.(task.id);
        browserSessionController?.focus?.();
      } else if (tab === "context") {
        const detail = contextDetail(task.id);
        if (!detail) {
          panelEl.innerHTML = '<div class="empty loading">Loading context...</div>';
          return;
        }
        const contextScrollState = (options.preserveContextScroll || panelEl.querySelector(".context-view"))
          ? captureContextScrollState()
          : null;
        const metrics = promptMetricsState(task.id);
        panelEl.innerHTML = renderContextPanelHtml({
          rawPromptHtml: renderRawPromptSection(metrics.data, metrics.total),
          promptMetricsHtml: renderPromptMetricsPanel(task.id)
        });
        restoreContextScrollState(contextScrollState);
      } else if (tab === "context-evidence") {
        panelEl.innerHTML = renderContextEvidencePanelHtml(contextEvidenceDetail(task.id));
      } else {
        panelEl.innerHTML = '<div class="empty">Unknown task view.</div>';
      }
    }

    async function activateTab(tab) {
      setActiveTab(tab || "conversation");
      if (activeTab() === "conversation") setConversationAutoFollow(true);
      if (activeTab() === "logs" && selectedTaskId()) logState(selectedTaskId()).autoFollow = true;
      if (activeTab() === "hardware" && selectedTaskId()) hardwareIoState(selectedTaskId()).autoFollow = true;
      documentRef.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item.dataset.tab === activeTab()));
      syncMobileActionPanel();
      prepareForTab(activeTab());
      if (activeTab() === "browser") renderPanel();
      await ensureActiveTabData();
      renderPanel();
    }

    return Object.freeze({
      renderPanel,
      isPanelNearBottom,
      activateTab
    });
  }

  window.AHAPanelController = Object.freeze({ createPanelController });
})();
