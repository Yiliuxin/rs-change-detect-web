// -*- coding: utf-8 -*-
// 批次 D（NF9 参数快照模板化 / REQ-04 口径确认）：A 卡「锁定口径面板」下方的模板区。
// 只消费 /api/v1/templates 的 POST / GET / DELETE 三个自建扩展接口；
// 前端只提交口径面板的白名单四键（index / method / block_size / threshold），
// 回填时只改面板状态、**不自动提交**（仍走既有「应用 AOI / 确认口径 → 运行变化检测」链路）。
//
// 三态与降级（与既有 demo 降级风格一致，不弹阻塞式错误）：
//   - 正常：下拉列出本人模板（created_at 倒序），选中即回填；
//   - 空列表：下拉显示「暂无模板，设置好口径后点保存」；
//   - 降级：端点裸 404 / 网络异常 → 下拉禁用 + 「模板服务不可用」提示，主流程完全不受影响。
// 业务错误（TMPL-01 同名 409 / EX1 参数非法 400 / TMPL-02 不存在 404）按 code 展示中文，
// 文案来自 errors.js 的内置映射（逐字对齐后端 constants.py），绝不臆造语义。

import * as api from './api.js';
import { errorMessage } from './errors.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

const NAME_MAX = 64;                 // 后端 TEMPLATE_NAME_MAX，与输入框 maxlength 一致
const VALID_INDEX = ['NDVI', 'NDWI'];
const VALID_METHOD = ['otsu', 'regression', 'cva'];   // 批次 G：与后端 VALID_CHANGE_METHODS 同步
const VALID_BLOCK = [256, 512];
const METHOD_LABELS = { otsu: '差值法', regression: '回归法', cva: '变化向量分析' };

// 模块内状态：模板列表 / 当前选中 / 请求中 / 是否已降级 / 面板读写回调（由 main.js 注入）
const T = {
  items: [],
  sel: '',
  busy: false,
  unavailable: false,
  pending: null,        // 命名弹层打开时的口径快照
  getParams: null,      // () => {index, method, block_size, threshold}
  applyParams: null,    // (params) => {changed:[...], note?:string}（只改面板，不提交）
};

// ---------- 提示条（沿用现有 .err-line / .ok-line / .tmpl-hint 风格） ----------
function setErr(text) {
  const el = $('tmpl-err');
  if (el) el.textContent = text || '';
}

function setHint(text) {
  const el = $('tmpl-hint');
  if (el) el.textContent = text || '';
}

function setOk(text, muted) {
  const el = $('tmpl-ok');
  if (!el) return;
  el.hidden = !text;
  el.textContent = text || '';
  el.classList.toggle('muted', !!muted);
}

// 业务错误体 → 「[code] 中文」。
// 优先用后端原文：模板口径参数按 EX1 口径报错时后端给的是「检测参数非法：<具体项>」，
// 比 EX1 的内置通用文案更精确（内置 EX1 文案是 AOI 跨度，套到参数错误上会产生误导）；
// 后端未给 message 时回退 errors.js 的按码内置映射，两者都不臆造语义。
function codeText(r) {
  const code = (r && r.code) || 'SYS';
  const zh = String((r && r.message) || '').trim() || errorMessage({ code });
  return '[' + code + '] ' + zh;
}

// ---------- 口径参数规范化（与后端 normalize_params 同口径，仅用于回填/预览） ----------
// index 大写、method 小写、block_size 整数、threshold 数字或 null（null = Otsu 自适应）。
function normalizeParams(raw) {
  const p = raw && typeof raw === 'object' ? raw : {};
  const out = {};
  const idx = String(p.index == null ? '' : p.index).trim().toUpperCase();
  if (VALID_INDEX.includes(idx)) out.index = idx;
  const method = String(p.method == null ? '' : p.method).trim().toLowerCase();
  if (VALID_METHOD.includes(method)) out.method = method;
  const block = Number(p.block_size);
  if (VALID_BLOCK.includes(block)) out.block_size = block;
  if (p.threshold == null || p.threshold === '') out.threshold = null;
  else {
    const t = Number(p.threshold);
    out.threshold = Number.isFinite(t) ? t : null;
  }
  return out;
}

