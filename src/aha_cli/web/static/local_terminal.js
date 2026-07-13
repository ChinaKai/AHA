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

    function terminalBottomGapForElement(el) {
      const styles = el && typeof getComputedStyle === "function" ? getComputedStyle(el) : null;
      const value = Number.parseFloat(styles?.getPropertyValue("--local-terminal-bottom-gap") || "0");
      return Number.isFinite(value) ? Math.max(0, value) : 0;
    }

    function terminalSizeForElement(el) {
      const rect = el?.getBoundingClientRect?.() || { width: 900, height: 420 };
      const bottomGap = terminalBottomGapForElement(el);
      const cols = Math.max(40, Math.min(240, Math.floor(Math.max(320, rect.width - 18) / 8.4)));
      const rows = Math.max(12, Math.min(80, Math.floor(Math.max(180, rect.height - 18 - bottomGap) / 17.2)));
      return { cols, rows };
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
          <span class="local-terminal-status ${escapeHtml(statusClass)}">${escapeHtml(terminalStatusText(state))}</span>
        </div>
        <div class="local-terminal-actions">
          <button type="button" data-local-terminal-action="connect" ${connected || state.connecting || !canConnect ? "disabled" : ""}>${escapeHtml(connectLabel)}</button>
          <button type="button" data-local-terminal-action="disconnect" ${!connected && !state.connecting ? "disabled" : ""}>${escapeHtml(t("local_terminal.disconnect", "Disconnect"))}</button>
          <button type="button" data-local-terminal-action="clear">${escapeHtml(t("local_terminal.clear", "Clear"))}</button>
        </div>
        <div class="local-terminal-xterm" data-local-terminal-xterm aria-label="${escapeHtml(t("local_terminal.screen", "Terminal output"))}"></div>
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
      documentRef.documentElement?.style?.setProperty("--local-terminal-keyboard-inset", `${inset}px`);
      elements.localTerminalPopoverEl?.classList?.toggle("local-terminal-keyboard-active", inset > 0);
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
      term = new Terminal({
        allowProposedApi: false,
        convertEol: false,
        cursorBlink: true,
        cols: state.cols,
        rows: state.rows,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
        fontSize: 13,
        scrollback: 5000,
        theme: {
          background: "#101828",
          foreground: "#f8fafc",
          cursor: "#f8fafc",
          selectionBackground: "#344054"
        }
      });
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

    function renderPopover() {
      if (!elements.localTerminalPopoverEl) return;
      disposeTerminal();
      state.canConnect = Boolean(currentRunId());
      elements.localTerminalPopoverEl.innerHTML = renderLocalTerminal(state, { escapeHtml: deps.escapeHtml });
      bindPopoverControls();
      ensureTerminal();
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
      renderPopover();
    }

    function connect() {
      if (socket || state.connecting || !currentRunId()) return;
      state.error = "";
      state.connecting = true;
      renderPopover();
      socket = new WebSocket(terminalWsUrl());
      socket.addEventListener("open", () => {
        state.connecting = false;
        state.connected = true;
        renderPopover();
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
          activeTerm?.writeln?.(`AHA local terminal: ${payload.shell || "shell"} @ ${payload.cwd || ""}`);
          resizeToContainer({ force: true });
          resizeSoon({ force: true });
        } else if (payload.type === "output") {
          activeTerm?.write?.(String(payload.data || ""));
        } else if (payload.type === "error") {
          state.error = String(payload.message || t("local_terminal.error", "Terminal error"));
          activeTerm?.writeln?.(`\r\n[AHA terminal error: ${state.error}]`);
          renderPopover();
        } else if (payload.type === "exit") {
          state.connected = false;
          activeTerm?.writeln?.(`\r\n[process exited: ${payload.returncode ?? ""}]`);
        }
      });
      socket.addEventListener("close", () => {
        socket = null;
        state.connected = false;
        state.connecting = false;
        if (open) renderPopover();
      });
      socket.addEventListener("error", () => {
        state.error = t("local_terminal.connect_failed", "Failed to connect local terminal");
        state.connected = false;
        state.connecting = false;
        if (socket) socket.close();
        socket = null;
        renderPopover();
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
          if (action === "clear") {
            ensureTerminal()?.clear?.();
            focusTerminal();
          }
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
