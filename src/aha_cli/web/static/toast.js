(function () {
  let toastRoot = null;

  function ensureRoot() {
    if (toastRoot && document.body.contains(toastRoot)) return toastRoot;
    toastRoot = document.createElement("div");
    toastRoot.className = "aha-toast-root";
    toastRoot.setAttribute("role", "status");
    toastRoot.setAttribute("aria-live", "polite");
    document.body.appendChild(toastRoot);
    return toastRoot;
  }

  function show(message, options = {}) {
    const text = String(message || "");
    if (!text) return null;
    const type = String(options.type || "info");
    const duration = Number.isFinite(Number(options.duration))
      ? Number(options.duration)
      : (type === "error" ? 5000 : 3200);
    const root = ensureRoot();
    const toast = document.createElement("div");
    toast.className = `aha-toast aha-toast-${type}`;
    toast.textContent = text;
    toast.setAttribute("role", type === "error" ? "alert" : "status");
    root.appendChild(toast);
    const dismiss = () => {
      toast.classList.add("aha-toast-leaving");
      window.setTimeout(() => {
        toast.remove();
        if (!root.childElementCount) root.remove();
      }, 220);
    };
    toast.addEventListener("click", dismiss);
    window.setTimeout(dismiss, duration);
    return dismiss;
  }

  function error(message, duration) {
    return show(message, { type: "error", duration });
  }

  function success(message, duration) {
    return show(message, { type: "success", duration });
  }

  function info(message, duration) {
    return show(message, { type: "info", duration });
  }

  window.AHAToast = Object.freeze({
    show,
    error,
    success,
    info
  });
}());
