(() => {
  function searchTerms(query) {
    const text = String(query || "").trim();
    if (!text) return [];
    const terms = [text, ...text.split(/\s+/).filter(Boolean)];
    return [...new Set(terms)].sort((left, right) => right.length - left.length);
  }

  function textMatch(value, query) {
    const text = String(value || "");
    const normalized = text.toLocaleLowerCase();
    for (const term of searchTerms(query)) {
      const index = normalized.indexOf(term.toLocaleLowerCase());
      if (index >= 0) return { index, length: term.length };
    }
    return null;
  }

  function reveal(element) {
    element?.setAttribute?.("tabindex", "-1");
    element?.scrollIntoView?.({ block: "center", behavior: "auto" });
    element?.focus?.({ preventScroll: true });
  }

  function highlightFirst(root, query, options = {}) {
    if (!root || !query) return null;
    const documentRef = options.documentRef || root.ownerDocument || document;
    const nodeFilter = documentRef.defaultView?.NodeFilter || window.NodeFilter;
    const walker = documentRef.createTreeWalker(root, nodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
      const parent = node.parentElement;
      if (parent && !parent.closest("mark,script,style,textarea,input")) {
        const match = textMatch(node.nodeValue, query);
        if (match) {
          const range = documentRef.createRange();
          range.setStart(node, match.index);
          range.setEnd(node, match.index + match.length);
          const mark = documentRef.createElement("mark");
          mark.className = "search-deep-link-hit";
          range.surroundContents(mark);
          reveal(mark);
          return mark;
        }
      }
      node = walker.nextNode();
    }
    return null;
  }

  function highlightInput(input, query) {
    const match = textMatch(input?.value, query);
    if (!input || !match) return null;
    input.classList?.add("search-deep-link-input-hit");
    input.setSelectionRange?.(match.index, match.index + match.length);
    input.scrollIntoView?.({ block: "center", behavior: "auto" });
    return input;
  }

  window.AHASearchHighlight = Object.freeze({
    highlightFirst,
    highlightInput,
    searchTerms,
    textMatch,
  });
})();
