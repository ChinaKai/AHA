(() => {
  const TERMINAL_ROW_HEIGHT_PX = 17.2;
  const TERMINAL_CHROME_HEIGHT_PX = 18;
  const TERMINAL_KEYS = Object.freeze([
    Object.freeze({ name: "enter", label: "⏎", data: "\r" }),
    Object.freeze({ name: "esc", label: "Esc", data: "\u001b" }),
    Object.freeze({ name: "tab", label: "Tab", data: "\t" }),
    Object.freeze({ name: "ctrl-c", label: "^C", data: "\u0003" }),
    Object.freeze({ name: "up", label: "↑", data: "\u001b[A" }),
    Object.freeze({ name: "down", label: "↓", data: "\u001b[B" }),
    Object.freeze({ name: "left", label: "←", data: "\u001b[D" }),
    Object.freeze({ name: "right", label: "→", data: "\u001b[C" }),
    Object.freeze({ name: "home", label: "Home", data: "\u001b[H" }),
    Object.freeze({ name: "end", label: "End", data: "\u001b[F" })
  ]);

  function terminalKeyBytes(name) {
    return TERMINAL_KEYS.find(item => item.name === String(name || ""))?.data || "";
  }

  function terminalKeys() {
    return TERMINAL_KEYS;
  }

  function terminalKeyboardInset(windowRef, navigatorRef) {
    const virtualKeyboardHeight = Number(navigatorRef?.virtualKeyboard?.boundingRect?.height || 0);
    if (virtualKeyboardHeight > 0) return virtualKeyboardHeight;
    const viewport = windowRef?.visualViewport;
    if (!viewport) return 0;
    return Math.max(0, Number(windowRef.innerHeight || 0) - Number(viewport.height || 0) - Number(viewport.offsetTop || 0));
  }

  function createTerminalViewportMonitor(options = {}) {
    const windowRef = options.windowRef || window;
    const documentRef = options.documentRef || windowRef.document || (typeof document !== "undefined" ? document : null);
    const navigatorRef = options.navigatorRef || windowRef.navigator || null;
    const viewport = windowRef.visualViewport || null;
    const virtualKeyboard = navigatorRef?.virtualKeyboard || null;
    let active = false;
    let timers = [];
    const update = () => {
      if (!options.isActive?.()) return;
      const inset = terminalKeyboardInset(windowRef, navigatorRef);
      const nextActive = inset > 0;
      options.onChange?.({
        active: nextActive,
        becameActive: nextActive && !active,
        inset,
        viewportHeight: Number(viewport?.height || windowRef.innerHeight || 0)
      });
      active = nextActive;
    };
    const schedule = () => {
      timers.forEach(timer => clearTimeout(timer));
      timers = [setTimeout(update, 0), setTimeout(update, 120), setTimeout(update, 280)];
    };
    windowRef.addEventListener?.("resize", schedule, { passive: true });
    viewport?.addEventListener?.("resize", schedule, { passive: true });
    viewport?.addEventListener?.("scroll", schedule, { passive: true });
    virtualKeyboard?.addEventListener?.("geometrychange", schedule);
    documentRef?.addEventListener?.("focusin", schedule, true);
    documentRef?.addEventListener?.("focusout", schedule, true);
    schedule();
    return Object.freeze({
      dispose() {
        timers.forEach(timer => clearTimeout(timer));
        timers = [];
        windowRef.removeEventListener?.("resize", schedule);
        viewport?.removeEventListener?.("resize", schedule);
        viewport?.removeEventListener?.("scroll", schedule);
        virtualKeyboard?.removeEventListener?.("geometrychange", schedule);
        documentRef?.removeEventListener?.("focusin", schedule, true);
        documentRef?.removeEventListener?.("focusout", schedule, true);
        if (active) options.onChange?.({ active: false, becameActive: false, inset: 0, viewportHeight: 0 });
        active = false;
      }
    });
  }

  function terminalBottomGapForElement(el) {
    const styles = el && typeof getComputedStyle === "function" ? getComputedStyle(el) : null;
    const value = Number.parseFloat(styles?.getPropertyValue("--terminal-bottom-gap") || "0");
    return Number.isFinite(value) ? Math.max(0, value) : 0;
  }

  function terminalMinRowsForElement(el) {
    const styles = el && typeof getComputedStyle === "function" ? getComputedStyle(el) : null;
    const value = Number.parseInt(styles?.getPropertyValue("--terminal-min-rows") || "12", 10);
    return Number.isFinite(value) ? Math.max(2, Math.min(80, value)) : 12;
  }

  function terminalSizeForElement(el) {
    const rect = el?.getBoundingClientRect?.() || { width: 900, height: 420 };
    const bottomGap = terminalBottomGapForElement(el);
    const minRows = terminalMinRowsForElement(el);
    const cols = Math.max(40, Math.min(240, Math.floor(Math.max(320, rect.width - 18) / 8.4)));
    const rows = Math.max(minRows, Math.min(80, Math.floor(Math.max(0, rect.height - TERMINAL_CHROME_HEIGHT_PX - bottomGap) / TERMINAL_ROW_HEIGHT_PX)));
    return { cols, rows };
  }

  // On mobile, a fixed 320px terminal can leave many unused logical rows below a short
  // prompt. Fit the primary buffer to the cursor plus one spare row, growing until the
  // original available height. Alternate-screen apps keep the full available height.
  function fitTerminalToContent(term, el, options = {}) {
    const style = el?.style;
    if (!style) return false;
    const previous = style.getPropertyValue?.("--terminal-adaptive-height") || "";
    if (!options.active) {
      style.removeProperty?.("--terminal-adaptive-height");
      return Boolean(previous);
    }
    const buffer = term?.buffer?.active;
    if (!buffer) return false;
    const maxHeight = Math.max(0, Number(options.maxHeight || 0));
    const alternate = term?.buffer?.alternate;
    let targetHeight = maxHeight;
    if (!alternate || buffer !== alternate) {
      const cursorRow = Math.max(0, Number(buffer.cursorY || 0));
      const contentRows = Math.max(2, Math.min(80, Math.floor(cursorRow) + 2));
      targetHeight = Math.ceil(
        TERMINAL_CHROME_HEIGHT_PX
        + terminalBottomGapForElement(el)
        + contentRows * TERMINAL_ROW_HEIGHT_PX
      );
      if (maxHeight > 0) targetHeight = Math.min(maxHeight, targetHeight);
    }
    if (targetHeight <= 0) return false;
    const next = `${Math.round(targetHeight)}px`;
    if (next === previous) return false;
    style.setProperty?.("--terminal-adaptive-height", next);
    return true;
  }

  function terminalOptions(options = {}) {
    return {
      allowProposedApi: false,
      convertEol: false,
      cursorBlink: true,
      cursorStyle: "block",
      cols: Number(options.cols || 100),
      rows: Number(options.rows || 28),
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
      fontSize: 13,
      scrollback: Number(options.scrollback || 10000),
      theme: {
        background: "#101828",
        foreground: "#f8fafc",
        cursor: "#f8fafc",
        selectionBackground: "#344054"
      }
    };
  }

  window.AHATerminalUi = Object.freeze({
    createTerminalViewportMonitor,
    fitTerminalToContent,
    terminalBottomGapForElement,
    terminalKeyboardInset,
    terminalKeyBytes,
    terminalKeys,
    terminalMinRowsForElement,
    terminalOptions,
    terminalSizeForElement
  });
})();
