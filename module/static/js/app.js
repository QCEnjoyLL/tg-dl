/* tg-dl web ui — data polling, table rendering, theme switching */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // Polling intervals per tab (ms). The status pill polls every second.
  const TAB_REFRESH = { downloading: 1000, downloaded: 4000, failed: 3000 };
  const STATUS_REFRESH = 1000;
  const FAILED_BADGE_REFRESH = 10000;
  const DOWNLOADING_COUNTER_REFRESH = 10000;

  const ICON_PAUSE =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 5v14M15 5v14"/></svg>';
  const ICON_PLAY =
    '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M8 5.5v13l11-6.5z"/></svg>';

  const state = {
    tab: "downloading",
    action: $("#pause-btn") ? $("#pause-btn").dataset.value : "pause",
    downloading: [],
    downloaded: [],
    failed: [],
  };

  const el = {
    speed: $("#speed"),
    statSpeed: $("#stat-speed"),
    statActive: $("#stat-active"),
    statFailed: $("#stat-failed"),
    statState: $("#stat-state"),
    badgeDownloading: $("#badge-downloading"),
    badgeFailed: $("#badge-failed"),
    updated: $("#updated"),
    liveDot: $("#live-dot"),
    empty: $("#empty"),
    emptyText: $("#empty-text"),
    pauseBtn: $("#pause-btn"),
    themeBtn: $("#theme-toggle"),
    toasts: $("#toasts"),
    tables: {
      downloading: $("#table-downloading"),
      downloaded: $("#table-downloaded"),
      failed: $("#table-failed"),
    },
  };

  /* ------------------------------------------------------------------ utils */

  const ESCAPE_MAP = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  };

  function esc(value) {
    return String(value === null || value === undefined ? "" : value).replace(
      /[&<>"']/g,
      (c) => ESCAPE_MAP[c]
    );
  }

  // Dim the extension so long, similar names stay readable.
  function nameHtml(name) {
    const text = String(name === null || name === undefined ? "" : name);
    const dot = text.lastIndexOf(".");
    const hasExt = dot > 0 && text.length - dot <= 8;
    if (!hasExt) {
      return esc(text) || '<span class="ext">(unnamed)</span>';
    }
    return (
      esc(text.slice(0, dot)) + '<span class="ext">' + esc(text.slice(dot)) + "</span>"
    );
  }

  // Long chat ids do not fit the column; keep the tail, which is the part that
  // tells supergroups apart, and expose the full value in the tooltip.
  function chatLabel(chat) {
    const text = String(chat === null || chat === undefined ? "" : chat);
    return text.length > 12 ? "…" + text.slice(-8) : text;
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
      pad(d.getMinutes()) +
      ":" +
      pad(d.getSeconds())
    );
  }

  function toast(message, tone) {
    const node = document.createElement("div");
    node.className = "toast";
    node.dataset.tone = tone || "info";
    node.textContent = message;
    el.toasts.appendChild(node);
    setTimeout(() => {
      node.classList.add("is-leaving");
      setTimeout(() => node.remove(), 220);
    }, 3200);
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

  function buildDownloadingRow(row) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      idCell(row) +
      '<td class="cell-name" title="' +
      esc(row.filename) +
      '">' +
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
      "</td>";
    return tr;
  }

  function updateDownloadingRow(tr, row) {
    const pct = pctValue(row.download_progress);
    const fill = tr.querySelector(".bar > i");
    if (fill.style.width !== pct + "%") fill.style.width = pct + "%";
    const label = tr.querySelector(".pct");
    const text = pctText(pct);
    if (label.textContent !== text) label.textContent = text;

    const speed = tr.querySelector(".cell-speed");
    const value = row.download_speed === undefined ? "" : String(row.download_speed);
    if (speed.textContent !== value) speed.textContent = value;
  }

  function buildDownloadedRow(row) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      idCell(row) +
      '<td class="cell-name" title="' +
      esc(row.filename) +
      '">' +
      nameHtml(row.filename) +
      "</td>" +
      '<td class="cell-size">' +
      esc(row.total_size) +
      "</td>" +
      '<td class="cell-path" title="点击复制：' +
      esc(row.save_path) +
      '">' +
      esc(row.save_path) +
      "</td>";
    return tr;
  }

  function buildFailedRow(row) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      idCell(row) +
      '<td class="cell-name" title="' +
      esc(row.filename || row.reason) +
      '">' +
      nameHtml(row.filename) +
      "</td>" +
      '<td class="cell-time">' +
      timeText(row.time) +
      "</td>" +
      '<td class="col-actions">' +
      '<button class="btn-ghost" type="button" data-action="retry">重试</button>' +
      '<button class="btn-ghost" type="button" data-action="drop" data-tone="danger">删除</button>' +
      "</td>";
    return tr;
  }

  function renderDownloading() {
    syncRows(
      el.tables.downloading.querySelector("tbody"),
      state.downloading,
      (r) => r.chat + ":" + r.id,
      buildDownloadingRow,
      updateDownloadingRow
    );
    el.statActive.textContent = state.downloading.length;
    el.badgeDownloading.textContent = state.downloading.length;
    el.badgeDownloading.hidden = state.downloading.length === 0;
    renderEmpty();
  }

  function renderDownloaded() {
    syncRows(
      el.tables.downloaded.querySelector("tbody"),
      state.downloaded,
      (r) => r.chat + ":" + r.id,
      buildDownloadedRow,
      () => {}
    );
    renderEmpty();
  }

  function renderFailed() {
    syncRows(
      el.tables.failed.querySelector("tbody"),
      state.failed,
      (r) => r.chat + ":" + r.id,
      buildFailedRow,
      () => {}
    );
    renderEmpty();
  }

  function renderEmpty() {
    const messages = {
      downloading: "队列空闲，暂无正在下载的任务",
      downloaded: "本次运行还没有完成的下载",
      failed: "没有失败任务",
    };
    const rows =
      state.tab === "downloading"
        ? state.downloading
        : state.tab === "downloaded"
        ? state.downloaded
        : state.failed;
    el.empty.hidden = rows.length > 0;
    el.emptyText.textContent = messages[state.tab] || "";
  }

  function renderStatus() {
    const running = state.action === "pause";
    el.statState.textContent = running ? "运行中" : "已暂停";
    el.statState.classList.toggle("is-running", running);
    el.statState.classList.toggle("is-paused", !running);

    el.liveDot.classList.toggle("is-paused", !running);

    el.pauseBtn.dataset.value = state.action;
    el.pauseBtn.dataset.variant = running ? "pause" : "resume";
    el.pauseBtn.innerHTML =
      (running ? ICON_PAUSE : ICON_PLAY) +
      '<span class="pause-label">' +
      (running ? "暂停" : "继续") +
      "</span>";
  }

  function renderSpeed(speed) {
    const text = speed || "--";
    el.speed.textContent = text;
    el.statSpeed.textContent = text;
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
    if (state.tab === "downloaded") renderDownloaded();
  }

  async function loadFailed() {
    const rows = await api("/get_failed_list");
    state.failed = Array.isArray(rows) ? rows : [];
    el.statFailed.textContent = state.failed.length;
    el.badgeFailed.textContent = state.failed.length;
    el.badgeFailed.hidden = state.failed.length === 0;
    if (state.tab === "failed") renderFailed();
  }

  async function loadStatus() {
    const res = await api("/get_download_status");
    renderSpeed(res && res.download_speed ? res.download_speed : "");
    el.updated.textContent = "更新于 " + clockText();
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

  function setTab(tab) {
    if (!el.tables[tab]) return;
    state.tab = tab;

    paintTab();
    // Paint whatever we already have, then refresh from the server.
    renderCachedTab();

    lastTabFetch = 0;
    refreshActive().catch(notifyError);

    try {
      history.replaceState(null, "", "#" + tab);
    } catch (e) {
      /* file:// or old browsers */
    }
  }

  /* ----------------------------------------------------------------- theme */

  function currentTheme() {
    return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const label = theme === "dark" ? "切换到浅色主题" : "切换到深色主题";
    el.themeBtn.setAttribute("aria-label", label);
    el.themeBtn.title = label;
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

    el.themeBtn.addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        localStorage.setItem("tgdl-theme", next);
      } catch (e) {
        /* private mode: the theme just will not persist */
      }
    });

    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (event) => {
      if (storedTheme()) return; // an explicit choice wins
      applyTheme(event.matches ? "dark" : "light");
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
      toast(state.action === "continue" ? "已暂停下载队列" : "已恢复下载", "success");
      lastTabFetch = 0;
      await refreshActive();
    } catch (err) {
      toast("操作失败：" + err.message, "error");
    } finally {
      el.pauseBtn.disabled = false;
    }
  }

  async function handleRowAction(button, row) {
    const action = button.dataset.action;
    const params =
      "chat_id=" +
      encodeURIComponent(row.chat) +
      "&message_id=" +
      encodeURIComponent(row.id);
    const url =
      action === "retry" ? "/retry_failed?" + params : "/clear_failed?" + params;

    button.disabled = true;
    try {
      const res = await api(url, { method: "POST" });
      if (!res || res.code !== "1") throw new Error((res && res.msg) || "未知错误");
      toast(action === "retry" ? "已重新加入下载队列" : "已删除该失败记录", "success");
      await loadFailed();
      if (action === "retry" && state.tab === "downloading") {
        await loadDownloading().catch(() => {});
      }
    } catch (err) {
      toast("操作失败：" + err.message, "error");
    } finally {
      button.disabled = false;
    }
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
      toast("复制失败，请手动选择", "error");
    }
  }

  /* ------------------------------------------------------------------ loop */

  let lastTabFetch = 0;
  let lastFailedFetch = 0;
  let lastCounterFetch = 0;
  let errorCount = 0;

  function notifyError(err) {
    errorCount += 1;
    if (errorCount === 1 || errorCount % 15 === 0) {
      el.updated.textContent = "连接异常：" + err.message;
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

    // Keep the "下载中" counter fresh even while another tab is open.
    if (
      state.tab !== "downloading" &&
      now - lastCounterFetch >= DOWNLOADING_COUNTER_REFRESH
    ) {
      lastCounterFetch = now;
      loadDownloading().catch(() => {});
    }
  }

  /* ------------------------------------------------------------------ init */

  function bind() {
    $$(".tab").forEach((btn) =>
      btn.addEventListener("click", () => setTab(btn.dataset.tab))
    );

    el.pauseBtn.addEventListener("click", togglePause);

    el.tables.failed.addEventListener("click", (event) => {
      const button = event.target.closest(".btn-ghost");
      if (!button) return;
      const row = button.closest("tr");
      const source = state.failed.find(
        (item) => item.chat + ":" + item.id === row.dataset.key
      );
      if (source) handleRowAction(button, source);
    });

    el.tables.downloaded.addEventListener("click", (event) => {
      const cell = event.target.closest(".cell-path");
      if (cell) copyPath(cell.textContent.trim());
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        lastTabFetch = 0;
        tick();
      }
    });
  }

  function boot() {
    initTheme();
    bind();
    renderStatus();
    renderSpeed("--");

    // Allow deep links such as /#failed.
    const initial = (location.hash || "").replace("#", "");
    if (el.tables[initial]) state.tab = initial;
    paintTab();
    renderEmpty();

    // First paint: fire everything in parallel.
    loadStatus().catch(notifyError);
    loadFailed().catch(() => {});
    loadDownloaded().catch(() => {});
    if (state.tab !== "downloading") loadDownloading().catch(() => {});
    refreshActive().catch(notifyError);

    setInterval(tick, STATUS_REFRESH);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
