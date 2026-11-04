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

// 本地生成的经纬网（覆盖 3 组 AOI 大致范围），无任何外部服务
function graticuleGeoJSON() {
  const w = 115.8, s = 28.0, e = 116.8, n = 29.6, step = 0.05;
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

function frameGeoJSON() {
  const w = 115.8, s = 28.0, e = 116.8, n = 29.6;
  return {
    type: 'FeatureCollection',
    features: [{
      type: 'Feature', properties: {},
      geometry: { type: 'LineString', coordinates: [[w, s], [e, s], [e, n], [w, n], [w, s]] },
    }],
  };
}

function initLayers(map) {
  // 经纬网 + 范围边框
  map.addSource('graticule', { type: 'geojson', data: graticuleGeoJSON() });
  map.addLayer({
    id: 'graticule-line', type: 'line', source: 'graticule',
    paint: { 'line-color': '#ccd7e3', 'line-width': 0.6, 'line-opacity': 0.9 },
  });
  map.addSource('graticule-frame', { type: 'geojson', data: frameGeoJSON() });
  map.addLayer({
    id: 'frame-line', type: 'line', source: 'graticule-frame',
    paint: { 'line-color': '#aebfce', 'line-width': 1.2, 'line-opacity': 0.9 },
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
  map.on('load', () => initLayers(map));
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

// 专题图 image source：coordinates = [左上, 右上, 右下, 左下]
export function setImage(map, url, b) {
  if (!url || !b) return;
  const coordinates = [[b.w, b.n], [b.e, b.n], [b.e, b.s], [b.w, b.s]];
  const src = map.getSource('change-img');
  if (src && typeof src.updateImage === 'function') {
    src.updateImage({ url, coordinates });
    return;
  }
  if (!src) {
    map.addSource('change-img', { type: 'image', url, coordinates });
    const layer = {
      id: 'change-img-layer', type: 'raster', source: 'change-img',
      paint: { 'raster-opacity': 0.85, 'raster-fade-duration': 0 },
    };
    const before = map.getLayer('aoi-fill') ? 'aoi-fill' : undefined;
    if (before) map.addLayer(layer, before);
    else map.addLayer(layer);
  }
}

export function setImageOpacity(map, o) {
  if (map.getLayer('change-img-layer')) {
    map.setPaintProperty('change-img-layer', 'raster-opacity', o);
  }
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
