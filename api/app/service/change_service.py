# -*- coding: utf-8 -*-
"""变化检测主流程编排（REQ-03/04/05/06/07/08/11/12/14）。

照算法样板 ``app.algorithm._smoke.run_group`` 编排，去掉 print、返回结构化 dict：
  task 取 aoi/scenes → COG 解析(read_aoi) → 网格对齐(EX4) → 辐射口径(EX8/EX9) →
  掩膜 valid_ratio 四段判定(EX3/EX11 终止、EX5 告警可继续) → 先掩膜后缩放(NF4) →
  NDVI / NDWI → Δ → Otsu(退化回退) 或手动阈值 → 三类分类 → 形态学 → 面积统计 →
  export_triplet 三件套 + 按 index 归档 + 双时相真彩合成 → 写 change_result/task 状态。

辐射定标铁律：一律 ``radiometric.to_reflectance(dn, sensor, proc_baseline)``，
绝不写 (DN-1000)/10000；公式常量取 ``constants.S2_FORMULA``（rho=DN/10000）。
"""
from __future__ import annotations

import json
import math
import shutil
from typing import Dict, Optional, Tuple

import numpy as np

from app.algorithm import index_calculator
from app.algorithm.change_detector import (
    change_pixel_ratio, change_stats, classify_change, delta_ndvi,
)
from app.algorithm.cog_reader import read_aoi
from app.algorithm.composite import (
    assert_grid_aligned, false_color, true_color, write_composite_png,
)
from app.algorithm.geotransform import BBox
from app.algorithm.index_raster import IndexRaster
from app.algorithm.mask_pipeline import (
    compute_valid_ratio, judge_valid_ratio, run_mask_pipeline,
)
from app.algorithm.morphology import clean_change_map
from app.algorithm.otsu import otsu_threshold
from app.algorithm.radiometric import assert_same_caliber, check_baseline, to_reflectance
from app.algorithm.writers import export_triplet
from app.api.errors import ApiError, as_api_error
from app.config.constants import (
    ATTRIBUTION_S2, ATTRIBUTION_S2_MODIFIED, BLOCK_SIZE, EXPORT_FORMATS,
    MANUAL_THRESHOLD_RANGE, S2_FORMULA, VALID_INDEXES,
    VALID_RATIO_MIN, VALID_RATIO_WARN,
)
from app.repository import aoi_repo, change_result_repo, scene_repo, task_repo
from app.service.cog_source import resolve_bands
from app.service.common import (
    artifact_manifest, artifact_url, index_artifact_names, run_dir, utc_now,
)
from app.service.concurrency import serial

__all__ = ["load_scene", "run"]

_BANDS_10M = ("B02", "B03", "B04", "B08")
# REQ-06/07：指数 → (band_a, band_b)，NDVI=(R,NIR)，NDWI=(G,NIR)。
# 仅波段对与所调用的 index_calculator 纯函数不同；后续 Δ/三类/统计口径完全一致。
_INDEX_BANDS: Dict[str, Tuple[str, str]] = {
    "NDVI": ("B04", "B08"),
    "NDWI": ("B03", "B08"),
}


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


def _build_index(key: str, rho_t: Dict, profile: dict, valid: np.ndarray,
                 run_id: str, snapshot: Dict) -> IndexRaster:
    """REQ-06/07：NDVI 走 ``index_calculator.ndvi``，NDWI 走 ``index_calculator.ndwi``。

    两者共用 clip_index 物理定义域约束、有效掩膜与 IndexRaster 值对象；
    变化检测的 Δ / Otsu / 三类判别 / 形态学 / 面积统计对两种指数完全一致。
    """
    band_a, band_b = _INDEX_BANDS[key]
    calc = index_calculator.ndvi if key == "NDVI" else index_calculator.ndwi
    data = index_calculator.clip_index(
        calc(rho_t[band_a], rho_t[band_b], valid_mask=valid)
    )
    return IndexRaster(
        data=data, profile=dict(profile), name=key, valid_mask=valid,
        run_id=run_id, params_snapshot=dict(snapshot),
    )


def _mask_then_scale(dn: np.ndarray, sensor: str, baseline: Optional[str],
                     valid: np.ndarray) -> np.ndarray:
    """NF4：先掩膜（只取有效像元）后缩放（DN → ρ），掩膜外恒为 NaN。

    定标一律走 ``to_reflectance``，公式口径由 sensor 锁定（S2 → rho=DN/10000）。
    """
    rho = np.full(dn.shape, np.float32(np.nan), dtype=np.float32)
    rho[valid] = to_reflectance(dn[valid], sensor, proc_baseline=baseline).astype(np.float32)
    return rho


