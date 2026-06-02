(() => {
  function escapeFallback(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function renderPlayConsole(state = {}, options = {}) {
    const escapeHtml = options.escapeHtml || escapeFallback;
    const games = Array.isArray(state.games) ? state.games : [];
    const content = (() => {
      if (state.loading && !state.loaded) {
        return `<p class="play-console-empty">正在加载小游戏...</p>`;
      }
      if (state.error) {
        return `<p class="play-console-error">${escapeHtml(state.error)}</p>`;
      }
      if (!games.length) {
        return `<p class="play-console-empty">暂无可用小游戏。</p>`;
      }
      return games.map(game => {
        const title = game?.title || game?.id || "未命名游戏";
        const description = game?.description || game?.id || "";
        const href = game?.href || (game?.id ? `/games/${encodeURIComponent(game.id)}/` : "");
        if (!game?.available || !href) {
          return `
          <div class="play-game-card unavailable" aria-disabled="true">
            <div>
              <strong>${escapeHtml(title)}</strong>
              <p>${escapeHtml(description || "入口文件不可用")}</p>
            </div>
            <span>不可用</span>
          </div>
        `;
        }
        return `
        <a class="play-game-card" href="${escapeHtml(href)}" target="_blank" rel="noopener">
          <div>
            <strong>${escapeHtml(title)}</strong>
            <p>${escapeHtml(description || "来自 webgame_workspace")}</p>
          </div>
          <span>在线玩</span>
        </a>
      `;
      }).join("");
    })();
    return `
    <div class="play-console">
      <div class="play-console-head">
        <div>
          <h3>玩了个玩</h3>
          <p>小游戏来自 webgame_workspace，点击后动态加载。</p>
        </div>
      </div>
      <div class="play-game-list">${content}</div>
    </div>
  `;
  }

  function createPlayConsoleController(elements = {}, deps = {}) {
    let open = false;
    const state = {
      games: [],
      loaded: false,
      loading: false,
      error: ""
    };

    function currentRunId() {
      return String(deps.currentRunId?.() || "").trim();
    }

    function renderConsole() {
      return renderPlayConsole(state, { escapeHtml: deps.escapeHtml });
    }

    function renderPopover() {
      if (!elements.playConsolePopoverEl) return;
      elements.playConsolePopoverEl.innerHTML = renderConsole();
    }

    async function loadPlayGames(options = {}) {
      if (!currentRunId() || state.loading) return;
      state.loading = true;
      if (!options.silent) renderPopover();
      try {
        const payload = await deps.fetchJson?.(deps.apiUrl?.("/api/games"), {}, "加载小游戏失败");
        state.games = Array.isArray(payload?.games) ? payload.games : [];
        state.loaded = true;
        state.error = "";
      } catch (err) {
        state.error = err?.message || String(err || "加载小游戏失败");
      } finally {
        state.loading = false;
        if (open) renderPopover();
      }
    }

    function setOpen(nextOpen) {
      open = Boolean(nextOpen && currentRunId() && elements.playConsolePopoverEl);
      if (!elements.playConsolePopoverEl) return;
      if (open) {
        deps.setRunMaintenanceConsoleOpen?.(false);
        deps.setWeixinConsoleOpen?.(false);
      }
      elements.sessionMenuEl?.classList.toggle("play-open", open);
      if (open) {
        renderPopover();
        elements.playConsolePopoverEl.hidden = false;
        void loadPlayGames({ silent: state.loaded });
      } else {
        elements.playConsolePopoverEl.hidden = true;
        elements.playConsolePopoverEl.innerHTML = "";
      }
      elements.playConsoleEl?.setAttribute("aria-expanded", String(open));
    }

    return Object.freeze({
      isOpen: () => open,
      loadPlayGames,
      renderPlayConsole: renderConsole,
      renderPlayConsolePopover: renderPopover,
      setPlayConsoleOpen: setOpen
    });
  }

  window.AHAPlayConsole = Object.freeze({ createPlayConsoleController, renderPlayConsole });
})();
