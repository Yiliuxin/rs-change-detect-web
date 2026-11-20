// -*- coding: utf-8 -*-
// 批次 E（方法对比 / 敏感性分析）：结果卡第 4/5 页签的全部交互逻辑。
// 仅消费 api.js 的两个长耗时实验接口 POST /result/compare · POST /result/sensitivity；
// 弹层 / 状态 / 请求 / 分阶段进度 / 表格渲染 / 自动结论 / 错误内联均在模块内闭环，不堆进 main.js。
// 契约字段逐字锁定：areas 恒 3 项、transition_pct / agreement_pct 为 0~100 百分数（不 ×100）、
// manual 阈值落在 (-1,1) 开区间、force:false 固定下发。所有后端字符串写入 innerHTML 前一律 esc()。
// 错误只按 errors.js 的 errorMessage / 后端 code + message 中文提示，不臆造错误语义、不新增错误码。

import * as api from './api.js';
import * as C from './charts.js';
import { errorMessage } from './errors.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

// 批次 E：中文配置标签（枚举 → 展示口径；提交仍用契约英文枚举）
// 批次 G：方法枚举扩为 3 种，与后端 VALID_CHANGE_METHODS 同步；短标签用于图表图例。
// 批次 D11：指数枚举扩为 4 项 NDVI / NDWI / NDBI / NDMI，与后端 VALID_INDEXES 同步；
// configLabel / shortLabel 直接展示英文枚举，新增指数无需改写即正常显示。
const METHOD_ZH = { otsu: '差值法', regression: '回归法', cva: '变化向量分析' };
const METHOD_SHORT = { otsu: '差', regression: '回', cva: '向' };
const MODE_ZH = { otsu: 'Otsu', manual: '手动' };
const INDEX_ENUM = ['NDVI', 'NDWI', 'NDBI', 'NDMI'];
const METHOD_ENUM = ['otsu', 'regression', 'cva'];
const MAX_VARIANTS = 4;             // 后端 COMPARE_MAX_VARIANTS

// 批次 D11：后端双波段指数栅格 band2 的辅助指数映射（constants.py INDEX_AUXILIARY 写死）。
// 「方法对比」是只读实验，只加载 index_t1/t2.tif 的两个波段（band1=主指数、band2=辅助指数），
// 因此只能复算基准 run 已落盘的这两个指数；其余指数需先以该指数重新检测生成对应 index raster。
const INDEX_AUXILIARY = { NDVI: 'NDWI', NDWI: 'NDVI', NDBI: 'NDVI', NDMI: 'NDVI' };
function recomputableIndexes(baseIndex) {
  return [baseIndex, INDEX_AUXILIARY[baseIndex] || 'NDVI'];
}

// 批次 E：模块状态（两个独立请求态 + 当前弹层的候选变体）
const E = {
  busyCompare: false,
  busySens: false,
  candidates: [],
  // 批次 F：CSV 导出请求态 + 最近一次成功实验的签名入参（CSV 端点只读缓存，靠该签名命中）
  busyCompareCsv: false,
  busySensCsv: false,
  lastCompare: null,   // { runId, variants } —— variants 与 compare 提交入参完全一致
  sensRunId: '',       // 最近一次成功敏感性分析对应 run_id（CSV 只需 run_id）
};
const DEPS = { getRunId: null, getBase: null };

// ---------- 批次 E：数值格式化（数字一律取自响应，绝不硬编码结论数值） ----------
const ha = (v) => Number(v || 0).toFixed(2);
const pct = (v) => Number(v || 0).toFixed(1) + '%';
const fmtT = (v) => (v == null || v === '' ? '—' : Number(v).toFixed(4));
const fmtScale = (v) => Number(v).toFixed(1) + '×';

function areasMap(areas) {
  const acc = { 1: 0, 2: 0, 3: 0 };
  (Array.isArray(areas) ? areas : []).forEach((s) => {
    const c = Number(s && s.change_class);
    if (c === 1 || c === 2 || c === 3) acc[c] += Number((s && s.area_ha) || 0);
  });
  return { 1: Number(acc[1].toFixed(2)), 2: Number(acc[2].toFixed(2)), 3: Number(acc[3].toFixed(2)) };
}

