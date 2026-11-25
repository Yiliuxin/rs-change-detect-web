# -*- coding: utf-8 -*-
"""变化检测主流程编排（REQ-03/04/05/06/07/08/11/12/14）。

照算法样板 ``app.algorithm._smoke.run_group`` 编排，去掉 print、返回结构化 dict：
  task 取 aoi/scenes → COG 解析(read_aoi) → 网格对齐(EX4) → 辐射口径(EX8/EX9) →
  掩膜 valid_ratio 四段判定(EX3/EX11 终止、EX5 告警可继续) → 先掩膜后缩放(NF4) →
  NDVI / NDWI / NDBI / NDMI → Δ → Otsu(退化回退) 或手动阈值 → 三类分类 → 形态学 → 面积统计 →
  export_triplet 三件套 + 按 index 归档 + 双时相真彩合成 → 写 change_result/task 状态。

  NDBI / NDMI 用 B11 短波红外：load_scene(need_b11=True) 按需读取并与 10m 参考网格
  对齐，缺失 → EX11 中文明确报错；辅助指数一律查 constants.INDEX_AUXILIARY 映射。

辐射定标铁律：一律 ``radiometric.to_reflectance(dn, sensor, proc_baseline)``，
绝不写 (DN-1000)/10000；公式常量取 ``constants.S2_FORMULA``（rho=DN/10000）。
"""
from __future__ import annotations

import inspect
import json
import math
import shutil
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

from app.algorithm import index_calculator
from app.algorithm.change_detector import (
    CHANGE_NODATA, CHANGE_TRANSITION, M2_PER_HA, change_pixel_ratio,
    change_stats, classify_change, delta_ndvi, transition_band_edges,
    transition_band_mask, transition_band_stats,
)
from app.algorithm.cog_reader import read_aoi
from app.algorithm.geotransform import aoi_coverage
from app.algorithm.composite import (
    assert_grid_aligned, false_color, true_color, write_composite_png,
)
from app.algorithm.geotransform import BBox
from app.algorithm.index_raster import IndexRaster
from app.algorithm.intensity import intensity_stats
from app.algorithm.landcover import classify_landcover_indexed, transition_matrix
from app.algorithm.mask_builder import landsat_dn_nodata_mask
from app.algorithm.mask_pipeline import (
    compute_valid_ratio, judge_valid_ratio, run_mask_pipeline,
)
from app.algorithm.morphology import clean_change_map
from app.algorithm.otsu import otsu_threshold
from app.algorithm.patches import patch_stats
from app.algorithm.radiometric import (
    assert_same_caliber, check_baseline, to_reflectance,
)
from app.algorithm.renderer import render_change_overlay
from app.algorithm.cva_detector import detect_change_cva
from app.algorithm.regression_detector import detect_change_regression
from app.algorithm.writers import (
    attribution_lines, export_triplet, write_index_raster,
)
from app.api.errors import ApiError, as_api_error
from app.config.constants import (
    AOI_COVERAGE_DIAGNOSIS, AOI_COVERAGE_MIN, ERROR_MESSAGES,
    B11_MISSING_MESSAGE, BLOCK_SIZE, EXPORT_FORMATS, INDEX_AUXILIARY,
    INDEX_RASTER_T1, INDEX_RASTER_T2,
    LANDSAT_SENSORS, MANUAL_THRESHOLD_RANGE, METHOD_CVA, METHOD_OTSU,
    METHOD_REGRESSION,
    REGRESSION_MAX_ROUNDS, REGRESSION_SIGMA_FACTOR, radiometric_formula,
    SWIR_INDEXES, TRANSITION_BAND_RATIO, VALID_CHANGE_METHODS, VALID_INDEXES,
    VALID_RATIO_MIN, VALID_RATIO_WARN,
)
from app.repository import aoi_repo, change_result_repo, scene_repo, task_repo
from app.service.cog_source import resolve_bands, resolve_optional_bands
from app.service.common import (
    artifact_manifest, artifact_url, index_artifact_names, index_raster_names,
    regression_archive_names, regression_artifact_names, run_dir, utc_now,
)
from app.service.concurrency import serial

__all__ = ["load_scene", "run", "run_pipeline"]

_BANDS_10M = ("B02", "B03", "B04", "B08")
# REQ-06/07：指数 → (band_a, band_b)：NDVI=(B04,B08 红/近红外)，
# NDWI=(B03,B08 绿/近红外)，NDBI=(B11,B08 短波红外/近红外)，
# NDMI=(B08,B11 近红外/短波红外)。仅波段对与所调用的纯函数不同；
# 后续 Δ / 三类 / 统计口径完全一致。B11 **不**并入 _BANDS_10M（按需读取）。
_INDEX_BANDS: Dict[str, Tuple[str, str]] = {
    "NDVI": ("B04", "B08"),
    "NDWI": ("B03", "B08"),
    "NDBI": ("B11", "B08"),
    "NDMI": ("B08", "B11"),
}
# 4 指数 → index_calculator 纯函数分派（NDVI / NDWI 既有取值逐字不变）。
_INDEX_CALCS = {
    "NDVI": index_calculator.ndvi,
    "NDWI": index_calculator.ndwi,
    "NDBI": index_calculator.ndbi,
    "NDMI": index_calculator.ndmi,
}


def _ex11_coverage_diagnosis(b04_path: str, bbox: BBox) -> Optional[str]:
    """EX11 终止时补充「AOI 越界」诊断；非越界（云多）或算不出时返回 None。

    仅失败路径调用：重新打开已解析到本地的 B04 头算覆盖率（与在线取数同口径）。
    覆盖率低于常量下限时给出可执行中文诊断，帮助区分「AOI 超出影像」与「真云多」。
    任何读取异常都吞掉返回 None——诊断是附加信息，绝不阻断/改写原有 EX11 语义。
    """
    try:
        import rasterio  # 局部 import：诊断专用，正常检测路径零依赖开销

        with rasterio.open(str(b04_path)) as ds:
            info = aoi_coverage(ds, bbox)
        if float(info["coverage"]) >= AOI_COVERAGE_MIN:
            return None  # 覆盖率合格，EX11 由云/掩膜造成，不附误导性诊断
        return AOI_COVERAGE_DIAGNOSIS.format(
            pct=float(info["coverage"]) * 100.0,
            min=AOI_COVERAGE_MIN * 100.0,
            miss=float(info["missing_pct"]),
        )
    except Exception:  # noqa: BLE001 - 诊断尽力而为，绝不影响主错误
        return None


