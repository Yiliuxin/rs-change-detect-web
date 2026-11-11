// -*- coding: utf-8 -*-
// MapLibre 图层封装：严格离线空白底图 + 本地生成的经纬网 + AOI / 专题图 image source / 样本点图层。
// 底图 style 的 sources 为空对象、仅一个 background 图层；运行时图层全部通过 addSource/addLayer 追加，
// 数据来源均为本地 GeoJSON 或 image source（后端 PNG / 前端 canvas dataURL），无任何在线瓦片。

import maplibregl from 'maplibre-gl';

// 硬约束：纯本地空白底图（浅色专业 GIS 风格）
export const BLANK_STYLE = {
  version: 8,
  sources: {},
  layers: [
    { id: 'bg', type: 'background', paint: { 'background-color': '#e9eef4' } },
  ],
};

function emptyFC() {
  return { type: 'FeatureCollection', features: [] };
}

// 视口自适应经纬网：按缩放级别挑步长，横向最多 20 条线。
// 以前是写死南昌/鄱阳一带的固定范围，选到别的省份就没有格网作参照。
const GRID_STEPS = [10, 5, 2, 1, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01];
function graticuleForBounds(bounds) {
  const w = bounds.getWest(), e = bounds.getEast();
  const s = bounds.getSouth(), n = bounds.getNorth();
  const span = Math.max(e - w, 1e-6);
  const step = GRID_STEPS.find((v) => span / v <= 20) || GRID_STEPS[GRID_STEPS.length - 1];
  const lines = [];
  for (let x = Math.ceil(w / step) * step; x <= e + 1e-9; x += step) {
    lines.push([[Number(x.toFixed(4)), s], [Number(x.toFixed(4)), n]]);
  }
  for (let y = Math.ceil(s / step) * step; y <= n + 1e-9; y += step) {
    lines.push([[w, Number(y.toFixed(4))], [e, Number(y.toFixed(4))]]);
  }
  return {
    type: 'FeatureCollection',
    features: [{ type: 'Feature', properties: {}, geometry: { type: 'MultiLineString', coordinates: lines } }],
  };
}

// 重算经纬网（moveend / 首次 load 时调用）
export function refreshGraticule(map) {
  const src = map.getSource('graticule');
  if (!src || typeof map.getBounds !== 'function') return;
  try {
    src.setData(graticuleForBounds(map.getBounds()));
  } catch (_) { /* 地图尚未就绪 */ }
}

// ===== B1 图层控制：t1 / t2 / change 三层（添加顺序 t1→t2→change，专题图在最上） =====
// 保持历史 source/layer id（change-img / change-img-layer）不变，setImage 仍是薄封装。
const IMAGE_LAYERS = {
  t1: { source: 't1-img', layer: 't1-img-layer', opacity: 1.0 },
  t2: { source: 't2-img', layer: 't2-img-layer', opacity: 1.0 },
  change: { source: 'change-img', layer: 'change-img-layer', opacity: 0.85 },
};
const LAYER_ORDER = ['t1', 't2', 'change'];
const _layerState = new WeakMap();

function stateFor(map) {
  let s = _layerState.get(map);
  if (!s) {
    s = {
      visible: { t1: true, t2: true, change: true },
      opacity: { t1: 1.0, t2: 1.0, change: 0.85 },
      bounds: null,
      pending: { t1: null, t2: null, change: null },
    };
    _layerState.set(map, s);
  }
  return s;
}

function imageCoordinates(b) {
  return [[b.w, b.n], [b.e, b.n], [b.e, b.s], [b.w, b.s]];
}

// 无论三层首次设置的先后，都插到「第一个已存在的更上层」之前；
// 都不存在时压在 AOI 线 / 样本点之下 → 最终顺序恒为 t1 → t2 → change。
function addLayerOrdered(map, key) {
  const def = IMAGE_LAYERS[key];
  if (!def || map.getLayer(def.layer)) return;
  const later = LAYER_ORDER.slice(LAYER_ORDER.indexOf(key) + 1)
    .map((k) => IMAGE_LAYERS[k].layer)
    .find((id) => map.getLayer(id));
  const before = later || (map.getLayer('aoi-fill') ? 'aoi-fill' : undefined);
  const s = stateFor(map);
  const layer = {
    id: def.layer, type: 'raster', source: def.source,
    layout: { visibility: s.visible[key] ? 'visible' : 'none' },
    paint: { 'raster-opacity': s.opacity[key], 'raster-fade-duration': 0 },
  };
  if (before) map.addLayer(layer, before);
  else map.addLayer(layer);
}

