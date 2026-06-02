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

  function runMaintenanceTextList(items, formatter, emptyText = "无") {
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
      { action: "archive", label: "归档", disabled: actionDisabled },
      { action: "compact", label: "归档并删除", disabled: actionDisabled, danger: true }
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
      summary: `容量 ${totalFiles} 文件 / ${formatBytes(totalBytes)} · 候选 ${parts.candidates.length} · stale ${parts.staleAgents.length}${message}`,
      rows: [
        ["分组", runMaintenanceTextList(parts.groups, group => `${group.name}: ${group.files || 0} / ${formatBytes(Number(group.bytes || 0))}`)],
        ["候选", runMaintenanceTextList(parts.candidates, item => `${item.path}: ${formatBytes(Number(item.bytes || 0))}`)],
        ["Stale", runMaintenanceTextList(parts.staleAgents, item => {
          const backend = item.backend ? ` ${item.backend}` : "";
          return `${item.task_id || "-"}/${item.agent_id || "-"}${backend}`;
        })],
        ["归档", runMaintenanceTextList(parts.archives, item => `${item.created_at || "-"}: ${item.files || 0} / ${formatBytes(Number(item.bytes || 0))}`)]
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
        title: "创建 retention 归档？",
        message: "将候选日志和提示文件打包为可恢复归档，不删除原文件。",
        confirmLabel: "创建归档",
        details: [
          ["Run", runId],
          ["候选", `${candidateCount} 文件 / ${candidateSize}`],
          ["影响", "只写入归档文件"]
        ]
      };
    }
    if (action === "compact") {
      return {
        title: "归档并删除候选原文件？",
        message: "会先创建可恢复归档，然后删除已经归档的候选原文件。",
        confirmLabel: "归档并删除",
        danger: true,
        details: [
          ["Run", runId],
          ["候选", `${candidateCount} 文件 / ${candidateSize}`],
          ["影响", "删除归档覆盖的原文件"]
        ]
      };
    }
    if (action === "recover") {
      return {
        title: "恢复 stale agent？",
        message: "会把该 agent 标记为 interrupted，让任务状态脱离 stale running。",
        confirmLabel: "标记 interrupted",
        details: [
          ["Run", runId],
          ["Task", String(detail.taskId || "-")],
          ["Agent", String(detail.agentId || "-")]
        ]
      };
    }
    if (action === "restore-archive") {
      return {
        title: "恢复 retention archive？",
        message: "会从所选归档恢复文件；已有文件由后端安全检查处理。",
        confirmLabel: "恢复归档",
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
