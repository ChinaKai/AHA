(() => {
  function createMessageComposer(elements = {}, options = {}) {
    let submitInFlight = false;
    let imageUploadInFlight = false;
    let pointerSubmitUntil = 0;
    let commandSelection = 0;
    const escapeHtml = options.escapeHtml || (value => String(value ?? ""));
    const windowRef = options.windowRef || window;
    const imagePaste = options.textareaImagePaste || windowRef.AHATextareaImagePaste;

    function hasMessage() {
      return Boolean(elements.messageEl?.value.trim());
    }

    function imageUploadsEnabled() {
      return Boolean(elements.messageEl && (!options.imageUploadsEnabled || options.imageUploadsEnabled()));
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

    function syncImageUploadState() {
      const disabled = imageUploadInFlight || !imageUploadsEnabled();
      if (elements.messageImageFileEl) elements.messageImageFileEl.disabled = disabled;
      if (elements.messageImageUploadEl) {
        elements.messageImageUploadEl.classList.toggle("is-disabled", disabled);
        elements.messageImageUploadEl.setAttribute("aria-disabled", String(disabled));
      }
    }

    function setImageUploadBusy(busy) {
      imageUploadInFlight = Boolean(busy);
      syncImageUploadState();
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
      syncImageUploadState();
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

    async function markdownForImage(file, index = 0) {
      if (typeof options.markdownForImage === "function") {
        const markdown = await options.markdownForImage({ file, index });
        if (markdown) return markdown;
      }
      const dataUrl = await imagePaste?.readAsDataUrl?.(file, { windowRef });
      if (!dataUrl) return "";
      if (typeof options.markdownForImage === "function") {
        return await options.markdownForImage({ dataUrl, file, index });
      }
      return imagePaste?.imageMarkdown?.(dataUrl, file) || "";
    }

    async function insertImageFiles(files) {
      const selectedFiles = Array.from(files || []).filter(Boolean);
      if (!selectedFiles.length || !elements.messageEl || !imageUploadsEnabled()) return;
      setImageUploadBusy(true);
      try {
        for (const [index, file] of selectedFiles.entries()) {
          const markdown = await markdownForImage(file, index);
          if (!markdown) continue;
          if (imagePaste?.insertTextareaImageMarkdown) {
            imagePaste.insertTextareaImageMarkdown(elements.messageEl, markdown, { windowRef });
          }
        }
        syncInputHeight();
        syncMobileAction();
        renderCommandMenu();
        options.onInput?.();
      } catch (err) {
        options.onError?.(err);
      } finally {
        setImageUploadBusy(false);
        if (elements.messageImageFileEl) elements.messageImageFileEl.value = "";
      }
    }

    function handleImagePaste(event) {
      if (!imageUploadsEnabled()) return;
      const files = imagePaste?.clipboardImageFiles?.(event) || [];
      if (!files.length) return;
      event.preventDefault();
      void insertImageFiles(files);
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
      elements.messageEl?.addEventListener("paste", handleImagePaste);
      elements.messageImageFileEl?.addEventListener("change", event => {
        void insertImageFiles(event.target?.files);
      });
      elements.messageEl?.addEventListener("input", () => {
        commandSelection = 0;
        syncInputHeight();
        syncMobileAction();
        renderCommandMenu();
        options.onInput?.();
      });
      elements.messageEl?.addEventListener("focus", renderCommandMenu);
      elements.messageEl?.addEventListener("keydown", event => {
        // Raw hardware-keyboard mode consumes the keystroke (sends it live to the serial
        // port) before any line-composer behaviour runs.
        if (options.handleRawKey?.(event)) return;
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
      syncImageUploadState();
    }

    return Object.freeze({
      bind,
      insertImageFiles,
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
