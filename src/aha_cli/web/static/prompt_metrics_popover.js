(() => {
  function createPromptMetricsPopoverController(elements = {}, deps = {}) {
    let openKey = "";

    function activePopover() {
      return elements.panelEl?.querySelector(".turn-metrics[open] .turn-metrics-popover") || null;
    }

    function activeTrigger() {
      return elements.panelEl?.querySelector(".turn-metrics[open] .turn-metrics-trigger") || null;
    }

    function closeBreakdowns(root = elements.panelEl) {
      root?.querySelectorAll("[data-metrics-breakdown][open]").forEach(details => {
        if (details instanceof HTMLDetailsElement) details.open = false;
      });
    }

    function close() {
      openKey = "";
      closeBreakdowns();
      elements.panelEl?.querySelectorAll(".turn-metrics[open]").forEach(details => {
        if (details instanceof HTMLDetailsElement) details.open = false;
      });
    }

    function targetInside(target) {
      const element = target instanceof Element ? target : null;
      return Boolean(element?.closest?.(".turn-metrics"));
    }

    function closeForOutsideEvent(event) {
      if (!openKey) return;
      if (targetInside(event.target)) return;
      close();
    }

    function captureState() {
      const popover = activePopover();
      if (!popover) return null;
      const trigger = activeTrigger();
      const breakdownOpen = {};
      popover.querySelectorAll("[data-metrics-breakdown]").forEach(details => {
        if (details instanceof HTMLDetailsElement) breakdownOpen[details.dataset.metricsBreakdown || ""] = details.open;
      });
      return {
        breakdownOpen,
        popoverScrollTop: popover.scrollTop,
        triggerTop: trigger?.getBoundingClientRect?.().top ?? null
      };
    }

    function position() {
      const popover = activePopover();
      const trigger = activeTrigger();
      if (!popover || !trigger) return;
      const margin = 16;
      const gap = 8;
      const windowRef = deps.windowRef || window;
      const composerTop = elements.sendFormEl?.getBoundingClientRect?.().top ?? windowRef.innerHeight;
      const lowerBoundary = Math.max(margin + 120, Math.min(windowRef.innerHeight - margin, composerTop - gap));
      const maxHeight = Math.max(120, Math.min(windowRef.innerHeight * 0.62, 520, lowerBoundary - margin));
      popover.style.maxHeight = `${maxHeight}px`;
      popover.style.left = "";
      popover.style.top = "";
      const triggerRect = trigger.getBoundingClientRect();
      const popoverRect = popover.getBoundingClientRect();
      const width = popoverRect.width || Math.min(480, windowRef.innerWidth - margin * 2);
      const height = popover.offsetHeight || popoverRect.height || Math.min(windowRef.innerHeight * 0.62, 520);
      const maxLeft = Math.max(margin, windowRef.innerWidth - width - margin);
      const left = Math.min(Math.max(margin, triggerRect.right - width), maxLeft);
      let top = triggerRect.top - height - gap;
      if (top < margin) top = triggerRect.bottom + gap;
      if (top + height > lowerBoundary) top = Math.max(margin, lowerBoundary - height);
      popover.style.left = `${left}px`;
      popover.style.top = `${top}px`;
    }

    function restoreState(state) {
      if (!state) return;
      const restore = () => {
        const popover = activePopover();
        const trigger = activeTrigger();
        if (trigger && state.triggerTop != null) {
          elements.panelEl.scrollTop += trigger.getBoundingClientRect().top - state.triggerTop;
        }
        Object.entries(state.breakdownOpen || {}).forEach(([key, open]) => {
          const breakdown = Array.from(popover?.querySelectorAll("[data-metrics-breakdown]") || [])
            .find(item => item instanceof HTMLDetailsElement && item.dataset.metricsBreakdown === key);
          if (breakdown instanceof HTMLDetailsElement) breakdown.open = Boolean(open);
        });
        if (popover && state.popoverScrollTop != null) popover.scrollTop = state.popoverScrollTop;
        position();
      };
      restore();
      (deps.windowRef || window).requestAnimationFrame(restore);
    }

    return Object.freeze({
      captureState,
      close,
      closeBreakdowns,
      closeForOutsideEvent,
      openKey: () => openKey,
      position,
      restoreState,
      setOpenKey: value => { openKey = String(value || ""); },
      targetInside
    });
  }

  window.AHAPromptMetricsPopover = Object.freeze({ createPromptMetricsPopoverController });
})();