function configLabel(index, method, mode) {
  return String(index || '—') + ' · ' + (METHOD_ZH[method] || method || '—')
    + ' · ' + (mode === 'manual' ? MODE_ZH.manual : MODE_ZH.otsu);
}
function shortLabel(index, method) {
  return String(index || '—') + '·' + (METHOD_SHORT[method] || '差');
}

// ---------- 批次 E：基准配置（只读，来自 S.change.params_snapshot，缺失默认 NDVI / otsu） ----------
// 批次 D11：按 4 项枚举判定，NDBI / NDMI 不再被吞成 NDVI。
function baseConfig() {
  const ch = (DEPS.getBase && DEPS.getBase()) || {};
  const snap = ch.params_snapshot || ch || {};
  const idx = String(snap.index == null ? '' : snap.index).trim().toUpperCase();
  const method = String(snap.method == null ? '' : snap.method).trim().toLowerCase();
  return {
    index: INDEX_ENUM.includes(idx) ? idx : 'NDVI',
    method: METHOD_ENUM.includes(method) ? method : 'otsu',
    threshold: snap.threshold != null ? Number(snap.threshold) : null,
    threshold_source: String(snap.threshold_source || 'otsu').toLowerCase(),
  };
}

// 批次 E / G / D11：4 指数 × 3 方法 = 12 种组合，排除与基准 (index,method) 完全相同的
// 那个 → 剩 11 个候选。默认勾选「与基准仅一项不同」且指数属于基准 run 已落盘两个指数
// （主指数 + 辅助指数）的候选，最多 MAX_VARIANTS=4 个：既保证至少一项与基准不同，
// 又不超出后端 COMPARE_MAX_VARIANTS。其余指数候选在 label 旁标注「只读实验仅能复算
// 基准 run 已落盘的两个指数」，提示用户需先以该指数重新检测。
function buildCandidates(base) {
  const out = [];
  const allowed = recomputableIndexes(base.index);
  INDEX_ENUM.forEach((index) => {
    METHOD_ENUM.forEach((method) => {
      if (index === base.index && method === base.method) return;
      const recomputable = allowed.includes(index);
      out.push({
        index, method,
        key: index + '|' + method,
        label: index + ' · ' + METHOD_ZH[method],
        note: recomputable
          ? ''
          : '只读实验仅能复算基准 run 已落盘的两个指数（' + allowed.join(' / ')
            + '），该指数需先以该指数重新检测',
        oneDiff: index === base.index || method === base.method,
        recomputable,
      });
    });
  });
  let picked = 0;
  out.forEach((c) => {
    const on = c.oneDiff && c.recomputable && picked < MAX_VARIANTS;
    c.defaultOn = on;
    if (on) picked += 1;
  });
  return out;
}

// ---------- 批次 E：错误内联（按 code / message 中文，不臆造语义） ----------
function inlineText(r) {
  if (!r) return '前端异常：未获得实验响应';
  if (r.unavailable) return '实验服务不可用（网络异常 / 请求超时 / 端点未启用），请稍后重试';
  const code = r.code || 'SYS';
  // 与 templates.js 同款：优先后端原文（EX1 / EX12 在这两个接口的语义不同于 AOI 跨度，
  // 内置通用映射易误导），后端未给 message 时回退 errors.js 的按码内置映射。
  // 批次 D11：后端缺 B11 时返回 EX11 + 「该影像对未包含 B11 短波红外，不支持 NDBI/NDMI」
  // 原文，这里逐字展示后端 message，不臆造新错误码、不改 ERROR_MESSAGES。
  const zh = String(r.message || '').trim() || errorMessage({ code });
  return '[' + code + '] ' + zh;
}
function showInlineError(id, r) {
  const el = $(id);
  if (el) el.textContent = inlineText(r);
}

