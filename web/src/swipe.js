// -*- coding: utf-8 -*-
// 结果卡「双时相对比」左右卷帘（REQ-10）：t1/t2_composite.png 重叠定位，
// 竖直分隔条用 pointer 事件拖动；该 run 无合成图产物时置灰提示，绝不留白块、不报错。

const $ = (id) => document.getElementById(id);

export const NO_COMPOSITE = '该运行未导出双时相合成图，请重新运行检测后使用';
const NO_RUN = '请先在「历史运行」卡片选择一条运行记录，或运行一次变化检测。';

let pos = 50;              // 左侧 t1 可见比例（%），拖动后保持，不回弹
let dragging = false;
let bound = false;

function apply(p) {
  pos = Math.max(2, Math.min(98, Number(p) || 50));
  const clip = $('swipe-clip');
  const divider = $('swipe-divider');
  if (!clip || !divider) return;
  // 左图用 clip-path 裁切：右侧裁掉 (100-pos)%，分隔条同步定位
  clip.style.clipPath = 'inset(0 ' + (100 - pos).toFixed(2) + '% 0 0)';
  divider.style.left = pos.toFixed(2) + '%';
  divider.setAttribute('aria-valuenow', String(Math.round(pos)));
}

function pctFromEvent(e) {
  const stage = $('swipe-stage');
  if (!stage) return pos;
  const r = stage.getBoundingClientRect();
  if (!r.width) return pos;
  return ((e.clientX - r.left) / r.width) * 100;
}

function onDown(e) {
  const stage = $('swipe-stage');
  if (!stage || stage.hidden) return;
  e.preventDefault();
  dragging = true;
  try { $('swipe-divider').setPointerCapture(e.pointerId); } catch (_) { /* 部分浏览器不支持 */ }
  apply(pctFromEvent(e));
}

function onMove(e) {
  if (!dragging) return;
  apply(pctFromEvent(e));
}

function onUp(e) {
  if (!dragging) return;
  dragging = false;
  try { $('swipe-divider').releasePointerCapture(e.pointerId); } catch (_) { /* 忽略 */ }
}

function onKey(e) {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  e.preventDefault();
  apply(pos + (e.key === 'ArrowRight' ? 5 : -5));
}

export function initSwipe() {
  if (bound) return;
  const stage = $('swipe-stage');
  const divider = $('swipe-divider');
  if (!stage || !divider) return;
  stage.addEventListener('pointerdown', onDown);
  divider.addEventListener('pointerdown', onDown);
  divider.addEventListener('keydown', onKey);
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
  window.addEventListener('pointercancel', onUp);
  bound = true;
  apply(pos);
  showUnavailable(NO_RUN);
}

export function showUnavailable(msg) {
  const note = $('swipe-unavailable');
  const stage = $('swipe-stage');
  const head = $('swipe-head');
  if (note) { note.textContent = msg || NO_COMPOSITE; note.hidden = false; }
  if (stage) stage.hidden = true;
  if (head) head.textContent = '双时相卷帘 · 不可用';
}

/**
 * 设置两时相图源。缺任一张 → 置灰提示（不显示空白图框）。
 * @param {{t1Url?:string,t2Url?:string,t1?:string,t2?:string,runId?:string}} src
 */
export function setSwipeSources(src) {
  const s = src || {};
  if (!s.t1Url || !s.t2Url) { showUnavailable(NO_COMPOSITE); return false; }
  const t1 = $('swipe-img-t1');
  const t2 = $('swipe-img-t2');
  const head = $('swipe-head');
  if (!t1 || !t2) return false;
  t1.src = s.t1Url;
  t2.src = s.t2Url;
  const l1 = $('swipe-label-t1'), l2 = $('swipe-label-t2');
  if (l1) l1.textContent = 't1 ' + (s.t1 || '—');
  if (l2) l2.textContent = 't2 ' + (s.t2 || '—');
  if (head) head.textContent = '双时相卷帘 · ' + (s.runId ? String(s.runId).slice(0, 8) : '本轮检测');
  const note = $('swipe-unavailable');
  const stage = $('swipe-stage');
  if (note) note.hidden = true;
  if (stage) stage.hidden = false;
  apply(pos); // 切换 run 时保留上一次拖动位置
  return true;
}

/** 由 run 详情产物清单驱动卷帘（旧 run 没有合成图 → 置灰提示）。 */
export function setSwipeRun(detail) {
  const arts = Array.isArray(detail && detail.artifacts) ? detail.artifacts : [];
  const find = (name) => {
    const a = arts.find((x) => x && x.name === name);
    return a ? String(a.url || '') : '';
  };
  const snap = (detail && detail.params_snapshot) || {};
  return setSwipeSources({
    t1Url: find('t1_composite.png'),
    t2Url: find('t2_composite.png'),
    t1: detail && (detail.t1 || snap.t1),
    t2: detail && (detail.t2 || snap.t2),
    runId: detail && detail.run_id,
  });
}
