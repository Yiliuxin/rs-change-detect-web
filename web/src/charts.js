// -*- coding: utf-8 -*-
// ECharts 图表：右栏「检测结果」三类面积占比饼图（REQ-08/11）与三类面积柱状图（REQ-11）。
// 数据来自 /result/change 的 stats，本地渲染保证首屏 < 5s（NF2）。浅色专业主题。

import * as echarts from 'echarts';

const COLORS = { 1: '#21a04c', 2: '#d94c3d', 3: '#5b7a99' };
const NAMES = { 1: '增强', 2: '退化', 3: '稳定' };

let pie = null;
let bar = null;
let intensityBar = null;
let sankey = null;
let compareBar = null;
let deltaHist = null;
// 批次 E：方法对比分组条形图 / 阈值敏感性折线图（独立实例，不与批次 A compareBar 复用）
let methodGroups = null;
let sensitivityLine = null;

// 桑基节点沿用既有三类主色；A3 地物判据的第 4 类用稳定蓝浅色变体，不引入新色系。
const SANKEY_COLORS = { ...COLORS, 4: '#8aa2bd' };

// A1 五档（与后端 constants.py INTENSITY_CODES / 文案一一对应，仅用于展示）
const INTENSITY_ZH = { 1: '显著退化', 2: '轻微退化', 3: '稳定', 4: '轻微改善', 5: '显著改善' };
const INTENSITY_COLORS = {
  1: '#8e2c25', 2: '#e4a23c', 3: '#788ca0', 4: '#67b86b', 5: '#1e8a45',
};

function normalizeStats(stats) {
  const acc = { 1: 0, 2: 0, 3: 0 };
  (stats || []).forEach((s) => {
    const c = Number(s.change_class);
    if (Object.prototype.hasOwnProperty.call(acc, c)) {
      acc[c] += Number(s.area_ha || 0);
    }
  });
  return [1, 2, 3].map((c) => ({
    code: c,
    name: NAMES[c] + '(类' + c + ')',
    value: Number(acc[c].toFixed(2)),
    itemStyle: { color: COLORS[c] },
  }));
}

function tooltipBase() {
  return {
    trigger: 'item',
    backgroundColor: 'rgba(255,255,255,0.97)',
    borderColor: '#d8e2ee',
    textStyle: { color: '#24344d', fontSize: 12 },
    extraCssText: 'box-shadow:0 4px 16px rgba(16,34,58,.12);border-radius:8px;',
    formatter: (p) => {
      const pct = p.percent != null ? p.percent + '%' : '';
      return p.name + '<br/>面积：' + p.value + ' ha' + (pct ? '（' + pct + '）' : '');
    },
  };
}

export function renderPie(el, stats) {
  if (!el) return;
  if (!pie || pie.isDisposed()) pie = echarts.init(el, null, { renderer: 'canvas' });
  const data = normalizeStats(stats);
  pie.setOption({
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif' },
    title: { text: '三类面积占比', left: 'center', top: 2, textStyle: { color: '#5b6b82', fontSize: 12, fontWeight: 600 } },
    tooltip: tooltipBase(),
    legend: { bottom: 0, textStyle: { color: '#5b6b82', fontSize: 11 }, itemWidth: 12, itemHeight: 10 },
    series: [{
      name: '变化类别', type: 'pie', radius: ['38%', '66%'], center: ['50%', '48%'],
      avoidLabelOverlap: true,
      label: { color: '#3c4d66', fontSize: 11, formatter: '{b}\n{d}%' },
      labelLine: { lineStyle: { color: '#b6c4d4' } },
      data,
    }],
  }, true);
  pie.resize();
}

export function renderBar(el, stats) {
  if (!el) return;
  if (!bar || bar.isDisposed()) bar = echarts.init(el, null, { renderer: 'canvas' });
  const data = normalizeStats(stats);
  bar.setOption({
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif' },
    title: { text: '三类面积统计（公顷）', left: 'center', top: 2, textStyle: { color: '#5b6b82', fontSize: 12, fontWeight: 600 } },
    tooltip: { ...tooltipBase(), trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 46, right: 16, top: 38, bottom: 26 },
    xAxis: {
      type: 'category', data: data.map((d) => d.name),
      axisLine: { lineStyle: { color: '#c3cfdd' } },
      axisLabel: { color: '#5b6b82', fontSize: 11 },
    },
    yAxis: {
      type: 'value', name: 'ha',
      nameTextStyle: { color: '#8494a9' },
      splitLine: { lineStyle: { color: '#e6edf4' } },
      axisLabel: { color: '#5b6b82', fontSize: 11 },
    },
    series: [{
      type: 'bar', barWidth: '46%',
      data: data.map((d) => ({ value: d.value, itemStyle: { color: d.itemStyle.color, borderRadius: [5, 5, 0, 0] } })),
      label: { show: true, position: 'top', color: '#3c4d66', fontSize: 11 },
    }],
  }, true);
  bar.resize();
}

