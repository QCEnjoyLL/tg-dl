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
    downloading: [],
    downloaded: [],
    failed: [],
  };

  const el = {
    speed: $("#speed"),
    statActive: $("#stat-active"),
    statDone: $("#stat-done"),
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

    const speed = tr.querySelector(".cell-speed");
    const value = row.download_speed === undefined ? "" : String(row.download_speed);
    if (speed && speed.textContent !== value) speed.textContent = value;

    const pauseBtn = tr.querySelector('[data-action="pause"]');
    if (pauseBtn) {
      const paused = Boolean(row.paused);
      pauseBtn.dataset.action = paused ? "resume" : "pause";
      pauseBtn.dataset.tone = paused ? "warn" : "";
      pauseBtn.innerHTML =
        (paused ? ICON_PLAY : ICON_PAUSE) + "<span>" + (paused ? "继续" : "暂停") + "</span>";
      pauseBtn.setAttribute(
        "aria-label",
        (paused ? "继续下载 " : "暂停下载 ") + row.filename
      );
    }

    const deleteBtn = tr.querySelector('[data-action="delete"]');
    if (deleteBtn) deleteBtn.setAttribute("aria-label", "删除任务 " + row.filename);
  }

  function buildDownloadedRow(row) {
    const tr = document.createElement("tr");
    const uploadChip = row.upload_success
      ? '<span class="state-chip is-uploaded">已上传</span>'
      : '<span class="state-chip">仅本地</span>';
    tr.innerHTML =
      idCell(row) +
      '<td class="cell-name" title="' +
      esc(row.filename) +
      '">' +
      nameHtml(row.filename) +
      " " +
      uploadChip +
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
      actionButton("copy", "复制路径", ICON_COPY) +
      actionButton("delete", "删除", ICON_TRASH, "danger") +
      "</div></td>";
    return tr;
  }

  function updateDownloadedRow(tr, row) {
    const deleteBtn = tr.querySelector('[data-action="delete"]');
    if (deleteBtn) deleteBtn.setAttribute("aria-label", "删除记录 " + row.filename);
    const copyBtn = tr.querySelector('[data-action="copy"]');
    if (copyBtn) copyBtn.setAttribute("aria-label", "复制保存路径 " + row.filename);
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
      '<td class="col-actions"><div class="row-actions">' +
      actionButton("retry", "重试", ICON_RETRY) +
      actionButton("delete", "删除", ICON_TRASH, "danger") +
      "</div></td>";
    return tr;
  }

  function updateFailedRow(tr, row) {
    const retryBtn = tr.querySelector('[data-action="retry"]');
    if (retryBtn) retryBtn.setAttribute("aria-label", "重试下载 " + row.filename);
    const deleteBtn = tr.querySelector('[data-action="delete"]');
    if (deleteBtn) deleteBtn.setAttribute("aria-label", "删除失败记录 " + row.filename);
  }

  function renderDownloading() {
    syncRows(
      el.tables.downloading.querySelector("tbody"),
      state.downloading,
      rowKey,
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
      rowKey,
      buildDownloadedRow,
      updateDownloadedRow
    );
    el.statDone.textContent = state.downloaded.length;
    renderEmpty();
  }

  function renderFailed() {
    syncRows(
      el.tables.failed.querySelector("tbody"),
      state.failed,
      rowKey,
      buildFailedRow,
      updateFailedRow
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
    el.pauseBtn.innerHTML =
      (running ? ICON_PAUSE : ICON_PLAY) +
      '<span class="pause-label">' +
      (running ? "暂停" : "继续") +
      "</span>";
    el.pauseBtn.setAttribute("aria-label", running ? "暂停全部下载" : "继续全部下载");
  }

  function renderSpeed(speed) {
    const text = speed || "--";
    if (el.speed) el.speed.textContent = text;
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
  function confirmOrRun(button, run) {
    if (button.dataset.confirm === "1") {
      clearTimeout(Number(button.dataset.timer || 0));
      button.dataset.confirm = "0";
      restoreLabel(button);
      run();
      return;
    }

    button.dataset.confirm = "1";
    button.dataset.previousHtml = button.innerHTML;
    button.innerHTML = ICON_CHECK + "<span>确认删除</span>";
    const previousLabel = button.getAttribute("aria-label") || "";
    button.dataset.previousAria = previousLabel;
    button.setAttribute("aria-label", "确认删除？再点一次执行");

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
      button.setAttribute("aria-label", button.dataset.previousAria);
      delete button.dataset.previousAria;
    }
  }

  async function handleRowAction(button, row) {
    const action = button.dataset.action;

    if (action === "delete" && button.dataset.confirm !== "1") {
      confirmOrRun(button, () => {
        button.disabled = true;
        deleteRow(row).catch((err) => toast("操作失败：" + err.message, "error"))
          .finally(() => {
            button.disabled = false;
          });
      });
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

  function setConfigError(message) {
    el.configError.textContent = message || "";
  }

  async function openConfig() {
    setConfigError("");
    el.configSave.disabled = true;
    el.configText.value = "正在读取 config.yaml…";
    if (typeof el.dialog.showModal === "function") {
      el.dialog.showModal();
    } else {
      el.dialog.setAttribute("open", "");
    }
    try {
      const res = await api("/get_config");
      if (!res || res.code !== "1") throw new Error((res && res.msg) || "读取失败");
      el.configPath.textContent = res.path;
      el.configText.value = res.text;
      el.configText.scrollTop = 0;
      el.configText.focus({ preventScroll: true });
      el.configText.setSelectionRange(0, 0);
      el.configText.scrollTop = 0;
      el.configSave.disabled = false;
    } catch (err) {
      el.configText.value = "";
      setConfigError("读取配置失败：" + err.message);
    }
  }

  function closeConfig() {
    if (typeof el.dialog.close === "function") el.dialog.close();
    else el.dialog.removeAttribute("open");
    if (el.configBtn) el.configBtn.focus();
  }

  async function saveConfig() {
    if (configSaving) return;
    configSaving = true;
    setConfigError("");
    el.configSave.disabled = true;
    el.configSave.textContent = "保存中…";

    try {
      const body = new URLSearchParams();
      body.set("text", el.configText.value);
      const res = await api("/set_config", { method: "POST", body: body });
      if (!res || res.code !== "1") {
        throw new Error((res && res.msg) || "保存失败");
      }
      const applied = (res.applied || []).length;
      const restart = res.restart_required || [];
      toast(
        "配置已保存。" +
          (applied ? "已生效 " + applied + " 项。" : "") +
          (restart.length ? "需要重启：" + restart.join(", ") : ""),
        "success"
      );
      closeConfig();
      lastTabFetch = 0;
      refreshActive().catch(() => {});
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

  function boot() {
    initTheme();
    bind();
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

    setInterval(tick, STATUS_REFRESH);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