function paramsSummary(p) {
  const idx = p.index || '—';
  const method = METHOD_LABELS[p.method] || p.method || '—';
  const block = p.block_size != null ? p.block_size + ' px' : '—';
  const thr = p.threshold == null
    ? 'Otsu 自适应'
    : '手动 T=' + (p.threshold >= 0 ? '+' : '') + Number(p.threshold).toFixed(2);
  return '指数 ' + idx + ' · 方法 ' + method + ' · 分块 ' + block + ' · 阈值 ' + thr;
}

// ---------- 下拉 / 按钮态 ----------
function syncButtons() {
  const save = $('tmpl-save');
  if (save) {
    save.disabled = T.busy || T.unavailable;
    save.title = T.unavailable
      ? '模板服务不可用，暂不能保存'
      : '把当前口径面板（index / method / block_size / threshold）保存为模板';
  }
  const del = $('tmpl-del');
  if (del) {
    del.disabled = T.busy || T.unavailable || !T.sel;
    del.title = T.sel ? '删除当前选中的模板' : '请先在下拉中选择一个模板';
  }
}

function renderSelect() {
  const sel = $('tmpl-select');
  const cnt = $('tmpl-count');
  if (!sel) return;
  if (T.unavailable) {
    sel.innerHTML = '<option value="">模板服务不可用</option>';
    sel.value = '';
    sel.disabled = true;
    if (cnt) cnt.textContent = '不可用';
    syncButtons();
    return;
  }
  if (!T.items.length) {
    sel.innerHTML = '<option value="">暂无模板，设置好口径后点保存</option>';
    sel.value = '';
    sel.disabled = true;                        // 无模板时不接受选择，但文案常驻可见
    if (cnt) cnt.textContent = '0 个';
    syncButtons();
    return;
  }
  sel.innerHTML = '<option value="">— 选择模板回填口径 —</option>'
    + T.items.map((it) => '<option value="' + esc(it.id) + '">' + esc(it.name) + '</option>').join('');
  sel.disabled = T.busy;
  sel.value = T.sel || '';
  if (cnt) cnt.textContent = '共 ' + T.items.length + ' 个';
  syncButtons();
}

// ---------- 降级 ----------
function degrade() {
  T.unavailable = true;
  T.items = [];
  T.sel = '';
  renderSelect();
  setOk('');
  setHint('模板服务不可用（/api/v1/templates 未启用或网络异常）。口径面板与检测主流程不受影响。');
}

// ---------- 加载列表 ----------
async function loadTemplates() {
  const r = await api.listTemplates();
  if (r.ok) {
    T.unavailable = false;
    T.items = Array.isArray(r.data && r.data.items) ? r.data.items : [];
    if (!T.items.some((it) => it.id === T.sel)) T.sel = '';   // 选中项已不存在 → 清空选择
    renderSelect();
    setErr('');
    return true;
  }
  if (r.unavailable) { degrade(); return false; }
  // 业务错误：按 code 中文提示，列表保持为空但不阻断主流程
  T.items = [];
  T.sel = '';
  renderSelect();
  setErr(codeText(r));
  return false;
}

// ---------- 命名弹层 ----------
function defaultName(p) {
  const thr = p.threshold == null ? 'otsu' : 'T' + Number(p.threshold).toFixed(2);
  return [p.index || 'NDVI', p.method || 'otsu', p.block_size || 256, thr].join('-');
}

function modalErr(text) {
  const el = $('tmpl-modal-err');
  if (el) el.textContent = text || '';
}

function openModal() {
  if (T.busy || T.unavailable) return;
  const modal = $('tmpl-modal');
  const input = $('tmpl-name');
  if (!modal || !input) return;
  const params = normalizeParams(T.getParams ? T.getParams() : {});
  T.pending = params;
  modalErr('');
  const summary = $('tmpl-modal-params');
  if (summary) summary.textContent = paramsSummary(params);
  input.value = defaultName(params);
  modal.hidden = false;
  input.focus();
  input.select();
}

function closeModal() {
  const modal = $('tmpl-modal');
  if (modal) modal.hidden = true;
  modalErr('');
  T.pending = null;
}

