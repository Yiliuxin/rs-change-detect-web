// -*- coding: utf-8 -*-
// 仅调用 HLD 2.2.3 的 5 个接口（统一前缀 /api/v1），不直连算法/数据库。
// 所有接口都带「演示降级层」：网络失败 / 404-405 / 返回空占位 → 本地演示数据。
// 后端真实业务错误体 {code,message,run_id} 一律抛出 ApiError，交由全局横幅展示，绝不降级吞掉。

import { ApiError } from './errors.js';
import * as demo from './demo.js';

const BASE = '/api/v1';
const TIMEOUT_MS = 2500; // constants.py API_TIMEOUT_SEC = 3

function fetchWithTimeout(url, options) {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  return fetch(url, { ...options, signal: ctrl.signal }).finally(() => window.clearTimeout(timer));
}

// 读取后端统一错误体；不是错误体则返回 null
async function safeError(resp) {
  try {
    const j = await resp.clone().json();
    if (j && j.code) return new ApiError(j.code, j.message, j.run_id);
  } catch (_) { /* 非 JSON 错误页 */ }
  return null;
}

function jsonOptions(body) {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}

/**
 * 通用请求：真实数据 / 空占位降级 / 网络失败降级 / 业务错误抛出
 * @returns {Promise<{data:object, demo:boolean}>}
 */
async function call(path, body, isPlaceholder, fallback) {
  let resp;
  try {
    resp = await fetchWithTimeout(BASE + path, jsonOptions(body));
  } catch (_) {
    return { data: fallback(), demo: true }; // 8000 未启动 / 超时
  }
  if (!resp.ok) {
    const err = await safeError(resp);
    if (err) throw err;                     // 真实业务错误 → 全局展示
    return { data: fallback(), demo: true }; // 404 / 405 / 5xx 非契约错误 → 降级
  }
  let data;
  try {
    data = await resp.json();
  } catch (_) {
    return { data: fallback(), demo: true };
  }
  if (isPlaceholder(data)) return { data: fallback(), demo: true };
  return { data, demo: false };
}

// ---------- 1) REQ-01：AOI 提交 ----------
export function selectAoi(payload) {
  return call(
    '/aoi/select',
    payload,
    (d) => !d.run_id && !d.aoi_id && (!d.matched_scenes || !d.matched_scenes.length),
    () => demo.demoAoiSelect(payload),
  );
}

// ---------- 2) REQ-02：列出双时相影像对（GET → POST → 本地 seed 兼容）----------
function pickPairs(j) {
  if (!j || typeof j !== 'object') return null;
  if (Array.isArray(j.pairs) && j.pairs.length) return j.pairs;
  if (Array.isArray(j.scene_pairs) && j.scene_pairs.length) return j.scene_pairs;
  return null;
}

export async function listScenePairs(aoiId) {
  // ① 用户口径：GET /api/v1/scene/match 用于「列出双时相影像对」
  try {
    const g = await fetchWithTimeout(BASE + '/scene/match', {
      method: 'GET', headers: { 'Content-Type': 'application/json' },
    });
    if (g.ok) {
      const pairs = pickPairs(await g.json());
      if (pairs) return { data: { pairs }, demo: false, via: 'GET' };
    } else {
      const err = await safeError(g);
      if (err) throw err;
    }
  } catch (e) {
    if (e instanceof ApiError) throw e;
    // 404 / 405 / 网络失败 → 回退
  }

  // ② 回退：仓库实际路由 POST /api/v1/scene/match
  try {
    const p = await fetchWithTimeout(BASE + '/scene/match', jsonOptions({
      aoi_id: aoiId || '', scene_t1_id: '', scene_t2_id: '', block_size: 256,
    }));
    if (p.ok) {
      const pairs = pickPairs(await p.json());
      if (pairs) return { data: { pairs }, demo: false, via: 'POST' };
    } else {
      const err = await safeError(p);
      if (err) throw err;
    }
  } catch (e) {
    if (e instanceof ApiError) throw e;
  }

  // ③ 本地演示数据
  return { data: demo.demoScenePairs(), demo: true, via: 'SEED' };
}

// ---------- 3) REQ-02/04：影像对口径确认 ----------
export function matchScene(payload) {
  return call(
    '/scene/match',
    payload,
    (d) => !d.run_id && !d.formula_id,
    () => demo.demoSceneMatch(payload),
  );
}

// ---------- 4) REQ-05/08/10/11：变化检测主流程 ----------
export function runChange(payload) {
  return call(
    '/result/change',
    payload,
    (d) => !d.run_id && !d.change_png && (!d.stats || !d.stats.length),
    () => demo.demoChange(payload),
  );
}

// ---------- 5) REQ-09：样本精度评价 ----------
export function submitSamples(payload) {
  return call(
    '/accuracy/sample',
    payload,
    (d) => !d.confusion_matrix || !d.confusion_matrix.length,
    () => demo.demoAccuracy(payload),
  );
}

// ---------- 6) REQ-12/14/15：报告导出 ----------
export function exportReport(payload) {
  return call(
    '/report/export',
    payload,
    (d) => !d.files || !d.files.length,
    () => demo.demoExport(payload),
  );
}