// ---------- 批次 E：分阶段进度（spinner + 阶段文案 + 每秒已耗时；请求结束清定时器） ----------
function makeProgress(boxId, statusId, elapsedId) {
  let timer = null;
  let t0 = 0;
  let stages = ['正在处理…'];
  return {
    start(list) {
      stages = (Array.isArray(list) && list.length) ? list : ['正在处理…'];
      t0 = Date.now();
      const box = $(boxId);
      const st = $(statusId);
      const ep = $(elapsedId);
      if (st) st.textContent = stages[0];
      if (ep) ep.textContent = '已耗时 0s';
      if (box) box.hidden = false;
      if (timer) window.clearInterval(timer);
      let step = 0;
      timer = window.setInterval(() => {
        const sec = Math.floor((Date.now() - t0) / 1000);
        const ep2 = $(elapsedId);
        const st2 = $(statusId);
        if (ep2) ep2.textContent = '已耗时 ' + sec + 's';
        step += 1;
        const idx = Math.min(step, stages.length - 1); // 末阶段保持，耗时持续更新，绝不空屏
        if (st2) st2.textContent = stages[idx];
      }, 1000);
    },
    stop() {
      if (timer) window.clearInterval(timer);
      timer = null;
      const box = $(boxId);
      if (box) box.hidden = true;
    },
  };
}

const PROG = {
  compare: makeProgress('exp-method-progress', 'exp-method-status', 'exp-method-elapsed'),
  sens: makeProgress('exp-sens-progress', 'exp-sens-status', 'exp-sens-elapsed'),
};

function compareStages(n) {
  const s = ['正在读取落盘影像与 AOI…'];
  for (let i = 1; i <= n; i += 1) s.push('正在复算变体 ' + i + '/' + n + '…');
  s.push('正在对齐有效像元并汇总…');
  return s;
}
const SENS_STAGES = [
  '正在读取落盘影像与 AOI…',
  '正在以基准 T 扫描阈值档 1/5…',
  '正在以基准 T 扫描阈值档 2/5…',
  '正在以基准 T 扫描阈值档 3/5…',
  '正在以基准 T 扫描阈值档 4/5…',
  '正在以基准 T 扫描阈值档 5/5…',
  '正在对齐有效像元并汇总…',
];

// ---------- 批次 E：按钮门控（无 run 置灰 + title；请求期间置灰） ----------
function syncButtons() {
  const runId = (DEPS.getRunId && DEPS.getRunId()) || '';
  const has = !!runId;
  const noRun = '请先运行变化检测或载入历史运行';
  const bc = $('btn-exp-compare');
  if (bc) {
    bc.disabled = !has || E.busyCompare;
    bc.title = has ? '运行对比实验：选择变体并对比三类面积' : noRun;
  }
  const bs = $('btn-exp-sensitivity');
  if (bs) {
    bs.disabled = !has || E.busySens;
    bs.title = has ? '运行敏感性分析：以基准 T 扫描五档阈值' : noRun;
  }
  // 批次 F：CSV 按钮 —— 结果区显示（最近一次成功实验）后才可用；无 run / 请求中禁用
  const bcc = $('btn-exp-compare-csv');
  if (bcc) {
    const ready = !!E.lastCompare && E.lastCompare.runId === runId;
    bcc.disabled = !has || !ready || E.busyCompare || E.busyCompareCsv;
    bcc.title = ready
      ? '导出方法对比 CSV（只读实验缓存，POST /result/compare/csv）'
      : (has ? '请先运行对比实验再导出 CSV' : noRun);
    if (!E.busyCompareCsv) bcc.textContent = '导出 CSV';
  }
  const bsc = $('btn-exp-sensitivity-csv');
  if (bsc) {
    const ready = !!has && E.sensRunId === runId;
    bsc.disabled = !has || !ready || E.busySens || E.busySensCsv;
    bsc.title = ready
      ? '导出阈值敏感性 CSV（只读实验缓存，POST /result/sensitivity/csv）'
      : (has ? '请先运行敏感性分析再导出 CSV' : noRun);
    if (!E.busySensCsv) bsc.textContent = '导出 CSV';
  }
}

