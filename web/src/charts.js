// -*- coding: utf-8 -*-
// ECharts 图表：UI-03 三类面积占比饼图（REQ-08/11）、UI-05 三类面积柱状图（REQ-11）。
// 数据来自 /result/change 的 stats，本地渲染保证首屏 < 5s（NF2）。

import * as echarts from 'echarts';

const COLORS = { 1: '#21ba45', 2: '#e74c3c', 3: '#5b7a99' };
const NAMES = { 1: '增强', 2: '退化', 3: '稳定' };

let pie = null;
let bar = null;

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
    backgroundColor: 'rgba(12,21,36,0.95)',
    borderColor: '#274067',
    textStyle: { color: '#e8effb', fontSize: 12 },
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
    title: { text: '三类面积占比', left: 'center', top: 2, textStyle: { color: '#cfe0f8', fontSize: 12 } },
    tooltip: tooltipBase(),
    legend: { bottom: 0, textStyle: { color: '#90a6c8', fontSize: 11 }, itemWidth: 12, itemHeight: 10 },
    series: [{
      name: '变化类别', type: 'pie', radius: ['38%', '66%'], center: ['50%', '50%'],
      avoidLabelOverlap: true,
      label: { color: '#c9d8ef', fontSize: 11, formatter: '{b}\n{d}%' },
      labelLine: { lineStyle: { color: '#40608a' } },
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
    title: { text: '三类面积统计（公顷）', left: 'center', top: 2, textStyle: { color: '#cfe0f8', fontSize: 12 } },
    tooltip: { ...tooltipBase(), trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 46, right: 16, top: 38, bottom: 26 },
    xAxis: {
      type: 'category', data: data.map((d) => d.name),
      axisLine: { lineStyle: { color: '#274067' } },
      axisLabel: { color: '#90a6c8', fontSize: 11 },
    },
    yAxis: {
      type: 'value', name: 'ha',
      nameTextStyle: { color: '#6d84a8' },
      splitLine: { lineStyle: { color: '#20324f' } },
      axisLabel: { color: '#90a6c8', fontSize: 11 },
    },
    series: [{
      type: 'bar', barWidth: '46%',
      data: data.map((d) => ({ value: d.value, itemStyle: { color: d.itemStyle.color, borderRadius: [5, 5, 0, 0] } })),
      label: { show: true, position: 'top', color: '#c9d8ef', fontSize: 11 },
    }],
  }, true);
  bar.resize();
}

export function resizeCharts() {
  if (pie && !pie.isDisposed()) pie.resize();
  if (bar && !bar.isDisposed()) bar.resize();
}
