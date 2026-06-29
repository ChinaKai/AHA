(() => {
  const memoImageMarkdownPattern = /!\[([^\]]*)\]\(([^)\s]+)(?:\s+["'][^)]*["'])?\)/g;
  const memoAttachmentMarkdownPattern = /\[([^\]]+)\]\(([^)\s]+)(?:\s+["'][^)]*["'])?\)/g;

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

  function memoAssetHref(path, apiUrl = value => value) {
    const text = String(path || "").trim();
    if (!text) return "";
    const filename = memoImageFilenameFromPath(text);
    return filename ? apiUrl(`/api/task-memo-assets/${encodeURIComponent(filename)}`) : "";
  }

  function memoAttachmentLabel(value) {
    return String(value || "")
      .replace(/^Attachment:\s*/i, "")
      .trim() || "attachment";
  }

  function memoAssetContentType(file = {}) {
    const type = String(file?.type || "").trim().toLowerCase();
    if (type === "image/jpg") return "image/jpeg";
    if (type) return type;
    const name = String(file?.name || "").trim().toLowerCase();
    if (/\.(jpe?g)$/.test(name)) return "image/jpeg";
    if (/\.png$/.test(name)) return "image/png";
    if (/\.svg$/.test(name)) return "image/svg+xml";
    if (/\.gif$/.test(name)) return "image/gif";
    if (/\.webp$/.test(name)) return "image/webp";
    if (/\.avif$/.test(name)) return "image/avif";
    if (/\.bmp$/.test(name)) return "image/bmp";
    if (/\.heic$/.test(name)) return "image/heic";
    if (/\.heif$/.test(name)) return "image/heif";
    if (/\.pdf$/.test(name)) return "application/pdf";
    if (/\.txt$/.test(name)) return "text/plain";
    if (/\.md$/.test(name)) return "text/markdown";
    if (/\.csv$/.test(name)) return "text/csv";
    if (/\.json$/.test(name)) return "application/json";
    if (/\.zip$/.test(name)) return "application/zip";
    if (/\.docx$/.test(name)) return "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
    if (/\.xlsx$/.test(name)) return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
    if (/\.pptx$/.test(name)) return "application/vnd.openxmlformats-officedocument.presentationml.presentation";
    if (/\.doc$/.test(name)) return "application/msword";
    if (/\.xls$/.test(name)) return "application/vnd.ms-excel";
    if (/\.ppt$/.test(name)) return "application/vnd.ms-powerpoint";
    return "application/octet-stream";
  }

  function normalizeMemoImageDataUrl(dataUrl, file) {
    const text = String(dataUrl || "");
    const type = memoAssetContentType(file);
    if (!type || !text.startsWith("data:")) return text;
    return text.replace(/^data:[^;,]*;base64,/i, `data:${type};base64,`);
  }

  async function uploadMemoImageJsonMarkdown({ dataUrl, file } = {}, options = {}) {
    const apiUrl = options.apiUrl || (value => value);
    const fetchJson = options.fetchJson;
    if (typeof fetchJson !== "function") throw new Error("Memo image upload is unavailable.");
    const contentType = memoAssetContentType(file);
    const payload = {
      filename: file?.name || "",
      content_type: contentType,
      data_url: normalizeMemoImageDataUrl(dataUrl, file)
    };
    const response = await fetchJson(apiUrl("/api/task-memo-assets"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }, "Failed to upload memo image");
    return response?.asset?.markdown || "";
  }

  async function uploadMemoImageFormDataMarkdown({ file } = {}, options = {}) {
    const windowRef = options.windowRef || window;
    const apiUrl = options.apiUrl || (value => value);
    const fetchJson = options.fetchJson;
    if (typeof fetchJson !== "function" || !windowRef.FormData || !file) {
      throw new Error("Memo image upload is unavailable.");
    }
    const contentType = memoAssetContentType(file);
    const form = new windowRef.FormData();
    form.append("image", file, file?.name || "memo-image");
    form.append("filename", file?.name || "");
    form.append("content_type", contentType);
    const response = await fetchJson(apiUrl("/api/task-memo-assets"), {
      method: "POST",
      body: form
    }, "Failed to upload memo image");
    return response?.asset?.markdown || "";
  }

  async function uploadMemoImageMarkdown({ file, dataUrl } = {}, options = {}) {
    const windowRef = options.windowRef || window;
    const imagePaste = options.imagePaste || windowRef.AHATextareaImagePaste;
    if (windowRef.FormData && file) {
      return uploadMemoImageFormDataMarkdown({ file }, options);
    }
    const sourceDataUrl = dataUrl || await imagePaste?.readAsDataUrl?.(file, { windowRef });
    if (!sourceDataUrl) throw new Error("Memo image upload is unavailable.");
    return uploadMemoImageJsonMarkdown({ dataUrl: sourceDataUrl, file }, options);
  }

  function collectMemoAttachments(markdown, options = {}) {
    const source = String(markdown || "");
    const attachments = [];
    const seen = new Set();
    memoAttachmentMarkdownPattern.lastIndex = 0;
    let match = memoAttachmentMarkdownPattern.exec(source);
    while (match) {
      const label = String(match[1] || "").trim();
      const path = String(match[2] || "").trim();
      if (source[match.index - 1] !== "!" && /^Attachment:\s*/i.test(label)) {
        const href = memoAssetHref(path, options.apiUrl);
        if (href && !seen.has(href)) {
          seen.add(href);
          attachments.push({ href, label: memoAttachmentLabel(label) });
        }
      }
      match = memoAttachmentMarkdownPattern.exec(source);
    }
    return attachments;
  }

  function safeMarkdownLinkHref(value) {
    const href = String(value || "").trim();
    if (/^www\./i.test(href)) return `https://${href}`;
    if (/^[a-z0-9.-]+\.[a-z]{2,}(?:[/:?#]|$)/i.test(href)) return `https://${href}`;
    return /^(https?:|mailto:|\/|#|\.)/i.test(href) ? href : "";
  }

  function createMarkdownLinkNode(documentRef, label, href, options = {}) {
    const resolveHref = typeof options.linkHref === "function" ? options.linkHref : safeMarkdownLinkHref;
    const safeHref = resolveHref(href);
    if (!safeHref) return null;
    const link = documentRef.createElement("a");
    link.href = safeHref;
    if (!String(safeHref).startsWith("#")) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    link.textContent = label || safeHref;
    return link;
  }

  function createImageMarkdownNode(documentRef, markdown, alt, path, options = {}) {
    // Pluggable resolver so non-memo callers (e.g. the knowledge capture inbox)
    // can resolve their own image URLs. Defaults to the memo asset resolver, so
    // existing memo rendering is unchanged.
    const resolveSrc = typeof options.imageSrc === "function" ? options.imageSrc : memoImageSrc;
    const src = resolveSrc(path, options.apiUrl);
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
    if (path) image.dataset.filename = path;
    wrapper.appendChild(image);
    return wrapper;
  }

  function appendInlineMarkdown(parent, text, options = {}) {
    const documentRef = options.documentRef || parent?.ownerDocument;
    if (!documentRef) return;
    const source = String(text || "");
    const pattern = /!\[([^\]]*)\]\(([^)\s]+)(?:\s+["'][^)]*["'])?\)|\[([^\]]+)\]\(([^)\s]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|__([^_]+)__|~~([^~]+)~~|<((?:https?:\/\/|mailto:)[^>\s]+)>|((?:https?:\/\/|www\.)[^\s<]+)|\*([^*]+)\*|_([^_]+)_/g;
    let cursor = 0;
    let match = pattern.exec(source);
    while (match) {
      if (match.index > cursor) parent.appendChild(documentRef.createTextNode(source.slice(cursor, match.index)));
      if (match[1] !== undefined) {
        parent.appendChild(createImageMarkdownNode(documentRef, match[0], match[1], match[2], options));
      } else if (match[3] !== undefined) {
        const link = createMarkdownLinkNode(documentRef, match[3], match[4], options);
        if (link) {
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
      } else if (match[8] !== undefined) {
        const deleted = documentRef.createElement("del");
        deleted.textContent = match[8] || "";
        parent.appendChild(deleted);
      } else if (match[9] !== undefined) {
        const link = createMarkdownLinkNode(documentRef, match[9], match[9], options);
        parent.appendChild(link || documentRef.createTextNode(match[0]));
      } else if (match[10] !== undefined) {
        const trailing = /[),.;:!?]+$/.exec(match[10])?.[0] || "";
        const href = trailing ? match[10].slice(0, -trailing.length) : match[10];
        const link = createMarkdownLinkNode(documentRef, href, href, options);
        parent.appendChild(link || documentRef.createTextNode(match[0]));
        if (trailing) parent.appendChild(documentRef.createTextNode(trailing));
      } else {
        const emphasis = documentRef.createElement("em");
        emphasis.textContent = match[11] || match[12] || "";
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

  function appendMarkdownCodeBlock(parent, lines, documentRef, language = "") {
    const pre = documentRef.createElement("pre");
    const code = documentRef.createElement("code");
    const lang = String(language || "").trim().replace(/[^\w-]/g, "");
    if (lang) {
      code.className = `language-${lang}`;
      code.dataset.language = lang;
    }
    code.textContent = lines.join("\n");
    pre.appendChild(code);
    parent.appendChild(pre);
  }

  function appendMarkdownTaskItem(parent, checked, text, options = {}) {
    const documentRef = options.documentRef;
    const item = documentRef.createElement("li");
    item.className = "task-memo-task-list-item";
    const checkbox = documentRef.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = Boolean(checked);
    checkbox.disabled = true;
    checkbox.setAttribute("aria-hidden", "true");
    item.appendChild(checkbox);
    const label = documentRef.createElement("span");
    appendInlineMarkdown(label, text, options);
    item.appendChild(label);
    parent.appendChild(item);
  }

  function splitMarkdownTableRow(line) {
    let text = String(line || "").trim();
    if (!text.includes("|")) return [];
    if (text.startsWith("|")) text = text.slice(1);
    if (text.endsWith("|")) text = text.slice(0, -1);
    return text.split("|").map(cell => cell.trim());
  }

  function markdownTableAlignments(line) {
    const cells = splitMarkdownTableRow(line);
    if (!cells.length) return null;
    const alignments = [];
    for (const cell of cells) {
      const marker = cell.replace(/\s+/g, "");
      if (!/^:?-{3,}:?$/.test(marker)) return null;
      if (marker.startsWith(":") && marker.endsWith(":")) {
        alignments.push("center");
      } else if (marker.endsWith(":")) {
        alignments.push("right");
      } else {
        alignments.push("");
      }
    }
    return alignments;
  }

  function markdownTableAt(lines, index) {
    if (index + 1 >= lines.length) return null;
    const header = splitMarkdownTableRow(lines[index]);
    const alignments = markdownTableAlignments(lines[index + 1]);
    if (!header.length || !alignments || header.length !== alignments.length) return null;
    const rows = [];
    let cursor = index + 2;
    while (cursor < lines.length && lines[cursor].trim() && lines[cursor].includes("|")) {
      const cells = splitMarkdownTableRow(lines[cursor]);
      if (!cells.length) break;
      rows.push(cells);
      cursor += 1;
    }
    return { header, alignments, rows, nextIndex: cursor - 1 };
  }

  function appendMarkdownTable(parent, table, options = {}) {
    const documentRef = options.documentRef;
    const element = documentRef.createElement("table");
    const thead = documentRef.createElement("thead");
    const headRow = documentRef.createElement("tr");
    table.header.forEach((cell, index) => {
      const th = documentRef.createElement("th");
      if (table.alignments[index]) th.style.textAlign = table.alignments[index];
      appendInlineMarkdown(th, cell, options);
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    element.appendChild(thead);
    const tbody = documentRef.createElement("tbody");
    for (const row of table.rows) {
      const tr = documentRef.createElement("tr");
      for (let index = 0; index < table.header.length; index += 1) {
        const td = documentRef.createElement("td");
        if (table.alignments[index]) td.style.textAlign = table.alignments[index];
        appendInlineMarkdown(td, row[index] || "", options);
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    element.appendChild(tbody);
    parent.appendChild(element);
  }

  function renderMemoAttachmentList(parent, markdown, options = {}) {
    const documentRef = options.documentRef || parent?.ownerDocument;
    if (!parent || !documentRef) return;
    parent.innerHTML = "";
    const attachments = collectMemoAttachments(markdown, options);
    parent.hidden = !attachments.length;
    parent.setAttribute("aria-hidden", String(!attachments.length));
    if (!attachments.length) return;
    const title = documentRef.createElement("div");
    title.className = "task-memo-attachments-title";
    title.textContent = options.t?.("memo.attachments", "Attachments") || "Attachments";
    const list = documentRef.createElement("div");
    list.className = "task-memo-attachments-list";
    for (const attachment of attachments) {
      const link = documentRef.createElement("a");
      link.className = "task-memo-attachment-link";
      link.href = attachment.href;
      link.download = attachment.label;
      link.textContent = attachment.label;
      list.appendChild(link);
    }
    parent.appendChild(title);
    parent.appendChild(list);
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
    let codeLanguage = "";
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
    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      const fence = /^```\s*([^`]*)$/.exec(line.trim());
      if (fence) {
        if (inCodeBlock) {
          appendMarkdownCodeBlock(parent, codeLines, documentRef, codeLanguage);
          codeLines = [];
          codeLanguage = "";
          inCodeBlock = false;
        } else {
          closeList();
          codeLanguage = String(fence[1] || "").trim().split(/\s+/, 1)[0] || "";
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
      const table = markdownTableAt(lines, index);
      if (table) {
        closeList();
        appendMarkdownTable(parent, table, previewOptions);
        index = table.nextIndex;
        continue;
      }
      if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
        closeList();
        parent.appendChild(documentRef.createElement("hr"));
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
      const task = /^\s*[-*+]\s+\[([ xX])\]\s+(.+)$/.exec(line);
      if (task) {
        const list = ensureList("ul");
        list.classList.add("task-memo-task-list");
        appendMarkdownTaskItem(list, task[1].toLowerCase() === "x", task[2], previewOptions);
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
    if (inCodeBlock) appendMarkdownCodeBlock(parent, codeLines, documentRef, codeLanguage);
  }

  function renderMarkdownHtml(markdown, options = {}) {
    const documentRef = options.documentRef || (typeof document !== "undefined" ? document : null);
    if (!documentRef?.createElement) return "";
    const container = documentRef.createElement("div");
    renderMarkdownPreview(container, markdown, { ...options, documentRef });
    return container.innerHTML;
  }

  const imageViewerStateByDocument = new WeakMap();

  function viewerState(documentRef) {
    if (!documentRef) return null;
    let state = imageViewerStateByDocument.get(documentRef);
    if (!state) {
      state = { dialog: null, image: null };
      imageViewerStateByDocument.set(documentRef, state);
    }
    return state;
  }

  function closeImageViewer(documentRef) {
    const state = viewerState(documentRef);
    if (!state?.dialog) return;
    if (typeof state.dialog.close === "function" && state.dialog.open) {
      state.dialog.close();
    } else {
      state.dialog.removeAttribute("open");
    }
    state.image?.removeAttribute("src");
    if (state.download) {
      state.download.removeAttribute("href");
      state.download.removeAttribute("download");
    }
  }

  function inferImageFilename(src, fallback) {
    if (!src) return fallback || "";
    try {
      const url = new URL(src, typeof window !== "undefined" ? window.location.href : "http://localhost/");
      const last = url.pathname.split("/").filter(Boolean).pop();
      if (last) {
        try { return decodeURIComponent(last); } catch (_) { return last; }
      }
      if (url.protocol === "data:") {
        const match = /^data:image\/([a-z0-9+.-]+);/i.exec(url.pathname);
        return `pasted-image.${(match && match[1]) ? match[1].replace("jpeg", "jpg") : "png"}`;
      }
    } catch (_) {
      const cleaned = String(src).split("?")[0].split("#")[0];
      const last = cleaned.split("/").filter(Boolean).pop();
      if (last) return last;
    }
    return fallback || "image";
  }

  function ensureImageViewer(documentRef, t = (_key, fallback = "") => fallback) {
    const state = viewerState(documentRef);
    if (!state || state.dialog || !documentRef?.body) return state?.dialog || null;
    const dialog = documentRef.createElement("dialog");
    dialog.className = "task-memo-image-viewer";
    dialog.setAttribute("aria-label", t("memo.image_viewer", "Memo image"));
    const frame = documentRef.createElement("div");
    frame.className = "task-memo-image-viewer-frame";
    const actions = documentRef.createElement("div");
    actions.className = "task-memo-image-viewer-actions";
    const closeButton = documentRef.createElement("button");
    closeButton.type = "button";
    closeButton.className = "task-memo-image-viewer-close";
    closeButton.setAttribute("aria-label", t("common.close", "Close"));
    closeButton.title = t("common.close", "Close");
    closeButton.textContent = t("common.close", "Close");
    const downloadButton = documentRef.createElement("a");
    downloadButton.className = "task-memo-image-viewer-download";
    downloadButton.setAttribute("role", "button");
    downloadButton.textContent = t("memo.image_download", "Download");
    const image = documentRef.createElement("img");
    image.className = "task-memo-image-viewer-img";
    actions.appendChild(downloadButton);
    actions.appendChild(closeButton);
    frame.appendChild(image);
    dialog.appendChild(frame);
    dialog.appendChild(actions);
    dialog.addEventListener("click", event => {
      if (event.target === dialog) closeImageViewer(documentRef);
    });
    closeButton.addEventListener("click", () => closeImageViewer(documentRef));
    documentRef.body.appendChild(dialog);
    state.dialog = dialog;
    state.image = image;
    state.download = downloadButton;
    return dialog;
  }

  function openImageViewer(image, options = {}) {
    const documentRef = options.documentRef || image?.ownerDocument || (typeof document !== "undefined" ? document : null);
    const t = options.t || ((_key, fallback = "") => fallback);
    const src = image?.currentSrc || image?.src || "";
    if (!src || !documentRef) return false;
    const viewer = ensureImageViewer(documentRef, t);
    const state = viewerState(documentRef);
    if (!viewer || !state?.image) return false;
    state.image.src = src;
    state.image.alt = image.alt || t("memo.pasted_image_alt", "pasted image");
    if (state.download) {
      const filename = inferImageFilename(src, image.getAttribute("data-filename") || image.alt || "image");
      state.download.setAttribute("href", src);
      state.download.setAttribute("download", filename);
      state.download.setAttribute("aria-label", t("memo.image_download", "Download"));
      state.download.title = t("memo.image_download", "Download");
    }
    if (typeof viewer.showModal === "function") {
      if (!viewer.open) viewer.showModal();
    } else {
      viewer.setAttribute("open", "");
    }
    return true;
  }

  function clickedInlineImage(target, root = null) {
    if (!target || typeof target.closest !== "function") return null;
    const wrapper = target.closest(".task-memo-inline-image");
    if (!wrapper || (root && !root.contains(wrapper))) return null;
    return wrapper.querySelector("img");
  }

  function openClickedImage(target, options = {}) {
    const image = clickedInlineImage(target, options.root || null);
    return image ? openImageViewer(image, options) : false;
  }

  function createTaskMemoMarkdownTools(options = {}) {
    const windowRef = options.windowRef || window;
    const documentRef = options.documentRef || windowRef.document;
    const elements = options.elements || {};
    const t = options.t || ((_key, fallback = "") => fallback);
    const apiUrl = options.apiUrl || (value => value);
    const imagePaste = options.textareaImagePaste || windowRef.AHATextareaImagePaste;
    let detachImagePaste = null;
    let detachModeControls = null;
    let markdownMode = "preview";
    let markdownDisabled = false;

    function syncMarkdownMode() {
      const isEdit = markdownMode === "edit";
      if (elements.taskMemoMarkdownEditorEl) {
        elements.taskMemoMarkdownEditorEl.dataset.mode = markdownMode;
        elements.taskMemoMarkdownEditorEl.classList.toggle("is-edit", isEdit);
        elements.taskMemoMarkdownEditorEl.classList.toggle("is-preview", markdownMode === "preview");
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
        elements.taskMemoPreviewModeEl.classList.toggle("active", markdownMode === "preview");
        elements.taskMemoPreviewModeEl.setAttribute("aria-pressed", String(markdownMode === "preview"));
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
      markdownDisabled = Boolean(disabled);
      [elements.taskMemoPreviewModeEl, elements.taskMemoEditModeEl].forEach(element => {
        if (element) element.disabled = markdownDisabled;
      });
      if (elements.taskMemoEditDescriptionEl) elements.taskMemoEditDescriptionEl.disabled = markdownDisabled;
      if (elements.taskMemoDescriptionEditorEl) {
        elements.taskMemoDescriptionEditorEl.setAttribute("aria-disabled", String(markdownDisabled));
      }
      if (markdownDisabled && markdownMode === "edit") markdownMode = "preview";
      syncMarkdownMode();
    }

    function renderDescriptionEditor() {
      const editorEl = elements.taskMemoDescriptionEditorEl;
      const markdown = elements.taskMemoEditDescriptionEl?.value || "";
      if (editorEl && documentRef) {
        editorEl.innerHTML = "";
        renderMarkdownPreview(editorEl, markdown, { documentRef, apiUrl, t });
        if (!editorEl.childNodes.length) editorEl.appendChild(documentRef.createElement("br"));
      }
      renderMemoAttachmentList(elements.taskMemoAttachmentListEl, markdown, { documentRef, apiUrl, t });
      syncMarkdownMode();
    }

    function clickedMemoImage(target) {
      return clickedInlineImage(target, elements.taskMemoDescriptionEditorEl || null);
    }

    function openMemoClickedImage(target) {
      const image = clickedMemoImage(target);
      return image ? openImageViewer(image, { documentRef, t }) : false;
    }

    function insertDescriptionImageMarkdown(markdown) {
      const textarea = elements.taskMemoEditDescriptionEl;
      if (markdownDisabled) return;
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

    function memoImageFileType(file = {}) {
      const type = memoAssetContentType(file);
      return type.startsWith("image/") ? type : "";
    }

    function memoImageFailureMessage(prefix, err) {
      const detail = String(err?.payload?.error || err?.message || "").trim()
        .replace(/^Failed to upload memo image:\s*/i, "");
      return detail ? `${prefix}: ${detail}` : prefix;
    }

    async function insertMemoImageFile(file, index = 0) {
      if (!elements.taskMemoEditDescriptionEl || !elements.taskMemoDescriptionEditorEl) {
        throw new Error("Memo image upload is unavailable.");
      }
      const markdown = await uploadMemoImageMarkdown({ file, index }, { fetchJson: options.fetchJson, apiUrl, windowRef, imagePaste });
      insertDescriptionImageMarkdown(markdown);
      return Boolean(markdown);
    }

    async function insertMemoImageFiles(files, insertOptions = {}) {
      const selectedFiles = Array.from(files || []).filter(Boolean);
      if (!selectedFiles.length) return;
      options.setState?.(t("memo.image_uploading", "Saving attachment..."));
      const failureMessage = insertOptions.failureMessage || t("memo.image_upload_failed", "Failed to upload attachment.");
      const successMessage = insertOptions.successMessage || t("memo.image_added", "Attachment added.");
      let inserted = 0;
      for (const [index, file] of selectedFiles.entries()) {
        try {
          if (await insertMemoImageFile(file, index)) inserted += 1;
        } catch (err) {
          options.consoleRef?.warn?.("Failed to add memo image", err);
          options.setState?.(memoImageFailureMessage(failureMessage, err));
        }
      }
      if (inserted) {
        options.updateSaveState?.();
        options.setState?.(successMessage);
      }
    }

    function attachImagePaste() {
      if (detachImagePaste || !elements.taskMemoEditDescriptionEl || !imagePaste?.clipboardImageFiles) return;
      const listener = event => {
        if (markdownDisabled) return;
        const files = imagePaste.clipboardImageFiles(event);
        if (!files.length) return;
        event.preventDefault();
        void insertMemoImageFiles(files, {
          failureMessage: t("memo.image_paste_failed", "Failed to paste image."),
          successMessage: t("memo.image_pasted", "Image pasted.")
        }).catch(options.reportError);
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
      openClickedImage: openMemoClickedImage,
      renderDescriptionEditor,
      renderMemoAttachmentList,
      setDisabled,
      setMode
    });
  }

  window.AHATaskMemoMarkdown = Object.freeze({
    appendInlineMarkdown,
    collectMemoAttachments,
    createTaskMemoMarkdownTools,
    openClickedImage,
    openImageViewer,
    memoImageFilenameFromPath,
    memoImageSrc,
    memoAssetContentType,
    normalizeMemoImageDataUrl,
    renderMarkdownHtml,
    renderMarkdownPreview,
    renderMemoAttachmentList,
    safeMarkdownLinkHref,
    uploadMemoImageFormDataMarkdown,
    uploadMemoImageJsonMarkdown,
    uploadMemoImageMarkdown
  });
})();