// ---------- 批次 E：方法对比变体选择弹层 ----------
function renderVariantRows() {
  const list = $('exp-variant-list');
  if (!list) return;
  list.innerHTML = E.candidates.map((c) =>
    '<div class="exp-variant-row' + (c.defaultOn ? '' : ' is-off') + '" data-key="' + esc(c.key) + '">'
    + '<div class="exp-variant-head"><label>'
    + '<input type="checkbox" class="exp-variant-chk"' + (c.defaultOn ? ' checked' : '') + ' />'
    + esc(c.label) + '</label></div>'
    + (c.note ? '<div class="exp-variant-note">' + esc(c.note) + '</div>' : '')
    + '<div class="exp-variant-fields">'
    + '<select class="exp-variant-mode">'
    + '<option value="otsu">Otsu 自适应</option>'
    + '<option value="manual">手动</option>'
    + '</select>'
    + '<input type="number" class="exp-variant-thr" step="0.01" min="-0.99" max="0.99"'
    + ' placeholder="T (-1,1)" disabled />'
    + '</div></div>').join('');
}

function rowMode(r) { const el = r.querySelector('.exp-variant-mode'); return el ? el.value : 'otsu'; }
function rowThr(r) { const el = r.querySelector('.exp-variant-thr'); return el ? el.value : ''; }

function syncModalSubmit() {
  const list = $('exp-variant-list');
  const rows = list ? Array.prototype.slice.call(list.querySelectorAll('.exp-variant-row')) : [];
  const chosen = rows.filter((r) => {
    const chk = r.querySelector('.exp-variant-chk');
    return chk && chk.checked;
  });
  let msg = '';
  if (!chosen.length) msg = '请至少勾选 1 个变体';
  else if (chosen.length > MAX_VARIANTS) msg = '最多勾选 ' + MAX_VARIANTS + ' 个变体';
  else {
    for (const r of chosen) {
      if (rowMode(r) === 'manual') {
        const raw = rowThr(r);
        const t = Number(raw);
        if (raw === '' || !Number.isFinite(t) || t <= -1 || t >= 1) {
          msg = '手动阈值需落在 (-1, 1) 开区间';
          break;
        }
      }
    }
  }
  const err = $('exp-modal-err');
  if (err) err.textContent = msg;
  const run = $('exp-modal-run');
  if (run) run.disabled = !!msg || E.busyCompare;
}

function collectVariants() {
  const list = $('exp-variant-list');
  if (!list) return [];
  return Array.prototype.slice.call(list.querySelectorAll('.exp-variant-row'))
    .filter((r) => {
      const chk = r.querySelector('.exp-variant-chk');
      return chk && chk.checked;
    })
    .map((r) => {
      const c = E.candidates.find((x) => x.key === r.dataset.key) || {};
      const manual = rowMode(r) === 'manual';
      return {
        index: c.index,
        method: c.method,
        threshold_mode: manual ? 'manual' : 'otsu',
        threshold: manual ? Number(rowThr(r)) : null,
      };
    });
}

function openCompareModal() {
  if (E.busyCompare) return;
  const modal = $('exp-modal');
  if (!modal || !(DEPS.getRunId && DEPS.getRunId())) return;
  const base = baseConfig();
  E.candidates = buildCandidates(base);
  const b = $('exp-modal-base');
  if (b) {
    const mode = base.threshold_source === 'manual' ? 'manual' : 'otsu';
    b.textContent = '基准（只读）：' + configLabel(base.index, base.method, mode)
      + ' · T=' + fmtT(base.threshold)
      + '　·　本 run 已落盘指数：' + recomputableIndexes(base.index).join(' / ')
      + '（只读实验仅能复算这两个指数）';
  }
  renderVariantRows();
  modal.hidden = false;
  syncModalSubmit();
}

function closeCompareModal() {
  const modal = $('exp-modal');
  if (modal) modal.hidden = true;
  const err = $('exp-modal-err');
  if (err) err.textContent = '';
}

