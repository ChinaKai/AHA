(() => {
  function asArray(value) {
    return Array.from(value || []);
  }

  function isImageType(value) {
    return String(value || "").toLowerCase().startsWith("image/");
  }

  function clipboardImageFiles(event) {
    const clipboard = event?.clipboardData;
    if (!clipboard) return [];
    const files = [];
    const seen = new Set();
    const addFile = file => {
      if (!file || seen.has(file) || !isImageType(file.type)) return;
      seen.add(file);
      files.push(file);
    };
    asArray(clipboard.items).forEach(item => {
      if (!isImageType(item?.type) || typeof item.getAsFile !== "function") return;
      addFile(item.getAsFile());
    });
    asArray(clipboard.files).forEach(addFile);
    return files;
  }

  function sanitizeAltText(value) {
    return String(value || "pasted image")
      .replace(/[\r\n]+/g, " ")
      .replace(/[\[\]]/g, "")
      .trim() || "pasted image";
  }

  function imageMarkdown(dataUrl, file, options = {}) {
    const altText = sanitizeAltText(options.altText || file?.name || "pasted image");
    return `![${altText}](${dataUrl})`;
  }

  async function markdownForImage(dataUrl, file, index, options = {}) {
    if (typeof options.markdownForImage === "function") {
      return await options.markdownForImage({ dataUrl, file, index });
    }
    return imageMarkdown(dataUrl, file, options);
  }

  function textareaSelection(textarea) {
    const value = String(textarea?.value || "");
    const start = Number.isFinite(textarea?.selectionStart) ? textarea.selectionStart : value.length;
    const end = Number.isFinite(textarea?.selectionEnd) ? textarea.selectionEnd : start;
    return {
      value,
      start: Math.max(0, Math.min(start, value.length)),
      end: Math.max(0, Math.min(end, value.length))
    };
  }

  function imageSnippetForSelection(textarea, markdown) {
    const { value, start, end } = textareaSelection(textarea);
    const before = value.slice(0, start);
    const after = value.slice(end);
    const prefix = before && !before.endsWith("\n") ? "\n\n" : "";
    const suffix = after && !after.startsWith("\n") ? "\n\n" : "\n";
    return `${prefix}${markdown}${suffix}`;
  }

  function dispatchInputEvent(textarea, windowRef) {
    const EventCtor = windowRef?.Event || (typeof Event === "function" ? Event : null);
    if (!EventCtor || typeof textarea?.dispatchEvent !== "function") return;
    textarea.dispatchEvent(new EventCtor("input", { bubbles: true }));
  }

  function insertTextareaText(textarea, text, options = {}) {
    if (!textarea) return;
    const { value, start, end } = textareaSelection(textarea);
    const nextValue = `${value.slice(0, start)}${text}${value.slice(end)}`;
    const nextCursor = start + text.length;
    textarea.value = nextValue;
    if (typeof textarea.setSelectionRange === "function") {
      textarea.setSelectionRange(nextCursor, nextCursor);
    }
    dispatchInputEvent(textarea, options.windowRef);
  }

  function insertTextareaImageMarkdown(textarea, markdown, options = {}) {
    if (!markdown) return;
    insertTextareaText(textarea, imageSnippetForSelection(textarea, markdown), options);
  }

  function readAsDataUrl(file, options = {}) {
    const windowRef = options.windowRef || window;
    const FileReaderCtor = options.FileReader || windowRef.FileReader;
    return new Promise((resolve, reject) => {
      if (typeof FileReaderCtor !== "function") {
        reject(new Error("FileReader is unavailable."));
        return;
      }
      const reader = new FileReaderCtor();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(reader.error || new Error("Failed to read pasted image."));
      reader.readAsDataURL(file);
    });
  }

  async function insertPastedImages(event, textarea, options = {}) {
    const files = clipboardImageFiles(event);
    if (!files.length) return;
    event.preventDefault();
    options.onStart?.(files);
    let inserted = 0;
    for (const [index, file] of files.entries()) {
      try {
        const dataUrl = await readAsDataUrl(file, options);
        if (!isImageType(dataUrl.slice(5))) throw new Error("Pasted file is not an image.");
        const markdown = await markdownForImage(dataUrl, file, index, options);
        if (!markdown) continue;
        insertTextareaImageMarkdown(textarea, markdown, options);
        inserted += 1;
      } catch (err) {
        options.onError?.(err);
      }
    }
    if (inserted) options.onInsert?.(inserted, files);
  }

  function attachTextareaImagePaste(textarea, options = {}) {
    if (!textarea || typeof textarea.addEventListener !== "function") return () => {};
    const listener = event => {
      void insertPastedImages(event, textarea, options);
    };
    textarea.addEventListener("paste", listener);
    return () => textarea.removeEventListener("paste", listener);
  }

  window.AHATextareaImagePaste = Object.freeze({
    attachTextareaImagePaste,
    clipboardImageFiles,
    imageMarkdown,
    insertTextareaImageMarkdown,
    insertTextareaText,
    markdownForImage,
    readAsDataUrl
  });
})();