async function submitSave() {
  if (T.busy) return;
  const input = $('tmpl-name');
  const name = String((input && input.value) || '').trim();
  if (!name) { modalErr('请输入模板名称（1–' + NAME_MAX + ' 个字符）'); return; }
  if (name.length > NAME_MAX) { modalErr('模板名称最多 ' + NAME_MAX + ' 个字符'); return; }
  const params = T.pending || normalizeParams(T.getParams ? T.getParams() : {});
  T.busy = true;
  syncButtons();
  modalErr('');
  try {
    const r = await api.createTemplate(name, params);
    if (r.ok) {
      closeModal();
      setErr('');
      setOk('✓ 已保存模板「' + name + '」：' + paramsSummary(params));
      T.sel = String((r.data && r.data.id) || '');
      await loadTemplates();
      renderSelect();                       // 刷新后保留新模板为当前选中
      return;
    }
    if (r.unavailable) { closeModal(); degrade(); return; }
    // 409 TMPL-01 重名 / 400 EX1 参数非法：留在弹层内按 code 展示，不关闭弹层
    modalErr(codeText(r));
  } finally {
    T.busy = false;
    syncButtons();
  }
}

// ---------- 回填 / 删除 ----------
function onPickTemplate() {
  const sel = $('tmpl-select');
  const id = sel ? String(sel.value || '') : '';
  T.sel = id;
  syncButtons();
  setErr('');
  if (!id) { setOk(''); setHint('选择模板后仅回填口径面板，不会自动提交检测。'); return; }
  const it = T.items.find((x) => x.id === id);
  if (!it) { setOk(''); return; }
  const params = normalizeParams(it.params);
  const res = (T.applyParams && T.applyParams(params)) || {};
  const changed = Array.isArray(res.changed) ? res.changed : [];
  // 可见反馈：与当前口径不符 → 列出差异；完全一致 → 说明无需改动（.ok-line.muted 弱化）
  if (changed.length) {
    setOk('✓ 已回填模板「' + it.name + '」：' + changed.join('、') + '（未自动提交，请点「运行变化检测」应用）', false);
  } else {
    setOk('模板「' + it.name + '」与当前口径一致，无需改动（' + paramsSummary(params) + '）', true);
  }
  if (res.note) setErr(res.note);            // 超出面板可表达范围的项如实说明，不偷偷改值
  else setErr('');
  setHint('模板回填只改面板状态：如需生效，请点 C 卡「运行变化检测」（仍走既有口径确认链路）。');
}

async function removeSelected() {
  if (T.busy || T.unavailable || !T.sel) return;
  const id = T.sel;
  const it = T.items.find((x) => x.id === id);
  const name = it ? String(it.name || '') : '';
  T.busy = true;
  syncButtons();
  try {
    const r = await api.deleteTemplate(id);
    if (r.ok) {
      T.sel = '';                            // 成功后清空选择并刷新下拉
      setErr('');
      setOk('✓ 已删除模板' + (name ? '「' + name + '」' : ''));
      await loadTemplates();
      setHint('模板已删除。下拉选中项已清空，重新选择后才可再次删除。');
      return;
    }
    if (r.unavailable) { degrade(); return; }
    setErr(codeText(r));                     // 404 TMPL-02 等按 code 中文提示
  } finally {
    T.busy = false;
    syncButtons();
  }
}

// ---------- 绑定 ----------
function bind() {
  const sel = $('tmpl-select');
  if (sel) sel.addEventListener('change', onPickTemplate);
  const save = $('tmpl-save');
  if (save) save.onclick = openModal;
  const del = $('tmpl-del');
  if (del) del.onclick = removeSelected;

  const modal = $('tmpl-modal');
  if (modal) {
    modal.addEventListener('click', (e) => {
      if (e.target === modal) { closeModal(); return; }       // 点遮罩取消
      if (e.target.closest && e.target.closest('#tmpl-modal-cancel')) closeModal();
      else if (e.target.closest && e.target.closest('#tmpl-modal-save')) submitSave();
    });
    const input = $('tmpl-name');
    if (input) {
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); submitSave(); }
        else if (e.key === 'Escape') { e.preventDefault(); closeModal(); }
      });
    }
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !modal.hidden) closeModal();
    });
  }
}

/**
 * 初始化模板区。
 * @param {{getParams:Function, applyParams:Function}} opts
 *   getParams   读取当前口径面板四键（阈值方式由 threshold 是否为空派生）；
 *   applyParams 把模板参数回填到面板控件，返回 {changed:[...], note?:string}（不得自动提交）。
 */
export function initTemplates(opts) {
  const o = opts || {};
  T.getParams = o.getParams || null;
  T.applyParams = o.applyParams || null;
  bind();
  renderSelect();
  syncButtons();
  loadTemplates();
  return { refresh: loadTemplates };
}
