(() => {
  function localAccessControlFallback(locationObj = window.location) {
    const hostname = String(locationObj.hostname || "").trim();
    const loopback = hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1" || hostname === "[::1]";
    return {
      auth_mode: "none",
      hostname,
      loopback,
      risk_level: loopback ? "low" : (hostname ? "high" : "unknown"),
      recommendation: loopback ? "local loopback access" : "bind to 127.0.0.1 or use SSH/VPN/authenticated proxy"
    };
  }

  function hostPortLabel(host, port) {
    const hostText = String(host || "").trim();
    const portText = String(port || "").trim();
    if (!hostText) return "";
    const labelHost = hostText.includes(":") && !hostText.startsWith("[") ? `[${hostText}]` : hostText;
    return portText ? `${labelHost}:${portText}` : labelHost;
  }

  function browserAccessAddress(locationObj = window.location) {
    return String(locationObj.host || locationObj.hostname || "").trim();
  }

  function accessControlView(payload = {}, options = {}) {
    const locationObj = options.locationObj || window.location;
    const safePayload = payload || localAccessControlFallback(locationObj);
    const risk = String(safePayload.risk_level || "unknown");
    const hostname = String(safePayload.bind_host || safePayload.hostname || locationObj.hostname || "-");
    const authMode = String(safePayload.auth_mode || "none");
    const accessAddress = browserAccessAddress(locationObj);
    const bindAddress = hostPortLabel(safePayload.bind_host, safePayload.bind_port);
    const riskText = risk === "low"
      ? "本地访问"
      : (safePayload.bind_network_visible ? "绑定风险" : (risk === "high" ? "访问风险" : "访问状态"));
    return {
      accessAddress,
      bindAddress,
      risk,
      className: `access-control-status access-${risk}`,
      text: `${riskText} ${hostname} · auth=${authMode}`,
      title: options.error || String(safePayload.recommendation || ""),
      addressText: bindAddress
        ? `访问 ${accessAddress || "-"} · 绑定 ${bindAddress}`
        : `访问 ${accessAddress || "-"}`,
      addressTitle: bindAddress
        ? `当前浏览器访问地址: ${accessAddress || "-"}\n服务绑定地址: ${bindAddress}`
        : `当前浏览器访问地址: ${accessAddress || "-"}`
    };
  }

  function createAccessControlController(elements = {}, deps = {}) {
    function renderWebServiceAddress(payload = deps.accessControlData?.() || {}) {
      if (!elements.webServiceAddressEl) return;
      const view = accessControlView(payload, { error: deps.accessControlError?.() || "" });
      elements.webServiceAddressEl.textContent = view.addressText;
      elements.webServiceAddressEl.title = view.addressTitle;
    }

    function renderAuthSessionControls(payload = deps.accessControlData?.() || {}) {
      if (!elements.authLogoutEl) return;
      const tokenAuth = Boolean(payload?.token_required) || String(payload?.auth_mode || "") === "token";
      elements.authLogoutEl.classList.toggle("hidden", !tokenAuth);
      elements.authLogoutEl.disabled = Boolean(deps.loginInFlight?.());
    }

    function renderAccessControlStatus() {
      if (!elements.accessControlStatusEl) return;
      const payload = deps.accessControlData?.() || localAccessControlFallback();
      const view = accessControlView(payload, { error: deps.accessControlError?.() || "" });
      renderWebServiceAddress(payload);
      elements.accessControlStatusEl.textContent = view.text;
      elements.accessControlStatusEl.title = view.title;
      elements.accessControlStatusEl.className = view.className;
      renderAuthSessionControls(payload);
    }

    async function loadAccessControlStatus() {
      try {
        deps.setAccessControlData?.(await deps.fetchJson?.("/api/access-control", { cache: "no-store" }, "Failed to load access-control status"));
        deps.setAccessControlError?.("");
      } catch (err) {
        if (deps.isAuthRequiredError?.(err)) {
          deps.renderLoginState?.("登录已失效，请重新输入 token。", true);
          return;
        }
        deps.setAccessControlData?.(null);
        deps.setAccessControlError?.(err?.message || String(err || "access-control status unavailable"));
      } finally {
        renderAccessControlStatus();
      }
    }

    return Object.freeze({
      loadAccessControlStatus,
      renderAccessControlStatus,
      renderAuthSessionControls,
      renderWebServiceAddress
    });
  }

  window.AHAAccessControl = Object.freeze({
    accessControlView,
    browserAccessAddress,
    createAccessControlController,
    hostPortLabel,
    localAccessControlFallback
  });
})();
