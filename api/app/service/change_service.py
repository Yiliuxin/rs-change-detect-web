# -*- coding: utf-8 -*-
"""变化检测主流程编排（REQ-03/04/05/06/07/08/11/12/14）。

照算法样板 ``app.algorithm._smoke.run_group`` 编排，去掉 print、返回结构化 dict：
  task 取 aoi/scenes → COG 解析(read_aoi) → 网格对齐(EX4) → 辐射口径(EX8/EX9) →
  掩膜 valid_ratio 四段判定(EX3/EX11 终止、EX5 告警可继续) → 先掩膜后缩放(NF4) →
  NDVI → ΔNDVI → Otsu(退化回退) → 三类分类 → 形态学 → 面积统计 →
  export_triplet 三件套 → 写 change_result/task 状态。

辐射定标铁律：一律 ``radiometric.to_reflectance(dn, sensor, proc_baseline)``，
绝不写 (DN-1000)/10000；公式常量取 ``constants.S2_FORMULA``（rho=DN/10000）。
"""
from __future__ import annotations

import json
from typing import Dict, Optional

import numpy as np

from app.algorithm.change_detector import (
    change_pixel_ratio, change_stats, classify_change, delta_ndvi,
)
from app.algorithm.cog_reader import read_aoi
from app.algorithm.composite import assert_grid_aligned, false_color, true_color
from app.algorithm.geotransform import BBox
from app.algorithm.index_raster import build_index_raster
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
    S2_FORMULA, VALID_RATIO_MIN, VALID_RATIO_WARN,
)
from app.repository import aoi_repo, change_result_repo, scene_repo, task_repo
from app.service.cog_source import resolve_bands
from app.service.common import artifact_url, run_dir, utc_now
from app.service.concurrency import serial

__all__ = ["load_scene", "run"]

_BANDS_10M = ("B02", "B03", "B04", "B08")


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


def run(run_id: str, index: str = "NDVI", block_size: int = BLOCK_SIZE) -> dict:
    """变化检测主流程（REQ-05/06/07/08/14）。"""
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
            for key in ("t1", "t2"):
                for band in ("B04", "B08"):
                    valid &= scenes[key]["dn"][band] > 0

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
            for key in ("t1", "t2"):
                baseline = scenes[key]["baseline"]
                rho[key] = {
                    band: _mask_then_scale(scenes[key]["dn"][band], sensor, baseline, valid)
                    for band in _BANDS_10M
                }

            # 真彩/假彩合成（验证 composite 纯函数链路，REQ-10）
            true_color(rho["t1"]["B04"], rho["t1"]["B03"], rho["t1"]["B02"], valid_mask=valid)
            false_color(rho["t1"]["B08"], rho["t1"]["B04"], rho["t1"]["B03"], valid_mask=valid)

            snapshot.update({
                "block_size": int(block_size),
                "index": str(index).strip().upper(),
                "sensor": sensor,
                "formula": S2_FORMULA,  # 常量源口径，绝不使用 scene 表历史旧字段
                "baselines": {k: scenes[k]["baseline"] for k in ("t1", "t2")},
                "valid_ratio": float(valid_ratio),
                "valid_ratio_code": verdict.code,
                "valid_ratio_min": VALID_RATIO_MIN,
                "valid_ratio_warn": VALID_RATIO_WARN,
            })

            # REQ-06 NDVI（MVP 仅 NDVI；build_index_raster 兼容 NDWI，契约不改）
            idx_t1 = build_index_raster(
                rho["t1"]["B04"], rho["t1"]["B08"], profile, valid_mask=valid,
                index=index, clip=True, run_id=run_id, params_snapshot=snapshot,
            )
            idx_t2 = build_index_raster(
                rho["t2"]["B04"], rho["t2"]["B08"], profile, valid_mask=valid,
                index=index, clip=True, run_id=run_id, params_snapshot=snapshot,
            )

            d = delta_ndvi(idx_t2.data, idx_t1.data)
            threshold, otsu_fallback, otsu_reason = otsu_threshold(d, valid_mask=valid)
            change_raw = classify_change(d, threshold, valid_mask=valid)
            change = clean_change_map(change_raw)
            summary = change_stats(change, profile=profile, valid_mask=valid,
                                   run_id=run_id, params_snapshot=snapshot)
            ratio_change = change_pixel_ratio(change, valid_mask=valid)
            stats = summary["stats"]

            snapshot.update({
                "otsu_threshold": float(threshold),
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
                "otsu_threshold": float(threshold),
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

            out_dir = run_dir(run_id)
            exported = export_triplet(
                change, profile, valid_ratio, report, out_dir,
                formats=EXPORT_FORMATS, run_id=run_id, params_snapshot=snapshot,
            )
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
            # 避免把本地绝对路径或 minio:// 自定义 scheme 暴露给前端（REQ-10/REQ-12）
            return {
                "run_id": run_id,
                "change_geotiff": artifact_url(run_id, "change.tif"),
                "change_png": artifact_url(run_id, "change.png"),
                "threshold": float(threshold),
                "valid_ratio": float(valid_ratio),
                "stats": stats,
                "params_snapshot": snapshot,
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
