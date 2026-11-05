// -*- coding: utf-8 -*-
// 表现层主控：GIS 工作台式三栏布局 · 自由流程（无步骤条）。
// 左栏=分析配置（AOI / 影像对 / 检测运行），中央=地图（始终可交互），右栏=分析结果（检测 / 精度 / 导出）。
// 所有功能区常驻可见；仅当技术前提不满足时按钮置灰，并以 title / 提示文案说明缺什么。
// 仅调用 api.js 的 5 个接口；不做任何影像处理/算法/SQL；不直连 MinIO/PostGIS。
// 每个卡片/控件标注 REQ-ID，对齐 HLD 4.x 与追溯表；UI-01~05 功能语义不变（仅布局重组）。

import 'maplibre-gl/dist/maplibre-gl.css';
import './style.css';
import { ERROR_MESSAGES, errorMessage, showToast, showError, ApiError } from './errors.js';
import * as api from './api.js';
import * as demo from './demo.js';
import * as M from './map.js';
import * as C from './charts.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

const ALLOW_FORMATS = ['geotiff', 'png', 'pdf']; // constants.py EXPORT_FORMATS

const HINTS = {
  browse: '地图浏览模式：自由缩放平移，查看 AOI 预览与专题图叠加。',
  aoi: 'AOI 框选模式：按住鼠标拖拽画矩形（仅允许 1 个 AOI），或在左栏手填四至（WGS84）。',
  sample: '样本打点模式：点击地图空白处按当前类别打点；点击已有样本点即删除该点。',
};

// ===== 全局状态 =====
const S = {
  mapMode: 'aoi', // browse | aoi | sample
  aoi: null, aoiHistory: [],
  t1: '', t2: '',
  aoiResult: null, aoiBusy: false,
  pairs: [], pairVia: '', pairFilter: '', selectedPair: null,
  sceneMatch: null, sceneKey: '', sceneBusy: false,
  change: null, changePngUrl: null, changeBusy: false,
  opacity: 0.85,
  samples: [], sampleSeq: 0, curClass: 1, accuracy: null, accBusy: false,
  exportResult: null, exportBusy: false,
  csvBusy: false,
};

// 在线取数（可选）卡片独立状态，与离线 S 完全隔离，互不污染。
const ON = {
  degraded: false,       // 后端未启用在线取数（discover 裸 404 / 网络失败）
  discoverBusy: false,
  candidates: [],
  selected: -1,
  runId: '',
  fetching: false,       // POST /scene/fetch 已受理，轮询进行中
  fetchId: '',
  pollTimer: null,
  pollBusy: false,       // 避免单次 GET 未返回时重叠发请求
  pollFails: 0,
  terminal: '',          // '' | 'SUCCESS' | 'FAILED'
};

let map = null;

// ===== 通用工具 =====
function handleErr(e) {
  if (e instanceof ApiError) {
    showError({ code: e.code, message: e.message, run_id: e.run_id });
  } else {
    showToast('前端异常：' + (e && e.message ? e.message : String(e)), 'error');
    // eslint-disable-next-line no-console
    console.error(e);
  }
}

// 按钮技术前提门控：置灰 + title 说明缺什么（绝不隐藏、绝不强制跳转）
function gate(id, ok, reason) {
  const btn = $(id);
  if (!btn) return;
  btn.disabled = !ok;
  btn.title = ok ? '' : reason;
}

function currentRunId() {
  return (S.change && S.change.run_id)
    || (S.sceneMatch && S.sceneMatch.run_id)
    || (S.aoiResult && S.aoiResult.run_id) || '';
}

// 数据源状态徽标：真实后端 / 演示数据（沿用 is-demo 机制）
function setDataBadge(isDemo, viaLabel) {
  const b = $('conn-badge');
  document.body.classList.toggle('is-demo', !!isDemo);
  if (!b) return;
  if (isDemo) {
    b.textContent = '● 演示数据';
    b.className = 'badge badge-source badge-source-demo';
  } else {
    b.textContent = '● 真实后端' + (viaLabel ? ' · ' + viaLabel : '');
    b.className = 'badge badge-source badge-source-live';
  }
}

let demoMarked = false;
function markDemo() {
  const first = !document.body.classList.contains('is-demo');
  setDataBadge(true);
  const fw = $('fill-wrap'); if (fw) fw.hidden = false;
  if (first && !demoMarked) {
    demoMarked = true;
    showToast('后端接口不可用或返回空占位，已启用本地演示数据（页面已标注）', 'warn', 5200);
  }
}

function normBounds(a, b) {
  return {
    w: Math.min(a.lng, b.lng), e: Math.max(a.lng, b.lng),
    s: Math.min(a.lat, b.lat), n: Math.max(a.lat, b.lat),
  };
}

function aoiGeomString(b) {
  const ring = [[b.w, b.s], [b.e, b.s], [b.e, b.n], [b.w, b.n], [b.w, b.s]];
  return JSON.stringify({ type: 'Polygon', coordinates: [ring] });
}

function fmtCloud(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(2) + '%' : '—';
}

function pushAoiHistory() {
  if (S.aoi) S.aoiHistory.push({ ...S.aoi });
  if (S.aoiHistory.length > 20) S.aoiHistory.shift();
}

function boundsFromArray(a) { return { w: a[0], s: a[1], e: a[2], n: a[3] }; }

// ===== UI-01：AOI 设置（REQ-01） =====
function fillAoiInputs(b) {
  ['aoi-w', 'aoi-s', 'aoi-e', 'aoi-n'].forEach((id) => { $(id).value = ''; });
  if (!b) return;
  $('aoi-w').value = b.w.toFixed(4);
  $('aoi-s').value = b.s.toFixed(4);
  $('aoi-e').value = b.e.toFixed(4);
  $('aoi-n').value = b.n.toFixed(4);
}

function syncFromInputs() {
  const raw = ['aoi-w', 'aoi-s', 'aoi-e', 'aoi-n'].map((id) => $(id).value.trim());
  if (raw.some((r) => r === '')) { S.aoi = null; M.setAoi(map, null); updateAoiMetrics(); return; }
  const [w, s, e, n] = raw.map(Number);
  if ([w, s, e, n].some((v) => !Number.isFinite(v))) return;
  S.aoi = { w, s, e, n };
  M.setAoi(map, S.aoi);
  updateAoiMetrics();
}

function applyAoi(b) {
  S.aoi = { ...b };
  fillAoiInputs(S.aoi);
  M.setAoi(map, S.aoi);
  updateAoiMetrics();
}

function aoiStatus() {
  const b = S.aoi;
  if (!b) return { ok: false, spanOver: false, reason: '', legal: false, lon: 0, lat: 0 };
  const lon = b.e - b.w, lat = b.n - b.s;
  const legal = lon > 0 && lat > 0;
  const spanOver = lon > 0.25 + 1e-9 || lat > 0.25 + 1e-9;
  let reason = '';
  if (!legal) reason = '四至不合法：需满足 东 > 西 且 北 > 南';
  else if (spanOver) reason = '[EX1] ' + ERROR_MESSAGES.EX1;
  return { ok: legal && !spanOver, spanOver, legal, lon, lat, reason };
}

function phaseValid() {
  return !!(S.t1 && S.t2 && S.t2 > S.t1);
}

function phaseErrText() {
  if (S.t1 && S.t2 && S.t2 <= S.t1) return '时相 t2 必须晚于 t1';
  if (!S.t1 || !S.t2) return '请选满 t1 与 t2 两个时相（预置清单）';
  return '';
}