def _require_b11(cog_path: str, run_id: str, sensor: str) -> Path:
    """按需解析 B11 短波红外；缺失 → EX11 中文明确报错（REQ-07）。

    ``resolve_optional_bands`` 缺波段只返回空 dict、不抛 SYS-01，故此处统一转成
    EX11 风格报错（不改 ``ERROR_MESSAGES["EX11"]``，用 message 覆写为中文明确文案）。
    仅主指数为 NDBI / NDMI（SWIR_INDEXES）时调用，普通 NDVI / NDWI 链路不受牵连。
    """
    paths = resolve_optional_bands(str(cog_path), run_id, sensor, bands=("B11",))
    b11 = paths.get("B11")
    if b11 is None:
        raise ApiError("EX11", message=B11_MISSING_MESSAGE, run_id=run_id)
    return b11


def _validate_index(index: str, run_id: str) -> str:
    """index 归一化为大写并校验白名单；非法 → EX12（REQ-07）。"""
    key = str(index or "").strip().upper()
    if key not in VALID_INDEXES:
        raise ApiError("EX12", run_id=run_id,
                       detail=f"index 非法：{index!r}，仅支持 {list(VALID_INDEXES)}")
    return key


def _validate_threshold(threshold, run_id: str) -> Optional[float]:
    """可选手动阈值：须为有限数且落在 (-1, 1) 开区间；否则 → EX12（REQ-07）。"""
    if threshold is None:
        return None
    try:
        value = float(threshold)
    except (TypeError, ValueError) as exc:
        raise ApiError("EX12", run_id=run_id,
                       detail=f"threshold 非法：{threshold!r}（需为数字）") from exc
    lo, hi = MANUAL_THRESHOLD_RANGE
    if not (math.isfinite(value) and lo < value < hi):
        raise ApiError("EX12", run_id=run_id,
                       detail=f"threshold 越界：{value!r}，需满足 {lo} < T < {hi}")
    return value


def _validate_method(method: str, run_id: str) -> str:
    """method 归一化为小写并校验白名单；非法 → EX12（REQ-11 提前实现）。

    大小写不敏感（与 index 的 upper 规范化同风格）；缺省 ``otsu`` 时既有差值法
    行为逐字不变。``cva`` 为批次 G 新增的第三种方法，与既有两法共用同一白名单校验，
    非法值在 task 状态更新之前抛出，不会把 task 误标为 FAILED。
    """
    key = str(method or "").strip().lower()
    if key not in VALID_CHANGE_METHODS:
        raise ApiError("EX12", run_id=run_id,
                       detail=f"method 非法：{method!r}，仅支持 {list(VALID_CHANGE_METHODS)}")
    return key


def _index_histogram(values, valid_mask, threshold: float, name: str) -> dict:
    """指数差值 / 回归残差分布直方图（32 bin，仅统计有效且有限值像元）。

    供 PDF 内嵌佐证阈值合理性；``band_low`` / ``band_high`` 为批次 B1
    置信度过渡带上下界，直方图据此画 4 条竖线。无有效值返回空 dict
    （写 PDF 时如实跳过）。
    """
    arr = np.asarray(values, dtype=float)
    mask = np.asarray(valid_mask, dtype=bool)
    vals = arr[mask & np.isfinite(arr)]
    if vals.size == 0:
        return {}
    counts, edges = np.histogram(vals, bins=32)
    band_low, band_high = transition_band_edges(threshold)
    return {
        "name": str(name),
        "counts": [int(c) for c in counts],
        "edges": [float(e) for e in edges],
        "threshold": float(threshold),
        "valid_count": int(vals.size),
        "band_low": float(band_low),
        "band_high": float(band_high),
    }


def _build_conclusion(report: dict) -> Tuple[str, str]:
    """组装一句「结论」+ 一行纯 ASCII 可检索摘要（批次 B3）。

    数字**全部取自本轮 report**（禁止编造）：两期水体/植被面积 = 转移矩阵
    row_totals / col_totals × 单像元面积；主导转移方向 = top[0]；三类面积 =
    stats；阈值 / 方法 / 来源 = 本轮参数快照。缺任一必要字段返回 ("", "")，
    老 run 的 PDF「结果摘要」段据此整段跳过，绝不补写占位数字。
    """
    stats = report.get("stats") or []
    trans = report.get("transition_matrix")
    if not isinstance(stats, list) or not stats or not isinstance(trans, dict):
        return "", ""
    st: Dict[int, dict] = {}
    for item in stats:
        try:
            st[int(item.get("change_class", 0))] = item
        except (TypeError, ValueError, AttributeError):
            continue
    if not all(c in st for c in (1, 2, 3)):
        return "", ""
    row_totals = trans.get("row_totals") or []
    col_totals = trans.get("col_totals") or []
    if not (isinstance(row_totals, list) and len(row_totals) >= 2
            and isinstance(col_totals, list) and len(col_totals) >= 2):
        return "", ""
    snap = report.get("params_snapshot") or {}
    area_m2 = float(snap.get("pixel_area_m2") or 0.0)
    if area_m2 <= 0:
        return "", ""

    # LANDCOVER_CODES 前两类 = 1 水体 / 2 植被；行列合计直接取本轮矩阵
    def _water_veg_ha(totals) -> float:
        return (float(totals[0]) + float(totals[1])) * area_m2 / M2_PER_HA

    x, y = _water_veg_ha(row_totals), _water_veg_ha(col_totals)
    pct = ((y - x) / x * 100.0) if x > 0 else 0.0
    verb = "增至" if pct >= 0 else "减至"
    sign = "+" if pct >= 0 else "−"

    top = trans.get("top") or []
    d0 = top[0] if (isinstance(top, list) and top and isinstance(top[0], dict)) else {}
    dominant_zh = ""
    if d0:
        dominant_zh = (f"主导转移方向为 {d0.get('from_label', '—')} → "
                       f"{d0.get('to_label', '—')}（{float(d0.get('area_ha', 0.0)):.2f} ha）")
    method = str(report.get("method") or "otsu").lower()
    if method == METHOD_REGRESSION:
        method_zh = "回归残差法"
    elif method == METHOD_CVA:
        method_zh = "变化向量分析"
    else:
        method_zh = "Otsu 差值法"
    thr = float(report.get("threshold") or 0.0)
    src = str(report.get("threshold_source") or "")
    band = report.get("transition_band") or {}
    band_zh = ""
    if isinstance(band, dict) and band.get("pixel_count") is not None:
        band_zh = (f"；过渡带（证据不足，非第四类变化）{int(band['pixel_count'])} 像元、"
                   f"占 {float(band.get('ratio', 0.0)) * 100:.2f}%")
    t1, t2 = str(snap.get("t1") or ""), str(snap.get("t2") or "")
    idx = str(report.get("index") or "")
    area = {c: float(st[c].get("area_ha") or 0.0) for c in (1, 2, 3)}

    zh = (f"结论（{idx}，{t1} → {t2}）：水体/植被面积由 {x:.2f} ha {verb} {y:.2f} ha"
          f"（{sign}{abs(pct):.1f}%）；{dominant_zh}；本轮三类统计为增强 {area[1]:.2f} ha、"
          f"退化 {area[2]:.2f} ha、稳定 {area[3]:.2f} ha{band_zh}；"
          f"采用 {method_zh}，阈值 T={thr:.4f}（{src}），"
          f"过渡带带宽 ±{TRANSITION_BAND_RATIO:.2f}T。")
    ascii_line = (
        f"INDEX={idx} T1={t1} T2={t2} "
        f"WATER_VEG_T1_HA={x:.4f} WATER_VEG_T2_HA={y:.4f} DELTA_PCT={pct:+.2f} "
        f"DOMINANT={d0.get('label_en', 'NA')} "
        f"ENHANCED_HA={area[1]:.4f} DEGRADED_HA={area[2]:.4f} STABLE_HA={area[3]:.4f} "
        f"TRANSITION_PIXELS={int(band.get('pixel_count', 0)) if isinstance(band, dict) else 0} "
        f"THRESHOLD_T={thr:.4f} METHOD={method} SOURCE={src}"
    )
    return zh, ascii_line


