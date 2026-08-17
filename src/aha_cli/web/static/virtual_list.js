(function () {
  // Minimal windowing list. Only items near the viewport are in the DOM; a spacer
  // preserves the full scroll height. Chat usage passes an estimated row height and
  // lets the list recalibrate from actual measured heights after each render.
  function createVirtualList(container, options = {}) {
    const defaults = {
      overscan: 8,           // extra items rendered above/below the viewport
      estimatedItemHeight: 40,
      anchorBottom: false,   // when true, keep pinned to the newest (bottom) item
      initialScrollTop: 0,   // scroll offset to restore on mount when not anchoring
      bufferPx: 80           // how far from the bottom still counts as "near bottom"
    };
    const cfg = { ...defaults, ...options };
    let itemCount = 0;
    let measured = [];       // measured heights per index (0 = unknown)
    let lastMeasuredIndex = -1;
    let scrollEl = null;
    let spacerEl = null;
    let renderFn = null;     // (index, isNew) => Node | string
    let onRendered = null;   // called after a render pass with { first, last, itemCount }
    let rafId = 0;
    let anchorBottom = cfg.anchorBottom;
    let pendingAppend = [];

    function estimatedHeight(index) {
      if (index <= lastMeasuredIndex) return measured[index] || cfg.estimatedItemHeight;
      const known = measured[index] || cfg.estimatedItemHeight;
      const remaining = itemCount - 1 - lastMeasuredIndex;
      return known + remaining * cfg.estimatedItemHeight;
    }

    function setMeasuredHeight(index, height) {
      const h = Number(height) || cfg.estimatedItemHeight;
      measured[index] = h;
      if (index > lastMeasuredIndex) {
        for (let i = lastMeasuredIndex + 1; i < index; i++) measured[i] = measured[i] || cfg.estimatedItemHeight;
        lastMeasuredIndex = index;
      }
    }

    function totalHeight() {
      if (!itemCount) return 0;
      let sum = 0;
      const upto = Math.min(itemCount - 1, lastMeasuredIndex);
      for (let i = 0; i <= upto; i++) sum += measured[i] || cfg.estimatedItemHeight;
      const remaining = itemCount - 1 - upto;
      sum += remaining * cfg.estimatedItemHeight;
      return sum;
    }

    function offsetForIndex(index) {
      let sum = 0;
      const upto = Math.min(index - 1, lastMeasuredIndex);
      for (let i = 0; i <= upto; i++) sum += measured[i] || cfg.estimatedItemHeight;
      if (index - 1 > upto) sum += (index - 1 - upto) * cfg.estimatedItemHeight;
      return sum;
    }

    function rangeForScrollTop(scrollTop, viewportH) {
      if (!itemCount) return { start: 0, end: 0 };
      // Binary search the first visible index.
      let lo = 0, hi = itemCount - 1, start = 0;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (offsetForIndex(mid) <= scrollTop) { start = mid; lo = mid + 1; }
        else hi = mid - 1;
      }
      let end = start;
      let acc = offsetForIndex(start);
      while (end < itemCount - 1 && acc < scrollTop + viewportH) {
        end++;
        acc += measured[end] || cfg.estimatedItemHeight;
      }
      return {
        start: Math.max(0, start - cfg.overscan),
        end: Math.min(itemCount - 1, end + cfg.overscan)
      };
    }

    function isNearBottom(scrollEl) {
      return scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight < cfg.bufferPx;
    }

    function render() {
      if (!scrollEl || !spacerEl || !renderFn) return;
      const viewportH = scrollEl.clientHeight || cfg.estimatedItemHeight * 10;
      const scrollTop = scrollEl.scrollTop;
      const { start, end } = rangeForScrollTop(scrollTop, viewportH);
      spacerEl.style.height = `${totalHeight()}px`;
      // Clear items, keeping the spacer. The spacer is the first child and items are
      // appended after it, so remove from the tail until only the spacer remains.
      // (Clearing from the head with `firstChild !== spacerEl` exits immediately and
      // leaks every rendered window, accumulating unbounded .vl-item nodes.)
      while (scrollEl.lastChild && scrollEl.lastChild !== spacerEl) scrollEl.removeChild(scrollEl.lastChild);
      const frag = document.createDocumentFragment();
      for (let i = start; i <= end; i++) {
        const node = renderFn(i, false);
        if (node == null) continue;
        const wrap = document.createElement("div");
        wrap.className = "vl-item";
        wrap.style.position = "absolute";
        wrap.style.top = `${offsetForIndex(i)}px`;
        wrap.style.left = "0";
        wrap.style.right = "0";
        wrap.dataset.vlIndex = String(i);
        if (typeof node === "string") wrap.innerHTML = node;
        else wrap.appendChild(node);
        frag.appendChild(wrap);
      }
      scrollEl.appendChild(frag);
      onRendered?.({ first: start, last: end, itemCount });
    }

    // Re-measure heights of rendered items (call after layout).
    function measureRendered() {
      if (!scrollEl) return;
      const items = scrollEl.querySelectorAll(".vl-item");
      for (const el of items) {
        const index = Number(el.dataset.vlIndex);
        if (Number.isFinite(index)) {
          const h = el.offsetHeight;
          if (h) setMeasuredHeight(index, h);
          el.style.height = `${h}px`;
        }
      }
    }

    function requestRender() {
      cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(() => {
        render();
        measureRendered();
      });
    }

    function setItemCount(n) {
      itemCount = Math.max(0, Number(n) || 0);
      measured = measured.slice(0, itemCount);
      if (lastMeasuredIndex >= itemCount) lastMeasuredIndex = itemCount - 1;
      requestRender();
    }

    // Append `n` new items at the end and keep the view anchored to the bottom if
    // the user was already near the bottom.
    function appendItems(n, forceAnchor) {
      if (!n) return;
      const wasNear = anchorBottom || (scrollEl ? isNearBottom(scrollEl) : false);
      itemCount += n;
      requestRender();
      if (forceAnchor || wasNear) {
        anchorBottom = true;
        if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
      }
    }

    function setRenderFn(fn) {
      renderFn = fn;
      requestRender();
    }

    function reset(opts = {}) {
      itemCount = 0;
      measured = [];
      lastMeasuredIndex = -1;
      anchorBottom = opts.anchorBottom ?? cfg.anchorBottom;
      if (scrollEl) scrollEl.scrollTop = opts.scrollTop ?? 0;
      requestRender();
    }

    function scrollToBottom() {
      anchorBottom = true;
      if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
    }

    function scrollTo(top) {
      anchorBottom = false;
      if (scrollEl) scrollEl.scrollTop = Number(top) || 0;
    }

    function onScroll() {
      if (anchorBottom && isNearBottom(scrollEl)) {
        // keep anchored; no-op here, scroll position is already at bottom
      } else if (anchorBottom) {
        anchorBottom = false;
      }
      requestRender();
    }

    function mount(el) {
      scrollEl = el;
      scrollEl.classList.add("vl-scroll");
      spacerEl = document.createElement("div");
      spacerEl.className = "vl-spacer";
      spacerEl.style.position = "relative";
      scrollEl.appendChild(spacerEl);
      scrollEl.addEventListener("scroll", onScroll, { passive: true });
      requestRender();
      // Anchor to the newest (bottom) item on initial mount, or restore the previous
      // scroll offset on re-mount (e.g. realtime re-render while the user scrolled up).
      // Both need the spacer height finalized after the first render pass, so apply
      // the scroll one frame later.
      requestAnimationFrame(() => {
        if (!scrollEl) return;
        if (anchorBottom) {
          scrollEl.scrollTop = scrollEl.scrollHeight;
        } else if (cfg.initialScrollTop > 0) {
          scrollEl.scrollTop = cfg.initialScrollTop;
        }
      });
    }

    function unmount() {
      if (scrollEl && spacerEl) {
        scrollEl.removeEventListener("scroll", onScroll);
        if (spacerEl.parentNode === scrollEl) scrollEl.removeChild(spacerEl);
      }
      scrollEl = null;
      spacerEl = null;
      cancelAnimationFrame(rafId);
    }

    return Object.freeze({
      appendItems,
      getItemCount: () => itemCount,
      mount,
      scrollTo,
      scrollToBottom,
      reset,
      setItemCount,
      setOnRendered: fn => { onRendered = fn; },
      setRenderFn,
      unmount,
      isNearBottom: () => scrollEl ? isNearBottom(scrollEl) : false
    });
  }

  window.AHAVirtualList = Object.freeze({ createVirtualList });
}());
