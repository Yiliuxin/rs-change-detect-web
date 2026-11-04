// -*- coding: utf-8 -*-
// 仅调用 HLD 2.2.3 的 5 个接口（统一前缀 /api/v1），不直连算法/数据库。
// 所有接口都带「演示降级层」：网络失败 / 404-405 / 返回空占位 → 本地演示数据。
// 后端真实业务错误体 {code,message,run_id} 一律抛出 ApiError，交由全局横幅展示，绝不降级吞掉。

import { ApiError } from './errors.js';
import * as demo from './demo.js';

const BASE = '/api/v1';
const TIMEOUT_MS = 15000; // 重计算接口（POST /scene/match 真实读取 COG 算掩膜）放宽到 15s；constants.py API_TIMEOUT_SEC = 3 仅适用于轻接口，避免 2.5s 误超时降级

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
// 真实后端 GET /scene/match 返回 {count, scenes:[...]}：按 cog_path 的 pair_<name> 段分组，
// 组内按 acq_date 升序配成 t1（较早）/ t2（较晚），并用 cog_path 的 /t1/ /t2/ 标记校对兜底。
// 导出为纯函数便于 Node 侧无 DOM 验证；不改变 listScenePairs 的 via:'GET' 返回口径。
const PAIR_LABELS = {
  poyang: '鄱阳湖 汛期→枯水期(水体消长)',
  farmland: '赣中农田 生长期→枯黄期(NDVI 季相)',
  nanchang: '南昌 初冬→深冬(城区/水体季相)',
};

function pairGroupKey(sc) {
  const cp = sc && sc.cog_path;
  if (typeof cp === 'string') {
    const m = cp.match(/pair_([A-Za-z0-9_-]+)/);
    if (m) return { key: m[1], byTile: false };
  }
  // cog_path 缺失时回退按 tile 分组（加前缀避免与 pair_<name> 键冲突）
  if (sc && sc.tile) return { key: 'tile_' + sc.tile, byTile: true };
  return null;
}

function cogPhase(sc) {
  const cp = sc && sc.cog_path;
  if (typeof cp !== 'string') return null;
  if (/\/t1(?:\/|$)/.test(cp)) return 't1';
  if (/\/t2(?:\/|$)/.test(cp)) return 't2';
  return null;
}

function sceneVO(sc, phase) {
  return {
    scene_id: sc.scene_id,
    date: sc.acq_date,
    sensor: sc.sensor,
    cloud_cover: sc.cloud_cover,
    proc_baseline: sc.proc_baseline,
    phase,
    cog_path: sc.cog_path,
  };
}

export function pickPairs(j) {
  if (!j || typeof j !== 'object') return null;
  // 兼容演示降级层 / 旧契约：pairs 或 scene_pairs 直接透传
  if (Array.isArray(j.pairs) && j.pairs.length) return j.pairs;
  if (Array.isArray(j.scene_pairs) && j.scene_pairs.length) return j.scene_pairs;
  // 真实 GET 契约：必须同时具备非空 scenes 与 count，否则返回 null 继续走降级
  if (!Array.isArray(j.scenes) || !j.scenes.length || j.count == null) return null;

  const groups = new Map();
  for (const sc of j.scenes) {
    const g = pairGroupKey(sc);
    if (!g) continue;
    if (!groups.has(g.key)) groups.set(g.key, {
      byTile: g.byTile,
      scenes: [],
    });
    groups.get(g.key).scenes.push(sc);
  }

  const pairs = [];
  for (const [key, grp] of groups) {
    const arr = grp.scenes;
    if (arr.length < 2) continue; // 单景无法配成 t1/t2
    // 主规则：组内按 acq_date 升序，较早者 t1、较晚者 t2
    const sorted = arr.slice().sort((a, b) => String(a.acq_date || '').localeCompare(String(b.acq_date || '')));
    let t1 = sorted[0];
    let t2 = sorted[sorted.length - 1];
    // 校对/兜底：cog_path 明确带 /t1/ /t2/ 标记时以其为准
    const mk1 = arr.find((s) => cogPhase(s) === 't1');
    const mk2 = arr.find((s) => cogPhase(s) === 't2');
    if (mk1 && mk2) { t1 = mk1; t2 = mk2; }

    const tile = t1.tile || t2.tile || '—';
    const label = PAIR_LABELS[key] || (key + ' · ' + t1.acq_date + ' → ' + t2.acq_date);
    pairs.push({
      key,
      label,
      tile,
      scenes: { t1: sceneVO(t1, 't1'), t2: sceneVO(t2, 't2') },
    });
  }
  return pairs.length ? pairs : null;
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
