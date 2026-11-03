// -*- coding: utf-8 -*-
// 表现层主控：UI-01 ~ UI-05 状态机、5 步流程、MapLibre/ECharts 编排。
// 仅调用 api.js 的 5 个接口；不做任何影像处理/算法/SQL；不直连 MinIO/PostGIS。
// 每个界面/控件标注 REQ-ID，对齐 HLD 4.x 与追溯表。

import 'maplibre-gl/dist/maplibre-gl.css';
import './style.css';
import { ERROR_MESSAGES, showToast, showError, ApiError } from './errors.js';
import * as api from './api.js';
import * as demo from './demo.js';
import * as M from './map.js';
import * as C from './charts.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

const STEPS = [
  { n: 1, t: 'AOI 框选', s: 'REQ-01' },
  { n: 2, t: '影像对确认', s: 'REQ-02 / REQ-04' },
  { n: 3, t: '变化专题图', s: 'REQ-10 / REQ-08' },
  { n: 4, t: '样本与精度', s: 'REQ-09' },
  { n: 5, t: '统计与报告', s: 'REQ-11 / REQ-12 / REQ-15' },
];

const HINTS = {
  1: '按住鼠标拖拽画矩形；或在左侧手填四至（WGS84）。仅允许 1 个 AOI。',
  2: '从预置清单选择一组双时相影像对，确认锁定的辐射口径。',
  3: '专题图以 image source 叠加到 AOI 四角，拖动滑杆可调不透明度。',
  4: '点击地图空白处按当前类别打点；点击已有样本点即删除该点。',
  5: '查看三类面积统计与只读参数快照，点击按钮触发报告下载。',
};
const MODES = {
  1: '模式：AOI 框选', 2: '模式：影像对确认', 3: '模式：专题图叠加',
  4: '模式：样本勾选', 5: '模式：报告导出',
};
const ALLOW_FORMATS = ['geotiff', 'png', 'pdf']; // constants.py EXPORT_FORMATS

// ===== 全局状态 =====
const S = {
  step: 1, maxStep: 1,
  demo: false,
  aoi: null, aoiHistory: [],
  t1: '', t2: '',
  aoiResult: null,
  pairs: [], pairVia: '', selectedPair: null, sceneMatch: null,
  change: null, changePngUrl: null, changeLoaded: false,
  opacity: 0.85,
  samples: [], sampleSeq: 0, curClass: 1, accuracy: null,
  exportResult: null,
};

let map = null;