def load_scene(cog_path: str, sensor: str, baseline: Optional[str],
               bbox: BBox, run_id: str) -> Dict:
    """读取单时相 COG：5 波段 → 同景网格对齐(EX4) → 掩膜流水线 → 结构化 dict。

    供 change_service 与 scene_service(POST 补 clip_window/valid_ratio) 复用。
    """
    check_baseline(sensor, baseline)  # EX9：基线缺失 / <04.00
    paths = resolve_bands(cog_path, run_id)  # SYS-01：COG 5 波段解析失败

    dn, profiles, ref_window = {}, {}, None
    for band in _BANDS_10M:
        arr, profile, window = read_aoi(str(paths[band]), bbox)  # REQ-03 AOI 裁切
        dn[band] = arr
        profiles[band] = profile
        if band == "B04":
            ref_window = window

    ref_profile = profiles["B04"]
    for band in ("B02", "B03", "B08"):
        assert_grid_aligned(ref_profile, profiles[band])  # EX4

    nodata_value = ref_profile.get("nodata")
    if nodata_value is not None:
        nodata_mask = np.zeros(dn["B04"].shape, dtype=bool)
        for band in _BANDS_10M:
            nodata_mask |= dn[band] == nodata_value
    else:
        nodata_mask = None

    # SCL 20m → 10m 重采样对齐后构建掩膜
    masks, ratio_result = run_mask_pipeline(
        str(paths["SCL"]), ref_profile, dn_nodata_mask=nodata_mask
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
        threshold: Optional[float] = None) -> dict:
    """变化检测主流程（REQ-05/06/07/08/14）。

    参数扩展（向后兼容、缺省行为不变）：
      * ``index``：NDVI（缺省，红+近红外）/ NDWI（绿+近红外），非法 → EX12；
      * ``threshold``：提供时跳过 Otsu 直接作对称阈值，须落在 (-1, 1)，越界 → EX12；
        ``params_snapshot`` 记录 ``threshold`` 与 ``threshold_source``("manual"/"otsu")。
    """
    # 参数校验放在 try 之前：非法参数不应把 task 误标为 FAILED
    key = _validate_index(index, run_id)
    manual = _validate_threshold(threshold, run_id)
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

        with serial(run_id):  # REQ-13：同步阻塞、并发上限 1（超限 EX7）
            task_repo.update_status(run_id, "RUNNING", started_at=utc_now())

            scenes = {
                "t1": load_scene(sc_t1["cog_path"], sc_t1["sensor"],
                                 sc_t1.get("proc_baseline"), bbox, run_id),
                "t2": load_scene(sc_t2["cog_path"], sc_t2["sensor"],
                                 sc_t2.get("proc_baseline"), bbox, run_id),
            }
            # REQ-03 / EX4：双时相左上角/尺寸/CRS 一致
            assert_grid_aligned(scenes["t1"]["profile"], scenes["t2"]["profile"])

            # 双时相有效掩膜交集 + 剔除 NODATA(=0)
            valid = scenes["t1"]["masks"].valid & scenes["t2"]["masks"].valid
            for phase in ("t1", "t2"):
                for band in ("B04", "B08"):
                    valid &= scenes[phase]["dn"][band] > 0

            valid_ratio = compute_valid_ratio(valid)
            verdict = judge_valid_ratio(valid_ratio, mask=valid)

            # EX3(=0) / EX11(<0.10) 终止档：不产出结果，如实报告
            if verdict.terminate:
                snapshot["valid_ratio"] = float(valid_ratio)
                snapshot["valid_ratio_code"] = verdict.code
                task_repo.update_snapshot(run_id, snapshot)
                task_repo.update_status(run_id, "FAILED",
                                        finished_at=utc_now(), error_code=verdict.code)
                raise ApiError(verdict.code, run_id=run_id)

            profile = scenes["t1"]["profile"]

            # EX5(0.10–0.50) 告警可继续档：200 且 params_snapshot 带 warning，检测继续
            if verdict.warn:
                snapshot["warning"] = {"code": verdict.code, "message": verdict.message}

            # NF4：先掩膜后缩放
            rho = {}
            for phase in ("t1", "t2"):
                baseline = scenes[phase]["baseline"]
                rho[phase] = {
                    band: _mask_then_scale(scenes[phase]["dn"][band], sensor, baseline, valid)
                    for band in _BANDS_10M
                }

            # REQ-10：双时相真彩合成落盘 t1_composite.png / t2_composite.png
            # （批次 2 卷帘对比消费）；假彩合成仍按既有链路在 t1 验证纯函数。
            out_dir = run_dir(run_id)
            for tag in ("t1", "t2"):
                rgb = true_color(
                    rho[tag]["B04"], rho[tag]["B03"], rho[tag]["B02"],
                    valid_mask=valid,
                )
                write_composite_png(rgb, out_dir / f"{tag}_composite.png")
            false_color(rho["t1"]["B08"], rho["t1"]["B04"], rho["t1"]["B03"], valid_mask=valid)

            # 产物清单（REQ-10/12/14）：主三件套 + 带 index 小写后缀归档 + 双时相合成图。
            # NDVI 沿用历史主名（缺省行为不变）；NDWI 另存 change_ndwi.* 等，互不覆盖。
            artifacts = artifact_manifest(run_id, key)

            snapshot.update({
                "block_size": int(block_size),
                "index": key,
                "index_bands": list(_INDEX_BANDS[key]),
                "sensor": sensor,
                "formula": S2_FORMULA,  # 常量源口径，绝不使用 scene 表历史旧字段
                "baselines": {k: scenes[k]["baseline"] for k in ("t1", "t2")},
                "valid_ratio": float(valid_ratio),
                "valid_ratio_code": verdict.code,
                "valid_ratio_min": VALID_RATIO_MIN,
                "valid_ratio_warn": VALID_RATIO_WARN,
                "artifacts": artifacts,
            })

            # REQ-06/07：NDVI 走 ndvi(R,NIR)，NDWI 走 ndwi(G,NIR)；
            # 后续 Δ / 阈值 / 三类判别 / 形态学 / 面积统计对两种指数完全一致。
            idx_t1 = _build_index(key, rho["t1"], profile, valid, run_id, snapshot)
            idx_t2 = _build_index(key, rho["t2"], profile, valid, run_id, snapshot)

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
            change = clean_change_map(change_raw)
            summary = change_stats(change, profile=profile, valid_mask=valid,
                                   run_id=run_id, params_snapshot=snapshot)
            ratio_change = change_pixel_ratio(change, valid_mask=valid)
            stats = summary["stats"]

            snapshot.update({
                "threshold": float(thr),
                "threshold_source": threshold_source,
                "otsu_threshold": float(thr),
                "otsu_fallback": bool(otsu_fallback),
                "otsu_reason": str(otsu_reason),
                "change_pixel_ratio": float(ratio_change),
                "stats": stats,
                "pixel_area_m2": float(summary["pixel_area_m2"]),
            })

            year = str(snapshot.get("t1") or "")[:4] or "2023"
            report = {
                "run_id": run_id,
                "year": year,
                "t1_date": snapshot.get("t1"),
                "valid_ratio": float(valid_ratio),
                "valid_ratio_code": verdict.code,
                "index": key,
                "threshold": float(thr),
                "threshold_source": threshold_source,
                "otsu_threshold": float(thr),
                "otsu_fallback": bool(otsu_fallback),
                "otsu_reason": str(otsu_reason),
                "change_pixel_ratio": float(ratio_change),
                "stats": stats,
                "accuracy": None,  # 样本由 /accuracy/sample 提交，此处不伪造
                "params_snapshot": snapshot,
                "attribution_lines": [
                    ATTRIBUTION_S2.format(year=year),
                    ATTRIBUTION_S2_MODIFIED.format(year=year),
                ],
            }

            exported = export_triplet(
                change, profile, valid_ratio, report, out_dir,
                formats=EXPORT_FORMATS, run_id=run_id, params_snapshot=snapshot,
            )
            # 归档一份带 index 小写后缀的三件套副本：NDVI / NDWI 各自留存，互不覆盖；
            # 主名三件套仍保持历史文件名，sample / report / e2e 链路不受影响。
            for src_name, dst_name in index_artifact_names(key).items():
                src = out_dir / src_name
                if src.is_file():
                    shutil.copy2(src, out_dir / dst_name)
            # 报告元数据落盘，供 /report/export 复用（不伪造数据）
            (out_dir / "run_report.json").write_text(
                json.dumps(report, ensure_ascii=False, default=str), encoding="utf-8"
            )

            change_geotiff_path = str(exported["files"]["geotiff"])
            change_png_path = str(exported["files"]["png"])

            # REQ-08/11/12：三类统计幂等入库 + task 终态
            # change_result 表仍登记本地绝对路径，schema / 仓储层均不改
            change_result_repo.upsert_stats(run_id, stats, change_geotiff_path, change_png_path)
            task_repo.update_snapshot(run_id, snapshot)
            task_repo.update_status(run_id, "SUCCESS", finished_at=utc_now())

            # 响应改为同源相对 URL：浏览器 image source / <a download> 可直接加载，
            # 避免把本地绝对路径或 minio:// 自定义 scheme 暴露给前端（REQ-10/REQ-12）。
            # NDVI 保持历史主名（缺省行为不变）；NDWI 指向带后缀归档，避免互相覆盖。
            resp_names = index_artifact_names(key) if key != "NDVI" else {}
            return {
                "run_id": run_id,
                "change_geotiff": artifact_url(
                    run_id, resp_names.get("change.tif", "change.tif")),
                "change_png": artifact_url(
                    run_id, resp_names.get("change.png", "change.png")),
                "threshold": float(thr),
                "valid_ratio": float(valid_ratio),
                "stats": stats,
                "params_snapshot": snapshot,
                "index": key,
                "threshold_source": threshold_source,
                "artifacts": artifacts,
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
