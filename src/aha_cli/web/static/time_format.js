(() => {
  function parseTimestamp(value) {
    if (!value) return null;
    const millis = Date.parse(value);
    return Number.isNaN(millis) ? null : millis;
  }

  function formatLocalTimestamp(value, fallback = "-") {
    const millis = parseTimestamp(value);
    if (millis === null) return fallback;
    return new Date(millis).toLocaleString("zh-CN", { hour12: false });
  }

  function localizeTimestampFields(value, key = "") {
    if (Array.isArray(value)) return value.map(item => localizeTimestampFields(item));
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([itemKey, itemValue]) => [itemKey, localizeTimestampFields(itemValue, itemKey)]));
    }
    if ((key === "ts" || key.endsWith("_at")) && typeof value === "string") {
      return formatLocalTimestamp(value, value);
    }
    return value;
  }

  function localizeTimestampText(value) {
    return String(value ?? "").replace(
      /\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b/g,
      match => formatLocalTimestamp(match, match)
    );
  }

  function formatDuration(millis) {
    const totalSeconds = Math.max(0, Math.floor((millis || 0) / 1000));
    const seconds = String(totalSeconds % 60).padStart(2, "0");
    const minutes = String(Math.floor(totalSeconds / 60) % 60).padStart(2, "0");
    const hours = Math.floor(totalSeconds / 3600);
    return hours > 0 ? `${hours}:${minutes}:${seconds}` : `${minutes}:${seconds}`;
  }

  function formatClock(millis) {
    if (!millis) return "-";
    return new Date(millis).toLocaleTimeString("zh-CN", { hour12: false });
  }

  window.AHATimeFormat = Object.freeze({
    parseTimestamp,
    formatLocalTimestamp,
    localizeTimestampFields,
    localizeTimestampText,
    formatDuration,
    formatClock
  });
})();