// ===== DOM 骨架 =====
function buildPanel() {
  const dateOpts = '<option value="">— 选择时相 —</option>' +
    demo.PRESET_DATES.map((d) => `<option value="${d}">${d}</option>`).join('');

  $('panel').innerHTML = `
  <!-- ============ UI-01 ============ -->
  <section class="view active" data-step="1">
    <div class="view-head">
      <h2>① AOI 框选与时相<span class="req">REQ-01</span></h2>
      <p>离线空白底图上拖拽矩形，或手填四至；跨度与面积实时计算。</p>
    </div>

    <div class="card">
      <div class="card-title">手填 AOI 四至（WGS84）<span class="req">REQ-01</span></div>
      <div class="grid4">
        <div><label class="fld">西 West (lon)</label><input type="number" step="0.0001" id="aoi-w" /></div>
        <div><label class="fld">南 South (lat)</label><input type="number" step="0.0001" id="aoi-s" /></div>
        <div><label class="fld">东 East (lon)</label><input type="number" step="0.0001" id="aoi-e" /></div>
        <div><label class="fld">北 North (lat)</label><input type="number" step="0.0001" id="aoi-n" /></div>
      </div>
      <div class="btn-row" style="margin-top:10px">
        <button type="button" class="btn btn-sm" id="btn-seed-poyang">鄱阳湖</button>
        <button type="button" class="btn btn-sm" id="btn-seed-farmland">赣中农田</button>
        <button type="button" class="btn btn-sm" id="btn-seed-nanchang">南昌</button>
      </div>
      <div class="btn-row" style="margin-top:8px">
        <button type="button" class="btn btn-sm btn-ghost" id="btn-aoi-clear">清空 AOI</button>
        <button type="button" class="btn btn-sm btn-ghost" id="btn-aoi-undo">撤销</button>
      </div>
      <div class="ok-line" style="color:var(--muted2);font-size:10.5px;font-weight:400">一键填入为本地影像对 seed 的 AOI<span class="demo-tag">演示数据</span></div>
    </div>

    <div class="card">
      <div class="card-title">实时跨度与面积<span class="req">REQ-01</span></div>
      <div class="metric-row">
        <div class="metric"><div class="k">经度跨度 (°)</div><div class="v" id="span-lon">—</div></div>
        <div class="metric"><div class="k">纬度跨度 (°)</div><div class="v" id="span-lat">—</div></div>
        <div class="metric"><div class="k">面积 (deg²)</div><div class="v" id="area-deg">—</div></div>
        <div class="metric"><div class="k">面积 (约 km²)</div><div class="v" id="area-km">—</div></div>
      </div>
      <div class="err-line" id="aoi-err"></div>
    </div>

    <div class="card">
      <div class="card-title">时相选择（预置清单）<span class="req">REQ-01 / REQ-02</span></div>
      <div class="grid2">
        <div><label class="fld">时相 t1（较早）</label><select id="t1">${dateOpts}</select></div>
        <div><label class="fld">时相 t2（较晚）</label><select id="t2">${dateOpts}</select></div>
      </div>
      <div class="err-line" id="phase-err"></div>
    </div>

    <button type="button" class="btn btn-primary btn-block" id="btn-aoi-submit" disabled>提交 AOI · POST /aoi/select</button>
  </section>

  <!-- ============ UI-02 ============ -->
  <section class="view" data-step="2">
    <div class="view-head">
      <h2>② 双时相影像对与口径<span class="req">REQ-02 / REQ-04</span></h2>
      <p>列出双时相影像对（日期 / 云量 / 影像块 / 基线），口径锁定只读。</p>
    </div>

    <div class="card">
      <div class="card-title">影像对列表<span class="req">REQ-02</span><span class="hint" id="pair-via">来源：—</span></div>
      <div id="pair-list"><div class="ok-line">正在获取影像对…</div></div>
      <div class="ok-line" style="color:var(--muted2);font-size:10.5px;font-weight:400">云量 &gt; 50% 将标黄提示<span class="demo-tag">演示数据</span></div>
    </div>

    <div class="card">
      <div class="card-title">锁定口径面板<span class="req">REQ-04</span><span class="readonly-badge">NF5 · 只读</span></div>
      <div class="kv" id="caliber-panel"><span class="hint">请先选择一组影像对</span></div>
    </div>

    <button type="button" class="btn btn-primary btn-block" id="btn-scene-confirm" disabled>确认影像对与口径 · POST /scene/match</button>
  </section>

  <!-- ============ UI-03 ============ -->
  <section class="view" data-step="3">
    <div class="view-head">
      <h2>③ 变化专题图叠加<span class="req">REQ-10 / REQ-08 / REQ-05</span></h2>
      <p>专题图 PNG 以 image source 叠加，三类色带 + 不透明度控制。</p>
    </div>

    <div class="card">
      <div class="card-title">有效像元占比 valid_ratio<span class="req">REQ-05</span></div>
      <div class="ratio-box">
        <div class="ratio-k">VALID RATIO</div>
        <div class="ratio-v" id="valid-ratio">—</div>
      </div>
      <div class="ratio-note" id="valid-note">等待检测结果…</div>
    </div>

    <div class="card">
      <div class="card-title">三类色带图例<span class="req">REQ-10</span></div>
      <div class="legend-inline">
        <div class="li"><span class="sw" style="background:#21ba45"></span>1 增强</div>
        <div class="li"><span class="sw" style="background:#e74c3c"></span>2 退化</div>
        <div class="li"><span class="sw" style="background:#5b7a99"></span>3 稳定</div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">叠加不透明度<span class="req">REQ-10</span></div>
      <div class="slider-row">
        <input type="range" id="opacity" min="0" max="100" value="85" />
        <span class="slider-val" id="opacity-val">85%</span>
      </div>
    </div>

    <div class="card">
      <div class="card-title">三类面积占比<span class="req">REQ-08 / REQ-11</span></div>
      <div class="chart" id="chart-pie"></div>
    </div>

    <button type="button" class="btn btn-primary btn-block" id="btn-to-u4">下一步：样本与精度评价 →</button>
    <div style="text-align:center;margin-top:8px"><span class="demo-tag">演示数据</span></div>
  </section>

  <!-- ============ UI-04 ============ -->
  <section class="view" data-step="4">
    <div class="view-head">
      <h2>④ 样本勾选与精度评价<span class="req">REQ-09</span></h2>
      <p>在专题图上按类别勾选样本点（WGS84），每类至少 30 个方可计算精度。</p>
    </div>

    <div class="card">
      <div class="card-title">当前打点类别<span class="req">REQ-09</span></div>
      <div class="class-btns" id="class-btns">
        <div class="class-btn active" data-cls="1"><span class="dot" style="background:#21ba45"></span>1 增强</div>
        <div class="class-btn" data-cls="2"><span class="dot" style="background:#e74c3c"></span>2 退化</div>
        <div class="class-btn" data-cls="3"><span class="dot" style="background:#5b7a99"></span>3 稳定</div>
      </div>
      <div class="ok-line" style="color:var(--muted2);font-size:10.5px;font-weight:400">点击地图空白处打点；点击已有样本点即删除该点。</div>
    </div>

    <div class="card">
      <div class="card-title">每类实时计数（≥ 30）<span class="req">REQ-09</span></div>
      <div class="cnt-row"><span class="cnt-name">1 增强</span><div class="cnt-bar"><i id="bar-1" style="background:#21ba45;width:0%"></i></div><span class="cnt-num" id="cnt-1">0</span></div>
      <div class="cnt-row"><span class="cnt-name">2 退化</span><div class="cnt-bar"><i id="bar-2" style="background:#e74c3c;width:0%"></i></div><span class="cnt-num" id="cnt-2">0</span></div>
      <div class="cnt-row"><span class="cnt-name">3 稳定</span><div class="cnt-bar"><i id="bar-3" style="background:#5b7a99;width:0%"></i></div><span class="cnt-num" id="cnt-3">0</span></div>
      <div class="warn-line" id="ex10-hint"></div>
    </div>

    <div class="card" id="fill-wrap" hidden>
      <div class="card-title">演示辅助<span class="demo-tag">演示数据</span></div>
      <button type="button" class="btn btn-sm btn-block" id="btn-fill-samples">一键补齐每类 32 个样本点</button>
    </div>

    <div class="btn-row">
      <button type="button" class="btn btn-sm btn-ghost" id="btn-sample-undo">撤销上一点</button>
      <button type="button" class="btn btn-sm btn-ghost" id="btn-samples-clear">清空样本</button>
    </div>

    <button type="button" class="btn btn-primary btn-block" id="btn-accuracy" disabled style="margin-top:10px">计算精度 · POST /accuracy/sample</button>

    <div class="card" id="accuracy-result" hidden style="margin-top:14px">
      <div class="card-title">精度评价结果<span class="req">REQ-09</span><span class="demo-tag">演示数据</span></div>
      <div class="metric-row">
        <div class="metric"><div class="k">总体精度 OA</div><div class="v" id="oa">—</div></div>
        <div class="metric"><div class="k">Kappa 系数</div><div class="v" id="kappa">—</div></div>
      </div>
      <div class="ok-line" id="acc-note"></div>
      <div style="margin-top:10px"><table class="tbl" id="cm-table"></table></div>
    </div>

    <button type="button" class="btn btn-primary btn-block" id="btn-to-u5" disabled style="margin-top:6px">下一步：统计与报告 →</button>
  </section>

  <!-- ============ UI-05 ============ -->
  <section class="view" data-step="5">
    <div class="view-head">
      <h2>⑤ 统计与报告下载<span class="req">REQ-11 / REQ-12 / REQ-15</span></h2>
      <p>三类面积柱状图、只读参数快照，触发 GeoTIFF / PNG / PDF 下载。</p>
    </div>

    <div class="card">
      <div class="card-title">三类面积统计<span class="req">REQ-11</span></div>
      <div class="chart" id="chart-bar"></div>
    </div>

    <div class="card">
      <div class="card-title">参数快照（只读）<span class="req">REQ-14</span><span class="readonly-badge">NF9 · 只读</span></div>
      <table class="tbl snapshot" id="snapshot-table"></table>
    </div>

    <div class="card">
      <div class="card-title">报告导出 / 下载<span class="req">REQ-12 / REQ-15</span></div>
      <div class="btn-row">
        <button type="button" class="btn btn-sm" id="btn-exp-pdf">下载 PDF</button>
        <button type="button" class="btn btn-sm" id="btn-exp-png">下载 PNG</button>
        <button type="button" class="btn btn-sm" id="btn-exp-tif">下载 GeoTIFF</button>
      </div>
      <div id="export-files"></div>
      <div style="margin-top:10px;border-top:1px dashed var(--line-soft);padding-top:10px">
        <label class="fld">格式校验诊断：输入越权格式触发 EX6<span class="req">REQ-12</span></label>
        <div class="btn-row">
          <input type="text" id="custom-format" placeholder="例如 shp（越权格式）" />
          <button type="button" class="btn btn-sm" id="btn-exp-custom">提交</button>
        </div>
      </div>
    </div>

    <div style="text-align:center"><span class="demo-tag">演示数据</span></div>
  </section>`;
}