function updateAoiMetrics() {
  const b = S.aoi;
  const st = aoiStatus();
  const lon = b ? b.e - b.w : 0, lat = b ? b.n - b.s : 0;
  const deg2 = b ? lon * lat : 0;
  const km2 = deg2 * 111.32 * 111.32; // constants.py DEG_TO_KM
  $('span-lon').textContent = b ? lon.toFixed(4) + ' / ' + lat.toFixed(4) : '—';
  $('area-deg').innerHTML = b ? deg2.toFixed(6) + ' <small>deg²</small> · ' + km2.toFixed(2) + ' <small>km²</small>' : '—';

  $('aoi-err').textContent = st.ok ? '' : st.reason;
  ['aoi-w', 'aoi-s', 'aoi-e', 'aoi-n'].forEach((id) => $(id).classList.toggle('invalid', !!(b && (st.spanOver || !st.legal))));
  $('phase-err').textContent = phaseErrText();
  refreshGates();
  refreshOnlineAoiEcho(); // 在线取数卡片复用 A 卡 AOI 的实时回显（展示层附加，不改离线行为）
}

async function submitAoi() {
  const st = aoiStatus();
  if (!st.ok || !phaseValid()) { updateAoiMetrics(); return; }
  const body = { aoi_geom: aoiGeomString(S.aoi), t1: S.t1, t2: S.t2 };
  const btn = $('btn-aoi-apply');
  S.aoiBusy = true;
  btn.disabled = true; btn.textContent = '提交中…';
  try {
    const res = await api.selectAoi(body);
    if (res.demo) markDemo();
    else setDataBadge(false, 'POST /aoi/select');
    S.aoiResult = res.data;
    const applied = $('aoi-applied');
    applied.hidden = false;
    applied.textContent = '✓ AOI 已应用 · aoi_id ' + (res.data.aoi_id || '—')
      + (res.data.run_id ? ' · run_id ' + res.data.run_id : '');
    showToast(res.demo ? 'AOI 已提交（演示数据）' : 'AOI 已提交', 'ok');
    scheduleAutoConfirm();
  } catch (e) {
    handleErr(e);
  } finally {
    S.aoiBusy = false;
    btn.textContent = '应用 AOI · POST /aoi/select';
    refreshGates();
  }
}

// ===== UI-02：影像对（REQ-02 / REQ-04） =====
async function loadPairs() {
  const box = $('pair-list');
  box.innerHTML = '<div class="status-line"><span class="spinner"></span>正在获取影像对…</div>';
  try {
    const res = await api.listScenePairs(S.aoiResult && S.aoiResult.aoi_id);
    if (res.demo) markDemo();
    else setDataBadge(false, (res.via === 'GET' ? 'GET' : 'POST') + ' /scene/match');
    S.pairs = (res.data && res.data.pairs) || [];
    S.pairVia = res.via || '';
    const viaLabel = { GET: 'GET /scene/match', POST: 'POST /scene/match', SEED: '本地演示 seed' }[S.pairVia] || S.pairVia;
    $('pair-count').textContent = '共 ' + S.pairs.length + ' 组 · ' + viaLabel;
    renderPairs();
    autoSelectPair();
  } catch (e) {
    handleErr(e);
    box.innerHTML = '<div class="err-line">影像对获取失败，可稍后重试</div>';
  }
}

function pairMatchesFilter(p) {
  const q = S.pairFilter.trim().toLowerCase();
  if (!q) return true;
  const t1 = (p.scenes && p.scenes.t1) || {};
  const t2 = (p.scenes && p.scenes.t2) || {};
  return [p.label, p.tile, t1.date, t2.date, t1.cloud_cover, t2.cloud_cover, t1.scene_id, t2.scene_id]
    .some((v) => v != null && String(v).toLowerCase().includes(q));
}

function renderPairs() {
  const box = $('pair-list');
  const visible = S.pairs.map((p, i) => ({ p, i })).filter(({ p }) => pairMatchesFilter(p));
  if (!S.pairs.length) {
    box.innerHTML = '<div class="empty-state compact"><p>未获取到影像对</p><p class="empty-sub">请检查后端服务是否可用。</p></div>';
    return;
  }
  if (!visible.length) {
    box.innerHTML = '<div class="empty-state compact"><p>无匹配过滤条件的影像对</p><p class="empty-sub">尝试清空过滤框。</p></div>';
    return;
  }
  box.innerHTML = visible.map(({ p, i }) => {
    const t1 = (p.scenes && p.scenes.t1) || {};
    const t2 = (p.scenes && p.scenes.t2) || {};
    const hi1 = Number(t1.cloud_cover) > 50, hi2 = Number(t2.cloud_cover) > 50;
    const sel = S.selectedPair === p ? ' selected' : '';
    const sceneRow = (tag, sc, hi) => `
      <div class="pair-scene">
        <span class="tag ${tag === 't1' ? 'tag-t1' : 'tag-t2'}">${tag.toUpperCase()}</span>
        <span class="dt">${esc(sc.date || '—')}</span>
        <span class="cloud ${hi ? 'high' : ''}">云量 ${fmtCloud(sc.cloud_cover)}${hi ? ' ⚠' : ''}</span>
      </div>
      <div class="pair-meta">基线 ${esc(sc.proc_baseline || '—')} · 传感器 ${esc(sc.sensor || '—')}</div>`;
    return `
      <div class="pair-card${sel}" data-idx="${i}">
        <div class="pair-head"><span class="radio"></span><span class="pair-name">${esc(p.label || p.name || '影像对 ' + (i + 1))}</span><span class="pair-tile">${esc(p.tile || '—')}</span></div>
        ${sceneRow('t1', t1, hi1)}
        ${sceneRow('t2', t2, hi2)}
        ${(hi1 || hi2) ? '<div class="cloud-note">⚠ 云量 &gt; 50%，该时相影像质量偏低</div>' : ''}
      </div>`;
  }).join('');
  box.querySelectorAll('.pair-card').forEach((el) => {
    el.onclick = () => selectPair(+el.dataset.idx);
  });
}

function autoSelectPair() {
  // 依据 t1/t2 优先命中同一组的双时相；否则默认第一组
  let idx = 0;
  if (S.t1 && S.t2) {
    const hit = S.pairs.findIndex((p) => p.scenes && p.scenes.t1.date === S.t1 && p.scenes.t2.date === S.t2);
    if (hit >= 0) idx = hit;
  }
  selectPair(idx);
}

function selectPair(i) {
  S.selectedPair = S.pairs[i] || null;
  renderPairs();
  renderCaliber();
  refreshGates();
  scheduleAutoConfirm();
}

function renderCaliber() {
  const p = S.selectedPair;
  const panel = $('caliber-panel');
  if (!p) { panel.innerHTML = '<span class="hint">选择一组影像对后自动确认口径</span>'; return; }
  const m = S.sceneMatch || {};
  const baseline = m.proc_baseline || (p.scenes && p.scenes.t2 && p.scenes.t2.proc_baseline) || '05.09';
  panel.innerHTML = `
    <div class="k">公式标识</div><div class="v"><code>${esc(m.formula_id || 'S2_L2A_BOA')}</code></div>
    <div class="k">辐射定标公式</div><div class="v"><code>rho=DN/10000</code></div>
    <div class="k">口径说明</div><div class="v">AWS COG DN 已应用 BOA_ADD_OFFSET（偏移已在 DN 中修正）</div>
    <div class="k">传感器</div><div class="v">S2 · Sentinel-2 L2A</div>
    <div class="k">处理基线版次</div><div class="v">${esc(baseline)}（S2 需 ≥ 04.00，达标）</div>
    <div class="k">分块尺寸</div><div class="v">${esc(String(($('block-size') && $('block-size').value) || 256))} px</div>
    <div class="k">口径状态</div><div class="v caliber-ok">${S.sceneMatch ? '已锁定 · 不可编辑（NF5）' : '待确认（选择后自动提交）'}</div>`;
}

// 口径确认（REQ-04）：AOI 与影像对齐备后自动提交 POST /scene/match
let confirmTimer = null;
function scheduleAutoConfirm() {
  window.clearTimeout(confirmTimer);
  confirmTimer = window.setTimeout(autoConfirmScene, 260);
}