function onVariantChange(e) {
  const t = e.target;
  const row = t && t.closest ? t.closest('.exp-variant-row') : null;
  if (!row) return;
  if (t.classList && t.classList.contains('exp-variant-mode')) {
    const thr = row.querySelector('.exp-variant-thr');
    if (thr) thr.disabled = t.value !== 'manual';
  }
  if (t.classList && t.classList.contains('exp-variant-chk')) {
    row.classList.toggle('is-off', !t.checked);
  }
  syncModalSubmit();
}

function submitCompareModal() {
  if (E.busyCompare) return;
  syncModalSubmit();
  const run = $('exp-modal-run');
  if (run && run.disabled) return;
  const variants = collectVariants();
  if (!variants.length) return;
  closeCompareModal();
  runCompare(variants);
}

function bind() {
  const bc = $('btn-exp-compare');
  if (bc) bc.onclick = openCompareModal;
  const bs = $('btn-exp-sensitivity');
  if (bs) bs.onclick = runSensitivity;
  // 批次 F：两个「导出 CSV」按钮绑定（结果区显示后才由 syncButtons 解除禁用）
  const bcc = $('btn-exp-compare-csv');
  if (bcc) bcc.onclick = onExportCompareCsv;
  const bsc = $('btn-exp-sensitivity-csv');
  if (bsc) bsc.onclick = onExportSensitivityCsv;

  const modal = $('exp-modal');
  if (modal) {
    modal.addEventListener('click', (e) => {
      if (e.target === modal) { closeCompareModal(); return; }
      if (e.target.closest && e.target.closest('#exp-modal-cancel')) closeCompareModal();
      else if (e.target.closest && e.target.closest('#exp-modal-run')) submitCompareModal();
    });
    const list = $('exp-variant-list');
    if (list) {
      list.addEventListener('change', onVariantChange);
      list.addEventListener('input', syncModalSubmit);
    }
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !modal.hidden) closeCompareModal();
    });
  }
  syncButtons();
}

// ---------- 批次 E：方法对比结果渲染 ----------
function renderCompareResult(data) {
  const base = (data && data.base) || {};
  const variants = (data && Array.isArray(data.variants)) ? data.variants : [];
  const baseA = areasMap(base.areas);
  const baseTrans = Number(base.transition_pct || 0);

  const meta = $('exp-method-meta');
  if (meta) {
    meta.innerHTML = '计算时间：' + esc(data && data.computed_at)
      + '　·　结果已缓存，重复请求秒开（experiments.json，口径与配置可复现）。'
      + '<br/>面积单位 ha、占比单位 %（O=Otsu 自适应 / M=手动阈值）；表格较宽，可左右滑动查看全部列。';
  }

  const groups = [{ name: '基准', stats: base.areas || [] }].concat(variants.map((v) => ({
    name: shortLabel((v.config || {}).index, (v.config || {}).method),
    stats: v.areas || [],
  })));
  C.renderMethodGroups($('chart-method'), groups);

  const head = '<thead><tr><th>配置</th><th>T</th><th>增强</th><th>退化</th>'
    + '<th>稳定</th><th>过渡带</th><th>一致率</th></tr></thead>';
  const baseMode = base.threshold_mode || (base.threshold_source === 'manual' ? 'manual' : 'otsu');
  const baseCfg = configLabel(base.index, base.method, baseMode);
  const baseRow = '<tr class="exp-base"><td class="k" title="' + esc(baseCfg) + '">'
    + esc(shortLabel(base.index, base.method)) + (baseMode === 'manual' ? '·M' : '·O') + '</td>'
    + '<td>' + esc(fmtT(base.threshold)) + '</td>'
    + '<td>' + ha(baseA[1]) + '</td><td>' + ha(baseA[2]) + '</td><td>' + ha(baseA[3]) + '</td>'
    + '<td>' + pct(baseTrans) + '</td><td>—</td></tr>';

  const cd = (on, val) => '<td' + (on ? ' class="exp-cell-diff"' : '') + '>' + val + '</td>';
  const varRows = variants.map((v) => {
    const cfg = v.config || {};
    const a = areasMap(v.areas);
    const trans = Number(v.transition_pct || 0);
    const mode = cfg.threshold_mode || (v.threshold != null ? 'manual' : 'otsu');
    const d = {
      thr: Number(Number(v.threshold || 0).toFixed(4)) !== Number(Number(base.threshold || 0).toFixed(4)),
      a1: a[1] !== baseA[1], a2: a[2] !== baseA[2], a3: a[3] !== baseA[3],
      trans: Number(trans.toFixed(1)) !== Number(baseTrans.toFixed(1)),
    };
    const any = d.thr || d.a1 || d.a2 || d.a3 || d.trans;
    const fullCfg = configLabel(cfg.index, cfg.method, mode);
    return '<tr' + (any ? ' class="exp-diff"' : '') + '>'
      + '<td class="k" title="' + esc(fullCfg) + '">'
      + esc(shortLabel(cfg.index, cfg.method)) + (mode === 'manual' ? '·M' : '·O') + '</td>'
      + cd(d.thr, esc(fmtT(v.threshold)))
      + cd(d.a1, ha(a[1])) + cd(d.a2, ha(a[2])) + cd(d.a3, ha(a[3]))
      + cd(d.trans, pct(trans))
      + '<td>' + (v.agreement_pct != null ? pct(v.agreement_pct) : '—') + '</td></tr>';
  }).join('');
  const tbl = $('exp-method-table');
  if (tbl) tbl.innerHTML = head + '<tbody>' + baseRow + varRows + '</tbody>';
  const res = $('exp-method-result');
  if (res) res.hidden = false;
  const empty = $('exp-method-empty');
  if (empty) empty.hidden = true;
  C.resizeExperiments();
}

