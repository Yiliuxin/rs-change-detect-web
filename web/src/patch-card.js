// -*- coding: utf-8 -*-
// 批次 A · 图斑详情卡（前端为主）：点击专题图 polygon 层弹出，绝对定位在地图容器内。
// 类别 / 面积 / 强度档 / run_id + t1、t2 真彩合成缩略（走带 token 的 artifactObjectUrl
// Blob 通道，加载失败显示占位文字）。点击地图空白处或关闭按钮收起；卡片本身很小，
// 不使用全屏遮罩，地图上其它位置的拖拽 / 框选 / 打点均不受影响。

import * as api from './api.js';
import * as M from './map.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

const CLASS_ZH = { 1: '增强', 2: '退化', 3: '稳定', 4: '过渡带（不确定）' };
const CLASS_COLOR = { 1: '#21a04c', 2: '#d94c3d', 3: '#5b7a99', 4: '#969696' };
const INTENSITY_ZH = {
  1: '显著退化', 2: '轻微退化', 3: '稳定', 4: '轻微改善', 5: '显著改善',
};
const NO_THUMB = '合成图暂不可用';

let state = null;
let loadSeq = 0;   // 换 run 时丢弃在途的 GeoJSON 加载
let cardSeq = 0;   // 换图斑时丢弃在途的缩略图加载

function whenReady(map, fn) {
  if (!map) return;
  if (typeof map.getSource === 'function' && map.getSource('change-patches')) { fn(); return; }
  if (typeof map.once === 'function') map.once('load', fn);
}

function hideCard() {
  cardSeq += 1;
  const card = $('patch-card');
  if (card) card.hidden = true;
}

// GeoJSON 缺 area_ha 时的近似兜底：WGS84 等距投影的球面面积（无新依赖）
function ringAreaHa(geometry) {
  if (!geometry || !Array.isArray(geometry.coordinates) || !geometry.coordinates.length) return null;
  const rings = geometry.coordinates;
  const area = (ring) => {
    let s = 0;
    for (let i = 0; i < ring.length - 1; i += 1) {
      const a = ring[i], b = ring[i + 1];
      s += Number(a[0]) * Number(b[1]) - Number(b[0]) * Number(a[1]);
    }
    return Math.abs(s) / 2;
  };
  let deg2 = area(rings[0]);
  for (let i = 1; i < rings.length; i += 1) deg2 -= area(rings[i]);
  const lat = rings[0].reduce((acc, p) => acc + Number(p[1] || 0), 0) / Math.max(1, rings[0].length);
  const m2 = deg2 * 111320 * 111320 * Math.cos(lat * Math.PI / 180);
  return Number.isFinite(m2) && m2 >= 0 ? m2 / 10000 : null;
}

// 逐斑块 Δ 不在 GeoJSON 属性里，强度档取本轮该类五档中的主档（真实统计，非臆造）
function dominantIntensity(intensity, cls) {
  if (cls === 4) return null; // 过渡带证据不足，不套用任何强度主档
  const groups = cls === 1 ? [4, 5] : (cls === 2 ? [1, 2] : [3]);
  const levels = (intensity && Array.isArray(intensity.levels)) ? intensity.levels : [];
  const hit = levels.filter((lv) => groups.includes(Number(lv && lv.code)));
  if (!hit.length) return null;
  const best = hit.reduce((a, b) => (Number(b.pixel_count || 0) > Number(a.pixel_count || 0) ? b : a));
  const code = Number(best.code);
  return { code, label: String(best.label || INTENSITY_ZH[code] || '—') };
}

function renderThumbs(thumbs, arts, seq) {
  thumbs.innerHTML = '';
  [['t1 真彩', arts['t1_composite.png']], ['t2 真彩', arts['t2_composite.png']]].forEach(([label, url]) => {
    const box = document.createElement('div');
    box.className = 'patch-thumb';
    const slot = document.createElement('div');
    slot.className = 'patch-thumb-img';
    const cap = document.createElement('div');
    cap.className = 'patch-thumb-cap';
    cap.textContent = label;
    box.appendChild(slot);
    box.appendChild(cap);
    thumbs.appendChild(box);

    const placeholder = () => {
      if (seq !== cardSeq) return;
      slot.textContent = NO_THUMB;
      slot.classList.add('is-placeholder');
    };
    if (!url) { placeholder(); return; }
    Promise.resolve(api.artifactObjectUrl(url))
      .then((blobUrl) => {
        if (seq !== cardSeq) return;
        if (!blobUrl) { placeholder(); return; }
        const img = document.createElement('img');
        img.alt = label;
        img.onerror = placeholder;
        img.src = blobUrl;
        slot.appendChild(img);
      })
      .catch(placeholder);
  });
}

