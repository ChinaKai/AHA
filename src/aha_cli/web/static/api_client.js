(() => {
  function createApiClient(options = {}) {
    const requestTimeoutMs = Number(options.requestTimeoutMs) || 30000;
    const currentRunId = () => String(options.currentRunId?.() || "").trim();

    async function readJsonResponse(res, fallbackMessage = "Request failed") {
      const payload = await res.json().catch(() => null);
      if (!res.ok) {
        const status = [res.status, res.statusText].filter(Boolean).join(" ");
        const detail = payload?.error || status || fallbackMessage;
        const error = new Error(`${fallbackMessage}: ${detail}`);
        error.status = res.status;
        error.payload = payload;
        throw error;
      }
      return payload || {};
    }

    async function fetchWithTimeout(url, fetchOptions = {}, timeoutMs = requestTimeoutMs) {
      const controller = new AbortController();
      const init = { ...fetchOptions, signal: fetchOptions.signal || controller.signal };
      const timer = fetchOptions.signal ? null : setTimeout(() => controller.abort(), timeoutMs);
      try {
        return await fetch(url, init);
      } catch (err) {
        if (err?.name === "AbortError") {
          throw new Error(`Request timed out after ${timeoutMs}ms: ${url}`);
        }
        throw err;
      } finally {
        if (timer) clearTimeout(timer);
      }
    }

    async function fetchJson(url, fetchOptions = {}, fallbackMessage = "Request failed") {
      const res = await fetchWithTimeout(url, fetchOptions);
      return readJsonResponse(res, fallbackMessage);
    }

    function isRequestTimeoutError(err) {
      return String(err?.message || err || "").includes("Request timed out after");
    }

    function isAuthRequiredError(err) {
      return Number(err?.status) === 401;
    }

    function apiUrl(path, params = {}, urlOptions = {}) {
      const query = new URLSearchParams();
      const source = params instanceof URLSearchParams ? params : new URLSearchParams(params);
      for (const [key, value] of source.entries()) {
        if (value !== null && value !== undefined && value !== "") query.set(key, value);
      }
      const runId = currentRunId();
      if (urlOptions.runScoped !== false && runId) query.set("run_id", runId);
      const suffix = query.toString();
      return suffix ? `${path}?${suffix}` : path;
    }

    return Object.freeze({
      readJsonResponse,
      fetchWithTimeout,
      fetchJson,
      isRequestTimeoutError,
      isAuthRequiredError,
      apiUrl
    });
  }

  window.AHAApiClient = Object.freeze({ createApiClient });
})();