// ===== 批次 B1 / D11 · Δ<指数> 分布直方图：竖线标出置信度过渡带区间 =====
// 数据来自后端 params_snapshot.index_histogram（_index_histogram 的 32 bin），
// 无数据整块隐藏、不伪造分布。4 条灰色竖线为 ±[band_low, band_high]，
// 红色虚线为 ±T；过渡带是「证据不足」而非第四类变化。
// 批次 D11：标题 / x 轴名按实际指数动态化（NDVI / NDWI / NDBI / NDMI），
// NDBI（建筑/不透水面）/ NDMI（植被水分）不再被显示成 NDVI；数值口径完全复用既有渲染。
export function renderDeltaHistogram(el, hist, index) {
  if (!el) return;
  const counts = (hist && Array.isArray(hist.counts)) ? hist.counts : [];
  const edges = (hist && Array.isArray(hist.edges)) ? hist.edges : [];
  if (counts.length < 2 || edges.length !== counts.length + 1) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  const deltaName = 'Δ' + (String(index || '').trim().toUpperCase() || '指数');
  if (!deltaHist || deltaHist.isDisposed()) {
    deltaHist = echarts.init(el, null, { renderer: 'canvas' });
  }
  const data = counts.map((c, i) => [
    Number(((edges[i] + edges[i + 1]) / 2).toFixed(5)), Number(c),
  ]);
  const lines = [];
  const low = Number(hist.band_low), high = Number(hist.band_high);
  if (Number.isFinite(low) && Number.isFinite(high)) {
    [-high, -low, low, high].forEach((v, i) => lines.push({
      xAxis: Number(v.toFixed(5)),
      lineStyle: { color: '#969696', type: 'dotted', width: 1.2 },
      label: {
        show: i === 3, formatter: '过渡带 ±0.25T', color: '#6f6f6f',
        fontSize: 10, position: 'insideEndTop',
      },
    }));
  }
  const thr = Number(hist.threshold);
  if (Number.isFinite(thr)) {
    [-thr, thr].forEach((v) => lines.push({
      xAxis: Number(v.toFixed(5)),
      lineStyle: { color: '#d63031', type: 'dashed', width: 1.3 },
      label: { show: false },
    }));
  }
  deltaHist.setOption({
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif' },
    title: {
      text: deltaName + ' 分布与置信度过渡带', left: 'center', top: 2,
      textStyle: { color: '#5b6b82', fontSize: 12, fontWeight: 600 },
    },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(255,255,255,0.97)', borderColor: '#d8e2ee',
      textStyle: { color: '#24344d', fontSize: 12 },
    },
    grid: { left: 54, right: 16, top: 38, bottom: 26 },
    xAxis: {
      type: 'value', name: deltaName, nameTextStyle: { color: '#8494a9' },
      splitLine: { lineStyle: { color: '#e6edf4' } },
      axisLabel: {
        color: '#5b6b82', fontSize: 10,
        formatter: (v) => Number(v).toFixed(2),
      },
    },
    yAxis: {
      type: 'value', name: '像元', nameTextStyle: { color: '#8494a9' },
      splitLine: { lineStyle: { color: '#e6edf4' } },
      axisLabel: { color: '#5b6b82', fontSize: 10 },
    },
    series: [{
      type: 'bar', barWidth: '92%', data,
      itemStyle: { color: '#378add', opacity: 0.85 },
      markLine: { symbol: 'none', silent: true, animation: false, data: lines },
    }],
  }, true);
  deltaHist.resize();
}

