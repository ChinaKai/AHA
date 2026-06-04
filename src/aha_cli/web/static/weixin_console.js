(() => {
  function escapeFallback(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function renderWeixinConsole(context = {}) {
    const escapeHtml = context.escapeHtml || escapeFallback;
    const formatDuration = context.formatDuration || (value => String(value || ""));
    const formatLocalTimestamp = context.formatLocalTimestamp || (value => String(value || ""));
    const t = window.AHAI18n?.t || ((_, fallback) => fallback);
    const formatText = window.AHAI18n?.format || ((_, __, fallback) => fallback);
    const payload = context.status || {};
    const pairing = payload.pairing || {};
    const account = payload.account || {};
    const accountId = String(account.user_id || pairing.user_id || t("weixin.unpaired", "not paired"));
    const paired = Boolean(payload.paired);
    const status = String(context.pairingStatus || "");
    const notifications = payload.notifications || {};
    const notificationsEnabled = Boolean(notifications.enabled);
    const sendContext = payload.send_context || {};
    const contextState = String(sendContext.state || "");
    const contextAgeSeconds = Number(sendContext.age_seconds);
    const contextAgeText = Number.isFinite(contextAgeSeconds) ? formatDuration(contextAgeSeconds * 1000) : "";
    const contextUpdatedAt = String(sendContext.updated_at || "").trim();
    const contextStatusText = !paired ? t("weixin.context_status_not_paired", "waiting for pairing") : ({
      fresh: t("weixin.context_status_fresh", "session can reply"),
      stale: t("weixin.context_status_stale", "needs a Weixin message"),
      missing: t("weixin.context_status_missing", "needs a Weixin message"),
      unknown: t("weixin.context_status_unknown", "session state unknown"),
      not_paired: t("weixin.context_status_not_paired", "waiting for pairing")
    }[contextState] || t("weixin.context_status_paired", "paired"));
    const contextDetail = (() => {
      if (!paired) return t("weixin.context_detail_check_after_pair", "Check session state after pairing");
      if (contextState === "fresh") {
        return contextUpdatedAt
          ? formatText("weixin.context_recent", { time: formatLocalTimestamp(contextUpdatedAt, contextUpdatedAt), age: contextAgeText }, `recent refresh ${formatLocalTimestamp(contextUpdatedAt, contextUpdatedAt)} · ${contextAgeText}`)
          : t("weixin.context_detail_available", "Recent session available");
      }
      if (contextState === "stale") {
        return contextUpdatedAt
          ? formatText("weixin.context_previous", { time: formatLocalTimestamp(contextUpdatedAt, contextUpdatedAt), age: contextAgeText }, `previous refresh ${formatLocalTimestamp(contextUpdatedAt, contextUpdatedAt)} · ${contextAgeText}`)
          : t("weixin.context_detail_expired", "Session expired");
      }
      if (contextState === "missing") return t("weixin.context_detail_send_message", "Send any message from Weixin, then refresh");
      return t("weixin.context_detail_refresh", "Refresh status and retry");
    })();
    const statusText = {
      idle: t("weixin.status_idle", "not paired"),
      waiting: t("weixin.status_waiting", "waiting for scan"),
      scanned: t("weixin.status_scanned", "scanned, waiting for confirmation"),
      paired: t("weixin.status_paired", "paired"),
      expired: t("weixin.status_expired", "QR code expired")
    }[status] || status;
    const qrSvg = pairing.qrcode_svg || "";
    const qrSrc = qrSvg ? `data:image/svg+xml;charset=utf-8,${encodeURIComponent(qrSvg)}` : "";
    const pairingActive = ["waiting", "scanned"].includes(status);
    const displayPaired = paired && !pairingActive;
    const pairButtonLabel = pairingActive ? t("weixin.regenerate", "Regenerate QR code") : displayPaired ? t("weixin.status_paired", "paired") : t("weixin.pair", "Pair");
    const pairButtonDisabled = context.loading || displayPaired;
    const canSendTest = paired && !context.sending && !context.loading;
    const notificationToggleDisabled = !paired || context.loading || context.togglingNotifications;
    const receivedMessages = Array.isArray(payload.received_messages) ? payload.received_messages.slice(0, 3) : [];
    const receivedList = receivedMessages.length ? `
    <ol class="weixin-received-list">
      ${receivedMessages.map(message => {
        const text = String(message?.text || "").trim() || t("weixin.non_text", "(non-text message)");
        const receivedAt = String(message?.received_at || "").trim();
        const time = receivedAt ? formatLocalTimestamp(receivedAt, receivedAt) : "";
        return `
          <li>
            ${time ? `<div class="weixin-received-meta"><time>${escapeHtml(time)}</time></div>` : ""}
            <p>${escapeHtml(text)}</p>
          </li>
        `;
      }).join("")}
    </ol>
  ` : `<div class="weixin-received-empty">${paired ? escapeHtml(t("weixin.empty_received_paired", "No received messages yet")) : escapeHtml(t("weixin.empty_received_unpaired", "Recent received messages appear after pairing"))}</div>`;
    return `
    <div class="weixin-console">
      <div class="weixin-console-head">
        <div>
          <h3>${escapeHtml(t("weixin.title", "Weixin console"))}</h3>
          <p>${escapeHtml(t("weixin.current_run", "Current run"))}: ${escapeHtml(context.currentRunId || "-")}</p>
        </div>
        <span class="status ${displayPaired ? "completed" : "session"}">${escapeHtml(statusText)}</span>
      </div>
      <div class="weixin-console-actions">
        <button type="button" data-weixin-action="pair" ${pairButtonDisabled ? "disabled" : ""}>${pairButtonLabel}</button>
        <button type="button" data-weixin-action="refresh" ${context.loading ? "disabled" : ""}>${escapeHtml(t("weixin.refresh", "Refresh status"))}</button>
        <button class="danger" type="button" data-weixin-action="reset" ${context.loading ? "disabled" : ""}>${escapeHtml(t("weixin.reset", "Reset"))}</button>
      </div>
      ${context.loading ? `<div class="weixin-console-note">${escapeHtml(t("weixin.console_loading", "Connecting to Weixin service..."))}</div>` : ""}
      ${context.error ? `<div class="weixin-console-note error">${escapeHtml(context.error)}</div>` : ""}
      ${payload.receive_error ? `<div class="weixin-console-note error">${escapeHtml(formatText("weixin.receive_error", { message: payload.receive_error }, `Failed to receive messages: ${payload.receive_error}`))}</div>` : ""}
      ${context.notice ? `<div class="weixin-console-note success">${escapeHtml(context.notice)}</div>` : ""}
      ${qrSrc && status !== "paired" ? `
        <div class="weixin-qr">
          <img src="${escapeHtml(qrSrc)}" alt="${escapeHtml(t("weixin.qr_alt", "Weixin pairing QR code"))}">
          <p>${status === "scanned" ? escapeHtml(t("weixin.authorize_scanned", "Scanned. Confirm authorization in Weixin.")) : escapeHtml(t("weixin.pairing_default", "Scan with Weixin and confirm authorization. Pairing status refreshes automatically."))}</p>
          ${pairing.qrcode_payload ? `<a href="${escapeHtml(pairing.qrcode_payload)}" target="_blank" rel="noreferrer">${escapeHtml(t("weixin.link_qr", "Open link if the QR code cannot be recognized"))}</a>` : ""}
        </div>
      ` : ""}
      <div class="weixin-console-grid">
        <section>
          <strong>${escapeHtml(t("weixin.account", "Account"))}</strong>
          <code class="weixin-account-id" title="${escapeHtml(accountId)}">${escapeHtml(accountId)}</code>
        </section>
        <section>
          <strong>${escapeHtml(t("weixin.channel", "Channel"))}</strong>
          <code class="weixin-session-state ${sendContext.requires_user_message ? "warning" : ""}">${escapeHtml(contextStatusText)}</code>
          <small>${escapeHtml(contextDetail)}</small>
        </section>
      </div>
      <div class="weixin-received">
        <div class="weixin-received-head">
          <strong>${escapeHtml(t("weixin.received_messages", "Recent received messages"))}</strong>
          <span>${escapeHtml(t("weixin.max_received", "up to 3"))}</span>
        </div>
        ${receivedList}
      </div>
      <div class="weixin-notifications">
        <label class="checkbox-line">
          <input type="checkbox" data-weixin-notifications-toggle ${notificationsEnabled ? "checked" : ""} ${notificationToggleDisabled ? "disabled" : ""}>
          <span>${escapeHtml(t("weixin.notifications", "Weixin notifications"))}</span>
        </label>
        <small>${paired ? escapeHtml(t("weixin.notifications_hint_paired", "Status changes, waiting-for-user states, and final summaries are pushed to the current Weixin.")) : escapeHtml(t("weixin.notifications_hint_unpaired", "Enable task notifications after pairing."))}</small>
      </div>
      <label class="weixin-test">
        <span>${escapeHtml(t("weixin.test_label", "Test notification"))}</span>
        <textarea data-weixin-test-message rows="3">${escapeHtml(context.testMessage)}</textarea>
      </label>
      <button type="button" data-weixin-action="test" ${canSendTest ? "" : "disabled"}>${context.sending ? escapeHtml(t("weixin.sending", "Sending...")) : escapeHtml(t("weixin.send_test", "Send test notification"))}</button>
    </div>
  `;
  }

  function createWeixinConsoleController(elements = {}, deps = {}) {
    let open = false;
    let pollTimer = null;
    const state = {
      loaded: false,
      loading: false,
      sending: false,
      togglingNotifications: false,
      error: "",
      notice: "",
      status: null,
      testMessage: window.AHAI18n?.t?.("weixin.test_message", "AHA Weixin notification test") || "AHA Weixin notification test"
    };

    function currentRunId() {
      return String(deps.currentRunId?.() || "").trim();
    }

    function clearPoll() {
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    }

    function pairingStatus() {
      const status = state.status || {};
      return status.pairing?.status || (status.paired ? "paired" : "idle");
    }

    function schedulePoll() {
      clearPoll();
      if (!open) return;
      if (state.loading || state.sending) return;
      if (!["waiting", "scanned"].includes(pairingStatus())) return;
      pollTimer = setTimeout(() => {
        void loadStatus({ silent: true });
      }, 2000);
    }

    function renderConsole() {
      return renderWeixinConsole({
        currentRunId: currentRunId(),
        error: state.error,
        escapeHtml: deps.escapeHtml,
        formatDuration: deps.formatDuration,
        formatLocalTimestamp: deps.formatLocalTimestamp,
        loading: state.loading,
        notice: state.notice,
        pairingStatus: pairingStatus(),
        sending: state.sending,
        status: state.status || {},
        testMessage: state.testMessage,
        togglingNotifications: state.togglingNotifications
      });
    }

    function renderPopover() {
      if (!elements.weixinConsolePopoverEl) return;
      elements.weixinConsolePopoverEl.innerHTML = renderConsole();
      schedulePoll();
    }

    function setOpen(nextOpen) {
      open = Boolean(nextOpen && currentRunId() && elements.weixinConsolePopoverEl);
      if (!elements.weixinConsolePopoverEl) return;
      if (open) {
        deps.setRunMaintenanceConsoleOpen?.(false);
        deps.setPlayConsoleOpen?.(false);
      }
      elements.sessionMenuEl?.classList.toggle("weixin-open", open);
      if (open) {
        renderPopover();
        elements.weixinConsolePopoverEl.hidden = false;
        void loadStatus({ silent: state.loaded });
      } else {
        clearPoll();
        elements.weixinConsolePopoverEl.hidden = true;
        elements.weixinConsolePopoverEl.innerHTML = "";
      }
      elements.weixinConsoleEl?.setAttribute("aria-expanded", String(open));
    }

    function closeForOutsideEvent(event) {
      if (!open) return;
      const target = event.target instanceof Element ? event.target : null;
      if (elements.weixinConsoleEl?.contains(target) || elements.weixinConsolePopoverEl?.contains(target)) return;
      setOpen(false);
    }

    async function loadStatus(options = {}) {
      if (!currentRunId()) return;
      const silent = Boolean(options.silent);
      if (!silent) {
        state.loading = true;
        renderPopover();
      }
      try {
        const payload = await deps.fetchJson?.(deps.apiUrl?.("/api/weixin"), {}, window.AHAI18n?.t?.("weixin.load_failed", "Failed to load Weixin status") || "Failed to load Weixin status");
        state.status = payload;
        state.loaded = true;
        state.error = payload?.error || "";
      } catch (err) {
        state.error = err?.message || String(err || (window.AHAI18n?.t?.("weixin.load_failed", "Failed to load Weixin status") || "Failed to load Weixin status"));
      } finally {
        state.loading = false;
        renderPopover();
      }
    }

    async function startPairing() {
      if (!currentRunId() || state.loading) return;
      state.loading = true;
      state.error = "";
      state.notice = "";
      renderPopover();
      try {
        const payload = await deps.fetchJson?.(deps.apiUrl?.("/api/weixin/pair"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({})
        }, window.AHAI18n?.t?.("weixin.pair_failed", "Failed to generate Weixin pairing QR code") || "Failed to generate Weixin pairing QR code");
        state.status = payload;
        state.loaded = true;
      } catch (err) {
        state.error = err?.message || String(err || (window.AHAI18n?.t?.("weixin.pair_failed", "Failed to generate Weixin pairing QR code") || "Failed to generate Weixin pairing QR code"));
      } finally {
        state.loading = false;
        renderPopover();
      }
    }

    async function resetPairing() {
      if (!currentRunId() || state.loading) return;
      const confirmed = await deps.confirmDialogAction?.({
        title: window.AHAI18n?.t?.("weixin.reset_title", "Reset Weixin pairing?") || "Reset Weixin pairing?",
        message: window.AHAI18n?.t?.("weixin.reset_confirm_message", "The current account, QR code, inbound sync state, and Weixin notification switch will be cleared.") || "The current account, QR code, inbound sync state, and Weixin notification switch will be cleared.",
        confirmLabel: window.AHAI18n?.t?.("weixin.confirm_reset", "Reset pairing") || "Reset pairing",
        danger: true,
        details: [["Run", currentRunId()]]
      });
      if (!confirmed) return;
      state.loading = true;
      state.error = "";
      state.notice = "";
      renderPopover();
      try {
        const payload = await deps.fetchJson?.(deps.apiUrl?.("/api/weixin/reset"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({})
        }, window.AHAI18n?.t?.("weixin.reset_failed", "Failed to reset Weixin pairing") || "Failed to reset Weixin pairing");
        state.status = payload;
        state.loaded = true;
        state.notice = window.AHAI18n?.t?.("weixin.notice_pair_reset", "Weixin pairing reset") || "Weixin pairing reset";
      } catch (err) {
        state.error = err?.message || String(err || (window.AHAI18n?.t?.("weixin.reset_failed", "Failed to reset Weixin pairing") || "Failed to reset Weixin pairing"));
      } finally {
        state.loading = false;
        renderPopover();
      }
    }

    async function sendTestNotification() {
      if (!currentRunId() || state.sending) return;
      state.sending = true;
      state.error = "";
      state.notice = "";
      renderPopover();
      try {
        await deps.fetchJson?.(deps.apiUrl?.("/api/weixin/test"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: state.testMessage })
        }, window.AHAI18n?.t?.("weixin.test_failed", "Failed to send Weixin test notification") || "Failed to send Weixin test notification");
        state.notice = window.AHAI18n?.t?.("weixin.notice_test_sent", "Test notification sent") || "Test notification sent";
        await loadStatus({ silent: true });
      } catch (err) {
        state.error = err?.message || String(err || (window.AHAI18n?.t?.("weixin.test_failed", "Failed to send Weixin test notification") || "Failed to send Weixin test notification"));
      } finally {
        state.sending = false;
        renderPopover();
      }
    }

    async function setNotificationsEnabled(enabled) {
      if (!currentRunId() || state.togglingNotifications) return;
      state.togglingNotifications = true;
      state.error = "";
      state.notice = "";
      renderPopover();
      try {
        const payload = await deps.fetchJson?.(deps.apiUrl?.("/api/weixin/notifications"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: Boolean(enabled) })
        }, window.AHAI18n?.t?.("weixin.notifications_failed", "Failed to update Weixin notifications") || "Failed to update Weixin notifications");
        state.status = { ...(state.status || {}), notifications: payload?.notifications || {} };
        state.notice = payload?.notifications?.enabled
          ? (window.AHAI18n?.t?.("weixin.notice_enabled", "Weixin notifications enabled") || "Weixin notifications enabled")
          : (window.AHAI18n?.t?.("weixin.notice_disabled", "Weixin notifications disabled") || "Weixin notifications disabled");
      } catch (err) {
        state.error = err?.message || String(err || (window.AHAI18n?.t?.("weixin.notifications_failed", "Failed to update Weixin notifications") || "Failed to update Weixin notifications"));
        await loadStatus({ silent: true });
      } finally {
        state.togglingNotifications = false;
        renderPopover();
      }
    }

    function bind() {
      elements.weixinConsolePopoverEl?.addEventListener("click", event => {
        const target = event.target instanceof Element ? event.target : null;
        const actionEl = target?.closest("[data-weixin-action]");
        const action = actionEl?.getAttribute("data-weixin-action") || "";
        if (action === "pair") void startPairing();
        if (action === "refresh") void loadStatus();
        if (action === "reset") void resetPairing();
        if (action === "test") void sendTestNotification();
      });
      elements.weixinConsolePopoverEl?.addEventListener("input", event => {
        const target = event.target instanceof HTMLTextAreaElement ? event.target : null;
        if (target?.matches("[data-weixin-test-message]")) state.testMessage = target.value;
      });
      elements.weixinConsolePopoverEl?.addEventListener("change", event => {
        const target = event.target instanceof HTMLInputElement ? event.target : null;
        if (target?.matches("[data-weixin-notifications-toggle]")) void setNotificationsEnabled(target.checked);
      });
      elements.documentRef?.addEventListener("pointerdown", closeForOutsideEvent, true);
    }

    return Object.freeze({
      bind,
      clearWeixinPoll: clearPoll,
      isOpen: () => open,
      loadWeixinStatus: loadStatus,
      renderWeixinConsole: renderConsole,
      renderWeixinConsolePopover: renderPopover,
      setWeixinConsoleOpen: setOpen
    });
  }

  window.AHAWeixinConsole = Object.freeze({ createWeixinConsoleController, renderWeixinConsole });
})();
