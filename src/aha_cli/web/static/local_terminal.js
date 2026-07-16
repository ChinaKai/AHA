(() => {
  function escapeFallback(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function t(key, fallback = "") {
    return window.AHAI18n?.t?.(key, fallback) || fallback;
  }

  function terminalStatusText(state) {
    if (state.error) return state.error;
    if (state.connecting) return t("local_terminal.connecting", "Connecting...");
    if (state.connected) return t("local_terminal.connected", "Connected");
    return t("local_terminal.disconnected", "Disconnected");
  }

  function terminalUi() {
    return window.AHATerminalUi;
  }

  function terminalSizeForElement(el) {
    return terminalUi().terminalSizeForElement(el);
  }

  function renderTerminalKeybar(escapeHtml) {
    const keys = terminalUi().terminalKeys()
      .map(item => `<button type="button" class="hardware-key-btn" data-local-terminal-key="${escapeHtml(item.name)}" title="Send ${escapeHtml(item.name)}">${escapeHtml(item.label)}</button>`)
      .join("");
    return `<div class="hardware-keybar local-terminal-keybar"><span class="hardware-keybar-keys">${keys}</span></div>`;
  }

  function renderLocalTerminal(state = {}, options = {}) {
    const escapeHtml = options.escapeHtml || escapeFallback;
    const connected = Boolean(state.connected);
    const canConnect = Boolean(state.canConnect);
    const statusClass = state.error ? "error" : connected ? "connected" : state.connecting ? "connecting" : "";
    const connectLabel = state.connecting ? t("local_terminal.connecting_short", "Connecting") : t("local_terminal.connect", "Connect");
    return `
      <div class="local-terminal-panel">
        <div class="local-terminal-head">
          <div>
            <h3>${escapeHtml(t("local_terminal.title", "Local terminal"))}</h3>
            <p>${escapeHtml(t("local_terminal.hint", "Interactive shell on this machine. Loopback access only."))}</p>
          </div>
          <span class="local-terminal-head-actions">
            <span class="local-terminal-status ${escapeHtml(statusClass)}">${escapeHtml(terminalStatusText(state))}</span>
            <button type="button" class="local-terminal-close" data-local-terminal-action="close">${escapeHtml(t("common.close", "Close"))}</button>
          </span>
        </div>
        <div class="local-terminal-actions">
          <button type="button" data-local-terminal-action="connect" ${connected || state.connecting || !canConnect ? "disabled" : ""}>${escapeHtml(connectLabel)}</button>
          <button type="button" data-local-terminal-action="disconnect" ${!connected && !state.connecting ? "disabled" : ""}>${escapeHtml(t("local_terminal.disconnect", "Disconnect"))}</button>
          <button type="button" data-local-terminal-action="clear">${escapeHtml(t("local_terminal.clear", "Clear"))}</button>
        </div>
        <div class="local-terminal-xterm aha-terminal-xterm" data-local-terminal-xterm aria-label="${escapeHtml(t("local_terminal.screen", "Terminal output"))}"></div>
        ${renderTerminalKeybar(escapeHtml)}
      </div>
    `;
  }

  function createLocalTerminalController(elements = {}, deps = {}) {
    let open = false;
    let socket = null;
    let term = null;
    let resizeObserver = null;
    let resizeTimer = 0;
    let viewportResizeCleanup = null;
    let keyboardActive = false;
    let expandedTerminalHeight = 0;
    const disposables = [];
    const state = {
      canConnect: true,
      connected: false,
      connecting: false,
      error: "",
      cols: 100,
      rows: 28
    };

    function currentRunId() {
      return String(deps.currentRunId?.() || "").trim();
    }

    function xtermCtor() {
      return deps.windowRef?.Terminal || window.Terminal || null;
    }

    function xtermEl() {
      return elements.localTerminalPopoverEl?.querySelector?.("[data-local-terminal-xterm]") || null;
    }

    function terminalWsUrl() {
      const path = deps.apiUrl?.("/ws/terminal", { cols: state.cols, rows: state.rows }) || "/ws/terminal";
      const url = new URL(path, deps.windowRef?.location?.href || window.location.href);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      return url.toString();
    }

    function sendMessage(payload) {
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify(payload));
    }

    function sendInput(data) {
      if (!data) return;
      sendMessage({ type: "input", data });
    }

    function disposeTerminal() {
      while (disposables.length) {
        try {
          disposables.pop()?.dispose?.();
        } catch (_err) {
          // Best effort cleanup for xterm disposables.
        }
      }
      if (resizeObserver) {
        resizeObserver.disconnect();
        resizeObserver = null;
      }
      if (resizeTimer) {
        clearTimeout(resizeTimer);
        resizeTimer = 0;
      }
      if (term) {
        term.dispose();
        term = null;
      }
    }

    function resizeToContainer(options = {}) {
      if (!term) return;
      const next = terminalSizeForElement(xtermEl());
      if (next.cols === state.cols && next.rows === state.rows && !options.force) return;
      state.cols = next.cols;
      state.rows = next.rows;
      term.resize(next.cols, next.rows);
      sendMessage({ type: "resize", cols: next.cols, rows: next.rows });
    }

    function scheduleResize() {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        resizeTimer = 0;
        resizeToContainer();
      }, 80);
    }

    function fitTerminalContent() {
      const changed = terminalUi().fitTerminalToContent(term, xtermEl(), {
        active: keyboardActive,
        maxHeight: expandedTerminalHeight
      });
      if (changed) scheduleResize();
    }

    function resizeSoon(options = {}) {
      setTimeout(() => resizeToContainer(options), 0);
      setTimeout(() => resizeToContainer(options), 120);
    }

    function currentViewportHeight(windowRef, viewport) {
      return Math.max(0, Number(viewport?.height || 0), Number(windowRef.innerHeight || 0));
    }

    function localKeyboardInset(windowRef, viewport, virtualKeyboard, baselineHeight = 0) {
      const virtualKeyboardHeight = Number(virtualKeyboard?.boundingRect?.height || 0);
      if (virtualKeyboardHeight > 0) return virtualKeyboardHeight;
      const viewportInset = viewport
        ? Number(windowRef.innerHeight || 0) - Number(viewport.height || 0) - Number(viewport.offsetTop || 0)
        : 0;
      const baselineInset = Number(baselineHeight || 0) - currentViewportHeight(windowRef, viewport) - Number(viewport?.offsetTop || 0);
      return Math.max(0, viewportInset, baselineInset);
    }

    function setLocalKeyboardInset(value) {
      const documentRef = deps.documentRef || (deps.windowRef || window).document || document;
      const inset = Math.max(0, Math.round(Number(value) || 0));
      const nextKeyboardActive = inset > 0;
      if (nextKeyboardActive && !keyboardActive) {
        expandedTerminalHeight = Math.max(180, Number(xtermEl()?.getBoundingClientRect?.()?.height || 0));
      }
      keyboardActive = nextKeyboardActive;
      documentRef.documentElement?.style?.setProperty("--local-terminal-keyboard-inset", `${inset}px`);
      elements.localTerminalPopoverEl?.classList?.toggle("local-terminal-keyboard-active", keyboardActive);
      fitTerminalContent();
      if (!keyboardActive) expandedTerminalHeight = 0;
    }

    function localTerminalMobileHeight(windowRef) {
      const rect = elements.localTerminalPopoverEl?.getBoundingClientRect?.();
      if (rect?.height > 0) return rect.height;
      return Math.max(260, Number(windowRef.innerHeight || 0) - 84);
    }

    function keyboardAdjustedTerminalHeight(baselineTerminalHeight, baselineViewportHeight, keyboardInset) {
      if (keyboardInset <= 0) return baselineTerminalHeight;
      const availableHeight = Math.max(120, Number(baselineViewportHeight || 0) - Number(keyboardInset || 0) - 84);
      return Math.min(Number(baselineTerminalHeight || 0), availableHeight);
    }

    function setLocalTerminalMobileHeight(value) {
      const documentRef = deps.documentRef || (deps.windowRef || window).document || document;
      const height = Math.max(0, Math.round(Number(value) || 0));
      const style = documentRef.documentElement?.style;
      if (!style) return;
      if (height > 0) {
        style.setProperty("--local-terminal-mobile-height", `${height}px`);
      } else {
        style.removeProperty("--local-terminal-mobile-height");
      }
    }

    function detachViewportResizeListeners() {
      viewportResizeCleanup?.();
      viewportResizeCleanup = null;
      setLocalKeyboardInset(0);
      setLocalTerminalMobileHeight(0);
    }

    function attachViewportResizeListeners() {
      detachViewportResizeListeners();
      const windowRef = deps.windowRef || window;
      const documentRef = deps.documentRef || windowRef.document || document;
      const viewport = windowRef.visualViewport || null;
      const virtualKeyboard = (deps.navigatorRef || windowRef.navigator || null)?.virtualKeyboard || null;
      let baselineViewportHeight = currentViewportHeight(windowRef, viewport);
      let baselineTerminalHeight = 0;
      const handleViewportChange = (options = {}) => {
        if (!open) return;
        if (options.forceHeight) {
          baselineViewportHeight = Math.max(baselineViewportHeight, currentViewportHeight(windowRef, viewport));
        }
        const inset = localKeyboardInset(windowRef, viewport, virtualKeyboard, baselineViewportHeight);
        if (inset <= 0) {
          baselineViewportHeight = Math.max(baselineViewportHeight, currentViewportHeight(windowRef, viewport));
        }
        setLocalKeyboardInset(inset);
        if (options.forceHeight || baselineTerminalHeight <= 0) {
          baselineTerminalHeight = localTerminalMobileHeight(windowRef);
        }
        setLocalTerminalMobileHeight(keyboardAdjustedTerminalHeight(baselineTerminalHeight, baselineViewportHeight, inset));
        resizeSoon({ force: true });
      };
      windowRef.addEventListener?.("resize", handleViewportChange, { passive: true });
      viewport?.addEventListener?.("resize", handleViewportChange, { passive: true });
      viewport?.addEventListener?.("scroll", handleViewportChange, { passive: true });
      virtualKeyboard?.addEventListener?.("geometrychange", handleViewportChange);
      documentRef.addEventListener?.("focusin", handleViewportChange, true);
      documentRef.addEventListener?.("focusout", handleViewportChange, true);
      handleViewportChange({ forceHeight: true, forceResize: true });
      setTimeout(() => handleViewportChange({ forceHeight: true, forceResize: true }), 0);
      setTimeout(handleViewportChange, 250);
      viewportResizeCleanup = () => {
        windowRef.removeEventListener?.("resize", handleViewportChange);
        viewport?.removeEventListener?.("resize", handleViewportChange);
        viewport?.removeEventListener?.("scroll", handleViewportChange);
        virtualKeyboard?.removeEventListener?.("geometrychange", handleViewportChange);
        documentRef.removeEventListener?.("focusin", handleViewportChange, true);
        documentRef.removeEventListener?.("focusout", handleViewportChange, true);
      };
    }

    function ensureTerminal() {
      const container = xtermEl();
      if (!container) return null;
      if (term) return term;
      const Terminal = xtermCtor();
      if (!Terminal) {
        state.error = t("local_terminal.xterm_missing", "Terminal runtime unavailable");
        return null;
      }
      const initialSize = terminalSizeForElement(container);
      state.cols = initialSize.cols;
      state.rows = initialSize.rows;
      term = new Terminal(terminalUi().terminalOptions({ cols: state.cols, rows: state.rows }));
      term.open(container);
      disposables.push(term.onData(data => sendInput(data)));
      disposables.push(term.onResize(size => {
        state.cols = size.cols;
        state.rows = size.rows;
        sendMessage({ type: "resize", cols: size.cols, rows: size.rows });
      }));
      if (typeof ResizeObserver !== "undefined") {
        resizeObserver = new ResizeObserver(scheduleResize);
        resizeObserver.observe(container);
      }
      resizeToContainer({ force: true });
      return term;
    }

    function focusTerminal() {
      ensureTerminal()?.focus?.();
    }

    function syncPopover() {
      const popover = elements.localTerminalPopoverEl;
      if (!popover) return;
      state.canConnect = Boolean(currentRunId());
      const connected = Boolean(state.connected);
      const statusClass = state.error ? "error" : connected ? "connected" : state.connecting ? "connecting" : "";
      const statusEl = popover.querySelector(".local-terminal-status");
      if (statusEl) {
        statusEl.className = `local-terminal-status ${statusClass}`.trim();
        statusEl.textContent = terminalStatusText(state);
      }
      const connectEl = popover.querySelector('[data-local-terminal-action="connect"]');
      if (connectEl) {
        connectEl.disabled = connected || state.connecting || !state.canConnect;
        connectEl.textContent = state.connecting
          ? t("local_terminal.connecting_short", "Connecting")
          : t("local_terminal.connect", "Connect");
      }
      const disconnectEl = popover.querySelector('[data-local-terminal-action="disconnect"]');
      if (disconnectEl) disconnectEl.disabled = !connected && !state.connecting;
    }

    function renderPopover() {
      const popover = elements.localTerminalPopoverEl;
      if (!popover) return;
      if (!popover.querySelector(".local-terminal-panel")) {
        popover.innerHTML = renderLocalTerminal(state, { escapeHtml: deps.escapeHtml });
        bindPopoverControls();
      }
      ensureTerminal();
      syncPopover();
    }

    function closeSocket() {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "close" }));
      }
      if (socket) socket.close();
      socket = null;
      state.connected = false;
      state.connecting = false;
    }

    function disconnect() {
      closeSocket();
      syncPopover();
    }

    function connect() {
      if (socket || state.connecting || !currentRunId()) return;
      state.error = "";
      state.connecting = true;
      syncPopover();
      socket = new WebSocket(terminalWsUrl());
      socket.addEventListener("open", () => {
        state.connecting = false;
        state.connected = true;
        syncPopover();
        resizeSoon({ force: true });
        focusTerminal();
      });
      socket.addEventListener("message", event => {
        let payload = null;
        try {
          payload = JSON.parse(String(event.data || ""));
        } catch (_err) {
          return;
        }
        const activeTerm = ensureTerminal();
        if (payload.type === "ready") {
          activeTerm?.writeln?.(`AHA local terminal: ${payload.shell || "shell"} @ ${payload.cwd || ""}`, fitTerminalContent);
          resizeToContainer({ force: true });
          resizeSoon({ force: true });
        } else if (payload.type === "output") {
          activeTerm?.write?.(String(payload.data || ""), fitTerminalContent);
        } else if (payload.type === "error") {
          state.error = String(payload.message || t("local_terminal.error", "Terminal error"));
          activeTerm?.writeln?.(`\r\n[AHA terminal error: ${state.error}]`, fitTerminalContent);
          syncPopover();
        } else if (payload.type === "exit") {
          state.connected = false;
          activeTerm?.writeln?.(`\r\n[process exited: ${payload.returncode ?? ""}]`, fitTerminalContent);
          syncPopover();
        }
      });
      socket.addEventListener("close", () => {
        socket = null;
        state.connected = false;
        state.connecting = false;
        if (open) syncPopover();
      });
      socket.addEventListener("error", () => {
        state.error = t("local_terminal.connect_failed", "Failed to connect local terminal");
        state.connected = false;
        state.connecting = false;
        if (socket) socket.close();
        socket = null;
        syncPopover();
      });
    }

    function bindPopoverControls() {
      const popover = elements.localTerminalPopoverEl;
      if (!popover) return;
      popover.querySelectorAll("[data-local-terminal-action]").forEach(button => {
        button.addEventListener("click", () => {
          const action = button.dataset.localTerminalAction || "";
          if (action === "connect") connect();
          if (action === "disconnect") disconnect();
          if (action === "close") setOpen(false);
          if (action === "clear") {
            ensureTerminal()?.clear?.();
            focusTerminal();
          }
        });
      });
      popover.querySelectorAll("[data-local-terminal-key]").forEach(button => {
        button.addEventListener("click", () => {
          sendInput(terminalUi().terminalKeyBytes(button.dataset.localTerminalKey || ""));
          focusTerminal();
        });
      });
    }

    function setOpen(nextOpen) {
      open = Boolean(nextOpen && elements.localTerminalPopoverEl);
      if (!elements.localTerminalPopoverEl) return;
      if (open) {
        deps.setObserveProxyOpen?.(false);
        deps.setPlayConsoleOpen?.(false);
        deps.setRunMaintenanceConsoleOpen?.(false);
        deps.setSkillsConsoleOpen?.(false);
        deps.setTokenUsageOpen?.(false);
        deps.setWeixinConsoleOpen?.(false);
        elements.sessionMenuEl?.classList.toggle("local-terminal-open", true);
        elements.localTerminalPopoverEl.hidden = false;
        renderPopover();
        attachViewportResizeListeners();
        setTimeout(() => {
          resizeToContainer({ force: true });
          focusTerminal();
        }, 0);
        setTimeout(() => resizeToContainer({ force: true }), 120);
      } else {
        detachViewportResizeListeners();
        closeSocket();
        disposeTerminal();
        elements.sessionMenuEl?.classList.toggle("local-terminal-open", false);
        elements.localTerminalPopoverEl.hidden = true;
        elements.localTerminalPopoverEl.innerHTML = "";
      }
      elements.localTerminalEl?.setAttribute("aria-expanded", String(open));
    }

    return Object.freeze({
      connect,
      disconnect,
      isOpen: () => open,
      renderLocalTerminalPopover: renderPopover,
      resizeToContainer,
      setLocalTerminalOpen: setOpen
    });
  }

  window.AHALocalTerminal = Object.freeze({ createLocalTerminalController, renderLocalTerminal, terminalSizeForElement });
})();
