// -*- coding: utf-8 -*-
// 左栏第七张常驻卡「历史运行」（REQ-13 / REQ-14）。
// 只消费 api.listRuns() / getRunDetail()，不参与 gate() 门控、不改离线 A~F 任何既有行为。
// 三态：加载中 / 空态 / 错误态（带重试）；列表分页「加载更多」；点开详情 → 参数快照 + 产物 + 载入结果区。

import * as api from './api.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

const PAGE_SIZE = 20;

const H = {
  items: [], total: 0, busy: false, error: '',
  expanded: '', detail: null,
  onSelect: null, onLoad: null,
};

function notify(fn, detail) {
  if (typeof fn !== 'function') return;
  try { fn(detail); } catch (_) { /* 回调异常不外溢，保证卡片独立、零 JS 异常 */ }
}

function shortSource(s) {
  const str = String(s || '').trim();
  if (!str) return '—';
  return str.replace(/^minio:\/\/[^/]+\//, '').replace(/\/$/, '') || str;
}

function when(s) {
  const str = String(s || '').trim();
  return str ? str.slice(0, 16).replace('T', ' ') : '—';
}

function area(v) {
  const n = Number(v);
  return (Number.isFinite(n) ? n.toFixed(2) : '0.00') + ' ha';
}

function fmt(v, fallback = '—') {
  if (v == null || v === '') return fallback;
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

// ---------- 三态 ----------
function renderLoading(reset) {
  const box = $('hist-list');
  if (reset) box.innerHTML = '<div class="status-line"><span class="spinner"></span>正在加载历史运行…</div>';
  $('hist-err').textContent = '';
  $('hist-retry').hidden = true;
  const more = $('hist-more');
  more.disabled = true;
  more.textContent = reset ? '加载中…' : '正在加载更多…';
}

function renderEmpty() {
  $('hist-list').innerHTML = '<div class="empty-state compact"><p>暂无历史运行</p>'
    + '<p class="empty-sub">先在 A 卡应用 AOI 并完成一次变化检测，历史记录会出现在这里。</p></div>';
}

function renderError(msg) {
  $('hist-err').textContent = '历史运行加载失败：' + msg;
  $('hist-retry').hidden = false;
  $('hist-list').innerHTML = '<div class="empty-state compact"><p>暂时无法读取历史运行</p>'
    + '<p class="empty-sub">检查后端 /api/v1/runs 是否可用，然后点击「重试」。</p></div>';
  $('hist-count').textContent = '加载失败';
}

function updateMore() {
  const more = $('hist-more');
  if (H.busy) {
    more.disabled = true;
    more.textContent = H.items.length ? '正在加载更多…' : '加载中…';
    return;
  }
  if (H.items.length >= H.total) {
    more.disabled = true;
    more.textContent = '已加载全部 ' + H.total + ' 条';
  } else {
    more.disabled = false;
    more.textContent = '加载更多（已显示 ' + H.items.length + ' / 共 ' + H.total + ' 条）';
  }
}

// ---------- 列表 ----------
function renderList() {
  const box = $('hist-list');
  if (!H.items.length) { renderEmpty(); }
  else {
    box.innerHTML = H.items.map((it) => {
      const rid = String(it.run_id || '');
      const sel = H.expanded === rid ? ' selected' : '';
      const st = String(it.status || '—');
      const src = shortSource(it.t1_source || it.t2_source);
      return `
        <div class="hist-item${sel}" data-rid="${esc(rid)}" title="点击查看该 run 详情">
          <div class="hist-item-head">
            <code>${esc(rid.slice(0, 8))}</code>
            <span class="hist-name">${esc(it.aoi_name || 'AOI')}</span>
            <span class="hist-status st-${esc(st)}">${esc(st)}</span>
          </div>
          <div class="hist-row"><span class="hist-k">时相</span><span>${esc(it.t1 || '—')} → ${esc(it.t2 || '—')}</span></div>
          <div class="hist-row"><span class="hist-k">source</span><span class="hist-src" title="${esc(it.t1_source || it.t2_source || '')}">${esc(src)}</span></div>
          <div class="hist-row"><span class="hist-k">创建时间</span><span>${esc(when(it.created_at))}</span></div>
          <div class="hist-row"><span class="hist-k">三类面积合计</span><span>${esc(area(it.total_area_ha))}</span></div>
        </div>`;
    }).join('');
  }
  $('hist-count').textContent = '共 ' + H.total + ' 条 · 已显示 ' + H.items.length + ' 条';
  updateMore();
}

async function loadPage(reset) {
  if (H.busy) return;
  H.busy = true;
  const offset = reset ? 0 : H.items.length;
  renderLoading(reset);
  try {
    const data = await api.listRuns({ limit: PAGE_SIZE, offset });
    const items = Array.isArray(data && data.items) ? data.items : [];
    H.total = Number(data && data.total) || 0;
    H.items = reset ? items : H.items.concat(items);
    H.error = '';
    renderList();
  } catch (e) {
    H.error = (e && e.message) || '未知原因';
    if (reset) { H.items = []; H.total = 0; }
    renderError(H.error);
    updateMore();
  } finally {
    H.busy = false;
    updateMore();
  }
}

// ---------- 详情 ----------
function renderDetail(d) {
  const snap = (d && d.params_snapshot) || {};
  const arts = Array.isArray(d && d.artifacts) ? d.artifacts : [];
  const rid = String((d && d.run_id) || '');
  const rows = [
    ['run_id', rid || '—', true],
    ['辐射定标公式', fmt(snap.formula || snap.formula_text || snap.formula_id), true],
    ['变化阈值', fmt(snap.threshold != null ? snap.threshold : d.threshold), false],
    ['threshold_source', fmt(snap.threshold_source || d.threshold_source), true],
    ['valid_ratio', snap.valid_ratio != null ? Number(snap.valid_ratio).toFixed(4) : '—', false],
    ['commit（代码版次）', fmt(snap.commit || snap.commit_hash), true],
    ['指数 / 分块尺寸', fmt(snap.index || d.index, 'NDVI') + ' / ' + fmt(snap.block_size) + ' px', false],
  ];
  const artHtml = arts.length ? arts.map((a) => {
    const name = String(a.name || '');
    const url = String(a.url || '');
    const isPng = /\.png$/i.test(name);
    const ext = (name.split('.').pop() || 'FILE').toUpperCase();
    const thumb = (isPng && /^\/api\//.test(url))
      ? '<img class="hist-art-img" src="' + esc(url) + '" alt="' + esc(name) + '" loading="lazy" />'
      : '<span class="hist-art-icon">' + esc(ext) + '</span>';
    return `
      <div class="hist-art">
        <div class="hist-art-thumb">${thumb}</div>
        <div class="hist-art-name" title="${esc(name)}">${esc(name)}</div>
        <a class="hist-art-dl" href="${esc(url)}" download="${esc(name)}" target="_blank" rel="noopener">下载</a>
      </div>`;
  }).join('') : '<div class="empty-state compact"><p>该运行暂无产物</p><p class="empty-sub">旧运行可能未完成检测，可重新运行后查看。</p></div>';

  $('hist-detail').innerHTML = `
    <div class="hist-detail-head">
      <strong>运行详情 · <code>${esc(rid.slice(0, 8))}</code></strong>
      <button type="button" class="btn btn-sm btn-ghost" id="hist-detail-close">收起</button>
    </div>
    <div class="card-subtitle">参数快照<span class="readonly-badge">NF9 · 只读</span></div>
    <table class="tbl snapshot"><tbody>${rows.map(([k, v, code]) =>
      '<tr><td class="k">' + esc(k) + '</td><td>' + (code ? '<code>' + esc(v) + '</code>' : esc(v)) + '</td></tr>').join('')}</tbody></table>
    <div class="card-subtitle">产物<span class="hint">${arts.length} 个</span></div>
    <div class="hist-artifacts">${artHtml}</div>
    <button type="button" class="btn btn-primary btn-block gap-top" id="hist-load" data-rid="${esc(rid)}">载入到结果区</button>
    <div class="ok-line" id="hist-load-note" hidden></div>`;
}

async function selectRun(rid) {
  if (!rid) return;
  H.expanded = rid;
  H.detail = null;
  renderList();
  const box = $('hist-detail');
  box.hidden = false;
  box.innerHTML = '<div class="status-line"><span class="spinner"></span>正在加载运行详情…</div>';
  try {
    const d = await api.getRunDetail(rid);
    if (H.expanded !== rid) return; // 已切换到别的 run，丢弃过期回调
    H.detail = d;
    renderDetail(d);
    notify(H.onSelect, d);          // 双时相卷帘随当前展开的 run 切换
  } catch (e) {
    if (H.expanded !== rid) return;
    box.innerHTML = '<div class="err-line">运行详情加载失败：' + esc((e && e.message) || '未知原因') + '</div>'
      + '<button type="button" class="btn btn-sm btn-ghost" id="hist-detail-retry">重试</button>';
  }
}

function closeDetail() {
  H.expanded = '';
  H.detail = null;
  $('hist-detail').hidden = true;
  renderList();
}

function bind() {
  $('hist-list').addEventListener('click', (e) => {
    const item = e.target.closest && e.target.closest('.hist-item');
    if (item) selectRun(item.dataset.rid);
  });
  $('hist-more').onclick = () => loadPage(false);
  $('hist-refresh').onclick = () => { closeDetail(); loadPage(true); };
  $('hist-retry').onclick = () => loadPage(true);
  $('hist-detail').addEventListener('click', (e) => {
    if (e.target.closest && e.target.closest('#hist-detail-close')) { closeDetail(); return; }
    if (e.target.closest && e.target.closest('#hist-detail-retry')) { selectRun(H.expanded); return; }
    const load = e.target.closest && e.target.closest('#hist-load');
    if (load) {
      const note = $('hist-load-note');
      if (H.detail) {
        notify(H.onLoad, H.detail);
        if (note) { note.hidden = false; note.textContent = '✓ 已载入结果区 · run_id ' + String(H.detail.run_id || ''); }
      }
    }
  });
}

export function initHistory({ onSelect, onLoad } = {}) {
  H.onSelect = onSelect || null;
  H.onLoad = onLoad || null;
  bind();
  loadPage(true);
  return { refresh: () => loadPage(true) };
}