async function autoConfirmScene() {
  if (!S.aoiResult || !S.selectedPair || S.sceneBusy) return;
  const p = S.selectedPair;
  const key = [S.aoiResult.aoi_id, p.scenes.t1.scene_id, p.scenes.t2.scene_id, $('block-size').value].join('|');
  if (S.sceneKey === key && S.sceneMatch) { renderCaliber(); return; }
  S.sceneBusy = true;
  renderCaliber();
  const body = {
    aoi_id: S.aoiResult.aoi_id,
    scene_t1_id: (p.scenes && p.scenes.t1 && p.scenes.t1.scene_id) || '',
    scene_t2_id: (p.scenes && p.scenes.t2 && p.scenes.t2.scene_id) || '',
    block_size: Number($('block-size').value) || 256,
  };
  try {
    const res = await api.matchScene(body);
    if (res.demo) markDemo();
    else setDataBadge(false, 'POST /scene/match');
    S.sceneMatch = res.data;
    S.sceneKey = key;
    showToast(res.demo ? '影像对口径已确认（演示数据）' : '影像对口径已确认', 'ok');
  } catch (e) {
    handleErr(e);
    S.sceneKey = '';
    S.sceneMatch = null;
  } finally {
    S.sceneBusy = false;
    renderCaliber();
    refreshGates();
  }
}

// ===== UI-03：检测运行（REQ-05~08 / REQ-10） =====
// 后端产物 URL 为同源相对路径 /api/v1/artifacts/...；若旧版/异常后端仍返回
// file:///、Windows 绝对路径、minio:// 等浏览器不可加载地址，直接跳过 image source
// 叠加，避免 "Not allowed to load local resource" 与 MapLibre Failed to fetch。
function isUnsafeImageUrl(u) {
  return !u
    || /^(file|minio):\/\//i.test(u)   // file:///、minio://
    || /^[A-Za-z]:[\\/]/.test(u)       // C:\... / C:/...
    || /^\\\\/.test(u);                // UNC \\server\share
}

// ===== 批次 1（REQ-07）：C 卡指数切换 + 「高级选项」手动阈值 =====
// 开关关闭时 threshold 为 undefined：api.runChange 不会下发该字段，后端走 Otsu 自适应。
function runParams() {
  const checked = document.querySelector('input[name="index-type"]:checked');
  const index = (checked && String(checked.value).toUpperCase()) || 'NDVI';
  const manual = !!($('manual-threshold') && $('manual-threshold').checked);
  const threshold = manual ? Number($('threshold-slider').value) : undefined;
  return { index, threshold };
}

// 折叠区实时态：开关控制滑块可用性，滑块拖动实时显值（step 0.01 / -0.5~0.5）
function syncRunAdvancedUi() {
  const manual = !!($('manual-threshold') && $('manual-threshold').checked);
  const slider = $('threshold-slider');
  const v = Number((slider && slider.value) || 0);
  if (slider) slider.disabled = !manual;
  const valEl = $('advanced-threshold-val');
  if (valEl) valEl.textContent = (v >= 0 ? '+' : '') + v.toFixed(2);
  const mode = $('threshold-mode');
  if (mode) {
    mode.textContent = manual
      ? '当前：手动阈值 T=' + v.toFixed(2) + '（跳过 Otsu，threshold_source=manual）'
      : '当前：Otsu 自适应（请求不携带 threshold）';
  }
}

async function runChangeDetection() {
  if (S.changeBusy) return;
  if (!(S.aoiResult && S.selectedPair)) { refreshGates(); return; }
  const btn = $('btn-run-change');
  S.changeBusy = true;
  btn.disabled = true;
  btn.textContent = '运行中…';
  $('run-progress').hidden = false;
  const { index, threshold } = runParams();
  $('run-status').textContent = '正在运行变化检测（读取 COG → 掩膜 → Δ' + index
    + (threshold === undefined ? ' → Otsu 自适应阈值' : ' → 手动阈值 T=' + threshold.toFixed(2))
    + ' → 渲染专题图）…';
  $('run-done').hidden = true;
  const body = { run_id: currentRunId(), index, block_size: Number($('block-size').value) || 256 };
  if (threshold !== undefined) body.threshold = threshold;
  try {
    const res = await api.runChange(body);
    if (res.demo) markDemo();
    else setDataBadge(false, 'POST /result/change');
    S.change = res.data;
    let url = res.data.change_png;
    if (!url || /^demo:/.test(url)) url = demo.makeChangePngDataUrl();
    S.changePngUrl = url;
    if (!isUnsafeImageUrl(url)) {
      M.setImage(map, url, S.aoi);
      M.setImageOpacity(map, S.opacity);
    }
    $('map-legend').hidden = false;
    renderChangeResult();
    renderSnapshot();
    refreshExportState();
    $('run-done').hidden = false;
    $('run-done').textContent = '✓ 检测完成 · ' + index
      + (res.data.threshold_source ? ' · ' + res.data.threshold_source : '')
      + (res.data.threshold != null ? ' T=' + Number(res.data.threshold).toFixed(2) : '')
      + ' · run_id ' + res.data.run_id;
    showToast(res.demo ? '变化检测完成（演示数据）' : '变化检测完成', 'ok');
  } catch (e) {
    handleErr(e);
    $('run-status').textContent = '检测失败，可调整参数后重试';
  } finally {
    S.changeBusy = false;
    $('run-progress').hidden = true;
    btn.textContent = '运行变化检测 · POST /result/change';
    refreshGates();
  }
}

// ===== 右栏 D：检测结果（REQ-05/08/10/11） =====
function renderChangeResult() {
  const d = S.change;
  $('result-empty').hidden = !!d;
  $('result-body').hidden = !d;
  if (!d) return;
  renderValidRatio();
  $('threshold-val').textContent = d.threshold != null ? Number(d.threshold).toFixed(3) : '—';
  $('runid-val').textContent = d.run_id || '—';
  const stats = d.stats || [];
  const totalPx = stats.reduce((a, s) => a + Number(s.pixel_count || 0), 0);
  const names = { 1: '增强', 2: '退化', 3: '稳定' };
  const colors = { 1: '#21a04c', 2: '#d94c3d', 3: '#5b7a99' };
  let html = '<thead><tr><th>类别</th><th>像元数</th><th>面积 (ha)</th><th>占比</th></tr></thead><tbody>';
  for (const s of stats) {
    const c = Number(s.change_class);
    const pct = totalPx ? (Number(s.pixel_count || 0) / totalPx * 100).toFixed(1) + '%' : '—';
    html += `<tr><td><span class="sw-sm" style="background:${colors[c] || '#999'}"></span>${c} ${esc(names[c] || '—')}</td>`
      + `<td>${esc(s.pixel_count != null ? Number(s.pixel_count).toLocaleString() : '—')}</td>`
      + `<td>${esc(s.area_ha != null ? Number(s.area_ha).toFixed(1) : '—')}</td>`
      + `<td>${pct}</td></tr>`;
  }
  html += '</tbody>';
  $('stats-table').innerHTML = html;
  C.renderPie($('chart-pie'), stats);
  C.renderBar($('chart-bar'), stats);
}