// 图源首次直接用真实 URL 建立：避免占位图请求被 updateImage 中止而抛 AbortError。
// 已有图源时等上一张图加载完成再更新，同样避免中止在途请求。
function applyLayerImage(map, key, url, b) {
  const s = stateFor(map);
  const def = IMAGE_LAYERS[key];
  const src = map.getSource(def.source);
  if (!src) {
    map.addSource(def.source, { type: 'image', url, coordinates: imageCoordinates(b) });
  } else {
    const loaded = (typeof src.loaded === 'function') ? src.loaded() : true;
    if (!loaded) return false;
    if (src.url !== url && typeof src.updateImage === 'function') {
      src.updateImage({ url, coordinates: imageCoordinates(b) });
    }
  }
  addLayerOrdered(map, key);
  s.pending[key] = null;
  setLayerVisible(map, key, s.visible[key]);
  setLayerOpacity(map, key, s.opacity[key]);
  return true;
}

function initLayers(map) {
  // 经纬网：随视口动态生成（原来固定写死在南昌/鄱阳一带，移到别的省份就没格网了）
  map.addSource('graticule', { type: 'geojson', data: emptyFC() });
  map.addLayer({
    id: 'graticule-line', type: 'line', source: 'graticule',
    paint: { 'line-color': '#ccd7e3', 'line-width': 0.6, 'line-opacity': 0.9 },
  });

  // 中国省级边界（离线 GeoJSON，由 setProvinceBoundaries 注入；含台湾省与南海诸岛）
  // 只有线要素，不铺填充，避免盖住背景色与专题图
  map.addSource('cn-provinces', { type: 'geojson', data: emptyFC() });
  map.addLayer({
    id: 'cn-provinces-line', type: 'line', source: 'cn-provinces',
    paint: { 'line-color': '#9fb2c4', 'line-width': ['interpolate', ['linear'], ['zoom'], 3, 0.7, 8, 1.3], 'line-opacity': 0.9 },
  });

  // 全国地点库（省/市/县三级）：用 circle 图层画点（无需字形服务，离线可用），
  // 中文名称标签由 DOM Marker 单独维护，仅渲染当前视口内的一小批，避免上千个 DOM 卡顿。
  map.addSource('cn-places', { type: 'geojson', data: emptyFC() });
  map.addLayer({
    id: 'cn-place-t3', type: 'circle', source: 'cn-places', minzoom: 7.2,
    filter: ['==', ['get', 't'], 3],
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 7.2, 2.4, 11, 4],
      'circle-color': '#8a9bb0', 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1,
      'circle-opacity': 0.95,
    },
  });
  map.addLayer({
    id: 'cn-place-t2', type: 'circle', source: 'cn-places', minzoom: 5,
    filter: ['==', ['get', 't'], 2],
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 2.8, 10, 4.6],
      'circle-color': '#3f7fd0', 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1.2,
      'circle-opacity': 0.95,
    },
  });
  map.addLayer({
    id: 'cn-place-t1', type: 'circle', source: 'cn-places',
    filter: ['==', ['get', 't'], 1],
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, 3.6, 9, 5.4],
      'circle-color': '#0b4aa2', 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1.4,
      'circle-opacity': 1,
    },
  });

  // AOI 矩形（框选 / 手填联动）
  map.addSource('aoi', { type: 'geojson', data: emptyFC() });
  map.addLayer({
    id: 'aoi-fill', type: 'fill', source: 'aoi',
    paint: { 'fill-color': '#1f7ae0', 'fill-opacity': 0.12 },
  });
  map.addLayer({
    id: 'aoi-line', type: 'line', source: 'aoi',
    paint: { 'line-color': '#1f7ae0', 'line-width': 2, 'line-dasharray': [2, 1.4] },
  });

  // 样本点（按类别着色，支持点选删除）
  map.addSource('samples', { type: 'geojson', data: emptyFC() });
  map.addLayer({
    id: 'sample-halo', type: 'circle', source: 'samples',
    paint: { 'circle-radius': 9.5, 'circle-color': '#1f7ae0', 'circle-opacity': 0.14 },
  });
  map.addLayer({
    id: 'sample-circles', type: 'circle', source: 'samples',
    paint: {
      'circle-radius': 5.5,
      'circle-color': ['match', ['get', 'cls'], 1, '#21a04c', 2, '#d94c3d', 3, '#5b7a99', '#ffffff'],
      'circle-stroke-color': '#ffffff',
      'circle-stroke-width': 1.5,
      'circle-opacity': 0.95,
    },
  });
  // B1：三层图片源在首次 setLayerImage 时按序懒创建（专题图恒在最上）
}

