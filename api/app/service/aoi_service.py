# -*- coding: utf-8 -*-
"""AOI 提交服务（REQ-01 / REQ-02 / REQ-14）。

编排：GeoJSON Polygon 解析（闭合/合法）→ 跨度校验（EX1）→ t2>t1 校验 →
PostGIS ``ST_Intersects`` 空间相交检索（**不按 scene.aoi_id 过滤**）→ 按日期配对 →
无匹配 EX2 → 生成 run_id/aoi_id，写 aoi + task（params_snapshot 含公式/版次/阈值/块大小）。
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Dict, List, Optional, Tuple

from app.algorithm.geotransform import BBox, validate_aoi_span
from app.api.errors import ApiError, as_api_error
from app.config.constants import (
    BLOCK_SIZE, MIN_SAMPLES_PER_CLASS, OTSU_FALLBACK_DNDVI,
    RADIOMETRIC_FORMULAS, VALID_RATIO_MIN, VALID_RATIO_WARN,
)
from app.repository import aoi_repo, scene_repo, task_repo
from app.service.cog_source import parse_cog_path
from app.service.common import git_short_commit

__all__ = ["select"]


def _parse_polygon(geom_str: str, run_id: str) -> Tuple[BBox, str, float]:
    """解析 GeoJSON Polygon（WGS84，闭合）→ (BBox, 原始 JSON 串, area_deg2)。

    几何非法（非 Polygon / 不闭合 / 解析失败）→ EX2（400，"请调整 AOI"）。
    """
    try:
        obj = json.loads(geom_str)
    except Exception as exc:  # noqa: BLE001
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail=f"GeoJSON 解析失败：{exc}") from exc

    if not isinstance(obj, dict) or str(obj.get("type", "")).lower() != "polygon":
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail="aoi_geom 需为 GeoJSON Polygon")
    coords = obj.get("coordinates") or []
    if not coords or not isinstance(coords[0], list):
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail="Polygon 缺少外环坐标")
    ring = coords[0]
    if len(ring) < 4:
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail="Polygon 外环至少 4 个点")
    if tuple(ring[0][:2]) != tuple(ring[-1][:2]):
        raise ApiError("EX2", run_id=run_id, http_status=400,
                       detail="Polygon 外环必须闭合（首尾点相同）")

    xs = [float(p[0]) for p in ring]
    ys = [float(p[1]) for p in ring]
    try:
        bbox = BBox.from_sequence([min(xs), min(ys), max(xs), max(ys)])
    except ValueError as exc:
        raise ApiError("EX2", run_id=run_id, http_status=400, detail=str(exc)) from exc

    # 规范化回写：保证 ST_GeomFromGeoJSON 收到合法闭合 Polygon
    canonical = json.dumps(
        {"type": "Polygon", "coordinates": [[[float(p[0]), float(p[1])] for p in ring]]},
        ensure_ascii=False,
    )
    return bbox, canonical, bbox.area_deg2


def _pair_name(scene: dict) -> str:
    """从 cog_path 取组名（pair_poyang / pair_farmland / pair_nanchang）。"""
    return parse_cog_path(scene.get("cog_path", ""))[2]


def _pick_pair(scenes: List[dict], t1: date, t2: date) -> Optional[Tuple[dict, dict]]:
    """按 acq_date 精确匹配 t1/t2，且优先同组（同 cog_path 前缀）配对，避免跨组 EX4。"""
    s1 = str(t1)
    s2 = str(t2)
    cand1 = [s for s in scenes if s["acq_date"] == s1]
    cand2 = [s for s in scenes if s["acq_date"] == s2]
    if not cand1 or not cand2:
        return None
    for a in cand1:
        for b in cand2:
            if (a["scene_id"] != b["scene_id"]
                    and a["sensor"] == b["sensor"]
                    and _pair_name(a) == _pair_name(b)):
                return a, b
    # 兜底：同 sensor 的任意一对
    for a in cand1:
        for b in cand2:
            if a["scene_id"] != b["scene_id"] and a["sensor"] == b["sensor"]:
                return a, b
    return None


def _scene_vo(scene: dict) -> dict:
    """转 SceneVO 字段口径。"""
    return {
        "scene_id": scene["scene_id"],
        "sensor": scene["sensor"],
        "acq_date": scene["acq_date"],
        "cloud_cover": scene.get("cloud_cover"),
        "cog_path": scene["cog_path"],
        "proc_baseline": scene.get("proc_baseline"),
    }


def select(aoi_geom: str, t1: date, t2: date) -> dict:
    """AOI 提交与预置影像对匹配（REQ-01/02/14）。"""
    # run_id 先生成，保证所有错误响应都能带上 run_id（REQ-14）
    run_id = str(uuid.uuid4())
    aoi_id = str(uuid.uuid4())
    try:
        bbox, geom_json, area_deg2 = _parse_polygon(aoi_geom, run_id)

        # REQ-01 / EX1：跨度 ≤ 0.25°
        validate_aoi_span(bbox)  # AoiTooLargeError.code = EX1

        # 时相合法性：t2 > t1（不臆造文案，复用 EX2「调整 AOI 或时相」）
        if t2 <= t1:
            raise ApiError("EX2", run_id=run_id, http_status=400,
                           detail=f"时相非法：t2({t2}) 须晚于 t1({t1})")

        # REQ-02：PostGIS 空间相交检索（按 footprint，不按 scene.aoi_id）
        intersecting = scene_repo.find_intersecting(geom_json)
        pair = _pick_pair(intersecting, t1, t2)
        if pair is None:
            raise ApiError("EX2", run_id=run_id)  # 404
        sc_t1, sc_t2 = pair

        sensor = sc_t1["sensor"]
        formula = RADIOMETRIC_FORMULAS.get(sensor, {})
        params_snapshot: Dict = {
            "run_id": run_id,
            "aoi_id": aoi_id,
            "aoi_wgs84": list(bbox.as_tuple()),
            "area_deg2": float(area_deg2),
            "t1": str(t1), "t2": str(t2),
            "sensor": sensor,
            # 公式/版次/阈值/块大小（REQ-14、NF9）——公式取常量源，绝不用 scene 表历史旧字段
            "formula_id": formula.get("formula_id"),
            "formula": formula.get("text"),
            "baselines": {"t1": sc_t1.get("proc_baseline"), "t2": sc_t2.get("proc_baseline")},
            "proc_baseline": {"t1": sc_t1.get("proc_baseline"), "t2": sc_t2.get("proc_baseline")},
            "otsu_fallback_dndvi": OTSU_FALLBACK_DNDVI,
            "block_size": BLOCK_SIZE,
            "valid_ratio_min": VALID_RATIO_MIN,
            "valid_ratio_warn": VALID_RATIO_WARN,
            "min_samples_per_class": MIN_SAMPLES_PER_CLASS,
            "index": "NDVI",
            "scene_t1_id": sc_t1["scene_id"],
            "scene_t2_id": sc_t2["scene_id"],
            "scene_cog_paths": {"t1": sc_t1["cog_path"], "t2": sc_t2["cog_path"]},
            "commit": git_short_commit(),
        }

        # 写 aoi + task（status=PENDING）
        aoi_repo.insert_aoi(aoi_id, geom_json, area_deg2)
        task_repo.insert_task(
            run_id, aoi_id, sc_t1["scene_id"], sc_t2["scene_id"],
            params_snapshot, commit_hash=params_snapshot["commit"],
        )

        return {
            "aoi_id": aoi_id,
            "run_id": run_id,
            "area_deg2": float(area_deg2),
            "matched_scenes": [_scene_vo(sc_t1), _scene_vo(sc_t2)],
        }
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一归一为带 run_id 的 ApiError
        raise as_api_error(exc, run_id=run_id) from exc
