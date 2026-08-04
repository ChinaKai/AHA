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
    let savingSettings = false;
    let loaded = false;
    let error = "";
    let notice = "";
    let status = {};
    let backendCatalog = [];

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

    function compactId(value) {
      const text = String(value || "");
      if (text.length <= 18) return text;
      return `${text.slice(0, 9)}...${text.slice(-5)}`;
    }

    function identityEntry(item, idKey) {
      const id = String(item?.[idKey] || item?.id || "");
      if (!id) return "";
      const name = String(item?.display_name || "").trim();
      const hash = String(item?.id_hash || item?.[`${idKey}_hash`] || "").trim();
      const chatType = String(item?.chat_type || "").trim();
      const seenAt = String(item?.last_seen_at || item?.updated_at || "").trim();
      const lookupError = String(item?.lookup_error || "").trim();
      const heading = `${compactId(id)}.${name || t("feishu.name_unresolved", "unresolved")}`;
      const meta = [chatType, hash ? `#${hash}` : "", seenAt, lookupError ? t("feishu.name_lookup_failed", "name lookup failed") : ""].filter(Boolean).join(" · ");
      return `
        <div>
          <strong title="${escapeHtml([id, lookupError].filter(Boolean).join("\n"))}">${escapeHtml(heading)}</strong>
          <code>${escapeHtml(id)}</code>
          ${meta ? `<small>${escapeHtml(meta)}</small>` : ""}
        </div>`;
    }

    function mergeIdentityRows(primaryItems, configuredItems, idKey, extraItems = []) {
      const rows = new Map();
      const put = item => {
        if (!item || typeof item !== "object") return;
        const id = String(item?.[idKey] || item?.id || "").trim();
        if (!id) return;
        rows.set(id, { ...(rows.get(id) || {}), ...item, [idKey]: id, id });
      };
      (Array.isArray(primaryItems) ? primaryItems : []).forEach(put);
      (Array.isArray(configuredItems) ? configuredItems : []).forEach(put);
      (Array.isArray(extraItems) ? extraItems : []).forEach(put);
      return Array.from(rows.values());
    }

    function ownerOptionsHtml(items, selected) {
      const cleanSelected = String(selected || "");
      const rows = mergeIdentityRows(items, [], "open_id");
      const options = [
        `<option value="" ${cleanSelected ? "" : "selected"}>${escapeHtml(t("feishu.owner_unset", "Not set"))}</option>`,
        ...rows.map(item => {
          const openId = String(item?.open_id || item?.id || "");
          const label = String(item?.display_name || "").trim()
            ? `${compactId(openId)}.${String(item.display_name).trim()}`
            : `${compactId(openId)}.${t("feishu.name_unresolved", "unresolved")}`;
          return `<option value="${escapeHtml(openId)}" ${cleanSelected === openId ? "selected" : ""}>${escapeHtml(label)}</option>`;
        })
      ];
      if (cleanSelected && !rows.some(item => String(item?.open_id || item?.id || "") === cleanSelected)) {
        options.push(`<option value="${escapeHtml(cleanSelected)}" selected>${escapeHtml(compactId(cleanSelected))}</option>`);
      }
      return options.join("");
    }

    function csvValues(input) {
      return String(input?.value || "").replaceAll("\n", ",").split(",").map(item => item.trim()).filter(Boolean);
    }

    function setCsvValues(input, values) {
      if (input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement) {
        input.value = Array.from(new Set(values.map(item => String(item || "").trim()).filter(Boolean))).join(", ");
      }
    }

    function backendOptions() {
      const catalog = backendCatalog.filter(item => item && typeof item === "object" && item.name);
      if (catalog.length) return catalog;
      return ["codex", "claude", "stub"].map(name => ({ name, models: [] }));
    }

    function backendOptionsHtml(selected) {
      const inherited = String(status.effective_backend || "codex");
      const options = [
        `<option value="" ${selected ? "" : "selected"}>${escapeHtml(`${t("feishu.inherit_backend", "Inherit AHA default")} (${inherited})`)}</option>`,
        ...backendOptions().map(item => `<option value="${escapeHtml(item.name)}" ${selected === item.name ? "selected" : ""}>${escapeHtml(item.name)}</option>`)
      ];
      if (selected && !backendOptions().some(item => item.name === selected)) {
        options.push(`<option value="${escapeHtml(selected)}" selected>${escapeHtml(selected)}</option>`);
      }
      return options.join("");
    }

    function modelOptionsHtml(backend, selected) {
      const effectiveBackend = backend || String(status.effective_backend || "codex");
      const inherited = backend && backend !== String(status.backend || "")
        ? t("common.default", "default")
        : String(status.effective_model || t("common.default", "default"));
      const catalog = backendOptions().find(item => item.name === effectiveBackend);
      const models = Array.isArray(catalog?.models) ? catalog.models : [];
      const envGroups = Array.isArray(status.env_groups?.[effectiveBackend])
        ? status.env_groups[effectiveBackend]
        : [];
      const envModels = envGroups.filter(item => item && typeof item === "object" && item.name).map(item => {
        const name = String(item.name);
        const model = String(item.model || "");
        return {
          name: `env:${name}`,
          label: model
            ? `${model} (${name} · ${t("feishu.env_group", "env group")})`
            : `${name} (${t("feishu.env_group", "env group")})`
        };
      });
      const allModels = [...models, ...envModels].filter((item, index, items) => {
        const name = String(item?.name || "");
        return name && items.findIndex(candidate => String(candidate?.name || "") === name) === index;
      });
      const options = [
        `<option value="" ${selected ? "" : "selected"}>${escapeHtml(`${t("feishu.inherit_model", "Inherit backend default")} (${inherited})`)}</option>`,
        ...allModels.map(item => {
          const value = String(item.name || "");
          if (!value) return "";
          const label = String(item.label || value);
          return `<option value="${escapeHtml(value)}" ${selected === value ? "selected" : ""}>${escapeHtml(label)}</option>`;
        })
      ];
      if (selected && !allModels.some(item => String(item?.name || "") === selected)) {
        options.push(`<option value="${escapeHtml(selected)}" selected>${escapeHtml(selected)}</option>`);
      }
      return options.join("");
    }

    function backendDefault(backend, key, fallback) {
      const effectiveBackend = backend || String(status.effective_backend || "codex");
      const defaults = status.backend_defaults?.[effectiveBackend];
      return defaults && Object.prototype.hasOwnProperty.call(defaults, key) ? defaults[key] : fallback;
    }

    function reasoningOptionsHtml(backend, model, selected) {
      const effectiveBackend = backend || String(status.effective_backend || "codex");
      const catalog = backendOptions().find(item => item.name === effectiveBackend);
      const modelOption = (Array.isArray(catalog?.models) ? catalog.models : []).find(item => String(item?.name || "") === model);
      const available = Array.isArray(modelOption?.reasoning_efforts) && modelOption.reasoning_efforts.length
        ? modelOption.reasoning_efforts
        : (Array.isArray(catalog?.reasoning_efforts) ? catalog.reasoning_efforts : []);
      const efforts = available.filter(item => item && typeof item === "object" && String(item.name || ""));
      const inherited = String(
        backendDefault(
          effectiveBackend,
          "reasoning_effort",
          effectiveBackend === String(status.effective_backend || "") ? status.effective_reasoning_effort : ""
        ) || t("common.default", "default")
      );
      const options = [
        `<option value="" ${selected ? "" : "selected"}>${escapeHtml(`${t("feishu.inherit_reasoning", "Inherit backend default")} (${inherited})`)}</option>`,
        ...efforts.map(item => {
          const value = String(item.name || "");
          const label = String(item.label || value);
          return `<option value="${escapeHtml(value)}" ${selected === value ? "selected" : ""}>${escapeHtml(label)}</option>`;
        })
      ];
      if (selected && !efforts.some(item => String(item?.name || "") === selected)) {
        options.push(`<option value="${escapeHtml(selected)}" selected>${escapeHtml(selected)}</option>`);
      }
      return options.join("");
    }

    function workRunOptionsHtml(selected) {
      const runs = Array.isArray(status.work_run_options) ? status.work_run_options : [];
      const options = [
        `<option value="" ${selected ? "" : "selected"}>${escapeHtml(t("feishu.default_run_unbound", "Not bound"))}</option>`,
        ...runs.map(run => {
          const id = String(run?.id || "");
          if (!id) return "";
          const goal = String(run?.goal || id);
          const statusText = String(run?.lifecycle_status || run?.status || "");
          const label = statusText ? `${goal} · ${id} · ${statusText}` : `${goal} · ${id}`;
          return `<option value="${escapeHtml(id)}" ${selected === id ? "selected" : ""}>${escapeHtml(label)}</option>`;
        })
      ];
      if (selected && !runs.some(run => String(run?.id || "") === selected)) {
        options.push(`<option value="${escapeHtml(selected)}" selected>${escapeHtml(`${selected} (${t("feishu.default_run_unavailable", "unavailable")})`)}</option>`);
      }
      return options.join("");
    }

    function setupMessage() {
      if (loading && !loaded) return t("feishu.loading", "Loading Feishu status...");
      if (!status.enabled) return t("feishu.disabled_hint", "Enable Feishu below, save, then restart the Web service.");
      if (!status.sdk_installed) return t("feishu.sdk_missing_hint", "Install the Feishu Channel SDK, then restart the Web service.");
      if (!status.configured) return t("feishu.not_configured_hint", "Configure App ID and App Secret below, then restart the Web service.");
      if (connectionState() !== "connected") return t("feishu.connection_hint", "Check the runtime error and Feishu application event subscription settings.");
      return t("feishu.connected_hint", "The Feishu assistant is ready to receive messages and push subscribed task replies.");
    }

    function renderPopover() {
      const popover = elements.feishuConsolePopoverEl;
      if (!popover) return;
      const runtime = status.runtime && typeof status.runtime === "object" ? status.runtime : {};
      const assistant = status.assistant && typeof status.assistant === "object" ? status.assistant : {};
      const profileRefresh = status.identity_profile_refresh && typeof status.identity_profile_refresh === "object" ? status.identity_profile_refresh : {};
      const runtimeState = connectionState();
      const runtimeError = String(runtime.error || "");
      const profileErrors = Array.isArray(profileRefresh.errors) ? profileRefresh.errors.map(item => String(item || "").trim()).filter(Boolean) : [];
      const allowedOpenIds = Array.isArray(status.allowed_open_ids) ? status.allowed_open_ids : [];
      const allowedChatIds = Array.isArray(status.allowed_chat_ids) ? status.allowed_chat_ids : [];
      const allowedOpenIdItems = Array.isArray(status.allowed_open_id_items) ? status.allowed_open_id_items : [];
      const allowedChatIdItems = Array.isArray(status.allowed_chat_id_items) ? status.allowed_chat_id_items : [];
      const allowedChatSet = new Set(allowedChatIds.map(value => String(value)));
      const allowedOpenSet = new Set(allowedOpenIds.map(value => String(value)));
      const recentGroups = Array.isArray(status.recent_groups) ? status.recent_groups : [];
      const recentPrivateChats = Array.isArray(status.recent_private_chats) ? status.recent_private_chats : [];
      const ownerOpenItem = status.owner_open_id_item && typeof status.owner_open_id_item === "object" ? status.owner_open_id_item : {};
      const privateChatItems = mergeIdentityRows(
        recentPrivateChats,
        allowedOpenIdItems,
        "open_id",
        status.owner_open_id ? [ownerOpenItem] : []
      );
      const groupItems = mergeIdentityRows(recentGroups, allowedChatIdItems, "chat_id");
      const recentGroupsHtml = groupItems.length
        ? groupItems.map(group => {
            const chatId = String(group?.chat_id || "");
            const added = allowedChatSet.has(chatId);
            return `
              <div class="feishu-recent-group">
                ${identityEntry(group, "chat_id")}
                <button type="button" data-feishu-action="${added ? "remove-group" : "add-group"}" data-feishu-chat-id="${escapeHtml(chatId)}">${escapeHtml(added ? t("feishu.remove_group", "Remove") : t("feishu.add_group", "Add"))}</button>
              </div>`;
          }).join("")
        : `<p class="feishu-console-section-help">${escapeHtml(t("feishu.group_list_empty", "No groups detected or allowed yet. Add the bot to a group, @ it once, then refresh."))}</p>`;
      const recentPrivateChatsHtml = privateChatItems.length
        ? privateChatItems.map(chat => {
            const openId = String(chat?.open_id || "");
            const chatId = String(chat?.chat_id || "");
            const isOwner = openId && String(status.owner_open_id || "") === openId;
            const added = openId && allowedOpenSet.has(openId);
            return `
              <div class="feishu-recent-group">
                ${identityEntry(chat, "open_id")}
                <div class="feishu-recent-actions">
                  <button type="button" data-feishu-action="set-owner" data-feishu-open-id="${escapeHtml(openId)}" data-feishu-chat-id="${escapeHtml(chatId)}" ${openId && !isOwner ? "" : "disabled"}>${escapeHtml(isOwner ? t("feishu.owner_set", "Owner") : t("feishu.set_owner", "Set owner"))}</button>
                  <button type="button" data-feishu-action="${added ? "remove-user" : "add-user"}" data-feishu-open-id="${escapeHtml(openId)}" ${openId ? "" : "disabled"}>${escapeHtml(added ? t("feishu.remove_user", "Remove") : t("feishu.add_user", "Add user"))}</button>
                </div>
              </div>`;
          }).join("")
        : `<p class="feishu-console-section-help">${escapeHtml(t("feishu.private_chat_list_empty", "No private chats detected or allowed yet. Send the bot a private message, then refresh."))}</p>`;
      const secretPlaceholder = status.app_secret_configured
        ? t("feishu.secret_configured", "Configured; leave blank to keep")
        : "";
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
            ${metric(t("feishu.configured", "Credentials configured"), yesNo(Boolean(status.configured)))}
            ${metric(t("feishu.sdk_installed", "Channel SDK installed"), yesNo(Boolean(status.sdk_installed)))}
            ${metric(t("feishu.allowed_users", "Allowed users"), String(status.allowed_open_id_count ?? 0))}
            ${metric(t("feishu.allowed_groups", "Allowed groups"), String(status.allowed_chat_id_count ?? 0))}
            ${metric(t("feishu.agent_runtime", "Agent default"), `${status.effective_backend || "codex"} / ${status.effective_model || "default"} / ${status.effective_reasoning_effort || "default"} / proxy ${status.effective_proxy_enabled ? "on" : "off"}`, { code: true })}
            ${metric(t("feishu.assistant_identity", "Assistant identity"), t("feishu.assistant_identity_value", "AHA service steward"))}
            ${metric(t("feishu.assistant_workspace", "Assistant workspace"), assistant.workspace_path || "-", { code: true })}
            ${metric(t("feishu.assistant_conversations", "Assistant conversations"), `${assistant.active_conversation_count ?? 0} / ${assistant.conversation_count ?? 0}`)}
            ${metric(t("feishu.assistant_guard", "Assistant guard"), `${assistant.sandbox || "read-only"} / ${assistant.approval || "never"}`, { code: true })}
          </div>
          ${runtimeError ? `<div class="feishu-console-message error"><strong>${escapeHtml(t("feishu.runtime_error", "Runtime error"))}</strong><span>${escapeHtml(runtimeError)}</span></div>` : ""}
          ${profileErrors.length ? `<div class="feishu-console-message warning"><strong>${escapeHtml(t("feishu.identity_lookup_warning", "ID name lookup"))}</strong><span>${escapeHtml(profileErrors[0])}</span></div>` : ""}
          <form class="feishu-console-settings" data-feishu-settings-form>
            <fieldset class="feishu-console-section">
              <legend>${escapeHtml(t("feishu.connection_section", "Connection"))}</legend>
              <label class="checkbox-line feishu-console-primary-toggle">
                <input name="enabled" type="checkbox" ${status.enabled ? "checked" : ""}>
                <span><strong>${escapeHtml(t("feishu.enable", "Enable Feishu"))}</strong><small>${escapeHtml(t("feishu.enable_hint", "Start the assistant with the AHA Web service"))}</small></span>
              </label>
              <div class="feishu-console-settings-grid">
                <label class="field-label">
                  <span>${escapeHtml(t("feishu.app_id", "App ID"))}</span>
                  <input name="app_id" placeholder="cli_xxx" value="${escapeHtml(status.app_id || "")}">
                </label>
                <label class="field-label">
                  <span>${escapeHtml(t("feishu.app_secret", "App Secret"))}</span>
                  <input name="app_secret" type="password" placeholder="${escapeHtml(secretPlaceholder)}" value="">
                </label>
              </div>
              <div class="field-help">${escapeHtml(t("feishu.secret_hint", "The secret is never shown. Leave blank to keep the existing value."))}</div>
              <input name="app_id_env" type="hidden" value="${escapeHtml(status.app_id_env || "AHA_FEISHU_APP_ID")}">
              <input name="app_secret_env" type="hidden" value="${escapeHtml(status.app_secret_env || "AHA_FEISHU_APP_SECRET")}">
              <input name="security_mode" type="hidden" value="${escapeHtml(status.security_mode || "audit")}">
            </fieldset>
            <fieldset class="feishu-console-section">
              <legend>${escapeHtml(t("feishu.agent_section", "Agent defaults"))}</legend>
              <div class="feishu-console-settings-grid">
                <label class="field-label">
                  <span>${escapeHtml(t("feishu.default_backend", "Default backend"))}</span>
                  <select name="backend">${backendOptionsHtml(String(status.backend || ""))}</select>
                </label>
                <label class="field-label">
                  <span>${escapeHtml(t("feishu.default_model", "Default model"))}</span>
                  <select name="model">${modelOptionsHtml(String(status.backend || ""), String(status.model || ""))}</select>
                </label>
                <label class="field-label">
                  <span>${escapeHtml(t("feishu.default_reasoning_effort", "Reasoning effort"))}</span>
                  <select name="reasoning_effort">${reasoningOptionsHtml(String(status.backend || ""), String(status.model || ""), String(status.reasoning_effort || ""))}</select>
                </label>
                <label class="field-label">
                  <span>${escapeHtml(t("feishu.default_proxy", "Proxy"))}</span>
                  <span class="checkbox-line">
                    <input name="proxy_enabled" type="checkbox" ${status.effective_proxy_enabled ? "checked" : ""}>
                    <span>${escapeHtml(t("feishu.default_proxy_toggle", "Use the selected backend proxy"))}</span>
                  </span>
                </label>
                <label class="field-label">
                  <span>${escapeHtml(t("feishu.default_run", "Default work Run"))}</span>
                  <select name="default_run_id">${workRunOptionsHtml(String(status.configured_default_run_id || status.default_run_id || ""))}</select>
                  <div class="field-help">${escapeHtml(t("feishu.default_run_hint", "Default landing Run for Feishu memos and task creation. System-managed Runs are excluded; specific operations may still choose another Run."))}</div>
                  ${status.default_run_error ? `<div class="field-help error">${escapeHtml(status.default_run_error)}</div>` : ""}
                </label>
              </div>
              <p class="feishu-console-section-help">${escapeHtml(t("feishu.agent_defaults_hint", "Used for new system-managed AHA service-steward conversations. Its workspace is AHA Home, not a project; existing conversations are unchanged."))}</p>
            </fieldset>
            <fieldset class="feishu-console-section">
              <legend>${escapeHtml(t("feishu.access_section", "Access and delivery"))}</legend>
              <input name="owner_chat_id" type="hidden" value="${escapeHtml(status.owner_chat_id || "")}">
              <input name="allowed_open_ids" type="hidden" value="${escapeHtml(allowedOpenIds.join(", "))}">
              <input name="allowed_chat_ids" type="hidden" value="${escapeHtml(allowedChatIds.join(", "))}">
              <label class="field-label">
                <span>${escapeHtml(t("feishu.owner_open_id", "Owner open_id"))}</span>
                <select name="owner_open_id">${ownerOptionsHtml(
                  mergeIdentityRows(allowedOpenIdItems, [], "open_id", status.owner_open_id ? [ownerOpenItem] : []),
                  String(status.owner_open_id || "")
                )}</select>
                <div class="field-help">${escapeHtml(t("feishu.owner_open_id_hint", "Choose one allowed open_id as the owner. The private chat_id is recorded automatically from the owner private chat."))}</div>
              </label>
              <label class="field-label">
                <span>${escapeHtml(t("feishu.group_access_mode", "Group member access"))}</span>
                <select name="group_access_mode">
                  <option value="allowed_users" ${status.group_access_mode !== "all_members" ? "selected" : ""}>${escapeHtml(t("feishu.group_access_allowed_users", "Allowed users only (recommended)"))}</option>
                  <option value="all_members" ${status.group_access_mode === "all_members" ? "selected" : ""}>${escapeHtml(t("feishu.group_access_all_members", "All members of allowed groups"))}</option>
                </select>
                <div class="field-help">${escapeHtml(t("feishu.group_access_mode_hint", "All members lets anyone in an allowed group use the AHA assistant; private chats still require an allowed open_id."))}</div>
              </label>
              <div class="feishu-recent-groups">
                <strong>${escapeHtml(t("feishu.private_chat_list", "Private chats list"))}</strong>
                <p class="feishu-console-section-help">${escapeHtml(t("feishu.private_chat_list_hint", "Manage allowed open_id values here. Set owner also adds the open_id to the allowed list."))}</p>
                ${recentPrivateChatsHtml}
              </div>
              <div class="feishu-recent-groups">
                <strong>${escapeHtml(t("feishu.group_list", "Group list"))}</strong>
                <p class="feishu-console-section-help">${escapeHtml(t("feishu.group_list_hint", "Manage allowed group chat_id values here. Groups must be allowed before the assistant responds."))}</p>
                ${recentGroupsHtml}
              </div>
              <div class="feishu-console-toggle-list">
                <label class="checkbox-line">
                  <input name="group_mentions_only" type="checkbox" ${status.group_mentions_only ? "checked" : ""}>
                  <span>${escapeHtml(t("feishu.group_mentions", "Require @bot in groups"))}</span>
                </label>
                <label class="checkbox-line">
                  <input name="notifications_enabled" type="checkbox" ${status.notifications_enabled ? "checked" : ""}>
                  <span>${escapeHtml(t("feishu.notifications_toggle", "Push task status changes to owner"))}</span>
                </label>
              </div>
              <p class="feishu-console-section-help">${escapeHtml(t("feishu.notifications_hint", "Pushes non-system project task changes only to the owner private chat. System runs, group chats, and other allowed users do not receive status pushes. Direct chat replies remain enabled when this is off."))}</p>
            </fieldset>
            <div class="feishu-console-settings-actions">
              <button type="submit" ${loading || savingSettings ? "disabled" : ""}>${escapeHtml(savingSettings ? t("feishu.saving", "Saving...") : t("common.save", "Save"))}</button>
              <small>${escapeHtml(t("feishu.restart_hint", "Restart for credential or connection changes; access and delivery changes apply to subsequent messages."))}</small>
            </div>
          </form>
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
        try {
          const catalog = await deps.fetchJson?.(
            deps.apiUrl?.("/api/backends", {}, { runScoped: false }) || "/api/backends",
            {},
            t("feishu.backends_failed", "Failed to load backend options")
          );
          backendCatalog = Array.isArray(catalog?.backends) ? catalog.backends : [];
        } catch (_err) {
          backendCatalog = [];
        }
        loaded = true;
      } catch (err) {
        error = err?.message || String(err || t("feishu.load_failed", "Failed to load Feishu status"));
      } finally {
        loading = false;
        if (open) renderPopover();
      }
    }

    async function saveSettings(form) {
      if (savingSettings) return;
      const field = name => form.querySelector(`[name="${name}"]`);
      const payload = {
        enabled: Boolean(field("enabled")?.checked),
        app_id: String(field("app_id")?.value || "").trim(),
        app_secret: String(field("app_secret")?.value || ""),
        app_id_env: String(field("app_id_env")?.value || "").trim() || "AHA_FEISHU_APP_ID",
        app_secret_env: String(field("app_secret_env")?.value || "").trim() || "AHA_FEISHU_APP_SECRET",
        backend: String(field("backend")?.value || "").trim(),
        model: String(field("model")?.value || "").trim(),
        reasoning_effort: String(field("reasoning_effort")?.value || "").trim(),
        default_run_id: String(field("default_run_id")?.value || "").trim(),
        proxy_enabled: Boolean(field("proxy_enabled")?.checked),
        owner_open_id: String(field("owner_open_id")?.value || "").trim(),
        owner_chat_id: String(field("owner_chat_id")?.value || "").trim(),
        allowed_open_ids: String(field("allowed_open_ids")?.value || "").split(",").map(item => item.trim()).filter(Boolean),
        allowed_chat_ids: String(field("allowed_chat_ids")?.value || "").replaceAll("\n", ",").split(",").map(item => item.trim()).filter(Boolean),
        group_access_mode: String(field("group_access_mode")?.value || "allowed_users"),
        group_mentions_only: Boolean(field("group_mentions_only")?.checked),
        notifications_enabled: Boolean(field("notifications_enabled")?.checked),
        security_mode: String(field("security_mode")?.value || "audit")
      };
      savingSettings = true;
      error = "";
      notice = "";
      renderPopover();
      try {
        const response = await deps.fetchJson?.(
          deps.apiUrl?.("/api/feishu/settings", {}, { runScoped: false }) || "/api/feishu/settings",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
          },
          t("feishu.save_failed", "Failed to save Feishu settings")
        );
        status = response?.feishu && typeof response.feishu === "object" ? response.feishu : status;
        notice = t("feishu.saved", "Feishu settings saved");
      } catch (err) {
        error = err?.message || String(err || t("feishu.save_failed", "Failed to save Feishu settings"));
      } finally {
        savingSettings = false;
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
      if (action === "add-group") {
        const button = target?.closest('[data-feishu-action="add-group"]');
        const chatId = String(button?.getAttribute("data-feishu-chat-id") || "").trim();
        const input = button?.closest("form")?.querySelector('[name="allowed_chat_ids"]');
        if (chatId && (input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement)) {
          const values = csvValues(input);
          if (!values.includes(chatId)) values.push(chatId);
          setCsvValues(input, values);
          button.setAttribute("data-feishu-action", "remove-group");
          button.textContent = t("feishu.remove_group", "Remove");
        }
      }
      if (action === "remove-group") {
        const button = target?.closest('[data-feishu-action="remove-group"]');
        const chatId = String(button?.getAttribute("data-feishu-chat-id") || "").trim();
        const input = button?.closest("form")?.querySelector('[name="allowed_chat_ids"]');
        if (chatId && (input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement)) {
          setCsvValues(input, csvValues(input).filter(value => value !== chatId));
          button.setAttribute("data-feishu-action", "add-group");
          button.textContent = t("feishu.add_group", "Add");
        }
      }
      if (action === "add-user") {
        const button = target?.closest('[data-feishu-action="add-user"]');
        const openId = String(button?.getAttribute("data-feishu-open-id") || "").trim();
        const input = button?.closest("form")?.querySelector('[name="allowed_open_ids"]');
        if (openId && input instanceof HTMLInputElement) {
          const values = csvValues(input);
          if (!values.includes(openId)) values.push(openId);
          setCsvValues(input, values);
          button.setAttribute("data-feishu-action", "remove-user");
          button.textContent = t("feishu.remove_user", "Remove");
        }
      }
      if (action === "remove-user") {
        const button = target?.closest('[data-feishu-action="remove-user"]');
        const openId = String(button?.getAttribute("data-feishu-open-id") || "").trim();
        const form = button?.closest("form");
        const input = form?.querySelector('[name="allowed_open_ids"]');
        const ownerInput = form?.querySelector('[name="owner_open_id"]');
        const ownerChatInput = form?.querySelector('[name="owner_chat_id"]');
        if (openId && input instanceof HTMLInputElement) {
          setCsvValues(input, csvValues(input).filter(value => value !== openId));
          if (ownerInput instanceof HTMLSelectElement && ownerInput.value === openId) ownerInput.value = "";
          if (ownerChatInput instanceof HTMLInputElement && ownerInput instanceof HTMLSelectElement && ownerInput.value === "") ownerChatInput.value = "";
          button.setAttribute("data-feishu-action", "add-user");
          button.textContent = t("feishu.add_user", "Add user");
        }
      }
      if (action === "set-owner") {
        const button = target?.closest('[data-feishu-action="set-owner"]');
        const openId = String(button?.getAttribute("data-feishu-open-id") || "").trim();
        const chatId = String(button?.getAttribute("data-feishu-chat-id") || "").trim();
        const form = button?.closest("form");
        const ownerInput = form?.querySelector('[name="owner_open_id"]');
        const ownerChatInput = form?.querySelector('[name="owner_chat_id"]');
        const allowedInput = form?.querySelector('[name="allowed_open_ids"]');
        if (openId && ownerInput instanceof HTMLSelectElement) {
          if (![...ownerInput.options].some(option => option.value === openId)) {
            ownerInput.append(new Option(compactId(openId), openId));
          }
          ownerInput.value = openId;
        }
        if (chatId && ownerChatInput instanceof HTMLInputElement) ownerChatInput.value = chatId;
        if (openId && allowedInput instanceof HTMLInputElement) {
          const values = csvValues(allowedInput);
          if (!values.includes(openId)) {
            values.unshift(openId);
            setCsvValues(allowedInput, values);
          }
        }
        if (openId || chatId) {
          button.textContent = t("feishu.owner_set", "Owner");
          button.disabled = true;
        }
      }
    });
    elements.feishuConsolePopoverEl?.addEventListener("submit", event => {
      const form = event.target instanceof HTMLFormElement ? event.target : null;
      if (!form?.matches("[data-feishu-settings-form]")) return;
      event.preventDefault();
      void saveSettings(form);
    });
    elements.feishuConsolePopoverEl?.addEventListener("change", event => {
      const target = event.target instanceof HTMLSelectElement ? event.target : null;
      if (!target) return;
      if (target.matches('[name="backend"]')) {
        const model = target.form?.querySelector('[name="model"]');
        const reasoning = target.form?.querySelector('[name="reasoning_effort"]');
        const proxy = target.form?.querySelector('[name="proxy_enabled"]');
        if (model) model.innerHTML = modelOptionsHtml(target.value, "");
        if (reasoning) reasoning.innerHTML = reasoningOptionsHtml(target.value, "", "");
        if (proxy) proxy.checked = Boolean(backendDefault(target.value, "proxy_enabled", false));
        return;
      }
      if (target.matches('[name="model"]')) {
        const backend = target.form?.querySelector('[name="backend"]');
        const reasoning = target.form?.querySelector('[name="reasoning_effort"]');
        if (reasoning) reasoning.innerHTML = reasoningOptionsHtml(backend?.value || "", target.value, reasoning.value);
      }
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