// ---------- 批次 E：敏感性自动结论（相邻档「增强+退化合计」变化绝对值最大的一档） ----------
function sensConclusion(sweep) {
  const sum = (r) => {
    const a = areasMap(r && r.areas);
    return Number((a[1] + a[2]).toFixed(2));
  };
  let best = null;
  for (let i = 1; i < sweep.length; i += 1) {
    const from = sweep[i - 1];
    const to = sweep[i];
    const delta = Math.abs(sum(to) - sum(from));
    const dev = Math.max(Math.abs(Number(from.scale) - 1), Math.abs(Number(to.scale) - 1));
    if (!best || delta > best.delta) {
      best = { delta, from: Number(from.scale), to: Number(to.scale), dev };
    }
  }
  if (!best) return '本次扫描未返回可比较的相邻档，无法给出敏感区间（如实说明，不作定性结论）。';
  const x = Math.round(best.dev * 100);
  const d = Number(best.delta.toFixed(2)).toFixed(2);
  return '结果对阈值敏感区间在 ±' + x + '%：最陡档 ' + fmtScale(best.from) + '→' + fmtScale(best.to)
    + '，增强＋退化面积合计变化 ' + d + ' ha（数字全部取自本次五档实测扫描）。';
}

function renderSensitivityResult(data) {
  const sweep = (data && Array.isArray(data.sweep))
    ? data.sweep.slice().sort((a, b) => Number(a.scale) - Number(b.scale)) : [];
  const meta = $('exp-sens-meta');
  if (meta) {
    meta.innerHTML = '基准 T：' + esc(fmtT(data && data.base_threshold))
      + '　·　计算时间：' + esc(data && data.computed_at)
      + '<br/>面积单位 ha、占比单位 %；表格较宽，可左右滑动查看全部列。';
  }
  C.renderSensitivityLine($('chart-sensitivity'), { ...(data || {}), sweep });
  const concl = $('exp-sens-conclusion');
  if (concl) concl.textContent = sensConclusion(sweep);

  const head = '<thead><tr><th>倍数</th><th>T</th><th>增强</th><th>退化</th>'
    + '<th>稳定</th><th>过渡带</th></tr></thead>';
  const rows = sweep.map((r) => {
    const a = areasMap(r.areas);
    const isBase = Number(r.scale) === 1;
    return '<tr' + (isBase ? ' class="exp-base"' : '') + '>'
      + '<td>' + esc(fmtScale(r.scale)) + '</td>'
      + '<td>' + esc(fmtT(r.t)) + '</td>'
      + '<td>' + ha(a[1]) + '</td><td>' + ha(a[2]) + '</td><td>' + ha(a[3]) + '</td>'
      + '<td>' + pct(r.transition_pct) + '</td></tr>';
  }).join('');
  const tbl = $('exp-sens-table');
  if (tbl) tbl.innerHTML = head + '<tbody>' + rows + '</tbody>';
  const res = $('exp-sens-result');
  if (res) res.hidden = false;
  const empty = $('exp-sens-empty');
  if (empty) empty.hidden = true;
  C.resizeExperiments();
}

