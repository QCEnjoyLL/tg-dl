/* tg-dl web ui — polling, rendering, per-task controls, config editor. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const TAB_REFRESH = { downloading: 1000, downloaded: 4000, failed: 3000 };
  const STATUS_REFRESH = 1000;
  const FAILED_BADGE_REFRESH = 10000;
  const COUNTER_REFRESH = 10000;
  const CONFIRM_TIMEOUT = 3500;

  const ICON_PAUSE =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M9 5v14M15 5v14"/></svg>';
  const ICON_PLAY =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M8 5.5v13l11-6.5z"/></svg>';
  const ICON_TRASH =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16"/><path d="M9 7V5h6v2"/><path d="M6 7l1 13h10l1-13"/></svg>';
  const ICON_RETRY =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v5h-5"/></svg>';
  const ICON_COPY =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg>';
  const ICON_CHECK =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 13 4 4L19 7"/></svg>';

  const state = {
    tab: "downloading",
    action: $("#pause-btn") ? $("#pause-btn").dataset.value : "pause",
    configTab: "form",
    downloading: [],
    downloaded: [],
    failed: [],
    // Client-side pagination: the polling endpoints keep returning the full
    // list (it is capped server-side), the table only renders one page.
    pageSize: 25,
    pages: { downloading: 1, downloaded: 1, failed: 1 },
  };

  const el = {
    speed: $("#speed"),
    statActive: $("#stat-active"),
    statDone: $("#stat-done"),
    statFailed: $("#stat-failed"),
    statState: $("#stat-state"),
    badgeDownloading: $("#badge-downloading"),
    badgeDone: $("#badge-done"),
    badgeFailed: $("#badge-failed"),
    updated: $("#updated"),
    liveDot: $("#live-dot"),
    empty: $("#empty"),
    emptyText: $("#empty-text"),
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
    if (node) node.textContent = text;
    return node;
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

  function nameHtml(name) {
    const text = String(name === null || name === undefined ? "" : name);
    const dot = text.lastIndexOf(".");
    const hasExt = dot > 0 && text.length - dot <= 8;
    if (!hasExt) {
      return esc(text) || '<span class="ext">(未命名)</span>';
    }
    return esc(text.slice(0, dot)) + '<span class="ext">' + esc(text.slice(dot)) + "</span>";
  }

  function chatLabel(chat) {
    // Show the full chat id; the column is wide enough on desktop and truncates
    // through CSS only on narrow screens.
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
    if (!ts) return "-";
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

  function toast(message, tone) {
    const node = document.createElement("div");
    node.className = "toast";
    node.dataset.tone = tone || "info";
    node.textContent = message;
    if (!el.toasts) return;
    el.toasts.appendChild(node);
    // Force a frame so the entrance transition runs.
    requestAnimationFrame(() => node.classList.add("is-visible"));
    setTimeout(() => {
      node.classList.add("is-leaving");
      setTimeout(() => node.remove(), 200);
    }, tone === "error" ? 5200 : 3200);
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
    button.setAttribute("aria-label", label);
    button.setAttribute("title", label);
  }

  function buildDownloadingRow(row) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      idCell(row) +
      '<td class="cell-name" title="' +
      esc(row.filename) +
      '">' +
      '<span class="row-chip state-chip is-paused" hidden>已暂停</span> ' +
      nameHtml(row.filename) +
      "</td>" +
      '<td class="cell-size">' +
      esc(row.total_size) +
      "</td>" +
      '<td class="cell-progress"><div class="progress">' +
      '<div class="bar"><i style="width:0%"></i></div>' +
      '<span class="pct">0%</span></div></td>' +
      '<td class="cell-speed">' +
      esc(row.download_speed) +
      "</td>" +
      '<td class="col-actions"><div class="row-actions">' +
      actionButton("pause", "暂停", ICON_PAUSE) +
      actionButton("delete", "删除", ICON_TRASH, "danger") +
      "</div></td>";
    return tr;
  }

  function updateDownloadingRow(tr, row) {
    const pct = pctValue(row.download_progress);
    const fill = tr.querySelector(".bar > i");
    if (fill && fill.style.width !== pct + "%") fill.style.width = pct + "%";
    const label = tr.querySelector(".pct");
    if (label) {
      const text = pctText(pct);
      if (label.textContent !== text) label.textContent = text;
    }

    const globalPaused = state.action === "continue";
    const speed = tr.querySelector(".cell-speed");
    const frozen = globalPaused || Boolean(row.paused);
    const value = frozen
      ? "—"
      : row.download_speed === undefined
      ? ""
      : String(row.download_speed);
    if (speed && speed.textContent !== value) speed.textContent = value;

    const chip = tr.querySelector(".row-chip");
    if (chip) {
      chip.hidden = !(globalPaused || row.paused);
      chip.textContent = row.paused ? "单条暂停" : "已暂停";
    }
    tr.classList.toggle("is-paused", globalPaused || Boolean(row.paused));

    const pauseBtn = tr.querySelector('[data-action="pause"]');
    if (pauseBtn) {
      const paused = Boolean(row.paused);
      pauseBtn.dataset.action = paused ? "resume" : "pause";
      pauseBtn.dataset.tone = paused ? "warn" : "";
      pauseBtn.innerHTML =
        (paused ? ICON_PLAY : ICON_PAUSE) + "<span>" + (paused ? "继续" : "暂停") + "</span>";
      setActionLabel(
        pauseBtn,
        (paused ? "继续下载 " : "暂停下载 ") + row.filename
      );
    }

    const deleteBtn = tr.querySelector('[data-action="delete"]');
    if (deleteBtn) setActionLabel(deleteBtn, "删除任务 " + row.filename);
  }

  function uploadChipHtml(row) {
    // upload_state: "uploaded" = bytes were transferred now,
    // "exists" = remote already had an identical file (rclone skipped it),
    // missing = still only local.
    const uploadState = row.upload_state;
    if (uploadState === "exists") {
      return '<span class="state-chip is-remote">云端已有</span>';
    }
    if (uploadState === "uploaded" || (!uploadState && row.upload_success)) {
      return '<span class="state-chip is-uploaded">已上传</span>';
    }
    return '<span class="state-chip">仅本地</span>';
  }

  function buildDownloadedRow(row) {
    const tr = document.createElement("tr");
    const uploadChip = uploadChipHtml(row);
    tr.innerHTML =
      idCell(row) +
      '<td class="cell-name" title="' +
      esc(row.filename) +
      '">' +
      uploadChip +
      " " +
      nameHtml(row.filename) +
      "</td>" +
      '<td class="cell-size">' +
      esc(row.total_size) +
      "</td>" +
      '<td class="cell-time">' +
      timeText(row.completed_time) +
      "</td>" +
      '<td class="cell-path" title="' +
      esc(row.save_path) +
      '">' +
      esc(row.save_path) +
      "</td>" +
      '<td class="col-actions"><div class="row-actions">' +
      actionButton("copy", "复制", ICON_COPY) +
      actionButton("delete", "删除", ICON_TRASH, "danger") +
      "</div></td>";
    return tr;
  }

  function updateDownloadedRow(tr, row) {
    const deleteBtn = tr.querySelector('[data-action="delete"]');
    if (deleteBtn) setActionLabel(deleteBtn, "删除记录 " + row.filename);
    const copyBtn = tr.querySelector('[data-action="copy"]');
    if (copyBtn) setActionLabel(copyBtn, "复制保存路径 " + row.filename);
  }

  function buildFailedRow(row) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      idCell(row) +
      '<td class="cell-name" title="' +
      esc(row.filename || row.reason) +
      '">' +
      nameHtml(row.filename) +
      '<span class="reason-inline">' +
      esc(row.reason) +
      "</span></td>" +
      '<td class="cell-reason" title="' +
      esc(row.reason) +
      '">' +
      esc(row.reason) +
      "</td>" +
      '<td class="cell-time">' +
      timeText(row.time) +
      "</td>" +
      '<td class="col-actions"><div class="row-actions">' +
      actionButton("retry", "重试", ICON_RETRY) +
      actionButton("delete", "删除", ICON_TRASH, "danger") +
      "</div></td>";
    return tr;
  }

  function updateFailedRow(tr, row) {
    const retryBtn = tr.querySelector('[data-action="retry"]');
    if (retryBtn) setActionLabel(retryBtn, "重试下载 " + row.filename);
    const deleteBtn = tr.querySelector('[data-action="delete"]');
    if (deleteBtn) setActionLabel(deleteBtn, "删除失败记录 " + row.filename);

    const text = row.reason || "";
    const reasonCell = tr.querySelector(".cell-reason");
    if (reasonCell) {
      if (reasonCell.textContent !== text) reasonCell.textContent = text;
      reasonCell.title = text;
    }
    // Narrow layouts hide the dedicated column and show the reason under the
    // file name instead.
    const inlineReason = tr.querySelector(".reason-inline");
    if (inlineReason) {
      if (inlineReason.textContent !== text) inlineReason.textContent = text;
    }
  }

  function tabRows(tab) {
    if (tab === "downloading") return state.downloading;
    if (tab === "downloaded") return state.downloaded;
    return state.failed;
  }

  /** Slice the full list into the current page for ``tab``. */
  function pageInfo(tab) {
    const rows = tabRows(tab);
    const total = rows.length;
    const size = state.pageSize;
    if (!size) {
      state.pages[tab] = 1;
      return { rows, total, page: 1, pages: 1, start: 0, end: total };
    }
    const pages = Math.max(1, Math.ceil(total / size));
    const page = Math.min(Math.max(1, state.pages[tab] || 1), pages);
    state.pages[tab] = page;
    const start = (page - 1) * size;
    return {
      rows: rows.slice(start, start + size),
      total,
      page,
      pages,
      start,
      end: Math.min(start + size, total),
    };
  }

  function renderPager(tab, info) {
    if (!el.pager) return;
    // One shared pager: only the visible tab may update it (background loads
    // for other tabs used to overwrite it mid-switch).
    if (tab !== state.tab) return;
    // Keep the footer visible whenever there is data: the page-size selector
    // lives there, so hiding it on a single page would trap the choice.
    if (!info.total) {
      el.pager.hidden = true;
      return;
    }
    el.pager.hidden = false;
    setText(
      el.pagerInfo,
      state.pageSize
        ? "第 " +
            (info.start + 1) +
            "-" +
            info.end +
            " 条 / 共 " +
            info.total +
            " 条 · 第 " +
            info.page +
            "/" +
            info.pages +
            " 页"
        : "共 " + info.total + " 条"
    );
    if (el.pagePrev) el.pagePrev.disabled = !state.pageSize || info.page <= 1;
    if (el.pageNext)
      el.pageNext.disabled = !state.pageSize || info.page >= info.pages;
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
    setText(el.statActive, state.downloading.length);
    setText(el.badgeDownloading, state.downloading.length);
    if (el.badgeDownloading) el.badgeDownloading.hidden = state.downloading.length === 0;
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
    setText(el.statDone, state.downloaded.length);
    setText(el.badgeDone, state.downloaded.length);
    if (el.badgeDone) el.badgeDone.hidden = state.downloaded.length === 0;
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
    renderPager("failed", info);
    renderEmpty();
  }

  function renderEmpty() {
    const messages = {
      downloading: "队列空闲，暂无正在下载的任务",
      downloaded: "本次运行还没有完成的下载",
      failed: "没有失败任务",
    };
    const rows = tabRows(state.tab);
    if (el.empty) el.empty.hidden = rows.length > 0;
    setText(el.emptyText, messages[state.tab] || "");
  }

  function renderStatus() {
    const running = state.action === "pause";
    setText(el.statState, running ? "运行中" : "已暂停");
    if (el.statState) {
      el.statState.classList.toggle("is-running", running);
      el.statState.classList.toggle("is-paused", !running);
    }
    if (el.liveDot) el.liveDot.classList.toggle("is-paused", !running);

    if (el.pausedBanner) el.pausedBanner.hidden = running;

    if (el.pauseBtn) {
      el.pauseBtn.dataset.value = state.action;
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
    setText(el.speed, speed || "--");
  }

  /* --------------------------------------------------------------- loading */

  async function loadDownloading() {
    const rows = await api("/get_download_list?already_down=false");
    state.downloading = Array.isArray(rows) ? rows : [];
    renderDownloading();
  }

  async function loadDownloaded() {
    const rows = await api("/get_download_list?already_down=true");
    state.downloaded = Array.isArray(rows) ? rows : [];
    renderDownloaded();
  }

  async function loadFailed() {
    const rows = await api("/get_failed_list");
    state.failed = Array.isArray(rows) ? rows : [];
    setText(el.statFailed, state.failed.length);
    setText(el.badgeFailed, state.failed.length);
    if (el.badgeFailed) el.badgeFailed.hidden = state.failed.length === 0;
    if (state.tab === "failed") renderFailed();
  }

  async function loadStatus() {
    const res = await api("/get_download_status");
    renderSpeed(res && res.download_speed ? res.download_speed : "");
    if (res && typeof res.completed === "number") {
      setText(el.statDone, res.completed);
      setText(el.badgeDone, res.completed);
      if (el.badgeDone) el.badgeDone.hidden = res.completed === 0;
    }
    const running = !(res && res.state === "paused");
    const nextAction = running ? "pause" : "continue";
    if (nextAction !== state.action) {
      state.action = nextAction;
      renderStatus();
      if (state.tab === "downloading") renderDownloading();
    }
    setText(el.updated, "更新于 " + clockText());
  }

  function stepPage(delta) {
    if (!state.pageSize) return;
    const tab = state.tab;
    const total = tabRows(tab).length;
    const pages = Math.max(1, Math.ceil(total / state.pageSize));
    const next = Math.min(Math.max(1, (state.pages[tab] || 1) + delta), pages);
    if (next === state.pages[tab]) return;
    state.pages[tab] = next;
    renderCachedTab();
  }

  function initPager() {
    let size = 25;
    try {
      const raw = localStorage.getItem("tgdl-page-size");
      const saved = raw === null ? NaN : Number(raw);
      if ([0, 25, 50, 100].indexOf(saved) !== -1) size = saved;
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
        state.pageSize = [0, 25, 50, 100].indexOf(next) !== -1 ? next : 25;
        try {
          localStorage.setItem("tgdl-page-size", String(state.pageSize));
        } catch (e) {
          /* private mode */
        }
        state.pages[state.tab] = 1;
        renderCachedTab();
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
    $$(".tab").forEach((btn) => {
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
    renderEmpty();
  }

  function setTab(tab, focus) {
    if (!el.tables[tab]) return;
    state.tab = tab;
    paintTab();
    renderCachedTab();

    lastTabFetch = 0;
    refreshActive().catch(notifyError);

    try {
      history.replaceState(null, "", "#" + tab);
    } catch (e) {
      /* file:// or old browsers */
    }

    if (focus) {
      const target = $$(".tab").find((btn) => btn.dataset.tab === tab);
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
    el.pauseBtn.disabled = true;
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
      el.pauseBtn.disabled = false;
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
    toast("已重新加入下载队列", "success");
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
   * The armed state swaps icon and label, so it is not carried by colour alone.
   */
  function confirmOrRun(button) {
    if (button.dataset.confirm === "1") {
      clearTimeout(Number(button.dataset.timer || 0));
      button.dataset.confirm = "0";
      restoreLabel(button);
      return;
    }

    button.dataset.confirm = "1";
    button.dataset.previousHtml = button.innerHTML;
    button.innerHTML = ICON_CHECK + "<span>确认删除</span>";
    const previousLabel = button.getAttribute("aria-label") || "";
    button.dataset.previousAria = previousLabel;
    setActionLabel(button, "确认删除？再点一次执行");

    const timer = setTimeout(() => {
      button.dataset.confirm = "0";
      restoreLabel(button);
    }, CONFIRM_TIMEOUT);
    button.dataset.timer = String(timer);
  }

  function restoreLabel(button) {
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
    const action = button.dataset.action;

    if (action === "delete" && button.dataset.confirm !== "1") {
      confirmOrRun(button);
      return;
    }

    button.disabled = true;
    try {
      if (action === "delete") await deleteRow(row);
      else if (action === "pause") await setTaskPaused(row, true);
      else if (action === "resume") await setTaskPaused(row, false);
      else if (action === "retry") await retryRow(row);
      else if (action === "copy") await copyPath(row.save_path);
    } catch (err) {
      toast("操作失败：" + err.message, "error");
    } finally {
      button.disabled = false;
      if (action === "delete") {
        clearTimeout(Number(button.dataset.timer || 0));
        button.dataset.confirm = "0";
        restoreLabel(button);
      }
    }
  }

  function bindTable(table, source) {
    table.addEventListener("click", (event) => {
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

  function cfgId(prefix, name) {
    return prefix + "-" + name.replace(/\./g, "-");
  }

  function setConfigError(message) {
    setText(el.configError, message || "");
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

    wrap.innerHTML =
      '<label class="cfg-label" for="' +
      id +
      '">' +
      esc(field.label) +
      requiredMark +
      "</label>" +
      (isTextarea
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
          ">") +
      hintHtml +
      errorHtml;

    const control = wrap.querySelector(".cfg-input");
    control.value = value === undefined || value === null ? "" : String(value);
    return wrap;
  }

  function renderConfigForm(payload) {
    const fields = (payload && payload.fields) || [];
    const values = (payload && payload.values) || {};
    const groups = (payload && payload.groups) || [];
    el.configForm.innerHTML = "";

    groups.forEach((group) => {
      const groupFields = fields.filter((field) => field.group === group);
      if (!groupFields.length) return;

      const section = document.createElement("section");
      section.className = "cfg-group";
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
      el.configSave.disabled = false;
      const first = el.configForm.querySelector(".cfg-input, input, select, textarea");
      if (first) first.focus({ preventScroll: true });
    } catch (err) {
      setText(el.configLoading, "读取配置失败：" + err.message);
      setConfigError(err.message);
    }
  }

  function closeConfig() {
    if (typeof el.dialog.close === "function") el.dialog.close();
    else el.dialog.removeAttribute("open");
    if (el.configBtn) el.configBtn.focus();
  }

  function afterConfigSaved(res) {
    const applied = (res.applied || []).length;
    const restart = (res.restart_required || []).map(labelForConfigKey);
    if (res.text !== undefined) el.configText.value = res.text;
    if (res.values) {
      renderConfigForm({ fields: configSchema.fields, groups: configSchema.groups, values: res.values });
    }
    toast(
      "配置已保存。" +
        (applied ? "已生效 " + applied + " 项。" : "") +
        (restart.length ? "需要重启：" + restart.join("、") : ""),
      "success"
    );
    closeConfig();
    lastTabFetch = 0;
    refreshActive().catch(() => {});
  }

  async function saveConfig() {
    if (configSaving) return;
    configSaving = true;
    setConfigError("");
    clearFieldErrors();
    el.configSave.disabled = true;
    el.configSave.textContent = "保存中…";

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
      el.configSave.disabled = false;
      el.configSave.textContent = "保存";
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
  let lastFailedFetch = 0;
  let lastCounterFetch = 0;
  let errorCount = 0;

  function notifyError(err) {
    errorCount += 1;
    if (errorCount === 1 || errorCount % 15 === 0) {
      setText(el.updated, "连接异常：" + err.message);
    }
  }

  function tick() {
    if (document.hidden) return;
    const now = Date.now();

    loadStatus()
      .then(() => {
        errorCount = 0;
      })
      .catch(notifyError);

    const interval = TAB_REFRESH[state.tab] || 1000;
    if (now - lastTabFetch >= interval) {
      lastTabFetch = now;
      refreshActive().catch(notifyError);
    }

    if (now - lastFailedFetch >= FAILED_BADGE_REFRESH) {
      lastFailedFetch = now;
      loadFailed().catch(() => {});
    }

    if (
      state.tab !== "downloading" &&
      now - lastCounterFetch >= COUNTER_REFRESH
    ) {
      lastCounterFetch = now;
      loadDownloading().catch(() => {});
    }
  }

  /* ------------------------------------------------------------------ init */

  function bind() {
    const tabs = $$(".tab");
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

    el.pauseBtn.addEventListener("click", togglePause);

    bindTable(el.tables.downloading, () => state.downloading);
    bindTable(el.tables.downloaded, () => state.downloaded);
    bindTable(el.tables.failed, () => state.failed);

    if (el.configBtn) el.configBtn.addEventListener("click", openConfig);
    if (el.configClose) el.configClose.addEventListener("click", closeConfig);
    if (el.configCancel) el.configCancel.addEventListener("click", closeConfig);
    if (el.configSave) el.configSave.addEventListener("click", saveConfig);

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
        closeConfig();
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
      if (!document.hidden) {
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
    renderSpeed("--");

    const initial = (location.hash || "").replace("#", "");
    if (el.tables[initial]) state.tab = initial;
    paintTab();
    renderEmpty();

    loadStatus().catch(notifyError);
    loadFailed().catch(() => {});
    loadDownloaded().catch(() => {});
    if (state.tab !== "downloading") loadDownloading().catch(() => {});
    refreshActive().catch(notifyError);
    checkVersion();

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