function renderValidRatio() {
  const r = Number((S.change && S.change.valid_ratio) || 0);
  const v = $('valid-ratio'), note = $('valid-note');
  v.textContent = (r * 100).toFixed(1) + ' %';
  v.className = 'ratio-v';
  note.className = 'ratio-note';
  let ex5 = false;
  if (r >= 0.50) {
    v.classList.add('pass'); note.classList.add('pass');
    note.textContent = '✓ 有效像元占比合格（≥ 0.50），结论有效';
  } else if (r >= 0.10) {
    v.classList.add('warn'); note.classList.add('warn');
    note.textContent = '⚠ 有效数据偏低，结论仅供参考　[EX5] ' + ERROR_MESSAGES.EX5;
    ex5 = true;
  } else {
    v.classList.add('stop'); note.classList.add('stop');
    note.textContent = '⛔ 有效像元不足，本轮检测终止　[EX11] ' + ERROR_MESSAGES.EX11;
    showError({ code: 'EX11', message: ERROR_MESSAGES.EX11, run_id: S.change && S.change.run_id });
  }
  const bar = $('ex5-bar');
  const snapWarn = S.change && S.change.params_snapshot && S.change.params_snapshot.warning;
  if (ex5 || snapWarn) {
    bar.hidden = false;
    bar.classList.add('warn');
    bar.innerHTML = '<strong>[EX5] ' + esc(ERROR_MESSAGES.EX5) + '</strong>'
      + (snapWarn ? '<div class="alert-sub">' + esc(snapWarn) + '</div>' : '');
  } else {
    bar.hidden = true;
  }
}

// ===== 右栏 E：精度评估（REQ-09） =====
function updateCounts() {
  const cnt = { 1: 0, 2: 0, 3: 0 };
  S.samples.forEach((p) => { if (cnt[p.cls] != null) cnt[p.cls]++; });
  let allOk = true;
  [1, 2, 3].forEach((c) => {
    $('cnt-' + c).textContent = String(cnt[c]);
    $('cnt-' + c).classList.toggle('short', cnt[c] < demo.MIN_SAMPLES_PER_CLASS);
    $('bar-' + c).style.width = Math.min(100, (cnt[c] / demo.MIN_SAMPLES_PER_CLASS) * 100) + '%';
    if (cnt[c] < demo.MIN_SAMPLES_PER_CLASS) allOk = false;
  });
  const btn = $('btn-accuracy');
  btn.disabled = !allOk || S.accBusy;
  btn.title = allOk ? '' : '[EX10] ' + ERROR_MESSAGES.EX10;
  const hint = $('ex10-hint');
  hint.textContent = allOk
    ? '✓ 三类样本均已达到每类 30 个，可计算精度'
    : '[EX10] ' + ERROR_MESSAGES.EX10 + '（当前缺口见上方计数条）';
  $('acc-empty').hidden = S.samples.length > 0 || !!S.accuracy;
}

function addSample(lng, lat, cls) {
  S.samples.push({ id: 's' + (++S.sampleSeq), x: lng, y: lat, cls });
  M.setSamples(map, S.samples);
  updateCounts();
}

function removeSample(id) {
  S.samples = S.samples.filter((p) => p.id !== id);
  M.setSamples(map, S.samples);
  updateCounts();
}

function fillSamplesDemo() {
  const b = S.aoi;
  if (!b) { showToast('请先设置 AOI，再一键补齐演示样本', 'warn'); return; }
  // 每类 32 个：低 valid_ratio 组首 round 触发 EX10（<30 不可计算），可多次点击直至每类 ≥ 30
  [1, 2, 3].forEach((cls) => {
    for (let i = 0; i < 32; i++) {
      S.samples.push({
        id: 's' + (++S.sampleSeq),
        x: b.w + Math.random() * (b.e - b.w),
        y: b.s + Math.random() * (b.n - b.s),
        cls,
      });
    }
  });
  M.setSamples(map, S.samples);
  updateCounts();
  showToast('已补齐演示样本（每类 32 个，可继续点击补齐）', 'ok');
}

async function calcAccuracy() {
  if (S.accBusy) return;
  const samples = S.samples.map((p) => ({ x: p.x, y: p.y, ref_class: demo.CLASS_INFO[p.cls].ref }));
  const body = { run_id: currentRunId(), samples };
  const btn = $('btn-accuracy');
  S.accBusy = true;
  btn.disabled = true; btn.textContent = '计算中…';
  try {
    const res = await api.submitSamples(body);
    if (res.demo) markDemo();
    else setDataBadge(false, 'POST /accuracy/sample');
    S.accuracy = res.data;
    renderAccuracy();
    showToast(res.demo ? '精度评价完成（演示数据）' : '精度评价完成', 'ok');
  } catch (e) {
    handleErr(e);
  } finally {
    S.accBusy = false;
    btn.textContent = '计算精度 · POST /accuracy/sample';
    updateCounts();
  }
}

function renderAccuracy() {
  const a = S.accuracy;
  if (!a) return;
  $('accuracy-result').hidden = false;
  $('oa').textContent = (Number(a.overall_accuracy) * 100).toFixed(1) + ' %';
  $('kappa').textContent = Number(a.kappa).toFixed(3);
  $('acc-note').textContent = a.note ? esc(a.note) : '';
  const cm = a.confusion_matrix || [];
  let html = '<thead><tr><th>参考 \\ 预测</th><th>1 增强</th><th>2 退化</th><th>3 稳定</th></tr></thead><tbody>';
  for (let i = 0; i < 3; i++) {
    html += `<tr><td class="k">${i + 1} ${demo.CLASS_INFO[i + 1].name}</td>`;
    for (let j = 0; j < 3; j++) {
      const val = (cm[i] && cm[i][j] != null) ? cm[i][j] : 0;
      html += `<td class="${i === j ? 'diag' : ''}">${esc(val)}</td>`;
    }
    html += '</tr>';
  }
  html += '</tbody>';
  $('cm-table').innerHTML = html;
}

// ===== 右栏 F：报告导出（REQ-12/14/15） =====
function refreshExportState() {
  const has = !!S.change;
  $('export-empty').hidden = has;
  $('export-body').hidden = !has;
  refreshGates();
}

function renderSnapshot() {
  const ps = (S.change && S.change.params_snapshot) || {};
  // 后端快照字段可能为对象（如基线 {S2: ...}），统一安全字符串化，避免 [object Object]
  const fmt = (v, dflt) => {
    if (v == null || v === '') return dflt;
    if (typeof v === 'object') {
      if (v.S2 != null) return String(v.S2); // proc_baseline 按传感器给出
      return JSON.stringify(v);
    }
    return String(v);
  };
  const commit = ps.commit || (S.exportResult && S.exportResult.commit) || '—';
  const formula = (ps.formula_id || 'S2_L2A_BOA') + (ps.formula_text ? ' · ' + ps.formula_text : '');
  const rows = [
    ['run_id', fmt((S.change && S.change.run_id) || (S.aoiResult && S.aoiResult.run_id), '—'), true],
    ['commit（代码版次）', fmt(commit, '—'), true],
    ['辐射定标公式', formula, true],
    ['处理基线版次', fmt(ps.proc_baseline, '—'), false],
    ['变化阈值 ΔNDVI', fmt(ps.threshold, '—'), false],
    ['分块尺寸 (px)', fmt(ps.block_size, '—'), false],
    ['指数类型', fmt(ps.index, 'NDVI'), false],
  ];
  $('snapshot-table').innerHTML = '<tbody>' + rows.map(([k, v, code]) =>
    `<tr><td class="k">${esc(k)}</td><td>${code ? '<code>' + esc(v) + '</code>' : esc(v)}</td></tr>`).join('') + '</tbody>';
}

function renderFiles(files, isDemo) {
  const box = $('export-files');
  if (!files || !files.length) { box.innerHTML = '<div class="ok-line muted">尚无导出文件</div>'; return; }
  box.innerHTML = files.map((f) => {
    const size = f.size != null ? (f.size > 1024 ? (f.size / 1024).toFixed(1) + ' KB' : f.size + ' B') : '—';
    const tag = (isDemo || f.demo) ? '<span class="demo-tag solid">演示数据</span>' : '';
    const ph = f.placeholder ? '<span class="ph-tag">占位</span>' : '';
    return `<div class="file-item"><span class="fi-fmt">${esc(f.format)}</span><span class="fi-url">${esc(String(f.url || '').slice(0, 40))}</span><span class="fi-size">${size}</span>${tag}${ph}</div>`;
  }).join('');
}

