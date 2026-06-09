(() => {
  const memoImageMarkdownPattern = /!\[([^\]]*)\]\(([^)\s]+)(?:\s+["'][^)]*["'])?\)/g;

  function memoImageFilenameFromPath(path) {
    const text = String(path || "").trim();
    const cleanPath = text.split("?", 1)[0].split("#", 1)[0];
    if (cleanPath.startsWith("task_memo_assets/")) return cleanPath.slice("task_memo_assets/".length);
    if (cleanPath.startsWith("memo_assets/")) return cleanPath.slice("memo_assets/".length);
    if (cleanPath.startsWith("/api/task-memo-assets/")) {
      const encoded = cleanPath.slice("/api/task-memo-assets/".length);
      try {
        return decodeURIComponent(encoded);
      } catch (_err) {
        return encoded;
      }
    }
    return "";
  }

  function memoImageSrc(path, apiUrl = value => value) {
    const text = String(path || "").trim();
    if (!text) return "";
    if (text.toLowerCase().startsWith("data:image/")) return text;
    const filename = memoImageFilenameFromPath(text);
    return filename ? apiUrl(`/api/task-memo-assets/${encodeURIComponent(filename)}`) : "";
  }

  function safeMarkdownLinkHref(value) {
    const href = String(value || "").trim();
    return /^(https?:|mailto:|\/|#|\.)/i.test(href) ? href : "";
  }

  function createImageMarkdownNode(documentRef, markdown, alt, path, options = {}) {
    const src = memoImageSrc(path, options.apiUrl);
    if (!src) return documentRef.createTextNode(markdown);
    const wrapper = documentRef.createElement("span");
    wrapper.className = "task-memo-inline-image";
    wrapper.contentEditable = "false";
    wrapper.dataset.memoImageMarkdown = markdown;
    wrapper.title = path || alt;
    wrapper.setAttribute("role", "button");
    wrapper.setAttribute("tabindex", "0");
    wrapper.setAttribute("aria-label", options.t?.("memo.image_open", "Open image") || "Open image");
    const image = documentRef.createElement("img");
    image.src = src;
    image.alt = alt || options.t?.("memo.pasted_image_alt", "pasted image") || "pasted image";
    wrapper.appendChild(image);
    return wrapper;
  }

  function appendInlineMarkdown(parent, text, options = {}) {
    const documentRef = options.documentRef || parent?.ownerDocument;
    if (!documentRef) return;
    const source = String(text || "");
    const pattern = /!\[([^\]]*)\]\(([^)\s]+)(?:\s+["'][^)]*["'])?\)|\[([^\]]+)\]\(([^)\s]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*]+)\*|_([^_]+)_/g;
    let cursor = 0;
    let match = pattern.exec(source);
    while (match) {
      if (match.index > cursor) parent.appendChild(documentRef.createTextNode(source.slice(cursor, match.index)));
      if (match[1] !== undefined) {
        parent.appendChild(createImageMarkdownNode(documentRef, match[0], match[1], match[2], options));
      } else if (match[3] !== undefined) {
        const href = safeMarkdownLinkHref(match[4]);
        if (href) {
          const link = documentRef.createElement("a");
          link.href = href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = match[3];
          parent.appendChild(link);
        } else {
          parent.appendChild(documentRef.createTextNode(match[0]));
        }
      } else if (match[5] !== undefined) {
        const code = documentRef.createElement("code");
        code.textContent = match[5];
        parent.appendChild(code);
      } else if (match[6] !== undefined || match[7] !== undefined) {
        const strong = documentRef.createElement("strong");
        strong.textContent = match[6] || match[7] || "";
        parent.appendChild(strong);
      } else {
        const emphasis = documentRef.createElement("em");
        emphasis.textContent = match[8] || match[9] || "";
        parent.appendChild(emphasis);
      }
      cursor = match.index + match[0].length;
      match = pattern.exec(source);
    }
    if (cursor < source.length) parent.appendChild(documentRef.createTextNode(source.slice(cursor)));
  }

  function appendMarkdownParagraph(parent, text, options = {}) {
    const paragraph = options.documentRef.createElement("p");
    appendInlineMarkdown(paragraph, text, options);
    parent.appendChild(paragraph);
  }

  function appendMarkdownCodeBlock(parent, lines, documentRef) {
    const pre = documentRef.createElement("pre");
    const code = documentRef.createElement("code");
    code.textContent = lines.join("\n");
    pre.appendChild(code);
    parent.appendChild(pre);
  }

  function renderMarkdownPreview(parent, markdown, options = {}) {
    const documentRef = options.documentRef || parent?.ownerDocument;
    if (!parent || !documentRef) return;
    const previewOptions = { ...options, documentRef };
    const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
    let listEl = null;
    let listType = "";
    let inCodeBlock = false;
    let codeLines = [];
    const closeList = () => {
      listEl = null;
      listType = "";
    };
    const ensureList = type => {
      if (listEl && listType === type) return listEl;
      closeList();
      listType = type;
      listEl = documentRef.createElement(type);
      parent.appendChild(listEl);
      return listEl;
    };
    for (const line of lines) {
      if (/^```/.test(line.trim())) {
        if (inCodeBlock) {
          appendMarkdownCodeBlock(parent, codeLines, documentRef);
          codeLines = [];
          inCodeBlock = false;
        } else {
          closeList();
          inCodeBlock = true;
        }
        continue;
      }
      if (inCodeBlock) {
        codeLines.push(line);
        continue;
      }
      if (!line.trim()) {
        closeList();
        parent.appendChild(documentRef.createElement("br"));
        continue;
      }
      const heading = /^(#{1,6})\s+(.+)$/.exec(line);
      if (heading) {
        closeList();
        const level = Math.min(6, heading[1].length);
        const title = documentRef.createElement(`h${level}`);
        appendInlineMarkdown(title, heading[2], previewOptions);
        parent.appendChild(title);
        continue;
      }
      const unordered = /^\s*[-*+]\s+(.+)$/.exec(line);
      if (unordered) {
        const item = documentRef.createElement("li");
        appendInlineMarkdown(item, unordered[1], previewOptions);
        ensureList("ul").appendChild(item);
        continue;
      }
      const ordered = /^\s*\d+\.\s+(.+)$/.exec(line);
      if (ordered) {
        const item = documentRef.createElement("li");
        appendInlineMarkdown(item, ordered[1], previewOptions);
        ensureList("ol").appendChild(item);
        continue;
      }
      const quote = /^>\s?(.*)$/.exec(line);
      if (quote) {
        closeList();
        const blockquote = documentRef.createElement("blockquote");
        appendInlineMarkdown(blockquote, quote[1], previewOptions);
        parent.appendChild(blockquote);
        continue;
      }
      closeList();
      appendMarkdownParagraph(parent, line, previewOptions);
    }
    closeList();
    if (inCodeBlock) appendMarkdownCodeBlock(parent, codeLines, documentRef);
  }

  function createTaskMemoMarkdownTools(options = {}) {
    const windowRef = options.windowRef || window;
    const documentRef = options.documentRef || windowRef.document;
    const elements = options.elements || {};
    const t = options.t || ((_key, fallback = "") => fallback);
    const apiUrl = options.apiUrl || (value => value);
    const imagePaste = options.textareaImagePaste || windowRef.AHATextareaImagePaste;
    let memoImageViewerEl = null;
    let memoImageViewerImgEl = null;
    let detachImagePaste = null;
    let detachModeControls = null;
    let markdownMode = "preview";

    function syncMarkdownMode() {
      const isEdit = markdownMode === "edit";
      if (elements.taskMemoMarkdownEditorEl) {
        elements.taskMemoMarkdownEditorEl.dataset.mode = markdownMode;
        elements.taskMemoMarkdownEditorEl.classList.toggle("is-edit", isEdit);
        elements.taskMemoMarkdownEditorEl.classList.toggle("is-preview", !isEdit);
      }
      if (elements.taskMemoEditDescriptionEl) {
        elements.taskMemoEditDescriptionEl.hidden = !isEdit;
        elements.taskMemoEditDescriptionEl.setAttribute("aria-hidden", String(!isEdit));
      }
      if (elements.taskMemoDescriptionEditorEl) {
        elements.taskMemoDescriptionEditorEl.hidden = isEdit;
        elements.taskMemoDescriptionEditorEl.setAttribute("aria-hidden", String(isEdit));
        elements.taskMemoDescriptionEditorEl.tabIndex = isEdit ? -1 : 0;
      }
      if (elements.taskMemoPreviewModeEl) {
        elements.taskMemoPreviewModeEl.classList.toggle("active", !isEdit);
        elements.taskMemoPreviewModeEl.setAttribute("aria-pressed", String(!isEdit));
      }
      if (elements.taskMemoEditModeEl) {
        elements.taskMemoEditModeEl.classList.toggle("active", isEdit);
        elements.taskMemoEditModeEl.setAttribute("aria-pressed", String(isEdit));
      }
    }

    function setMode(mode, controlOptions = {}) {
      markdownMode = mode === "edit" ? "edit" : "preview";
      renderDescriptionEditor();
      syncMarkdownMode();
      if (markdownMode === "edit" && controlOptions.focus !== false) {
        elements.taskMemoEditDescriptionEl?.focus?.();
      }
    }

    function setDisabled(disabled) {
      const isDisabled = Boolean(disabled);
      [elements.taskMemoPreviewModeEl, elements.taskMemoEditModeEl].forEach(element => {
        if (element) element.disabled = isDisabled;
      });
      if (elements.taskMemoEditDescriptionEl) elements.taskMemoEditDescriptionEl.disabled = isDisabled;
      if (elements.taskMemoDescriptionEditorEl) {
        elements.taskMemoDescriptionEditorEl.setAttribute("aria-disabled", String(isDisabled));
      }
      if (isDisabled) markdownMode = "preview";
      syncMarkdownMode();
    }

    function renderDescriptionEditor() {
      const editorEl = elements.taskMemoDescriptionEditorEl;
      if (!editorEl || !documentRef) return;
      editorEl.innerHTML = "";
      renderMarkdownPreview(editorEl, elements.taskMemoEditDescriptionEl?.value || "", { documentRef, apiUrl, t });
      if (!editorEl.childNodes.length) editorEl.appendChild(documentRef.createElement("br"));
      syncMarkdownMode();
    }

    function closeMemoImageViewer() {
      if (!memoImageViewerEl) return;
      if (typeof memoImageViewerEl.close === "function" && memoImageViewerEl.open) {
        memoImageViewerEl.close();
      } else {
        memoImageViewerEl.removeAttribute("open");
      }
      memoImageViewerImgEl?.removeAttribute("src");
    }

    function ensureMemoImageViewer() {
      if (memoImageViewerEl || !documentRef?.body) return memoImageViewerEl;
      const dialog = documentRef.createElement("dialog");
      dialog.className = "task-memo-image-viewer";
      dialog.setAttribute("aria-label", t("memo.image_viewer", "Memo image"));
      const frame = documentRef.createElement("div");
      frame.className = "task-memo-image-viewer-frame";
      const closeButton = documentRef.createElement("button");
      closeButton.type = "button";
      closeButton.className = "task-memo-image-viewer-close";
      closeButton.textContent = t("common.close", "Close");
      const image = documentRef.createElement("img");
      image.className = "task-memo-image-viewer-img";
      frame.appendChild(closeButton);
      frame.appendChild(image);
      dialog.appendChild(frame);
      dialog.addEventListener("click", event => {
        if (event.target === dialog) closeMemoImageViewer();
      });
      closeButton.addEventListener("click", closeMemoImageViewer);
      documentRef.body.appendChild(dialog);
      memoImageViewerEl = dialog;
      memoImageViewerImgEl = image;
      return memoImageViewerEl;
    }

    function openMemoImageViewer(image) {
      const src = image?.currentSrc || image?.src || "";
      if (!src) return false;
      const viewer = ensureMemoImageViewer();
      if (!viewer || !memoImageViewerImgEl) return false;
      memoImageViewerImgEl.src = src;
      memoImageViewerImgEl.alt = image.alt || t("memo.pasted_image_alt", "pasted image");
      if (typeof viewer.showModal === "function") {
        if (!viewer.open) viewer.showModal();
      } else {
        viewer.setAttribute("open", "");
      }
      return true;
    }

    function clickedMemoImage(target) {
      if (!target || typeof target.closest !== "function") return null;
      const wrapper = target.closest(".task-memo-inline-image");
      if (!wrapper || !elements.taskMemoDescriptionEditorEl?.contains(wrapper)) return null;
      return wrapper.querySelector("img");
    }

    function openClickedImage(target) {
      const image = clickedMemoImage(target);
      return image ? openMemoImageViewer(image) : false;
    }

    function insertDescriptionImageMarkdown(markdown) {
      const textarea = elements.taskMemoEditDescriptionEl;
      if (!textarea || !markdown) return;
      if (imagePaste?.insertTextareaImageMarkdown) {
        imagePaste.insertTextareaImageMarkdown(textarea, markdown, { windowRef });
      } else {
        const value = String(textarea.value || "");
        const start = Number.isFinite(textarea.selectionStart) ? textarea.selectionStart : value.length;
        const end = Number.isFinite(textarea.selectionEnd) ? textarea.selectionEnd : start;
        const before = value.slice(0, start);
        const after = value.slice(end);
        const prefix = before && !before.endsWith("\n") ? "\n\n" : "";
        const suffix = after && !after.startsWith("\n") ? "\n\n" : "\n";
        const snippet = `${prefix}${markdown}${suffix}`;
        textarea.value = `${before}${snippet}${after}`;
        textarea.setSelectionRange?.(start + snippet.length, start + snippet.length);
      }
      renderDescriptionEditor();
      options.updateSaveState?.();
    }

    async function uploadMemoImageMarkdown({ dataUrl, file }) {
      const payload = {
        filename: file?.name || "",
        content_type: file?.type || "",
        data_url: dataUrl
      };
      const response = await options.fetchJson(apiUrl("/api/task-memo-assets"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      }, "Failed to upload memo image");
      return response?.asset?.markdown || "";
    }

    async function insertMemoImageFile(file, index = 0) {
      if (!imagePaste?.readAsDataUrl || !elements.taskMemoEditDescriptionEl || !elements.taskMemoDescriptionEditorEl) {
        throw new Error("Memo image upload is unavailable.");
      }
      const dataUrl = await imagePaste.readAsDataUrl(file, { windowRef });
      const markdown = await uploadMemoImageMarkdown({ dataUrl, file, index });
      insertDescriptionImageMarkdown(markdown);
      return Boolean(markdown);
    }

    async function insertMemoImageFiles(files) {
      const selectedFiles = Array.from(files || []).filter(file => String(file?.type || "").toLowerCase().startsWith("image/"));
      if (!selectedFiles.length) return;
      options.setState?.(t("memo.image_uploading", "Saving image..."));
      let inserted = 0;
      for (const [index, file] of selectedFiles.entries()) {
        try {
          if (await insertMemoImageFile(file, index)) inserted += 1;
        } catch (err) {
          options.consoleRef?.warn?.("Failed to add memo image", err);
          options.setState?.(t("memo.image_paste_failed", "Failed to paste image."));
        }
      }
      if (inserted) {
        options.updateSaveState?.();
        options.setState?.(t("memo.image_pasted", "Image pasted."));
      }
    }

    function attachImagePaste() {
      if (detachImagePaste || !elements.taskMemoEditDescriptionEl || !imagePaste?.clipboardImageFiles) return;
      const listener = event => {
        const files = imagePaste.clipboardImageFiles(event);
        if (!files.length) return;
        event.preventDefault();
        void insertMemoImageFiles(files).catch(options.reportError);
      };
      elements.taskMemoEditDescriptionEl.addEventListener("paste", listener);
      elements.taskMemoDescriptionEditorEl?.addEventListener("paste", listener);
      detachImagePaste = () => {
        elements.taskMemoEditDescriptionEl?.removeEventListener("paste", listener);
        elements.taskMemoDescriptionEditorEl?.removeEventListener("paste", listener);
      };
    }

    function attachModeControls() {
      if (detachModeControls) return;
      const previewListener = () => setMode("preview", { focus: false });
      const editListener = () => setMode("edit");
      elements.taskMemoPreviewModeEl?.addEventListener("click", previewListener);
      elements.taskMemoEditModeEl?.addEventListener("click", editListener);
      detachModeControls = () => {
        elements.taskMemoPreviewModeEl?.removeEventListener("click", previewListener);
        elements.taskMemoEditModeEl?.removeEventListener("click", editListener);
      };
    }

    function bind() {
      attachImagePaste();
      attachModeControls();
      syncMarkdownMode();
    }

    return Object.freeze({
      bind,
      insertMemoImageFiles,
      openClickedImage,
      renderDescriptionEditor,
      setDisabled,
      setMode
    });
  }

  window.AHATaskMemoMarkdown = Object.freeze({
    appendInlineMarkdown,
    createTaskMemoMarkdownTools,
    memoImageFilenameFromPath,
    memoImageSrc,
    renderMarkdownPreview,
    safeMarkdownLinkHref
  });
})();
