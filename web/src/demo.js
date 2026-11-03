// -*- coding: utf-8 -*-
// 演示降级层：本地 seed 与占位产物（所有产出必须在页面上标注「演示数据」）。
// 数据来源：C:\Test\Web\data\ 下 3 组真实影像对 manifest.json 的元数据。
// 后端 5 个路由当前为空占位骨架、8000 端口可能未启动，故接口不可用/返回空占位时使用本层。

// ===== 稳定演示标识（贯穿 5 个界面，参数快照一致）=====
export const DEMO_RUN_ID = 'demo-run-0e7a7f9';
export const DEMO_AOI_ID = 'demo-aoi-50RMT';
export const DEMO_COMMIT = '0e7a7f9';
export const MIN_SAMPLES_PER_CLASS = 30; // 照抄 constants.py MIN_SAMPLES_PER_CLASS

// 三类变化编码（1 增强 / 2 退化 / 3 稳定），颜色即色带
export const CLASS_INFO = {
  1: { code: 1, name: '增强', ref: '增强', color: '#21ba45' },
  2: { code: 2, name: '退化', ref: '退化', color: '#e74c3c' },
  3: { code: 3, name: '稳定', ref: '稳定', color: '#5b7a99' },
};

// ===== 3 组真实影像对 seed =====
export const SEEDS = [
  {
    key: 'poyang',
    name: 'poyang',
    label: '鄱阳湖 汛期→枯水期(水体消长)',
    tile: '50RMT',
    aoi: [116.25, 29.15, 116.35, 29.25],
    center: [116.3, 29.2],
    scenes: {
      t1: { scene_id: 'S2B_50RMT_20230804_0_L2A', sensor: 'S2', date: '2023-08-04', cloud_cover: 9.557234, proc_baseline: '05.09', phase: '汛期(2023 夏)' },
      t2: { scene_id: 'S2B_50RMT_20231102_0_L2A', sensor: 'S2', date: '2023-11-02', cloud_cover: 0.005861, proc_baseline: '05.09', phase: '枯水期(2023 冬)' },
    },
  },
  {
    key: 'farmland',
    name: 'farmland',
    label: '赣中农田 生长期→枯黄期(NDVI 季相)',
    tile: '50RMS',
    aoi: [116.05, 28.25, 116.15, 28.35],
    center: [116.1, 28.3],
    scenes: {
      t1: { scene_id: 'S2A_50RMS_20230918_0_L2A', sensor: 'S2', date: '2023-09-18', cloud_cover: 1.601876, proc_baseline: '05.09', phase: '生长期(2023 夏)' },
      t2: { scene_id: 'S2A_50RMS_20231227_0_L2A', sensor: 'S2', date: '2023-12-27', cloud_cover: 0.003902, proc_baseline: '05.10', phase: '枯黄期(2023 冬)' },
    },
  },
  {
    // 第 3 组元数据读取自 data/pair_nanchang_50RMT_20231102_20231227/manifest.json
    key: 'nanchang',
    name: 'nanchang',
    label: '南昌 初冬→深冬(城区/水体季相)',
    tile: '50RMT',
    aoi: [116.485, 29.2842, 116.585, 29.3842],
    center: [116.535, 29.3342],
    scenes: {
      t1: { scene_id: 'S2B_50RMT_20231102_0_L2A', sensor: 'S2', date: '2023-11-02', cloud_cover: 0.005861, proc_baseline: '05.09', phase: '初冬(2023-11)' },
      t2: { scene_id: 'S2A_50RMT_20231227_0_L2A', sensor: 'S2', date: '2023-12-27', cloud_cover: 0.863699, proc_baseline: '05.10', phase: '深冬(2023-12)' },
    },
  },
];

// 预置时相清单（所有 seed 的成像日期去重升序）
export const PRESET_DATES = Array.from(
  new Set(SEEDS.flatMap((s) => [s.scenes.t1.date, s.scenes.t2.date])),
).sort();

// ===== 影像对列表（GET→POST 均失败时的本地 seed）=====
export function demoScenePairs() {
  return { pairs: SEEDS.map((s) => ({ ...s })) };
}

