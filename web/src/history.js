// -*- coding: utf-8 -*-
// 左栏第七张常驻卡「历史运行」（REQ-13 / REQ-14）。
// 只消费 api.listRuns() / getRunDetail()，不参与 gate() 门控、不改离线 A~F 任何既有行为。
// 三态：加载中 / 空态 / 错误态（带重试）；列表分页「加载更多」；点开详情 → 参数快照 + 产物 + 载入结果区。

import * as api from './api.js';
import { artifactObjectUrl, downloadArtifact } from './api.js';
import { renderCompareBar } from './charts.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

const PAGE_SIZE = 20;

const H = {
  items: [], total: 0, busy: false, error: '',
  // 批次 D（REQ-14）：筛选条件（GET /runs 的可选 q / index / days）；空串=不筛选
  filters: { q: '', index: '', days: '' },
  filterTimer: null, pendingReset: false,
  expanded: '', detail: null,
  onSelect: null, onLoad: null,
  // 批次 A：运行对比视图（两个下拉都来自 GET /runs，详情走 GET /runs/{id}）
  compare: { open: false, busy: false, seq: 0, a: '', b: '' },
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

// 创建时间：后端（Postgres timestamptz）统一返回 UTC ISO8601（带 +00:00），
// 这里必须解析成 Date 再按浏览器本地时区格式化，不能直接截字符串——
// 直接截前 16 位会把时区偏移丢掉，UTC 会被当成当地时间显示（差 8 小时）。
function when(s) {
  const str = String(s || '').trim();
  if (!str) return '—';
  // 纯日期（如影像拍摄日 2023-08-04）不做时区换算，原样返回
  if (/^\d{4}-\d{2}-\d{2}$/.test(str)) return str;
  // 无时区标记的裸时间按 UTC 解释（库列为 timestamptz，正常一定带偏移，此处仅兜底）
  const iso = /[Zz]$|[+-]\d{2}:?\d{2}$/.test(str) ? str : str + 'Z';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return str.slice(0, 16).replace('T', ' ');
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
    + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
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

// 批次 D11：指数按参数快照实际值展示（NDVI / NDWI / NDBI / NDMI 共 4 项）；
// 缺失时不臆断为 NDVI，统一显示 —，避免把新指数显示成 NDVI。
function showIndex(d) {
  const snap = (d && d.params_snapshot) || {};
  const v = snap.index || (d && d.index);
  const s = String(v == null ? '' : v).trim().toUpperCase();
  return s || '—';
}

// 批次 D11：运行详情「变化阈值」标签按实际指数动态化（Δ<指数>），无指数时退回中性标签。
function detailThresholdLabel(d) {
  const idx = showIndex(d);
  return idx === '—' ? '变化阈值' : '变化阈值 Δ' + idx;
}

// ---------- 批次 D（REQ-14）：筛选条 ⇄ /runs 查询参数 ----------
// 三个条件全部可选：缺省时不带该 query 参数，请求与历史版本完全一致。
// 筛选状态以 H.filters 为准（输入防抖 300ms 后才写入），保证「加载更多」翻页时
// 携带的条件与第一页完全一致，不会因输入框里的半成品关键词而错位。
function hasFilter() {
  return !!(H.filters.q || H.filters.index || H.filters.days);
}

function filterQuery() {
  const out = {};
  if (H.filters.q) out.q = H.filters.q;
  if (H.filters.index) out.index = H.filters.index;
  if (H.filters.days) out.days = Number(H.filters.days);
  return out;
}

function readFilters() {
  const qEl = $('hist-q'), iEl = $('hist-index'), dEl = $('hist-days');
  H.filters = {
    q: qEl ? String(qEl.value || '').trim().slice(0, 100) : '',   // 后端 q 上限 100 字符
    index: iEl ? String(iEl.value || '') : '',
    days: dEl ? String(dEl.value || '') : '',
  };
}

// 筛选变化：offset 归零 → 重新拉第一页（total 由后端按过滤后计数返回）
function applyFilter() {
  closeDetail();      // 条件变了，已展开的详情不再对应本列表
  loadPage(true);
}

// 关键词输入防抖 300ms；指数 / 时间下拉变化即时生效
function onFilterInput() {
  window.clearTimeout(H.filterTimer);
  H.filterTimer = window.setTimeout(() => { readFilters(); applyFilter(); }, 300);
}

function onFilterChange() {
  window.clearTimeout(H.filterTimer);
  readFilters();
  applyFilter();
}

function clearFilter() {
  window.clearTimeout(H.filterTimer);
  ['hist-q', 'hist-index', 'hist-days'].forEach((id) => { const el = $(id); if (el) el.value = ''; });
  H.filters = { q: '', index: '', days: '' };
  applyFilter();
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
  // 有筛选条件时的空态与「确实没有历史运行」区分，避免用户误以为数据丢了
  if (hasFilter()) {
    $('hist-list').innerHTML = '<div class="empty-state compact"><p>没有符合条件的历史运行</p>'
      + '<p class="empty-sub">换个关键词，或点「清空」复位指数与时间筛选后重试。</p></div>';
    return;
  }
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
  $('hist-count').textContent = '共 ' + H.total + ' 条'
    + (hasFilter() ? '（已筛选）' : '') + ' · 已显示 ' + H.items.length + ' 条';
  updateMore();
}

async function loadPage(reset) {
  // 请求在途时若筛选又变化，不丢条件：打标记，本次结束后再重拉第一页
  if (H.busy) {
    if (reset) H.pendingReset = true;
    return;
  }
  H.busy = true;
  const offset = reset ? 0 : H.items.length;
  renderLoading(reset);
  try {
    // reset=true：offset 归零重拉第一页；否则按累计 offset 追加。
    // 两种情况都携带当前筛选条件，保证翻页不丢条件（total 恒为过滤后计数）。
    const data = await api.listRuns({ limit: PAGE_SIZE, offset, ...filterQuery() });
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
    if (H.pendingReset) { H.pendingReset = false; loadPage(true); }
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
    [detailThresholdLabel(d), fmt(snap.threshold != null ? snap.threshold : d.threshold), false],
    ['threshold_source', fmt(snap.threshold_source || d.threshold_source), true],
    ['valid_ratio', snap.valid_ratio != null ? Number(snap.valid_ratio).toFixed(4) : '—', false],
    ['commit（代码版次）', fmt(snap.commit || snap.commit_hash), true],
    ['指数 / 分块尺寸', showIndex(d) + ' / ' + fmt(snap.block_size) + ' px', false],
  ];
  const artHtml = arts.length ? arts.map((a) => {
    const name = String(a.name || '');
    const url = String(a.url || '');
    const isPng = /\.png$/i.test(name);
    const ext = (name.split('.').pop() || 'FILE').toUpperCase();
    // 受保护产物端点不可直连（<img>/<a> 带不上 Authorization → 401）：
    // 缩略图先占位、由下方 artifactObjectUrl 异步取 blob 填充；下载走 downloadArtifact(Blob)。
    const thumb = (isPng && /^\/api\//.test(url))
      ? '<img class="hist-art-img" data-art-url="' + esc(url) + '" alt="' + esc(name) + '" />'
      : '<span class="hist-art-icon">' + esc(ext) + '</span>';
    return `
      <div class="hist-art">
        <div class="hist-art-thumb">${thumb}</div>
        <div class="hist-art-name" title="${esc(name)}">${esc(name)}</div>
        <button type="button" class="hist-art-dl" data-url="${esc(url)}" data-name="${esc(name)}">下载</button>
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

  // 缩略图异步填充（blob 通道，与产物图/导出同一「受保护端点不可直连」口径）；
  // 详情已切换到别的 run 时丢弃过期回调，防止串图。
  const gen = rid;
  const box = $('hist-detail');
  box.querySelectorAll('img.hist-art-img[data-art-url]').forEach(async (img) => {
    const objUrl = await artifactObjectUrl(img.dataset.artUrl);
    if (H.expanded !== gen || !img.isConnected) return;
    if (objUrl) {
      img.src = objUrl;
    } else {
      const ph = document.createElement('span');
      ph.className = 'hist-art-icon';
      ph.textContent = '加载失败';
      img.replaceWith(ph);
    }
  });
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

// ---------- 批次 A：运行对比（前端 only，无新增后端接口） ----------
// 两个下拉都取自 /runs 列表；分别 GET /runs/{id} 拿摘要 + params_snapshot，
// 渲染三类面积并排条形图与「阈值/方法/指数/分块尺寸/OA/Kappa」两列表格。
function runOptions(items, selected) {
  if (!items.length) return '<option value="">— 暂无可选运行 —</option>';
  return items.map((it) => {
    const rid = String(it.run_id || '');
    const label = String(it.aoi_name || 'AOI') + ' · ' + rid.slice(0, 8)
      + ' · ' + (it.t1 || '—');
    return '<option value="' + esc(rid) + '"' + (rid === selected ? ' selected' : '') + '>'
      + esc(label) + '</option>';
  }).join('');
}

function compareShell(items, err) {
  const box = $('hist-compare');
  if (!box) return;
  box.innerHTML = `
    <div class="hist-detail-head">
      <strong>运行对比 <span class="readonly-badge">REQ-14 · 只读</span></strong>
      <button type="button" class="btn btn-sm btn-ghost" id="hist-compare-close">关闭</button>
    </div>
    <div class="cmp-selects">
      <label>运行 A<select id="cmp-run-a">${runOptions(items, H.compare.a)}</select></label>
      <label>运行 B<select id="cmp-run-b">${runOptions(items, H.compare.b)}</select></label>
    </div>
    <div class="err-line" id="cmp-err">${esc(err || '')}</div>
    <div id="cmp-body"><div class="empty-state compact"><p>请选择两条历史运行</p></div></div>`;
}

function closeCompare() {
  if (!H.compare.open) return;
  H.compare.open = false;
  H.compare.seq += 1;
  $('hist-compare').hidden = true;
  $('hist-list').hidden = false;
  $('hist-more').hidden = false;
  $('hist-detail').hidden = !H.expanded;
  renderList(); // 关闭即恢复原列表 / 计数 / 详情态
}

function openCompare() {
  if (H.compare.open) return;
  H.compare.open = true;
  H.compare.seq += 1;
  $('hist-compare').hidden = false;
  ['hist-list', 'hist-more', 'hist-detail'].forEach((id) => { $(id).hidden = true; });
  $('hist-count').textContent = '对比模式';
  H.compare.a = H.items.length ? String(H.items[0].run_id || '') : '';
  H.compare.b = H.items.length > 1 ? String(H.items[1].run_id || '') : '';
  compareShell([]);
  const seq = H.compare.seq;
  // 第二个下拉（以及第一个）统一由 GET /runs 列表填充，取前 100 条
  api.listRuns({ limit: 100, offset: 0 })
    .then((data) => {
      if (seq !== H.compare.seq || !H.compare.open) return;
      const items = Array.isArray(data && data.items) ? data.items : [];
      if (!items.length) { compareShell([], '暂无历史运行可对比'); return; }
      if (!items.some((x) => String(x.run_id) === H.compare.a)) {
        H.compare.a = String(items[0].run_id || '');
      }
      if (!items.some((x) => String(x.run_id) === H.compare.b)) {
        H.compare.b = String((items[1] || items[0]).run_id || '');
      }
      compareShell(items);
      loadCompare();
    })
    .catch((e) => {
      if (seq !== H.compare.seq || !H.compare.open) return;
      compareShell([], '运行列表加载失败：' + ((e && e.message) || '未知原因'));
    });
}

function statsOf(d) {
  const snap = (d && d.params_snapshot) || {};
  return Array.isArray(snap.stats) ? snap.stats : [];
}

function num4(v) {
  if (v == null || v === '' || !Number.isFinite(Number(v))) return '—';
  return Number(v).toFixed(4);
}

// 对比卡 6 个字段：阈值 / 方法 / 指数 / 分块尺寸 / OA / Kappa
function compareFields(d) {
  const snap = (d && d.params_snapshot) || {};
  const acc = snap.accuracy || snap.accuracy_report || (d && d.accuracy) || {};
  return [
    ['变化阈值', fmt(snap.threshold != null ? snap.threshold : d.threshold)],
    ['检测方法', fmt(snap.method || d.method, 'otsu')],
    ['指数', showIndex(d)],
    ['分块尺寸 (px)', fmt(snap.block_size)],
    ['OA', num4(acc.overall_accuracy)],
    ['Kappa', num4(acc.kappa)],
  ];
}

function renderCompareCard(da, db) {
  const body = $('cmp-body');
  if (!body) return;
  const ridA = String((da && da.run_id) || H.compare.a);
  const ridB = String((db && db.run_id) || H.compare.b);
  const fa = compareFields(da), fb = compareFields(db);
  const rows = fa.map((it, i) => {
    const va = it[1], vb = fb[i] ? fb[i][1] : '—';
    return '<tr class="' + (String(va) !== String(vb) ? 'cmp-diff' : '') + '">'
      + '<td class="k">' + esc(it[0]) + '</td><td>' + esc(va) + '</td><td>' + esc(vb) + '</td></tr>';
  }).join('');
  body.innerHTML = `
    <div class="card-subtitle">三类面积对比<span class="hint">增强 / 退化 / 稳定</span></div>
    <div class="chart chart-compare" id="chart-compare"></div>
    <div class="card-subtitle">参数与精度<span class="hint">差异行高亮</span></div>
    <table class="tbl cmp-table">
      <thead><tr><th>字段</th><th>A ${esc(ridA.slice(0, 8))}</th><th>B ${esc(ridB.slice(0, 8))}</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="cmp-note">OA / Kappa 取自参数快照中的精度记录；未做样本评价的运行如实显示 —。</div>`;
  renderCompareBar($('chart-compare'), [
    { name: 'A ' + ridA.slice(0, 8), stats: statsOf(da) },
    { name: 'B ' + ridB.slice(0, 8), stats: statsOf(db) },
  ]);
}

async function loadCompare() {
  const body = $('cmp-body');
  // 连续切换下拉不丢弃请求：用 seq 只丢弃过期回调，最后一次选择必定生效
  if (!body || !H.compare.open) return;
  const a = $('cmp-run-a'), b = $('cmp-run-b');
  const ridA = a ? String(a.value || '') : H.compare.a;
  const ridB = b ? String(b.value || '') : H.compare.b;
  if (!ridA || !ridB) {
    body.innerHTML = '<div class="empty-state compact"><p>请选择两条历史运行</p></div>';
    return;
  }
  H.compare.busy = true;
  const seq = ++H.compare.seq;
  body.innerHTML = '<div class="status-line"><span class="spinner"></span>正在读取两条运行详情…</div>';
  try {
    const [da, db] = await Promise.all([api.getRunDetail(ridA), api.getRunDetail(ridB)]);
    if (seq !== H.compare.seq || !H.compare.open) return;
    renderCompareCard(da, db);
  } catch (e) {
    if (seq !== H.compare.seq || !H.compare.open) return;
    body.innerHTML = '<div class="err-line">对比数据加载失败：'
      + esc((e && e.message) || '未知原因') + '</div>';
  } finally {
    H.compare.busy = false;
  }
}

function bind() {
  // 批次 D（REQ-14）：筛选条路由——关键词防抖 300ms、下拉即时生效、清空一键复位
  const qEl = $('hist-q');
  if (qEl) qEl.addEventListener('input', onFilterInput);
  ['hist-index', 'hist-days'].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener('change', onFilterChange);
  });
  const clr = $('hist-filter-clear');
  if (clr) clr.onclick = clearFilter;

  $('hist-list').addEventListener('click', (e) => {
    const item = e.target.closest && e.target.closest('.hist-item');
    if (item) selectRun(item.dataset.rid);
  });
  $('hist-more').onclick = () => loadPage(false);
  $('hist-refresh').onclick = () => { closeCompare(); closeDetail(); loadPage(true); };
  $('hist-retry').onclick = () => { closeCompare(); loadPage(true); };
  $('hist-compare-btn').onclick = openCompare;
  $('hist-compare').addEventListener('click', (e) => {
    if (e.target.closest && e.target.closest('#hist-compare-close')) closeCompare();
  });
  $('hist-compare').addEventListener('change', (e) => {
    if (!e.target || !e.target.id) return;
    if (e.target.id === 'cmp-run-a') H.compare.a = String(e.target.value || '');
    else if (e.target.id === 'cmp-run-b') H.compare.b = String(e.target.value || '');
    else return;
    loadCompare();
  });
  $('hist-detail').addEventListener('click', (e) => {
    if (e.target.closest && e.target.closest('#hist-detail-close')) { closeDetail(); return; }
    if (e.target.closest && e.target.closest('#hist-detail-retry')) { selectRun(H.expanded); return; }
    const dl = e.target.closest && e.target.closest('.hist-art-dl');
    if (dl) {
      e.preventDefault();
      if (dl.disabled) return;
      dl.disabled = true;
      const original = dl.textContent;
      dl.textContent = '下载中…';
      downloadArtifact(dl.dataset.url, dl.dataset.name).then((ok) => {
        dl.textContent = ok ? '已下载' : '下载失败';
        window.setTimeout(() => { dl.textContent = original; dl.disabled = false; }, 1600);
      });
      return;
    }
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