def _stage_progress(on_stage, name: str, done=None, total=None) -> None:
    """B3：阶段内百分比回报；对只接受 ``name`` 的旧回调自动降级，向后兼容。

    ``on_stage(name, done, total)`` 为可选扩展；同步 / 历史调用传
    ``on_stage=lambda name: ...`` 时仍按单参数调用，不抛 TypeError。
    """
    if on_stage is None:
        return
    if done is None or total is None:
        on_stage(name)
        return
    try:
        params = list(inspect.signature(on_stage).parameters.values())
        positional = [p for p in params
                      if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                    inspect.Parameter.POSITIONAL_OR_KEYWORD)]
        supports = (any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params)
                    or len(positional) >= 3)
    except (TypeError, ValueError):
        supports = False
    if supports:
        on_stage(name, done, total)
    else:
        on_stage(name)


def _build_index(key: str, rho_t: Dict, profile: dict, valid: np.ndarray,
                 run_id: str, snapshot: Dict) -> IndexRaster:
    """REQ-06/07：按 key 分派 4 个 ``index_calculator`` 纯函数（NDVI/NDWI/NDBI/NDMI）。

    band_a / band_b 由 ``_INDEX_BANDS`` 给定；四者共用 clip_index 物理定义域约束、
    有效掩膜与 IndexRaster 值对象；后续 Δ / Otsu / 三类判别 / 形态学 / 面积统计
    对所有指数完全一致（NDVI / NDWI 取值逐字不变）。
    """
    band_a, band_b = _INDEX_BANDS[key]
    calc = _INDEX_CALCS[key]
    data = index_calculator.clip_index(
        calc(rho_t[band_a], rho_t[band_b], valid_mask=valid)
    )
    return IndexRaster(
        data=data, profile=dict(profile), name=key, valid_mask=valid,
        run_id=run_id, params_snapshot=dict(snapshot),
    )


def _build_pair_indexes(key: str, rho_t: Dict, profile: dict,
                        valid: np.ndarray, run_id: str,
                        snapshot: Dict) -> Dict:
    """A3/B2：按 INDEX_AUXILIARY 取主/辅助指数，并额外算齐 NDVI + NDWI。

    primary = ``_build_index(key)``；aux = ``_build_index(INDEX_AUXILIARY[key]).data``；
    NDVI / NDWI 主指数时 primary / aux 与既有值对象逐字一致（NDVI↔NDWI 互为辅助）。
    NDBI / NDMI 主指数时 aux 固定为 NDVI（需 B11 的分支由 rho_t["B11"] 供给）。
    ``ndvi`` / ``ndwi`` 数组恒返回，供 A3 判水体/植被；``primary`` 供 A3 第三类
    「裸土/建成」按主指数判据分类（NDVI 主与旧口径逐字一致）。
    """
    aux_key = INDEX_AUXILIARY[key]
    # 一次算齐 NDVI + NDWI（A3 联合判据对所有主指数保持既有口径，不臆造数值）
    ndvi = _build_index("NDVI", rho_t, profile, valid, run_id, snapshot)
    ndwi = _build_index("NDWI", rho_t, profile, valid, run_id, snapshot)
    caches = {"NDVI": ndvi, "NDWI": ndwi}
    # 需要 B11 的 NDBI / NDMI 按需补算；NDVI / NDWI 直接复用上面的值对象
    primary = caches.get(key) or _build_index(key, rho_t, profile, valid, run_id, snapshot)
    aux = caches.get(aux_key) or _build_index(aux_key, rho_t, profile, valid, run_id, snapshot)
    return {
        "primary": primary, "aux": aux.data,
        "ndvi": ndvi.data, "ndwi": ndwi.data,
    }


def _mask_then_scale(dn: np.ndarray, sensor: str, baseline: Optional[str],
                     valid: np.ndarray) -> np.ndarray:
    """NF4：先掩膜（只取有效像元）后缩放（DN → ρ），掩膜外恒为 NaN。

    定标一律走 ``to_reflectance``，公式口径由 sensor 锁定（S2 → rho=DN/10000）。
    """
    rho = np.full(dn.shape, np.float32(np.nan), dtype=np.float32)
    rho[valid] = to_reflectance(dn[valid], sensor, proc_baseline=baseline).astype(np.float32)
    return rho


