// -*- coding: utf-8 -*-
// 全国地点库（省 / 地级市 / 区县 三级）+ 省级边界：静态 JSON 放在 public/geo/，
// 运行时按需 fetch 一次并缓存，纯离线、不发任何外部请求。
// 数据源为公开行政区划数据（含台湾省、港澳与南海诸岛边界），坐标 WGS84。
// 与 places.js 的关系：那里是 25 个「精选专题地点」（湖泊湿地/江段/农区，带精确 bbox），
// 这里提供的是全国行政区点位，用于自由选址，二者并存互不覆盖。

const _cache = {};

function geoUrl(file) {
  const base = (import.meta && import.meta.env && import.meta.env.BASE_URL) || '/';
  return base.replace(/\/+$/, '') + '/geo/' + file;
}

async function fetchJson(file) {
  if (_cache[file]) return _cache[file];
  const res = await fetch(geoUrl(file), { cache: 'force-cache' });
  if (!res.ok) throw new Error('加载 ' + file + ' 失败：HTTP ' + res.status);
  const data = await res.json();
  _cache[file] = data;
  return data;
}

// 返回 [{ name, lng, lat, tier, province }]，tier：1 省会/直辖 · 2 地级市 · 3 区县
export async function loadChinaPlaces() {
  const raw = await fetchJson('china-places.json');
  return (raw || []).map((r) => ({
    name: r[0], lng: r[1], lat: r[2], tier: r[3] || 3, province: r[4] || '',
  }));
}

// 省级边界 FeatureCollection（name 属性为省名，南海诸岛为空名要素）
export async function loadChinaProvinces() {
  return fetchJson('china-provinces.json');
}

function bareName(name) {
  return String(name || '')
    .replace(/(特别行政区|维吾尔自治区|壮族自治区|回族自治区|自治区|自治州|自治县|地区|林区|省|市|区|县|盟|旗)$/g, '');
}

// 地名搜索：完全匹配 > 前缀匹配 > 包含匹配，同级按名称排序，最多 limit 条
export function searchPlaces(list, query, limit = 8) {
  const q = String(query || '').trim();
  if (!q || !Array.isArray(list)) return [];
  const out = [];
  for (const p of list) {
    const bare = bareName(p.name);
    let score = -1;
    if (p.name === q || bare === q) score = 0;
    else if (bare.startsWith(q) || p.name.startsWith(q)) score = 1;
    else if (bare.includes(q) || p.name.includes(q)) score = 2;
    if (score >= 0) out.push([score, p]);
  }
  out.sort((a, b) => a[0] - b[0] || a[1].tier - b[1].tier
    || a[1].name.localeCompare(b[1].name, 'zh'));
  return out.slice(0, limit).map((x) => x[1]);
}

// 以点位为中心生成方形 AOI；后端限制单边 ≤0.25°，默认 0.2° 留余量
export function boxAround(lng, lat, size = 0.2) {
  const half = size / 2;
  return {
    w: Number((lng - half).toFixed(4)),
    s: Number((lat - half).toFixed(4)),
    e: Number((lng + half).toFixed(4)),
    n: Number((lat + half).toFixed(4)),
  };
}

// 层级文案，用于提示条与搜索结果副标题
export function tierLabel(tier) {
  return tier === 1 ? '省会 / 直辖市' : tier === 2 ? '地级市' : '区县';
}
