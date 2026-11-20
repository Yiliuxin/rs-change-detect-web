// -*- coding: utf-8 -*-
// 批次 H · 平台总览仪表盘（工作台「总览」页签 + 历史面板「总览」入口）。
// 只消费 api.getStatsOverview()（GET /api/v1/stats/overview，JWT 由 fetchWithTimeout 自动附带），
// 不写影像处理/算法/SQL，不直连数据库或对象存储。
// 顶部四张数据卡 + 两张 ECharts 图（按指数 run 数柱状图 / 最近 run 三类面积堆积条形图）
// + 最近 run 摘要表（字段口径、数字格式与历史面板一致，公顷保留两位）。
// 三类配色沿用专题图语义：绿=增强、红=退化、灰蓝=稳定（这是面积构成，不是涨跌，禁用红涨绿跌语义）。
// ECharts 实例：单例持有，重渲染前 dispose 旧实例、页签切换/窗口 resize 时 resize、卸载时 destroy，
// 避免重复 init 造成内存泄漏；加载失败/空数据给中文空态，绝不白屏或抛未捕获异常。

import * as echarts from 'echarts'; // 与 charts.js 同源
import { getStatsOverview } from './api.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

// 三类语义色：与 charts.js 的 COLORS{1,2,3} 及 CSS --green/--red/--stable 完全同源
const CLASS_COLORS = { gain: '#21a04c', loss: '#d94c3d', stable: '#5b7a99' };
const CLASS_NAMES = { gain: '增强', loss: '退化', stable: '稳定' };

let chartIndex = null;   // 按指数 run 数柱状图实例
let chartRecent = null;  // 最近 run 三类面积堆积条形图实例

const ST = { loaded: false, loading: false, seq: 0 };
const FONT = 'Segoe UI, Microsoft YaHei, sans-serif';
const TIP_BASE = {
  backgroundColor: 'rgba(255,255,255,0.97)',
  borderColor: '#d8e2ee',
  textStyle: { color: '#24344d', fontSize: 12 },
  extraCssText: 'box-shadow:0 4px 16px rgba(16,34,58,.12);border-radius:8px;',
};

