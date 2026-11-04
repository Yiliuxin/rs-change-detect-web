// pickPairs Node 侧验证：构造 22 景 / 11 组（新旧 pair 键混合）GET JSON，断言 11 个 pair 且 label 正确。
import { pickPairs } from '../src/api.js';

// 11 个分组键：3 个旧键 + 8 个新键
const KEYS = [
  ['poyang', '鄱阳湖 汛期→枯水期(水体消长)'],
  ['farmland', '赣中农田 生长期→枯黄期(NDVI 季相)'],
  ['nanchang', '南昌 初冬→深冬(城区/水体季相)'],
  ['dongting', '洞庭湖 汛期→枯水期(水体消长)'],
  ['taihu', '太湖 夏季→冬季(湖面与岸带季相)'],
  ['yellow_delta', '黄河三角洲 生长季→枯水期(湿地演变)'],
  ['north_china', '华北平原 夏玉米→冬小麦(农田轮作)'],
  ['sanjiang', '三江平原 水田生长期→收割后(水稻物候)'],
  ['qinghaihu', '青海湖 湖水位与草甸枯黄(高寒区)'],
  ['hetao', '河套灌区 作物生长期→收割后(灌溉农业)'],
  ['xilingol', '锡林郭勒草原 草生长期→枯黄期(草甸季相)'],
];

// 每组 2 景（t1 早 / t2 晚），共 22 景；故意乱序放入（t2 在前）
const scenes = [];
KEYS.forEach(([key], gi) => {
  const d1 = `2023-0${(gi % 8) + 1}-10`;
  const d2 = `2023-1${gi % 2}-20`;
  scenes.push({ scene_id: `S2A_${key}_T1`, sensor: 'S2', acq_date: d2, cloud_cover: 5 + gi, tile: `T${String(gi).padStart(2, '0')}`, proc_baseline: '05.09', cog_path: `/data/pair_${key}/t2/img.tif` });
  scenes.push({ scene_id: `S2B_${key}_T2`, sensor: 'S2', acq_date: d1, cloud_cover: 1 + gi * 0.5, tile: `T${String(gi).padStart(2, '0')}`, proc_baseline: '05.09', cog_path: `/data/pair_${key}/t1/img.tif` });
});

const payload = { count: scenes.length, scenes }; // 22 景
const pairs = pickPairs(payload);

let failed = 0;
const assert = (cond, msg) => { if (!cond) { failed++; console.error('FAIL:', msg); } };

assert(Array.isArray(pairs), 'pickPairs 应返回数组');
assert(pairs.length === 11, `应返回 11 组，实际 ${pairs.length}`);
assert(payload.count === 22, '输入应为 22 景');

const byKey = new Map(pairs.map((p) => [p.key, p]));
for (const [key, label] of KEYS) {
  const p = byKey.get(key);
  assert(!!p, `缺少分组 ${key}`);
  if (p) {
    assert(p.label === label, `${key} label 应为「${label}」，实际「${p.label}」`);
    assert(p.scenes && p.scenes.t1 && p.scenes.t2, `${key} 应含 t1/t2`);
    assert(p.scenes.t1.date <= p.scenes.t2.date, `${key} t1 应早于 t2`);
  }
}
// 未知键兜底（不应误用新键文案）
const unknown = pickPairs({ count: 2, scenes: [
  { scene_id: 'X1', sensor: 'S2', acq_date: '2023-01-01', tile: 'T99', cog_path: '/data/pair_mystery/t1/a.tif' },
  { scene_id: 'X2', sensor: 'S2', acq_date: '2023-06-01', tile: 'T99', cog_path: '/data/pair_mystery/t2/b.tif' },
] });
assert(!!unknown && unknown.length === 1, '未知键应兜底成组');
assert(unknown[0].label === 'mystery · 2023-01-01 → 2023-06-01', `未知键兜底 label 实际「${unknown[0].label}」`);
// 非法输入
assert(pickPairs(null) === null, 'null 应返回 null');
assert(pickPairs({}) === null, '空对象应返回 null');

if (failed) { console.error(`\n${failed} 项断言失败`); process.exit(1); }
console.log('PASS: 22 景 / 11 组全部配对成功，11 个 label 与要求逐字一致，未知键兜底与非法输入正常');