function triggerDownload(f) {
  if (!f || !f.url) return;
  const a = document.createElement('a');
  a.href = f.url;
  a.download = f.filename || (f.format + '.bin');
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ===== 批次 1（REQ-08/11/14）：面积统计 CSV → Blob 同源下载 =====
// 与 triggerDownload 同为「<a download> 触发」，区别仅在于 href 用 Blob URL
// （后端 CSV 带 \ufeff BOM，用 Blob 下载可原样保留编码，不做二次文本转换）。
async function downloadAreaStatsCsv() {
  const rid = currentRunId();
  if (!rid) { showToast('尚无 run_id，请先完成一次变化检测', 'warn'); return; }
  if (S.csvBusy) return;
  const btn = $('btn-export-csv');
  const label = btn.textContent;
  S.csvBusy = true;
  btn.disabled = true;
  btn.textContent = '导出中…';
  try {
    const blob = await api.exportAreaStatsCsv(rid);
    if (!blob) { showToast('面积统计 CSV 为空，导出未完成', 'warn'); return; }
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'area_stats_' + rid + '.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 2000);
    showToast('面积统计 CSV 已导出', 'ok');
  } catch (e) {
    // 后端业务错误体 → 全局中文横幅；非契约错误 → 内联 toast，绝不产生未捕获 JS 异常
    if (e instanceof ApiError) {
      showError({ code: e.code, message: e.message, run_id: e.run_id });
    } else {
      showToast('面积统计 CSV 导出失败：' + (e && e.message ? e.message : '未知原因'), 'warn');
    }
  } finally {
    S.csvBusy = false;
    btn.disabled = false;
    btn.textContent = label;
  }
}

async function doExport(formats) {
  const fmts = formats.map((f) => String(f).toLowerCase());
  if (!fmts.length) { showToast('请先勾选至少一种导出格式', 'warn'); return; }
  // 前端格式白名单（REQ-12 / EX6）：越权格式即时按内置映射展示，不臆造语义
  const bad = fmts.find((f) => !ALLOW_FORMATS.includes(f));
  if (bad) {
    showError({ code: 'EX6', message: ERROR_MESSAGES.EX6, run_id: currentRunId() || null });
    return;
  }
  if (!currentRunId()) { showToast('尚无 run_id，请先完成一次变化检测', 'warn'); return; }
  const btn = $('btn-export');
  S.exportBusy = true;
  btn.disabled = true; btn.textContent = '导出中…';
  const body = { run_id: currentRunId(), formats: fmts };
  try {
    const res = await api.exportReport(body);
    if (res.demo) markDemo();
    else setDataBadge(false, 'POST /report/export');
    S.exportResult = res.data;
    renderFiles(res.data.files, res.demo);
    (res.data.files || []).forEach(triggerDownload);
    renderSnapshot();
    showToast(res.demo ? '导出完成（演示数据，文件已标注）' : '导出完成', 'ok');
  } catch (e) {
    handleErr(e);
  } finally {
    S.exportBusy = false;
    btn.textContent = '导出所选格式 · POST /report/export';
    refreshGates();
  }
}

// ===== 技术前提门控汇总（自由流程：全部常驻，缺前提才置灰） =====
function refreshGates() {
  const st = aoiStatus();
  const phOk = phaseValid();
  gate('btn-aoi-apply', st.ok && phOk,
    !st.ok ? (st.reason || '请填写完整四至（WGS84）')
      : (st.spanOver ? '[EX1] ' + ERROR_MESSAGES.EX1 : phaseErrText()));

  const canRun = !!(S.aoiResult && S.selectedPair);
  gate('btn-run-change', canRun && !S.changeBusy,
    !S.aoiResult ? '需先应用 AOI（左栏「AOI 设置」卡片）'
      : (!S.selectedPair ? '需先选择影像对（左栏「双时相影像对」卡片）' : '检测运行中，请稍候…'));

  const fmts = ['cb-geotiff', 'cb-png', 'cb-pdf'].filter((id) => $(id) && $(id).checked);
  gate('btn-export', !!currentRunId() && fmts.length > 0 && !S.exportBusy,
    !currentRunId() ? '需先完成一次变化检测获得 run_id' : '请至少勾选一种导出格式');
}

// ===== 地图模式（浏览 / AOI 框选 / 样本打点） =====
function setMapMode(mode) {
  S.mapMode = mode;
  document.querySelectorAll('.mode-btn').forEach((b) => b.classList.toggle('active', b.dataset.mode === mode));
  $('map-hint').textContent = HINTS[mode] || HINTS.browse;
  if (map && map.getCanvas()) {
    map.getCanvas().style.cursor = mode === 'browse' ? '' : 'crosshair';
  }
}

// ===== 在线取数（可选）· B 级扩展 / REQ-02 在线分支 =====
// 只消费 api.discoverScenes / startFetch / pollFetch 三个新接口；复用 A 卡 S.aoi（WGS84 bbox），
// 不重复输入 AOI。状态与错误展示完全内联于本卡片，绝不调用 handleErr / showError / gate()，
// 因此不影响离线预置路径 A/B/C/D/E/F 的任何交互；错误文案按 code 走 errorMessage() 映射。

const ON_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const ON_NAME_RE = /^[a-z0-9_]{1,40}$/;
const ON_POLL_MS = 2000; // 每 2s 轮询一次 GET /scene/fetch/{fetch_id}

function onlineDefaultName() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return 'user_' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate())
    + '_' + p(d.getHours()) + p(d.getMinutes());
}

// 规范化为后端 name 约束 ^[a-z0-9_]{1,40}$
function onlineNormalizeName(raw) {
  let s = String(raw == null ? '' : raw).toLowerCase();
  s = s.replace(/[^a-z0-9_]+/g, '_').replace(/_+/g, '_').replace(/^_+|_+$/g, '');
  return s.slice(0, 40);
}

function renderOnlineNamePreview() {
  const el = $('onl-name-preview');
  if (!el) return;
  const n = onlineNormalizeName($('onl-name').value);
  const ok = ON_NAME_RE.test(n);
  el.className = 'onl-name-preview' + (ok ? '' : ' bad');
  el.innerHTML = ok
    ? '提交组名：<code>' + esc(n) + '</code>'
    : '组名规范化后为空，仅允许小写字母 / 数字 / 下划线（1–40 位）';
}

function refreshOnlineAoiEcho() {
  const el = $('onl-aoi-text');
  if (!el) return;
  const b = S.aoi;
  if (!b) { el.textContent = '—（请先在 A 卡设置 AOI）'; return; }
  el.textContent = '[' + Number(b.w).toFixed(4) + ', ' + Number(b.s).toFixed(4)
    + ', ' + Number(b.e).toFixed(4) + ', ' + Number(b.n).toFixed(4) + ']';
}

function onlineAoiBbox() {
  const b = S.aoi;
  return b ? [Number(b.w), Number(b.s), Number(b.e), Number(b.n)] : null;
}

function onlineWindows() {
  return {
    t1_window: [$('onl-t1-start').value, $('onl-t1-end').value],
    t2_window: [$('onl-t2-start').value, $('onl-t2-end').value],
  };
}

// 本地前置校验（减少无效请求）：AOI 存在/合法/跨度≤0.25°(EX1) + 两窗口齐全、end>start、t2>t1
function onlinePrecheck() {
  const b = onlineAoiBbox();
  if (!b) return { err: '请先在 A 卡「AOI 设置」填写 AOI（本卡片复用其当前 AOI）。' };
  const st = aoiStatus();
  if (!st.legal) return { err: st.reason || 'AOI 四至不合法' };
  if (st.spanOver) return { err: '[EX1] ' + ERROR_MESSAGES.EX1 };
  const w = onlineWindows();
  const all = w.t1_window.concat(w.t2_window);
  if (all.some((d) => !ON_DATE_RE.test(d))) return { err: '请填全 t1 / t2 两个时相窗口的起止日期（YYYY-MM-DD）。' };
  if (w.t1_window[1] <= w.t1_window[0] || w.t2_window[1] <= w.t2_window[0]) {
    return { err: '每个窗口的结束日期须晚于开始日期。' };
  }
  if (w.t2_window[0] <= w.t1_window[0]) return { err: 't2 窗口须晚于 t1 窗口（t2 > t1）。' };
  return { bbox: b, t1_window: w.t1_window, t2_window: w.t2_window };
}