def load_scene(cog_path: str, sensor: str, baseline: Optional[str],
               bbox: BBox, run_id: str, on_band=None,
               need_b11: bool = False) -> Dict:
    """读取单时相 COG：5 波段 → 同景网格对齐(EX4) → 掩膜流水线 → 结构化 dict。

    供 change_service 与 scene_service(POST 补 clip_window/valid_ratio) 复用。
    ``on_band(i)`` 为 B3 新增的可选回调（4 波段循环内回报阶段内进度），
    缺省 None 时既有调用签名 / 行为逐字不变。

    ``need_b11`` 仅主指数为 NDBI / NDMI（SWIR_INDEXES）时置 True：在 4 波段
    循环**之外**按需解析并读取 B11 短波红外，与 10m 参考网格做 EX4 几何对齐
    （B11 已由 fetch_b11 重采样对齐，无需再重采样），并并入 ``dn["B11"]``。
    B11 缺失 → EX11 中文明确报错（``_require_b11``）；``on_band`` 仍恒为
    2 时相 × 4 波段 = 8 个进度单位，默认 False 时既有行为逐字不变。
    """
    check_baseline(sensor, baseline)  # EX9：基线缺失 / <04.00 / 非 C2 L2
    # B11 按需解析（在 5 波段读取之前判定缺失）；默认 False 时本行完全跳过，
    # 普通 NDVI / NDWI 与 scene_service 调用逐字不变，绝不受 B11 牵连。
    b11_path = _require_b11(cog_path, run_id, sensor) if need_b11 else None
    # 按 scene 的 sensor 分派资产别名（L8/L9 → SR_B2..B5 / QA_PIXEL）
    paths = resolve_bands(cog_path, run_id, sensor)  # SYS-01：COG 5 波段解析失败
    is_landsat = str(sensor or "").strip().upper() in LANDSAT_SENSORS

    dn, profiles, ref_window = {}, {}, None
    for i, band in enumerate(_BANDS_10M, start=1):
        arr, profile, window = read_aoi(str(paths[band]), bbox)  # REQ-03 AOI 裁切
        dn[band] = arr
        profiles[band] = profile
        if band == "B04":
            ref_window = window
        if on_band is not None:
            on_band(i)  # B3：4 波段循环逐波段回报

    ref_profile = profiles["B04"]
    for band in ("B02", "B03", "B08"):
        assert_grid_aligned(ref_profile, profiles[band])  # EX4

    if need_b11:
        # B11 已在 10m 参考网格（原生 20m 经双线性重采样对齐），与 NDWI 的
        # B03 / B08 同窗口裁切 + 几何对齐口径一致；循环外单独读，不进 on_band。
        b11_arr, b11_profile, _b11_window = read_aoi(str(b11_path), bbox)
        assert_grid_aligned(ref_profile, b11_profile)  # EX4：B11 与参考网格对齐
        dn["B11"] = b11_arr

    if is_landsat:
        # Landsat C2 L2：SR 波段 DN=0 即 NODATA（不因 nodata 元信息缺失而漏掩）
        nodata_mask = landsat_dn_nodata_mask(dn, _BANDS_10M)
        qa_kind = "qa_pixel"
    else:
        nodata_value = ref_profile.get("nodata")
        if nodata_value is not None:
            nodata_mask = np.zeros(dn["B04"].shape, dtype=bool)
            for band in _BANDS_10M:
                nodata_mask |= dn[band] == nodata_value
        else:
            nodata_mask = None
        qa_kind = "scl"

    # NF4：先掩膜后缩放。SCL 20m→10m 重采样 / QA_PIXEL 位域解析，四段判定共用不改
    masks, ratio_result = run_mask_pipeline(
        str(paths["SCL"]), ref_profile, dn_nodata_mask=nodata_mask,
        qa_kind=qa_kind,
    )
    return {
        "dn": dn, "profile": ref_profile, "paths": paths,
        "masks": masks, "ratio_result": ratio_result,
        "baseline": baseline, "window": ref_window,
    }


def _resolve_bbox(task: dict) -> BBox:
    """优先用参数快照里的 aoi_wgs84，回退 aoi 表几何外接矩形。"""
    snapshot = task.get("params_snapshot") or {}
    seq = snapshot.get("aoi_wgs84")
    if seq:
        return BBox.from_sequence(list(seq))
    box = aoi_repo.find_bbox(task["aoi_id"])
    if not box:
        raise ValueError("task 关联的 AOI 不存在")
    return BBox.from_sequence(list(box))


def run(run_id: str, index: str = "NDVI", block_size: int = BLOCK_SIZE,
        threshold: Optional[float] = None, method: str = METHOD_OTSU) -> dict:
    """变化检测主流程（REQ-05/06/07/08/14），同步阻塞入口。

    保持既有语义不变：``with serial`` 占用唯一处理槽（REQ-13，超限 EX7），
    内部调用 :func:`run_pipeline` 执行完整管线。``method`` 缺省 ``otsu`` 时
    与历史版本逐字一致。
    """
    with serial(run_id):  # REQ-13：同步阻塞、并发上限 1（超限 EX7）
        return run_pipeline(run_id, index, block_size, threshold, method=method)