// A1：五档变化强度横向条形图（数据来自 /result/change 的 intensity.levels）
export function renderIntensityBar(el, intensity) {
  if (!el) return;
  const levels = (intensity && Array.isArray(intensity.levels)) ? intensity.levels : [];
  if (!levels.length) return;
  const byCode = {};
  levels.forEach((lv) => { byCode[Number(lv.code)] = lv; });
  const codes = [1, 2, 3, 4, 5];
  if (!intensityBar || intensityBar.isDisposed()) {
    intensityBar = echarts.init(el, null, { renderer: 'canvas' });
  }
  intensityBar.setOption({
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif' },
    title: { text: '变化强度五档（像元数）', left: 'center', top: 2,
      textStyle: { color: '#5b6b82', fontSize: 12, fontWeight: 600 } },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(255,255,255,0.97)', borderColor: '#d8e2ee',
      textStyle: { color: '#24344d', fontSize: 12 },
      formatter: (p) => {
        const it = p && p[0];
        if (!it) return '';
        const lv = byCode[codes[it.dataIndex]] || {};
        return it.name + '<br/>像元数：' + Number(it.value).toLocaleString()
          + '<br/>占比：' + (Number(lv.ratio || 0) * 100).toFixed(2) + '%';
      },
    },
    grid: { left: 92, right: 62, top: 38, bottom: 26 },
    xAxis: {
      type: 'value', name: '像元',
      nameTextStyle: { color: '#8494a9' },
      splitLine: { lineStyle: { color: '#e6edf4' } },
      axisLabel: {
        color: '#5b6b82', fontSize: 10,
        formatter: (v) => (Number(v) >= 10000
          ? (Number(v) / 10000).toFixed(Number(v) % 10000 === 0 ? 0 : 1) + '万'
          : String(Number(v))),
      },
    },
    yAxis: {
      type: 'category', inverse: true, data: codes.map((c) => INTENSITY_ZH[c]),
      axisLine: { lineStyle: { color: '#c3cfdd' } },
      axisLabel: { color: '#3c4d66', fontSize: 11 },
    },
    series: [{
      type: 'bar', barWidth: '58%',
      data: codes.map((c) => ({
        value: Number((byCode[c] && byCode[c].pixel_count) || 0),
        itemStyle: { color: INTENSITY_COLORS[c], borderRadius: [0, 4, 4, 0] },
      })),
      label: { show: true, position: 'right', color: '#3c4d66', fontSize: 11,
        formatter: (p) => Number(p.value).toLocaleString() },
    }],
  }, true);
  intensityBar.resize();
}

// ===== 批次 A · 转移流向桑基图（前端 only） =====
// 数据直接由 change_service 的转移矩阵变换：行=变化前类别、列=变化后类别、
// value=矩阵值；源/目标节点加 t1·/t2· 前缀避免 ECharts 同名节点合流成环。
function sankeyModel(transition) {
  const tr = transition && !Array.isArray(transition) ? transition : {};
  const matrix = Array.isArray(transition) ? transition
    : (Array.isArray(tr.pixel_matrix) && tr.pixel_matrix.length ? tr.pixel_matrix
      : (Array.isArray(tr.area_matrix_ha) && tr.area_matrix_ha.length ? tr.area_matrix_ha
        : (Array.isArray(tr.matrix) ? tr.matrix : [])));
  const n = matrix.length;
  const codes = (Array.isArray(tr.codes) && tr.codes.length === n)
    ? tr.codes.map(Number)
    : Array.from({ length: n }, (_, i) => i + 1);
  const labels = tr.labels || {};
  const name = (c) => String(labels[String(c)] || NAMES[c] || ('类' + c)).split('/')[0];
  const src = (c) => 't1·' + name(c);
  const dst = (c) => 't2·' + name(c);
  const links = [];
  for (let i = 0; i < n; i += 1) {
    const row = Array.isArray(matrix[i]) ? matrix[i] : [];
    for (let j = 0; j < n; j += 1) {
      const value = Number(row[j] || 0);
      if (!Number.isFinite(value) || value <= 0) continue; // 过滤 value=0 的边
      links.push({
        source: src(codes[i]), target: dst(codes[j]), value,
        lineStyle: { color: SANKEY_COLORS[codes[i]] || '#8aa2bd' },
      });
    }
  }
  const used = new Set();
  links.forEach((l) => { used.add(l.source); used.add(l.target); });
  const nodes = [];
  codes.forEach((c) => {
    if (used.has(src(c))) nodes.push({ name: src(c), itemStyle: { color: SANKEY_COLORS[c] || '#8aa2bd' } });
  });
  codes.forEach((c) => {
    if (used.has(dst(c))) nodes.push({ name: dst(c), itemStyle: { color: SANKEY_COLORS[c] || '#8aa2bd' } });
  });
  const unit = Array.isArray(tr.pixel_matrix) ? '像元' : 'ha';
  return { nodes, links, unit };
}