export function createMap(containerId, center, zoom) {
  const map = new maplibregl.Map({
    container: containerId,
    style: BLANK_STYLE,
    center: center || [116.3, 29.2],
    zoom: zoom || 9,
    attributionControl: false,
    dragRotate: false,
    pitchWithRotate: false,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false, visualizePitch: false }), 'top-right');
  map.on('load', () => {
    initLayers(map);
    refreshGraticule(map);
    // 视口变化统一重算：经纬网跟随、地名标签按层级与屏幕范围重取
    map.on('moveend', () => {
      refreshGraticule(map);
      syncChinaLabels(map);
    });
  });
  return map;
}

// 更新 AOI 矩形；b = {w,s,e,n} 或 null（清空）
export function setAoi(map, b) {
  const src = map.getSource('aoi');
  if (!src) return;
  const ring = [[b.w, b.s], [b.e, b.s], [b.e, b.n], [b.w, b.n], [b.w, b.s]];
  src.setData({
    type: 'FeatureCollection',
    features: b ? [{ type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [ring] } }] : [],
  });
}

// 设置某一层的图片源（t1 / t2 / change）；b = {w,s,e,n} WGS84 外接矩形
export function setLayerImage(map, key, url, b) {
  if (!IMAGE_LAYERS[key] || !url || !b) return;
  const s = stateFor(map);
  s.bounds = b;
  s.pending[key] = { url, b };
  const run = () => {
    const pending = s.pending[key];
    if (!pending) return true;
    try {
      return applyLayerImage(map, key, pending.url, pending.b);
    } catch (_) {
      return false;
    }
  };
  if (run()) return;
  // style 未就绪 / 上一张图仍在加载：有限次重试，不残留事件监听
  let tries = 0;
  const retry = () => {
    if (!s.pending[key]) return;
    if (run()) return;
    tries += 1;
    if (tries < 20) setTimeout(retry, 250);
  };
  setTimeout(retry, 150);
}

export function setLayerVisible(map, key, visible) {
  if (!IMAGE_LAYERS[key]) return;
  const s = stateFor(map);
  s.visible[key] = !!visible;
  const def = IMAGE_LAYERS[key];
  if (map.getLayer(def.layer)) {
    map.setLayoutProperty(def.layer, 'visibility', s.visible[key] ? 'visible' : 'none');
  }
}

export function setLayerOpacity(map, key, o) {
  if (!IMAGE_LAYERS[key]) return;
  const s = stateFor(map);
  const v = Number(o);
  if (Number.isFinite(v)) s.opacity[key] = Math.max(0, Math.min(1, v));
  const def = IMAGE_LAYERS[key];
  if (map.getLayer(def.layer)) {
    map.setPaintProperty(def.layer, 'raster-opacity', s.opacity[key]);
  }
}

// 历史回看薄封装：语义与旧版完全一致（只作用于专题图 change 层）
export function setImage(map, url, b) {
  setLayerImage(map, 'change', url, b);
}

export function setImageOpacity(map, o) {
  setLayerOpacity(map, 'change', o);
}

// ===== 地名标注：纯 DOM Marker（离线空白底图无 glyphs 字体端点，symbol 文本层不可用） =====
// 数据来自前端内置地名库与后端预置影像区中心点，零网络请求；元素 pointer-events:none，
// 不拦截地图拖拽 / AOI 框选。kind='preset' 为预置影像区（蓝色强调），其余为通用地名。
const _placeMarkers = [];
export function setPlaceMarkers(map, places) {
  _placeMarkers.splice(0).forEach((m) => m.remove());
  (places || []).forEach((p) => {
    if (!p || !Array.isArray(p.center) || !Number.isFinite(p.center[0]) || !Number.isFinite(p.center[1])) return;
    if (!p.name) return;
    const el = document.createElement('div');
    el.className = 'place-marker' + (p.kind === 'preset' ? ' place-marker-preset' : '');
    const dot = document.createElement('span');
    dot.className = 'place-dot';
    const label = document.createElement('span');
    label.className = 'place-label';
    label.textContent = p.name; // textContent 防注入，不用 innerHTML
    el.appendChild(dot);
    el.appendChild(label);
    _placeMarkers.push(new maplibregl.Marker({ element: el, anchor: 'left' }).setLngLat(p.center).addTo(map));
  });
}