function buildLegend() {
  $('map-legend').innerHTML = `
    <h4>变化专题图色带 · 编码</h4>
    <div class="legend-row"><span class="legend-code">1</span><span class="legend-swatch" style="background:#21ba45"></span>增强</div>
    <div class="legend-row"><span class="legend-code">2</span><span class="legend-swatch" style="background:#e74c3c"></span>退化</div>
    <div class="legend-row"><span class="legend-code">3</span><span class="legend-swatch" style="background:#5b7a99"></span>稳定</div>`;
}

function renderSteps() {
  let html = '';
  STEPS.forEach((st, i) => {
    const cls = ['step', S.step === st.n ? 'active' : '', S.maxStep > st.n ? 'done' : '', S.maxStep < st.n ? 'locked' : ''].filter(Boolean).join(' ');
    html += `<div class="${cls}" data-step="${st.n}"><div class="step-num">${S.maxStep > st.n ? '✓' : st.n}</div><div class="step-label"><b>${st.t}</b><small>${st.s}</small></div></div>`;
    if (i < STEPS.length - 1) html += '<div class="step-sep"></div>';
  });
  $('steps').innerHTML = html;
  $('steps').querySelectorAll('.step').forEach((d) => {
    d.onclick = () => { const n = +d.dataset.step; if (n <= S.maxStep) gotoStep(n); };
  });
}

