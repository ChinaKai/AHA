(() => {
  function escapeFallback(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function createRunLifecycleView(options = {}) {
    const escapeHtml = options.escapeHtml || escapeFallback;

    function filtersHtml(filters = []) {
      return filters.map(item => (
        `<button class="run-lifecycle-filter${item.selected ? " active" : ""}" type="button" data-run-lifecycle-filter="${escapeHtml(item.filter)}" aria-pressed="${item.selected ? "true" : "false"}">${escapeHtml(item.label)}</button>`
      )).join("");
    }

    function rowsHtml(rows = []) {
      return rows.map(row => {
        const currentAttr = row.current ? ` aria-current="true"` : "";
        const settingsOpen = Boolean(row.settingsOpen);
        const metaHtml = row.meta ? `<div class="run-lifecycle-meta meta truncate">${escapeHtml(row.meta)}</div>` : "";
        return `<div class="run-lifecycle-row${row.current ? " active" : ""}${settingsOpen ? " settings-open" : ""}" data-run-lifecycle-row="${escapeHtml(row.id)}"><div class="run-lifecycle-identity"><button class="run-lifecycle-select" type="button" data-run-select-run="${escapeHtml(row.id)}" title="${escapeHtml(row.id)}"${currentAttr}>${escapeHtml(row.title)}</button>${metaHtml}</div><span class="run-lifecycle status ${escapeHtml(row.lifecycleClass)}">${escapeHtml(row.lifecycle)}</span><button class="run-settings-trigger" type="button" data-run-settings-toggle="${escapeHtml(row.id)}" aria-controls="run-settings-panel" aria-expanded="${settingsOpen ? "true" : "false"}" aria-label="${escapeHtml(row.settingsTitle)}" title="${escapeHtml(row.settingsTitle)}"><span aria-hidden="true">⚙</span></button></div>`;
      }).join("");
    }

    return Object.freeze({ filtersHtml, rowsHtml });
  }

  window.AHARunLifecycleView = Object.freeze({ createRunLifecycleView });
})();