/** 转移矩阵 → 桑基图（series.type='sankey'）；无矩阵数据时给出占位标题。 */
export function renderSankey(el, transition) {
  if (!el) return;
  if (!sankey || sankey.isDisposed()) sankey = echarts.init(el, null, { renderer: 'canvas' });
  const { nodes, links, unit } = sankeyModel(transition);
  const short = (s) => String(s || '').replace(/^t[12]·/, '');
  sankey.setOption({
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif' },
    title: {
      text: links.length ? '转移流向（t1 → t2）' : '暂无转移矩阵数据',
      left: 'center', top: 2,
      textStyle: { color: '#5b6b82', fontSize: 12, fontWeight: 600 },
    },
    tooltip: {
      trigger: 'item',
      backgroundColor: 'rgba(255,255,255,0.97)', borderColor: '#d8e2ee',
      textStyle: { color: '#24344d', fontSize: 12 },
      extraCssText: 'box-shadow:0 4px 16px rgba(16,34,58,.12);border-radius:8px;',
      formatter: (p) => (p.dataType === 'edge'
        ? short(p.data.source) + ' → ' + short(p.data.target)
          + '<br/>' + Number(p.data.value).toLocaleString() + ' ' + unit
        : short(p.name)),
    },
    series: [{
      type: 'sankey', left: 14, right: 84, top: 40, bottom: 14,
      nodeWidth: 12, nodeGap: 12, layoutIterations: 32,
      emphasis: { focus: 'adjacency' },
      label: {
        color: '#3c4d66', fontSize: 11,
        formatter: (p) => short(p.name),
      },
      lineStyle: { curveness: 0.5, opacity: 0.38 },
      data: nodes, links,
    }],
  }, true);
  sankey.resize();
}

export function resizeSankey() {
  if (sankey && !sankey.isDisposed()) sankey.resize();
}

// ===== 批次 A · 历史运行对比：面积三分类并排条形图 =====
/** groups: [{name, stats:[{change_class, area_ha}]}, ...]，最多取前两条。 */
export function renderCompareBar(el, groups) {
  if (!el) return;
  if (!compareBar || compareBar.isDisposed()) compareBar = echarts.init(el, null, { renderer: 'canvas' });
  const gs = (Array.isArray(groups) ? groups : []).slice(0, 2);
  const areas = (g) => {
    const acc = { 1: 0, 2: 0, 3: 0 };
    ((g && g.stats) || []).forEach((s) => {
      const c = Number(s && s.change_class);
      if (Object.prototype.hasOwnProperty.call(acc, c)) acc[c] += Number((s && s.area_ha) || 0);
    });
    return [1, 2, 3].map((c) => Number(acc[c].toFixed(2)));
  };
  const palette = ['#1f7ae0', '#e4a23c'];
  compareBar.setOption({
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif' },
    title: { text: '三类面积对比（公顷）', left: 'center', top: 2,
      textStyle: { color: '#5b6b82', fontSize: 12, fontWeight: 600 } },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(255,255,255,0.97)', borderColor: '#d8e2ee',
      textStyle: { color: '#24344d', fontSize: 12 },
    },
    legend: { bottom: 0, textStyle: { color: '#5b6b82', fontSize: 11 },
      data: gs.map((g) => g.name) },
    grid: { left: 48, right: 14, top: 38, bottom: 34 },
    xAxis: {
      type: 'category', data: [1, 2, 3].map((c) => NAMES[c]),
      axisLine: { lineStyle: { color: '#c3cfdd' } },
      axisLabel: { color: '#5b6b82', fontSize: 11 },
    },
    yAxis: {
      type: 'value', name: 'ha', nameTextStyle: { color: '#8494a9' },
      splitLine: { lineStyle: { color: '#e6edf4' } },
      axisLabel: { color: '#5b6b82', fontSize: 11 },
    },
    series: gs.map((g, i) => ({
      name: g.name, type: 'bar', barMaxWidth: 26,
      data: areas(g),
      itemStyle: { color: palette[i] || palette[0], borderRadius: [4, 4, 0, 0] },
    })),
  }, true);
  compareBar.resize();
}

