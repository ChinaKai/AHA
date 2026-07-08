(() => {
  function createAuthController(elements = {}, options = {}) {
    const tokenParamNames = options.tokenParamNames || ["token", "aha_token"];
    let required = false;
    let loginInFlight = false;
    let message = "";
    let messageIsError = false;

    function t(key, fallback = "") {
      return window.AHAI18n?.t?.(key, fallback) || fallback;
    }

    function renderLoginState(nextMessage = message, isError = messageIsError) {
      required = true;
      message = nextMessage || "";
      messageIsError = Boolean(isError);
      options.setBootstrapError?.("");
      elements.body?.classList.add("auth-required", "empty-run");
      elements.loginViewEl?.classList.remove("hidden");
      if (elements.loginStateEl) {
        elements.loginStateEl.textContent = message;
        elements.loginStateEl.classList.toggle("error", messageIsError);
      }
      const submit = elements.loginFormEl?.querySelector('button[type="submit"]');
      if (submit) submit.disabled = loginInFlight;
      if (elements.loginTokenEl) elements.loginTokenEl.disabled = loginInFlight;
      if (elements.loginTokenEl && document.activeElement !== elements.loginTokenEl && !elements.loginTokenEl.value) {
        window.setTimeout(() => elements.loginTokenEl.focus(), 0);
      }
      options.closeRealtime?.();
    }

    function clearLoginState() {
      required = false;
      loginInFlight = false;
      message = "";
      messageIsError = false;
      elements.body?.classList.remove("auth-required");
      elements.loginViewEl?.classList.add("hidden");
      if (elements.loginStateEl) {
        elements.loginStateEl.textContent = "";
        elements.loginStateEl.classList.remove("error");
      }
      const submit = elements.loginFormEl?.querySelector('button[type="submit"]');
      if (submit) submit.disabled = false;
      if (elements.loginTokenEl) elements.loginTokenEl.disabled = false;
    }

    function scrubAuthTokenFromUrl() {
      if (!window.history?.replaceState) return;
      const url = new URL(window.location.href);
      let changed = false;
      for (const name of tokenParamNames) {
        if (url.searchParams.has(name)) {
          url.searchParams.delete(name);
          changed = true;
        }
      }
      if (changed) window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    }

    async function submitLoginForm() {
      if (loginInFlight) return;
      const token = String(elements.loginTokenEl?.value || "").trim();
      if (!token) {
        renderLoginState(t("auth.token_required", "Enter a token."), true);
        return;
      }
      loginInFlight = true;
      renderLoginState(t("auth.logging_in", "Logging in..."));
      try {
        await options.fetchJson?.("/api/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token })
        }, t("auth.login_failed", "Login failed"));
        if (elements.loginTokenEl) elements.loginTokenEl.value = "";
        clearLoginState();
        await options.afterLogin?.();
      } catch (err) {
        const text = options.isAuthRequiredError?.(err) ? t("auth.token_invalid", "Token is incorrect. Try again.") : (err?.message || String(err || t("auth.login_failed", "Login failed")));
        renderLoginState(text, true);
      } finally {
        loginInFlight = false;
        if (required) renderLoginState(message, messageIsError);
      }
    }

    async function logoutAuthSession() {
      try {
        await options.fetchJson?.("/api/logout", { method: "POST" }, t("auth.logout_failed", "Logout failed"));
      } catch (_err) {
        // The local cookie should still be considered invalid for this page session.
      }
      options.afterLogout?.();
      renderLoginState(t("auth.logged_out", "Logged out."));
    }

    return Object.freeze({
      renderLoginState,
      clearLoginState,
      scrubAuthTokenFromUrl,
      submitLoginForm,
      logoutAuthSession,
      isRequired: () => required,
      loginInFlight: () => loginInFlight
    });
  }

  window.AHAAuthController = Object.freeze({ createAuthController });
})();
