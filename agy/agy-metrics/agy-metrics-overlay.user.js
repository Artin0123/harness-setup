// ==UserScript==
// @name         agy-metrics Remote Control 內嵌上下文監控
// @namespace    https://github.com/Artin0123/harness-setup
// @version      2.1.6
// @description  在 antigravity.google.com 對話框模型選擇器右側內嵌上下文進度環按鈕，即時顯示對話統計與 5h 額度消耗，支援手機橫向滑動、動態高度貼合標題欄與區網主機 IP 連線（連接本機或區網 agy-metrics；支援跨網域 iframe 與 Google Trusted Types）。
// @updateURL    https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy/agy-metrics/agy-metrics-overlay.user.js
// @downloadURL  https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy/agy-metrics/agy-metrics-overlay.user.js
// @match        https://antigravity.google.com/*
// @match        https://antigravity.static.usercontent.goog/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      127.0.0.1
// @connect      localhost
// @connect      *
// @run-at       document-idle
// ==/UserScript==

(() => {
  'use strict';

  const TOP_ORIGIN = 'https://antigravity.google.com';
  const IFRAME_ORIGIN = 'https://antigravity.static.usercontent.goog';

  // =========================================================================
  // ROLE 1: Top-level shell (antigravity.google.com)
  // Forwards clicks on the outer Google header bar so the iframe panel closes.
  // =========================================================================
  if (location.hostname === 'antigravity.google.com') {
    if (window.__agyMetricsBridgeInstalled) return;
    window.__agyMetricsBridgeInstalled = true;

    document.addEventListener('pointerdown', () => {
      for (const f of document.querySelectorAll('iframe')) {
        if (typeof f.src === 'string' && f.src.startsWith(IFRAME_ORIGIN)) {
          try { f.contentWindow?.postMessage({ type: 'agy-metrics:outside-click' }, IFRAME_ORIGIN); } catch {}
        }
      }
    }, true);
    return;
  }

  // =========================================================================
  // ROLE 2: Remote Control UI iframe (antigravity.static.usercontent.goog)
  // =========================================================================
  const DEFAULT_PORT = 47891;
  const LOCAL_BASE = `http://127.0.0.1:${DEFAULT_PORT}`;
  const HOST_STORE_KEY = 'agy_metrics_custom_host';
  const PING_INTERVAL_MS = 1500;
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const MODEL_BTN_SEL = '[data-testid="model-selector-trigger"]';
  const CONV_PATH_RE = /\/c\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;
  const TRIGGER_ID = 'agy-metrics-inline-trigger';
  const HOST_ID = 'agy-metrics-panel-host';
  const SCROLL_STYLE_ID = 'agy-metrics-scroll-style';

  if (document.getElementById(HOST_ID)) return;

  // Pure Tampermonkey storage
  const storeGet = (key, fallback = '') => {
    try { return String(GM_getValue(key, fallback) ?? fallback); } catch { return fallback; }
  };
  const storeSet = (key, val) => {
    try { GM_setValue(key, val); } catch {}
  };

  const normalizeHostToBase = (raw) => {
    let s = String(raw || '').trim().replace(/\/+$/, '');
    if (!s) return '';
    if (!/^https?:\/\//i.test(s)) s = 'http://' + s;
    try {
      const u = new URL(s);
      if (!u.port) u.port = String(DEFAULT_PORT);
      return u.origin;
    } catch {
      return '';
    }
  };

  const extractConvId = () => {
    const m = location.pathname.match(CONV_PATH_RE);
    return m ? m[1].toLowerCase() : '';
  };

  // Pure DOM helper — zero innerHTML so Google Trusted Types CSP never blocks it
  const h = (tag, props = null, ...children) => {
    const el = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v == null || v === false) continue;
        if (k === 'className') el.className = v;
        else if (k === 'style' && typeof v === 'string') el.style.cssText = v;
        else if (k === 'value') el.value = String(v);
        else if (k === 'disabled') el.disabled = Boolean(v);
        else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
        else el.setAttribute(k, String(v));
      }
    }
    for (const ch of children.flat()) {
      if (ch == null || ch === false) continue;
      el.appendChild(typeof ch === 'string' || typeof ch === 'number' ? document.createTextNode(String(ch)) : ch);
    }
    return el;
  };

  const svgEl = (tag, attrs = {}) => {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) {
      el.setAttribute(k, String(v));
    }
    return el;
  };

  const fmtTok = (n) => {
    const v = Number(n ?? 0);
    if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
    return String(Math.round(v));
  };

  const fmtSec = (s) => {
    if (s == null) return '—';
    const v = Math.max(0, Number(s));
    if (v < 60) return `${v.toFixed(2)}s`;
    const totalSec = Math.floor(v);
    if (totalSec < 3600) return `${Math.floor(totalSec / 60)}m${totalSec % 60}s`;
    const totalMin = Math.floor(totalSec / 60);
    return `${Math.floor(totalMin / 60)}h${totalMin % 60}m`;
  };

  const fmtAge = (ms) => {
    if (!ms) return '—';
    const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
    if (s < 60) return `${s} 秒前`;
    if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒前`;
    if (s < 86400) return `${Math.floor(s / 3600)} 時 ${Math.floor((s % 3600) / 60)} 分前`;
    return `${Math.floor(s / 86400)} 天 ${Math.floor((s % 86400) / 3600)} 時前`;
  };

  // Razor-thin scrollbar style for the model+plugin horizontal scroll container
  if (!document.getElementById(SCROLL_STYLE_ID)) {
    const scStyle = document.createElement('style');
    scStyle.id = SCROLL_STYLE_ID;
    scStyle.textContent = `
      [data-agy-scroll-row="1"] {
        overflow-x: auto !important;
        overflow-y: hidden !important;
        flex-wrap: nowrap !important;
        min-width: 0 !important;
        scrollbar-width: thin;
        scrollbar-color: rgba(148, 163, 184, 0.28) transparent;
        -webkit-overflow-scrolling: touch;
      }
      [data-agy-scroll-row="1"]::-webkit-scrollbar { height: 2px; }
      [data-agy-scroll-row="1"]::-webkit-scrollbar-track { background: transparent; }
      [data-agy-scroll-row="1"]::-webkit-scrollbar-thumb { background: rgba(148, 163, 184, 0.32); border-radius: 999px; }
    `;
    (document.head || document.documentElement).appendChild(scStyle);
  }

  // Shadow DOM host for popup panel
  const panelHost = document.createElement('div');
  panelHost.id = HOST_ID;
  panelHost.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none;';
  (document.body || document.documentElement).appendChild(panelHost);
  const shadow = panelHost.attachShadow({ mode: 'open' });

  const style = document.createElement('style');
  style.textContent = `
    :host { all: initial; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
    * { box-sizing: border-box; scrollbar-width: thin; scrollbar-color: rgba(148, 163, 184, 0.32) transparent; }
    *::-webkit-scrollbar { width: 4px; height: 4px; }
    *::-webkit-scrollbar-track { background: transparent; }
    *::-webkit-scrollbar-thumb { background: rgba(148, 163, 184, 0.32); border-radius: 999px; }
    *::-webkit-scrollbar-thumb:hover { background: rgba(148, 163, 184, 0.52); }

    .panel {
      position: fixed;
      pointer-events: auto;
      background: #0f1117;
      border: 1px solid #2b3244;
      border-radius: 12px;
      box-shadow: 0 18px 50px rgba(0, 0, 0, 0.72);
      padding: 14px 16px;
      display: none;
      flex-direction: column;
      gap: 12px;
      user-select: text;
      color: #e6e9f0;
      font-size: 14px;
      line-height: 1.45;
      font-variant-numeric: tabular-nums;
      overflow: hidden;
    }
    .panel.open { display: flex; }

    .ok { color: #3ddc97; }
    .warn { color: #f5b942; }
    .bad { color: #ff6b6b; }

    .summary-wrap {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      flex-shrink: 0;
    }
    .sub-card {
      background: #171a23;
      border: 1px solid #262b38;
      border-radius: 10px;
      padding: 11px 14px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      min-width: 0;
    }
    @media (max-width: 600px) {
      .summary-wrap {
        display: flex;
        flex-direction: column;
        gap: 8px;
        background: #171a23;
        border: 1px solid #262b38;
        border-radius: 10px;
        padding: 11px 14px;
      }
      .sub-card { display: contents; }
    }

    .kv {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 10px;
      font-size: 14px;
      line-height: 1.5;
      min-width: 0;
    }
    .kv-k { color: #b6bfd3; flex-shrink: 0; white-space: nowrap; }
    .kv-v { color: #e6e9f0; font-weight: 600; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }

    .table-box {
      flex: 1;
      min-height: 0;
      width: 100%;
      overflow-x: auto;
      overflow-y: hidden;
      border: 1px solid #262b38;
      border-radius: 10px;
      background: #171a23;
      display: flex;
      flex-direction: column;
    }
    .table-inner {
      min-width: 700px;
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
    }
    .table-head {
      flex-shrink: 0;
      overflow-y: hidden;
      scrollbar-gutter: stable;
      background: #1c202b;
      border-bottom: 1px solid #262b38;
    }
    .table-body {
      flex: 1;
      min-height: 48px;
      overflow-y: auto;
      scrollbar-gutter: stable;
    }
    .table-foot {
      flex-shrink: 0;
      overflow-y: hidden;
      scrollbar-gutter: stable;
      background: #1a1f2b;
      border-top: 1px solid #2f3649;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 14px;
      font-variant-numeric: tabular-nums;
    }
    th, td {
      padding: 9px 11px;
      text-align: left;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    th { color: #b6bfd3; font-weight: 600; }
    .table-body td { border-bottom: 1px solid #232836; color: #e6e9f0; }
    .table-body tbody tr:last-child td { border-bottom: none; }
    .table-foot td { font-weight: 700; color: #f1f5f9; }

    col:nth-child(1) { width: 10%; }
    col:nth-child(2) { width: 13%; }
    col:nth-child(3) { width: 10%; }
    col:nth-child(4) { width: 22%; }
    col:nth-child(5) { width: 11%; }
    col:nth-child(6) { width: 18%; }
    col:nth-child(7) { width: 16%; }

    .host-box {
      background: #171a23;
      border: 1px solid #262b38;
      border-radius: 10px;
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .host-title { font-size: 15px; font-weight: 600; color: #e6e9f0; }
    .host-form { display: flex; align-items: center; gap: 8px; }
    .host-input {
      flex: 1;
      min-width: 0;
      padding: 8px 12px;
      border-radius: 8px;
      border: 1px solid #2e3547;
      background: #0f1117;
      color: #e6e9f0;
      font-size: 14px;
      outline: none;
    }
    .host-input:focus { border-color: #6aa9ff; }
    .host-submit {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 38px;
      height: 38px;
      flex-shrink: 0;
      border-radius: 8px;
      border: 1px solid #2e3547;
      background: #1f293d;
      color: #e6e9f0;
      font-size: 16px;
      cursor: pointer;
    }
    .host-submit:hover { border-color: #6aa9ff; background: #263552; color: #ffffff; }
    .host-submit:disabled { opacity: 0.5; cursor: wait; }
    .host-msg { font-size: 14px; color: #fca5a5; }

    .err-banner {
      background: rgba(255, 107, 107, 0.14);
      border: 1px solid rgba(255, 107, 107, 0.4);
      color: #fecaca;
      border-radius: 8px;
      padding: 10px 12px;
      font-size: 14px;
      flex-shrink: 0;
    }
  `;
  shadow.appendChild(style);

  const panelEl = h('div', { className: 'panel' });
  shadow.appendChild(panelEl);

  // Inline trigger button matching native [data-testid="model-selector-trigger"]
  const RING_R = 5.8;
  const RING_CIRC = 2 * Math.PI * RING_R;
  const ringTrack = svgEl('circle', {
    cx: 8, cy: 8, r: RING_R,
    fill: 'none', stroke: 'currentColor', 'stroke-opacity': 0.28, 'stroke-width': 2,
  });
  const ringArc = svgEl('circle', {
    cx: 8, cy: 8, r: RING_R,
    fill: 'none', stroke: '#64748b', 'stroke-width': 2,
    'stroke-linecap': 'round',
    'stroke-dasharray': RING_CIRC.toFixed(2),
    'stroke-dashoffset': RING_CIRC.toFixed(2),
  });
  const ringSvg = svgEl('svg', {
    viewBox: '0 0 16 16', width: 14, height: 14, 'aria-hidden': 'true',
    style: 'display:block;flex-shrink:0;transform:rotate(-90deg);',
  });
  ringSvg.appendChild(ringTrack);
  ringSvg.appendChild(ringArc);

  const inlineLabelEl = h('span', { className: 'whitespace-nowrap', style: 'font-variant-numeric:tabular-nums;' }, '0.0%');
  const triggerBtn = h('button', {
    id: TRIGGER_ID,
    type: 'button',
    'data-no-drag': '',
    'aria-label': '上下文使用狀態與對話遙測',
    className: 'flex cursor-pointer items-center h-7 gap-1 rounded-full px-2 text-sm text-secondary-foreground select-none outline-none bg-transparent border-0 hover:bg-secondary transition-colors shrink-0',
  }, ringSvg, inlineLabelEl);

  // Connection & UI state
  let activeBase = '';
  let needsHostInput = false;
  let hostInputDraft = storeGet(HOST_STORE_KEY, '');
  let hostFeedback = '';
  let hostTesting = false;
  let autoConnecting = false;
  let lastHostPromptKey = '';
  let pollGen = 0;
  let pingTimer = null;
  let lastSigToken = '';

  let isOpen = false;
  let lastPathname = location.pathname;
  let currentConvId = extractConvId();
  let lastData = null;
  let lastMtimeMs = 0;
  let lastRenderKey = '';
  let fetchGen = 0;
  let observedComposer = null;

  const emptyActiveData = (cid = '', quota = null) => ({
    conversation: cid ? { id: cid } : null,
    metrics: null,
    quota: quota
      ? { ...quota, convDeltas: [], convTotalDeltaPct: null }
      : { convDeltas: [], convTotalDeltaPct: null },
  });

  const normalizeActiveData = (data) => {
    if (!currentConvId) return emptyActiveData('', data?.quota);
    const respCid = (data?.conversation?.id || '').toLowerCase();
    if (respCid !== currentConvId) return emptyActiveData(currentConvId, data?.quota);
    return data;
  };

  const pctClass = (pct) => (pct >= 80 ? 'bad' : pct >= 60 ? 'warn' : 'ok');
  const ctxColor = (pct) => (pct >= 80 ? '#ff6b6b' : pct >= 60 ? '#f5b942' : '#3ddc97');

  const setRing = (pct, color) => {
    const clamped = Math.min(100, Math.max(0, Number(pct) || 0));
    ringArc.setAttribute('stroke', color);
    ringArc.setAttribute('stroke-dashoffset', (RING_CIRC * (1 - clamped / 100)).toFixed(2));
  };

  const findComposerEl = () => {
    if (!triggerBtn.isConnected) return null;
    let cur = triggerBtn.parentElement;
    while (cur && cur !== document.body && cur !== document.documentElement) {
      const r = cur.getBoundingClientRect();
      if (r.width >= 260 && r.width <= window.innerWidth - 8) {
        const hasInput = cur.querySelector('textarea, [contenteditable="true"]');
        if (hasInput && r.height <= window.innerHeight * 0.65) return cur;
      }
      cur = cur.parentElement;
    }
    return null;
  };

  const getTopHeaderBottom = () => {
    let maxBottom = 0;
    for (const el of document.querySelectorAll('header, [role="banner"]')) {
      const r = el.getBoundingClientRect();
      if (r.top <= 8 && r.height >= 28 && r.height <= 96 && r.width >= window.innerWidth * 0.4) {
        if (r.bottom > maxBottom) maxBottom = r.bottom;
      }
    }
    return maxBottom > 0 ? maxBottom : 52;
  };

  const composerRO = new ResizeObserver(() => {
    if (isOpen) positionPanel();
  });

  const positionPanel = () => {
    if (!isOpen) return;
    const compEl = findComposerEl();
    if (compEl !== observedComposer) {
      if (observedComposer) composerRO.unobserve(observedComposer);
      observedComposer = compEl;
      if (observedComposer) composerRO.observe(observedComposer);
    }

    const topLimit = getTopHeaderBottom() + 8;
    if (compEl) {
      const compRect = compEl.getBoundingClientRect();
      const left = Math.max(8, compRect.left);
      const width = Math.min(compRect.width, window.innerWidth - left - 8);
      const bottom = Math.max(12, window.innerHeight - compRect.top + 8);
      const availH = Math.max(160, compRect.top - 8 - topLimit);
      panelEl.style.left = `${left}px`;
      panelEl.style.width = `${width}px`;
      panelEl.style.bottom = `${bottom}px`;
      panelEl.style.top = 'auto';
      panelEl.style.maxHeight = `${Math.min(availH, 680)}px`;
    } else {
      const availH = Math.max(160, window.innerHeight - 24 - topLimit);
      panelEl.style.left = '8px';
      panelEl.style.width = `${Math.max(280, window.innerWidth - 16)}px`;
      panelEl.style.bottom = '16px';
      panelEl.style.top = 'auto';
      panelEl.style.maxHeight = `${Math.min(availH, 680)}px`;
    }
  };

  const setOpen = (nextOpen) => {
    isOpen = nextOpen;
    panelEl.classList.toggle('open', isOpen);
    triggerBtn.classList.toggle('bg-secondary', isOpen);
    if (isOpen) {
      lastRenderKey = '';
      render();
      positionPanel();
    } else {
      composerRO.disconnect();
      observedComposer = null;
    }
  };

  triggerBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    setOpen(!isOpen);
  });

  document.addEventListener('pointerdown', (e) => {
    if (!isOpen) return;
    const path = e.composedPath ? e.composedPath() : [];
    if (path.includes(panelHost) || path.includes(triggerBtn)) return;
    setOpen(false);
  }, true);
  document.addEventListener('keydown', (e) => {
    if (isOpen && e.key === 'Escape') setOpen(false);
  });
  window.addEventListener('resize', () => {
    if (isOpen) positionPanel();
  });
  window.addEventListener('message', (e) => {
    if (e.origin === TOP_ORIGIN && e.data?.type === 'agy-metrics:outside-click' && isOpen) {
      setOpen(false);
    }
  });

  // Watch iframe's own /c/<uuid> route directly
  const checkIframeRoute = () => {
    if (location.pathname === lastPathname) return;
    lastPathname = location.pathname;
    const cid = extractConvId();
    if (cid !== currentConvId) {
      currentConvId = cid;
      if (!cid) {
        if (activeBase) {
          lastData = emptyActiveData('', lastData?.quota);
          lastRenderKey = '';
          render();
        }
      } else if (activeBase) {
        lastData = emptyActiveData(cid, lastData?.quota);
        lastRenderKey = '';
        render();
        fetchActive();
      }
    }
  };
  const wrapHistory = (fn) =>
    function (...args) {
      const ret = fn.apply(this, args);
      checkIframeRoute();
      return ret;
    };
  history.pushState = wrapHistory(history.pushState);
  history.replaceState = wrapHistory(history.replaceState);
  window.addEventListener('popstate', checkIframeRoute);
  window.addEventListener('hashchange', checkIframeRoute);
  if (window.navigation && typeof window.navigation.addEventListener === 'function') {
    window.navigation.addEventListener('navigatesuccess', checkIframeRoute);
  }

  // Mount inline next to model selector; ONLY model selector + triggerBtn scroll horizontally
  const mountInlineButton = () => {
    const modelBtn = document.querySelector(MODEL_BTN_SEL);
    if (!modelBtn) return false;
    const anchor = modelBtn.closest('.no-focus-agent-input') || modelBtn.parentElement;
    const container = anchor?.parentElement;
    if (!container) return false;

    if (triggerBtn.parentElement === container && triggerBtn.previousElementSibling === anchor) {
      return true;
    }

    if (container.previousElementSibling) {
      container.previousElementSibling.style.flexShrink = '0';
    }
    container.dataset.agyScrollRow = '1';
    anchor.style.flexShrink = '0';

    anchor.insertAdjacentElement('afterend', triggerBtn);
    if (isOpen) positionPanel();
    return true;
  };

  // Leading rAF mount throttle
  let mountRaf = 0;
  const scheduleMount = () => {
    if (mountRaf) return;
    mountRaf = requestAnimationFrame(() => {
      mountRaf = 0;
      if (!triggerBtn.isConnected) mountInlineButton();
    });
  };
  const mo = new MutationObserver(() => {
    checkIframeRoute();
    if (!triggerBtn.isConnected) scheduleMount();
  });
  mo.observe(document.body || document.documentElement, { childList: true, subtree: true });
  mountInlineButton();

  const kvRow = (label, valueNode, valueId = null) => {
    const valProps = { className: 'kv-v' };
    if (valueId) valProps.id = valueId;
    return h('div', { className: 'kv' },
      h('span', { className: 'kv-k' }, label),
      h('span', valProps, valueNode)
    );
  };

  const makeColgroup = () =>
    h('colgroup', null, h('col'), h('col'), h('col'), h('col'), h('col'), h('col'), h('col'));

  const gmGetJson = (url, timeout = 4000) =>
    new Promise((resolve) => {
      GM_xmlhttpRequest({
        method: 'GET',
        url,
        timeout,
        onload: (res) => {
          if (res.status >= 200 && res.status < 300) {
            try {
              resolve({ ok: true, data: JSON.parse(res.responseText) });
            } catch {
              resolve({ ok: false, error: '回傳格式錯誤' });
            }
          } else {
            resolve({ ok: false, error: `HTTP ${res.status}` });
          }
        },
        onerror: () => resolve({ ok: false, error: '無法連線到目標主機' }),
        ontimeout: () => resolve({ ok: false, error: '連線逾時' }),
      });
    });

  const buildActiveUrl = (base) =>
    currentConvId ? `${base}/api/active?id=${encodeURIComponent(currentConvId)}` : `${base}/api/active`;

  const submitCustomHost = async () => {
    const base = normalizeHostToBase(hostInputDraft);
    if (!base) {
      hostFeedback = '格式無效，請輸入正確的 IP 或主機位址（例如 192.168.1.100）';
      render();
      return;
    }
    hostTesting = true;
    hostFeedback = '連線測試中...';
    render();

    const fetchedConvId = currentConvId;
    const res = await gmGetJson(buildActiveUrl(base), 3500);
    hostTesting = false;
    if (res.ok) {
      storeSet(HOST_STORE_KEY, hostInputDraft.trim());
      activeBase = base;
      needsHostInput = false;
      hostFeedback = '';
      lastData = normalizeActiveData(res.data);
      lastSigToken = '';
      lastRenderKey = '';
      render();
      startPingLoop(++pollGen);
      if (currentConvId && currentConvId !== fetchedConvId) fetchActive();
    } else {
      hostFeedback = `無法連線到 ${base}（${res.error}），請確認電腦端已加 --lan 啟動 agy-metrics`;
      render();
    }
  };

  const renderHostPrompt = () => {
    const promptKey = `${hostTesting}|${hostFeedback}`;
    if (promptKey === lastHostPromptKey && panelEl.querySelector('.host-form')) {
      positionPanel();
      return;
    }
    lastHostPromptKey = promptKey;

    const inputEl = h('input', {
      type: 'text',
      className: 'host-input',
      placeholder: '例如 192.168.1.100 或 192.168.1.100:47891',
      value: hostInputDraft,
      disabled: hostTesting,
      onInput: (e) => { hostInputDraft = e.target.value; },
      onKeydown: (e) => {
        if (e.key === 'Enter' && !hostTesting) {
          e.preventDefault();
          submitCustomHost();
        }
      },
    });
    const submitBtn = h('button', {
      type: 'button',
      className: 'host-submit',
      title: '連線',
      disabled: hostTesting,
      onClick: () => submitCustomHost(),
    }, '→');

    const boxNodes = [
      h('div', { className: 'host-title' }, '請輸入要連線的主機 IP'),
      h('div', { className: 'host-form' }, inputEl, submitBtn),
    ];
    if (hostFeedback) boxNodes.push(h('div', { className: 'host-msg' }, hostFeedback));
    panelEl.replaceChildren(h('div', { className: 'host-box' }, ...boxNodes));
    positionPanel();
  };

  const render = () => {
    mountInlineButton();

    if (needsHostInput) {
      setRing(0, '#ff6b6b');
      inlineLabelEl.textContent = '離線';
      lastRenderKey = '';
      if (isOpen) renderHostPrompt();
      return;
    }

    if (!lastData) {
      setRing(0, '#64748b');
      inlineLabelEl.textContent = '連線中';
      lastRenderKey = '';
      if (isOpen) {
        lastHostPromptKey = '';
        panelEl.replaceChildren(h('div', { className: 'host-box' }, h('div', { className: 'host-title' }, '連線中...')));
        positionPanel();
      }
      return;
    }

    const m = lastData.metrics;
    const q = lastData.quota || {};
    const l = m?.latest;
    const t = m?.totals;
    const turns = m?.turns || [];
    const ctxPct = l && l.maxContextTokens && l.contextTokens != null ? (l.contextTokens / l.maxContextTokens) * 100 : 0;

    if (l && l.maxContextTokens) {
      setRing(ctxPct, ctxColor(ctxPct));
      inlineLabelEl.textContent = `${ctxPct.toFixed(1)}%`;
    } else {
      setRing(0, '#64748b');
      inlineLabelEl.textContent = '0.0%';
    }

    if (!isOpen) return;

    const convId = lastData.conversation?.id || '';
    const displayErr = q.error || '';
    const renderKey = `${convId}|${m?.mtimeMs || 0}|${t?.requests || 0}|${l?.model || ''}|${l?.contextTokens ?? ''}|${l?.maxContextTokens ?? ''}|${q.convTotalDeltaPct ?? ''}|${displayErr}`;
    if (renderKey === lastRenderKey) return;
    lastRenderKey = renderKey;
    lastHostPromptKey = '';

    lastMtimeMs = m?.mtimeMs || 0;
    const cachePct = t && t.totalIn ? (t.cachedIn / t.totalIn) * 100 : 0;

    const prevBodyEl = shadow.getElementById('agy-table-body');
    const prevBodyTop = prevBodyEl ? prevBodyEl.scrollTop : 0;
    const prevBoxEl = shadow.getElementById('agy-table-box');
    const prevBoxLeft = prevBoxEl ? prevBoxEl.scrollLeft : 0;

    const children = [];
    if (displayErr) children.push(h('div', { className: 'err-banner' }, displayErr));

    const convDeltaTotal = q.convTotalDeltaPct;

    children.push(
      h('div', { className: 'summary-wrap' },
        h('div', { className: 'sub-card' },
          kvRow('模型名稱', l?.model || '—'),
          kvRow('上下文', l ? h('span', null, h('span', { className: pctClass(ctxPct) }, fmtTok(l.contextTokens)), ` / ${l.maxContextTokens ? fmtTok(l.maxContextTokens) : '—'}`) : '—')
        ),
        h('div', { className: 'sub-card' },
          kvRow('上次請求', fmtAge(lastMtimeMs), 'agy-last-age'),
          kvRow('快取命中率', `${cachePct.toFixed(1)}%`)
        )
      )
    );

    const tableHead = h('div', { className: 'table-head' },
      h('table', null,
        makeColgroup(),
        h('thead', null,
          h('tr', null,
            h('th', null, '請求數'),
            h('th', null, '耗時'),
            h('th', null, 'TPS'),
            h('th', null, '輸入 (已快取)'),
            h('th', null, '輸出'),
            h('th', null, '額度扣減 (5h)'),
            h('th', null, '執行時間')
          )
        )
      )
    );

    const tableBody = h('div', { className: 'table-body', id: 'agy-table-body' },
      h('table', null,
        makeColgroup(),
        h('tbody', null,
          turns.length
            ? turns.map((tr) =>
                h('tr', null,
                  h('td', null, String(tr.requestCount)),
                  h('td', null, fmtSec(tr.durSum)),
                  h('td', null, tr.tps ? tr.tps.toFixed(1) : '—'),
                  h('td', null, `${fmtTok(tr.totalIn)} (${fmtTok(tr.cachedIn)})`),
                  h('td', null, fmtTok(tr.totalOut)),
                  h('td', { className: tr.quotaDeltaPct != null ? 'warn' : '' }, tr.quotaDeltaPct != null ? `${tr.quotaDeltaPct.toFixed(3)}%` : '—'),
                  h('td', null, tr.time || '—')
                )
              )
            : [h('tr', null, h('td', { colspan: '7' }, '尚無請求紀錄'))]
        )
      )
    );

    const tableFoot = h('div', { className: 'table-foot' },
      h('table', null,
        makeColgroup(),
        h('tbody', null,
          h('tr', null,
            h('td', null, t ? String(t.requests) : '0'),
            h('td', null, t ? fmtSec(t.durSum) : '—'),
            h('td', null, t && t.tps ? t.tps.toFixed(1) : '—'),
            h('td', null, t ? `${fmtTok(t.totalIn)} (${fmtTok(t.cachedIn)})` : '—'),
            h('td', null, t ? fmtTok(t.totalOut) : '—'),
            h('td', { className: convDeltaTotal != null ? 'warn' : '' }, convDeltaTotal != null ? `${convDeltaTotal.toFixed(3)}%` : '—'),
            h('td', null, `${turns.length} 輪對話`)
          )
        )
      )
    );

    children.push(
      h('div', { className: 'table-box', id: 'agy-table-box' },
        h('div', { className: 'table-inner' }, tableHead, tableBody, tableFoot)
      )
    );

    panelEl.replaceChildren(...children);
    const newBodyEl = shadow.getElementById('agy-table-body');
    if (newBodyEl) newBodyEl.scrollTop = prevBodyTop;
    const newBoxEl = shadow.getElementById('agy-table-box');
    if (newBoxEl) newBoxEl.scrollLeft = prevBoxLeft;
    positionPanel();
  };

  const stopPingLoop = () => {
    pollGen++;
    if (pingTimer) {
      clearInterval(pingTimer);
      pingTimer = null;
    }
  };

  const handleDisconnect = (reason) => {
    activeBase = '';
    lastData = null;
    needsHostInput = true;
    hostFeedback = reason || '連線中斷，請重新輸入主機 IP';
    stopPingLoop();
    render();
  };

  const fetchActive = async () => {
    if (!activeBase) return;
    checkIframeRoute();
    const gen = ++fetchGen;
    const baseSnapshot = activeBase;
    const res = await gmGetJson(buildActiveUrl(baseSnapshot), 4000);
    if (gen !== fetchGen) return;
    if (res.ok) {
      lastData = normalizeActiveData(res.data);
      needsHostInput = false;
      render();
    } else if (activeBase === baseSnapshot) {
      handleDisconnect(`無法連線到 ${baseSnapshot}（${res.error}）`);
    }
  };

  // Single clean update loop: <1ms /api/ping every 1.5s while tab is visible
  const startPingLoop = (gen) => {
    if (!activeBase || gen !== pollGen) return;
    if (pingTimer) clearInterval(pingTimer);
    let pingBusy = false;
    pingTimer = setInterval(async () => {
      if (gen !== pollGen || !activeBase || document.visibilityState !== 'visible' || pingBusy) return;
      checkIframeRoute();
      pingBusy = true;
      const baseSnap = activeBase;
      const res = await gmGetJson(`${baseSnap}/api/ping`, 2500);
      pingBusy = false;
      if (gen !== pollGen) return;
      if (res.ok && res.data?.token) {
        if (lastSigToken && res.data.token !== lastSigToken) {
          lastSigToken = res.data.token;
          fetchActive();
        } else {
          lastSigToken = res.data.token;
        }
      } else if (!res.ok && activeBase === baseSnap) {
        handleDisconnect(`與 ${baseSnap} 連線中斷`);
      }
    }, PING_INTERVAL_MS);
  };

  const autoConnect = async () => {
    if (autoConnecting) return;
    autoConnecting = true;
    try {
      const localConvId = currentConvId;
      const localRes = await gmGetJson(buildActiveUrl(LOCAL_BASE), 1800);
      if (localRes.ok) {
        activeBase = LOCAL_BASE;
        needsHostInput = false;
        hostFeedback = '';
        lastData = normalizeActiveData(localRes.data);
        lastSigToken = '';
        render();
        startPingLoop(++pollGen);
        if (currentConvId && currentConvId !== localConvId) fetchActive();
        return;
      }

      const savedRaw = storeGet(HOST_STORE_KEY, '');
      const savedBase = normalizeHostToBase(savedRaw);
      if (savedBase && savedBase !== LOCAL_BASE) {
        hostInputDraft = savedRaw;
        const savedConvId = currentConvId;
        const savedRes = await gmGetJson(buildActiveUrl(savedBase), 2500);
        if (savedRes.ok) {
          activeBase = savedBase;
          needsHostInput = false;
          hostFeedback = '';
          lastData = normalizeActiveData(savedRes.data);
          lastSigToken = '';
          render();
          startPingLoop(++pollGen);
          if (currentConvId && currentConvId !== savedConvId) fetchActive();
          return;
        }
        handleDisconnect(`無法連線到先前設定的 ${savedBase}（${savedRes.error}）`);
        return;
      }

      needsHostInput = true;
      hostFeedback = '';
      render();
    } finally {
      autoConnecting = false;
    }
  };

  // Lightweight 1s local tick updates relative age text and guards SPA route changes
  setInterval(() => {
    checkIframeRoute();
    if (!isOpen || !lastMtimeMs || needsHostInput) return;
    const el = shadow.getElementById('agy-last-age');
    if (el) el.textContent = fmtAge(lastMtimeMs);
  }, 1000);

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      if (activeBase) fetchActive();
      else if (!hostTesting && !autoConnecting) autoConnect();
    }
  });

  autoConnect();
})();