// ---------- 批次 E：两个长耗时请求（判别式返回，run 切换后丢弃过期结果） ----------
async function runCompare(variants) {
  if (E.busyCompare) return;
  const runId = DEPS.getRunId && DEPS.getRunId();
  if (!runId) return;
  const btn = $('btn-exp-compare');
  E.busyCompare = true;
  if (btn) { btn.disabled = true; btn.textContent = '运行中…'; }
  const err = $('exp-method-err');
  if (err) err.textContent = '';
  PROG.compare.start(compareStages(variants.length));
  try {
    const r = await api.compareVariants({ run_id: runId, variants, force: false });
    if (r.ok) {
      if ((DEPS.getRunId && DEPS.getRunId()) !== runId) return; // run 已切换，丢弃过期结果
      renderCompareResult(r.data);
      // 批次 F：记住本次成功实验的提交入参；CSV 端点按同一 variants 签名只读命中缓存
      E.lastCompare = { runId, variants: variants.map((v) => ({ ...v })) };
    } else {
      showInlineError('exp-method-err', r);
    }
  } catch (e) {
    showInlineError('exp-method-err', { message: (e && e.message) || '' });
  } finally {
    PROG.compare.stop();
    E.busyCompare = false;
    if (btn) btn.textContent = '运行对比实验';
    syncButtons();
  }
}

async function runSensitivity() {
  if (E.busySens) return;
  const runId = DEPS.getRunId && DEPS.getRunId();
  if (!runId) return;
  const btn = $('btn-exp-sensitivity');
  E.busySens = true;
  if (btn) { btn.disabled = true; btn.textContent = '运行中…'; }
  const err = $('exp-sens-err');
  if (err) err.textContent = '';
  PROG.sens.start(SENS_STAGES);
  try {
    const r = await api.thresholdSensitivity({ run_id: runId, force: false });
    if (r.ok) {
      if ((DEPS.getRunId && DEPS.getRunId()) !== runId) return; // run 已切换，丢弃过期结果
      renderSensitivityResult(r.data);
      // 批次 F：敏感性 CSV 只需 run_id；记录结果区显示所属 run，避免导出错 run
      E.sensRunId = runId;
    } else {
      showInlineError('exp-sens-err', r);
    }
  } catch (e) {
    showInlineError('exp-sens-err', { message: (e && e.message) || '' });
  } finally {
    PROG.sens.stop();
    E.busySens = false;
    if (btn) btn.textContent = '运行敏感性分析';
    syncButtons();
  }
}

// ---------- 批次 F：实验结果 CSV 导出（只读缓存，POST /result/compare/csv · /result/sensitivity/csv） ----------
// 文件名与后端 Content-Disposition 同名：compare_<run_id 前8位>.csv / sensitivity_<run_id 前8位>.csv。
function csvFilename(prefix, runId) {
  return prefix + '_' + String(runId || '').slice(0, 8) + '.csv';
}

