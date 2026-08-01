(() => {
  function escapeFallback(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function createFeishuConsoleController(elements = {}, deps = {}) {
    const escapeHtml = deps.escapeHtml || escapeFallback;
    let open = false;
    let loading = false;
    let savingNotifications = false;
    let loaded = false;
    let error = "";
    let notice = "";
    let status = {};

    function t(key, fallback) {
      return window.AHAI18n?.t?.(key, fallback) || fallback;
    }

    function yesNo(value) {
      return value ? t("common.yes", "Yes") : t("common.no", "No");
    }

    function connectionState() {
      if (loading && !loaded) return "loading";
      return String(status.runtime?.status || (status.enabled ? "unknown" : "disabled"));
    }

    function connectionClass(value) {
      if (value === "connected") return "connected";
      if (["connecting", "loading", "unknown"].includes(value)) return "pending";
      if (["error", "sdk_missing", "not_configured"].includes(value)) return "error";
      return "disabled";
    }

    function metric(label, value, options = {}) {
      const content = options.code
        ? `<code>${escapeHtml(value || "-")}</code>`
        : `<strong>${escapeHtml(value || "-")}</strong>`;
      return `<div class="feishu-console-metric"><span>${escapeHtml(label)}</span>${content}</div>`;
    }

    function setupMessage() {
      if (loading && !loaded) return t("feishu.loading", "Loading Feishu status...");
      if (!status.enabled) return t("feishu.disabled_hint", "Enable Feishu in AHA Settings, then restart the Web service.");
      if (!status.sdk_installed) return t("feishu.sdk_missing_hint", "Install the Feishu Channel SDK, then restart the Web service.");
      if (!status.configured) return t("feishu.not_configured_hint", "Configure App ID and App Secret in AHA Settings, then restart the Web service.");
      if (connectionState() !== "connected") return t("feishu.connection_hint", "Check the runtime error and Feishu application event subscription settings.");
      return t("feishu.connected_hint", "The Feishu assistant is ready to receive messages and push subscribed task replies.");
    }

    function renderPopover() {
      const popover = elements.feishuConsolePopoverEl;
      if (!popover) return;
      const runtime = status.runtime && typeof status.runtime === "object" ? status.runtime : {};
      const runtimeState = connectionState();
      const runtimeError = String(runtime.error || "");
      const command = String(status.install_command || 'python3 -m pip install -e ".[feishu]"');
      popover.innerHTML = `
        <section class="feishu-console-panel">
          <div class="feishu-console-head">
            <div>
              <h3>${escapeHtml(t("feishu.title", "Feishu assistant"))}</h3>
              <p>${escapeHtml(t("feishu.subtitle", "Long-connection status, access policy, and task message delivery."))}</p>
            </div>
            <div class="feishu-console-actions">
              <button type="button" data-feishu-action="refresh" ${loading ? "disabled" : ""}>${escapeHtml(loading ? t("feishu.refreshing", "Refreshing...") : t("common.refresh", "Refresh"))}</button>
              <button class="button-ghost" type="button" data-feishu-action="close">${escapeHtml(t("common.close", "Close"))}</button>
            </div>
          </div>
          ${error ? `<div class="feishu-console-message error">${escapeHtml(error)}</div>` : ""}
          ${notice ? `<div class="feishu-console-message success">${escapeHtml(notice)}</div>` : ""}
          <div class="feishu-console-status-line">
            <span class="feishu-console-badge ${escapeHtml(connectionClass(runtimeState))}">${escapeHtml(runtimeState)}</span>
            <span>${escapeHtml(setupMessage())}</span>
          </div>
          <div class="feishu-console-grid">
            ${metric(t("feishu.enabled", "Enabled"), yesNo(Boolean(status.enabled)))}
            ${metric(t("feishu.configured", "Credentials configured"), yesNo(Boolean(status.configured)))}
            ${metric(t("feishu.sdk_installed", "Channel SDK installed"), yesNo(Boolean(status.sdk_installed)))}
            ${metric(t("feishu.app_id", "App ID"), String(status.app_id || "-"), { code: true })}
            ${metric(t("feishu.allowed_users", "Allowed users"), String(status.allowed_open_id_count ?? 0))}
            ${metric(t("feishu.group_mentions", "Group @ required"), yesNo(Boolean(status.group_mentions_only)))}
            ${metric(t("feishu.security_mode", "Security mode"), String(status.security_mode || "audit"), { code: true })}
          </div>
          <div class="feishu-console-notifications">
            <label class="checkbox-line">
              <input type="checkbox" data-feishu-notifications-toggle ${status.notifications_enabled ? "checked" : ""} ${loading || savingNotifications ? "disabled" : ""}>
              <span>${escapeHtml(t("feishu.notifications_toggle", "Push task status changes to Feishu"))}</span>
            </label>
            <small>${escapeHtml(t("feishu.notifications_hint", "Pushes task changes from every run with the triggering user message, agent reply, or system reason. Direct chat replies remain enabled when this is off."))}</small>
          </div>
          ${runtimeError ? `<div class="feishu-console-message error"><strong>${escapeHtml(t("feishu.runtime_error", "Runtime error"))}</strong><span>${escapeHtml(runtimeError)}</span></div>` : ""}
          ${status.sdk_installed ? "" : `<div class="feishu-console-command"><span>${escapeHtml(t("feishu.install_command", "Install command"))}</span><code>${escapeHtml(command)}</code></div>`}
          <p class="feishu-console-footnote">${escapeHtml(t("feishu.secret_hint", "App Secret may come from AHA Settings or the configured environment fallback and is never shown here."))}</p>
        </section>
      `;
    }

    async function loadStatus() {
      if (loading) return;
      loading = true;
      error = "";
      notice = "";
      renderPopover();
      try {
        const payload = await deps.fetchJson?.(
          deps.apiUrl?.("/api/feishu", {}, { runScoped: false }) || "/api/feishu",
          {},
          t("feishu.load_failed", "Failed to load Feishu status")
        );
        status = payload?.feishu && typeof payload.feishu === "object" ? payload.feishu : {};
        loaded = true;
      } catch (err) {
        error = err?.message || String(err || t("feishu.load_failed", "Failed to load Feishu status"));
      } finally {
        loading = false;
        if (open) renderPopover();
      }
    }

    async function setNotificationsEnabled(enabled) {
      if (savingNotifications) return;
      const previous = Boolean(status.notifications_enabled);
      status = { ...status, notifications_enabled: Boolean(enabled) };
      savingNotifications = true;
      error = "";
      notice = "";
      renderPopover();
      try {
        const payload = await deps.fetchJson?.(
          deps.apiUrl?.("/api/feishu/notifications", {}, { runScoped: false }) || "/api/feishu/notifications",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: Boolean(enabled) })
          },
          t("feishu.notifications_failed", "Failed to update task status notifications")
        );
        status = payload?.feishu && typeof payload.feishu === "object" ? payload.feishu : status;
        notice = status.notifications_enabled
          ? t("feishu.notifications_enabled", "Task status notifications enabled")
          : t("feishu.notifications_disabled", "Task status notifications disabled");
      } catch (err) {
        status = { ...status, notifications_enabled: previous };
        error = err?.message || String(err || t("feishu.notifications_failed", "Failed to update task status notifications"));
      } finally {
        savingNotifications = false;
        if (open) renderPopover();
      }
    }

    function setOpen(nextOpen) {
      open = Boolean(nextOpen && elements.feishuConsolePopoverEl);
      if (!elements.feishuConsolePopoverEl) return;
      if (open) {
        deps.setRunMaintenanceConsoleOpen?.(false);
        deps.setObserveProxyOpen?.(false);
        deps.setLocalTerminalOpen?.(false);
        deps.setPlayConsoleOpen?.(false);
        deps.setSkillsConsoleOpen?.(false);
        deps.setTokenUsageOpen?.(false);
        deps.setWeixinConsoleOpen?.(false);
      }
      elements.sessionMenuEl?.classList?.toggle("feishu-open", open);
      elements.feishuConsolePopoverEl.hidden = !open;
      elements.feishuConsoleEl?.setAttribute("aria-expanded", String(open));
      if (open) {
        renderPopover();
        void loadStatus();
      } else {
        elements.feishuConsolePopoverEl.innerHTML = "";
      }
    }

    elements.feishuConsolePopoverEl?.addEventListener("click", event => {
      event.stopPropagation();
      const target = event.target instanceof Element ? event.target : null;
      const action = target?.closest("[data-feishu-action]")?.getAttribute("data-feishu-action") || "";
      if (action === "close") setOpen(false);
      if (action === "refresh") void loadStatus();
    });
    elements.feishuConsolePopoverEl?.addEventListener("change", event => {
      const target = event.target instanceof HTMLInputElement ? event.target : null;
      if (target?.matches("[data-feishu-notifications-toggle]")) void setNotificationsEnabled(target.checked);
    });
    deps.windowRef?.addEventListener?.("aha:languagechange", () => {
      if (open) renderPopover();
    });

    return Object.freeze({
      isOpen: () => open,
      loadFeishuStatus: loadStatus,
      renderFeishuConsolePopover: renderPopover,
      setFeishuConsoleOpen: setOpen
    });
  }

  window.AHAFeishuConsole = Object.freeze({ createFeishuConsoleController });
})();