// ==========================================================================
// 批次 E · 结果卡第 4/5 页签图表（方法对比分组条形图 / 阈值敏感性折线图）
// 与批次 A 的 renderCompareBar（2 组历史对比）完全独立：仅新增实例与导出，
// 不改既有 renderCompareBar 及 resizeCharts 对其它实例的行为。
// ==========================================================================

/** 批次 E：方法对比分组条形图。
 *  groups: [{name, stats:[{change_class, area_ha}]}, ...]，支持 1~5 组；
 *  每组 3 条柱（增强 / 退化 / 稳定），y 轴面积 ha，沿用三类主色 COLORS。
 */
export function renderMethodGroups(el, groups) {
  if (!el) return;
  if (!methodGroups || methodGroups.isDisposed()) {
    methodGroups = echarts.init(el, null, { renderer: 'canvas' });
  }
  const gs = (Array.isArray(groups) ? groups : []).slice(0, 5);
  const areas = (g) => {
    const acc = { 1: 0, 2: 0, 3: 0 };
    ((g && g.stats) || []).forEach((s) => {
      const c = Number(s && s.change_class);
      if (Object.prototype.hasOwnProperty.call(acc, c)) acc[c] += Number((s && s.area_ha) || 0);
    });
    return [1, 2, 3].map((c) => Number(acc[c].toFixed(2)));
  };
  const names = gs.map((g) => String((g && g.name) || '—'));
  const cols = gs.map((g) => areas(g));
  methodGroups.setOption({
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif' },
    title: { text: '方法对比 · 三类面积（公顷）', left: 'center', top: 2,
      textStyle: { color: '#5b6b82', fontSize: 12, fontWeight: 600 } },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(255,255,255,0.97)', borderColor: '#d8e2ee',
      textStyle: { color: '#24344d', fontSize: 12 },
      formatter: (ps) => {
        const arr = Array.isArray(ps) ? ps : [ps];
        if (!arr.length) return '';
        return String(arr[0].name) + arr.map((p) => '<br/>' + p.marker + p.seriesName
          + '：' + Number(p.value).toFixed(2) + ' ha').join('');
      },
    },
    legend: { bottom: 0, data: [1, 2, 3].map((c) => NAMES[c]),
      textStyle: { color: '#5b6b82', fontSize: 11 }, itemWidth: 12, itemHeight: 10 },
    grid: { left: 48, right: 14, top: 38, bottom: 48 },
    xAxis: {
      type: 'category', data: names,
      axisLine: { lineStyle: { color: '#c3cfdd' } },
      axisLabel: { color: '#5b6b82', fontSize: 10, interval: 0, width: 58, overflow: 'break', lineHeight: 12 },
    },
    yAxis: {
      type: 'value', name: 'ha', nameTextStyle: { color: '#8494a9' },
      splitLine: { lineStyle: { color: '#e6edf4' } },
      axisLabel: { color: '#5b6b82', fontSize: 10 },
    },
    series: [1, 2, 3].map((c) => ({
      name: NAMES[c], type: 'bar', barMaxWidth: 20,
      data: cols.map((col) => col[c - 1]),
      itemStyle: { color: COLORS[c], borderRadius: [4, 4, 0, 0] },
    })),
  }, true);
  methodGroups.resize();
}

/** 批次 E：阈值敏感性折线图。
 *  payload: {base_threshold, sweep:[{t, scale, areas:[...], transition_pct}], computed_at}；
 *  x 轴取各档 scale，tooltip 同时显示该档实际 T；左 y 轴增强 / 退化两条实线（ha），
 *  右 y 轴过渡带占比一条灰色虚线（%）；scale===1.0 的基准点标注「基准 T」。
 */
