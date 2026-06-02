(() => {
  function createMessageComposer(elements = {}, options = {}) {
    let submitInFlight = false;
    let pointerSubmitUntil = 0;
    let commandSelection = 0;
    const escapeHtml = options.escapeHtml || (value => String(value ?? ""));

    function hasMessage() {
      return Boolean(elements.messageEl?.value.trim());
    }

    function requestSubmit() {
      if (elements.sendFormEl?.requestSubmit) {
        elements.sendFormEl.requestSubmit();
      } else {
        elements.sendFormEl?.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
      }
    }

    function requestSubmitFromPointer(event) {
      if (!hasMessage()) return false;
      event.preventDefault();
      pointerSubmitUntil = Date.now() + 500;
      requestSubmit();
      return true;
    }

    function pointerSubmitActive() {
      return Date.now() < pointerSubmitUntil;
    }

    function setBusy(busy) {
      const sendButton = elements.sendFormEl?.querySelector("button.send");
      if (sendButton) sendButton.disabled = busy;
      if (elements.mobileActionsToggleEl) elements.mobileActionsToggleEl.disabled = busy;
    }

    function syncInputHeight() {
      if (!(elements.messageEl instanceof HTMLTextAreaElement)) return;
      elements.messageEl.style.height = "auto";
      elements.messageEl.style.height = `${Math.min(elements.messageEl.scrollHeight, 160)}px`;
    }

    function syncMobileAction() {
      const hasText = hasMessage();
      options.syncMobileComposerToggle?.(hasText);
      if (hasText) options.closeMobileActionPanel?.();
    }

    function mobileViewportMatches() {
      return window.matchMedia("(max-width: 640px)").matches;
    }

    function plainEnterCreatesNewline() {
      const coarsePointer = Boolean(window.matchMedia?.("(pointer: coarse)")?.matches);
      const touchPoints = Number(navigator.maxTouchPoints || 0) > 0;
      return mobileViewportMatches() || coarsePointer || touchPoints;
    }

    function matchingCommands() {
      return options.matchingCommands?.(elements.messageEl?.value || "") || [];
    }

    function renderCommandMenu() {
      const commands = matchingCommands();
      if (!commands.length) {
        elements.commandMenuEl?.classList.add("hidden");
        if (elements.commandMenuEl) elements.commandMenuEl.innerHTML = "";
        return;
      }
      commandSelection = Math.min(commandSelection, commands.length - 1);
      elements.commandMenuEl?.classList.remove("hidden");
      if (!elements.commandMenuEl) return;
      elements.commandMenuEl.innerHTML = commands.map((item, index) => `
        <button class="command-item ${index === commandSelection ? "active" : ""}" type="button" data-command-index="${index}">
          <span class="command-scope">${escapeHtml(item.scope)}</span>
          <span class="command-name">${escapeHtml(item.name)}</span>
          <span class="command-desc">${escapeHtml(item.desc)}</span>
        </button>
      `).join("");
    }

    function applySlashCommand(index) {
      const command = matchingCommands()[index];
      if (!command || !elements.messageEl) return;
      elements.messageEl.value = command.insert;
      elements.messageEl.focus();
      elements.commandMenuEl?.classList.add("hidden");
      syncMobileAction();
    }

    async function submitForm(event) {
      event.preventDefault();
      if (submitInFlight) return;
      const task = options.selectedTask?.();
      const message = String(elements.messageEl?.value || "").trim();
      if (!task || !message) return;
      submitInFlight = true;
      const originalMessage = elements.messageEl?.value || "";
      if (elements.messageEl) elements.messageEl.value = "";
      syncInputHeight();
      syncMobileAction();
      elements.commandMenuEl?.classList.add("hidden");
      options.closeMobileActionPanel?.();
      setBusy(true);
      try {
        await options.onSubmit?.({ task, message, originalMessage });
      } catch (err) {
        if (elements.messageEl && !elements.messageEl.value.trim()) {
          elements.messageEl.value = originalMessage;
          syncInputHeight();
          syncMobileAction();
        }
        options.onError?.(err);
      } finally {
        setBusy(false);
        submitInFlight = false;
      }
    }

    function bind() {
      elements.sendFormEl?.addEventListener("submit", submitForm);
      elements.sendFormEl?.querySelector("button.send")?.addEventListener("pointerdown", requestSubmitFromPointer);
      elements.messageEl?.addEventListener("input", () => {
        commandSelection = 0;
        syncInputHeight();
        syncMobileAction();
        renderCommandMenu();
      });
      elements.messageEl?.addEventListener("focus", renderCommandMenu);
      elements.messageEl?.addEventListener("keydown", event => {
        if (event.isComposing || event.keyCode === 229) return;
        const commands = matchingCommands();
        const plainEnter = event.key === "Enter" && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey;
        const plainEnterSubmits = plainEnter && !plainEnterCreatesNewline();
        if (commands.length && event.key === "ArrowDown") {
          event.preventDefault();
          commandSelection = (commandSelection + 1) % commands.length;
          renderCommandMenu();
        } else if (commands.length && event.key === "ArrowUp") {
          event.preventDefault();
          commandSelection = (commandSelection + commands.length - 1) % commands.length;
          renderCommandMenu();
        } else if (commands.length && event.key === "Tab") {
          event.preventDefault();
          applySlashCommand(commandSelection);
        } else if (commands.length && plainEnterSubmits) {
          const command = commands[commandSelection];
          if (command && elements.messageEl?.value.trim() !== command.insert.trim()) {
            event.preventDefault();
            applySlashCommand(commandSelection);
          } else {
            event.preventDefault();
            requestSubmit();
          }
        } else if (commands.length && event.key === "Escape") {
          elements.commandMenuEl?.classList.add("hidden");
        } else if (plainEnterSubmits) {
          event.preventDefault();
          requestSubmit();
        }
      });
      elements.commandMenuEl?.addEventListener("mousedown", event => {
        const target = event.target instanceof Element ? event.target.closest("[data-command-index]") : null;
        if (!target) return;
        event.preventDefault();
        applySlashCommand(Number(target.dataset.commandIndex || "0"));
      });
    }

    return Object.freeze({
      bind,
      requestSubmit,
      requestSubmitFromPointer,
      pointerSubmitActive,
      hasMessage,
      syncInputHeight,
      syncMobileAction,
      renderCommandMenu
    });
  }

  window.AHAMessageComposer = Object.freeze({ createMessageComposer });
})();
