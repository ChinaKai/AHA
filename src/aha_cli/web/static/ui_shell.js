(() => {
  function createUiShell(elements = {}, deps = {}) {
    const windowRef = deps.windowRef || window;
    const documentRef = deps.documentRef || windowRef.document || document;
    const navigatorRef = deps.navigatorRef || windowRef.navigator || navigator;
    let mobileViewportRaf = 0;

    function sidebarStorageKey(side) {
      return `aha.${side}.sidebarCollapsed`;
    }

    function setRunManagerCollapsed(collapsed) {
      elements.runManagerEl?.classList.toggle("run-manager-collapsed", collapsed);
      if (elements.runManagerToggleEl) {
        const labelKey = collapsed ? "run.manager_expand" : "run.manager_collapse";
        const fallback = collapsed ? "Expand run management" : "Collapse run management";
        const label = windowRef.AHAI18n?.t?.(labelKey, fallback) || fallback;
        elements.runManagerToggleEl.setAttribute("aria-expanded", String(!collapsed));
        elements.runManagerToggleEl.setAttribute("aria-label", label);
        elements.runManagerToggleEl.setAttribute("title", label);
        elements.runManagerToggleEl.textContent = collapsed ? "▸" : "▾";
      }
    }

    function readSidebarCollapsed(side) {
      try {
        return windowRef.localStorage?.getItem(sidebarStorageKey(side)) === "true";
      } catch {
        return false;
      }
    }

    function writeSidebarCollapsed(side, collapsed) {
      try {
        windowRef.localStorage?.setItem(sidebarStorageKey(side), collapsed ? "true" : "false");
      } catch {
        // localStorage can be unavailable in restricted browser modes.
      }
    }

    function setSidebarCollapsed(side, collapsed) {
      const className = `${side}-collapsed`;
      elements.body?.classList.toggle(className, collapsed);
      const expanded = String(!collapsed);
      const controls = side === "overview"
        ? [elements.collapseOverviewEl, elements.overviewRailToggleEl]
        : [elements.collapseAgentsEl, elements.agentsRailToggleEl];
      for (const control of controls) {
        if (control) control.setAttribute("aria-expanded", expanded);
      }
      writeSidebarCollapsed(side, collapsed);
    }

    function initDesktopSidebars() {
      setSidebarCollapsed("overview", readSidebarCollapsed("overview"));
      setSidebarCollapsed("agents", readSidebarCollapsed("agents"));
      setRunManagerCollapsed(false);
      elements.collapseOverviewEl?.addEventListener("click", () => setSidebarCollapsed("overview", true));
      elements.overviewRailToggleEl?.addEventListener("click", () => setSidebarCollapsed("overview", false));
      elements.collapseAgentsEl?.addEventListener("click", () => setSidebarCollapsed("agents", true));
      elements.agentsRailToggleEl?.addEventListener("click", () => setSidebarCollapsed("agents", false));
      elements.runManagerToggleEl?.addEventListener("click", () => {
        setRunManagerCollapsed(!elements.runManagerEl?.classList.contains("run-manager-collapsed"));
      });
    }

    function closeTaskCreateDialog() {
      if (!elements.taskCreateDialogEl) return;
      if (typeof elements.taskCreateDialogEl.close === "function" && elements.taskCreateDialogEl.open) {
        elements.taskCreateDialogEl.close();
      } else {
        elements.taskCreateDialogEl.removeAttribute("open");
      }
    }

    function openTaskCreateDialog() {
      if (!deps.currentRunId?.()) {
        deps.alertError?.(window.AHAI18n?.t?.("task.create_run_first", "Create a run before adding a task.") || "Create a run before adding a task.");
        return;
      }
      if (!elements.taskCreateDialogEl) return;
      closeMobileSheets();
      closeMobileActionPanel();
      deps.syncCreateProxyDefaultForBackend?.({ force: true });
      deps.syncCreateTaskSupervisionModeFields?.({ force: true });
      try {
        if (typeof elements.taskCreateDialogEl.showModal === "function") {
          if (!elements.taskCreateDialogEl.open) elements.taskCreateDialogEl.showModal();
        } else {
          elements.taskCreateDialogEl.setAttribute("open", "");
        }
      } catch (_err) {
        elements.taskCreateDialogEl.setAttribute("open", "");
      }
      windowRef.setTimeout(() => elements.newTaskTitleEl?.focus(), 0);
    }

    function initTaskCreateDialog() {
      elements.openTaskCreateEl?.addEventListener("click", openTaskCreateDialog);
      elements.closeTaskCreateEl?.addEventListener("click", closeTaskCreateDialog);
      elements.cancelTaskCreateEl?.addEventListener("click", closeTaskCreateDialog);
      elements.taskCreateDialogEl?.addEventListener("click", event => {
        if (event.target === elements.taskCreateDialogEl) closeTaskCreateDialog();
      });
    }

    function setMobileSheet(sheet) {
      const taskOpen = sheet === "tasks";
      const agentsOpen = sheet === "agents";
      if (taskOpen || agentsOpen) setMobileActionPanel(false);
      elements.body?.classList.toggle("mobile-tasks-open", taskOpen);
      elements.body?.classList.toggle("mobile-agents-open", agentsOpen);
      if (elements.mobileSheetBackdropEl) elements.mobileSheetBackdropEl.hidden = !taskOpen && !agentsOpen;
      elements.openTasksSheetEl?.setAttribute("aria-expanded", String(taskOpen));
      elements.openAgentsSheetEl?.setAttribute("aria-expanded", String(agentsOpen));
      elements.mobileTaskSummaryEl?.setAttribute("aria-expanded", String(taskOpen));
    }

    function setMobileActionPanel(open, options = {}) {
      if (!elements.mobileActionPanelEl) return false;
      const nextOpen = Boolean(open && !options.hasMessage);
      elements.mobileActionPanelEl.hidden = !nextOpen;
      elements.body?.classList.toggle("mobile-actions-open", nextOpen);
      elements.mobileActionsToggleEl?.setAttribute("aria-expanded", String(nextOpen));
      if (nextOpen) elements.commandMenuEl?.classList.add("hidden");
      return nextOpen;
    }

    function closeMobileSheets() {
      setMobileSheet(null);
    }

    function closeMobileActionPanel() {
      setMobileActionPanel(false);
    }

    function targetInsideMobileActionPanel(target) {
      const element = target instanceof Element ? target : null;
      return Boolean(element && (
        elements.mobileActionPanelEl?.contains(element) ||
        elements.mobileActionsToggleEl?.contains(element)
      ));
    }

    function closeMobileActionPanelForOutsideEvent(event) {
      if (!elements.mobileActionPanelEl || elements.mobileActionPanelEl.hidden) return;
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
      if (["conversation", "final", "logs", "hardware", "context"].includes(action)) {
        await deps.activateTab?.(action);
      }
    }

    function syncMobileActionPanel(activeTab = deps.activeTab?.()) {
      const hardwareEnabled = Boolean(
        windowRef.AHATaskList?.taskHardwareDebugEnabled?.(deps.selectedTask?.())
      );
      elements.mobileActionPanelEl?.querySelectorAll("[data-mobile-action]").forEach(button => {
        const action = button.dataset.mobileAction || "";
        if (action === "hardware") {
          button.hidden = !hardwareEnabled;
        }
        button.classList.toggle("active", action === activeTab);
      });
    }

    function syncMobileComposerToggle(hasMessage) {
      if (!elements.mobileActionsToggleEl) return;
      elements.mobileActionsToggleEl.classList.toggle("sending", Boolean(hasMessage));
      const sendLabel = window.AHAI18n?.t?.("conversation.send", "Send") || "Send";
      const toolsLabel = window.AHAI18n?.t?.("aha.console", "AHA console") || "AHA console";
      elements.mobileActionsToggleEl.textContent = hasMessage ? sendLabel : "+";
      elements.mobileActionsToggleEl.setAttribute("aria-label", hasMessage ? sendLabel : toolsLabel);
      elements.mobileActionsToggleEl.title = hasMessage ? sendLabel : toolsLabel;
    }

    function mobileViewportMatches() {
      return windowRef.matchMedia("(max-width: 640px)").matches;
    }

    function isKeyboardTextControl(element) {
      if (!(element instanceof windowRef.HTMLElement)) return false;
      if (element.isContentEditable) return true;
      if (element instanceof windowRef.HTMLTextAreaElement) return true;
      if (!(element instanceof windowRef.HTMLInputElement)) return false;
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
      return isKeyboardTextControl(documentRef.activeElement) ? documentRef.activeElement : null;
    }

    function mobileKeyboardInset() {
      const virtualKeyboardHeight = Number(navigatorRef.virtualKeyboard?.boundingRect?.height || 0);
      if (virtualKeyboardHeight > 0) return virtualKeyboardHeight;
      const viewport = windowRef.visualViewport;
      if (!viewport) return 0;
      return Math.max(0, windowRef.innerHeight - viewport.height - viewport.offsetTop);
    }

    function mobileDialogScrollerFor(element) {
      if (!element) return null;
      if (elements.taskCreateDialogEl?.open && elements.taskCreateDialogEl.contains(element)) {
        return elements.taskCreateDialogEl.querySelector(".task-dialog-panel");
      }
      if (elements.runCreateDialogEl?.open && elements.runCreateDialogEl.contains(element)) {
        return elements.runCreateDialogEl.querySelector(".task-dialog-panel");
      }
      if (
        (elements.taskMemoDialogEl?.open || elements.taskMemoDialogEl?.hasAttribute?.("open")) &&
        elements.taskMemoDialogEl.contains(element)
      ) {
        return elements.taskMemoDialogEl;
      }
      return null;
    }

    function keepMobileControlVisible(control, keyboardInset) {
      if (!control || !mobileViewportMatches()) return;
      const scroller = mobileDialogScrollerFor(control);
      if (!scroller) return;
      const rect = control.getBoundingClientRect();
      const topLimit = 16;
      const bottomLimit = windowRef.innerHeight - keyboardInset - 86;
      if (rect.bottom > bottomLimit) {
        scroller.scrollTop += rect.bottom - bottomLimit;
      } else if (rect.top < topLimit) {
        scroller.scrollTop -= topLimit - rect.top;
      }
    }

    function applyMobileViewport() {
      mobileViewportRaf = 0;
      if (!mobileViewportMatches()) {
        documentRef.documentElement.style.setProperty("--mobile-keyboard-inset", "0px");
        documentRef.body.classList.remove("mobile-keyboard-active");
        return;
      }
      const keyboardActive = Boolean(activeKeyboardTextControl());
      const keyboardInset = keyboardActive ? mobileKeyboardInset() : 0;
      documentRef.body.classList.toggle("mobile-keyboard-active", keyboardActive);
      documentRef.documentElement.style.setProperty("--mobile-keyboard-inset", `${Math.round(keyboardInset)}px`);
      if (keyboardActive) keepMobileControlVisible(activeKeyboardTextControl(), keyboardInset);
    }

    function scheduleMobileViewportSync() {
      if (!mobileViewportRaf) {
        mobileViewportRaf = windowRef.requestAnimationFrame(applyMobileViewport);
      }
    }

    function clearMobileViewportSync() {
      if (mobileViewportRaf) windowRef.cancelAnimationFrame(mobileViewportRaf);
      mobileViewportRaf = 0;
    }

    function initMobileViewport() {
      const mobileQuery = windowRef.matchMedia("(max-width: 640px)");
      if (mobileQuery.addEventListener) {
        mobileQuery.addEventListener("change", scheduleMobileViewportSync);
      } else {
        mobileQuery.addListener(scheduleMobileViewportSync);
      }
      windowRef.addEventListener("resize", scheduleMobileViewportSync, { passive: true });
      windowRef.addEventListener("orientationchange", scheduleMobileViewportSync);
      windowRef.visualViewport?.addEventListener("resize", scheduleMobileViewportSync, { passive: true });
      windowRef.visualViewport?.addEventListener("scroll", scheduleMobileViewportSync, { passive: true });
      if (navigatorRef.virtualKeyboard) {
        try {
          navigatorRef.virtualKeyboard.overlaysContent = true;
        } catch (_err) {
          // Some browsers expose the API as read-only.
        }
        navigatorRef.virtualKeyboard.addEventListener?.("geometrychange", scheduleMobileViewportSync);
      }
      documentRef.addEventListener("focusin", event => {
        if (isKeyboardTextControl(event.target)) scheduleMobileViewportSync();
      }, true);
      documentRef.addEventListener("focusout", event => {
        if (isKeyboardTextControl(event.target)) scheduleMobileViewportSync();
      }, true);
      windowRef.addEventListener("pagehide", clearMobileViewportSync);
      scheduleMobileViewportSync();
    }

    function initMobileSheets() {
      const mobileQuery = windowRef.matchMedia("(max-width: 640px)");
      elements.mobileTaskSummaryEl?.addEventListener("click", () => setMobileSheet("tasks"));
      elements.openTasksSheetEl?.addEventListener("click", () => setMobileSheet("tasks"));
      elements.openAgentsSheetEl?.addEventListener("click", () => setMobileSheet("agents"));
      elements.closeTasksSheetEl?.addEventListener("click", closeMobileSheets);
      elements.closeAgentsSheetEl?.addEventListener("click", closeMobileSheets);
      elements.mobileSheetBackdropEl?.addEventListener("click", closeMobileSheets);
      documentRef.addEventListener("keydown", event => {
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
      elements.mobileActionsToggleEl?.addEventListener("pointerdown", event => {
        deps.requestComposerSubmitFromPointer?.(event);
      });
      elements.mobileActionsToggleEl?.addEventListener("click", event => {
        if (deps.pointerSubmitActive?.()) {
          event.preventDefault();
          return;
        }
        if (deps.hasMessage?.()) {
          event.preventDefault();
          deps.requestComposerSubmit?.();
          return;
        }
        setMobileActionPanel(Boolean(elements.mobileActionPanelEl?.hidden), { hasMessage: Boolean(deps.hasMessage?.()) });
      });
      elements.mobileActionPanelEl?.addEventListener("click", event => {
        const button = event.target instanceof Element ? event.target.closest("[data-mobile-action]") : null;
        if (!button) return;
        void handleMobileAction(button.dataset.mobileAction || "");
      });
      documentRef.addEventListener("pointerdown", closeMobileActionPanelForOutsideEvent, true);
      documentRef.addEventListener("focusin", closeMobileActionPanelForOutsideEvent, true);
      syncMobileActionPanel();
      deps.syncMobileComposerAction?.();
    }

    return Object.freeze({
      setMobileSheet,
      setSidebarCollapsed,
      setMobileActionPanel,
      closeMobileSheets,
      closeMobileActionPanel,
      closeTaskCreateDialog,
      openTaskCreateDialog,
      targetInsideMobileActionPanel,
      closeMobileActionPanelForOutsideEvent,
      handleMobileAction,
      syncMobileActionPanel,
      syncMobileComposerToggle,
      mobileViewportMatches,
      isKeyboardTextControl,
      activeKeyboardTextControl,
      mobileKeyboardInset,
      mobileDialogScrollerFor,
      keepMobileControlVisible,
      applyMobileViewport,
      scheduleMobileViewportSync,
      clearMobileViewportSync,
      initDesktopSidebars,
      initMobileViewport,
      initMobileSheets,
      initMobileActionPanel,
      initTaskCreateDialog
    });
  }

  window.AHAUiShell = Object.freeze({ createUiShell });
})();