export function setSamples(map, list) {
  const src = map.getSource('samples');
  if (!src) return;
  src.setData({
    type: 'FeatureCollection',
    features: list.map((p) => ({
      type: 'Feature',
      properties: { id: p.id, cls: p.cls },
      geometry: { type: 'Point', coordinates: [p.x, p.y] },
    })),
  });
}

export function fitBounds(map, b, padding = 90) {
  if (!b) return;
  map.fitBounds([[b.w, b.s], [b.e, b.n]], { padding, duration: 600, maxZoom: 13 });
}

// ===== 全国地点库图层（省 / 地级市 / 区县）=====
// 点位用 circle 图层绘制：不依赖字形服务，几千个点也不掉帧。
// 中文名标签走 DOM Marker，但只渲染当前视口内的有限数量（见 drawChinaLabels），
// 否则 3000+ 个 DOM 元素会让拖动明显卡顿。

const CHINA_LAYER_IDS = ['cn-place-t1', 'cn-place-t2', 'cn-place-t3'];
const _chinaState = new WeakMap();

function chinaState(map) {
  let s = _chinaState.get(map);
  if (!s) {
    s = { places: [], markers: [], timer: 0 };
    _chinaState.set(map, s);
  }
  return s;
}

export function setProvinceBoundaries(map, fc) {
  const src = map.getSource('cn-provinces');
  if (src) src.setData(fc || emptyFC());
}

// places: [{ name, lng, lat, tier, province }]
export function setChinaPlaces(map, places) {
  const list = (places || []).filter((p) => p && Number.isFinite(p.lng) && Number.isFinite(p.lat));
  chinaState(map).places = list;
  const src = map.getSource('cn-places');
  if (src) {
    src.setData({
      type: 'FeatureCollection',
      features: list.map((p, i) => ({
        type: 'Feature',
        properties: { i, t: p.tier || 3 },
        geometry: { type: 'Point', coordinates: [p.lng, p.lat] },
      })),
    });
  }
  syncChinaLabels(map);
}

// 点击点位回调：handler({ name, lng, lat, tier, province })
export function onPlacePick(map, handler) {
  if (typeof handler !== 'function') return;
  CHINA_LAYER_IDS.forEach((id) => {
    map.on('click', id, (ev) => {
      const f = ev.features && ev.features[0];
      if (!f) return;
      const p = chinaState(map).places[f.properties.i];
      if (p) handler(p);
    });
    map.on('mouseenter', id, () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', id, () => { map.getCanvas().style.cursor = ''; });
  });
}

// 标签层级：放大到不同级别逐级放开（省会 → 地级市 → 区县），避免一屏挤满文字
function labelTiers(z) {
  if (z >= 7.2) return [1, 2, 3];
  if (z >= 5) return [1, 2];
  return [1];
}

// moveend 会连续触发，合并成一次重绘
export function syncChinaLabels(map) {
  const st = chinaState(map);
  if (!st.places.length || typeof map.getZoom !== 'function') return;
  if (st.timer) clearTimeout(st.timer);
  st.timer = setTimeout(() => { st.timer = 0; drawChinaLabels(map); }, 90);
}

function drawChinaLabels(map) {
  const st = chinaState(map);
  if (!st.places.length) return;
  const z = map.getZoom();
  const tiers = labelTiers(z);
  const b = map.getBounds();
  const limit = z >= 7.2 ? 110 : z >= 5 ? 80 : 60;
  // places 已按层级排序，先入选省会，超限时丢掉的是县级
  const picked = [];
  for (const p of st.places) {
    if (!tiers.includes(p.tier || 3)) continue;
    if (p.lng < b.getWest() || p.lng > b.getEast()) continue;
    if (p.lat < b.getSouth() || p.lat > b.getNorth()) continue;
    picked.push(p);
    if (picked.length >= limit) break;
  }
  st.markers.splice(0).forEach((m) => m.remove());
  picked.forEach((p) => {
    const el = document.createElement('div');
    el.className = 'place-marker place-pin place-t' + (p.tier || 3);
    const label = document.createElement('span');
    label.className = 'place-label';
    label.textContent = p.name; // textContent 防注入
    el.appendChild(label);
    st.markers.push(new maplibregl.Marker({ element: el, anchor: 'left', offset: [6, 0] })
      .setLngLat([p.lng, p.lat]).addTo(map));
  });
}