function onlCloud(v) {
  return fmtCloud(v);
}

// ---------- discover：候选清单（空态 / 加载中 / 错误 / 列表 三态） ----------
function renderOnlineCandidates() {
  const box = $('onl-cand-list');
  const list = ON.candidates;
  if (!list.length) {
    box.innerHTML = '<div class="empty-state compact"><p>无候选影像对</p>'
      + '<p class="empty-sub">可调整 AOI 或 t1 / t2 时相窗口后重新查询。</p></div>';
    $('onl-cand-count').textContent = '0 组';
    return;
  }
  $('onl-cand-count').textContent = list.length + ' 组 · 综合云量升序';
  box.innerHTML = list.map((c, i) => {
    const t1 = c.t1 || {}, t2 = c.t2 || {};
    const sel = ON.selected === i ? ' selected' : '';
    const hi1 = Number(t1.cloud_cover) > 50, hi2 = Number(t2.cloud_cover) > 50;
    return `
      <div class="onl-cand${sel}" data-idx="${i}">
        <div class="onl-cand-head"><span class="radio"></span><span class="onl-tile">tile ${esc(c.tile || '—')}</span>
          <span class="onl-combined">综合云量 ${esc(onlCloud(c.combined_cloud))}</span></div>
        <div class="onl-scene"><span class="tag tag-t1">T1</span><span class="dt">${esc(t1.date || '—')}</span><span class="cloud ${hi1 ? 'high' : ''}">云量 ${esc(onlCloud(t1.cloud_cover))}</span></div>
        <div class="onl-scene"><span class="tag tag-t2">T2</span><span class="dt">${esc(t2.date || '—')}</span><span class="cloud ${hi2 ? 'high' : ''}">云量 ${esc(onlCloud(t2.cloud_cover))}</span></div>
        <div class="onl-cand-meta">基线 ${esc(t1.proc_baseline || '—')} / ${esc(t2.proc_baseline || '—')} · S2 L2A</div>
        ${ON.selected === i ? '<div class="onl-sel-note">✓ 已选定该候选（t1 + t2 同时选中）</div>' : ''}
      </div>`;
  }).join('');
}

function renderOnlineEmptyState(msg) {
  $('onl-cand-list').innerHTML = '<div class="empty-state compact"><p>暂无候选</p>'
    + '<p class="empty-sub">' + esc(msg) + '</p></div>';
  $('onl-cand-count').textContent = '—';
}

function updateOnlineFetchGate() {
  const btn = $('onl-fetch');
  if (!btn) return;
  const can = ON.selected >= 0 && !!ON.candidates[ON.selected] && !ON.fetching && !ON.discoverBusy;
  btn.disabled = !can;
  btn.title = can ? '' : (ON.fetching ? '下载进行中' : '请先在候选清单中点选一个候选影像对');
}

// 优雅降级：discover 404（端点未启用）或网络失败 → 置灰说明，不弹错误弹窗、不影响离线路径
function showOnlineDegraded(detail) {
  ON.degraded = true;
  stopOnlinePolling();
  ON.fetching = false;
  $('onl-degraded').hidden = false;
  $('onl-body').hidden = true;
  $('onl-badge').textContent = '未启用';
  const sub = $('onl-degraded').querySelector('.empty-sub');
  if (sub && detail) sub.textContent = detail;
}

function clearOnlineDegraded() {
  ON.degraded = false;
  $('onl-degraded').hidden = true;
  $('onl-body').hidden = false;
}

// 按 code 展示中文提示（errors.js ERROR_MESSAGES，含 SYS-06/SYS-07），未命中回退后端原文
function onlineErrText(payload) {
  const code = payload && payload.code;
  const zh = errorMessage({ code, message: payload && payload.message });
  return (code ? '[' + code + '] ' : '') + zh;
}

async function onlineDiscover() {
  if (ON.discoverBusy || ON.fetching) return;
  const errEl = $('onl-discover-err');
  errEl.textContent = '';
  const pre = onlinePrecheck();
  if (pre.err) { errEl.textContent = pre.err; return; }

  ON.discoverBusy = true;
  ON.selected = -1;
  ON.candidates = [];
  const btn = $('onl-discover');
  btn.disabled = true; btn.textContent = '检索中…';
  $('onl-discover-loading').hidden = false;
  $('onl-cand-list').innerHTML = '<div class="status-line"><span class="spinner"></span>正在检索 STAC 候选影像…</div>';
  $('onl-progress').hidden = true;
  $('onl-fetch-err').textContent = '';
  updateOnlineFetchGate();
  try {
    const res = await api.discoverScenes({
      bbox: pre.bbox,
      t1_window: pre.t1_window,
      t2_window: pre.t2_window,
    });
    if (res.ok) {
      clearOnlineDegraded();
      ON.candidates = (res.data && Array.isArray(res.data.candidates)) ? res.data.candidates : [];
      ON.runId = (res.data && res.data.run_id) || '';
      ON.selected = -1;
      renderOnlineCandidates();
      $('onl-badge').textContent = ON.runId ? ('STAC 在线 · ' + ON.runId.slice(0, 8)) : 'STAC 在线';
      if (!ON.candidates.length) errEl.textContent = '本次检索无候选影像对，可调整窗口后重试。';
    } else if (res.unavailable || res.status === 404) {
      // 端点未启用（裸 404）/ 网络失败 → 置灰降级，绝不弹错误弹窗
      showOnlineDegraded('discover 返回 404 或网络不可达；当前走离线预置流程，A / B / C / D / E / F 全部功能不受影响。');
    } else {
      // 业务错误体 {code,message,run_id}：内联中文提示，不弹全局错误横幅
      errEl.textContent = onlineErrText(res);
      renderOnlineEmptyState(onlineErrText(res));
      $('onl-badge').textContent = 'STAC 在线';
    }
  } catch (e) {
    // 兜底：任何异常也只做置灰降级，不弹错误弹窗、不污染离线路径
    showOnlineDegraded();
  } finally {
    ON.discoverBusy = false;
    btn.disabled = false; btn.textContent = '查询候选影像 · POST /scene/discover';
    $('onl-discover-loading').hidden = true;
    updateOnlineFetchGate();
  }
}

function selectOnlineCandidate(i) {
  if (!ON.candidates[i]) return;
  ON.selected = i;
  renderOnlineCandidates();
  updateOnlineFetchGate();
}

// ===== 批次 1（REQ-02 在线分支）：「自动选最优」 =====
// 取 combined_cloud 数值最小的候选，调用既有 selectOnlineCandidate(i)：
// 与用户手点该 .onl-cand 完全等价（同一 ON.selected / 同一高亮 / 同一 fetch 门控）。
function autoSelectBestOnlineCandidate() {
  if (!ON.candidates.length) {
    showToast('请先「查询候选影像」，再自动选最优', 'warn');
    return;
  }
  let best = 0, bestCloud = Infinity;
  ON.candidates.forEach((c, i) => {
    const v = Number(c && c.combined_cloud);
    if (Number.isFinite(v) && v < bestCloud) { bestCloud = v; best = i; }
  });
  if (!Number.isFinite(bestCloud)) {
    showToast('候选缺少综合云量 combined_cloud，无法自动选优', 'warn');
    return;
  }
  selectOnlineCandidate(best);
  showToast('已自动选定综合云量最低的候选（' + fmtCloud(bestCloud) + '）', 'ok');
}