export function renderSensitivityLine(el, payload) {
  if (!el) return;
  if (!sensitivityLine || sensitivityLine.isDisposed()) {
    sensitivityLine = echarts.init(el, null, { renderer: 'canvas' });
  }
  const sweep = (payload && Array.isArray(payload.sweep)) ? payload.sweep : [];
  const areaOf = (row, c) => {
    const a = (row && Array.isArray(row.areas))
      ? row.areas.find((x) => Number(x && x.change_class) === c) : null;
    return Number((a && a.area_ha) || 0);
  };
  const xs = sweep.map((r) => Number(r.scale).toFixed(1) + '×');
  const tVals = sweep.map((r) => Number(r.t));
  const enhance = sweep.map((r) => Number(areaOf(r, 1).toFixed(2)));
  const degrade = sweep.map((r) => Number(areaOf(r, 2).toFixed(2)));
  const trans = sweep.map((r) => Number(Number(r.transition_pct || 0).toFixed(2)));
  const baseIdx = sweep.findIndex((r) => Number(r.scale) === 1);
  const markPoint = baseIdx >= 0 ? {
    symbol: 'pin', symbolSize: 34,
    itemStyle: { color: COLORS[1] },
    label: { show: true, formatter: '基准 T', color: '#fff', fontSize: 9, fontWeight: 700 },
    data: [{ coord: [xs[baseIdx], enhance[baseIdx]] }],
  } : { data: [] };
  sensitivityLine.setOption({
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif' },
    title: { text: '阈值敏感性 · 三类面积随阈值变化', left: 'center', top: 2,
      textStyle: { color: '#5b6b82', fontSize: 12, fontWeight: 600 } },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(255,255,255,0.97)', borderColor: '#d8e2ee',
      textStyle: { color: '#24344d', fontSize: 12 },
      formatter: (ps) => {
        const arr = Array.isArray(ps) ? ps : [ps];
        if (!arr.length) return '';
        const i = arr[0].dataIndex;
        const head = '阈值 ' + String(xs[i]) + '　实际 T=' + tVals[i].toFixed(4);
        return head + arr.map((p) => '<br/>' + p.marker + p.seriesName + '：'
          + Number(p.value).toFixed(2) + (p.seriesName === '过渡带占比' ? '%' : ' ha')).join('');
      },
    },
    legend: { bottom: 0, data: ['增强', '退化', '过渡带占比'],
      textStyle: { color: '#5b6b82', fontSize: 11 }, itemWidth: 14, itemHeight: 10 },
    grid: { left: 50, right: 48, top: 40, bottom: 36 },
    xAxis: {
      type: 'category', data: xs, boundaryGap: false,
      axisLine: { lineStyle: { color: '#c3cfdd' } },
      axisLabel: { color: '#5b6b82', fontSize: 10 },
    },
    yAxis: [
      { type: 'value', name: 'ha', nameTextStyle: { color: '#8494a9' },
        splitLine: { lineStyle: { color: '#e6edf4' } },
        axisLabel: { color: '#5b6b82', fontSize: 10 } },
      { type: 'value', name: '%', nameTextStyle: { color: '#8494a9' },
        splitLine: { show: false },
        axisLabel: { color: '#5b6b82', fontSize: 10, formatter: '{value}%' } },
    ],
    series: [
      { name: '增强', type: 'line', yAxisIndex: 0, data: enhance,
        lineStyle: { color: COLORS[1], width: 2 }, itemStyle: { color: COLORS[1] },
        symbol: 'circle', symbolSize: 6, markPoint },
      { name: '退化', type: 'line', yAxisIndex: 0, data: degrade,
        lineStyle: { color: COLORS[2], width: 2 }, itemStyle: { color: COLORS[2] },
        symbol: 'circle', symbolSize: 6 },
      { name: '过渡带占比', type: 'line', yAxisIndex: 1, data: trans,
        lineStyle: { color: '#8a94a3', width: 2, type: 'dashed' },
        itemStyle: { color: '#8a94a3' }, symbol: 'circle', symbolSize: 6 },
    ],
  }, true);
  sensitivityLine.resize();
}

/** 批次 E：切到 method / sensitivity 页签时单独重算两个新实例（隐藏容器宽度为 0 需重算）。 */
export function resizeExperiments() {
  if (methodGroups && !methodGroups.isDisposed()) methodGroups.resize();
  if (sensitivityLine && !sensitivityLine.isDisposed()) sensitivityLine.resize();
}

export function resizeCharts() {
  if (pie && !pie.isDisposed()) pie.resize();
  if (bar && !bar.isDisposed()) bar.resize();
  if (deltaHist && !deltaHist.isDisposed()) deltaHist.resize();
  if (intensityBar && !intensityBar.isDisposed()) intensityBar.resize();
  if (sankey && !sankey.isDisposed()) sankey.resize();
  if (compareBar && !compareBar.isDisposed()) compareBar.resize();
  // 批次 E：新实例一并纳入全局 resize（窗口 resize 时重算）
  if (methodGroups && !methodGroups.isDisposed()) methodGroups.resize();
  if (sensitivityLine && !sensitivityLine.isDisposed()) sensitivityLine.resize();
}
