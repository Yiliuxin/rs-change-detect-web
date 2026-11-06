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
  dongting: '洞庭湖 汛期→枯水期(水体消长)',
  taihu: '太湖 夏季作物→冬季裸地(太湖流域农田季相)',
  yellow_delta: '黄河三角洲 生长季→枯水期(湿地演变)',
  north_china: '华北平原 夏玉米→冬小麦(农田轮作)',
  sanjiang: '三江平原 水田生长期→收割后(水稻物候)',
  qinghaihu: '青海湖 高寒草甸 生长期→枯黄期(湖岸草甸季相)',
  hetao: '河套灌区 作物生长期→收割后(灌溉农业)',
  xilingol: '锡林郭勒草原 草生长期→枯黄期(草甸季相)',
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
// 批次 1 扩展（REQ-07）：payload 可选携带 index / threshold。
// 签名与 {data, demo} 返回结构不变；threshold 未提供时**不下发该字段**，
// 后端走 Otsu 自适应（threshold_source=otsu），缺省行为与历史版本一致。
export function runChange(payload) {
  const body = { ...(payload || {}) };
  body.index = String(body.index || 'NDVI').toUpperCase();
  if (body.threshold == null) delete body.threshold;
  else body.threshold = Number(body.threshold);
  return call(
    '/result/change',
    body,
    (d) => !d.run_id && !d.change_png && (!d.stats || !d.stats.length),
    () => demo.demoChange(body),
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

// ==========================================================================
// B 级扩展（可选）：在线取数 —— REQ-02 在线分支
//   POST /scene/discover            STAC 检索候选 tile（纯检索，不下载）
//   POST /scene/fetch               按需下载入库，返回 {fetch_id, status}
//   GET  /scene/fetch/{fetch_id}    查询进度 {status, progress, error, scene_ids}
// 与 5 个 MVP 接口并存但相互独立：不做演示降级（在线能力是可选的），
// 绝不把后端错误抛到全局（由调用方 main.js 在卡片内联优雅降级，不弹错误弹窗）。
//
// 统一返回「判别式结果」，调用方无需 try/catch 也能保证零 JS 异常：
//   { ok:true,  data }                                 成功
//   { ok:false, unavailable:true, status }            网络失败 / 裸 404（端点未启用）→ 卡片置灰降级
//   { ok:false, code, message, run_id, status }       后端业务错误体 {code,message,run_id} → 按 code 内联中文
//   { ok:false, transient:true }                      仅 pollFetch：单次 GET 抖动，可安全跳过本次、继续轮询
//
// 超时说明：onlineFetch 的超时「只作用于单次请求」，不包裹任何轮询循环；
//   - discover：STAC 两次检索（后端各 30s），放宽到 120000ms 避免慢出口误超时；
//   - startFetch：POST 仅受理建任务（后台线程下载），30000ms；
//   - pollFetch：沿用 TIMEOUT_MS(15000)，且只作用于这一发 GET；
//     main.js 每 2s 独立调用一次，长下载不会被 15s 整体掐断。
// ==========================================================================
const ONLINE_DISCOVER_TIMEOUT_MS = 120000;
const ONLINE_FETCH_START_TIMEOUT_MS = 30000;

function onlineFetch(path, options, timeoutMs) {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs);
  return fetch(BASE + path, { ...options, signal: ctrl.signal })
    .finally(() => window.clearTimeout(timer));
}

async function onlineCall(path, options, timeoutMs, transientOnFail) {
  let resp;
  try {
    resp = await onlineFetch(path, options, timeoutMs);
  } catch (_) {
    // 8000 未启动 / 单次超时 / 断网
    return transientOnFail
      ? { ok: false, transient: true }
      : { ok: false, unavailable: true, status: 0 };
  }
  if (resp.ok) {
    try {
      return { ok: true, data: await resp.json() };
    } catch (_) {
      return transientOnFail
        ? { ok: false, transient: true }
        : { ok: false, unavailable: true, status: resp.status };
    }
  }
  // 5xx 抖动在轮询场景视为可重试（长下载期间偶发代理/网关错误不中断流程）
  if (transientOnFail && resp.status >= 500) return { ok: false, transient: true };
  let body = null;
  try { body = await resp.clone().json(); } catch (_) { /* 非 JSON 错误页 */ }
  if (body && body.code) {
    return {
      ok: false,
      code: body.code,
      message: body.message,
      run_id: body.run_id || null,
      status: resp.status,
    };
  }
  // 裸 404 / 405 / 非契约错误页：在线端点未启用 → 优雅降级
  return transientOnFail
    ? { ok: false, transient: true }
    : { ok: false, unavailable: true, status: resp.status };
}

// REQ-02 在线分支：STAC 检索候选（复用 A 卡 AOI 的 bbox / aoi_geom + 两时相窗口）
export function discoverScenes(payload) {
  return onlineCall('/scene/discover', jsonOptions(payload), ONLINE_DISCOVER_TIMEOUT_MS, false);
}

// REQ-02 在线分支：按需下载入库（返回 fetch_id 后立即由轮询跟进）
export function startFetch(payload) {
  return onlineCall('/scene/fetch', jsonOptions(payload), ONLINE_FETCH_START_TIMEOUT_MS, false);
}

// REQ-02 在线分支：单次进度查询。15s 超时只作用于这一发 GET；
// 轮询循环在 main.js（每 2s 调一次），不会被 15s 包住导致长下载被中断。
export function pollFetch(fetchId) {
  return onlineCall(
    '/scene/fetch/' + encodeURIComponent(fetchId),
    { method: 'GET', headers: { 'Content-Type': 'application/json' } },
    TIMEOUT_MS,
    true,
  );
}

// ==========================================================================
// 批次 1 扩展（REQ-07/13/14）：面积统计 CSV + 历史 run 查询
// 与既有 5 接口 / 在线取数分支完全独立；沿用 fetchWithTimeout 与 safeError，
// 不改动 TIMEOUT_MS、演示降级层或任何既有导出函数的签名与返回结构。
// 后端业务错误体 {code,message,run_id} 一律抛 ApiError → 全局中文横幅；
// 非契约错误（超时 / 裸错误页）抛普通 Error，由调用方内联提示，不产生 JS 异常。
// ==========================================================================
async function apiGet(path, asBlob = false) {
  let resp;
  try {
    resp = await fetchWithTimeout(BASE + path, { method: 'GET' });
  } catch (_) {
    throw new Error('请求超时或后端不可达：' + path);
  }
  if (!resp.ok) {
    const err = await safeError(resp);
    if (err) throw err;
    throw new Error('请求失败（HTTP ' + resp.status + '）：' + path);
  }
  return asBlob ? resp.blob() : resp.json();
}

// GET /report/area_stats/{run_id}.csv → Blob（含 UTF-8 BOM），由调用方 Blob URL 同源下载
export function exportAreaStatsCsv(runId) {
  return apiGet('/report/area_stats/' + encodeURIComponent(runId) + '.csv', true);
}

// GET /runs?limit=&offset= → { total, limit, offset, items:[...] }（按 created_at 倒序）
export function listRuns(params = {}) {
  const q = new URLSearchParams();
  if (params.limit != null) q.set('limit', String(params.limit));
  if (params.offset != null) q.set('offset', String(params.offset));
  const qs = q.toString();
  return apiGet('/runs' + (qs ? '?' + qs : ''));
}

// GET /runs/{run_id} → 摘要 + params_snapshot 全量 + 全部产物下载 URL
export function getRunDetail(runId) {
  return apiGet('/runs/' + encodeURIComponent(runId));
}

// ==========================================================================
// 3A（REQ-13）：C 卡「后台运行」—— 变化检测异步受理 + 任务状态轮询
// 只新增 startAsyncChange / pollChangeTask 两个导出；既有所有导出签名、
// 返回结构、演示降级层、5 接口行为完全不动。
//
// 复用上面 onlineCall 的「判别式返回」（调用方无需 try/catch，零未捕获 JS 异常）：
//   { ok:true,  data }                             成功
//   { ok:false, unavailable:true, status }        网络失败 / 裸 404 / 非契约错误页
//   { ok:false, code, message, run_id, status }   后端业务错误体（如 EX7 并发超限）
//   { ok:false, transient:true }                  仅 pollChangeTask：单次 GET 抖动可跳过
//
// 超时口径（关键）：onlineCall → onlineFetch 内部为「单次请求」各起一个 AbortController
// 定时器，pollChangeTask 的 15s 只作用于这一发 GET；2s 轮询循环在 main.js 的
// setInterval 里，绝不把整个轮询循环包进 15s，长检测不会被整体掐断。
// ==========================================================================
const CHANGE_ASYNC_START_TIMEOUT_MS = 15000;

// POST /result/change（body 携带 async:true）→ {run_id, task_id, status:'PENDING'}
export function startAsyncChange(payload) {
  const body = { ...(payload || {}), async: true };
  return onlineCall('/result/change', jsonOptions(body), CHANGE_ASYNC_START_TIMEOUT_MS, false);
}

// GET /result/change/task/{task_id} → {task_id, status, progress, error, result}
// 15s 超时只作用于本次 GET；main.js 每 2s 独立调用一次。
export function pollChangeTask(taskId) {
  return onlineCall(
    '/result/change/task/' + encodeURIComponent(taskId),
    { method: 'GET', headers: { 'Content-Type': 'application/json' } },
    TIMEOUT_MS,
    true,
  );
}

// ==========================================================================
// 2B：样本「图上点选」—— POST /sample/from_lnglat（REQ-09）
// 只新增本导出；既有导出签名、演示降级层与 5 接口行为完全不动。
// 判别式返回（调用方无需 try/catch，绝不产生未捕获 JS 异常）：
//   { ok:true, data:{sample_id,px_x,px_y,ref_class} }   成功
//   { ok:false, code, message, run_id, status }          后端业务错误体（含 EX12 AOI 外）
//   { ok:false, unavailable:true, status }               网络失败 / 非契约错误页
// ==========================================================================
export async function fromLngLat(payload) {
  let resp;
  try {
    resp = await fetchWithTimeout(BASE + '/sample/from_lnglat', jsonOptions(payload));
  } catch (_) {
    return { ok: false, unavailable: true, status: 0 };
  }
  if (resp.ok) {
    try {
      return { ok: true, data: await resp.json() };
    } catch (_) {
      return { ok: false, unavailable: true, status: resp.status };
    }
  }
  const err = await safeError(resp);
  if (err) {
    return { ok: false, code: err.code, message: err.message, run_id: err.run_id, status: resp.status };
  }
  return { ok: false, unavailable: true, status: resp.status };
}
