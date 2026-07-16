(() => {
  const MAX_PENDING_INPUT_CHARS = 65536;

  function terminalUi() {
    return window.AHATerminalUi;
  }

  function terminalSizeForElement(el) {
    return terminalUi().terminalSizeForElement(el);
  }

  function createHardwareTerminalController(elements = {}, deps = {}) {
    const panelEl = elements.panelEl;
    const windowRef = deps.windowRef || window;
    const WebSocketImpl = windowRef.WebSocket || (typeof WebSocket !== "undefined" ? WebSocket : null);
    let socket = null;
    let term = null;
    let rootEl = null;
    let terminalEl = null;
    let resizeObserver = null;
    let resizeTimer = 0;
    let reconnectTimer = 0;
    let socketGeneration = 0;
    let terminalReady = false;
    let pendingInput = "";
    let mountKey = "";
    let taskId = "";
    let transport = "serial";
    let readOnly = false;
    let bridge = {};
    let cols = 100;
    let rows = 28;
    let keyboardActive = false;
    let expandedTerminalHeight = 0;
    const disposables = [];

    function xtermCtor() {
      return windowRef.Terminal || window.Terminal || null;
    }

    function socketOpen() {
      return Boolean(socket && WebSocketImpl && socket.readyState === WebSocketImpl.OPEN);
    }

    function bridgeAcceptsInput() {
      if (readOnly || bridge?.paused || !bridge?.alive) return false;
      if (transport === "network") return bridge?.connected !== false && bridge?.status === "running";
      return bridge?.status === "running" && !bridge?.error;
    }

    function terminalWsUrl() {
      const path = deps.apiUrl?.("/ws/hardware-terminal", { task_id: taskId, transport, cols, rows })
        || `/ws/hardware-terminal?task_id=${encodeURIComponent(taskId)}&transport=${encodeURIComponent(transport)}&cols=${cols}&rows=${rows}`;
      const url = new URL(path, windowRef.location?.href || window.location.href);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      return url.toString();
    }

    function updateToolbar(connection = "") {
      const statusEl = rootEl?.querySelector?.("[data-hardware-terminal-status]");
      const toggleEl = rootEl?.querySelector?.("[data-hardware-bridge-action]");
      const takeoverEl = rootEl?.querySelector?.("[data-hardware-takeover]");
      const ownerDetailEl = rootEl?.querySelector?.("[data-hardware-owner-detail]");
      const paused = Boolean(bridge?.paused);
      const alive = Boolean(bridge?.alive);
      const owner = bridge?.device_owner || {};
      const occupied = Boolean(bridge?.error && owner?.pid);
      const canTakeover = Boolean(occupied && owner?.can_terminate === true);
      const connected = transport === "network" ? Boolean(bridge?.connected) : alive;
      let label = "connecting…";
      let variant = "idle";
      if (readOnly) {
        label = "read-only";
      } else if (occupied) {
        label = "occupied";
        variant = "failed";
      } else if (connection === "error" || bridge?.error) {
        label = "error";
        variant = "failed";
      } else if (paused) {
        label = "paused";
        variant = "awaiting_user";
      } else if (connection === "open" && connected) {
        label = "live";
        variant = "running";
      }
      if (statusEl) {
        statusEl.textContent = label;
        statusEl.className = `status hardware-bridge-status ${variant}`;
        statusEl.setAttribute("data-hardware-terminal-status", "");
      }
      if (toggleEl) {
        toggleEl.dataset.hardwareBridgeAction = paused ? "resume" : "pause";
        toggleEl.textContent = paused ? "Resume" : "Pause";
      }
      if (takeoverEl) {
        takeoverEl.hidden = !canTakeover;
        takeoverEl.dataset.ownerPid = String(owner?.pid || "");
        takeoverEl.dataset.ownerProcess = String(owner?.process || "process");
      }
      if (ownerDetailEl) {
        const uid = owner?.uid === null || owner?.uid === undefined ? "" : `, UID ${owner.uid}`;
        const identity = `${owner?.process || "process"} (PID ${owner?.pid || "unknown"}${uid})`;
        const detail = occupied
          ? canTakeover
            ? `Serial device is in use by ${identity}. Take over sends SIGTERM only.`
            : `Serial device is in use by ${identity}. AHA has no permission to terminate it; close it manually.`
          : bridge?.error ? String(bridge.error) : "";
        ownerDetailEl.textContent = detail;
        ownerDetailEl.hidden = !detail;
      }
    }

    function sendMessage(payload) {
      if (!socketOpen()) return false;
      try {
        socket.send(JSON.stringify(payload));
        return true;
      } catch (_err) {
        return false;
      }
    }

    function queueInput(data) {
      const remaining = MAX_PENDING_INPUT_CHARS - pendingInput.length;
      if (remaining > 0) pendingInput += String(data).slice(0, remaining);
    }

    function flushPendingInput() {
      if (!pendingInput || readOnly || !terminalReady) return false;
      const data = pendingInput;
      if (!sendMessage({ type: "input", data })) return false;
      pendingInput = "";
      term?.focus?.();
      return true;
    }

    function sendInput(data) {
      if (!data || readOnly) return false;
      const text = String(data);
      const sent = terminalReady && sendMessage({ type: "input", data: text });
      if (sent) term?.focus?.();
      else if (mountKey) queueInput(text);
      return sent || Boolean(mountKey);
    }

    function resizeToContainer(options = {}) {
      if (!term || !terminalEl) return;
      const next = terminalSizeForElement(terminalEl);
      if (!options.force && next.cols === cols && next.rows === rows) return;
      cols = next.cols;
      rows = next.rows;
      term.resize(cols, rows);
      sendMessage({ type: "resize", cols, rows });
    }

    function scheduleResize() {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        resizeTimer = 0;
        resizeToContainer();
      }, 80);
    }

    function fitTerminalContent() {
      const changed = terminalUi().fitTerminalToContent(term, terminalEl, {
        active: keyboardActive,
        maxHeight: expandedTerminalHeight
      });
      if (changed) scheduleResize();
    }

    function clearReconnect() {
      if (!reconnectTimer) return;
      clearTimeout(reconnectTimer);
      reconnectTimer = 0;
    }

    function closeSocket() {
      socketGeneration += 1;
      terminalReady = false;
      const current = socket;
      socket = null;
      if (!current) return;
      try {
        current.send(JSON.stringify({ type: "close" }));
      } catch (_err) {
        // The peer may already be gone.
      }
      try {
        current.close();
      } catch (_err) {
        // Best effort cleanup.
      }
    }

    function disposeTerminal() {
      while (disposables.length) {
        try {
          disposables.pop()?.dispose?.();
        } catch (_err) {
          // Best effort cleanup for xterm disposables.
        }
      }
      resizeObserver?.disconnect?.();
      resizeObserver = null;
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = 0;
      term?.dispose?.();
      term = null;
    }

    function connect() {
      if (!mountKey || !WebSocketImpl) return;
      clearReconnect();
      closeSocket();
      const generation = socketGeneration;
      updateToolbar("connecting");
      const next = new WebSocketImpl(terminalWsUrl());
      socket = next;
      next.addEventListener("open", () => {
        if (generation !== socketGeneration || socket !== next) return;
        updateToolbar("open");
      });
      next.addEventListener("message", event => {
        if (generation !== socketGeneration || socket !== next) return;
        let payload;
        try {
          payload = JSON.parse(String(event.data || "{}"));
        } catch (_err) {
          return;
        }
        if (payload.type === "ready") {
          readOnly = Boolean(payload.read_only);
          bridge = payload.bridge || bridge;
          term?.reset?.();
          terminalReady = bridgeAcceptsInput();
          if (readOnly) pendingInput = "";
          else flushPendingInput();
          updateToolbar("open");
          resizeToContainer({ force: true });
          fitTerminalContent();
        } else if (payload.type === "output") {
          term?.write?.(String(payload.data || ""), fitTerminalContent);
        } else if (payload.type === "status") {
          bridge = payload.bridge || payload.status || payload;
          terminalReady = bridgeAcceptsInput();
          if (terminalReady) flushPendingInput();
          updateToolbar("open");
        } else if (payload.type === "error") {
          updateToolbar("error");
        }
      });
      next.addEventListener("error", () => {
        if (generation === socketGeneration && socket === next) {
          terminalReady = false;
          updateToolbar("error");
        }
      });
      next.addEventListener("close", () => {
        if (generation !== socketGeneration || socket !== next) return;
        socket = null;
        terminalReady = false;
        updateToolbar("closed");
        if (mountKey) reconnectTimer = setTimeout(connect, 800);
      });
    }

    function mount(nextTaskId, state = {}) {
      const nextTransport = String(state.transport || "serial");
      const nextKey = `${String(deps.currentRunId?.() || "")}:${String(nextTaskId || "")}:${nextTransport}`;
      const nextRoot = panelEl?.querySelector?.("[data-hardware-terminal-root]") || null;
      const nextTerminal = nextRoot?.querySelector?.("[data-hardware-terminal-xterm]") || null;
      if (!nextRoot || !nextTerminal) return false;
      if (mountKey === nextKey && rootEl === nextRoot && term) {
        sync(state);
        scheduleResize();
        return true;
      }
      unmount();
      const Ctor = xtermCtor();
      if (!Ctor) return false;
      mountKey = nextKey;
      taskId = String(nextTaskId || "");
      transport = nextTransport;
      rootEl = nextRoot;
      terminalEl = nextTerminal;
      readOnly = Boolean(state.readOnly);
      bridge = state.bridge || {};
      const size = terminalSizeForElement(terminalEl);
      cols = size.cols;
      rows = size.rows;
      term = new Ctor(terminalUi().terminalOptions({ cols, rows }));
      term.open(terminalEl);
      disposables.push(term.onData(data => sendInput(data)));
      disposables.push(term.onResize(sizeValue => {
        cols = sizeValue.cols;
        rows = sizeValue.rows;
        sendMessage({ type: "resize", cols, rows });
      }));
      const ResizeObserverImpl = windowRef.ResizeObserver || (typeof ResizeObserver !== "undefined" ? ResizeObserver : null);
      if (ResizeObserverImpl) {
        resizeObserver = new ResizeObserverImpl(scheduleResize);
        resizeObserver.observe(terminalEl);
      }
      const viewportMonitor = terminalUi().createTerminalViewportMonitor({
        windowRef,
        documentRef: windowRef.document,
        navigatorRef: windowRef.navigator,
        isActive: () => Boolean(mountKey && rootEl),
        onChange: ({ active, becameActive, inset }) => {
          if (active && !keyboardActive) {
            expandedTerminalHeight = Math.max(180, Number(terminalEl?.getBoundingClientRect?.()?.height || 0));
          }
          keyboardActive = active;
          rootEl?.classList?.toggle("terminal-keyboard-active", active);
          windowRef.document?.documentElement?.style?.setProperty("--hardware-terminal-keyboard-inset", `${Math.round(inset)}px`);
          fitTerminalContent();
          if (!keyboardActive) expandedTerminalHeight = 0;
          scheduleResize();
          if (becameActive) terminalEl?.scrollIntoView?.({ block: "start", behavior: "smooth" });
        }
      });
      disposables.push(viewportMonitor);
      const clearEl = rootEl.querySelector?.('[data-hardware-terminal-action="clear"]');
      if (clearEl) {
        const clearTerminal = () => {
          term?.clear?.();
          term?.focus?.();
        };
        clearEl.addEventListener("click", clearTerminal);
        disposables.push({ dispose: () => clearEl.removeEventListener("click", clearTerminal) });
      }
      updateToolbar("connecting");
      connect();
      setTimeout(() => resizeToContainer({ force: true }), 0);
      term.focus();
      return true;
    }

    function sync(state = {}) {
      readOnly = Boolean(state.readOnly);
      bridge = state.bridge || bridge;
      updateToolbar(socketOpen() ? "open" : "connecting");
    }

    function focus() {
      term?.focus?.();
    }

    function isMounted() {
      return Boolean(mountKey && term);
    }

    function unmount() {
      clearReconnect();
      mountKey = "";
      closeSocket();
      disposeTerminal();
      taskId = "";
      transport = "serial";
      rootEl = null;
      terminalEl = null;
      bridge = {};
      readOnly = false;
      terminalReady = false;
      pendingInput = "";
      keyboardActive = false;
      expandedTerminalHeight = 0;
      windowRef.document?.documentElement?.style?.setProperty("--hardware-terminal-keyboard-inset", "0px");
    }

    return Object.freeze({ mount, sync, sendInput, focus, isMounted, unmount });
  }

  window.AHAHardwareTerminal = Object.freeze({
    createHardwareTerminalController,
    terminalSizeForElement
  });
})();