function sceneVO(seed, which) {
  const sc = seed.scenes[which];
  return {
    scene_id: sc.scene_id,
    sensor: sc.sensor,
    acq_date: sc.date,
    cloud_cover: sc.cloud_cover,
    cog_path: 'demo://scenes/' + sc.scene_id,
    proc_baseline: sc.proc_baseline,
  };
}

function bboxFromGeom(str) {
  try {
    const g = JSON.parse(str);
    const ring = g.coordinates[0];
    const xs = ring.map((p) => p[0]);
    const ys = ring.map((p) => p[1]);
    return { w: Math.min(...xs), e: Math.max(...xs), s: Math.min(...ys), n: Math.max(...ys) };
  } catch (_) {
    return { w: 116.25, s: 29.15, e: 116.35, n: 29.25 };
  }
}

// POST /api/v1/aoi/select 降级
export function demoAoiSelect(payload) {
  const b = bboxFromGeom(payload && payload.aoi_geom);
  const areaDeg2 = Math.max(0, (b.e - b.w) * (b.n - b.s));
  return {
    aoi_id: DEMO_AOI_ID,
    run_id: DEMO_RUN_ID,
    area_deg2: Number(areaDeg2.toFixed(6)),
    matched_scenes: SEEDS.flatMap((s) => [sceneVO(s, 't1'), sceneVO(s, 't2')]),
  };
}

// POST /api/v1/scene/match 降级（口径确认：锁定公式与版次）
export function demoSceneMatch(payload) {
  void payload;
  return {
    run_id: DEMO_RUN_ID,
    formula_id: 'S2_L2A_BOA',
    proc_baseline: '05.09',
    clip_window: { row_off: 0, col_off: 0, width: 1098, height: 1098, crs: 'EPSG:4326', res: 0.0000909 },
    valid_ratio: 0.86,
  };
}

// POST /api/v1/result/change 降级（三类面积统计 / 有效像元占比 / 参数快照）
export function demoChange(payload) {
  void payload;
  return {
    run_id: DEMO_RUN_ID,
    change_geotiff: 'demo://results/change_demo.tif',
    change_png: '', // 空 → 前端用本地 canvas 生成三类色带 dataURL
    threshold: 0.1,
    valid_ratio: 0.86,
    stats: [
      { change_class: 1, pixel_count: 31240, area_ha: 312.4 },
      { change_class: 2, pixel_count: 18670, area_ha: 186.7 },
      { change_class: 3, pixel_count: 74290, area_ha: 742.9 },
    ],
    params_snapshot: {
      run_id: DEMO_RUN_ID,
      commit: DEMO_COMMIT,
      formula_id: 'S2_L2A_BOA',
      formula_text: 'rho=(DN-1000)/10000',
      proc_baseline: '05.09',
      threshold: 0.1,
      block_size: 256,
      index: 'NDVI',
      sensor: 'S2',
    },
  };
}

// POST /api/v1/accuracy/sample 降级（固定合理示例值，不冒充真实评价）
export function demoAccuracy(payload) {
  void payload;
  return {
    confusion_matrix: [[28, 2, 1], [3, 27, 2], [1, 2, 29]],
    overall_accuracy: 0.873,
    kappa: 0.809,
    note: null,
  };
}

// ===== 本地 canvas 生成三类色带占位专题图（dataURL）=====
let _pngDataUrl = null;

