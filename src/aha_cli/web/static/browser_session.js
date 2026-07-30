(() => {
  const RECONNECT_DELAY_MS = 1000;
  const WHEEL_THROTTLE_MS = 32;
  const TOUCH_SCROLL_MULTIPLIER = 2;

  function createBrowserSessionController(elements = {}, deps = {}) {
    const panelEl = elements.panelEl;
    const windowRef = deps.windowRef || window;
    const documentRef = deps.documentRef
      || windowRef.document
      || (typeof document !== "undefined" ? document : null);
    const WebSocketImpl = windowRef.WebSocket || (typeof WebSocket !== "undefined" ? WebSocket : null);
    let socket = null;
    let rootEl = null;
    let taskId = "";
    let mountGeneration = 0;
    let reconnectTimer = 0;
    let requestSequence = 0;
    let frameWidth = 1280;
    let frameHeight = 720;
    let frameImageWidth = 1280;
    let frameImageHeight = 720;
    let frameInputReady = false;
    let latestState = {};
    let bridgeInstanceId = "";
    let lastWheelAt = 0;
    let composingText = false;
    let compositionCommitPending = "";
    let touchGesture = null;
    let suppressFrameClickUntil = 0;
    let suppressControlClickUntil = 0;
    let suppressControlTarget = null;
    let deviceMode = "desktop";
    let manualClosed = false;
    let deviceBusy = false;
    let lifecycleBusy = false;
    let bookmarksBusy = false;
    let bookmarks = [];
    let keyboardCaptureActive = false;
    let keyboardPinnedOpen = false;
    let keyboardViewportContracted = false;
    let defaultKeyboardFocusAttempted = false;
    let stableViewportHeight = 0;
    const pendingActions = new Map();
    const listeners = [];

    function socketOpen() {
      return Boolean(socket && WebSocketImpl && socket.readyState === WebSocketImpl.OPEN);
    }

    function browserWsUrl() {
      const path = deps.apiUrl?.("/ws/browser-session", { task_id: taskId })
        || `/ws/browser-session?task_id=${encodeURIComponent(taskId)}`;
      const url = new URL(path, windowRef.location?.href || window.location.href);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      return url.toString();
    }

    function setError(message = "") {
      const errorEl = rootEl?.querySelector?.("[data-browser-error]");
      if (!errorEl) return;
      errorEl.textContent = String(message || "");
      errorEl.hidden = !message;
    }

    function setStatus(label, variant = "idle") {
      const statusEl = rootEl?.querySelector?.("[data-browser-status]");
      if (!statusEl) return;
      const statusLabel = String(label || "unknown");
      statusEl.textContent = "";
      statusEl.className = `browser-session-status ${variant}`;
      statusEl.setAttribute("data-browser-status", "");
      statusEl.setAttribute("aria-label", `Browser status: ${statusLabel}`);
      statusEl.setAttribute("title", statusLabel);
    }

    function renderControls() {
      const nativeActive = String(latestState.display?.active || "") === "native";
      rootEl?.querySelectorAll?.("[data-browser-device-mode]")?.forEach(button => {
        const active = String(button.dataset.browserDeviceMode || "") === deviceMode;
        button.classList?.toggle?.("active", active);
        button.setAttribute("aria-pressed", String(active));
        button.disabled = deviceBusy || lifecycleBusy;
      });
      rootEl?.querySelectorAll?.("[data-browser-lifecycle]")?.forEach(button => {
        const action = String(button.dataset.browserLifecycle || "");
        button.hidden = manualClosed ? action !== "start" : action === "start";
        button.disabled = lifecycleBusy;
      });
      rootEl?.querySelectorAll?.(
        "[data-browser-action], [data-browser-address], [data-browser-go], "
          + "[data-browser-select-tab], [data-browser-close-tab], "
          + "[data-browser-bookmark-url]"
      )?.forEach(element => {
        const nativeFocus = (
          nativeActive
          && String(element.dataset?.browserAction || "") === "focus_window"
        );
        element.disabled = (
          manualClosed
          || lifecycleBusy
          || deviceBusy
          || (!frameInputReady && !nativeFocus)
        );
      });
      const bookmarkToggle = rootEl?.querySelector?.("[data-browser-bookmark-toggle]");
      if (bookmarkToggle) {
        const currentUrl = String(latestState.url || "");
        bookmarkToggle.disabled = (
          bookmarksBusy
          || manualClosed
          || lifecycleBusy
          || !frameInputReady
          || !/^https?:\/\//i.test(currentUrl)
        );
      }
      const settingsSave = rootEl?.querySelector?.("[data-browser-runtime-settings-save]");
      if (settingsSave) settingsSave.disabled = lifecycleBusy || deviceBusy;
    }

    function setFrameInputReady(ready) {
      frameInputReady = Boolean(ready);
      rootEl?.classList?.toggle?.("browser-frame-input-ready", frameInputReady);
      const stage = rootEl?.querySelector?.("[data-browser-input-surface]");
      stage?.setAttribute?.("aria-busy", String(!frameInputReady));
      renderControls();
    }

    function mobileViewportMatches() {
      return Boolean(windowRef.matchMedia?.("(max-width: 640px)")?.matches);
    }

    function browserKeyboardControl(target) {
      const address = rootEl?.querySelector?.("[data-browser-address]");
      const keyboardInput = rootEl?.querySelector?.("[data-browser-keyboard-input]");
      return target === address || target === keyboardInput;
    }

    function browserAddressTarget(value) {
      const input = String(value || "").trim();
      if (!input) return "";
      if (/^https?:\/\//i.test(input)) return input;
      if (/^(?:localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::\d+)?(?:[/?#]|$)/i.test(input)) {
        return `http://${input}`;
      }
      if (/^(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]|$)/i.test(input)) {
        return `https://${input}`;
      }
      return `https://www.bing.com/search?q=${encodeURIComponent(input)}`;
    }

    function beginKeyboardCapture(options = {}) {
      if (!mobileViewportMatches()) return;
      if (options.pin) keyboardPinnedOpen = true;
      if (!keyboardCaptureActive) {
        keyboardViewportContracted = false;
        const visualViewport = windowRef.visualViewport;
        stableViewportHeight = Math.max(
          1,
          Number(windowRef.innerHeight || 0),
          Number(visualViewport?.height || 0) + Number(visualViewport?.offsetTop || 0)
        );
      }
      keyboardCaptureActive = true;
      const documentElement = documentRef?.documentElement;
      documentElement?.style?.setProperty?.(
        "--browser-stable-viewport-height",
        `${Math.round(stableViewportHeight)}px`
      );
      documentElement?.classList?.add?.("browser-keyboard-capture-active");
      rootEl?.classList?.add?.("browser-keyboard-capture-active");
    }

    function endKeyboardCapture() {
      keyboardCaptureActive = false;
      keyboardPinnedOpen = false;
      keyboardViewportContracted = false;
      stableViewportHeight = 0;
      const documentElement = documentRef?.documentElement;
      documentElement?.classList?.remove?.("browser-keyboard-capture-active");
      documentElement?.style?.removeProperty?.("--browser-stable-viewport-height");
      documentElement?.style?.setProperty?.("--mobile-keyboard-inset", "0px");
      documentRef?.body?.classList?.remove?.("mobile-keyboard-active");
      rootEl?.classList?.remove?.("browser-keyboard-capture-active");
    }

    function dismissKeyboardCapture() {
      const address = rootEl?.querySelector?.("[data-browser-address]");
      const keyboardInput = rootEl?.querySelector?.("[data-browser-keyboard-input]");
      if (documentRef?.activeElement === keyboardInput) keyboardInput?.blur?.();
      if (documentRef?.activeElement === address) address?.blur?.();
      endKeyboardCapture();
    }

    function focusBrowserKeyboardInput(options = {}) {
      const keyboardInput = rootEl?.querySelector?.("[data-browser-keyboard-input]");
      if ((!frameInputReady && !options.allowBeforeFrame) || !keyboardInput) return;
      keyboardInput.value = "";
      compositionCommitPending = "";
      beginKeyboardCapture({ pin: Boolean(options.pin) });
      keyboardInput.focus?.({ preventScroll: true });
    }

    function syncBrowserKeyboardForRemoteFocus(acceptsTextInput) {
      if (acceptsTextInput) {
        focusBrowserKeyboardInput({ pin: true });
        return;
      }
      if (keyboardPinnedOpen && keyboardCaptureActive) {
        rootEl?.querySelector?.("[data-browser-keyboard-input]")?.focus?.({ preventScroll: true });
        return;
      }
      const keyboardInput = rootEl?.querySelector?.("[data-browser-keyboard-input]");
      if (documentRef?.activeElement === keyboardInput) keyboardInput?.blur?.();
      endKeyboardCapture();
    }

    function releaseKeyboardAfterUserDismiss() {
      if (!keyboardCaptureActive || !keyboardPinnedOpen || !stableViewportHeight) return;
      const visualViewport = windowRef.visualViewport;
      const visibleHeight = (
        Number(visualViewport?.height || 0)
        + Number(visualViewport?.offsetTop || 0)
      );
      if (visibleHeight < stableViewportHeight - 48) {
        keyboardViewportContracted = true;
        return;
      }
      if (!keyboardViewportContracted) return;
      keyboardPinnedOpen = false;
      const keyboardInput = rootEl?.querySelector?.("[data-browser-keyboard-input]");
      if (documentRef?.activeElement === keyboardInput) keyboardInput?.blur?.();
      endKeyboardCapture();
    }

    function renderClosed() {
      manualClosed = true;
      dismissKeyboardCapture();
      setFrameInputReady(false);
      renderTabs([]);
      const image = rootEl?.querySelector?.("[data-browser-frame]");
      const empty = rootEl?.querySelector?.("[data-browser-empty]");
      if (image) image.hidden = true;
      if (empty) {
        empty.textContent = "Browser is closed.";
        empty.hidden = false;
      }
      setError("");
      setStatus("closed", "idle");
      renderControls();
    }

    function clearFrame(message = "Starting shared browser…") {
      const image = rootEl?.querySelector?.("[data-browser-frame]");
      const empty = rootEl?.querySelector?.("[data-browser-empty]");
      dismissKeyboardCapture();
      setFrameInputReady(false);
      if (image) {
        image.hidden = true;
        image.onload = null;
        image.onerror = null;
        image.removeAttribute?.("src");
        if (image.dataset) image.dataset.browserFrameInstance = "";
      }
      if (empty) {
        empty.textContent = message;
        empty.hidden = false;
      }
      renderTabs([]);
      bridgeInstanceId = "";
      frameWidth = 0;
      frameHeight = 0;
      frameImageWidth = 0;
      frameImageHeight = 0;
    }

    function send(action, args = {}) {
      if (manualClosed) {
        setError("The task browser is closed. Select Start to open it.");
        return false;
      }
      if (!socketOpen()) {
        setError("Shared browser is reconnecting.");
        return false;
      }
      if (["mouse", "text", "press"].includes(String(action || "")) && !frameInputReady) {
        setError("Shared browser input is waiting for the current frame.");
        return false;
      }
      requestSequence += 1;
      const requestId = `browser-ui-${requestSequence}`;
      pendingActions.set(requestId, String(action || ""));
      socket.send(JSON.stringify({
        type: "command",
        id: requestId,
        action,
        args
      }));
      return true;
    }

    function renderTabs(tabs = [], activePageId = "") {
      const tabsEl = rootEl?.querySelector?.("[data-browser-tabs]");
      if (!tabsEl) return;
      tabsEl.replaceChildren();
      for (const tab of Array.isArray(tabs) ? tabs : []) {
        const wrapper = documentRef.createElement("span");
        wrapper.className = `browser-tab ${tab?.page_id === activePageId ? "active" : ""}`;
        const select = documentRef.createElement("button");
        select.type = "button";
        select.className = "browser-tab-select";
        select.dataset.browserSelectTab = String(tab?.page_id || "");
        select.title = String(tab?.url || tab?.title || "");
        select.textContent = String(tab?.title || tab?.url || "New tab").slice(0, 48);
        select.setAttribute("role", "tab");
        select.setAttribute("aria-selected", String(tab?.page_id === activePageId));
        const close = documentRef.createElement("button");
        close.type = "button";
        close.className = "browser-tab-close";
        close.dataset.browserCloseTab = String(tab?.page_id || "");
        close.title = "Close tab";
        close.setAttribute("aria-label", "Close tab");
        close.textContent = "×";
        wrapper.append(select, close);
        tabsEl.append(wrapper);
      }
    }

    function renderBookmarks(items = bookmarks) {
      bookmarks = Array.isArray(items) ? items : [];
      const bookmarksEl = rootEl?.querySelector?.("[data-browser-bookmarks]");
      if (bookmarksEl) {
        bookmarksEl.replaceChildren();
        if (!bookmarks.length) {
          const empty = documentRef.createElement("span");
          empty.className = "browser-bookmarks-empty";
          empty.textContent = "No bookmarks";
          bookmarksEl.append(empty);
        } else {
          for (const bookmark of bookmarks) {
            const wrapper = documentRef.createElement("span");
            wrapper.className = "browser-bookmark";
            const open = documentRef.createElement("button");
            open.type = "button";
            open.className = "browser-bookmark-open";
            open.dataset.browserBookmarkUrl = String(bookmark?.url || "");
            open.title = String(bookmark?.url || bookmark?.title || "");
            open.textContent = String(bookmark?.title || bookmark?.url || "Bookmark").slice(0, 40);
            wrapper.append(open);
            bookmarksEl.append(wrapper);
          }
        }
      }
      const currentUrl = String(latestState.url || "");
      const currentBookmarked = bookmarks.some(bookmark => String(bookmark?.url || "") === currentUrl);
      const count = rootEl?.querySelector?.("[data-browser-bookmarks-count]");
      if (count) count.textContent = String(bookmarks.length);
      const toggle = rootEl?.querySelector?.("[data-browser-bookmark-toggle]");
      if (toggle) {
        toggle.textContent = currentBookmarked ? "★" : "☆";
        toggle.classList?.toggle?.("active", currentBookmarked);
        toggle.setAttribute("aria-pressed", String(currentBookmarked));
        toggle.setAttribute(
          "aria-label",
          currentBookmarked ? "Remove current page from bookmarks" : "Bookmark current page"
        );
        toggle.setAttribute(
          "title",
          currentBookmarked ? "Remove current page from bookmarks" : "Bookmark current page"
        );
      }
      renderControls();
    }

    async function loadBookmarks(generation = mountGeneration) {
      if (!taskId || typeof deps.fetchJson !== "function") return;
      const requestedTaskId = taskId;
      try {
        const payload = await deps.fetchJson(
          deps.apiUrl?.(`/api/task/${encodeURIComponent(requestedTaskId)}/browser-bookmarks`)
            || `/api/task/${encodeURIComponent(requestedTaskId)}/browser-bookmarks`,
          {},
          "Failed to load browser bookmarks"
        );
        if (generation !== mountGeneration || requestedTaskId !== taskId) return;
        renderBookmarks(payload?.items);
      } catch (error) {
        if (generation !== mountGeneration || requestedTaskId !== taskId) return;
        setError(error?.message || String(error));
      }
    }

    async function updateBookmark(action, fields = {}) {
      if (bookmarksBusy || !taskId || typeof deps.fetchJson !== "function") return;
      const requestedTaskId = taskId;
      const generation = mountGeneration;
      bookmarksBusy = true;
      renderControls();
      try {
        const payload = await deps.fetchJson(
          deps.apiUrl?.(`/api/task/${encodeURIComponent(requestedTaskId)}/browser-bookmarks`)
            || `/api/task/${encodeURIComponent(requestedTaskId)}/browser-bookmarks`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action, ...fields })
          },
          "Failed to update browser bookmarks"
        );
        if (generation !== mountGeneration || requestedTaskId !== taskId) return;
        setError("");
        renderBookmarks(payload?.items);
      } catch (error) {
        if (generation !== mountGeneration || requestedTaskId !== taskId) return;
        setError(error?.message || String(error));
      } finally {
        if (generation === mountGeneration && requestedTaskId === taskId) {
          bookmarksBusy = false;
          renderControls();
        }
      }
    }

    function renderDisplay(state = {}) {
      const runtime = state?.display && typeof state.display === "object"
        ? state.display
        : {};
      const active = String(runtime.active || "embedded");
      const nativeActive = active === "native";
      rootEl?.querySelectorAll?.("[data-browser-embedded-session]")?.forEach(element => {
        element.hidden = nativeActive;
      });
      const nativeSession = rootEl?.querySelector?.("[data-browser-native-session]");
      if (nativeSession) nativeSession.hidden = !nativeActive;
      const nativeMessage = rootEl?.querySelector?.("[data-browser-native-message]");
      if (nativeMessage) {
        const productLabel = String(state.browser_product || (
          state.runtime === "user_chrome" ? "User Chrome" : "Playwright Chromium"
        ));
        const profileLabel = state.profile === "named"
          ? `named profile · ${state.profile_name || "unnamed"}`
          : state.profile === "task"
            ? "persistent task profile"
            : "ephemeral profile";
        nativeMessage.textContent = nativeActive
          ? `${productLabel} · active on the AHA host desktop · ${profileLabel}`
          : "";
      }
      return nativeActive;
    }

    function renderState(state = {}) {
      latestState = state && typeof state === "object" ? state : {};
      const stateDeviceMode = String(latestState.device_mode || "");
      if (["desktop", "mobile"].includes(stateDeviceMode)) {
        deviceMode = stateDeviceMode;
        if (rootEl?.dataset) rootEl.dataset.browserConfigDeviceMode = stateDeviceMode;
      }
      manualClosed = false;
      const nativeActive = renderDisplay(latestState);
      const address = rootEl?.querySelector?.("[data-browser-address]");
      if (address && documentRef?.activeElement !== address) address.value = String(latestState.url || "");
      renderTabs(latestState.tabs, latestState.page_id);
      renderBookmarks();
      setStatus(
        nativeActive
          ? "native window"
          : latestState.user_active
            ? "user controlling"
            : "live",
        "running"
      );
      renderControls();
    }

    function renderFrame(payload = {}) {
      const payloadInstanceId = String(payload.instance_id || "");
      if (
        bridgeInstanceId
        && payloadInstanceId !== bridgeInstanceId
      ) return;
      if (payload.page_id && latestState.page_id && payload.page_id !== latestState.page_id) return;
      const image = rootEl?.querySelector?.("[data-browser-frame]");
      const empty = rootEl?.querySelector?.("[data-browser-empty]");
      if (!image || !payload.data) return;
      const nextFrameWidth = Math.max(1, Number(payload.width || latestState.viewport?.width || 1280));
      const nextFrameHeight = Math.max(1, Number(payload.height || latestState.viewport?.height || 720));
      const nextImageWidth = Math.max(1, Number(payload.image_width || nextFrameWidth));
      const nextImageHeight = Math.max(1, Number(payload.image_height || nextFrameHeight));
      const frameSource = `data:${payload.mime || "image/jpeg"};base64,${payload.data}`;
      const sameFrameInstance = (
        frameInputReady
        && String(image.dataset?.browserFrameInstance || "") === payloadInstanceId
      );
      if (!sameFrameInstance) setFrameInputReady(false);
      image.onload = () => {
        if (!rootEl || image.src !== frameSource) return;
        if (bridgeInstanceId && payloadInstanceId !== bridgeInstanceId) return;
        frameWidth = nextFrameWidth;
        frameHeight = nextFrameHeight;
        frameImageWidth = Math.max(1, Number(image.naturalWidth || nextImageWidth));
        frameImageHeight = Math.max(1, Number(image.naturalHeight || nextImageHeight));
        if (image.dataset) image.dataset.browserFrameInstance = payloadInstanceId;
        image.hidden = false;
        setError("");
        if (empty) empty.hidden = true;
        setFrameInputReady(true);
      };
      image.onerror = () => {
        if (image.src !== frameSource) return;
        setFrameInputReady(false);
        image.hidden = true;
        if (empty) {
          empty.textContent = "Unable to decode shared browser frame.";
          empty.hidden = false;
        }
      };
      image.src = frameSource;
      if (sameFrameInstance) image.hidden = false;
    }

    function handlePayload(payload = {}) {
      if (payload.type === "ready") {
        setError("");
        setFrameInputReady(false);
        bridgeInstanceId = String(payload.state?.instance_id || "");
        renderState(payload.state || {});
        if (payload.state?.display?.active !== "native") {
          send("screenshot", { type: "jpeg", quality: 70, full_page: false });
        }
        return;
      }
      if (payload.type === "event") {
        if (payload.event === "state") {
          const stateInstanceId = String(payload.state?.instance_id || "");
          if (!bridgeInstanceId || !stateInstanceId || stateInstanceId === bridgeInstanceId) {
            renderState(payload.state || {});
          }
        }
        else if (payload.event === "frame") renderFrame(payload);
        else if (payload.event === "frame_error") setError(payload.message || "Unable to capture browser frame.");
        else if (payload.message) setError(payload.message);
        return;
      }
      if (payload.type === "result") {
        const completedAction = pendingActions.get(String(payload.id || "")) || "";
        pendingActions.delete(String(payload.id || ""));
        if (!payload.ok) {
          const code = String(payload.error?.code || "");
          const message = String(payload.error?.message || "Browser action failed.");
          setError(code ? `${code}: ${message}` : message);
        } else {
          setError("");
          if (
            completedAction === "mouse"
            && Object.prototype.hasOwnProperty.call(payload.result || {}, "accepts_text_input")
          ) {
            syncBrowserKeyboardForRemoteFocus(Boolean(payload.result.accepts_text_input));
          }
          if (payload.result?.data && String(payload.result?.mime || "").startsWith("image/")) {
            renderFrame({
              ...payload.result,
              instance_id: payload.result.instance_id || bridgeInstanceId,
              width: latestState.viewport?.width || frameWidth,
              height: latestState.viewport?.height || frameHeight
            });
          }
          if (payload.result?.status === "running" || payload.result?.tabs) renderState(payload.result);
        }
        return;
      }
      if (payload.type === "error") {
        if (String(payload.code || "") === "browser_closed") {
          renderClosed();
          return;
        }
        setError(payload.message || "Browser session failed.");
      }
    }

    function openSocket(generation) {
      if (!WebSocketImpl || !taskId || manualClosed || generation !== mountGeneration) {
        if (!WebSocketImpl) setError("WebSocket is unavailable in this browser.");
        return;
      }
      setStatus("connecting…", "idle");
      let candidate;
      try {
        candidate = new WebSocketImpl(browserWsUrl());
        socket = candidate;
      } catch (error) {
        setError(error?.message || String(error));
        scheduleReconnect(generation);
        return;
      }
      candidate.addEventListener("open", () => {
        if (generation !== mountGeneration || candidate !== socket) return;
        setStatus("connected", "running");
      });
      candidate.addEventListener("message", event => {
        if (generation !== mountGeneration || candidate !== socket) return;
        try {
          handlePayload(JSON.parse(String(event.data || "{}")));
        } catch (_error) {
          setError("Shared browser returned an invalid message.");
        }
      });
      candidate.addEventListener("error", () => {
        if (generation === mountGeneration && candidate === socket && !manualClosed) {
          setStatus("connection error", "failed");
        }
      });
      candidate.addEventListener("close", () => {
        if (generation !== mountGeneration || candidate !== socket) return;
        socket = null;
        if (manualClosed) return;
        setStatus("reconnecting…", "idle");
        scheduleReconnect(generation);
      });
    }

    function scheduleReconnect(generation) {
      if (reconnectTimer || generation !== mountGeneration || !taskId || manualClosed) return;
      reconnectTimer = windowRef.setTimeout(() => {
        reconnectTimer = 0;
        openSocket(generation);
      }, RECONNECT_DELAY_MS);
    }

    function closeSocket() {
      const active = socket;
      socket = null;
      pendingActions.clear();
      if (!active) return;
      try {
        active.send(JSON.stringify({ type: "close" }));
        active.close();
      } catch (_error) {
        // The task Browser Bridge may already be gone.
      }
    }

    function runtimeSettingValue(key) {
      return String(
        rootEl?.querySelector?.(`[data-browser-runtime-field="${key}"]`)?.value || ""
      ).trim();
    }

    function syncRuntimeProxySettings() {
      const proxyMode = runtimeSettingValue("proxy_mode") || "direct";
      const custom = rootEl?.querySelector?.("[data-browser-runtime-proxy-custom]");
      if (custom) custom.hidden = proxyMode !== "custom";
    }

    function applyRuntimeSettings(policy = {}) {
      const set = (key, value) => {
        const input = rootEl?.querySelector?.(`[data-browser-runtime-field="${key}"]`);
        if (input) input.value = value ?? "";
      };
      set("runtime", policy.runtime || "playwright");
      set("display", policy.display || "native");
      set("downloads", policy.downloads || "deny");
      set("uploads", policy.uploads || "deny");
      set("proxy_mode", policy.proxy_mode || "direct");
      set("proxy_server", policy.proxy_server || "");
      set("proxy_bypass", policy.proxy_bypass || "");
      set("proxy_username", policy.proxy_username || "");
      set("proxy_password", "");
      const password = rootEl?.querySelector?.('[data-browser-runtime-field="proxy_password"]');
      if (password) {
        password.placeholder = policy.proxy_password_configured
          ? "Configured; leave blank to keep"
          : "";
      }
      const clearPassword = rootEl?.querySelector?.("[data-browser-runtime-clear-password]");
      if (clearPassword) clearPassword.checked = false;
      if (rootEl?.dataset) {
        rootEl.dataset.browserRuntime = policy.runtime || "playwright";
        rootEl.dataset.browserDisplay = policy.display || "native";
      }
      syncRuntimeProxySettings();
    }

    async function saveRuntimeSettings(event) {
      event?.preventDefault?.();
      if (lifecycleBusy || deviceBusy || typeof deps.fetchJson !== "function") return;
      const shouldRestart = !manualClosed;
      if (
        shouldRestart
        && latestState.profile === "ephemeral"
        && typeof windowRef.confirm === "function"
        && !windowRef.confirm("Saving browser runtime settings restarts this ephemeral browser and discards its login state and history. Continue?")
      ) return;
      const body = {
        runtime: runtimeSettingValue("runtime") || "playwright",
        display: runtimeSettingValue("display") || "native",
        downloads: runtimeSettingValue("downloads") || "deny",
        uploads: runtimeSettingValue("uploads") || "deny",
        proxy_mode: runtimeSettingValue("proxy_mode") || "direct",
        proxy_server: runtimeSettingValue("proxy_server"),
        proxy_bypass: runtimeSettingValue("proxy_bypass"),
        proxy_username: runtimeSettingValue("proxy_username"),
        restart_browser: shouldRestart
      };
      const proxyPassword = String(
        rootEl?.querySelector?.('[data-browser-runtime-field="proxy_password"]')?.value || ""
      );
      if (proxyPassword) body.proxy_password = proxyPassword;
      if (rootEl?.querySelector?.("[data-browser-runtime-clear-password]")?.checked) {
        body.clear_proxy_password = true;
      }
      lifecycleBusy = true;
      if (shouldRestart) {
        clearFrame("Applying browser settings…");
        closeSocket();
      }
      renderControls();
      try {
        const payload = await deps.fetchJson(
          deps.apiUrl?.(`/api/task/${encodeURIComponent(taskId)}/browser-control`)
            || `/api/task/${encodeURIComponent(taskId)}/browser-control`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
          },
          "Failed to update browser settings"
        );
        const policy = payload?.task?.browser_control;
        if (policy) applyRuntimeSettings(policy);
        rootEl?.querySelector?.("[data-browser-status-settings]")?.removeAttribute?.("open");
        setError("");
        if (shouldRestart) {
          manualClosed = false;
          if (reconnectTimer) windowRef.clearTimeout(reconnectTimer);
          reconnectTimer = 0;
          openSocket(mountGeneration);
        }
      } catch (error) {
        setError(error?.message || String(error));
        if (shouldRestart) {
          manualClosed = false;
          scheduleReconnect(mountGeneration);
        }
      } finally {
        lifecycleBusy = false;
        renderControls();
      }
    }

    async function setDeviceMode(nextMode) {
      const normalized = String(nextMode || "").toLowerCase();
      if (!["desktop", "mobile"].includes(normalized) || normalized === deviceMode || deviceBusy) return;
      if (typeof deps.fetchJson !== "function") {
        setError("Browser settings API is unavailable.");
        return;
      }
      if (
        latestState.profile === "ephemeral"
        && typeof windowRef.confirm === "function"
        && !windowRef.confirm("Switching device mode restarts this ephemeral browser and discards its login state and history. Continue?")
      ) return;
      const previousMode = deviceMode;
      deviceBusy = true;
      lifecycleBusy = true;
      deviceMode = normalized;
      if (rootEl?.dataset) rootEl.dataset.browserConfigDeviceMode = normalized;
      clearFrame(`Restarting in ${normalized} mode…`);
      closeSocket();
      renderControls();
      try {
        const payload = await deps.fetchJson(
          deps.apiUrl?.(`/api/task/${encodeURIComponent(taskId)}/browser-control`)
            || `/api/task/${encodeURIComponent(taskId)}/browser-control`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              device_mode: normalized,
              restart_browser: true
            })
          },
          "Failed to change browser device mode"
        );
        if (payload?.task?.browser_control?.device_mode) {
          deviceMode = String(payload.task.browser_control.device_mode) === "mobile"
            ? "mobile"
            : "desktop";
        }
        manualClosed = false;
        setError("");
        if (reconnectTimer) windowRef.clearTimeout(reconnectTimer);
        reconnectTimer = 0;
        openSocket(mountGeneration);
      } catch (error) {
        deviceMode = previousMode;
        if (rootEl?.dataset) rootEl.dataset.browserConfigDeviceMode = previousMode;
        setError(error?.message || String(error));
        manualClosed = false;
        scheduleReconnect(mountGeneration);
      } finally {
        deviceBusy = false;
        lifecycleBusy = false;
        renderControls();
      }
    }

    async function runLifecycle(action) {
      const command = String(action || "").toLowerCase();
      if (!["start", "restart", "close"].includes(command) || lifecycleBusy) return;
      if (typeof deps.fetchJson !== "function") {
        setError("Browser lifecycle API is unavailable.");
        return;
      }
      if (
        latestState.profile === "ephemeral"
        && command !== "start"
        && !windowRef.confirm?.("This browser uses an ephemeral profile. Restarting or closing it will discard its login state and history. Continue?")
      ) return;
      lifecycleBusy = true;
      renderControls();
      setError("");
      setStatus(command === "close" ? "closing…" : `${command}ing…`, "idle");
      try {
        const payload = await deps.fetchJson(
          deps.apiUrl?.(`/api/task/${encodeURIComponent(taskId)}/browser-session`)
            || `/api/task/${encodeURIComponent(taskId)}/browser-session`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: command })
          },
          `Failed to ${command} browser`
        );
        if (command === "close" || payload?.bridge?.status === "closed") {
          manualClosed = true;
          closeSocket();
          renderClosed();
        } else {
          manualClosed = false;
          clearFrame(command === "restart" ? "Restarting shared browser…" : "Starting shared browser…");
          if (reconnectTimer) windowRef.clearTimeout(reconnectTimer);
          reconnectTimer = 0;
          closeSocket();
          openSocket(mountGeneration);
        }
      } catch (error) {
        setError(error?.message || String(error));
        if (!manualClosed) scheduleReconnect(mountGeneration);
      } finally {
        lifecycleBusy = false;
        renderControls();
      }
    }

    function viewportCoordinates(event) {
      const image = rootEl?.querySelector?.("[data-browser-frame]");
      if (
        !frameInputReady
        || !bridgeInstanceId
        || String(image?.dataset?.browserFrameInstance || "") !== bridgeInstanceId
      ) return null;
      const rect = image?.getBoundingClientRect?.();
      if (!rect || !rect.width || !rect.height) return null;
      const actualImageWidth = Math.max(1, Number(image.naturalWidth || frameImageWidth || frameWidth));
      const actualImageHeight = Math.max(1, Number(image.naturalHeight || frameImageHeight || frameHeight));
      const frameRatio = actualImageWidth / actualImageHeight;
      const elementRatio = rect.width / rect.height;
      const contentWidth = elementRatio > frameRatio ? rect.height * frameRatio : rect.width;
      const contentHeight = elementRatio > frameRatio ? rect.height : rect.width / frameRatio;
      const contentLeft = rect.left + (rect.width - contentWidth) / 2;
      const contentTop = rect.top + (rect.height - contentHeight) / 2;
      const localX = Number(event.clientX) - contentLeft;
      const localY = Number(event.clientY) - contentTop;
      if (
        !Number.isFinite(localX)
        || !Number.isFinite(localY)
        || localX < 0
        || localY < 0
        || localX > contentWidth
        || localY > contentHeight
      ) return null;
      return {
        x: Math.max(0, Math.min(frameWidth, localX * frameWidth / contentWidth)),
        y: Math.max(0, Math.min(frameHeight, localY * frameHeight / contentHeight))
      };
    }

    function commandKey(event) {
      const aliases = { " ": "Space", Esc: "Escape", Del: "Delete" };
      const raw = aliases[event.key] || event.key;
      const modifierOnly = new Set(["Control", "Shift", "Alt", "Meta"]);
      if (modifierOnly.has(raw)) return "";
      const parts = [];
      if (event.ctrlKey) parts.push("Control");
      if (event.altKey) parts.push("Alt");
      if (event.shiftKey && raw.length > 1) parts.push("Shift");
      if (event.metaKey) parts.push("Meta");
      parts.push(raw);
      return parts.join("+");
    }

    function listen(target, type, handler, options) {
      if (!target?.addEventListener) return;
      target.addEventListener(type, handler, options);
      listeners.push(() => target.removeEventListener?.(type, handler, options));
    }

    function closestTarget(target, selector) {
      return typeof target?.closest === "function" ? target.closest(selector) : null;
    }

    function activateRootControl(event) {
      const bookmarkToggle = closestTarget(event.target, "[data-browser-bookmark-toggle]");
      if (bookmarkToggle) {
        if (bookmarkToggle.disabled) return true;
        const url = String(latestState.url || "");
        if (/^https?:\/\//i.test(url)) {
          void updateBookmark("toggle", { url, title: String(latestState.title || url) });
        }
        return true;
      }
      const bookmarkOpen = closestTarget(event.target, "[data-browser-bookmark-url]");
      if (bookmarkOpen) {
        if (bookmarkOpen.disabled) return true;
        const url = String(bookmarkOpen.dataset.browserBookmarkUrl || "");
        if (url) {
          send("navigate", { url });
          rootEl?.querySelector?.("[data-browser-bookmarks-popover]")?.removeAttribute?.("open");
        }
        return true;
      }
      const deviceButton = closestTarget(event.target, "[data-browser-device-mode]");
      if (deviceButton) {
        if (deviceButton.disabled) return true;
        void setDeviceMode(deviceButton.dataset.browserDeviceMode);
        return true;
      }
      const lifecycleButton = closestTarget(event.target, "[data-browser-lifecycle]");
      if (lifecycleButton) {
        if (lifecycleButton.disabled) return true;
        void runLifecycle(lifecycleButton.dataset.browserLifecycle);
        return true;
      }
      const actionButton = closestTarget(event.target, "[data-browser-action]");
      if (actionButton) {
        if (actionButton.disabled) return true;
        const action = actionButton.dataset.browserAction;
        const args = action === "new_tab"
          ? { url: String(rootEl?.dataset?.browserStartUrl || "").trim() }
          : {};
        send(action, args);
        return true;
      }
      const select = closestTarget(event.target, "[data-browser-select-tab]");
      if (select) {
        if (select.disabled) return true;
        send("select_tab", { page_id: select.dataset.browserSelectTab || "" });
        return true;
      }
      const close = closestTarget(event.target, "[data-browser-close-tab]");
      if (close) {
        if (close.disabled) return true;
        send("close_tab", { page_id: close.dataset.browserCloseTab || "" });
        return true;
      }
      return false;
    }

    function activateTouchControl(event) {
      if (Date.now() < suppressControlClickUntil && event.target === suppressControlTarget) {
        event.preventDefault();
        return;
      }
      if (!activateRootControl(event)) return;
      event.preventDefault();
      suppressControlClickUntil = Date.now() + 700;
      suppressControlTarget = event.target;
    }

    function bindRoot() {
      const addressForm = rootEl?.querySelector?.("[data-browser-address-form]");
      const stage = rootEl?.querySelector?.("[data-browser-input-surface]");
      const keyboardInput = rootEl?.querySelector?.("[data-browser-keyboard-input]");
      const runtimeSettings = rootEl?.querySelector?.("[data-browser-runtime-settings]");
      const runtimeSettingsCancel = rootEl?.querySelector?.("[data-browser-runtime-settings-cancel]");
      const statusSettings = rootEl?.querySelector?.("[data-browser-status-settings]");
      const bookmarksPopover = rootEl?.querySelector?.("[data-browser-bookmarks-popover]");
      const flushTouchScroll = (touch, force = false) => {
        if (!touchGesture || !touch) return;
        if (
          Math.abs(touchGesture.pendingDeltaX) < 0.5
          && Math.abs(touchGesture.pendingDeltaY) < 0.5
        ) return;
        const now = Date.now();
        if (!force && now - lastWheelAt < WHEEL_THROTTLE_MS) return;
        const point = viewportCoordinates(touch);
        if (!point) return;
        send("mouse", {
          event: "wheel",
          ...point,
          delta_x: touchGesture.pendingDeltaX * TOUCH_SCROLL_MULTIPLIER,
          delta_y: touchGesture.pendingDeltaY * TOUCH_SCROLL_MULTIPLIER
        });
        touchGesture.pendingDeltaX = 0;
        touchGesture.pendingDeltaY = 0;
        lastWheelAt = now;
      };
      let keyboardMirrorValue = "";
      const resetKeyboardInput = () => {
        if (keyboardInput) keyboardInput.value = "";
        keyboardMirrorValue = "";
      };
      const forwardKeyboardValue = (nextValue, inputData = null) => {
        const next = String(nextValue || "");
        const explicitText = typeof inputData === "string" ? inputData : "";
        if (keyboardMirrorValue === next && !explicitText) return;
        const text = explicitText || next.slice(keyboardMirrorValue.length);
        if (text) send("text", { text });
        keyboardMirrorValue = next;
      };
      listen(addressForm, "submit", event => {
        event.preventDefault();
        const url = browserAddressTarget(rootEl?.querySelector?.("[data-browser-address]")?.value);
        if (url) send("navigate", { url });
      });
      listen(runtimeSettings, "submit", event => {
        void saveRuntimeSettings(event);
      });
      listen(runtimeSettings, "change", event => {
        if (event.target?.matches?.('[data-browser-runtime-field="proxy_mode"]')) {
          syncRuntimeProxySettings();
        }
      });
      listen(runtimeSettingsCancel, "click", () => {
        rootEl?.querySelector?.("[data-browser-status-settings]")?.removeAttribute?.("open");
      });
      listen(bookmarksPopover, "toggle", () => {
        if (bookmarksPopover?.open) statusSettings?.removeAttribute?.("open");
      });
      listen(statusSettings, "toggle", () => {
        if (statusSettings?.open) bookmarksPopover?.removeAttribute?.("open");
      });
      listen(rootEl, "focusin", event => {
        if (browserKeyboardControl(event.target)) beginKeyboardCapture();
      });
      listen(rootEl, "focusout", event => {
        if (!browserKeyboardControl(event.target)) return;
        windowRef.setTimeout(() => {
          if (!browserKeyboardControl(documentRef?.activeElement)) endKeyboardCapture();
        }, 0);
      });
      listen(keyboardInput, "focus", beginKeyboardCapture);
      listen(keyboardInput, "blur", () => {
        windowRef.setTimeout(() => {
          if (!browserKeyboardControl(documentRef?.activeElement)) endKeyboardCapture();
        }, 0);
      });
      listen(windowRef.visualViewport, "resize", releaseKeyboardAfterUserDismiss);
      listen(rootEl, "pointerup", event => {
        if (event.pointerType !== "touch") return;
        activateTouchControl(event);
      });
      listen(rootEl, "touchend", activateTouchControl, { passive: false });
      listen(rootEl, "click", event => {
        if (Date.now() < suppressControlClickUntil && event.target === suppressControlTarget) {
          suppressControlTarget = null;
          return;
        }
        if (activateRootControl(event)) return;
        if (event.target?.matches?.("[data-browser-frame]")) {
          if (!frameInputReady) return;
          if (Date.now() < suppressFrameClickUntil) return;
          const point = viewportCoordinates(event);
          if (point) send("mouse", { event: "click", ...point, button: "left", click_count: event.detail || 1 });
        }
      });
      listen(stage, "pointerdown", event => {
        if (!frameInputReady) return;
        if (event.button !== undefined && event.button !== 0) return;
        if (
          event.pointerType === "touch"
          && keyboardCaptureActive
          && event.target?.matches?.("[data-browser-frame]")
        ) {
          event.preventDefault();
          keyboardInput?.focus?.({ preventScroll: true });
          return;
        }
      });
      listen(stage, "contextmenu", event => {
        if (!frameInputReady) return;
        if (!event.target?.matches?.("[data-browser-frame]")) return;
        event.preventDefault();
        const point = viewportCoordinates(event);
        if (point) send("mouse", { event: "click", ...point, button: "right", click_count: 1 });
      });
      listen(stage, "wheel", event => {
        if (!frameInputReady) return;
        const now = Date.now();
        if (now - lastWheelAt < WHEEL_THROTTLE_MS) return;
        lastWheelAt = now;
        const point = viewportCoordinates(event);
        if (!point) return;
        event.preventDefault();
        send("mouse", { event: "wheel", ...point, delta_x: event.deltaX, delta_y: event.deltaY });
      }, { passive: false });
      listen(stage, "touchstart", event => {
        if (!frameInputReady) return;
        if (keyboardCaptureActive && event.target?.matches?.("[data-browser-frame]")) {
          event.preventDefault();
          keyboardInput?.focus?.({ preventScroll: true });
        }
        const touch = event.touches?.[0];
        touchGesture = touch ? {
          lastX: touch.clientX,
          lastY: touch.clientY,
          pendingDeltaX: 0,
          pendingDeltaY: 0,
          moved: false
        } : null;
      }, { passive: false });
      listen(stage, "touchmove", event => {
        if (!frameInputReady) return;
        const touch = event.touches?.[0];
        if (!touch || !touchGesture) return;
        const deltaX = touchGesture.lastX - touch.clientX;
        const deltaY = touchGesture.lastY - touch.clientY;
        if (Math.abs(deltaX) < 2 && Math.abs(deltaY) < 2) return;
        event.preventDefault();
        touchGesture.moved = true;
        touchGesture.pendingDeltaX += deltaX;
        touchGesture.pendingDeltaY += deltaY;
        touchGesture.lastX = touch.clientX;
        touchGesture.lastY = touch.clientY;
        flushTouchScroll(touch);
      }, { passive: false });
      listen(stage, "touchend", event => {
        if (!frameInputReady) return;
        const gesture = touchGesture;
        const touch = event.changedTouches?.[0] || {
          clientX: gesture?.lastX,
          clientY: gesture?.lastY
        };
        if (gesture?.moved) {
          event.preventDefault();
          flushTouchScroll(touch, true);
          touchGesture = null;
          return;
        }
        touchGesture = null;
        if (!gesture || !event.target?.matches?.("[data-browser-frame]")) return;
        const point = viewportCoordinates(touch);
        if (!point) return;
        event.preventDefault();
        suppressFrameClickUntil = Date.now() + 700;
        send("mouse", { event: "click", ...point, button: "left", click_count: 1 });
        if (keyboardCaptureActive) keyboardInput?.focus?.({ preventScroll: true });
      }, { passive: false });
      listen(keyboardInput, "beforeinput", event => {
        if (composingText || event.isComposing) return;
        const inputType = String(event.inputType || "");
        if (inputType === "deleteContentBackward" || inputType === "deleteContentForward") {
          event.preventDefault();
          send("press", { key: inputType.endsWith("Forward") ? "Delete" : "Backspace" });
          resetKeyboardInput();
        } else if (inputType === "insertLineBreak" || inputType === "insertParagraph") {
          event.preventDefault();
          send("press", { key: "Enter" });
          resetKeyboardInput();
        }
      });
      listen(keyboardInput, "compositionstart", () => {
        composingText = true;
      });
      listen(keyboardInput, "compositionend", event => {
        composingText = false;
        const currentValue = String(keyboardInput?.value || "");
        if (event.data) {
          compositionCommitPending = String(event.data);
          send("text", { text: compositionCommitPending });
        } else if (currentValue) {
          send("text", { text: currentValue });
        }
        resetKeyboardInput();
      });
      listen(keyboardInput, "input", event => {
        if (composingText || event.isComposing) return;
        if (compositionCommitPending) {
          compositionCommitPending = "";
          resetKeyboardInput();
          return;
        }
        forwardKeyboardValue(keyboardInput?.value || "", event.data);
        resetKeyboardInput();
      });
      listen(keyboardInput, "keydown", event => {
        if (event.isComposing || event.key === "Process" || event.key === "Unidentified") return;
        if (event.key.length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey) return;
        if ((event.ctrlKey || event.metaKey) && String(event.key).toLowerCase() === "v") return;
        const key = commandKey(event);
        if (!key) return;
        if (
          event.key === "Backspace"
          || event.key === "Delete"
          || event.key === "Enter"
          || event.key === "Tab"
          || event.key === "Escape"
        ) {
          event.preventDefault();
        }
        send("press", { key });
        if (event.key === "Backspace" || event.key === "Delete") resetKeyboardInput();
      });
    }

    function unmount(options = {}) {
      const preserveKeyboardCapture = Boolean(options.preserveKeyboardCapture && keyboardCaptureActive);
      const preserveDefaultKeyboardFocus = Boolean(options.preserveDefaultKeyboardFocus);
      mountGeneration += 1;
      taskId = "";
      if (reconnectTimer) windowRef.clearTimeout(reconnectTimer);
      reconnectTimer = 0;
      for (const dispose of listeners.splice(0)) dispose();
      closeSocket();
      if (!preserveKeyboardCapture) dismissKeyboardCapture();
      rootEl = null;
      latestState = {};
      bridgeInstanceId = "";
      frameWidth = 1280;
      frameHeight = 720;
      frameImageWidth = 1280;
      frameImageHeight = 720;
      frameInputReady = false;
      if (!preserveKeyboardCapture) composingText = false;
      touchGesture = null;
      suppressFrameClickUntil = 0;
      suppressControlClickUntil = 0;
      suppressControlTarget = null;
      deviceMode = "desktop";
      manualClosed = false;
      deviceBusy = false;
      lifecycleBusy = false;
      bookmarksBusy = false;
      bookmarks = [];
      if (!preserveDefaultKeyboardFocus) defaultKeyboardFocusAttempted = false;
      if (!preserveKeyboardCapture) {
        keyboardCaptureActive = false;
        keyboardPinnedOpen = false;
        keyboardViewportContracted = false;
        stableViewportHeight = 0;
        compositionCommitPending = "";
      }
      pendingActions.clear();
    }

    function mount(nextTaskId) {
      const nextRoot = panelEl?.querySelector?.("[data-browser-session-root]");
      const nextId = String(nextTaskId || "");
      if (!nextRoot || !nextId) {
        unmount();
        return;
      }
      if (rootEl === nextRoot && taskId === nextId) return;
      const preserveKeyboardCapture = Boolean(
        rootEl
        && taskId === nextId
        && keyboardCaptureActive
      );
      const preserveDefaultKeyboardFocus = Boolean(
        rootEl
        && taskId === nextId
        && defaultKeyboardFocusAttempted
      );
      unmount({ preserveKeyboardCapture, preserveDefaultKeyboardFocus });
      rootEl = nextRoot;
      taskId = nextId;
      deviceMode = String(rootEl.dataset?.browserConfigDeviceMode || "") === "mobile" ? "mobile" : "desktop";
      manualClosed = false;
      const generation = mountGeneration;
      bindRoot();
      renderBookmarks([]);
      if (preserveKeyboardCapture) focusBrowserKeyboardInput();
      renderControls();
      void loadBookmarks(generation);
      openSocket(generation);
    }

    function focus() {
      if (defaultKeyboardFocusAttempted) return;
      defaultKeyboardFocusAttempted = true;
      if (mobileViewportMatches()) {
        focusBrowserKeyboardInput({ allowBeforeFrame: true, pin: true });
        return;
      }
      rootEl?.querySelector?.("[data-browser-input-surface]")?.focus?.({ preventScroll: true });
    }

    return Object.freeze({
      focus,
      isMounted: () => Boolean(rootEl && taskId),
      mount,
      send,
      unmount
    });
  }

  window.AHABrowserSession = Object.freeze({ createBrowserSessionController });
})();