// ---------- fetch：提交 + 2s 轮询状态 ----------
function renderOnlineProgress(status, done, total) {
  const st = String(status || 'PENDING').toUpperCase();
  const badge = $('onl-status-badge');
  badge.textContent = st;
  badge.className = 'onl-status-badge st-' + st;
  const d = Number(done) || 0, t = Number(total) || 10;
  $('onl-band').textContent = '波段 ' + d + '/' + t;
  const pct = Math.max(0, Math.min(100, Math.round((d / t) * 100)));
  $('onl-bar-fill').style.width = pct + '%';
  const bar = $('onl-progress').querySelector('.onl-bar');
  bar.classList.toggle('done', st === 'SUCCESS');
  bar.classList.toggle('failed', st === 'FAILED');
  const spinning = st === 'PENDING' || st === 'RUNNING';
  $('onl-prog-spinner').hidden = !spinning;
  return { st, d, t };
}

function stopOnlinePolling() {
  if (ON.pollTimer) { window.clearInterval(ON.pollTimer); ON.pollTimer = null; }
  ON.pollBusy = false;
}

function restoreOnlineFetchButton() {
  ON.fetching = false;
  const btn = $('onl-fetch');
  btn.textContent = '下载并入库 · POST /scene/fetch';
  updateOnlineFetchGate();
}

function onlineFetchTerminal(status, data) {
  stopOnlinePolling();
  ON.terminal = status;
  const pg = (data && data.progress) || {};
  renderOnlineProgress(status, pg.done, pg.total);
  restoreOnlineFetchButton();
  if (status === 'SUCCESS') {
    const ids = (data && data.scene_ids) || {};
    const ok = $('onl-success');
    ok.hidden = false;
    ok.textContent = '✓ 入库完成 · scene t1 ' + (ids.t1 || '—') + ' / t2 ' + (ids.t2 || '—') + '，影像对列表已刷新';
    showToast('在线取数完成，影像对列表已刷新', 'ok', 4200);
    // 自动刷新 B 卡影像对列表（loadPairs → api.listScenePairs()）
    loadPairs();
  } else {
    const box = $('onl-failed');
    box.hidden = false;
    const err = (data && data.error) || {};
    box.textContent = onlineErrText({ code: err.code, message: err.message });
  }
  // 终态后按钮进度态复位为可再次提交
  $('onl-fetch').textContent = '下载并入库 · POST /scene/fetch';
}

async function onlinePollTick() {
  if (!ON.fetching || ON.pollBusy || !ON.fetchId) return;
  ON.pollBusy = true;
  try {
    // 单次 GET：15s 超时只作用于本次请求；2s 轮询循环在 setInterval 内，不被整体超时中断
    const res = await api.pollFetch(ON.fetchId);
    if (!ON.fetching) return; // 已终止/切换任务，丢弃过期回调
    if (res.ok) {
      ON.pollFails = 0;
      const data = res.data || {};
      const st = String(data.status || '').toUpperCase();
      const pg = data.progress || {};
      renderOnlineProgress(st || 'PENDING', pg.done, pg.total);
      $('onl-fetch').textContent = '下载中… 波段 ' + (Number(pg.done) || 0) + '/' + (Number(pg.total) || 10);
      if (st === 'SUCCESS' || st === 'FAILED') onlineFetchTerminal(st, data);
    } else if (res.transient) {
      // 单次 GET 抖动（网络/5xx/超时）：跳过本次，2s 后继续，绝不中断长下载
      ON.pollFails += 1;
      if (ON.pollFails === 3) {
        $('onl-fetch').textContent = '下载中…（进度查询重试中）';
      }
    } else {
      // 裸 404 / 业务错误体：停止轮询，内联中文提示
      const detail = res.unavailable ? '后端未启用在线取数或网络不可达' : onlineErrText(res);
      onlineFetchTerminal('FAILED', { error: { code: res.code, message: detail } });
    }
  } catch (e) {
    // 兜底：单次轮询异常按瞬态处理，继续下一轮
    ON.pollFails += 1;
  } finally {
    ON.pollBusy = false;
  }
}

function startOnlinePolling(fetchId, initialStatus) {
  stopOnlinePolling();
  ON.fetchId = fetchId;
  ON.fetching = true;
  ON.terminal = '';
  ON.pollFails = 0;
  ON.pollBusy = false;
  $('onl-progress').hidden = false;
  $('onl-success').hidden = true;
  $('onl-failed').hidden = true;
  $('onl-fetch-id').textContent = 'fetch_id ' + fetchId;
  renderOnlineProgress(initialStatus || 'PENDING', 0, 10);
  $('onl-fetch').textContent = '下载中… 波段 0/10';
  updateOnlineFetchGate();
  onlinePollTick();                                  // 立即拉一次，尽快进入 RUNNING
  ON.pollTimer = window.setInterval(onlinePollTick, ON_POLL_MS); // 之后每 2s 独立轮询
}

async function onlineFetch() {
  if (ON.fetching || ON.discoverBusy) return;
  const errEl = $('onl-fetch-err');
  errEl.textContent = '';
  const pre = onlinePrecheck();
  if (pre.err) { errEl.textContent = pre.err; return; }
  const c = ON.candidates[ON.selected];
  if (!c) { errEl.textContent = '请先在候选清单中点选一个候选影像对。'; return; }
  const rawName = $('onl-name').value;
  const name = onlineNormalizeName(rawName) || onlineDefaultName();
  $('onl-name').value = name;
  renderOnlineNamePreview();
  if (!ON_NAME_RE.test(name)) { errEl.textContent = '组名不合法：仅允许 [a-z0-9_]（1–40 位）。'; return; }

  const phase = (p) => ({
    source_id: String((p && p.source_id) || ''),
    date: String((p && p.date) || ''),
    proc_baseline: String((p && p.proc_baseline) || ''),
    cloud_cover: (p && p.cloud_cover != null) ? Number(p.cloud_cover) : null,
  });
  const body = {
    name,
    bbox: pre.bbox,
    tile: String(c.tile || ''),
    t1: phase(c.t1),
    t2: phase(c.t2),
  };
  const btn = $('onl-fetch');
  btn.disabled = true; btn.textContent = '提交中…';
  try {
    const res = await api.startFetch(body);
    if (res.ok && res.data && res.data.fetch_id) {
      startOnlinePolling(res.data.fetch_id, res.data.status);
    } else if (res.unavailable || res.status === 404) {
      showOnlineDegraded('在线取数接口不可用；当前走离线预置流程，A / B / C / D / E / F 全部功能不受影响。');
      restoreOnlineFetchButton();
    } else {
      errEl.textContent = onlineErrText(res);
      restoreOnlineFetchButton();
    }
  } catch (e) {
    showOnlineDegraded();
    restoreOnlineFetchButton();
  }
}

function retryOnline() {
  clearOnlineDegraded();
  $('onl-badge').textContent = '可选 · 离线优先';
  $('onl-discover-err').textContent = '';
  onlineDiscover();
}