function showCard(feature, point) {
  if (state && typeof state.canShow === 'function' && !state.canShow()) return;
  const card = $('patch-card'), body = $('patch-card-body'), thumbs = $('patch-thumbs');
  if (!card || !body || !thumbs) return;
  const p = (feature && feature.properties) || {};
  const cls = Number(p.change_class)
    || (p.class_zh === '增强' ? 1 : (p.class_zh === '退化' ? 2 : 3));
  const area = Number(p.area_ha);
  const fallback = ringAreaHa(feature && feature.geometry);
  const areaText = (Number.isFinite(area) && area > 0)
    ? area.toFixed(2) + ' ha'
    : (fallback != null ? '约 ' + fallback.toFixed(2) + ' ha' : '—');
  const fromProp = Number(p.intensity_level) > 0;
  const dom = fromProp
    ? { code: Number(p.intensity_level), label: String(p.intensity_zh || INTENSITY_ZH[Number(p.intensity_level)] || '—') }
    : dominantIntensity(state && state.getIntensity ? state.getIntensity() : null, cls);
  const intText = dom ? dom.code + ' · ' + dom.label : '—';
  const rid = state && state.getRunId ? String(state.getRunId() || '') : '';
  const color = CLASS_COLOR[cls] || '#8aa2bd';
  // 批次 B2：三项形态指标与 Top10 表 / GeoJSON properties 同源；老产物缺字段显示 —
  const metric = (v, d, unit) => (v == null || v === '' || !Number.isFinite(Number(v)))
    ? '—' : Number(v).toFixed(d) + unit;

  body.innerHTML = `
    <div class="patch-row"><span>类别</span><b style="color:${color}">${esc(CLASS_ZH[cls] || p.class_zh || '—')}</b></div>
    <div class="patch-row"><span>面积</span><b>${esc(areaText)}</b></div>
    <div class="patch-row"><span>强度档</span><b>${esc(intText)}</b></div>
    <div class="patch-row"><span>周长</span><b>${esc(metric(p.perimeter_m, 1, ' m'))}</b></div>
    <div class="patch-row"><span>紧凑度</span><b>${esc(metric(p.compactness, 3, ''))}</b></div>
    <div class="patch-row"><span>主走向</span><b>${esc(metric(p.orientation_deg, 1, '°'))}</b></div>
    <div class="patch-row"><span>run_id</span><b title="${esc(rid)}">${esc(rid ? rid.slice(0, 8) + '…' : '—')}</b></div>
    <div class="patch-note">强度档按本轮同类主档；周长/紧凑度/主走向为 111320·cos(lat) 近似口径</div>`;

  const map = state && state.map;
  const container = (map && typeof map.getContainer === 'function') ? map.getContainer() : null;
  const w = container ? container.clientWidth : 800;
  const h = container ? container.clientHeight : 600;
  const px = point ? point.x : w - 260;
  const py = point ? point.y : 60;
  card.style.left = Math.max(12, Math.min(px + 14, w - 250)) + 'px';
  card.style.top = Math.max(52, Math.min(py + 12, h - 250)) + 'px';
  card.hidden = false;

  const seq = ++cardSeq;
  const arts = (state && state.getArtifacts && state.getArtifacts()) || {};
  renderThumbs(thumbs, arts, seq);
}

/** 载入 change.geojson（经 artifactObjectUrl 取 Blob 后解析），并按需显示 polygon 层。 */
export function loadPatches(map, url) {
  const seq = ++loadSeq;
  hideCard();
  const clear = () => whenReady(map, () => { if (seq === loadSeq) M.setChangePatches(map, null); });
  if (!url) { clear(); return; }
  (async () => {
    try {
      const blobUrl = await api.artifactObjectUrl(url);
      if (seq !== loadSeq) return;
      if (!blobUrl) { clear(); return; }
      const resp = await fetch(blobUrl);
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const fc = await resp.json();
      if (seq !== loadSeq) return;
      whenReady(map, () => { if (seq === loadSeq) M.setChangePatches(map, fc); });
    } catch (_) {
      if (seq === loadSeq) clear();
    }
  })();
}

/** 绑定 polygon 点击 / 空白点击 / 关闭按钮；调用一次即可。 */
export function initPatchCard(opts) {
  const o = opts || {};
  state = {
    map: o.map,
    getRunId: o.getRunId,
    getArtifacts: o.getArtifacts,
    getIntensity: o.getIntensity,
    canShow: o.canShow,
  };
  const map = o.map;
  if (!map) return;
  M.onPatchClick(map, (feature, point) => showCard(feature, point));
  map.on('click', (e) => {
    const feats = map.queryRenderedFeatures(e.point, { layers: ['change-patch-fill'] });
    if (!feats || !feats.length) hideCard();
  });
  const close = $('patch-card-close');
  if (close) close.onclick = hideCard;
}
