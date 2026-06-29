(() => {
  function t(key, fallback = "") {
    return window.AHAI18n?.t?.(key, fallback) || fallback;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function numberText(value) {
    const number = Number(value || 0);
    if (!Number.isFinite(number)) return "0";
    return new Intl.NumberFormat("en-US").format(number);
  }

  function configString(value, fallback = "") {
    if (value === null || value === undefined) return fallback;
    const text = String(value);
    return text || fallback;
  }

  function selectOptions(options, current) {
    return options.map(value => `<option value="${escapeHtml(value)}" ${value === current ? "selected" : ""}>${escapeHtml(value)}</option>`).join("");
  }

  function normalizeHeadroomConfig(value = {}) {
    const raw = value && typeof value === "object" ? value : {};
    const mode = ["token", "cache"].includes(configString(raw.mode)) ? configString(raw.mode) : "token";
    return {
      enabled: Boolean(raw.enabled),
      package: configString(raw.package, "headroom-ai[proxy]"),
      command: configString(raw.command, "headroom"),
      port: Number(raw.port || 8787),
      mode,
      ccr_enabled: Boolean(raw.ccr_enabled)
    };
  }

  function createHeadroomIntegrationController(elements = {}, deps = {}) {
    const button = elements.headroomIntegrationEl;
    const popover = elements.headroomIntegrationPopoverEl;
    const sessionMenu = elements.sessionMenuEl;
    let open = false;
    let loading = false;
    let saving = false;
    let error = "";
    let notice = "";
    let bootstrapPayload = null;
    let headroomConfig = null;
    let status = null;
    let bound = false;

    function currentConfig() {
      return bootstrapPayload?.config || deps.bootstrapData?.()?.config || {};
    }

    function currentHeadroomConfig() {
      if (headroomConfig) return normalizeHeadroomConfig(headroomConfig);
      const cfg = currentConfig();
      return normalizeHeadroomConfig(cfg?.integrations?.headroom);
    }

    function statusLine() {
      if (loading) return t("headroom.loading", "Loading Headroom integration...");
      if (saving) return t("headroom.saving", "Saving...");
      if (error) return error;
      if (notice) return notice;
      if (!status) return t("headroom.status_unknown", "Status unknown");
      if (!status.installed) return t("headroom.not_installed", "Command not found");
      if (status.running) return t("headroom.running", "Runtime running");
      return t("headroom.ready", "Configured");
    }

  function statusGridHtml() {
      const configuredCommand = currentHeadroomConfig().command || "headroom";
      const commandDisplay = status?.command_path || (status && !status.installed ? `${configuredCommand} (${t("headroom.not_found_short", "not found")})` : configuredCommand);
      const rows = [
        ["Enabled", currentHeadroomConfig().enabled ? "on" : "off"],
        ["Installed", status?.installed ? "yes" : "no"],
      ["Runtime", status?.running ? "running" : "stopped"],
      ["Port", status?.port || currentHeadroomConfig().port || "-"],
      ["Mode", status?.mode || currentHeadroomConfig().mode || "-"],
      ["Enabled tasks", numberText(status?.usage?.enabled_tasks)],
      ["Ready turns", numberText(status?.usage?.ready_turns)],
      ["Skipped turns", numberText(status?.usage?.skipped_turns)],
      ["Command", commandDisplay || "-"]
    ];
    return `<div class="headroom-status-grid">${rows.map(([label, value]) => `
      <div><span>${escapeHtml(label)}</span><code>${escapeHtml(value)}</code></div>
    `).join("")}</div>`;
  }

  function usageByTaskHtml() {
    const usage = status?.usage || {};
    const tasks = Array.isArray(usage.tasks) ? usage.tasks : [];
    if (!tasks.length) {
      return `<div class="headroom-usage-empty">${escapeHtml(t("headroom.usage_empty", "No Headroom turns recorded for this run."))}</div>`;
    }
    const hiddenCount = Math.max(0, Number(usage.task_count || 0) - tasks.length);
    const rows = tasks.map(task => {
      const agents = Array.isArray(task.agents) ? task.agents : [];
      const hasTurns = Number(task.ready_turns || 0) > 0 || Number(task.skipped_turns || 0) > 0;
      const agentSummary = hasTurns
        ? agents.map(agent => {
          const skipped = Number(agent.skipped_turns || 0);
          const suffix = skipped ? `, ${numberText(skipped)} skipped` : "";
          return `${agent.agent_id || "main"} ${numberText(agent.ready_turns)}${suffix}`;
        }).join(" · ")
        : task.enabled
          ? t("headroom.usage_enabled_no_turns", "enabled, no turns yet")
          : "";
      const skippedTaskTurns = Number(task.skipped_turns || 0);
      return `
        <div class="headroom-usage-row">
          <div>
            <strong>${escapeHtml(task.task_id || "run")}</strong>
            <span>${escapeHtml(agentSummary || "-")}</span>
          </div>
          <code>${escapeHtml(numberText(task.ready_turns))}${skippedTaskTurns ? ` / ${escapeHtml(numberText(skippedTaskTurns))} skipped` : ""}</code>
        </div>
      `;
    }).join("");
    return `
      <section class="headroom-usage">
        <div class="headroom-usage-head">
          <span>${escapeHtml(t("headroom.usage_by_task", "By task"))}</span>
          <code>${escapeHtml(t("headroom.usage_turns", "ready turns"))}</code>
        </div>
        ${rows}
        ${hiddenCount ? `<div class="headroom-usage-more">${escapeHtml(t("headroom.usage_more", "{count} more task(s)").replace("{count}", numberText(hiddenCount)))}</div>` : ""}
      </section>
    `;
  }

  function renderPopover() {
      if (!popover || !open) return;
      const headroom = currentHeadroomConfig();
      const stateClass = error || (status && !status.installed) ? "error" : notice ? "success" : "";
      const fieldDisabled = loading || saving ? "disabled" : "";
      popover.innerHTML = `<section class="headroom-integration-panel">
        <div class="headroom-integration-head">
          <div>
            <h3>${escapeHtml(t("headroom.title", "Headroom"))}</h3>
            <div class="meta ${stateClass}">${escapeHtml(statusLine())}</div>
          </div>
          <button type="button" data-headroom-refresh ${loading || saving ? "disabled" : ""}>${escapeHtml(t("common.refresh", "Refresh"))}</button>
      </div>
      ${statusGridHtml()}
      ${usageByTaskHtml()}
      <form class="headroom-config-form" data-headroom-form>
          <label class="field-label checkbox-field">
            <span>${escapeHtml(t("headroom.title", "Headroom"))}</span>
            <span class="checkbox-line">
              <input data-headroom-field="enabled" type="checkbox" ${headroom.enabled ? "checked" : ""} ${fieldDisabled}>
              <span>${escapeHtml(t("headroom.enable_hint", "Required by task token saving"))}</span>
            </span>
          </label>
          <div class="headroom-config-grid">
            <details class="headroom-config-advanced">
              <summary>Advanced</summary>
              <div class="headroom-config-grid">
                <label class="field-label">
                  <span>Command</span>
                  <input data-headroom-field="command" value="${escapeHtml(headroom.command)}" ${fieldDisabled}>
                </label>
                <label class="field-label">
                  <span>Port</span>
                  <input data-headroom-field="port" type="number" min="1" max="65535" step="1" value="${escapeHtml(headroom.port)}" ${fieldDisabled}>
                </label>
                <label class="field-label">
                  <span>Mode</span>
                  <select data-headroom-field="mode" ${fieldDisabled}>${selectOptions(["token", "cache"], headroom.mode)}</select>
                </label>
              </div>
              <label class="field-label checkbox-field">
                <span>Retrieval tool</span>
                <span class="checkbox-line">
                  <input data-headroom-field="ccr_enabled" type="checkbox" ${headroom.ccr_enabled ? "checked" : ""} ${fieldDisabled}>
                  <span>${escapeHtml(t("headroom.ccr_hint", "Allow Headroom to inject its CCR retrieval tool"))}</span>
                </span>
              </label>
            </details>
          </div>
          <div class="headroom-config-actions">
            <button type="submit" ${loading || saving ? "disabled" : ""}>${escapeHtml(t("common.save", "Save"))}</button>
          </div>
        </form>
      </section>`;
    }

    function readFormConfig(form) {
      const field = name => form.querySelector(`[data-headroom-field="${name}"]`);
      const text = name => String(field(name)?.value || "").trim();
      const previous = currentHeadroomConfig();
      return normalizeHeadroomConfig({
        enabled: Boolean(field("enabled")?.checked),
        package: previous.package || "headroom-ai[proxy]",
        command: text("command") || "headroom",
        port: Number(text("port") || 8787),
        mode: text("mode") || "token",
        ccr_enabled: Boolean(field("ccr_enabled")?.checked)
      });
    }

    async function loadHeadroom(options = {}) {
      if (!options.silent) {
        loading = true;
        error = "";
        notice = "";
        renderPopover();
      }
      try {
        bootstrapPayload = deps.bootstrapData?.() || await deps.fetchJson?.("/api/bootstrap", {}, "Failed to load AHA config");
        headroomConfig = normalizeHeadroomConfig(bootstrapPayload?.config?.integrations?.headroom);
        const payload = await deps.fetchJson?.("/api/integrations/headroom", {}, "Failed to load Headroom status");
        status = payload?.headroom || null;
      } catch (err) {
        error = String(err?.message || err || "Failed to load Headroom integration");
      } finally {
        loading = false;
        if (open) renderPopover();
      }
    }

    async function saveHeadroom(form) {
      const nextHeadroomConfig = readFormConfig(form);
      headroomConfig = nextHeadroomConfig;
      saving = true;
      error = "";
      notice = "";
      renderPopover();
      try {
        const config = currentConfig();
        const backend = ["codex", "claude"].includes(configString(config.backend)) ? config.backend : "codex";
        const body = {
          ...config,
          backend,
          integrations: {
            ...(config.integrations || {}),
            headroom: nextHeadroomConfig
          },
          force: true
        };
        const payload = await deps.fetchJson?.("/api/bootstrap", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        }, "Failed to save Headroom integration");
        bootstrapPayload = payload;
        headroomConfig = normalizeHeadroomConfig(payload?.config?.integrations?.headroom || nextHeadroomConfig);
        deps.applyBootstrapPayload?.(payload);
        notice = t("headroom.saved", "Saved.");
        const statusPayload = await deps.fetchJson?.("/api/integrations/headroom", {}, "Failed to load Headroom status");
        status = statusPayload?.headroom || status;
      } catch (err) {
        error = String(err?.message || err || "Failed to save Headroom integration");
      } finally {
        saving = false;
        if (open) renderPopover();
      }
    }

    function setOpen(nextOpen) {
      open = Boolean(nextOpen && popover);
      if (!popover) return;
      if (open) {
        deps.setRunMaintenanceConsoleOpen?.(false);
        deps.setTokenUsageOpen?.(false);
        deps.setWeixinConsoleOpen?.(false);
        deps.setPlayConsoleOpen?.(false);
        deps.setSkillsConsoleOpen?.(false);
      }
      sessionMenu?.classList?.toggle("headroom-integration-open", open);
      popover.hidden = !open;
      button?.setAttribute("aria-expanded", String(open));
      if (open) {
        renderPopover();
        void loadHeadroom({ silent: Boolean(status) });
      } else {
        popover.innerHTML = "";
        error = "";
        notice = "";
      }
    }

    function bind() {
      if (bound) return;
      bound = true;
      popover?.addEventListener("click", event => {
        event.stopPropagation();
        const target = event.target instanceof Element ? event.target : null;
        if (target?.closest("[data-headroom-refresh]")) void loadHeadroom();
      });
      popover?.addEventListener("submit", event => {
        const form = event.target instanceof Element ? event.target.closest("[data-headroom-form]") : null;
        if (!form) return;
        event.preventDefault();
        void saveHeadroom(form);
      });
      deps.windowRef?.addEventListener?.("aha:languagechange", () => {
        if (open) renderPopover();
      });
    }

    return Object.freeze({
      bind,
      isOpen: () => open,
      loadHeadroom,
      renderHeadroomIntegrationPopover: renderPopover,
      setHeadroomIntegrationOpen: setOpen
    });
  }

  window.AHAHeadroomIntegration = Object.freeze({ createHeadroomIntegrationController });
})();
