// -*- coding: utf-8 -*-
// ECharts 图表：右栏「检测结果」三类面积占比饼图（REQ-08/11）与三类面积柱状图（REQ-11）。
// 数据来自 /result/change 的 stats，本地渲染保证首屏 < 5s（NF2）。浅色专业主题。

import * as echarts from 'echarts';

const COLORS = { 1: '#21a04c', 2: '#d94c3d', 3: '#5b7a99' };
const NAMES = { 1: '增强', 2: '退化', 3: '稳定' };

let pie = null;
let bar = null;
let intensityBar = null;

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

export function resizeCharts() {
  if (pie && !pie.isDisposed()) pie.resize();
  if (bar && !bar.isDisposed()) bar.resize();
  if (intensityBar && !intensityBar.isDisposed()) intensityBar.resize();
}
