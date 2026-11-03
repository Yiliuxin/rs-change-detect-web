# -*- coding: utf-8 -*-
"""算法模块 3+4 全链路 headless 自测（不连数据库、不写路由）。

链路：读 B02/B03/B04/B08 + SCL → 网格对齐(EX4) → 辐射定标(EX8/EX9) →
     掩膜(valid_ratio 单调四段 EX3/EX11/EX5/OK) → 先掩膜后缩放(NF4) →
     NDVI(物理定义域截断) → ΔNDVI → Otsu(退化回退) → 形态学去碎块 →
     三类变化图 → 精度(EX10 / OA / Kappa) → GeoTIFF + PNG + PDF 三件套。

用法（沿用既有 _API_ROOT sys.path 处理）：
    cd C:\\Test\\Web\\api\\app && python algorithm\\_smoke.py
    cd C:\\Test\\Web\\api     && python -m app.algorithm._smoke

run_id 确定性可复现：poyang_smoke / farmland_smoke，产物落 api/runs/<run_id>/。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 支持以脚本方式直接运行：把 api/ 目录加入模块搜索路径
_HERE = Path(__file__).resolve()
_API_ROOT = _HERE.parents[2]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

import numpy as np  # noqa: E402

from app.algorithm.accuracy import SampleInsufficientError, accuracy_report  # noqa: E402
from app.algorithm.change_detector import (  # noqa: E402
    CHANGE_CODES, CHANGE_LABELS_ZH, change_pixel_ratio, change_stats,
    classify_change, delta_ndvi,
)
from app.algorithm.cog_reader import read_aoi  # noqa: E402
from app.algorithm.composite import (  # noqa: E402
    GridMismatchError, assert_grid_aligned, false_color, true_color,
)
from app.algorithm.geotransform import BBox, validate_aoi_span  # noqa: E402
from app.algorithm.index_calculator import ndvi  # noqa: E402
from app.algorithm.index_raster import build_index_raster  # noqa: E402
from app.algorithm.mask_pipeline import (  # noqa: E402
    compute_valid_ratio, judge_valid_ratio, run_mask_pipeline,
)
from app.algorithm.morphology import clean_change_map  # noqa: E402
from app.algorithm.otsu import otsu_threshold  # noqa: E402
from app.algorithm.radiometric import (  # noqa: E402
    assert_same_caliber, check_baseline, to_reflectance,
)
from app.algorithm.renderer import build_change_canvas  # noqa: E402
from app.algorithm.writers import (  # noqa: E402
    ExportFormatError, export_triplet, valid_ratio_notice, validate_export_formats,
)
from app.config.constants import (  # noqa: E402
    AOI_MAX_SPAN_DEG, ATTRIBUTION_S2, ATTRIBUTION_S2_MODIFIED, BLOCK_SIZE,
    MIN_SAMPLES_PER_CLASS, OTSU_FALLBACK_DNDVI, S2_FORMULA, SENSOR_S2,
    VALID_RATIO_MIN, VALID_RATIO_WARN,
)

DATA_ROOT = _API_ROOT.parent / "data"
RUNS_ROOT = _API_ROOT / "runs"
BANDS = ("B02", "B03", "B04", "B08", "SCL")
GROUPS = (
    ("poyang_smoke", "pair_poyang_20230804_20231102"),
    ("farmland_smoke", "pair_farmland_20230918_20231227"),
)


def _pick(scene_files, band: str) -> str:
    """在 local_files 中挑选指定波段文件（B02/B03/B04/B08/SCL）。"""
    hits = [f for f in scene_files if f.upper().endswith(f"_{band}.TIF")]
    if not hits:
        raise FileNotFoundError(f"未找到 {band} 波段：{scene_files}")
    return hits[0]


def _sensor(scene: dict) -> str:
    """由 source_id 前缀锁定 sensor（S2A/S2B → S2），供 EX8 口径校验。"""
    sid = str(scene.get("source_id", "")).upper()
    return SENSOR_S2 if sid.startswith("S2") else sid.split("_")[0]


def _read_scene(pair_dir: Path, scene: dict, bbox: BBox) -> dict:
    paths = {band: pair_dir / _pick(scene["local_files"], band) for band in BANDS}
    baseline = scene.get("processing_baseline")
    check_baseline(SENSOR_S2, baseline)  # EX9：基线缺失 / < 04.00

    dn, profiles, ref_window = {}, {}, None
    for band in ("B02", "B03", "B04", "B08"):
        arr, profile, window = read_aoi(paths[band], bbox)
        dn[band] = arr
        profiles[band] = profile
        if band == "B04":
            ref_window = window

    ref_profile = profiles["B04"]
    # 同景四波段网格必须一致（10 m 全分辨率）
    for band in ("B02", "B03", "B08"):
        assert_grid_aligned(ref_profile, profiles[band])

    nodata_value = ref_profile.get("nodata")
    if nodata_value is not None:
        nodata_mask = np.zeros(dn["B04"].shape, dtype=bool)
        for band in ("B02", "B03", "B04", "B08"):
            nodata_mask |= dn[band] == nodata_value
    else:
        nodata_mask = None

    # SCL 为 20 m 半分辨率，流水线内部先重采样对齐到 10 m 网格
    masks, ratio_result = run_mask_pipeline(paths["SCL"], ref_profile,
                                            dn_nodata_mask=nodata_mask)
    return {
        "dn": dn, "profile": ref_profile, "paths": paths,
        "masks": masks, "ratio_result": ratio_result,
        "baseline": baseline, "window": ref_window,
    }


def _mask_then_scale(dn: np.ndarray, baseline: str, valid: np.ndarray) -> np.ndarray:
    """NF4：先掩膜（只取有效像元）后缩放（DN → ρ），掩膜外恒为 NaN。"""
    rho = np.full(dn.shape, np.float32(np.nan), dtype=np.float32)
    rho[valid] = to_reflectance(dn[valid], SENSOR_S2,
                                proc_baseline=baseline).astype(np.float32)
    return rho


def _build_samples(change: np.ndarray, per_class: int):
    """用变化图构造**可复核**的参考/预测样本（三类各 ≥ MIN_SAMPLES_PER_CLASS）。

    参考 = 变化图类别；预测 = 每 3 个样本确定性循环错分 1 个（1→2→3→1），
    使 OA/Kappa 非平凡且可复算。样本点按类内像素行主序等距抽取，无随机数。
    """
    flip = {1: 2, 2: 3, 3: 1}
    yt_parts, yp_parts, counts = [], [], {}
    for code in CHANGE_CODES:
        coords = np.argwhere(change == code)
        total = int(coords.shape[0])
        n = min(int(per_class), total)
        if total > n:
            idx = np.linspace(0, total - 1, n, dtype=np.int64)
            coords = coords[idx]
        yt = np.full(n, code, dtype=np.int64)
        yp = yt.copy()
        yp[::3] = flip[code]
        yt_parts.append(yt)
        yp_parts.append(yp)
        counts[int(code)] = n
    return np.concatenate(yt_parts), np.concatenate(yp_parts), counts


def _self_check() -> None:
    """纯函数边界自检（不落盘）：四段 / EX4 / EX10 / EX6 / Otsu 回退 / NDVI 零分母 / EX5 渲染。"""
    # ① valid_ratio 单调四段
    cases = (
        (0.0, "EX3", True),
        (VALID_RATIO_MIN / 2.0, "EX11", True),
        ((VALID_RATIO_MIN + VALID_RATIO_WARN) / 2.0, "EX5", False),
        (VALID_RATIO_WARN, "OK", False),
    )
    for ratio, code, terminate in cases:
        v = judge_valid_ratio(ratio)
        assert v.code == code and v.terminate == terminate, (ratio, v.code, v.terminate)

    # ② EX4 网格不一致
    base = {"transform": (10.0, 0.0, 100.0, 0.0, -10.0, 200.0),
            "width": 10, "height": 10, "crs": "EPSG:32650"}
    bad = dict(base, transform=(10.0, 0.0, 110.0, 0.0, -10.0, 200.0))
    assert assert_grid_aligned(base, dict(base)) is True
    try:
        assert_grid_aligned(base, bad)
        raise AssertionError("EX4 未触发")
    except GridMismatchError as exc:
        assert exc.code == "EX4"

    # ③ NDVI 分母零保护 + 常规数值
    assert np.isnan(ndvi(np.array([[0.0]], np.float32),
                         np.array([[0.0]], np.float32))[0, 0])
    assert abs(float(ndvi(np.array([[0.1]], np.float32),
                          np.array([[0.3]], np.float32))[0, 0]) - 0.5) < 1e-6

    # ④ Otsu 全 NaN / 单值退化 → 回退固定阈值
    for degenerate in (np.full((2, 2), np.nan, np.float32),
                       np.full((4, 4), 0.2, np.float32)):
        t, fb, reason = otsu_threshold(degenerate)
        assert fb and t == OTSU_FALLBACK_DNDVI and reason

    # ⑤ EX10：任一类 < 30
    yt = np.array([1] * (MIN_SAMPLES_PER_CLASS - 1) + [2] * 40 + [3] * 40)
    try:
        accuracy_report(yt, yt)
        raise AssertionError("EX10 未触发")
    except SampleInsufficientError as exc:
        assert exc.code == "EX10"

    # ⑥ EX6 导出白名单
    assert validate_export_formats(["geotiff", "png", "pdf"])
    try:
        validate_export_formats(["svg"])
        raise AssertionError("EX6 未触发")
    except ExportFormatError as exc:
        assert exc.code == "EX6"

    # ⑦ EX5 告警路径（纯构建画布，不落盘）+ 四段提示文案
    synthetic = np.full((8, 8), 3, np.uint8)
    synthetic[0, 0] = 1
    _, info = build_change_canvas(synthetic, (VALID_RATIO_MIN + VALID_RATIO_WARN) / 2.0)
    assert info["warn"] is True and info["code"] == "EX5"
    for ratio, code in ((0.0, "EX3"), (0.05, "EX11"), (0.30, "EX5"), (0.80, "OK")):
        assert valid_ratio_notice(ratio)["code"] == code
    assert "有效数据偏低，结论仅供参考" in valid_ratio_notice(0.30)["message_zh"]
    assert valid_ratio_notice(0.0)["terminate"] and valid_ratio_notice(0.05)["terminate"]

    # ⑧ 字体不可用 → 内置 5x7 ASCII 点阵退化路径确实画出像素文字
    wide = np.full((8, 400), 3, np.uint8)
    wide[0, 0] = 1
    # 中文 TTF 路径（当前环境 msyh.ttc 可用）：EX5 告警底框确实被绘制
    canvas_zh, info_zh = build_change_canvas(wide, 0.30)
    assert info_zh["warn"] is True
    if info_zh["text_mode"] == "ttf":
        banner = canvas_zh[44:68]
        assert int(((banner[:, :, 0] > 250) & (banner[:, :, 1] > 220)
                    & (banner[:, :, 1] < 245) & (banner[:, :, 2] < 200)).sum()) > 0
    canvas_fb, info_fb = build_change_canvas(wide, 0.30,
                                             font_path="C:/__no_such_font__.ttf")
    assert info_fb["degraded"] is True and info_fb["text_mode"] == "ascii5x7"
    top = canvas_fb[6:20]
    assert int(((top[:, :, 0] < 60) & (top[:, :, 1] < 60) & (top[:, :, 2] < 60)).sum()) > 0
    warn_band = canvas_fb[48:62]
    assert int(((warn_band[:, :, 0] > 120) & (warn_band[:, :, 1] < 90)
                & (warn_band[:, :, 2] < 90)).sum()) > 0

    # ⑨ PDF 中文字体注册失败 → 退 Helvetica 英文
    from app.algorithm.writers import _register_cjk

    font_name, cn_ok = _register_cjk("C:/__no_such_font__.ttc")
    assert font_name == "Helvetica" and cn_ok is False

    print(f"[self-check] 边界自检通过：EX3/EX11/EX5/OK 四段、EX4、EX10、EX6、"
          f"Otsu 回退、NDVI 零分母、valid_ratio 渲染回显")
    print(f"[self-check] 常量口径：BLOCK_SIZE={BLOCK_SIZE}  "
          f"MIN_SAMPLES_PER_CLASS={MIN_SAMPLES_PER_CLASS}  "
          f"OTSU_FALLBACK_DNDVI={OTSU_FALLBACK_DNDVI}  "
          f"VALID_RATIO_MIN={VALID_RATIO_MIN}  VALID_RATIO_WARN={VALID_RATIO_WARN}")
    print("-" * 78)


def run_group(run_id: str, pair_dir: Path) -> dict:
    manifest = json.loads((pair_dir / "manifest.json").read_text(encoding="utf-8"))
    bbox = BBox.from_sequence(manifest["aoi_wgs84"])
    validate_aoi_span(bbox)

    print("=" * 78)
    print(f"组：{run_id}  ({pair_dir.name})  — {manifest.get('label', '')}")
    print(f"AOI(WGS84)={bbox.as_tuple()}  跨度={bbox.span_x:.4f}°×{bbox.span_y:.4f}°"
          f"（上限 {AOI_MAX_SPAN_DEG}°）")
    print("-" * 78)

    scenes = {k: _read_scene(pair_dir, manifest["scenes"][k], bbox)
              for k in ("t1", "t2")}
    # REQ-03 / EX4：双时相左上角 / 尺寸 / CRS 必须一致
    assert_grid_aligned(scenes["t1"]["profile"], scenes["t2"]["profile"])
    # REQ-04 / NF5 / EX8：双时相辐射口径一致
    sensor_t1 = _sensor(manifest["scenes"]["t1"])
    sensor_t2 = _sensor(manifest["scenes"]["t2"])
    assert_same_caliber(sensor_t1, sensor_t2)
    print(f"[grid] t1/t2 profile 左上角/尺寸/CRS 一致 → assert_grid_aligned = True（EX4 未触发）")
    print(f"[caliber] sensor={sensor_t1}/{sensor_t2} → assert_same_caliber 一致（EX8 未触发）")

    for key in ("t1", "t2"):
        sc = manifest["scenes"][key]
        r = scenes[key]["ratio_result"]
        c = scenes[key]["masks"].counts()
        print(f"[{key}] {sc['date']} 基线={sc['processing_baseline']} "
              f"ρ={S2_FORMULA}  window={scenes[key]['window']}")
        print(f"    掩膜: 总={c['total']} 有效={c['valid']} 云={c['cloud']} "
              f"云影={c['cloud_shadow']} 雪={c['snow']} 无效={c['invalid']} "
              f"→ valid_ratio={r.valid_ratio:.4f} / {r.code}")

    profile = scenes["t1"]["profile"]
    # 双时相有效掩膜取交集，并剔除 NODATA(=0) 像元
    valid = scenes["t1"]["masks"].valid & scenes["t2"]["masks"].valid
    for key in ("t1", "t2"):
        for band in ("B04", "B08"):
            valid &= scenes[key]["dn"][band] > 0

    valid_ratio = compute_valid_ratio(valid)
    verdict = judge_valid_ratio(valid_ratio, mask=valid)
    print(f"[mask] 双时相交集 valid_ratio = {valid_ratio:.4f} → {verdict.code} "
          f"(terminate={verdict.terminate}, warn={verdict.warn}) {verdict.message}")

    result: dict = {"run_id": run_id, "group": pair_dir.name,
                    "valid_ratio": valid_ratio, "code": verdict.code,
                    "terminated": verdict.terminate}
    if verdict.terminate:
        print(f"[terminate] {verdict.code} 触发终止档：本组不产出三件套（如实报告，不伪造数据）")
        print()
        return result

    # NF4 先掩膜后缩放
    rho = {}
    for key in ("t1", "t2"):
        baseline = scenes[key]["baseline"]
        rho[key] = {
            band: _mask_then_scale(scenes[key]["dn"][band], baseline, valid)
            for band in ("B02", "B03", "B04", "B08")
        }

    # 真彩 / 假彩合成（用到 B02/B03，验证 composite 纯函数链路）
    tc = true_color(rho["t1"]["B04"], rho["t1"]["B03"], rho["t1"]["B02"], valid_mask=valid)
    fc = false_color(rho["t1"]["B08"], rho["t1"]["B04"], rho["t1"]["B03"], valid_mask=valid)
    print(f"[composite] true_color={tc.shape} false_color={fc.shape} dtype={tc.dtype}")

    year = str(manifest["scenes"]["t1"]["date"])[:4]
    params_snapshot = {
        "run_id": run_id,
        "group": pair_dir.name,
        "aoi_wgs84": bbox.as_tuple(),
        "sensor": sensor_t1,
        "formula": S2_FORMULA,
        "baselines": {k: scenes[k]["baseline"] for k in ("t1", "t2")},
        "dates": {k: manifest["scenes"][k]["date"] for k in ("t1", "t2")},
        "block_size": BLOCK_SIZE,
        "class_codes": {1: "增强", 2: "退化", 3: "稳定", 0: "无效"},
        "ndvi_domain": [-1.0, 1.0],
        "valid_ratio_min": VALID_RATIO_MIN,
        "valid_ratio_warn": VALID_RATIO_WARN,
        "min_samples_per_class": MIN_SAMPLES_PER_CLASS,
    }

    # REQ-06：NDVI（分母零保护 + 物理定义域截断，抑制暗水体近零分母伪影）
    idx_t1 = build_index_raster(rho["t1"]["B04"], rho["t1"]["B08"], profile,
                                valid_mask=valid, index="NDVI", clip=True,
                                run_id=run_id, params_snapshot=params_snapshot)
    idx_t2 = build_index_raster(rho["t2"]["B04"], rho["t2"]["B08"], profile,
                                valid_mask=valid, index="NDVI", clip=True,
                                run_id=run_id, params_snapshot=params_snapshot)
    print(f"[index] NDVI t1 {idx_t1.stats()}  t2 {idx_t2.stats()}")

    d = delta_ndvi(idx_t2.data, idx_t1.data)
    threshold, otsu_fallback, otsu_reason = otsu_threshold(d, valid_mask=valid)
    params_snapshot["otsu_threshold"] = threshold
    params_snapshot["otsu_fallback"] = otsu_fallback

    change_raw = classify_change(d, threshold, valid_mask=valid)
    change = clean_change_map(change_raw)
    summary = change_stats(change, profile=profile, valid_mask=valid,
                           run_id=run_id, params_snapshot=params_snapshot)
    ratio_change = change_pixel_ratio(change, valid_mask=valid)
    counts = {int(item["change_class"]): int(item["pixel_count"])
              for item in summary["stats"]}

    # REQ-09：用变化图构造可复核样本（三类各 ≥ 30）
    per_class = 2 * MIN_SAMPLES_PER_CLASS
    y_true, y_pred, sample_counts = _build_samples(change, per_class)
    try:
        acc = accuracy_report(y_true, y_pred, run_id=run_id,
                              params_snapshot=params_snapshot)
        oa, kappa, acc_note = acc["overall_accuracy"], acc["kappa"], acc["note"]
    except SampleInsufficientError as exc:
        acc = None
        oa = kappa = None
        acc_note = f"EX10 禁用计算精度：{exc}"
    params_snapshot["accuracy"] = acc

    report = {
        "run_id": run_id,
        "group": pair_dir.name,
        "year": year,
        "t1_date": manifest["scenes"]["t1"]["date"],
        "valid_ratio": valid_ratio,
        "valid_ratio_code": verdict.code,
        "otsu_threshold": threshold,
        "otsu_fallback": otsu_fallback,
        "otsu_reason": otsu_reason,
        "change_pixel_ratio": ratio_change,
        "stats": summary["stats"],
        "accuracy": acc,
        "params_snapshot": params_snapshot,
        "attribution_lines": [ATTRIBUTION_S2.format(year=year),
                              ATTRIBUTION_S2_MODIFIED.format(year=year)],
    }

    out_dir = RUNS_ROOT / run_id
    exported = export_triplet(change, profile, valid_ratio, report, out_dir,
                              run_id=run_id, params_snapshot=params_snapshot)

    print("-" * 78)
    print(f"[result] valid_ratio            = {valid_ratio:.4f}  ({verdict.code})")
    print(f"[result] Otsu 阈值              = {threshold:.4f}  "
          f"回退={otsu_fallback}  原因={otsu_reason}")
    print(f"[result] 变化像元占比           = {ratio_change:.4f}")
    print(f"[result] 三类像元数            = "
          + "  ".join(f"{c}{CHANGE_LABELS_ZH[c]}={counts[c]}" for c in CHANGE_CODES)
          + f"  (面积/ha: " + ", ".join(
              f"{int(it['change_class'])}={float(it['area_ha']):.2f}" for it in summary["stats"]) + ")")
    print(f"[result] 单像元面积             = {summary['pixel_area_m2']:.2f} m^2")
    print(f"[result] 精度样本(每类)         = {sample_counts}（由变化图确定性构造，非真实地面真值）")
    print(f"[result] OA                    = {oa if oa is None else round(oa, 4)}")
    print(f"[result] Kappa                 = {kappa if kappa is None else round(kappa, 4)}  {acc_note}")
    for fmt in ("geotiff", "png", "pdf"):
        print(f"[export] {fmt:<8} -> {exported['files'].get(fmt, '(未导出)')}")
    png_note = exported["notes"].get("png", {})
    pdf_note = exported["notes"].get("pdf", {})
    print(f"[export] PNG 文字={png_note.get('text_mode')} writer={png_note.get('writer')} "
          f"退化={png_note.get('degraded')} {png_note.get('reason', '')}")
    print(f"[export] PDF 字体={pdf_note.get('font_mode')} 退化={pdf_note.get('degraded')} "
          f"{pdf_note.get('reason', '')}")
    print()

    result.update({
        "terminated": False,
        "otsu_threshold": threshold,
        "otsu_fallback": otsu_fallback,
        "otsu_reason": otsu_reason,
        "change_pixel_ratio": ratio_change,
        "counts": counts,
        "oa": oa,
        "kappa": kappa,
        "acc_note": acc_note,
        "files": exported["files"],
        "png_note": png_note,
        "pdf_note": pdf_note,
    })
    return result


def main() -> int:
    print("=" * 78)
    print("算法模块3+4 全链路 smoke：指数 / 变化检测 / Otsu / 形态学 / 精度 / 渲染 / 报告")
    print(f"数据根={DATA_ROOT}  产物根={RUNS_ROOT}")
    print(f"阈值口径：VALID_RATIO_MIN={VALID_RATIO_MIN}（终止档）  "
          f"VALID_RATIO_WARN={VALID_RATIO_WARN}（告警可继续档）")
    _self_check()

    for run_id, dirname in GROUPS:
        pair_dir = DATA_ROOT / dirname
        if not (pair_dir / "manifest.json").exists():
            print(f"[error] 未找到影像对目录：{pair_dir}")
            return 1

    results = {run_id: run_group(run_id, DATA_ROOT / dirname)
               for run_id, dirname in GROUPS}

    print("=" * 78)
    print("汇总")
    for run_id, _ in GROUPS:
        r = results[run_id]
        if r.get("terminated"):
            print(f"  {run_id:<16} valid_ratio={r['valid_ratio']:.4f} → {r['code']} "
                  f"终止档（未产出三件套，如实报告）")
        else:
            print(f"  {run_id:<16} valid_ratio={r['valid_ratio']:.4f}  "
                  f"Otsu T={r['otsu_threshold']:.4f}(回退={r['otsu_fallback']})  "
                  f"变化占比={r['change_pixel_ratio']:.4f}  "
                  f"OA={r['oa']:.4f}  Kappa={r['kappa']:.4f}")

    p, f = results["poyang_smoke"], results["farmland_smoke"]
    if p.get("terminated") or f.get("terminated"):
        print("  结论校验：某组触发终止档，不做占比比较（如实报告）")
        return 0

    # 判据一（本质正确性，验变化方向）：鄱阳湖水退→滩地返青，NDVI 上升故以「增强」为主；
    # 农田水稻收割→枯黄，NDVI 下降故以「退化」为主。方向反了即说明 NDVI / ΔNDVI 语义错。
    p_cnt, f_cnt = p["counts"], f["counts"]
    ok_dir = (p_cnt[1] > p_cnt[2]) and (f_cnt[2] > f_cnt[1])
    # 判据二（防跑飞，验量级）：变化占比落在 2%~60% 的合理区间
    ok_ratio = all(0.02 <= r["change_pixel_ratio"] <= 0.60 for r in (p, f))

    print(f"  结论校验-方向：鄱阳湖 增强{p_cnt[1]}/退化{p_cnt[2]}、农田 增强{f_cnt[1]}/退化{f_cnt[2]}"
          f" → {ok_dir}（水退返青 NDVI 升 / 收割枯黄 NDVI 降）")
    print(f"  结论校验-量级：鄱阳湖 {p['change_pixel_ratio']:.4f}、农田 {f['change_pixel_ratio']:.4f}"
          f" → {ok_ratio}（农田为均质大田整片枯黄，占比高于局部水体消长属正常）")
    if not (ok_dir and ok_ratio):
        print("[FAIL] 变化方向或量级不合理，请检查 NDVI / 掩膜 / ΔNDVI / 阈值 / 形态学逻辑")
        return 1
    print("smoke 完成：两组全链路通过，三件套已落盘")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