// ---------- 格式化：与历史面板口径一致 ----------
function num2(v) {
  const n = Number(v);
  return Number.isFinite(n) ? Number(n.toFixed(2)) : 0;
}
function haNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(2) : '0.00';
}
function ha(v) { return haNum(v) + ' ha'; }
function int(v) {
  const n = Number(v);
  return Number.isFinite(n) ? String(Math.round(n)) : '0';
}
function threshold(v) {
  if (v == null || v === '') return '—';
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(4) : String(v);
}
// 时间：后端 timestamptz 为 UTC ISO8601，解析后按浏览器本地时区显示；纯日期不换算。
// 与 history.js 的 when() 完全同款口径。
function when(s) {
  const str = String(s == null ? '' : s).trim();
  if (!str) return '—';
  if (/^\d{4}-\d{2}-\d{2}$/.test(str)) return str;
  const iso = /[Zz]$|[+-]\d{2}:?\d{2}$/.test(str) ? str : str + 'Z';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return str.slice(0, 16).replace('T', ' ');
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
    + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

// ---------- 实例管理 ----------
function disposeChart(which) {
  if (which === 'index') {
    if (chartIndex && !chartIndex.isDisposed()) chartIndex.dispose();
    chartIndex = null;
    const el = $('chart-ov-index');
    if (el) el.innerHTML = '';
  } else {
    if (chartRecent && !chartRecent.isDisposed()) chartRecent.dispose();
    chartRecent = null;
    const el = $('chart-ov-recent');
    if (el) el.innerHTML = '';
  }
}

/** 页签切换 / 窗口 resize 时重算（隐藏容器首次显示宽度为 0，必须 resize）。 */
export function resizeOverview() {
  if (chartIndex && !chartIndex.isDisposed()) chartIndex.resize();
  if (chartRecent && !chartRecent.isDisposed()) chartRecent.resize();
}

/** 卸载时销毁两个实例，避免内存泄漏。 */
export function destroyOverview() {
  if (chartIndex && !chartIndex.isDisposed()) chartIndex.dispose();
  if (chartRecent && !chartRecent.isDisposed()) chartRecent.dispose();
  chartIndex = null;
  chartRecent = null;
}
window.addEventListener('beforeunload', destroyOverview);

// ---------- 三态 ----------
function renderStatus(kind, msg) {
  const box = $('ov-status');
  const body = $('ov-body');
  if (!box) return;
  if (kind === 'loading') {
    box.hidden = false;
    box.className = 'status-line';
    box.innerHTML = '<span class="spinner"></span>正在加载平台总览…';
    if (body) body.hidden = true;
  } else if (kind === 'error') {
    box.hidden = false;
    box.className = 'err-line';
    box.innerHTML = '总览数据加载失败：' + esc(msg || '未知原因')
      + ' <button type="button" class="btn btn-sm btn-ghost" id="ov-retry">重试</button>';
    if (body) body.hidden = true;
    const retry = $('ov-retry');
    if (retry) retry.onclick = () => { loadOverview(true); };
  } else {
    box.hidden = true;
    box.className = 'status-line';
    box.innerHTML = '';
    if (body) body.hidden = false;
  }
}

// ---------- 顶部数据卡 ----------
function renderCards(d) {
  const cards = [
    ['总 run 数', int(d.total_runs), ''],
    ['检测总面积', ha(d.total_area_ha), ''],
    ['增强 + 退化面积', ha(d.changed_area_ha), ''],
    ['最近一次检测', when(d.last_run_at), 'v-sm'],
  ];
  const box = $('ov-cards');
  if (box) {
    box.innerHTML = cards.map(([k, v, cls]) =>
      '<div class="metric"><div class="k">' + esc(k) + '</div>'
      + '<div class="v ' + cls + '">' + esc(v) + '</div></div>').join('');
  }
  // skipped > 0：如实提示老 run 缺统计字段已跳过、不补算
  const skipped = Number(d.skipped) || 0;
  const skEl = $('ov-skipped');
  if (skEl) {
    skEl.hidden = skipped <= 0;
    if (skipped > 0) {
      skEl.innerHTML = '<span class="ov-warn-mark">!</span> 有 <b>' + skipped
        + '</b> 条历史 run 缺少面积统计字段，已如实跳过、未补算（统计口径不变）。';
    }
  }
}

// ---------- 图①：按指数 run 数柱状图 ----------
function renderIndexChart(byIndex) {
  disposeChart('index');
  const el = $('chart-ov-index');
  if (!el) return;
  const entries = Object.keys(byIndex || {})
    .map((k) => [String(k).toUpperCase(), Number(byIndex[k]) || 0])
    .filter((x) => x[1] > 0);
  if (!entries.length) {
    el.innerHTML = '<div class="empty-state compact"><p>暂无按指数统计</p>'
      + '<p class="empty-sub">完成检测后，这里按 NDVI / NDWI 等指数统计 run 数。</p></div>';
    return;
  }
  chartIndex = echarts.init(el, null, { renderer: 'canvas' });
  chartIndex.setOption({
    backgroundColor: 'transparent',
    textStyle: { fontFamily: FONT },
    title: { text: '按指数 run 数', left: 'center', top: 2,
      textStyle: { color: '#5b6b82', fontSize: 12, fontWeight: 600 } },
    tooltip: {
      ...TIP_BASE, trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: (ps) => {
        const p = Array.isArray(ps) ? ps[0] : ps;
        return p ? p.name + '<br/>run 数：<b>' + p.value + '</b>' : '';
      },
    },
    grid: { left: 44, right: 16, top: 38, bottom: 24 },
    xAxis: {
      type: 'category', data: entries.map((e) => e[0]),
      axisLine: { lineStyle: { color: '#c3cfdd' } },
      axisLabel: { color: '#5b6b82', fontSize: 11 },
    },
    yAxis: {
      type: 'value', name: 'run', minInterval: 1,
      nameTextStyle: { color: '#8494a9' },
      splitLine: { lineStyle: { color: '#e6edf4' } },
      axisLabel: { color: '#5b6b82', fontSize: 11 },
    },
    series: [{
      type: 'bar', barWidth: '46%', data: entries.map((e) => e[1]),
      itemStyle: { color: '#1f7ae0', borderRadius: [5, 5, 0, 0] },
      label: { show: true, position: 'top', color: '#3c4d66', fontSize: 11 },
    }],
  }, true);
  chartIndex.resize();
}

// ---------- 图②：最近 run 三类面积堆积条形图 ----------
function renderRecentChart(recent) {
  disposeChart('recent');
  const el = $('chart-ov-recent');
  if (!el) return;
  const rows = (Array.isArray(recent) ? recent : []).slice(0, 10);
  if (!rows.length) {
    el.innerHTML = '<div class="empty-state compact"><p>暂无最近检测记录</p>'
      + '<p class="empty-sub">完成检测后，这里按增强 / 退化 / 稳定三类展示最近 10 条 run 的面积构成。</p></div>';
    return;
  }
  const labels = rows.map((r) => {
    const name = String(r.aoi_name || 'AOI').slice(0, 10);
    const idx = String(r.index || '').toUpperCase();
    return name + (idx ? ' · ' + idx : '') + ' · ' + String(r.run_id || '').slice(0, 8);
  });
  chartRecent = echarts.init(el, null, { renderer: 'canvas' });
  chartRecent.setOption({
    backgroundColor: 'transparent',
    textStyle: { fontFamily: FONT },
    title: { text: '最近运行 · 三类面积构成（ha）', left: 'center', top: 2,
      textStyle: { color: '#5b6b82', fontSize: 12, fontWeight: 600 } },
    tooltip: {
      ...TIP_BASE, trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: (ps) => {
        const arr = Array.isArray(ps) ? ps : [];
        if (!arr.length) return '';
        return arr[0].name + arr.map((p) => '<br/>' + p.marker + p.seriesName
          + '：<b>' + haNum(p.value) + ' ha</b>').join('');
      },
    },
    legend: {
      bottom: 0, data: [CLASS_NAMES.gain, CLASS_NAMES.loss, CLASS_NAMES.stable],
      textStyle: { color: '#5b6b82', fontSize: 11 }, itemWidth: 12, itemHeight: 10,
    },
    grid: { left: 118, right: 18, top: 38, bottom: 30 },
    xAxis: {
      type: 'value', name: 'ha', nameTextStyle: { color: '#8494a9' },
      splitLine: { lineStyle: { color: '#e6edf4' } },
      axisLabel: { color: '#5b6b82', fontSize: 10 },
    },
    yAxis: {
      type: 'category', inverse: true, data: labels,
      axisLine: { lineStyle: { color: '#c3cfdd' } },
      axisLabel: { color: '#3c4d66', fontSize: 10, width: 108, overflow: 'truncate' },
    },
    series: [
      { name: CLASS_NAMES.gain, type: 'bar', stack: 'area', barMaxWidth: 20,
        data: rows.map((r) => num2(r.gain_ha)), itemStyle: { color: CLASS_COLORS.gain } },
      { name: CLASS_NAMES.loss, type: 'bar', stack: 'area', barMaxWidth: 20,
        data: rows.map((r) => num2(r.loss_ha)), itemStyle: { color: CLASS_COLORS.loss } },
      { name: CLASS_NAMES.stable, type: 'bar', stack: 'area', barMaxWidth: 20,
        data: rows.map((r) => num2(r.stable_ha)),
        itemStyle: { color: CLASS_COLORS.stable, borderRadius: [0, 3, 3, 0] } },
    ],
  }, true);
  chartRecent.resize();
}

// ---------- 最近 run 摘要表：字段口径与历史面板一致 ----------
function renderTable(recent) {
  const rows = Array.isArray(recent) ? recent : [];
  const head = '<thead><tr><th>run_id</th><th>地名</th><th>指数</th>'
    + '<th>日期</th><th>阈值</th><th>增强</th><th>退化</th><th>稳定</th></tr></thead>';
  const body = rows.length
    ? '<tbody>' + rows.map((r) =>
      '<tr>'
      + '<td><code>' + esc(String(r.run_id || '').slice(0, 8)) + '</code></td>'
      + '<td>' + esc(r.aoi_name || '—') + '</td>'
      + '<td>' + esc(String(r.index || '').toUpperCase() || '—') + '</td>'
      + '<td>' + esc(when(r.date)) + '</td>'
      + '<td>' + esc(threshold(r.threshold)) + '</td>'
      + '<td>' + haNum(r.gain_ha) + '</td>'
      + '<td>' + haNum(r.loss_ha) + '</td>'
      + '<td>' + haNum(r.stable_ha) + '</td>'
      + '</tr>').join('') + '</tbody>'
    : '<tbody><tr><td colspan="8" class="hint">暂无最近运行记录</td></tr></tbody>';
  const table = $('ov-table');
  if (table) table.innerHTML = head + body;
  const count = $('ov-recent-count');
  if (count) count.textContent = '最近 ' + rows.length + ' 条 · 面积单位 ha';
}

function renderEmpty() {
  disposeChart('index');
  disposeChart('recent');
  const empty = '<div class="empty-state compact"><p>暂无检测记录</p>'
    + '<p class="empty-sub">完成一次变化检测后，这里会展示指数分布与三类面积统计。</p></div>';
  const e1 = $('chart-ov-index');
  const e2 = $('chart-ov-recent');
  if (e1) e1.innerHTML = empty;
  if (e2) e2.innerHTML = empty;
  renderTable([]);
}

function renderOverview(d) {
  const data = d && typeof d === 'object' ? d : {};
  renderStatus('ok');
  renderCards(data);
  const total = Number(data.total_runs) || 0;
  const recent = Array.isArray(data.recent) ? data.recent : [];
  // 空数据：零值数据卡照常展示，图表/摘要表给中文空态，不白屏
  if (total === 0 && recent.length === 0) { renderEmpty(); return; }
  renderIndexChart(data.by_index);
  renderRecentChart(recent);
  renderTable(recent);
}

/**
 * 首次点击「总览」才异步加载接口数据；已加载过切页签只 resize 不重复请求。
 * force=true（重试）会重新拉取并覆盖。任何失败都由内联中文空态承接，不抛未捕获异常。
 */
export async function loadOverview(force) {
  if (ST.loading) return;
  if (ST.loaded && !force) return;
  ST.loading = true;
  const seq = ++ST.seq;
  renderStatus('loading');
  try {
    const data = await getStatsOverview();
    if (seq !== ST.seq) return; // 过期请求丢弃，避免竞态覆盖
    ST.loaded = true;
    renderOverview(data);
  } catch (e) {
    if (seq !== ST.seq) return;
    renderStatus('error', (e && e.message) || '未知原因');
  } finally {
    ST.loading = false;
  }
}

/** 切到「总览」页签：懒加载 + 容器刚显示时重算图表尺寸。 */
export function activateOverview() {
  loadOverview(false);
  window.requestAnimationFrame(() => resizeOverview());
}