def run_pipeline(run_id: str, index: str = "NDVI", block_size: int = BLOCK_SIZE,
                 threshold: Optional[float] = None,
                 on_stage: Optional[object] = None,
                 method: str = METHOD_OTSU) -> dict:
    """变化检测完整管线（REQ-05/06/07/08/11/14），不含并发闸门。

    供同步入口 :func:`run` 与异步入口 ``change_async`` 复用。``on_stage(name)``
    为可选进度回调（异步模式用于推进 5 阶段进度），同步模式传 None 即空操作。
    B3 扩展：支持 ``on_stage(name, done, total)`` 回报阶段内百分比；只接受
    ``name`` 的旧回调自动降级为单参数调用，行为完全向后兼容。

    参数扩展（向后兼容、缺省行为不变，且 index / threshold / method / async 正交）：
      * ``index``：NDVI（缺省，红+近红外）/ NDWI（绿+近红外）/ NDBI（B11−B08，
        建筑）/ NDMI（B08−B11，水分），非法 → EX12；后两者需 B11，缺失 → EX11；
      * ``threshold``：提供时跳过 Otsu 直接作对称阈值，须落在 (-1, 1)，越界 → EX12；
        ``params_snapshot`` 记录 ``threshold`` 与 ``threshold_source``("manual"/"otsu")；
      * ``method``：``otsu``（缺省，差值法）/ ``regression``（REQ-11 回归残差法）/
        ``cva``（批次 G 变化向量分析，双波段欧氏幅值）；非法 → EX12，
        回归法产物带 ``_reg`` 后缀，不覆盖差值法三件套；CVA 沿用主名，
        只替换「幅值计算」这一步，其余管线与两法完全同口径。
    """
    # 参数校验：非法参数不应把 task 误标为 FAILED
    key = _validate_index(index, run_id)
    manual = _validate_threshold(threshold, run_id)
    method_key = _validate_method(method, run_id)
    # REQ-07：主指数为 NDBI / NDMI（SWIR_INDEXES）时需要 B11 短波红外。
    need_b11 = key in SWIR_INDEXES
    try:
        task = task_repo.get_task(run_id)
        if not task:
            raise ApiError("EX2", run_id=run_id,
                           detail=f"run_id 无对应 task：{run_id}")
        snapshot: Dict = dict(task.get("params_snapshot") or {})
        bbox = _resolve_bbox(task)

        sc_t1 = scene_repo.get_scene(task["scene_t1_id"])
        sc_t2 = scene_repo.get_scene(task["scene_t2_id"])
        if not sc_t1 or not sc_t2:
            raise ApiError("EX2", run_id=run_id)

        # REQ-04 / NF5 / EX8：双时相辐射口径一致
        sensor = assert_same_caliber(sc_t1["sensor"], sc_t2["sensor"])

        # B11 缺失预检：仅 need_b11 时执行，放在 RUNNING 状态更新之前（与「非法
        # 参数不误标 task 状态」既有风格一致）；NDVI / NDWI 链路完全不触发。
        if need_b11:
            for sc in (sc_t1, sc_t2):
                _require_b11(str(sc["cog_path"]), run_id, sc["sensor"])

        task_repo.update_status(run_id, "RUNNING", started_at=utc_now())

        # B3：读裁切阶段内进度 = 2 时相 × 4 波段 = 8 个单位（B11 循环外读，不占单位）
        def _band_cb(offset: int):
            return lambda i: _stage_progress(on_stage, "read_clip", offset + i, 8)

        scenes = {
            "t1": load_scene(sc_t1["cog_path"], sc_t1["sensor"],
                             sc_t1.get("proc_baseline"), bbox, run_id,
                             on_band=_band_cb(0), need_b11=need_b11),
            "t2": load_scene(sc_t2["cog_path"], sc_t2["sensor"],
                             sc_t2.get("proc_baseline"), bbox, run_id,
                             on_band=_band_cb(4), need_b11=need_b11),
        }
        _stage_progress(on_stage, "read_clip", 8, 8)  # 阶段 1：读裁切

        # REQ-03 / EX4：双时相左上角/尺寸/CRS 一致
        assert_grid_aligned(scenes["t1"]["profile"], scenes["t2"]["profile"])

        # 双时相有效掩膜交集 + 剔除 NODATA(=0)。
        # NDVI / NDWI 分支下面这段逐字不变；仅 need_b11（NDBI / NDMI）时把 B11
        # 有效性并入，避免 B11 的 NaN / 0 参与 Otsu 直方图。
        valid = scenes["t1"]["masks"].valid & scenes["t2"]["masks"].valid
        for phase in ("t1", "t2"):
            for band in ("B04", "B08"):
                valid &= scenes[phase]["dn"][band] > 0
        if need_b11:
            for phase in ("t1", "t2"):
                valid &= (np.isfinite(scenes[phase]["dn"]["B11"])
                          & (scenes[phase]["dn"]["B11"] > 0))
        _stage_progress(on_stage, "mask", 1, 3)

        valid_ratio = compute_valid_ratio(valid)
        _stage_progress(on_stage, "mask", 2, 3)
        verdict = judge_valid_ratio(valid_ratio, mask=valid)
        _stage_progress(on_stage, "mask", 3, 3)  # 阶段 2：掩膜

        # EX3(=0) / EX11(<0.10) 终止档：不产出结果，如实报告
        if verdict.terminate:
            snapshot["valid_ratio"] = float(valid_ratio)
            snapshot["valid_ratio_code"] = verdict.code
            err_message = None
            if verdict.code == "EX11":
                # 兜底诊断：若是在线取数遗留的「AOI 越界」坏数据，给出可执行建议；
                # 云多导致的 EX11 不附诊断（aoi_coverage≥下限返回 None）。
                try:
                    diag = _ex11_coverage_diagnosis(
                        str(scenes["t1"]["paths"]["B04"]), bbox
                    )
                except Exception:  # noqa: BLE001 - 诊断尽力而为
                    diag = None
                if diag:
                    snapshot["coverage_diagnosis"] = diag
                    err_message = ERROR_MESSAGES["EX11"] + "；" + diag
            task_repo.update_snapshot(run_id, snapshot)
            task_repo.update_status(run_id, "FAILED",
                                    finished_at=utc_now(), error_code=verdict.code)
            raise ApiError(verdict.code, run_id=run_id, message=err_message)

        profile = scenes["t1"]["profile"]

        # EX5(0.10–0.50) 告警可继续档：200 且 params_snapshot 带 warning，检测继续
        if verdict.warn:
            snapshot["warning"] = {"code": verdict.code, "message": verdict.message}

        # NF4：先掩膜后缩放。B11 与 10m 波段同走 _mask_then_scale → to_reflectance
        # （ρ=DN/10000，B11 为 DN 量级，不额外缩放）；NDVI / NDWI 分支仍仅 4 波段、
        # 逐字不变。need_b11 时 rho[phase] 额外含 "B11" 供 NDBI / NDMI 计算。
        rho = {}
        rho_bands = _BANDS_10M + (("B11",) if need_b11 else ())
        for phase in ("t1", "t2"):
            baseline = scenes[phase]["baseline"]
            rho[phase] = {
                band: _mask_then_scale(scenes[phase]["dn"][band], sensor, baseline, valid)
                for band in rho_bands
            }

        # REQ-10：双时相真彩合成落盘 t1_composite.png / t2_composite.png
        # （批次 2 卷帘对比消费）；假彩合成仍按既有链路在 t1 验证纯函数。
        out_dir = run_dir(run_id)
        for i, tag in enumerate(("t1", "t2"), start=1):
            rgb = true_color(
                rho[tag]["B04"], rho[tag]["B03"], rho[tag]["B02"],
                valid_mask=valid,
            )
            write_composite_png(rgb, out_dir / f"{tag}_composite.png")
            _stage_progress(on_stage, "index", i, 6)
        false_color(rho["t1"]["B08"], rho["t1"]["B04"], rho["t1"]["B03"], valid_mask=valid)
        _stage_progress(on_stage, "index", 3, 6)

        # 产物清单（REQ-10/12/14）：本轮主三件套 + 带 index / method 后缀归档 + 合成图。
        # 差值法 NDVI 沿用历史主名（缺省行为不变）；回归法改用 _reg 主名，互不覆盖。
        artifacts = artifact_manifest(run_id, key, method_key)
        out_names = (regression_artifact_names()
                     if method_key == METHOD_REGRESSION else None)

        snapshot.update({
            "block_size": int(block_size),
            "index": key,
            "method": method_key,
            "index_bands": list(_INDEX_BANDS[key]),
            "sensor": sensor,
            # 常量源按 sensor 锁定公式口径，绝不使用 scene 表历史旧字段
            "formula": str(radiometric_formula(sensor)["text"]),
            "baselines": {k: scenes[k]["baseline"] for k in ("t1", "t2")},
            "valid_ratio": float(valid_ratio),
            "valid_ratio_code": verdict.code,
            "valid_ratio_min": VALID_RATIO_MIN,
            "valid_ratio_warn": VALID_RATIO_WARN,
            "artifacts": artifacts,
        })

        # REQ-06/07：4 指数按 _INDEX_CALCS 分派（NDVI/NDWI/NDBI/NDMI）；
        # 后续 Δ / 阈值 / 三类判别 / 形态学 / 面积统计对所有指数完全一致。
        # A3/B2：主键走 _build_index(key)、辅助按 INDEX_AUXILIARY 取，且恒算齐
        # NDVI + NDWI 供 A3 联合判据；NDVI / NDWI 主检测指数口径逐字不变。
        _stage_progress(on_stage, "index", 4, 6)
        pair_t1 = _build_pair_indexes(key, rho["t1"], profile, valid, run_id, snapshot)
        _stage_progress(on_stage, "index", 5, 6)
        pair_t2 = _build_pair_indexes(key, rho["t2"], profile, valid, run_id, snapshot)
        idx_t1, idx_t2 = pair_t1["primary"], pair_t2["primary"]

        # B2 前置：探针只读 1 个像元的双时相指数栅格（回归法 _reg 隔离命名）。
        # 必须先落盘，否则 /result/change/probe 会缺产物而卡死。
        idx_names = index_raster_names(method_key)
        write_index_raster(pair_t1["primary"].data, pair_t1["aux"], profile,
                           out_dir / idx_names[INDEX_RASTER_T1])
        write_index_raster(pair_t2["primary"].data, pair_t2["aux"], profile,
                           out_dir / idx_names[INDEX_RASTER_T2])
        _stage_progress(on_stage, "index", 6, 6)  # 阶段 3：指数

        _stage_progress(on_stage, "change_detect", 1, 6)  # 阶段 4：变化检测
        comparison = None
        if method_key == METHOD_REGRESSION:
            # REQ-11：稳健拟合 index_t2 = a·index_t1 + b → 残差 r → Otsu / 手动阈值。
            reg = detect_change_regression(
                idx_t1.data, idx_t2.data, valid_mask=valid, threshold=manual,
            )
            thr = float(reg["threshold"])
            threshold_source = str(reg["threshold_source"])
            otsu_fallback = bool(reg["otsu_fallback"])
            otsu_reason = str(reg["otsu_reason"])
            change_raw = reg["change_raw"]

            # 报告对比表基线：差值法在同一 AOI / index / 手动阈值下重算，仅用于
            # 对比展示，不写主结果、不覆盖任何差值法产物。
            d_base = delta_ndvi(idx_t2.data, idx_t1.data)
            if manual is None:
                base_thr, base_fb, _ = otsu_threshold(d_base, valid_mask=valid)
                base_source = "otsu"
            else:
                base_thr, base_fb, base_source = float(manual), False, "manual"
            base_change = clean_change_map(
                classify_change(d_base, abs(float(base_thr)), valid_mask=valid)
            )
            base_stats = change_stats(
                base_change, profile=profile, valid_mask=valid,
                run_id=run_id, params_snapshot=snapshot,
            )["stats"]
        elif method_key == METHOD_CVA:
            # 批次 G：与回归法同源的 t1/t2 指数栅格（主指数 + 辅助指数双波段），
            # 逐波段差分取欧氏幅值 → Otsu / 手动幅值阈值；只新增「幅值计算」这一步。
            cva = detect_change_cva(
                (idx_t1.data, pair_t1["aux"]),
                (idx_t2.data, pair_t2["aux"]),
                valid_mask=valid, threshold=manual,
            )
            thr = float(cva["threshold"])
            threshold_source = str(cva["threshold_source"])
            otsu_fallback = bool(cva["otsu_fallback"])
            otsu_reason = str(cva["otsu_reason"])
            change_raw = cva["change_raw"]
            cva_hist = cva["signed_magnitude"]
            # NF9 可复现：幅值阈值与波段顺序入参数快照（不影响既有两法字段）。
            snapshot.update({
                "cva_threshold": float(thr),
                "cva_bands": [key, INDEX_AUXILIARY[key]],
            })
        else:
            d = delta_ndvi(idx_t2.data, idx_t1.data)
            if manual is None:
                thr, otsu_fallback, otsu_reason = otsu_threshold(d, valid_mask=valid)
                threshold_source = "otsu"
            else:
                thr = float(manual)
                otsu_fallback, otsu_reason = False, "手动阈值覆盖，跳过 Otsu 自适应（REQ-07）"
                threshold_source = "manual"
            # 对称阈值 T 需非负；(-1,1) 内负值取 |T| 判别等价，快照/响应如实记录原值。
            change_raw = classify_change(d, abs(thr), valid_mask=valid)

        _stage_progress(on_stage, "change_detect", 2, 6)
        # 形态学去碎块对两种检测方法完全一致（同一函数、同一参数）
        change = clean_change_map(change_raw)
        _stage_progress(on_stage, "change_detect", 3, 6)
        summary = change_stats(change, profile=profile, valid_mask=valid,
                               run_id=run_id, params_snapshot=snapshot)
        ratio_change = change_pixel_ratio(change, valid_mask=valid)
        stats = summary["stats"]
        _stage_progress(on_stage, "change_detect", 4, 6)

        # REQ-12 扩展：指数差值 / 回归残差 / CVA 幅值分布直方图（PDF 内嵌，佐证阈值合理性）。
        # 回归法残差 = index_t2 − (a·index_t1 + b)；差值法 = index_t2 − index_t1；
        # CVA = 定向欧氏幅值（|hist_src| 恒等于 CVA 幅值，过渡带口径 |CVA−T| ≤ 0.25T）。
        if method_key == METHOD_REGRESSION:
            hist_name = f"regression residual ({key})"
            hist_src = (idx_t2.data
                        - (float(reg["a"]) * idx_t1.data + float(reg["b"])))
        elif method_key == METHOD_CVA:
            hist_name = f"cva magnitude ({key})"
            hist_src = cva_hist
        else:
            hist_name = f"delta {key}"
            hist_src = d
        index_histogram = _index_histogram(hist_src, valid, thr, hist_name)

        # 批次 B1 置信度过渡带：| |Δ| − T | ≤ 0.25·T 的像元判定「证据不足」。
        # 口径见 constants.TRANSITION_BAND_*：过渡带不是第四类变化，这里只生成
        # 专题图渲染副本 + 单列 stats 字段，**不改写三类 change**——GeoTIFF、
        # 图斑统计、五档强度、精度评价、探针仍严格只有 1/2/3。
        band_mask = transition_band_mask(hist_src, thr, valid_mask=valid)
        band_mask &= (change != CHANGE_NODATA)   # nodata 永远不进过渡带
        band_marked = change.copy()
        band_marked[band_mask] = CHANGE_TRANSITION
        band_summary = transition_band_stats(
            band_mask, int(summary["valid_count"]),
            float(summary["pixel_area_m2"]), thr,
        )
        # 掩膜落盘：/report/export 重新生成 PNG / GeoJSON 时复用同一口径；
        # 老 run 无此文件则如实跳过过渡带呈现，不反推、不伪造。
        np.save(str(out_dir / "transition_band_mask.npy"), band_mask)

        # A1 变化强度分级：与三类严格同一阈值 T、同一差分源 hist_src
        # （回归法即残差）。传入形态学后的最终三类图作粗类权威，保证
        # 4+5 档 == 增强、1+2 档 == 退化、3 == 稳定，三类数字逐项自洽。
        intensity_summary = intensity_stats(
            hist_src, thr, change=change, valid_mask=valid,
            pixel_area_m2=float(summary["pixel_area_m2"]),
        )
        # A2 变化图斑统计：4 邻域连通域（与 GeoJSON shapes 默认连通性一致）
        patch_summary = patch_stats(
            change, profile, pixel_area_m2=float(summary["pixel_area_m2"]),
        )
        _stage_progress(on_stage, "change_detect", 5, 6)

        # A3 地物转移矩阵：水体(NDWI>0)/植被(NDVI≥0.30)恒定，第三类「裸土/建成」
        # 由主检测指数按 LANDCOVER_PRIMARY_CRITERIA 判定；NDVI 主逐字沿用旧口径。
        # 同一 valid 掩膜、同一像元面积，切换主指数后矩阵数字真实变化。
        lc_t1 = classify_landcover_indexed(
            pair_t1["ndvi"], pair_t1["ndwi"], pair_t1["primary"].data,
            key, valid_mask=valid)
        lc_t2 = classify_landcover_indexed(
            pair_t2["ndvi"], pair_t2["ndwi"], pair_t2["primary"].data,
            key, valid_mask=valid)
        transition_summary = transition_matrix(
            lc_t1, lc_t2, float(summary["pixel_area_m2"]), valid_mask=valid,
            index=key,
        )
        _stage_progress(on_stage, "change_detect", 6, 6)

        if method_key == METHOD_REGRESSION:
            comparison = {
                "difference": {
                    "method": METHOD_OTSU,
                    "threshold": float(base_thr),
                    "threshold_source": base_source,
                    "otsu_fallback": bool(base_fb),
                    "stats": base_stats,
                },
                "regression": {
                    "method": METHOD_REGRESSION,
                    "threshold": float(thr),
                    "threshold_source": threshold_source,
                    "otsu_fallback": bool(otsu_fallback),
                    "stats": stats,
                    "a": float(reg["a"]),
                    "b": float(reg["b"]),
                    "sigma": float(reg["sigma"]),
                    "rounds": int(reg["rounds"]),
                },
            }
            # REQ-14 可复现：method / a / b / sigma / 迭代轮数 / reg_threshold 全量入快照
            snapshot.update({
                "a": float(reg["a"]),
                "b": float(reg["b"]),
                "sigma": float(reg["sigma"]),
                "rounds": int(reg["rounds"]),
                "reg_threshold": float(thr),
                "regression_max_rounds": REGRESSION_MAX_ROUNDS,
                "regression_sigma_factor": REGRESSION_SIGMA_FACTOR,
                "regression_valid_count": int(reg["n_valid"]),
                "regression_inlier_count": int(reg["n_inliers"]),
                "comparison": comparison,
            })

        snapshot.update({
            "threshold": float(thr),
            "threshold_source": threshold_source,
            "otsu_threshold": float(thr),
            "otsu_fallback": bool(otsu_fallback),
            "otsu_reason": str(otsu_reason),
            "change_pixel_ratio": float(ratio_change),
            "stats": stats,
            "pixel_area_m2": float(summary["pixel_area_m2"]),
            # A1 / A2 / A3：透出点 ① 参数快照（历史回看从快照取）
            "intensity": intensity_summary,
            "patch_stats": patch_summary,
            "transition_matrix": transition_summary,
            # 批次 B1/B3：过渡带统计、Δ 直方图与结论走参数快照这一既有字段通道，
            # 历史 run 回看与前端结果卡不新增接口即可取到同源数据。
            "transition_band": band_summary,
            "index_histogram": index_histogram,
        })

        year = str(snapshot.get("t1") or "")[:4] or "2023"
        report = {
            "run_id": run_id,
            "year": year,
            "t1_date": snapshot.get("t1"),
            "valid_ratio": float(valid_ratio),
            "valid_ratio_code": verdict.code,
            "index": key,
            "method": method_key,
            "comparison": comparison,
            "threshold": float(thr),
            "threshold_source": threshold_source,
            "otsu_threshold": float(thr),
            "otsu_fallback": bool(otsu_fallback),
            "otsu_reason": str(otsu_reason),
            "change_pixel_ratio": float(ratio_change),
            "stats": stats,
            "index_histogram": index_histogram,
            "transition_band": band_summary,
            # A1 / A2 / A3：透出点 ② run_report.json（PDF 与报告导出复用）
            "intensity": intensity_summary,
            "patch_stats": patch_summary,
            "transition_matrix": transition_summary,
            "accuracy": None,  # 样本由 /accuracy/sample 提交，此处不伪造
            "params_snapshot": snapshot,
            # NF6 / NF7 常量逐字复用（PDF 署名页 / PNG / 导出响应同源）
            "attribution_lines": attribution_lines({"year": year}),
        }

        # 批次 B3：一句话结论只取本轮 stats / 转移矩阵 / 阈值方法（数字可追溯，
        # 禁止编造）；缺字段返回空串，老 run 的 PDF「结果摘要」段据此整段跳过。
        conclusion_zh, conclusion_ascii = _build_conclusion(report)
        report["conclusion"] = conclusion_zh
        report["conclusion_ascii"] = conclusion_ascii
        # 复用 params_snapshot 这一既有字段通道透给前端结果卡，不新增接口
        snapshot["conclusion"] = conclusion_zh

        _stage_progress(on_stage, "render_export", 1, 3)  # 阶段 5：渲染导出
        exported = export_triplet(
            change, profile, valid_ratio, report, out_dir,
            formats=EXPORT_FORMATS, run_id=run_id, params_snapshot=snapshot,
            names=out_names, render_change=band_marked,
        )
        _stage_progress(on_stage, "render_export", 2, 3)
        # 归档一份带 index 小写后缀的三件套副本：4 指数各自留存，互不覆盖；
        # 回归法从 _reg 主名复制到 <index>_reg 归档名，差值法主名保持历史文件名。
        archive_names = (regression_archive_names(key)
                         if method_key == METHOD_REGRESSION
                         else index_artifact_names(key))
        for src_name, dst_name in archive_names.items():
            src = out_dir / src_name
            if src.is_file():
                shutil.copy2(src, out_dir / dst_name)
        # 报告元数据落盘，供 /report/export 复用（不伪造数据）
        (out_dir / "run_report.json").write_text(
            json.dumps(report, ensure_ascii=False, default=str), encoding="utf-8"
        )
        _stage_progress(on_stage, "render_export", 3, 3)

        change_geotiff_path = str(exported["files"]["geotiff"])
        change_png_path = str(exported["files"]["png"])

        # 地图叠加专用纯分类图：尺寸严格等于变化栅格、无页眉/图例/署名。
        # 报告仍用 change.png（带制图要素成品图），二者用途分离。用 band_marked
        # 与 render_change 同一份数据，保证四类（含过渡带）着色与图例联动一致。
        overlay_meta = render_change_overlay(
            band_marked, out_dir / "change_overlay.png")

        # REQ-08/11/12：三类统计幂等入库 + task 终态
        # change_result 表仍登记本地绝对路径，schema / 仓储层均不改
        change_result_repo.upsert_stats(run_id, stats, change_geotiff_path, change_png_path)
        task_repo.update_snapshot(run_id, snapshot)
        task_repo.update_status(run_id, "SUCCESS", finished_at=utc_now())

        # 响应改为同源相对 URL：浏览器 image source / <a download> 可直接加载，
        # 避免把本地绝对路径或 minio:// 自定义 scheme 暴露给前端（REQ-10/REQ-12）。
        # NDVI 保持历史主名（缺省行为不变）；NDWI 指向带后缀归档，避免互相覆盖；
        # 回归法一律指向 _reg 主名，与差值法三件套物理隔离。
        if method_key == METHOD_REGRESSION:
            reg_names = regression_artifact_names()
            resp_names = {
                "change.tif": reg_names["geotiff"],
                "change.png": reg_names["png"],
            }
        else:
            resp_names = index_artifact_names(key) if key != "NDVI" else {}
        return {
            "run_id": run_id,
            "change_geotiff": artifact_url(
                run_id, resp_names.get("change.tif", "change.tif")),
            "change_png": artifact_url(
                run_id, resp_names.get("change.png", "change.png")),
            # 地图叠加专用：纯分类图（无制图要素），前端优先用它铺 AOI 层。
            # 老 run 无此产物时前端自动回退 change.png，历史行为不变。
            "change_overlay": artifact_url(run_id, "change_overlay.png"),
            "threshold": float(thr),
            "valid_ratio": float(valid_ratio),
            "stats": stats,
            "params_snapshot": snapshot,
            "index": key,
            "method": method_key,
            "threshold_source": threshold_source,
            "artifacts": artifacts,
            # A1 / A2 / A3：透出点 ③ 接口响应（新增字段带默认值，历史调用不受影响）
            "intensity": intensity_summary,
            "patch_stats": patch_summary,
            "transition_matrix": transition_summary,
            # 批次 B1/B3：过渡带单列统计与结论句（同源亦写入 params_snapshot）
            "transition_band": band_summary,
            "conclusion": conclusion_zh,
        }
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一封装为带 run_id 的 EX*/SYS-*
        mapped = as_api_error(exc, run_id=run_id)
        try:
            task_repo.update_status(run_id, "FAILED", finished_at=utc_now(),
                                    error_code=mapped.code)
        except Exception:
            pass
        raise mapped from exc