// 方法对比：CSV 入参同 compare（variants 原样回传，缓存签名才能命中），force:false 固定下发。
async function onExportCompareCsv() {
  if (E.busyCompareCsv) return;
  const runId = (DEPS.getRunId && DEPS.getRunId()) || '';
  // 结果区显示且签名入参归属当前 run 才允许导出（避免导出错 run 的 CSV）
  if (!runId || !E.lastCompare || E.lastCompare.runId !== runId) return;
  const btn = $('btn-exp-compare-csv');
  E.busyCompareCsv = true;
  if (btn) { btn.disabled = true; btn.textContent = '导出中…'; }
  const err = $('exp-method-csv-err');
  if (err) err.textContent = '';
  try {
    const r = await api.exportCompareCsv({
      run_id: runId,
      variants: E.lastCompare.variants,
      force: false,
    });
    if ((DEPS.getRunId && DEPS.getRunId()) !== runId) return; // run 已切换，丢弃过期结果
    if (r.ok) {
      // 复用 downloadArtifact 抽出的同一 Blob 下载通道，不另写一套下载逻辑
      api.downloadBlob(r.blob, csvFilename('compare', runId));
    } else {
      // 后端中文提示内联展示（EX3 → 「请先在页面执行实验」），不臆造语义、不弹全局横幅
      showInlineError('exp-method-csv-err', r);
    }
  } catch (e) {
    // 判别式返回不应抛异常；兜底内联，绝不产生未捕获 JS 异常
    showInlineError('exp-method-csv-err', { message: (e && e.message) || '' });
  } finally {
    E.busyCompareCsv = false;
    syncButtons();
  }
}

// 阈值敏感性：CSV 入参只需 run_id（+ force:false），结果区显示后才可用。
async function onExportSensitivityCsv() {
  if (E.busySensCsv) return;
  const runId = (DEPS.getRunId && DEPS.getRunId()) || '';
  if (!runId || E.sensRunId !== runId) return;
  const btn = $('btn-exp-sensitivity-csv');
  E.busySensCsv = true;
  if (btn) { btn.disabled = true; btn.textContent = '导出中…'; }
  const err = $('exp-sens-csv-err');
  if (err) err.textContent = '';
  try {
    const r = await api.exportSensitivityCsv({ run_id: runId, force: false });
    if ((DEPS.getRunId && DEPS.getRunId()) !== runId) return; // run 已切换，丢弃过期结果
    if (r.ok) {
      api.downloadBlob(r.blob, csvFilename('sensitivity', runId));
    } else {
      showInlineError('exp-sens-csv-err', r);
    }
  } catch (e) {
    showInlineError('exp-sens-csv-err', { message: (e && e.message) || '' });
  } finally {
    E.busySensCsv = false;
    syncButtons();
  }
}

// ---------- 批次 E：run 变化时清空上一次实验结果，避免展示错 run 的数据 ----------
export function onRunChanged() {
  ['exp-method-result', 'exp-sens-result'].forEach((id) => { const el = $(id); if (el) el.hidden = true; });
  ['exp-method-empty', 'exp-sens-empty'].forEach((id) => { const el = $(id); if (el) el.hidden = false; });
  ['exp-method-err', 'exp-sens-err', 'exp-method-meta', 'exp-sens-meta', 'exp-sens-conclusion',
    'exp-method-csv-err', 'exp-sens-csv-err']
    .forEach((id) => { const el = $(id); if (el) el.textContent = ''; });
  ['exp-method-table', 'exp-sens-table'].forEach((id) => { const el = $(id); if (el) el.innerHTML = ''; });
  // 批次 F：清空最近一次成功实验的签名入参与 CSV 请求态，避免导出错 run 的 CSV
  E.lastCompare = null;
  E.sensRunId = '';
  E.busyCompareCsv = false;
  E.busySensCsv = false;
  syncButtons();
}

/**
 * 批次 E：初始化结果卡第 4/5 页签。
 * @param {{getRunId:Function, getBase:Function}} deps
 *   getRunId () => 当前 run_id（无则空串）；getBase () => S.change（读 params_snapshot 的口径）。
 */
export function initExperiments(deps) {
  const d = deps || {};
  DEPS.getRunId = d.getRunId || null;
  DEPS.getBase = d.getBase || null;
  bind();
}