function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function makeChangePngDataUrl(size = 512) {
  if (_pngDataUrl) return _pngDataUrl;
  const c = document.createElement('canvas');
  c.width = size; c.height = size;
  const ctx = c.getContext('2d');
  ctx.fillStyle = CLASS_INFO[3].color; // 稳定灰蓝打底
  ctx.fillRect(0, 0, size, size);
  const rng = mulberry32(20231102);
  const palette = [CLASS_INFO[1].color, CLASS_INFO[2].color, CLASS_INFO[3].color];
  for (let i = 0; i < 260; i++) {
    const x = rng() * size, y = rng() * size;
    const w = 10 + rng() * 78, h = 10 + rng() * 78;
    const r = rng();
    const cls = r < 0.30 ? 0 : (r < 0.52 ? 1 : 2);
    ctx.globalAlpha = 0.45 + rng() * 0.5;
    ctx.fillStyle = palette[cls];
    ctx.beginPath();
    ctx.ellipse(x, y, w, h, rng() * Math.PI, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;
  // 细网格，方便辨认叠加边界
  ctx.strokeStyle = 'rgba(255,255,255,0.05)';
  ctx.lineWidth = 1;
  for (let i = 1; i < 8; i++) {
    const p = (size / 8) * i;
    ctx.beginPath(); ctx.moveTo(p, 0); ctx.lineTo(p, size); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, p); ctx.lineTo(size, p); ctx.stroke();
  }
  _pngDataUrl = c.toDataURL('image/png');
  return _pngDataUrl;
}

// ===== 演示下载产物（仅证明下载链路可用，明确标注演示）=====
let _pdfUrl = null;
let _tifUrl = null;

function makeDemoPdfUrl() {
  const lines = [
    'RS Change Detection Report',
    'DEMO DATA - NOT A REAL BACKEND RESULT',
    'run_id: ' + DEMO_RUN_ID,
    'commit: ' + DEMO_COMMIT,
    'formula: S2_L2A_BOA  rho=(DN-1000)/10000',
    'threshold: 0.10   block_size: 256   index: NDVI',
    'Valid ratio: 0.86  OA: 0.873  Kappa: 0.809',
    'Contains modified Copernicus Sentinel data [2023]',
  ];
  const esc = (s) => s.replace(/([\\()])/g, '\\$1');
  const stream = 'BT /F1 12 Tf 50 780 Td 18 TL ' + lines.map((l) => '(' + esc(l) + ') Tj T*').join(' ') + ' ET';
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
    '<< /Length ' + stream.length + ' >>\nstream\n' + stream + '\nendstream',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  ];
  let pdf = '%PDF-1.4\n';
  const offsets = [];
  objects.forEach((o, i) => {
    offsets.push(pdf.length);
    pdf += (i + 1) + ' 0 obj\n' + o + '\nendobj\n';
  });
  const xrefPos = pdf.length;
  pdf += 'xref\n0 ' + (objects.length + 1) + '\n0000000000 65535 f \n';
  offsets.forEach((off) => { pdf += String(off).padStart(10, '0') + ' 00000 n \n'; });
  pdf += 'trailer\n<< /Size ' + (objects.length + 1) + ' /Root 1 0 R >>\nstartxref\n' + xrefPos + '\n%%EOF';
  return URL.createObjectURL(new Blob([pdf], { type: 'application/pdf' }));
}

function makeDemoGeotiffUrl() {
  const txt = [
    'DEMO DATA - placeholder, NOT a real GeoTIFF',
    'run_id: ' + DEMO_RUN_ID,
    'This file only proves the download flow (REQ-12).',
  ].join('\n');
  return URL.createObjectURL(new Blob([txt], { type: 'application/octet-stream' }));
}

function demoFile(format) {
  const f = String(format || '').toLowerCase();
  if (f === 'png') {
    const url = makeChangePngDataUrl();
    return { url, size: url.length, filename: 'change_demo.png', placeholder: false };
  }
  if (f === 'pdf') {
    if (!_pdfUrl) _pdfUrl = makeDemoPdfUrl();
    return { url: _pdfUrl, size: 2048, filename: 'report_demo.pdf', placeholder: false };
  }
  if (f === 'geotiff') {
    if (!_tifUrl) _tifUrl = makeDemoGeotiffUrl();
    return { url: _tifUrl, size: 1024, filename: 'change_demo.tif', placeholder: true };
  }
  return null;
}

// POST /api/v1/report/export 降级
export function demoExport(payload) {
  const formats = (payload && payload.formats) || [];
  const files = formats.map((f) => {
    const d = demoFile(f) || { url: '', size: 0, filename: String(f), placeholder: true };
    return { format: f, url: d.url, size: d.size, filename: d.filename, placeholder: d.placeholder, demo: true };
  });
  return {
    files,
    run_id: DEMO_RUN_ID,
    commit: DEMO_COMMIT,
    attribution: 'Contains modified Copernicus Sentinel data [2023]',
  };
}