// ===== 事件绑定 =====
function bindEvents() {
  ['aoi-w', 'aoi-s', 'aoi-e', 'aoi-n'].forEach((id) => {
    $(id).addEventListener('input', syncFromInputs);
  });

  const seedBtns = [
    ['btn-seed-poyang', 0], ['btn-seed-farmland', 1], ['btn-seed-nanchang', 2],
  ];
  seedBtns.forEach(([id, i]) => {
    $(id).onclick = () => {
      pushAoiHistory();
      applyAoi(boundsFromArray(demo.SEEDS[i].aoi));
      M.fitBounds(map, S.aoi, 90);
      // 联动时相下拉
      $('t1').value = demo.SEEDS[i].scenes.t1.date;
      $('t2').value = demo.SEEDS[i].scenes.t2.date;
      S.t1 = $('t1').value; S.t2 = $('t2').value;
      updateAoiMetrics();
    };
  });

  $('btn-aoi-clear').onclick = () => {
    pushAoiHistory();
    S.aoi = null;
    fillAoiInputs(null);
    M.setAoi(map, null);
    updateAoiMetrics();
  };
  $('btn-aoi-undo').onclick = () => {
    const prev = S.aoiHistory.pop();
    if (!prev) { showToast('没有可撤销的 AOI', 'info'); return; }
    applyAoi(prev);
  };

  $('t1').onchange = () => { S.t1 = $('t1').value; updateAoiMetrics(); };
  $('t2').onchange = () => { S.t2 = $('t2').value; updateAoiMetrics(); };
  $('btn-aoi-apply').onclick = submitAoi;

  $('pair-filter').addEventListener('input', () => { S.pairFilter = $('pair-filter').value; renderPairs(); });
  $('block-size').onchange = () => { if (S.selectedPair) scheduleAutoConfirm(); };

  $('btn-run-change').onclick = runChangeDetection;

  $('opacity').addEventListener('input', () => {
    const v = Number($('opacity').value);
    S.opacity = v / 100;
    $('opacity-val').textContent = v + '%';
    M.setImageOpacity(map, S.opacity);
  });

  document.querySelectorAll('.mode-btn').forEach((b) => {
    b.onclick = () => setMapMode(b.dataset.mode);
  });

  $('class-btns').querySelectorAll('.class-btn').forEach((el) => {
    el.onclick = () => {
      S.curClass = +el.dataset.cls;
      $('class-btns').querySelectorAll('.class-btn').forEach((x) => x.classList.toggle('active', x === el));
    };
  });
  $('btn-fill-samples').onclick = fillSamplesDemo;
  $('btn-sample-undo').onclick = () => {
    S.samples.pop(); M.setSamples(map, S.samples); updateCounts();
  };
  $('btn-samples-clear').onclick = () => {
    S.samples = []; M.setSamples(map, S.samples); updateCounts();
  };
  $('btn-accuracy').onclick = calcAccuracy;

  ['cb-geotiff', 'cb-png', 'cb-pdf'].forEach((id) => {
    $(id).addEventListener('change', refreshGates);
  });
  $('btn-export').onclick = () => {
    const fmts = ['cb-geotiff', 'cb-png', 'cb-pdf'].filter((id) => $(id).checked)
      .map((id) => id.replace('cb-', ''));
    doExport(fmts);
  };
  $('btn-exp-custom').onclick = () => {
    const v = $('custom-format').value.trim();
    if (!v) { showToast('请输入要校验的导出格式', 'warn'); return; }
    doExport([v]);
  };

  // 在线取数（可选）卡片事件（B 级扩展 / REQ-02 在线分支）
  $('onl-discover').onclick = onlineDiscover;
  $('onl-fetch').onclick = onlineFetch;
  $('onl-retry').onclick = retryOnline;
  $('onl-name').addEventListener('input', renderOnlineNamePreview);
  $('onl-name').addEventListener('blur', () => {
    const n = onlineNormalizeName($('onl-name').value);
    if (n) $('onl-name').value = n;
    renderOnlineNamePreview();
  });
  // 日期窗口变化时清空旧的候选/选择，避免用过期候选提交
  ['onl-t1-start', 'onl-t1-end', 'onl-t2-start', 'onl-t2-end'].forEach((id) => {
    $(id).addEventListener('change', () => {
      if (ON.candidates.length && !ON.fetching) {
        ON.candidates = []; ON.selected = -1;
        renderOnlineEmptyState('时相窗口已变更，请重新「查询候选影像」。');
        updateOnlineFetchGate();
      }
    });
  });
  // 候选清单点选（事件委托）：点选一个候选即同时选定其 t1 与 t2
  $('onl-cand-list').addEventListener('click', (e) => {
    const card = e.target.closest && e.target.closest('.onl-cand');
    if (card) selectOnlineCandidate(Number(card.dataset.idx));
  });

  // ===== 批次 1：C 卡高级选项 / 统计区 CSV / 在线候选自动选优 =====
  $('manual-threshold').addEventListener('change', syncRunAdvancedUi);
  $('threshold-slider').addEventListener('input', syncRunAdvancedUi);
  $('btn-export-csv').onclick = downloadAreaStatsCsv;
  $('btn-onl-auto-best').onclick = autoSelectBestOnlineCandidate;
  syncRunAdvancedUi(); // 初始化开关/滑块/实时显值，不改变任何既有门控与离线路径
}

function bindMapEvents() {
  // 矩形框选（AOI 框选模式，REQ-01）
  let drawing = false, moved = false, startPx = null, startLL = null;
  map.on('mousedown', (e) => {
    if (S.mapMode !== 'aoi') return;
    drawing = true; moved = false;
    startPx = { x: e.point.x, y: e.point.y };
    startLL = map.unproject(e.point);
    pushAoiHistory();
    map.dragPan.disable();
  });
  map.on('mousemove', (e) => {
    if (!drawing || S.mapMode !== 'aoi') return;
    const d = Math.hypot(e.point.x - startPx.x, e.point.y - startPx.y);
    if (d <= 4) return;
    moved = true;
    applyAoi(normBounds(startLL, map.unproject(e.point)));
  });
  const endDraw = () => {
    if (!drawing) return;
    drawing = false;
    map.dragPan.enable();
    if (moved) updateAoiMetrics();
  };
  window.addEventListener('mouseup', endDraw);

  // 样本点打点 / 删除（样本打点模式，REQ-09）
  map.on('click', (e) => {
    if (S.mapMode !== 'sample') return;
    const feats = map.queryRenderedFeatures(e.point, { layers: ['sample-circles'] });
    if (feats.length) {
      removeSample(feats[0].properties.id);
      return;
    }
    addSample(e.lngLat.lng, e.lngLat.lat, S.curClass);
  });
}

// ===== 启动 =====
function init() {
  // 时相预置清单（REQ-01）
  const dateOpts = '<option value="">— 选择时相 —</option>' +
    demo.PRESET_DATES.map((d) => `<option value="${d}">${d}</option>`).join('');
  $('t1').innerHTML = dateOpts;
  $('t2').innerHTML = dateOpts;

  // 在线取数卡片（可选）默认组名 user_<yyyymmdd>_<hhmm> + AOI 回显
  $('onl-name').value = onlineDefaultName();
  renderOnlineNamePreview();
  refreshOnlineAoiEcho();

  // 地图图例（三类色带 · REQ-10）
  $('map-legend').innerHTML = `
    <h4>变化专题图色带 · 编码</h4>
    <div class="legend-row"><span class="legend-code">1</span><span class="legend-swatch" style="background:#21a04c"></span>增强</div>
    <div class="legend-row"><span class="legend-code">2</span><span class="legend-swatch" style="background:#d94c3d"></span>退化</div>
    <div class="legend-row"><span class="legend-code">3</span><span class="legend-swatch" style="background:#5b7a99"></span>稳定</div>`;

  bindEvents();

  map = M.createMap('map', demo.SEEDS[0].center, 9);
  map.on('load', () => {
    fillAoiInputs(boundsFromArray(demo.SEEDS[0].aoi));
    syncFromInputs();
    M.fitBounds(map, S.aoi, 90);
    setMapMode('aoi');
  });
  bindMapEvents();

  updateCounts();
  renderSnapshot();
  renderFiles([], false);
  refreshExportState();
  refreshGates();
  setMapMode('aoi');

  // 影像对清单启动即加载（自由流程：无 AOI 也可浏览，GET /scene/match 不强制 aoi_id）
  loadPairs();

  window.addEventListener('resize', () => {
    try { if (map) map.resize(); } catch (_) { /* style 未就绪 */ }
    C.resizeCharts();
  });
}

init();
