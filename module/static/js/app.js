/* tg-dl web ui — polling, rendering, filtering, per-task & bulk controls, config editor. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const TAB_REFRESH = { downloading: 1000, downloaded: 4000, failed: 3000 };
  const STATUS_REFRESH = 1000;
  const CONFIRM_TIMEOUT = 3500;

  const ICON_PAUSE =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 5v14M15 5v14"/></svg>';
  const ICON_PLAY =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M9 5.5v13l10-6.5z"/></svg>';
  const ICON_TRASH =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16"/><path d="M9 7V5h6v2"/><path d="M6 7l1 13h10l1-13"/></svg>';
  const ICON_RETRY =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v5h-5"/></svg>';
  const ICON_COPY =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg>';
  const ICON_CHECK =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 13 4 4L19 7"/></svg>';
  const ICON_CLOSE =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>';
  const ICON_EYE =
    '<svg class="icon-eye" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
  const ICON_EYE_OFF =
    '<svg class="icon-eye-off" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round"><path d="M9.9 4.2A10.6 10.6 0 0 1 12 4c6.4 0 10 8 10 8a17.6 17.6 0 0 1-3.2 4.3"/><path d="M6.6 6.6A17.2 17.2 0 0 0 2 12s3.6 8 10 8a9.8 9.8 0 0 0 5.4-1.6"/><path d="M14.1 14.1a3 3 0 0 1-4.2-4.2"/><path d="m2 2 20 20"/></svg>';

  const state = {
    tab: "downloading",
    action: $("#pause-btn") ? $("#pause-btn").dataset.value : "pause",
    configTab: "form",
    query: "",
    uploadFilter: "all",
    pollingPaused: false,
    downloading: [],
    downloaded: [],
    failed: [],
    // The list endpoints filter and paginate on the server (the history lives
    // in SQLite), so each tab keeps only the current page plus this pager state.
    pageSize: 25,
    pager: {
      downloading: { page: 1, pages: 1, total: 0, allTotal: 0 },
      downloaded: { page: 1, pages: 1, total: 0, allTotal: 0 },
      failed: { page: 1, pages: 1, total: 0, allTotal: 0 },
    },
    // Tab totals from /get_download_status, independent of the visible page.
    counts: { downloading: 0, completed: 0, failed: 0 },
  };

  const el = {
    speed: $("#speed"),
    metaSpeed: $("#meta-speed"),
    statActive: $("#stat-active"),
    statDone: $("#stat-done"),
    statFailed: $("#stat-failed"),
    statState: $("#stat-state"),
    badgeDownloading: $("#badge-downloading"),
    badgeDone: $("#badge-done"),
    badgeFailed: $("#badge-failed"),
    updated: $("#updated"),
    liveDot: $("#live-dot"),
    pollToggle: $("#poll-toggle"),
    updatePill: $("#update-pill"),
    updateVersion: $("#update-version"),
    taskSearch: $("#task-search"),
    taskSearchClear: $("#task-search-clear"),
    uploadFilters: $("#upload-filters"),
    completedBulkActions: $("#completed-bulk-actions"),
    clearCompletedBtn: $("#clear-completed-btn"),
    failedBulkActions: $("#failed-bulk-actions"),
    retryAllBtn: $("#retry-all-btn"),
    clearFailedBtn: $("#clear-failed-btn"),
    empty: $("#empty"),
    emptyText: $("#empty-text"),
    emptyAction: $("#empty-action"),
    pausedBanner: $("#paused-banner"),
    pager: $("#pager"),
    pagerInfo: $("#pager-info"),
    pageSize: $("#page-size"),
    pagePrev: $("#page-prev"),
    pageNext: $("#page-next"),
    pauseBtn: $("#pause-btn"),
    themeBtn: $("#theme-toggle"),
    configBtn: $("#config-btn"),
    logoutBtn: $("#logout-btn"),
    toasts: $("#toasts"),
    dialog: $("#config-dialog"),
    configPath: $("#config-path"),
    configText: $("#config-text"),
    configError: $("#config-error"),
    configSave: $("#config-save"),
    configCancel: $("#config-cancel"),
    configClose: $("#config-close"),
    configForm: $("#config-form"),
    configLoading: $("#config-loading"),
    configToolbar: $("#config-toolbar"),
    configSearch: $("#config-search"),
    configGroupNav: $("#config-group-nav"),
    configEmpty: $("#config-empty"),
    configFormSyncNote: $("#config-form-sync-note"),
    configYamlSyncNote: $("#config-yaml-sync-note"),
    formPane: $("#config-form-pane"),
    yamlPane: $("#config-yaml-pane"),
    tables: {
      downloading: $("#table-downloading"),
      downloaded: $("#table-downloaded"),
      failed: $("#table-failed"),
    },
  };

  const ESCAPE_MAP = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  };

  /* ------------------------------------------------------------------ utils */

  function esc(value) {
    return String(value === null || value === undefined ? "" : value).replace(
      /[&<>"']/g,
      (c) => ESCAPE_MAP[c]
    );
  }

  /* Every DOM write goes through a helper: a stale cached script running next to
     newer markup must degrade instead of throwing and killing the whole page. */
  function setText(node, text) {
    if (node && node.textContent !== text) node.textContent = text;
    return node;
  }

  function setBusy(button, busy) {
    if (!button) return;
    if (busy) button.setAttribute("aria-disabled", "true");
    else button.removeAttribute("aria-disabled");
  }

  function isBusy(button) {
    return Boolean(
      button && (button.disabled || button.getAttribute("aria-disabled") === "true")
    );
  }

  function showFatalBanner(message) {
    if (!document.body || document.getElementById("fatal-banner")) return;
    const banner = document.createElement("div");
    banner.id = "fatal-banner";
    banner.className = "fatal-banner";
    banner.setAttribute("role", "alert");
    const label = document.createElement("span");
    label.textContent = message;
    const button = document.createElement("button");
    button.className = "btn btn-ghost";
    button.type = "button";
    button.textContent = "重新加载";
    button.addEventListener("click", () => window.location.reload());
    banner.appendChild(label);
    banner.appendChild(button);
    document.body.prepend(banner);
  }

  async function checkVersion() {
    const expected = document.documentElement.dataset.version || "";
    if (!expected) return;
    try {
      const serverVersion = String(await api("/get_app_version")).trim();
      if (serverVersion && serverVersion !== expected) {
        showFatalBanner(
          "页面资源是 v" +
            expected +
            "，后端是 v" +
            serverVersion +
            "：请按 Ctrl+F5（Mac 用 Cmd+Shift+R）强制刷新。"
        );
      }
    } catch (e) {
      /* older backends do not expose the version endpoint */
    }
  }

  /** Show a link to the release page when the server knows a newer build. */
  async function checkUpdate() {
    try {
      const info = await api("/get_latest_version");
      if (!info || !info.newer || !info.latest) return;
      if (!el.updatePill) return;
      el.updatePill.hidden = false;
      el.updatePill.href = info.url || "https://github.com/QCEnjoyLL/tg-dl/releases";
      el.updatePill.title =
        "新版本 v" +
        info.latest +
        " 可用（当前 v" +
        (info.current || "") +
        "），点击查看更新日志";
      setText(el.updateVersion, "v" + info.latest);
    } catch (e) {
      /* offline or rate limited: no hint, no noise */
    }
  }

  function nameHtml(name) {
    const text = String(name === null || name === undefined ? "" : name);
    const dot = text.lastIndexOf(".");
    const hasExt = dot > 0 && text.length - dot <= 8;
    if (!hasExt) {
      return esc(text) || '<span class="ext">（未命名）</span>';
    }
    return (
      esc(text.slice(0, dot)) +
      '<span class="ext">' +
      esc(text.slice(dot)) +
      "</span>"
    );
  }

  function chatLabel(chat) {
    return String(chat === null || chat === undefined ? "" : chat);
  }

  function pctValue(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return 0;
    return Math.min(100, Math.max(0, n));
  }

  function pctText(value) {
    return Number.isInteger(value) ? value + "%" : value.toFixed(1) + "%";
  }

  function clockText(date) {
    return (date || new Date()).toLocaleTimeString("zh-CN", { hour12: false });
  }

  function timeText(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return (
      pad(d.getMonth() + 1) +
      "-" +
      pad(d.getDate()) +
      " " +
      pad(d.getHours()) +
      ":" +
      pad(d.getMinutes())
    );
  }

  function rowKey(row) {
    return row.chat + ":" + row.id;
  }

  function removeToast(node) {
    if (!node || node.dataset.closing === "1") return;
    node.dataset.closing = "1";
    node.classList.add("is-leaving");
    setTimeout(() => node.remove(), 200);
  }

  function toast(message, tone) {
    if (!el.toasts) return;
    const node = document.createElement("div");
    node.className = "toast";
    node.dataset.tone = tone || "info";
    if (tone === "error") node.setAttribute("role", "alert");

    const msgSpan = document.createElement("span");
    msgSpan.className = "toast-msg";
    msgSpan.textContent = message;
    node.appendChild(msgSpan);

    const closeBtn = document.createElement("button");
    closeBtn.className = "toast-close";
    closeBtn.type = "button";
    closeBtn.setAttribute("aria-label", "关闭通知");
    closeBtn.innerHTML = ICON_CLOSE;
    closeBtn.addEventListener("click", () => removeToast(node));
    node.appendChild(closeBtn);

    el.toasts.appendChild(node);
    requestAnimationFrame(() => node.classList.add("is-visible"));

    // Error notifications carry actionable diagnostics and stay until dismissed (WCAG 2.2.2).
    if (tone !== "error") {
      setTimeout(() => removeToast(node), 3200);
    }
  }

  async function api(url, options) {
    const res = await fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      ...(options || {}),
    });
    if (res.redirected && res.url.indexOf("/login") !== -1) {
      window.location.href = "/login";
      throw new Error("需要登录");
    }
    if (!res.ok) throw new Error("HTTP " + res.status);
    const type = res.headers.get("content-type") || "";
    return type.indexOf("json") !== -1 ? res.json() : res.text();
  }

  /**
   * Reconcile a table body with a list of rows, keyed by `data-key`.
   * Only the rows that changed are touched, so progress bars keep animating
   * and the pointer does not jump while hovering.
   */
  function syncRows(tbody, rows, keyOf, build, update) {
    const existing = new Map();
    for (const node of Array.from(tbody.children)) {
      existing.set(node.dataset.key, node);
    }

    rows.forEach((row, index) => {
      const key = keyOf(row);
      let node = existing.get(key);
      if (!node) {
        node = build(row);
        node.dataset.key = key;
      }
      update(node, row);

      const current = tbody.children[index];
      if (current !== node) tbody.insertBefore(node, current || null);
    });

    while (tbody.children.length > rows.length) {
      tbody.lastElementChild.remove();
    }
  }

  /* ------------------------------------------------------------- rendering */

  function idCell(row) {
    return (
      '<td class="cell-id"><span>' +
      esc(row.id) +
      '</span><span class="sub" title="' +
      esc(row.chat) +
      '">' +
      esc(chatLabel(row.chat)) +
      "</span></td>"
    );
  }

  function actionButton(action, label, icon, tone) {
    return (
      '<button class="btn-mini" type="button" data-action="' +
      action +
      '"' +
      (tone ? ' data-tone="' + tone + '"' : "") +
      ">" +
      icon +
      "<span>" +
      label +
      "</span></button>"
    );
  }

  /**
   * Below 900px the action buttons lose their label (the column is too narrow
   * for "确认删除"), so the accessible name doubles as the tooltip.
   */
  function setActionLabel(button, label) {
    if (!button) return;
    button.setAttribute("aria-label", label);
    button.setAttribute("title", label);
  }

  function buildDownloadingRow(row) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      idCell(row) +
      '<td class="cell-name">' +
      '<div class="name-line">' +
      '<span class="row-chip state-chip is-paused" hidden>已暂停</span>' +
      '<span class="file-title"></span>' +
      "</div>" +
      '<div class="progress-inline">' +
      '<div class="bar" aria-hidden="true"><i style="width:0%"></i></div>' +
      '<span class="pct">0%</span>' +
      "</div>" +
      '<span class="speed-inline">↓ —</span>' +
      "</td>" +
      '<td class="cell-size"></td>' +
      '<td class="cell-progress"><div class="progress">' +
      '<div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" aria-label="下载进度"><i style="width:0%"></i></div>' +
      '<span class="pct">0%</span></div></td>' +
      '<td class="cell-speed"></td>' +
      '<td class="col-actions"><div class="row-actions">' +
      actionButton("pause", "暂停", ICON_PAUSE) +
      actionButton("delete", "删除", ICON_TRASH, "danger") +
      "</div></td>";
    return tr;
  }

  function updateDownloadingRow(tr, row) {
    const name = String(row.filename || "");
    if (tr.dataset.filename !== name) {
      tr.dataset.filename = name;
      const nameCell = tr.querySelector(".cell-name");
      if (nameCell) nameCell.title = name;
      const titleSpan = tr.querySelector(".file-title");
      if (titleSpan) titleSpan.innerHTML = nameHtml(name);
    }

    const sizeCell = tr.querySelector(".cell-size");
    setText(sizeCell, row.total_size === undefined ? "" : String(row.total_size));

    const pct = pctValue(row.download_progress);
    const pctStr = pctText(pct);
    const desktopBar = tr.querySelector(".cell-progress .bar");
    if (desktopBar) desktopBar.setAttribute("aria-valuenow", String(pct));
    tr.querySelectorAll(".bar > i").forEach((fill) => {
      if (fill.style.width !== pct + "%") fill.style.width = pct + "%";
    });
    tr.querySelectorAll(".pct").forEach((label) => {
      setText(label, pctStr);
    });

    const globalPaused = state.action === "continue";
    const speed = tr.querySelector(".cell-speed");
    const frozen = globalPaused || Boolean(row.paused);
    const value = frozen
      ? "—"
      : row.download_speed === undefined
      ? ""
      : String(row.download_speed);
    setText(speed, value);

    const inlineSpeed = tr.querySelector(".speed-inline");
    if (inlineSpeed) {
      const sizePart = row.total_size ? " · " + row.total_size : "";
      const inlineText = "↓ " + (value || "—") + sizePart;
      setText(inlineSpeed, inlineText);
      inlineSpeed.classList.toggle("is-muted", !value || value === "—");
    }

    const chip = tr.querySelector(".row-chip");
    if (chip) {
      chip.hidden = !(globalPaused || row.paused);
      setText(chip, row.paused ? "单条暂停" : "已暂停");
    }
    tr.classList.toggle("is-paused", globalPaused || Boolean(row.paused));

    const pauseBtn = tr.querySelector('[data-action="pause"], [data-action="resume"]');
    if (pauseBtn) {
      const paused = Boolean(row.paused);
      const targetAction = paused ? "resume" : "pause";
      if (pauseBtn.dataset.action !== targetAction) {
        pauseBtn.dataset.action = targetAction;
        pauseBtn.dataset.tone = paused ? "warn" : "";
        pauseBtn.innerHTML =
          (paused ? ICON_PLAY : ICON_PAUSE) + "<span>" + (paused ? "继续" : "暂停") + "</span>";
      }
      setActionLabel(pauseBtn, (paused ? "继续下载 " : "暂停下载 ") + name);
    }

    const deleteBtn = tr.querySelector('[data-action="delete"]');
    if (deleteBtn && deleteBtn.dataset.confirm !== "1") {
      setActionLabel(deleteBtn, "删除任务 " + name);
    }
  }

  function uploadStateKey(row) {
    if (row.upload_state === "exists") return "exists";
    if (row.upload_state === "uploaded" || (!row.upload_state && row.upload_success)) {
      return "uploaded";
    }
    return "local";
  }

  function uploadChipHtml(key) {
    if (key === "exists") return '<span class="state-chip is-remote">云端已有</span>';
    if (key === "uploaded") return '<span class="state-chip is-uploaded">已上传</span>';
    return '<span class="state-chip">仅本地</span>';
  }

  function buildDownloadedRow(row) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      idCell(row) +
      '<td class="cell-name">' +
      '<div class="name-line">' +
      '<span class="chip-slot"></span>' +
      '<span class="file-title"></span>' +
      "</div>" +
      '<span class="meta-inline"></span>' +
      '<div class="path-inline trunc-wrap">' +
      '<span class="trunc-text"></span>' +
      '<button class="expand-btn" type="button" data-expand="1" aria-expanded="false" hidden>展开</button>' +
      "</div>" +
      "</td>" +
      '<td class="cell-size"></td>' +
      '<td class="cell-time"></td>' +
      '<td class="cell-path">' +
      '<div class="trunc-wrap">' +
      '<span class="trunc-text"></span>' +
      '<button class="expand-btn" type="button" data-expand="1" aria-expanded="false" hidden>展开</button>' +
      "</div>" +
      "</td>" +
      '<td class="col-actions"><div class="row-actions">' +
      actionButton("copy", "复制", ICON_COPY) +
      actionButton("delete", "删除", ICON_TRASH, "danger") +
      "</div></td>";
    return tr;
  }

  function syncExpandButtons(tr, show) {
    const expanded = tr.classList.contains("is-expanded");
    tr.querySelectorAll(".expand-btn").forEach((btn) => {
      btn.hidden = !show;
      btn.setAttribute("aria-expanded", expanded ? "true" : "false");
      setText(btn, expanded ? "收起" : "展开");
    });
  }

  function updateDownloadedRow(tr, row) {
    const name = String(row.filename || "");
    if (tr.dataset.filename !== name) {
      tr.dataset.filename = name;
      const nameCell = tr.querySelector(".cell-name");
      if (nameCell) nameCell.title = name;
      const titleSpan = tr.querySelector(".file-title");
      if (titleSpan) titleSpan.innerHTML = nameHtml(name);
    }

    const uKey = uploadStateKey(row);
    if (tr.dataset.uploadKey !== uKey) {
      tr.dataset.uploadKey = uKey;
      const slot = tr.querySelector(".chip-slot");
      if (slot) slot.innerHTML = uploadChipHtml(uKey);
    }

    const sizeStr = row.total_size === undefined ? "" : String(row.total_size);
    const timeStr = timeText(row.completed_time);
    setText(tr.querySelector(".cell-size"), sizeStr);
    setText(tr.querySelector(".cell-time"), timeStr);
    setText(
      tr.querySelector(".meta-inline"),
      [sizeStr, timeStr].filter(Boolean).join(" · ")
    );

    const pathStr = String(row.save_path || "");
    const pathCell = tr.querySelector(".cell-path");
    if (pathCell) pathCell.title = pathStr;
    setText(tr.querySelector(".cell-path .trunc-text"), pathStr);
    setText(tr.querySelector(".path-inline .trunc-text"), pathStr);
    syncExpandButtons(tr, pathStr.length > 34);

    const copyBtn = tr.querySelector('[data-action="copy"]');
    if (copyBtn) setActionLabel(copyBtn, "复制保存路径 " + name);
    const deleteBtn = tr.querySelector('[data-action="delete"]');
    if (deleteBtn && deleteBtn.dataset.confirm !== "1") {
      setActionLabel(deleteBtn, "删除记录 " + name);
    }
  }

  function buildFailedRow(row) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      idCell(row) +
      '<td class="cell-name">' +
      '<div class="name-line"><span class="file-title"></span></div>' +
      '<span class="meta-inline"></span>' +
      '<div class="reason-inline trunc-wrap">' +
      '<span class="trunc-text"></span>' +
      '<button class="expand-btn" type="button" data-expand="1" aria-expanded="false" hidden>展开</button>' +
      "</div>" +
      "</td>" +
      '<td class="cell-reason">' +
      '<div class="trunc-wrap">' +
      '<span class="trunc-text"></span>' +
      '<button class="expand-btn" type="button" data-expand="1" aria-expanded="false" hidden>展开</button>' +
      "</div>" +
      "</td>" +
      '<td class="cell-time"></td>' +
      '<td class="col-actions"><div class="row-actions">' +
      actionButton("retry", "重试", ICON_RETRY) +
      actionButton("delete", "删除", ICON_TRASH, "danger") +
      "</div></td>";
    return tr;
  }

  function updateFailedRow(tr, row) {
    const name = String(row.filename || "");
    const reason = String(row.reason || "");
    if (tr.dataset.filename !== name) {
      tr.dataset.filename = name;
      const nameCell = tr.querySelector(".cell-name");
      if (nameCell) nameCell.title = name || reason;
      const titleSpan = tr.querySelector(".file-title");
      if (titleSpan) titleSpan.innerHTML = nameHtml(name);
    }

    const timeStr = timeText(row.time);
    setText(tr.querySelector(".cell-time"), timeStr);
    setText(tr.querySelector(".meta-inline"), timeStr !== "—" ? "失败于 " + timeStr : "");

    const reasonCell = tr.querySelector(".cell-reason");
    if (reasonCell) reasonCell.title = reason;
    setText(tr.querySelector(".cell-reason .trunc-text"), reason);
    setText(tr.querySelector(".reason-inline .trunc-text"), reason);
    syncExpandButtons(tr, reason.length > 42);

    const retryBtn = tr.querySelector('[data-action="retry"]');
    if (retryBtn) setActionLabel(retryBtn, "重试下载 " + name);
    const deleteBtn = tr.querySelector('[data-action="delete"]');
    if (deleteBtn && deleteBtn.dataset.confirm !== "1") {
      setActionLabel(deleteBtn, "删除失败记录 " + name);
    }
  }

  /* ------------------------------------------------- filtering & pagination */

  function tabRows(tab) {
    if (tab === "downloading") return state.downloading;
    if (tab === "downloaded") return state.downloaded;
    return state.failed;
  }

  function countKey(tab) {
    if (tab === "downloading") return "downloading";
    if (tab === "downloaded") return "completed";
    return "failed";
  }

  function pagerState(tab) {
    if (!state.pager[tab]) {
      state.pager[tab] = { page: 1, pages: 1, total: 0, allTotal: 0 };
    }
    return state.pager[tab];
  }

  /**
   * Total rows for a tab. The visible page only holds a slice, so the number
   * comes from /get_download_status (or from the last page response).
   */
  function tabCount(tab) {
    return Math.max(
      state.counts[countKey(tab)] || 0,
      pagerState(tab).total || 0,
      tabRows(tab).length
    );
  }

  function hasActiveFilters(tab) {
    if (state.query.trim()) return true;
    if (tab === "downloaded" && state.uploadFilter !== "all") return true;
    return false;
  }

  /** The server already filtered and sliced the list, so this is the page. */
  function filteredRows(tab) {
    return tabRows(tab);
  }

  /** Pager metadata for ``tab``, built from the last list response. */
  function pageInfo(tab) {
    const rows = filteredRows(tab);
    const info = pagerState(tab);
    const page = info.page || 1;
    const pages = info.pages || 1;
    const size = state.pageSize || rows.length || 1;
    const start = rows.length ? (page - 1) * size : 0;
    return {
      rows,
      total: info.total || rows.length,
      page,
      pages,
      start,
      end: start + rows.length,
    };
  }

  function renderPager(tab, info) {
    if (!el.pager) return;
    if (tab !== state.tab) return;
    if (!info.total) {
      el.pager.hidden = true;
      return;
    }
    el.pager.hidden = false;
    setText(
      el.pagerInfo,
      "第 " +
        (info.start + 1) +
        "–" +
        info.end +
        " 条 / 共 " +
        info.total +
        " 条 · 第 " +
        info.page +
        "/" +
        info.pages +
        " 页"
    );
    if (el.pagePrev) el.pagePrev.disabled = info.page <= 1;
    if (el.pageNext) el.pageNext.disabled = info.page >= info.pages;
  }

  function syncToolbar() {
    if (el.taskSearchClear) {
      el.taskSearchClear.hidden = !state.query.length;
    }
    if (el.uploadFilters) {
      el.uploadFilters.hidden = state.tab !== "downloaded";
      $$("[data-upload-filter]", el.uploadFilters).forEach((btn) => {
        const on = btn.dataset.uploadFilter === state.uploadFilter;
        btn.classList.toggle("is-active", on);
        btn.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }
    if (el.completedBulkActions) {
      el.completedBulkActions.hidden =
        state.tab !== "downloaded" || tabCount("downloaded") === 0;
    }
    if (el.failedBulkActions) {
      el.failedBulkActions.hidden =
        state.tab !== "failed" || tabCount("failed") === 0;
    }
  }

  function renderEmpty() {
    const messages = {
      downloading: "队列空闲，暂无正在下载的任务",
      downloaded: "本次运行还没有完成的下载",
      failed: "没有失败任务",
    };
    const rawRows = tabRows(state.tab);
    const visibleRows = filteredRows(state.tab);
    const isFiltered = hasActiveFilters(state.tab);

    if (el.empty) el.empty.hidden = visibleRows.length > 0;
    if (visibleRows.length === 0) {
      if (rawRows.length > 0 && isFiltered) {
        const q = state.query.trim();
        setText(
          el.emptyText,
          q ? "没有与「" + q + "」匹配的任务" : "当前筛选条件下没有任务"
        );
        if (el.emptyAction) el.emptyAction.hidden = false;
      } else {
        setText(el.emptyText, messages[state.tab] || "");
        if (el.emptyAction) el.emptyAction.hidden = true;
      }
    }
  }

  /** Tab totals (stat cards + tab badges) from the server-side counts. */
  function renderCounts() {
    const active = tabCount("downloading");
    const done = tabCount("downloaded");
    const failed = tabCount("failed");

    setText(el.statActive, String(active));
    setText(el.badgeDownloading, String(active));
    if (el.badgeDownloading) el.badgeDownloading.hidden = active === 0;

    setText(el.statDone, String(done));
    setText(el.badgeDone, String(done));
    if (el.badgeDone) el.badgeDone.hidden = done === 0;

    setText(el.statFailed, String(failed));
    if (el.statFailed) el.statFailed.classList.toggle("is-danger", failed > 0);
    setText(el.badgeFailed, String(failed));
    if (el.badgeFailed) el.badgeFailed.hidden = failed === 0;
  }

  function renderDownloading() {
    const info = pageInfo("downloading");
    syncRows(
      el.tables.downloading.querySelector("tbody"),
      info.rows,
      rowKey,
      buildDownloadingRow,
      updateDownloadingRow
    );
    renderCounts();
    syncToolbar();
    renderPager("downloading", info);
    renderEmpty();
  }

  function renderDownloaded() {
    const info = pageInfo("downloaded");
    syncRows(
      el.tables.downloaded.querySelector("tbody"),
      info.rows,
      rowKey,
      buildDownloadedRow,
      updateDownloadedRow
    );
    renderCounts();
    syncToolbar();
    renderPager("downloaded", info);
    renderEmpty();
  }

  function renderFailed() {
    const info = pageInfo("failed");
    syncRows(
      el.tables.failed.querySelector("tbody"),
      info.rows,
      rowKey,
      buildFailedRow,
      updateFailedRow
    );
    renderCounts();
    syncToolbar();
    renderPager("failed", info);
    renderEmpty();
  }

  function renderStatus() {
    const running = state.action === "pause";
    setText(el.statState, running ? "运行中" : "已暂停");
    if (el.statState) {
      el.statState.classList.toggle("is-running", running);
      el.statState.classList.toggle("is-paused", !running);
    }
    if (el.liveDot) {
      el.liveDot.classList.toggle("is-paused", !running || state.pollingPaused);
    }

    if (el.pausedBanner) el.pausedBanner.hidden = running;

    if (el.pauseBtn) {
      el.pauseBtn.dataset.value = state.action;
      el.pauseBtn.classList.add("has-icon");
      el.pauseBtn.innerHTML =
        (running ? ICON_PAUSE : ICON_PLAY) +
        '<span class="pause-label">' +
        (running ? "暂停" : "继续") +
        "</span>";
      el.pauseBtn.setAttribute(
        "aria-label",
        running ? "暂停全部下载" : "继续全部下载"
      );
    }
  }

  function renderSpeed(speed) {
    const clean = speed && speed !== "--" ? speed : "—";
    setText(el.speed, clean);
    setText(el.metaSpeed, "↓ " + clean);
  }

  /* --------------------------------------------------------------- loading */

  const inFlight = {
    status: false,
    downloading: false,
    downloaded: false,
    failed: false,
  };

  function tabEndpoint(tab) {
    if (tab === "downloading") return "/get_download_list?already_down=false";
    if (tab === "downloaded") return "/get_download_list?already_down=true";
    return "/get_failed_list";
  }

  /**
   * The list endpoints page on the server: send the current page, page size,
   * the search query and (for the completed tab) the upload-state filter.
   */
  function tabUrl(tab) {
    const params = [
      "page=" + (pagerState(tab).page || 1),
      "page_size=" + state.pageSize,
    ];
    const q = state.query.trim();
    if (q) params.push("q=" + encodeURIComponent(q));
    if (tab === "downloaded") {
      params.push("upload_state=" + encodeURIComponent(state.uploadFilter));
    }
    const base = tabEndpoint(tab);
    // /get_failed_list has no query string yet, /get_download_list?already_down=…
    // does, so pick the right separator instead of always appending "&".
    return base + (base.indexOf("?") === -1 ? "?" : "&") + params.join("&");
  }

  function applyPageResponse(tab, payload) {
    let rows = [];
    let info = { page: 1, pages: 1, total: 0, allTotal: 0 };
    if (Array.isArray(payload)) {
      // Legacy shape (no paging parameters): the list is already complete.
      rows = payload;
      info = { page: 1, pages: 1, total: payload.length, allTotal: payload.length };
    } else if (payload && Array.isArray(payload.rows)) {
      rows = payload.rows;
      info = {
        page: Number(payload.page) || 1,
        pages: Number(payload.pages) || 1,
        total: Number(payload.total) || 0,
        allTotal: Number(payload.all_total || payload.total) || 0,
      };
    }
    state[tab] = rows;
    state.pager[tab] = info;
    // The tab badges show the unfiltered total, so a filtered page must not
    // overwrite the server count (loadStatus refreshes it every second).
    const filtered =
      Boolean(state.query.trim()) ||
      (tab === "downloaded" && state.uploadFilter !== "all");
    if (!filtered) state.counts[countKey(tab)] = info.total;
  }

  async function loadTab(tab) {
    if (inFlight[tab]) return;
    inFlight[tab] = true;
    try {
      const payload = await api(tabUrl(tab));
      applyPageResponse(tab, payload);
      if (tab === "downloading") renderDownloading();
      else if (tab === "downloaded") renderDownloaded();
      else if (state.tab === "failed") renderFailed();
      else {
        renderCounts();
        syncToolbar();
      }
    } finally {
      inFlight[tab] = false;
    }
  }

  function loadDownloading() {
    return loadTab("downloading");
  }

  function loadDownloaded() {
    return loadTab("downloaded");
  }

  function loadFailed() {
    return loadTab("failed");
  }

  async function loadStatus() {
    if (inFlight.status) return;
    inFlight.status = true;
    try {
      const res = await api("/get_download_status");
      renderSpeed(res && res.download_speed ? res.download_speed : "");
      if (res) {
        if (typeof res.downloading === "number") {
          state.counts.downloading = res.downloading;
        }
        if (typeof res.completed === "number") {
          state.counts.completed = res.completed;
        }
        if (typeof res.failed === "number") {
          state.counts.failed = res.failed;
        }
        renderCounts();
        syncToolbar();
      }
      const running = !(res && res.state === "paused");
      const nextAction = running ? "pause" : "continue";
      if (nextAction !== state.action) {
        state.action = nextAction;
        renderStatus();
        if (state.tab === "downloading") renderDownloading();
      }
      if (!state.pollingPaused) {
        setText(el.updated, "更新于 " + clockText());
      }
    } finally {
      inFlight.status = false;
    }
  }

  function stepPage(delta) {
    const tab = state.tab;
    const info = pagerState(tab);
    const next = Math.min(Math.max(1, (info.page || 1) + delta), info.pages || 1);
    if (next === info.page) return;
    info.page = next;
    refreshActive().catch(notifyError);
  }

  const PAGE_SIZES = [25, 50, 100, 200];

  function initPager() {
    let size = 25;
    try {
      const raw = localStorage.getItem("tgdl-page-size");
      const saved = raw === null ? NaN : Number(raw);
      if (PAGE_SIZES.indexOf(saved) !== -1) size = saved;
    } catch (e) {
      /* private mode: keep the default */
    }
    state.pageSize = size;
    if (el.pageSize) el.pageSize.value = String(size);
    if (el.pagePrev) el.pagePrev.addEventListener("click", () => stepPage(-1));
    if (el.pageNext) el.pageNext.addEventListener("click", () => stepPage(1));
    if (el.pageSize) {
      el.pageSize.addEventListener("change", () => {
        const next = Number(el.pageSize.value);
        state.pageSize = PAGE_SIZES.indexOf(next) !== -1 ? next : 25;
        try {
          localStorage.setItem("tgdl-page-size", String(state.pageSize));
        } catch (e) {
          /* private mode */
        }
        pagerState(state.tab).page = 1;
        refreshActive().catch(notifyError);
      });
    }
  }

  function refreshActive() {
    if (state.tab === "downloading") return loadDownloading();
    if (state.tab === "downloaded") return loadDownloaded();
    return loadFailed();
  }

  /* ------------------------------------------------------------------ tabs */

  function paintTab() {
    $$(".tab[data-tab]").forEach((btn) => {
      const on = btn.dataset.tab === state.tab;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
      btn.tabIndex = on ? 0 : -1;
    });

    Object.keys(el.tables).forEach((name) => {
      el.tables[name].hidden = name !== state.tab;
    });
  }

  function renderCachedTab() {
    if (state.tab === "downloading") renderDownloading();
    if (state.tab === "downloaded") renderDownloaded();
    if (state.tab === "failed") renderFailed();
  }

  function setTab(tab, focus) {
    if (!el.tables[tab]) return;
    state.tab = tab;
    paintTab();
    renderCachedTab();

    lastTabFetch = 0;
    if (!state.pollingPaused) {
      refreshActive().catch(notifyError);
    }

    try {
      history.replaceState(null, "", "#" + tab);
    } catch (e) {
      /* file:// or old browsers */
    }

    if (focus) {
      const target = $$(".tab[data-tab]").find((btn) => btn.dataset.tab === tab);
      if (target) target.focus();
    }
  }

  /* ----------------------------------------------------------------- theme */

  function currentTheme() {
    return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const label = theme === "dark" ? "切换到浅色主题" : "切换到深色主题";
    if (el.themeBtn) {
      el.themeBtn.setAttribute("aria-label", label);
      el.themeBtn.title = label;
    }
  }

  function swapTheme(theme) {
    // Suppress every transition for the swap, force a style flush, restore.
    document.documentElement.classList.add("theme-switching");
    applyTheme(theme);
    void document.documentElement.offsetWidth;
    requestAnimationFrame(() => {
      requestAnimationFrame(() =>
        document.documentElement.classList.remove("theme-switching")
      );
    });
  }

  function storedTheme() {
    try {
      return localStorage.getItem("tgdl-theme");
    } catch (e) {
      return null;
    }
  }

  function initTheme() {
    applyTheme(currentTheme());
    if (el.themeBtn) {
      el.themeBtn.addEventListener("click", () => {
        const next = currentTheme() === "dark" ? "light" : "dark";
        swapTheme(next);
        try {
          localStorage.setItem("tgdl-theme", next);
        } catch (e) {
          /* private mode: the theme just will not persist */
        }
      });
    }

    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (event) => {
      if (storedTheme()) return; // an explicit choice wins
      swapTheme(event.matches ? "dark" : "light");
    };
    if (media.addEventListener) media.addEventListener("change", onChange);
  }

  /* --------------------------------------------------------------- actions */

  async function togglePause() {
    if (isBusy(el.pauseBtn)) return;
    setBusy(el.pauseBtn, true);
    try {
      const next = await api(
        "/set_download_state?state=" + encodeURIComponent(state.action),
        { method: "POST" }
      );
      const value = String(next || "").trim();
      state.action =
        value === "pause" || value === "continue"
          ? value
          : state.action === "pause"
          ? "continue"
          : "pause";
      renderStatus();
      toast(state.action === "continue" ? "已暂停全部下载" : "已恢复全部下载", "success");
      lastTabFetch = 0;
      await refreshActive();
    } catch (err) {
      toast("操作失败：" + err.message, "error");
    } finally {
      setBusy(el.pauseBtn, false);
    }
  }

  function taskParams(row) {
    return (
      "chat_id=" + encodeURIComponent(row.chat) + "&message_id=" + encodeURIComponent(row.id)
    );
  }

  async function setTaskPaused(row, paused) {
    const url = (paused ? "/pause_task?" : "/resume_task?") + taskParams(row);
    const res = await api(url, { method: "POST" });
    if (!res || res.code !== "1") throw new Error((res && res.msg) || "未知错误");
    toast(paused ? "已暂停该任务" : "已恢复该任务", "success");
    lastTabFetch = 0;
    await loadDownloading();
  }

  async function deleteRow(row, label) {
    const res = await api("/delete_task?" + taskParams(row), { method: "POST" });
    if (!res || res.code !== "1") throw new Error((res && res.msg) || "未知错误");
    toast(label || "已删除任务", "success");
    lastTabFetch = 0;
    await Promise.all([
      loadDownloading().catch(() => {}),
      loadDownloaded().catch(() => {}),
      loadFailed().catch(() => {}),
    ]);
  }

  async function retryRow(row) {
    const res = await api("/retry_failed?" + taskParams(row), { method: "POST" });
    if (!res || res.code !== "1") throw new Error((res && res.msg) || "未知错误");
    toast(res.msg || "已重新加入下载队列", "success");
    lastTabFetch = 0;
    await Promise.all([loadFailed(), loadDownloading().catch(() => {})]);
  }

  async function copyPath(text) {
    if (!text) return;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const area = document.createElement("textarea");
        area.value = text;
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        area.remove();
      }
      toast("路径已复制", "success");
    } catch (e) {
      toast("复制失败，请手动选择路径", "error");
    }
  }

  /**
   * Two-step confirm: the first click arms the button for a few seconds.
   * Destructive actions get a distinct treatment instead of a silent delete.
   */
  function confirmOrRun(button, confirmText, confirmAria) {
    if (button.dataset.confirm === "1") {
      clearTimeout(Number(button.dataset.timer || 0));
      button.dataset.confirm = "0";
      restoreLabel(button);
      return;
    }

    button.dataset.confirm = "1";
    button.dataset.previousHtml = button.innerHTML;
    button.innerHTML = ICON_CHECK + "<span>" + (confirmText || "确认删除") + "</span>";
    const previousLabel = button.getAttribute("aria-label") || "";
    button.dataset.previousAria = previousLabel;
    setActionLabel(button, confirmAria || "确认删除？再点一次执行");

    const timer = setTimeout(() => {
      button.dataset.confirm = "0";
      restoreLabel(button);
    }, CONFIRM_TIMEOUT);
    button.dataset.timer = String(timer);
  }

  function restoreLabel(button) {
    if (!button) return;
    if (button.dataset.previousHtml) {
      button.innerHTML = button.dataset.previousHtml;
      delete button.dataset.previousHtml;
    }
    if (button.dataset.previousAria !== undefined) {
      setActionLabel(button, button.dataset.previousAria);
      delete button.dataset.previousAria;
    }
  }

  async function handleRowAction(button, row) {
    if (isBusy(button)) return;
    const action = button.dataset.action;

    if (action === "delete" && button.dataset.confirm !== "1") {
      confirmOrRun(button, "确认删除", "确认删除？再点一次执行");
      return;
    }

    setBusy(button, true);
    try {
      if (action === "delete") await deleteRow(row);
      else if (action === "pause") await setTaskPaused(row, true);
      else if (action === "resume") await setTaskPaused(row, false);
      else if (action === "retry") await retryRow(row);
      else if (action === "copy") await copyPath(row.save_path);
    } catch (err) {
      toast("操作失败：" + err.message, "error");
    } finally {
      setBusy(button, false);
      if (action === "delete") {
        clearTimeout(Number(button.dataset.timer || 0));
        button.dataset.confirm = "0";
        restoreLabel(button);
      }
    }
  }

  async function retryAllFailed() {
    if (isBusy(el.retryAllBtn) || tabCount("failed") === 0) return;

    setBusy(el.retryAllBtn, true);
    const originalText = el.retryAllBtn.textContent;
    setText(el.retryAllBtn, "提交中…");
    try {
      // No chat_id: the backend re-queues every recorded failure at once, so
      // the bulk action no longer only covers the visible page.
      const res = await api("/retry_failed", { method: "POST" });
      if (res && res.code === "1") {
        const msg = res.pending
          ? "已提交批量重试，稍后刷新查看结果"
          : "已重新加入 " + Number(res.retried || 0) + " 个失败任务";
        toast(msg, "success");
      } else {
        toast("批量重试失败：" + ((res && res.msg) || "未知错误"), "error");
      }
      lastTabFetch = 0;
      await Promise.all([
        loadFailed().catch(() => {}),
        loadDownloading().catch(() => {}),
        loadStatus().catch(() => {}),
      ]);
    } catch (err) {
      toast("批量重试失败：" + err.message, "error");
    } finally {
      setBusy(el.retryAllBtn, false);
      setText(el.retryAllBtn, originalText);
    }
  }

  async function clearAllFailed() {
    if (isBusy(el.clearFailedBtn) || tabCount("failed") === 0) return;
    if (el.clearFailedBtn.dataset.confirm !== "1") {
      confirmOrRun(el.clearFailedBtn, "确认清空", "确认清空全部失败记录？再点一次执行");
      return;
    }

    clearTimeout(Number(el.clearFailedBtn.dataset.timer || 0));
    el.clearFailedBtn.dataset.confirm = "0";
    restoreLabel(el.clearFailedBtn);

    setBusy(el.clearFailedBtn, true);
    try {
      const res = await api("/clear_failed", { method: "POST" });
      if (!res || res.code !== "1") throw new Error((res && res.msg) || "清空失败");
      const count = Number(res.cleared || 0);
      toast(count ? "已清空 " + count + " 条失败记录" : "已清空失败记录", "success");
      pagerState("failed").page = 1;
      lastTabFetch = 0;
      await Promise.all([loadFailed(), loadStatus().catch(() => {})]);
    } catch (err) {
      toast("清空失败：" + err.message, "error");
    } finally {
      setBusy(el.clearFailedBtn, false);
    }
  }

  async function clearAllCompleted() {
    if (isBusy(el.clearCompletedBtn) || tabCount("downloaded") === 0) return;
    if (el.clearCompletedBtn.dataset.confirm !== "1") {
      confirmOrRun(
        el.clearCompletedBtn,
        "确认清空",
        "确认清空全部已完成记录？只删记录，不删本地文件。再点一次执行"
      );
      return;
    }

    clearTimeout(Number(el.clearCompletedBtn.dataset.timer || 0));
    el.clearCompletedBtn.dataset.confirm = "0";
    restoreLabel(el.clearCompletedBtn);

    setBusy(el.clearCompletedBtn, true);
    try {
      const res = await api("/clear_completed", { method: "POST" });
      if (!res || res.code !== "1") throw new Error((res && res.msg) || "清空失败");
      const count = Number(res.cleared || 0);
      toast(
        count ? "已清空 " + count + " 条已完成记录（本地文件保留）" : "已完成记录已清空",
        "success"
      );
      pagerState("downloaded").page = 1;
      lastTabFetch = 0;
      await Promise.all([loadDownloaded(), loadStatus().catch(() => {})]);
    } catch (err) {
      toast("清空失败：" + err.message, "error");
    } finally {
      setBusy(el.clearCompletedBtn, false);
    }
  }

  function bindTable(table, source) {
    table.addEventListener("click", (event) => {
      const expandBtn = event.target.closest("[data-expand]");
      if (expandBtn) {
        const tr = expandBtn.closest("tr");
        if (tr) {
          tr.classList.toggle("is-expanded");
          syncExpandButtons(tr, true);
        }
        return;
      }

      const button = event.target.closest(".btn-mini");
      if (!button) return;
      const tr = button.closest("tr");
      const key = tr && tr.dataset.key;
      const row = source().find((item) => rowKey(item) === key);
      if (row) handleRowAction(button, row);
    });
  }

  /* ---------------------------------------------------------------- config */

  let configSaving = false;
  let configSchema = null;
  let configGroupFilter = "all";
  let configQuery = "";
  let formDirty = false;
  let yamlDirty = false;
  let discardArmed = false;
  let discardTimer = 0;

  function cfgId(prefix, name) {
    return prefix + "-" + name.replace(/\./g, "-");
  }

  function setConfigError(message) {
    setText(el.configError, message || "");
  }

  function resetDiscardPrompt() {
    discardArmed = false;
    clearTimeout(discardTimer);
    setText(el.configCancel, "取消");
    setConfigError("");
  }

  function syncDirtyNotes() {
    if (el.configFormSyncNote) {
      el.configFormSyncNote.hidden = !(state.configTab === "form" && yamlDirty);
    }
    if (el.configYamlSyncNote) {
      el.configYamlSyncNote.hidden = !(state.configTab === "yaml" && formDirty);
    }
  }

  function clearFieldErrors() {
    $$(".cfg-field.has-error", el.configForm).forEach((node) => {
      node.classList.remove("has-error");
      const error = node.querySelector(".cfg-error");
      if (error) {
        error.hidden = true;
        error.textContent = "";
      }
      const control = node.querySelector(".cfg-input, input, select, textarea");
      if (control) control.removeAttribute("aria-invalid");
    });
  }

  function showFieldErrors(errors) {
    // Clear filters first so failing fields are always visible and focusable.
    if (configQuery || configGroupFilter !== "all") {
      configQuery = "";
      configGroupFilter = "all";
      if (el.configSearch) el.configSearch.value = "";
      applyConfigFilter();
    }

    let firstControl = null;
    Object.keys(errors || {}).forEach((name) => {
      const node = el.configForm.querySelector(
        '[data-field="' + name.replace(/"/g, '\\"') + '"]'
      );
      if (!node) return;
      node.classList.add("has-error");
      const error = node.querySelector(".cfg-error");
      if (error) {
        error.textContent = errors[name];
        error.hidden = false;
      }
      const control = node.querySelector(".cfg-input, input, select, textarea");
      if (control) {
        control.setAttribute("aria-invalid", "true");
        if (!firstControl) firstControl = control;
      }
    });
    if (firstControl) {
      firstControl.focus({ preventScroll: true });
      firstControl.scrollIntoView({ block: "center" });
    }
  }

  function hintText(field) {
    let text = field.hint || "";
    if (field.default !== undefined && field.default !== null && field.default !== "") {
      text += (text ? " " : "") + "默认：" + String(field.default);
    }
    return text;
  }

  function buildField(field, value) {
    const name = field.name;
    const id = cfgId("f", name);
    const wrap = document.createElement("div");
    wrap.className = "cfg-field" + (field.span === 2 ? " cfg-span-2" : "");
    wrap.dataset.field = name;
    wrap.dataset.search = [field.label, field.name, field.hint, field.group]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();

    const hint = hintText(field);
    const hintHtml = hint
      ? '<p class="cfg-hint" id="' + cfgId("h", name) + '">' + esc(hint) + "</p>"
      : "";
    const errorHtml =
      '<p class="cfg-error" id="' + cfgId("e", name) + '" role="alert" hidden></p>';
    const describedBy = hint ? ' aria-describedby="' + cfgId("h", name) + '"' : "";
    const requiredMark = field.required
      ? ' <span class="req" aria-hidden="true">*</span>'
      : "";
    const requiredAttr = field.required ? ' aria-required="true"' : "";

    if (field.type === "bool") {
      wrap.classList.add("cfg-switch");
      wrap.innerHTML =
        '<div class="cfg-switch-row">' +
        '<label class="cfg-label" for="' +
        id +
        '">' +
        esc(field.label) +
        "</label>" +
        '<span class="switch"><input type="hidden" name="' +
        esc(name) +
        '__present" value="1"><input id="' +
        id +
        '" name="' +
        esc(name) +
        '" type="checkbox" value="1"' +
        describedBy +
        '><span class="switch-track" aria-hidden="true"></span></span>' +
        "</div>" +
        hintHtml +
        errorHtml;
      wrap.querySelector('input[type="checkbox"]').checked = Boolean(value);
      return wrap;
    }

    if (field.type === "multiselect") {
      const chosen = Array.isArray(value) ? value : [];
      const options = (field.options || [])
        .map(
          (option) =>
            '<label class="cfg-option"><input type="checkbox" name="' +
            esc(name) +
            '" value="' +
            esc(option[0]) +
            '"' +
            (chosen.indexOf(option[0]) !== -1 ? " checked" : "") +
            "><span>" +
            esc(option[1]) +
            "</span></label>"
        )
        .join("");
      wrap.classList.add("cfg-checks");
      wrap.innerHTML =
        '<fieldset class="cfg-fieldset"><legend class="cfg-label">' +
        esc(field.label) +
        requiredMark +
        "</legend>" +
        '<input type="hidden" name="' +
        esc(name) +
        '__present" value="1">' +
        '<div class="cfg-options"' +
        describedBy +
        ">" +
        options +
        "</div></fieldset>" +
        hintHtml +
        errorHtml;
      return wrap;
    }

    if (field.type === "select") {
      const options = (field.options || [])
        .map(
          (option) =>
            '<option value="' +
            esc(option[0]) +
            '">' +
            esc(option[1]) +
            "</option>"
        )
        .join("");
      wrap.innerHTML =
        '<label class="cfg-label" for="' +
        id +
        '">' +
        esc(field.label) +
        requiredMark +
        "</label>" +
        '<select id="' +
        id +
        '" name="' +
        esc(name) +
        '" class="cfg-input"' +
        describedBy +
        requiredAttr +
        '><option value="">（未设置）</option>' +
        options +
        "</select>" +
        hintHtml +
        errorHtml;
      wrap.querySelector("select").value =
        value === undefined || value === null ? "" : String(value);
      return wrap;
    }

    const isTextarea = ["lines", "chat_list", "textarea"].indexOf(field.type) !== -1;
    const inputType =
      field.type === "number"
        ? "number"
        : field.type === "password"
        ? "password"
        : "text";
    const placeholder = String(field.placeholder || "").replace(/\n/g, " / ");
    const bounds =
      field.type === "number"
        ? (field.min !== undefined ? ' min="' + field.min + '"' : "") +
          (field.max !== undefined ? ' max="' + field.max + '"' : "")
        : "";
    const autocomplete = field.type === "password" ? ' autocomplete="new-password"' : "";

    const inputMarkup = isTextarea
      ? '<textarea id="' +
        id +
        '" name="' +
        esc(name) +
        '" class="cfg-input cfg-textarea" rows="' +
        (field.type === "chat_list" ? 4 : 3) +
        '" placeholder="' +
        esc(placeholder) +
        '" spellcheck="false"' +
        describedBy +
        requiredAttr +
        "></textarea>"
      : '<input id="' +
        id +
        '" name="' +
        esc(name) +
        '" class="cfg-input" type="' +
        inputType +
        '" placeholder="' +
        esc(placeholder) +
        '"' +
        bounds +
        autocomplete +
        describedBy +
        requiredAttr +
        ">";

    wrap.innerHTML =
      '<label class="cfg-label" for="' +
      id +
      '">' +
      esc(field.label) +
      requiredMark +
      "</label>" +
      (field.type === "password"
        ? '<div class="cfg-input-wrap">' +
          inputMarkup +
          '<button class="pw-toggle" type="button" aria-label="显示' +
          esc(field.label) +
          '" aria-pressed="false" title="显示' +
          esc(field.label) +
          '">' +
          ICON_EYE +
          ICON_EYE_OFF +
          "</button></div>"
        : inputMarkup) +
      hintHtml +
      errorHtml;

    const control = wrap.querySelector(".cfg-input");
    control.value = value === undefined || value === null ? "" : String(value);

    const pwBtn = wrap.querySelector(".pw-toggle");
    if (pwBtn && control) {
      pwBtn.addEventListener("click", () => {
        const visible = control.type === "text";
        control.type = visible ? "password" : "text";
        pwBtn.setAttribute("aria-pressed", visible ? "false" : "true");
        const nextTitle = (visible ? "显示" : "隐藏") + field.label;
        pwBtn.setAttribute("aria-label", nextTitle);
        pwBtn.title = nextTitle;
        control.focus();
      });
    }

    return wrap;
  }

  function renderConfigGroupNav(groups) {
    if (!el.configGroupNav) return;
    el.configGroupNav.innerHTML = "";
    const allItems = ["all"].concat(groups || []);
    allItems.forEach((group) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "filter-pill" + (configGroupFilter === group ? " is-active" : "");
      btn.dataset.cfgGroup = group;
      btn.textContent = group === "all" ? "全部" : group;
      btn.setAttribute("aria-pressed", configGroupFilter === group ? "true" : "false");
      btn.addEventListener("click", () => {
        configGroupFilter = group;
        applyConfigFilter();
      });
      el.configGroupNav.appendChild(btn);
    });
  }

  function applyConfigFilter() {
    const q = configQuery.trim().toLowerCase();
    let visibleCount = 0;

    if (el.configGroupNav) {
      $$("[data-cfg-group]", el.configGroupNav).forEach((btn) => {
        const on = btn.dataset.cfgGroup === configGroupFilter;
        btn.classList.toggle("is-active", on);
        btn.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }

    $$(".cfg-group", el.configForm).forEach((section) => {
      const groupName = section.dataset.group || "";
      const groupMatch = configGroupFilter === "all" || groupName === configGroupFilter;
      let sectionHasVisible = false;

      $$(".cfg-field", section).forEach((fieldEl) => {
        const textMatch = !q || (fieldEl.dataset.search || "").indexOf(q) !== -1;
        const show = groupMatch && textMatch;
        fieldEl.hidden = !show;
        if (show) {
          sectionHasVisible = true;
          visibleCount += 1;
        }
      });
      section.hidden = !sectionHasVisible;
    });

    if (el.configEmpty) el.configEmpty.hidden = visibleCount > 0;
  }

  function renderConfigForm(payload) {
    const fields = (payload && payload.fields) || [];
    const values = (payload && payload.values) || {};
    const groups = (payload && payload.groups) || [];
    el.configForm.innerHTML = "";

    const activeGroups = [];
    groups.forEach((group) => {
      const groupFields = fields.filter((field) => field.group === group);
      if (!groupFields.length) return;
      activeGroups.push(group);

      const section = document.createElement("section");
      section.className = "cfg-group";
      section.dataset.group = group;
      const title = document.createElement("h3");
      title.className = "cfg-group-title";
      title.textContent = group;
      section.appendChild(title);

      const grid = document.createElement("div");
      grid.className = "cfg-grid";
      groupFields.forEach((field) => {
        grid.appendChild(buildField(field, values[field.name]));
      });
      section.appendChild(grid);
      el.configForm.appendChild(section);
    });

    renderConfigGroupNav(activeGroups);
    applyConfigFilter();
  }

  function labelForConfigKey(key) {
    if (!configSchema) return key;
    const match = (configSchema.fields || []).find(
      (field) => field.path && field.path[0] === key
    );
    return match ? match.label : key;
  }

  function paintConfigTab() {
    $$("[data-cfg-tab]").forEach((btn) => {
      const on = btn.dataset.cfgTab === state.configTab;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
      btn.tabIndex = on ? 0 : -1;
    });
    el.formPane.hidden = state.configTab !== "form";
    el.yamlPane.hidden = state.configTab !== "yaml";
    syncDirtyNotes();
  }

  function switchConfigTab(tab, focus) {
    state.configTab = tab;
    paintConfigTab();
    if (focus) {
      const target = $$("[data-cfg-tab]").find((btn) => btn.dataset.cfgTab === tab);
      if (target) target.focus();
    }
    if (tab === "yaml") {
      el.configText.scrollTop = 0;
    }
  }

  async function openConfig() {
    setConfigError("");
    clearFieldErrors();
    formDirty = false;
    yamlDirty = false;
    resetDiscardPrompt();
    configGroupFilter = "all";
    configQuery = "";
    if (el.configSearch) el.configSearch.value = "";
    if (el.configToolbar) el.configToolbar.hidden = true;
    if (el.configEmpty) el.configEmpty.hidden = true;

    el.configSave.disabled = true;
    el.configLoading.hidden = false;
    setText(el.configLoading, "正在读取 config.yaml…");
    el.configForm.innerHTML = "";
    state.configTab = "form";
    paintConfigTab();

    if (typeof el.dialog.showModal === "function") {
      el.dialog.showModal();
    } else {
      el.dialog.setAttribute("open", "");
    }

    try {
      const res = await api("/get_config_form");
      if (!res || res.code !== "1") throw new Error((res && res.msg) || "读取失败");
      configSchema = res;
      setText(el.configPath, res.path);
      el.configText.value = res.text;
      el.configText.scrollTop = 0;
      renderConfigForm(res);
      el.configLoading.hidden = true;
      if (el.configToolbar) el.configToolbar.hidden = false;
      el.configSave.disabled = false;
      const first = el.configForm.querySelector(".cfg-input, input, select, textarea");
      if (first) first.focus({ preventScroll: true });
    } catch (err) {
      setText(el.configLoading, "读取配置失败：" + err.message);
      setConfigError(err.message);
    }
  }

  function requestCloseConfig() {
    if ((formDirty || yamlDirty) && !discardArmed) {
      discardArmed = true;
      setText(el.configCancel, "确认放弃");
      setConfigError("有未保存修改，再点一次「确认放弃」或按 Esc 关闭");
      clearTimeout(discardTimer);
      discardTimer = setTimeout(resetDiscardPrompt, 4500);
      return;
    }
    forceCloseConfig();
  }

  function forceCloseConfig() {
    formDirty = false;
    yamlDirty = false;
    resetDiscardPrompt();
    if (typeof el.dialog.close === "function") el.dialog.close();
    else el.dialog.removeAttribute("open");
    if (el.configBtn) el.configBtn.focus();
  }

  function afterConfigSaved(res) {
    const applied = (res.applied || []).length;
    const restart = (res.restart_required || []).map(labelForConfigKey);
    if (res.text !== undefined) el.configText.value = res.text;
    if (res.values) {
      renderConfigForm({
        fields: configSchema.fields,
        groups: configSchema.groups,
        values: res.values,
      });
    }
    toast(
      "配置已保存。" +
        (applied ? "已生效 " + applied + " 项。" : "") +
        (restart.length ? "需要重启：" + restart.join("、") : ""),
      "success"
    );
    forceCloseConfig();
    lastTabFetch = 0;
    refreshActive().catch(() => {});
  }

  async function saveConfig() {
    if (configSaving || isBusy(el.configSave)) return;
    configSaving = true;
    setConfigError("");
    clearFieldErrors();
    setBusy(el.configSave, true);
    setText(el.configSave, "保存中…");

    try {
      let res;
      if (state.configTab === "yaml") {
        const body = new URLSearchParams();
        body.set("text", el.configText.value);
        res = await api("/set_config", { method: "POST", body: body });
        if (res && res.code === "1") {
          if (res.text !== undefined) el.configText.value = res.text;
          afterConfigSaved(res);
          return;
        }
      } else {
        const body = new FormData(el.configForm);
        res = await api("/set_config_form", { method: "POST", body: body });
        if (res && res.code === "1") {
          afterConfigSaved(res);
          return;
        }
      }

      if (res && res.errors) showFieldErrors(res.errors);
      setConfigError((res && res.msg) || "保存失败");
    } catch (err) {
      setConfigError(err.message);
    } finally {
      configSaving = false;
      setBusy(el.configSave, false);
      setText(el.configSave, "保存");
    }
  }

  async function logout() {
    try {
      await api("/logout", { method: "POST" });
    } catch (e) {
      /* the cookie is gone even if the response is odd; fall through */
    }
    window.location.href = "/login";
  }

  /* ------------------------------------------------------------------ loop */

  let lastTabFetch = 0;
  let errorCount = 0;

  function notifyError(err) {
    errorCount += 1;
    if (el.liveDot) el.liveDot.classList.add("is-error");
    if (errorCount === 1 || errorCount % 15 === 0) {
      setText(el.updated, "连接异常：" + err.message);
    }
  }

  function clearPollError() {
    errorCount = 0;
    if (el.liveDot) el.liveDot.classList.remove("is-error");
  }

  function tick() {
    if (document.hidden || state.pollingPaused) return;
    const now = Date.now();

    // Status also carries the per-tab counts, so the badges stay right without
    // pulling the whole lists down.
    loadStatus()
      .then(clearPollError)
      .catch(notifyError);

    const interval = TAB_REFRESH[state.tab] || 1000;
    if (now - lastTabFetch >= interval) {
      lastTabFetch = now;
      refreshActive().catch(notifyError);
    }
  }

  function togglePolling() {
    state.pollingPaused = !state.pollingPaused;
    if (el.pollToggle) {
      el.pollToggle.setAttribute("aria-pressed", state.pollingPaused ? "true" : "false");
      const label = state.pollingPaused ? "恢复自动刷新" : "暂停自动刷新";
      el.pollToggle.setAttribute("aria-label", label);
      el.pollToggle.title = label;
    }
    renderStatus();
    if (state.pollingPaused) {
      setText(el.updated, "已暂停自动刷新");
    } else {
      lastTabFetch = 0;
      tick();
    }
  }

  /* ------------------------------------------------------------------ init */

  function clearFilters() {
    state.query = "";
    state.uploadFilter = "all";
    pagerState(state.tab).page = 1;
    if (el.taskSearch) el.taskSearch.value = "";
    refreshActive().catch(notifyError);
    syncToolbar();
  }

  function bind() {
    const tabs = $$(".tab[data-tab]");
    tabs.forEach((btn) => {
      btn.addEventListener("click", () => setTab(btn.dataset.tab, false));
    });

    const tablist = $(".tabs");
    if (tablist) {
      // APG tab key map: arrows move between tabs, Home/End jump to the ends.
      tablist.addEventListener("keydown", (event) => {
        const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
        if (keys.indexOf(event.key) === -1) return;
        event.preventDefault();
        const index = tabs.indexOf(document.activeElement);
        if (index === -1) return;
        let next = index;
        if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
        if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = tabs.length - 1;
        setTab(tabs[next].dataset.tab, true);
      });
    }

    window.addEventListener("hashchange", () => {
      const target = (location.hash || "").replace("#", "");
      if (el.tables[target] && target !== state.tab) {
        setTab(target, false);
      }
    });

    if (el.taskSearch) {
      // Debounce: every keystroke triggers a server-side search query.
      let searchTimer = 0;
      el.taskSearch.addEventListener("input", () => {
        state.query = el.taskSearch.value || "";
        pagerState(state.tab).page = 1;
        syncToolbar();
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          refreshActive().catch(notifyError);
        }, 250);
      });
    }

    if (el.taskSearchClear) {
      el.taskSearchClear.addEventListener("click", () => {
        state.query = "";
        pagerState(state.tab).page = 1;
        if (el.taskSearch) {
          el.taskSearch.value = "";
          el.taskSearch.focus();
        }
        syncToolbar();
        refreshActive().catch(notifyError);
      });
    }

    if (el.uploadFilters) {
      el.uploadFilters.addEventListener("click", (event) => {
        const btn = event.target.closest("[data-upload-filter]");
        if (!btn) return;
        state.uploadFilter = btn.dataset.uploadFilter || "all";
        pagerState("downloaded").page = 1;
        syncToolbar();
        loadDownloaded().catch(notifyError);
      });
    }

    if (el.emptyAction) {
      el.emptyAction.addEventListener("click", clearFilters);
    }

    if (el.retryAllBtn) el.retryAllBtn.addEventListener("click", retryAllFailed);
    if (el.clearFailedBtn) el.clearFailedBtn.addEventListener("click", clearAllFailed);
    if (el.clearCompletedBtn) {
      el.clearCompletedBtn.addEventListener("click", clearAllCompleted);
    }
    if (el.pollToggle) el.pollToggle.addEventListener("click", togglePolling);

    el.pauseBtn.addEventListener("click", togglePause);

    bindTable(el.tables.downloading, () => state.downloading);
    bindTable(el.tables.downloaded, () => state.downloaded);
    bindTable(el.tables.failed, () => state.failed);

    if (el.configBtn) el.configBtn.addEventListener("click", openConfig);
    if (el.configClose) el.configClose.addEventListener("click", requestCloseConfig);
    if (el.configCancel) el.configCancel.addEventListener("click", requestCloseConfig);
    if (el.configSave) el.configSave.addEventListener("click", saveConfig);

    if (el.configSearch) {
      el.configSearch.addEventListener("input", () => {
        configQuery = el.configSearch.value || "";
        applyConfigFilter();
      });
    }

    if (el.configForm) {
      const markFormDirty = () => {
        formDirty = true;
        if (discardArmed) resetDiscardPrompt();
      };
      el.configForm.addEventListener("input", markFormDirty);
      el.configForm.addEventListener("change", markFormDirty);
    }

    if (el.configText) {
      el.configText.addEventListener("input", () => {
        yamlDirty = true;
        if (discardArmed) resetDiscardPrompt();
      });
    }

    $$("[data-cfg-tab]").forEach((btn) => {
      btn.addEventListener("click", () => switchConfigTab(btn.dataset.cfgTab, false));
    });

    const cfgTablist = $(".dialog-tabs");
    if (cfgTablist) {
      const cfgTabs = $$("[data-cfg-tab]");
      cfgTablist.addEventListener("keydown", (event) => {
        const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
        if (keys.indexOf(event.key) === -1) return;
        event.preventDefault();
        const index = cfgTabs.indexOf(document.activeElement);
        if (index === -1) return;
        let next = index;
        if (event.key === "ArrowLeft") next = (index - 1 + cfgTabs.length) % cfgTabs.length;
        if (event.key === "ArrowRight") next = (index + 1) % cfgTabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = cfgTabs.length - 1;
        switchConfigTab(cfgTabs[next].dataset.cfgTab, true);
      });
    }

    if (el.logoutBtn) el.logoutBtn.addEventListener("click", logout);

    if (el.dialog) {
      el.dialog.addEventListener("cancel", (event) => {
        event.preventDefault();
        requestCloseConfig();
      });
      el.dialog.addEventListener("click", (event) => {
        if (event.target === el.dialog) {
          requestCloseConfig();
        }
      });
      el.dialog.addEventListener("close", () => {
        if (el.configBtn) el.configBtn.focus();
      });
      el.dialog.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
          event.preventDefault();
          saveConfig();
        }
      });
    }

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && !state.pollingPaused) {
        lastTabFetch = 0;
        tick();
      }
    });
  }

  function start() {
    initTheme();
    bind();
    initPager();
    renderStatus();
    renderSpeed("—");

    const initial = (location.hash || "").replace("#", "");
    if (el.tables[initial]) state.tab = initial;
    paintTab();
    syncToolbar();
    renderEmpty();

    loadStatus().then(clearPollError).catch(notifyError);
    loadFailed().catch(() => {});
    loadDownloaded().catch(() => {});
    if (state.tab !== "downloading") loadDownloading().catch(() => {});
    refreshActive().catch(notifyError);
    checkVersion();
    checkUpdate();
    // The server caches the answer for hours; this only refreshes the hint.
    setInterval(checkUpdate, 30 * 60 * 1000);

    setInterval(tick, STATUS_REFRESH);
  }

  function boot() {
    // A cached script from another release used to throw here and leave every
    // button dead with no explanation. Show what happened and how to fix it.
    try {
      start();
    } catch (err) {
      showFatalBanner(
        "界面初始化失败：" + err.message + "。请按 Ctrl+F5 强制刷新。"
      );
    }
  }

  window.addEventListener("error", (event) => {
    if (event && event.message) {
      showFatalBanner(
        "页面脚本出错：" + event.message + "。请按 Ctrl+F5 强制刷新。"
      );
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
