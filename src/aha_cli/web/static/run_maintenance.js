(function () {
  function safeArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function formatNumber(value) {
    const number = Number(value || 0);
    if (!Number.isFinite(number)) return "0";
    return new Intl.NumberFormat("en-US").format(number);
  }

  function defaultFormatBytes(value) {
    const number = Number(value || 0);
    if (!Number.isFinite(number) || number <= 0) return "0 B";
    if (number < 1024) return `${formatNumber(number)} B`;
    if (number < 1024 * 1024) return `${(number / 1024).toFixed(1)} KB`;
    return `${(number / 1024 / 1024).toFixed(2)} MB`;
  }

  function formatterFrom(options = {}) {
    return typeof options.formatBytes === "function" ? options.formatBytes : defaultFormatBytes;
  }

  function t(key, fallback = "") {
    return window.AHAI18n?.t?.(key, fallback) || fallback;
  }

  function formatText(key, values = {}, fallback = "") {
    return window.AHAI18n?.format?.(key, values, fallback) || fallback;
  }

  function runMaintenanceTextList(items, formatter, emptyText = "none") {
    if (!Array.isArray(items) || !items.length) return emptyText;
    return items.slice(0, 3).map(formatter).filter(Boolean).join(" · ") || emptyText;
  }

  function retentionParts(payload = {}) {
    const retention = payload.retention || {};
    const recovery = payload.recovery || {};
    const archiveList = payload.retention_archives || {};
    return {
      retention,
      recovery,
      archiveList,
      total: retention.total || {},
      groups: safeArray(retention.groups),
      candidates: safeArray(retention.candidates),
      staleAgents: safeArray(recovery.candidates),
      archives: safeArray(archiveList.archives)
    };
  }

  function candidateBytes(candidates) {
    return safeArray(candidates).reduce((total, item) => total + Number(item?.bytes || 0), 0);
  }

  function runMaintenanceButtons(parts, actionInFlight = false) {
    const actionDisabled = Boolean(actionInFlight || !parts.candidates.length);
    const buttons = [
      { action: "archive", label: t("maintenance.archive", "Archive"), disabled: actionDisabled },
      { action: "compact", label: t("maintenance.compact", "Archive & delete"), disabled: actionDisabled, danger: true }
    ];
    for (const item of parts.staleAgents.slice(0, 3)) {
      const taskId = String(item.task_id || "");
      const agentId = String(item.agent_id || "");
      buttons.push({
        action: "recover",
        label: `${taskId || "-"}/${agentId || "-"}`,
        taskId,
        agentId,
        disabled: Boolean(actionInFlight)
      });
    }
    for (const item of parts.archives.slice(0, 3)) {
      const archive = String(item.path || "");
      buttons.push({
        action: "restore-archive",
        label: `${item.files || 0} files`,
        archive,
        title: archive,
        disabled: Boolean(actionInFlight)
      });
    }
    return buttons;
  }

  function runMaintenanceView(payload = {}, options = {}) {
    const formatBytes = formatterFrom(options);
    const parts = retentionParts(payload);
    const totalFiles = Number(parts.total.files || 0);
    const totalBytes = Number(parts.total.bytes || 0);
    const message = options.message ? ` · ${options.message}` : "";
    return {
      summary: formatText("maintenance.capacity_summary", {
        files: totalFiles,
        bytes: formatBytes(totalBytes),
        candidates: parts.candidates.length,
        stale: parts.staleAgents.length,
        message
      }, `Size ${totalFiles} files / ${formatBytes(totalBytes)} · candidates ${parts.candidates.length} · stale ${parts.staleAgents.length}${message}`),
      rows: [
        [t("maintenance.groups", "Groups"), runMaintenanceTextList(parts.groups, group => `${group.name}: ${group.files || 0} / ${formatBytes(Number(group.bytes || 0))}`, t("maintenance.none", "none"))],
        [t("maintenance.candidates", "Candidates"), runMaintenanceTextList(parts.candidates, item => `${item.path}: ${formatBytes(Number(item.bytes || 0))}`, t("maintenance.none", "none"))],
        [t("maintenance.stale", "Stale"), runMaintenanceTextList(parts.staleAgents, item => {
          const backend = item.backend ? ` ${item.backend}` : "";
          return `${item.task_id || "-"}/${item.agent_id || "-"}${backend}`;
        }, t("maintenance.none", "none"))],
        [t("maintenance.archives", "Archives"), runMaintenanceTextList(parts.archives, item => `${item.created_at || "-"}: ${item.files || 0} / ${formatBytes(Number(item.bytes || 0))}`, t("maintenance.none", "none"))]
      ].map(([label, value]) => ({ label, value })),
      buttons: runMaintenanceButtons(parts, options.actionInFlight)
    };
  }

  function runMaintenanceActionConfirm(action, detail = {}, payload = {}, options = {}) {
    const formatBytes = formatterFrom(options);
    const parts = retentionParts(payload);
    const runId = String(options.runId || "").trim() || "-";
    const candidateCount = parts.candidates.length;
    const candidateSize = formatBytes(candidateBytes(parts.candidates));
    if (action === "archive") {
      return {
        title: t("maintenance.archive_title", "Create retention archive?"),
        message: t("maintenance.archive_message", "Package candidate logs and prompt files into a recoverable archive without deleting originals."),
        confirmLabel: t("maintenance.archive_confirm", "Create archive"),
        details: [
          ["Run", runId],
          [t("maintenance.candidates", "Candidates"), formatText("maintenance.files_size", { count: candidateCount, size: candidateSize }, `${candidateCount} files / ${candidateSize}`)],
          [t("maintenance.impact", "Impact"), t("maintenance.archive_impact", "Only writes the archive file")]
        ]
      };
    }
    if (action === "compact") {
      return {
        title: t("maintenance.compact_title", "Archive and delete candidate originals?"),
        message: t("maintenance.compact_message", "Creates a recoverable archive first, then deletes archived candidate originals."),
        confirmLabel: t("maintenance.compact_confirm", "Archive & delete"),
        danger: true,
        details: [
          ["Run", runId],
          [t("maintenance.candidates", "Candidates"), formatText("maintenance.files_size", { count: candidateCount, size: candidateSize }, `${candidateCount} files / ${candidateSize}`)],
          [t("maintenance.impact", "Impact"), t("maintenance.compact_impact", "Deletes originals covered by the archive")]
        ]
      };
    }
    if (action === "recover") {
      return {
        title: t("maintenance.recover_title", "Recover stale agent?"),
        message: t("maintenance.recover_message", "Marks this agent as interrupted so the task leaves stale running state."),
        confirmLabel: t("maintenance.recover_confirm", "Mark interrupted"),
        details: [
          ["Run", runId],
          ["Task", String(detail.taskId || "-")],
          ["Agent", String(detail.agentId || "-")]
        ]
      };
    }
    if (action === "restore-archive") {
      return {
        title: t("maintenance.restore_archive_title", "Restore retention archive?"),
        message: t("maintenance.restore_archive_message", "Restores files from the selected archive. Existing files are handled by backend safety checks."),
        confirmLabel: t("maintenance.restore_archive_confirm", "Restore archive"),
        details: [
          ["Run", runId],
          ["Archive", String(detail.archive || "-")]
        ]
      };
    }
    return null;
  }

  window.AHARunMaintenance = Object.freeze({
    runMaintenanceTextList,
    runMaintenanceView,
    runMaintenanceActionConfirm
  });
}());
