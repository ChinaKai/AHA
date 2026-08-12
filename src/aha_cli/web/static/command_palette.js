(function () {
  function createCommandPalette(elements = {}, deps = {}) {
    const dialogEl = elements.commandPaletteEl;
    const inputEl = elements.commandPaletteInputEl;
    const listEl = elements.commandPaletteListEl;
    const hintsEl = elements.commandPaletteHintsEl;
    let selection = 0;
    let items = [];
    // Breadcrumb stack of group commands the user has drilled into. Each entry is a
    // { name, scope } group; Esc pops back one level. Cleared whenever the query changes.
    let stack = [];
    let query = "";

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function close() {
      if (typeof dialogEl?.close === "function" && dialogEl.open) dialogEl.close();
      else dialogEl?.setAttribute("open", "");
    }

    function open() {
      if (!dialogEl) return;
      if (typeof dialogEl.showModal === "function") {
        try {
          if (!dialogEl.open) dialogEl.showModal();
        } catch (_err) {
          dialogEl.setAttribute("open", "");
        }
      } else {
        dialogEl.setAttribute("open", "");
      }
      stack = [];
      if (inputEl) {
        inputEl.value = "";
        inputEl.focus();
      }
      query = "";
      runSearch("");
    }

    function isOpen() {
      return Boolean(dialogEl?.open);
    }

    function currentQuery() {
      return stack.length ? "" : query;
    }

    function runSearch(raw) {
      const q = String(raw || "").trim();
      query = q;
      stack = [];
      buildItems(q);
    }

    // Build the visible list. Without a query and inside a group, only that group's
    // subcommands show. With a query, everything is flattened (group drills ignored).
    function buildItems(q) {
      const groupKey = stack.length ? stack[stack.length - 1].key : null;
      const built = deps.buildCommands?.(q, groupKey) || [];
      items = built.slice(0, 24);
      selection = 0;
      render();
    }

    function pushGroup(item) {
      stack.push({ key: item.key, name: item.name, scope: item.scope });
      query = "";
      if (inputEl) inputEl.value = "";
      buildItems("");
    }

    function popGroup() {
      if (stack.length) {
        stack.pop();
        buildItems("");
      } else {
        close();
      }
    }

    function render() {
      if (!listEl) return;
      listEl.replaceChildren();
      // Breadcrumb header when inside a group
      if (stack.length) {
        const crumb = document.createElement("div");
        crumb.className = "command-palette-breadcrumb";
        const crumbText = document.createElement("span");
        crumbText.textContent = `${stack.map(g => g.name).join(" / ")} /`;
        const backBtn = document.createElement("button");
        backBtn.type = "button";
        backBtn.className = "command-palette-back";
        backBtn.textContent = "Back";
        backBtn.addEventListener("click", () => popGroup());
        crumb.append(crumbText, backBtn);
        listEl.appendChild(crumb);
      }
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "command-palette-empty";
        empty.textContent = "No matching commands.";
        listEl.appendChild(empty);
        return;
      }
      items.forEach((item, index) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = `command-palette-item${index === selection ? " active" : ""}`;
        row.dataset.index = String(index);
        const number = document.createElement("span");
        number.className = "command-palette-number";
        number.textContent = index < 9 ? String(index + 1) : "";
        const scope = document.createElement("span");
        scope.className = "command-palette-scope";
        scope.textContent = item.scope || "";
        const title = document.createElement("span");
        title.className = "command-palette-name";
        title.textContent = item.name || "";
        const desc = document.createElement("span");
        desc.className = "command-palette-desc";
        desc.textContent = item.desc || "";
        const marker = document.createElement("span");
        marker.className = "command-palette-marker";
        marker.textContent = item.subcommands ? "›" : "";
        row.append(number, scope, title, desc, marker);
        row.addEventListener("mousedown", event => {
          event.preventDefault();
          activate(index);
        });
        listEl.appendChild(row);
      });
      const active = listEl.querySelector(".command-palette-item.active");
      active?.scrollIntoView({ block: "nearest" });
      renderHints();
    }

    function renderHints() {
      if (!hintsEl) return;
      const item = items[selection];
      if (!item) {
        hintsEl.textContent = stack.length ? "Esc to go back" : "";
        return;
      }
      hintsEl.textContent = item.subcommands
        ? `→ enter ${item.name} ›`
        : `↵ ${item.desc || item.name || ""}`;
    }

    function activate(index) {
      const item = items[index];
      if (!item) return;
      if (item.subcommands?.length) {
        pushGroup(item);
        return;
      }
      close();
      void deps.execute?.(item);
    }

    function moveSelection(delta) {
      if (!items.length) return;
      selection = (selection + delta + items.length) % items.length;
      render();
    }

    function bind() {
      if (!dialogEl) return;
      dialogEl.addEventListener("click", event => {
        if (event.target === dialogEl) close();
      });
      if (inputEl) {
        inputEl.addEventListener("input", event => {
          // Typing while inside a group clears the drill-down and searches globally.
          const raw = event.target?.value || "";
          if (stack.length && String(raw).trim()) {
            stack = [];
            query = String(raw).trim();
            buildItems(query);
          } else {
            runSearch(raw);
          }
        });
        inputEl.addEventListener("keydown", event => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            moveSelection(1);
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            moveSelection(-1);
          } else if (event.key === "Enter") {
            event.preventDefault();
            activate(selection);
          } else if (event.key === "Escape") {
            event.preventDefault();
            if (stack.length) popGroup();
            else close();
          } else if (event.key === "ArrowRight" && stack.length === 0) {
            const item = items[selection];
            if (item?.subcommands?.length) {
              event.preventDefault();
              pushGroup(item);
            }
          } else if (/^[1-9]$/.test(event.key)) {
            // Number keys jump straight to that command (1-indexed) when the query is empty,
            // in both the top level and inside a group (items already reflect the active layer).
            if (!String(inputEl.value || "").trim()) {
              const index = Number(event.key) - 1;
              if (index < items.length) {
                event.preventDefault();
                activate(index);
              }
            }
          }
        });
      }
    }

    return Object.freeze({
      bind,
      close,
      isOpen,
      open
    });
  }

  window.AHACommandPalette = Object.freeze({ createCommandPalette });
}());