// ===== 通用工具 =====
function markDemo() {
  const first = !document.body.classList.contains('is-demo');
  document.body.classList.add('is-demo');
  const fw = $('fill-wrap'); if (fw) fw.hidden = false;
  if (first) showToast('后端接口不可用或返回空占位，已启用本地演示数据（页面已标注）', 'warn', 5200);
}

function handleErr(e) {
  if (e instanceof ApiError) {
    showError({ code: e.code, message: e.message, run_id: e.run_id });
  } else {
    showToast('前端异常：' + (e && e.message ? e.message : String(e)), 'error');
    // eslint-disable-next-line no-console
    console.error(e);
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

// ===== UI-01 =====
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
  if (raw.some((r) => r === '')) return;
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
  if (!b) return { ok: false, spanOver: false, reason: '' };
  const lon = b.e - b.w, lat = b.n - b.s;
  const legal = lon > 0 && lat > 0;
  const spanOver = lon > 0.25 + 1e-9 || lat > 0.25 + 1e-9;
  return { ok: legal && !spanOver, spanOver, legal, lon, lat };
}

function phaseValid() {
  return !!(S.t1 && S.t2 && S.t2 > S.t1);
}

function updateAoiMetrics() {
  const b = S.aoi;
  const st = aoiStatus();
  const lon = b ? b.e - b.w : 0, lat = b ? b.n - b.s : 0;
  const deg2 = b ? lon * lat : 0;
  const km2 = deg2 * 111.32 * 111.32; // constants.py DEG_TO_KM
  $('span-lon').textContent = b ? lon.toFixed(4) + ' °' : '—';
  $('span-lat').textContent = b ? lat.toFixed(4) + ' °' : '—';
  $('area-deg').innerHTML = b ? deg2.toFixed(6) + ' <small>deg²</small>' : '—';
  $('area-km').innerHTML = b ? km2.toFixed(2) + ' <small>km²</small>' : '—';

  let msg = '';
  if (b && !st.legal) msg = '四至不合法：需满足 东 > 西 且 北 > 南';
  else if (st.spanOver) msg = '[EX1] ' + ERROR_MESSAGES.EX1;
  $('aoi-err').textContent = msg;
  ['aoi-w', 'aoi-s', 'aoi-e', 'aoi-n'].forEach((id) => $(id).classList.toggle('invalid', !!(b && (st.spanOver || !st.legal))));
  updateSubmitState();
}

function updateSubmitState() {
  const st = aoiStatus();
  const ph = phaseValid();
  let pmsg = '';
  if (S.t1 && S.t2 && S.t2 <= S.t1) pmsg = '时相 t2 必须晚于 t1';
  else if (!S.t1 || !S.t2) pmsg = '请选满 t1 与 t2 两个时相';
  $('phase-err').textContent = pmsg;
  $('btn-aoi-submit').disabled = !(st.ok && ph);
}

async function submitAoi() {
  const st = aoiStatus();
  if (!st.ok || !phaseValid()) return;
  const body = { aoi_geom: aoiGeomString(S.aoi), t1: S.t1, t2: S.t2 };
  const btn = $('btn-aoi-submit');
  btn.disabled = true; btn.textContent = '提交中…';
  try {
    const res = await api.selectAoi(body);
    if (res.demo) markDemo();
    S.aoiResult = res.data;
    showToast(res.demo ? 'AOI 已提交（演示数据）' : 'AOI 已提交', 'ok');
    gotoStep(2);
    loadPairs();
  } catch (e) {
    handleErr(e);
  } finally {
    btn.textContent = '提交 AOI · POST /aoi/select';
    updateSubmitState();
  }
}

// ===== UI-02 =====
async function loadPairs() {
  const box = $('pair-list');
  box.innerHTML = '<div class="ok-line">正在获取影像对…</div>';
  try {
    const res = await api.listScenePairs(S.aoiResult && S.aoiResult.aoi_id);
    if (res.demo) markDemo();
    S.pairs = (res.data && res.data.pairs) || [];
    S.pairVia = res.via || '';
    const viaLabel = { GET: 'GET /scene/match', POST: 'POST /scene/match', SEED: '本地演示 seed' }[S.pairVia] || S.pairVia;
    $('pair-via').textContent = '来源：' + viaLabel;
    renderPairs();
    autoSelectPair();
  } catch (e) {
    handleErr(e);
    box.innerHTML = '<div class="err-line">影像对获取失败</div>';
  }
}

function renderPairs() {
  const box = $('pair-list');
  if (!S.pairs.length) {
    box.innerHTML = '<div class="warn-line">未获取到影像对，请检查后端服务。</div>';
    return;
  }
  box.innerHTML = S.pairs.map((p, i) => {
    const t1 = (p.scenes && p.scenes.t1) || {};
    const t2 = (p.scenes && p.scenes.t2) || {};
    const hi1 = Number(t1.cloud_cover) > 50, hi2 = Number(t2.cloud_cover) > 50;
    const sel = S.selectedPair === p ? ' selected' : '';
    const sceneRow = (tag, sc, hi) => `
      <div class="pair-scene">
        <span class="tag ${tag === 't1' ? 'tag-t1' : 'tag-t2'}">${tag.toUpperCase()}</span>
        <span class="dt">${esc(sc.date || '—')}</span>
        <span class="pair-phase">${esc(sc.phase || '')}</span>
        <span class="cloud ${hi ? 'high' : ''}">云量 ${fmtCloud(sc.cloud_cover)}${hi ? ' ⚠' : ''}</span>
      </div>
      <div class="pair-phase" style="padding-left:2px">基线 ${esc(sc.proc_baseline || '—')} · 传感器 ${esc(sc.sensor || '—')}</div>`;
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
  $('btn-scene-confirm').disabled = !S.selectedPair;
}

function renderCaliber() {
  const p = S.selectedPair;
  const panel = $('caliber-panel');
  if (!p) { panel.innerHTML = '<span class="hint">请先选择一组影像对</span>'; return; }
  const baseline = (p.scenes && p.scenes.t2 && p.scenes.t2.proc_baseline) || '05.09';
  panel.innerHTML = `
    <div class="k">公式标识</div><div class="v"><code>S2_L2A_BOA</code></div>
    <div class="k">辐射定标公式</div><div class="v"><code>rho=(DN-1000)/10000</code></div>
    <div class="k">传感器</div><div class="v">S2 · Sentinel-2 L2A</div>
    <div class="k">处理基线版次</div><div class="v">${esc(baseline)}（S2 需 ≥ 04.00，达标）</div>
    <div class="k">分块尺寸</div><div class="v">256 px</div>
    <div class="k">口径状态</div><div class="v" style="color:#8ce0a0">已锁定 · 不可编辑（NF5）</div>`;
}

async function confirmScene() {
  const p = S.selectedPair;
  if (!p) { showToast('请先选择一组影像对', 'warn'); return; }
  const body = {
    aoi_id: (S.aoiResult && S.aoiResult.aoi_id) || '',
    scene_t1_id: (p.scenes && p.scenes.t1 && p.scenes.t1.scene_id) || '',
    scene_t2_id: (p.scenes && p.scenes.t2 && p.scenes.t2.scene_id) || '',
    block_size: 256,
  };
  const btn = $('btn-scene-confirm');
  btn.disabled = true; btn.textContent = '确认中…';
  try {
    const res = await api.matchScene(body);
    if (res.demo) markDemo();
    S.sceneMatch = res.data;
    showToast(res.demo ? '口径已确认（演示数据）' : '口径已确认', 'ok');
    gotoStep(3);
  } catch (e) {
    handleErr(e);
  } finally {
    btn.textContent = '确认影像对与口径 · POST /scene/match';
    btn.disabled = !S.selectedPair;
  }
}

// ===== UI-03 =====
async function loadChange() {
  if (S.changeLoaded) return;
  S.changeLoaded = true;
  const body = { run_id: (S.sceneMatch && S.sceneMatch.run_id) || (S.aoiResult && S.aoiResult.run_id) || '', index: 'NDVI', block_size: 256 };
  try {
    const res = await api.runChange(body);
    if (res.demo) markDemo();
    S.change = res.data;
    let url = S.change.change_png;
    if (!url || /^demo:/.test(url)) url = demo.makeChangePngDataUrl();
    S.changePngUrl = url;
    M.setImage(map, url, S.aoi);
    M.setImageOpacity(map, S.opacity);
    renderValidRatio();
    C.renderPie($('chart-pie'), S.change.stats);
    showToast(res.demo ? '变化检测完成（演示数据）' : '变化检测完成', 'ok');
  } catch (e) {
    handleErr(e);
    S.changeLoaded = false;
  }
}

function renderValidRatio() {
  const r = Number((S.change && S.change.valid_ratio) || 0);
  const v = $('valid-ratio'), note = $('valid-note');
  v.textContent = (r * 100).toFixed(1) + ' %';
  v.className = 'ratio-v';
  note.className = 'ratio-note';
  if (r >= 0.50) {
    v.classList.add('pass'); note.classList.add('pass');
    note.textContent = '✓ 有效像元占比合格（≥ 0.50），结论有效';
    $('btn-to-u4').disabled = false;
  } else if (r >= 0.10) {
    v.classList.add('warn'); note.classList.add('warn');
    note.textContent = '⚠ 有效数据偏低，结论仅供参考　[EX5] ' + ERROR_MESSAGES.EX5;
    $('btn-to-u4').disabled = false;
  } else {
    v.classList.add('stop'); note.classList.add('stop');
    note.textContent = '⛔ 有效像元不足，本轮检测终止　[EX11] ' + ERROR_MESSAGES.EX11;
    $('btn-to-u4').disabled = true;
    showError({ code: 'EX11', message: ERROR_MESSAGES.EX11, run_id: S.change && S.change.run_id });
  }
}

// ===== UI-04 =====
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
  $('btn-accuracy').disabled = !allOk;
  $('ex10-hint').textContent = allOk
    ? '✓ 三类样本均已达到每类 30 个，可计算精度'
    : '[EX10] ' + ERROR_MESSAGES.EX10;
  $('ex10-hint').style.color = allOk ? '#8ce0a0' : 'var(--warn)';
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
  if (!b) return;
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
  showToast('已补齐演示样本（每类 32 个，演示数据）', 'ok');
}

async function calcAccuracy() {
  const samples = S.samples.map((p) => ({ x: p.x, y: p.y, ref_class: demo.CLASS_INFO[p.cls].ref }));
  const body = { run_id: (S.aoiResult && S.aoiResult.run_id) || '', samples };
  const btn = $('btn-accuracy');
  btn.disabled = true; btn.textContent = '计算中…';
  try {
    const res = await api.submitSamples(body);
    if (res.demo) markDemo();
    S.accuracy = res.data;
    renderAccuracy();
    $('btn-to-u5').disabled = false;
    showToast(res.demo ? '精度评价完成（演示数据）' : '精度评价完成', 'ok');
  } catch (e) {
    handleErr(e);
  } finally {
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

// ===== UI-05 =====
function renderSnapshot() {
  const ps = (S.change && S.change.params_snapshot) || {};
  const commit = ps.commit || (S.exportResult && S.exportResult.commit) || '—';
  const formula = (ps.formula_id || 'S2_L2A_BOA') + (ps.formula_text ? ' · ' + ps.formula_text : '');
  const rows = [
    ['run_id', String((S.change && S.change.run_id) || (S.aoiResult && S.aoiResult.run_id) || '—'), true],
    ['commit（代码版次）', String(commit), true],
    ['辐射定标公式', formula, true],
    ['处理基线版次', String(ps.proc_baseline || '—'), false],
    ['变化阈值 ΔNDVI', String(ps.threshold != null ? ps.threshold : '—'), false],
    ['分块尺寸 (px)', String(ps.block_size != null ? ps.block_size : '—'), false],
    ['指数类型', String(ps.index || 'NDVI'), false],
  ];
  $('snapshot-table').innerHTML = '<tbody>' + rows.map(([k, v, code]) =>
    `<tr><td class="k">${esc(k)}</td><td>${code ? '<code>' + esc(v) + '</code>' : esc(v)}</td></tr>`).join('') + '</tbody>';
}

function renderFiles(files, isDemo) {
  const box = $('export-files');
  if (!files || !files.length) { box.innerHTML = '<div class="ok-line" style="color:var(--muted2);font-size:11px">尚无导出文件</div>'; return; }
  box.innerHTML = files.map((f) => {
    const size = f.size != null ? (f.size > 1024 ? (f.size / 1024).toFixed(1) + ' KB' : f.size + ' B') : '—';
    const tag = (isDemo || f.demo) ? '<span style="font-size:10px;font-weight:800;color:#231700;background:#f5c451;border-radius:4px;padding:1px 6px;white-space:nowrap">演示数据</span>' : '';
    const ph = f.placeholder ? '<span style="color:var(--warn);font-size:10px;white-space:nowrap">占位</span>' : '';
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

async function doExport(formats) {
  const fmts = formats.map((f) => String(f).toLowerCase());
  // 前端格式白名单（REQ-12 / EX6）：越权格式即时按内置映射展示，不臆造语义
  const bad = fmts.find((f) => !ALLOW_FORMATS.includes(f));
  if (bad) {
    showError({ code: 'EX6', message: ERROR_MESSAGES.EX6, run_id: (S.change && S.change.run_id) || null });
    return;
  }
  const body = { run_id: (S.aoiResult && S.aoiResult.run_id) || '', formats: fmts };
  try {
    const res = await api.exportReport(body);
    if (res.demo) markDemo();
    S.exportResult = res.data;
    renderFiles(res.data.files, res.demo);
    (res.data.files || []).forEach(triggerDownload);
    renderSnapshot();
    showToast(res.demo ? '导出完成（演示数据，文件已标注）' : '导出完成', 'ok');
  } catch (e) {
    handleErr(e);
  }
}

// ===== 流程切换 =====
function gotoStep(n) {
  S.maxStep = Math.max(S.maxStep, n);
  S.step = n;
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', +v.dataset.step === n));
  renderSteps();
  $('map-hint').textContent = HINTS[n];
  $('map-mode').textContent = MODES[n];
  $('map-legend').hidden = !(n >= 3 && n <= 5);
  if (n === 3) loadChange();
  if (n === 5) {
    C.renderBar($('chart-bar'), S.change && S.change.stats);
    renderSnapshot();
  }
  window.setTimeout(() => {
    try { if (map) map.resize(); } catch (_) { /* style 未就绪 */ }
    C.resizeCharts();
  }, 60);
  $('panel').scrollTop = 0;
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
      updateAoiMetrics(); updateSubmitState();
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

  $('t1').onchange = () => { S.t1 = $('t1').value; updateSubmitState(); };
  $('t2').onchange = () => { S.t2 = $('t2').value; updateSubmitState(); };
  $('btn-aoi-submit').onclick = submitAoi;

  $('btn-scene-confirm').onclick = confirmScene;

  $('opacity').addEventListener('input', () => {
    const v = Number($('opacity').value);
    S.opacity = v / 100;
    $('opacity-val').textContent = v + '%';
    M.setImageOpacity(map, S.opacity);
  });
  $('btn-to-u4').onclick = () => gotoStep(4);

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
  $('btn-to-u5').onclick = () => gotoStep(5);

  $('btn-exp-pdf').onclick = () => doExport(['pdf']);
  $('btn-exp-png').onclick = () => doExport(['png']);
  $('btn-exp-tif').onclick = () => doExport(['geotiff']);
  $('btn-exp-custom').onclick = () => {
    const v = $('custom-format').value.trim();
    if (!v) { showToast('请输入要校验的导出格式', 'warn'); return; }
    doExport([v]);
  };
}

function boundsFromArray(a) { return { w: a[0], s: a[1], e: a[2], n: a[3] }; }

function bindMapEvents() {
  // 矩形框选（仅 UI-01）
  let drawing = false, moved = false, startPx = null, startLL = null;
  map.on('mousedown', (e) => {
    if (S.step !== 1) return;
    drawing = true; moved = false;
    startPx = { x: e.point.x, y: e.point.y };
    startLL = map.unproject(e.point);
    pushAoiHistory();
    map.dragPan.disable();
  });
  map.on('mousemove', (e) => {
    if (!drawing || S.step !== 1) return;
    const d = Math.hypot(e.point.x - startPx.x, e.point.y - startPx.y);
    if (d <= 4) return;
    moved = true;
    applyAoi(normBounds(startLL, map.unproject(e.point)));
  });
  const endDraw = () => {
    if (!drawing) return;
    drawing = false;
    map.dragPan.enable();
    if (moved) updateSubmitState();
  };
  window.addEventListener('mouseup', endDraw);

  // 样本点打点 / 删除（仅 UI-04）
  map.on('click', (e) => {
    if (S.step !== 4) return;
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
  buildPanel();
  buildLegend();
  renderSteps();
  bindEvents();

  map = M.createMap('map', demo.SEEDS[0].center, 9);
  map.on('load', () => {
    fillAoiInputs(boundsFromArray(demo.SEEDS[0].aoi));
    syncFromInputs();
    M.fitBounds(map, S.aoi, 90);
  });
  bindMapEvents();

  updateCounts();
  renderSnapshot();
  gotoStep(1);
}

init();
