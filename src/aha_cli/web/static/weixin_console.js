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
    const payload = context.status || {};
    const pairing = payload.pairing || {};
    const account = payload.account || {};
    const accountId = String(account.user_id || pairing.user_id || "未配对");
    const paired = Boolean(payload.paired);
    const status = String(context.pairingStatus || "");
    const notifications = payload.notifications || {};
    const notificationsEnabled = Boolean(notifications.enabled);
    const sendContext = payload.send_context || {};
    const contextState = String(sendContext.state || "");
    const contextAgeSeconds = Number(sendContext.age_seconds);
    const contextAgeText = Number.isFinite(contextAgeSeconds) ? formatDuration(contextAgeSeconds * 1000) : "";
    const contextUpdatedAt = String(sendContext.updated_at || "").trim();
    const contextStatusText = !paired ? "等待配对" : ({
      fresh: "会话可回复",
      stale: "需微信发消息",
      missing: "需微信发消息",
      unknown: "会话状态未知",
      not_paired: "等待配对"
    }[contextState] || "已配对");
    const contextDetail = (() => {
      if (!paired) return "配对后检测会话状态";
      if (contextState === "fresh") {
        return contextUpdatedAt
          ? `最近刷新 ${formatLocalTimestamp(contextUpdatedAt, contextUpdatedAt)} · ${contextAgeText}`
          : "最近会话可用";
      }
      if (contextState === "stale") {
        return contextUpdatedAt
          ? `上次刷新 ${formatLocalTimestamp(contextUpdatedAt, contextUpdatedAt)} · ${contextAgeText}`
          : "会话已过期";
      }
      if (contextState === "missing") return "从微信发任意消息后刷新";
      return "刷新状态后重试";
    })();
    const statusText = {
      idle: "未配对",
      waiting: "等待扫码",
      scanned: "已扫码，等待确认",
      paired: "已配对",
      expired: "二维码已过期"
    }[status] || status;
    const qrSvg = pairing.qrcode_svg || "";
    const qrSrc = qrSvg ? `data:image/svg+xml;charset=utf-8,${encodeURIComponent(qrSvg)}` : "";
    const pairingActive = ["waiting", "scanned"].includes(status);
    const displayPaired = paired && !pairingActive;
    const pairButtonLabel = pairingActive ? "重新生成二维码" : displayPaired ? "已配对" : "配对";
    const pairButtonDisabled = context.loading || displayPaired;
    const canSendTest = paired && !context.sending && !context.loading;
    const notificationToggleDisabled = !paired || context.loading || context.togglingNotifications;
    const receivedMessages = Array.isArray(payload.received_messages) ? payload.received_messages.slice(0, 3) : [];
    const receivedList = receivedMessages.length ? `
    <ol class="weixin-received-list">
      ${receivedMessages.map(message => {
        const text = String(message?.text || "").trim() || "(非文本消息)";
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
  ` : `<div class="weixin-received-empty">${paired ? "暂无接收消息" : "配对后显示最近接收消息"}</div>`;
    return `
    <div class="weixin-console">
      <div class="weixin-console-head">
        <div>
          <h3>微信操作台</h3>
          <p>当前 Run: ${escapeHtml(context.currentRunId || "-")}</p>
        </div>
        <span class="status ${displayPaired ? "completed" : "session"}">${escapeHtml(statusText)}</span>
      </div>
      <div class="weixin-console-actions">
        <button type="button" data-weixin-action="pair" ${pairButtonDisabled ? "disabled" : ""}>${pairButtonLabel}</button>
        <button type="button" data-weixin-action="refresh" ${context.loading ? "disabled" : ""}>刷新状态</button>
        <button class="danger" type="button" data-weixin-action="reset" ${context.loading ? "disabled" : ""}>重置</button>
      </div>
      ${context.loading ? '<div class="weixin-console-note">正在连接微信服务...</div>' : ""}
      ${context.error ? `<div class="weixin-console-note error">${escapeHtml(context.error)}</div>` : ""}
      ${payload.receive_error ? `<div class="weixin-console-note error">接收消息失败：${escapeHtml(payload.receive_error)}</div>` : ""}
      ${context.notice ? `<div class="weixin-console-note success">${escapeHtml(context.notice)}</div>` : ""}
      ${qrSrc && status !== "paired" ? `
        <div class="weixin-qr">
          <img src="${escapeHtml(qrSrc)}" alt="微信配对二维码">
          <p>${status === "scanned" ? "已扫码，请在微信里确认授权。" : "用微信扫码并确认授权，页面会自动刷新配对状态。"}</p>
          ${pairing.qrcode_payload ? `<a href="${escapeHtml(pairing.qrcode_payload)}" target="_blank" rel="noreferrer">二维码无法识别时打开链接</a>` : ""}
        </div>
      ` : ""}
      <div class="weixin-console-grid">
        <section>
          <strong>账号</strong>
          <code class="weixin-account-id" title="${escapeHtml(accountId)}">${escapeHtml(accountId)}</code>
        </section>
        <section>
          <strong>通道</strong>
          <code class="weixin-session-state ${sendContext.requires_user_message ? "warning" : ""}">${escapeHtml(contextStatusText)}</code>
          <small>${escapeHtml(contextDetail)}</small>
        </section>
      </div>
      <div class="weixin-received">
        <div class="weixin-received-head">
          <strong>最近接收消息</strong>
          <span>最多 3 条</span>
        </div>
        ${receivedList}
      </div>
      <div class="weixin-notifications">
        <label class="checkbox-line">
          <input type="checkbox" data-weixin-notifications-toggle ${notificationsEnabled ? "checked" : ""} ${notificationToggleDisabled ? "disabled" : ""}>
          <span>微信通知</span>
        </label>
        <small>${paired ? "状态变更、等待用户、完成摘要会推送到当前微信。" : "配对成功后可开启任务通知。"}</small>
      </div>
      <label class="weixin-test">
        <span>测试通知</span>
        <textarea data-weixin-test-message rows="3">${escapeHtml(context.testMessage)}</textarea>
      </label>
      <button type="button" data-weixin-action="test" ${canSendTest ? "" : "disabled"}>${context.sending ? "发送中..." : "发送测试通知"}</button>
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
      testMessage: "AHA 微信通知测试"
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
        const payload = await deps.fetchJson?.(deps.apiUrl?.("/api/weixin"), {}, "加载微信状态失败");
        state.status = payload;
        state.loaded = true;
        state.error = payload?.error || "";
      } catch (err) {
        state.error = err?.message || String(err || "加载微信状态失败");
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
        }, "生成微信配对二维码失败");
        state.status = payload;
        state.loaded = true;
      } catch (err) {
        state.error = err?.message || String(err || "生成微信配对二维码失败");
      } finally {
        state.loading = false;
        renderPopover();
      }
    }

    async function resetPairing() {
      if (!currentRunId() || state.loading) return;
      const confirmed = await deps.confirmDialogAction?.({
        title: "重置微信配对？",
        message: "当前账号、二维码、入站同步状态和微信通知开关都会清除。",
        confirmLabel: "重置配对",
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
        }, "重置微信配对失败");
        state.status = payload;
        state.loaded = true;
        state.notice = "微信配对已重置";
      } catch (err) {
        state.error = err?.message || String(err || "重置微信配对失败");
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
        }, "发送微信测试通知失败");
        state.notice = "测试通知已发送";
        await loadStatus({ silent: true });
      } catch (err) {
        state.error = err?.message || String(err || "发送微信测试通知失败");
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
        }, "更新微信通知开关失败");
        state.status = { ...(state.status || {}), notifications: payload?.notifications || {} };
        state.notice = payload?.notifications?.enabled ? "微信通知已开启" : "微信通知已关闭";
      } catch (err) {
        state.error = err?.message || String(err || "更新微信通知开关失败");
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
