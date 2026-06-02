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
        const buttons = row.actions.map(action => (
          `<button class="run-lifecycle-action" type="button" data-run-lifecycle-run="${escapeHtml(row.id)}" data-run-lifecycle-status="${escapeHtml(action.status)}"${action.disabled ? " disabled" : ""} title="${escapeHtml(action.title)}">${escapeHtml(action.label)}</button>`
        )).join("");
        const protectedText = row.reasonText ? `<span class="run-lifecycle-protection">${escapeHtml(row.reasonText)}</span>` : "";
        return `<div class="run-lifecycle-row" data-run-lifecycle-row="${escapeHtml(row.id)}"><span class="run-lifecycle-name" title="${escapeHtml(row.id)}">${escapeHtml(row.title)}</span><span class="run-lifecycle status ${escapeHtml(row.lifecycleClass)}">${escapeHtml(row.lifecycle)}</span><div class="run-lifecycle-buttons">${buttons}</div>${protectedText}</div>`;
      }).join("");
    }

    return Object.freeze({ filtersHtml, rowsHtml });
  }

  window.AHARunLifecycleView = Object.freeze({ createRunLifecycleView });
})();
